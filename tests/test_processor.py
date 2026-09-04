"""Tests that exercise the real face detector against the repo's own images."""

import base64
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from src.processors.base import BaseProcessor
from src.processors.deal_with_it import (
    DealWithItProcessor,
    Face,
    Landmarks,
    NoFacesFound,
    detect,
    find_faces,
    landmarks_for,
    predictor_box,
)


def as_array(path) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(ImageOps.exif_transpose(img).convert('RGB'), dtype=np.uint8)


def decode_output(data_uri: str) -> Image.Image:
    payload = data_uri.split(',', 1)[1]
    return Image.open(BytesIO(base64.b64decode(payload)))


class TestBaseProcessor:
    def test_call_is_abstract(self, png_bytes):
        with pytest.raises(NotImplementedError):
            BaseProcessor(np.zeros((2, 2, 3), dtype=np.uint8)).call()

    def test_output_is_none_before_processing(self):
        assert BaseProcessor(np.zeros((2, 2, 3), dtype=np.uint8)).base64_output is None

    def test_encoding_is_memoised_on_the_instance(self):
        """Guards the fix for a functools.cache that leaked every processor."""
        processor = BaseProcessor(np.zeros((2, 2, 3), dtype=np.uint8))
        processor.output = Image.new('RGB', (2, 2), 'red')
        first = processor.base64_output
        assert first is processor.base64_output

        other = BaseProcessor(np.zeros((2, 2, 3), dtype=np.uint8))
        other.output = Image.new('RGB', (2, 2), 'blue')
        assert other.base64_output != first, 'each instance must encode its own output'

    def test_no_module_level_cache_retains_instances(self):
        """A cache keyed on self would keep the object alive after we drop it."""
        import gc
        import weakref

        processor = BaseProcessor(np.zeros((2, 2, 3), dtype=np.uint8))
        processor.output = Image.new('RGB', (2, 2), 'red')
        assert processor.base64_output is not None
        ref = weakref.ref(processor)

        del processor
        gc.collect()
        assert ref() is None


class TestDealWithItProcessor:
    def test_draws_glasses_on_a_single_face(self, single_face_image):
        original = as_array(single_face_image)
        processor = DealWithItProcessor(original)
        processor.call()

        assert processor.output is not None
        result = np.asarray(processor.output.convert('RGB'))
        assert result.shape == original.shape, 'the output keeps the original dimensions'
        assert not np.array_equal(result, original), 'something must have been drawn'

    def test_a_jpeg_output_is_a_jpeg_data_uri(self, single_face_image):
        processor = DealWithItProcessor(as_array(single_face_image))
        processor.img_format = 'JPEG'
        processor.call()
        assert processor.base64_output.startswith('data:image/jpeg;base64,')
        assert decode_output(processor.base64_output).format == 'JPEG'

    def test_output_is_a_png_data_uri(self, single_face_image):
        processor = DealWithItProcessor(as_array(single_face_image))
        processor.call()

        assert processor.base64_output.startswith('data:image/png;base64,')
        assert decode_output(processor.base64_output).format == 'PNG'

    def test_draws_on_every_face_in_a_group_photo(self, multiple_faces_image):
        original = as_array(multiple_faces_image)
        processor = DealWithItProcessor(original)
        processor.call()

        changed_rows = np.any(np.asarray(processor.output.convert('RGB')) != original, axis=(1, 2))
        assert changed_rows.sum() > 0
        assert len(find_faces(original)) > 1

    def test_raises_when_there_are_no_faces(self, faceless_image):
        processor = DealWithItProcessor(as_array(faceless_image))
        with pytest.raises(NoFacesFound, match='No faces were found'):
            processor.call()
        assert processor.output is None

    def test_reads_the_glasses_asset_once_per_process(self, single_face_image, monkeypatch):
        """The artwork used to be re-opened from disk for every face."""
        from src.processors import deal_with_it

        deal_with_it._glasses_source.cache_clear()
        read = []
        real_read = Path.read_text

        def counting_read(path, *args, **kwargs):
            read.append(path)
            return real_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'read_text', counting_read)
        processor = DealWithItProcessor(as_array(single_face_image))
        processor.call()
        assert len(read) == 1

    def test_the_glasses_are_drawn_at_the_size_the_face_needs(
            self, multiple_faces_image, monkeypatch):
        """The point of a vector: no 3000px raster scaled down per face.

        Eight faces of similar size should also share renders rather than ask
        for one apiece -- widths are rounded to a step for exactly that.
        """
        from src.processors import deal_with_it

        deal_with_it._render_glasses.cache_clear()
        widths = []
        real_render = deal_with_it._render_glasses.__wrapped__

        def recording_render(source, width):
            widths.append(width)
            return real_render(source, width)

        monkeypatch.setattr(deal_with_it, '_render_glasses', recording_render)
        DealWithItProcessor(as_array(multiple_faces_image)).call()

        assert widths, 'nothing was drawn'
        assert all(w <= deal_with_it.MAX_RENDER for w in widths)
        assert all(w % deal_with_it.RENDER_STEP == 0 for w in widths)
        assert max(widths) < 3000, 'the old raster was 3000px wide for every face'

    def test_missing_glasses_asset_is_an_error(self, single_face_image, tmp_path):
        processor = DealWithItProcessor(
            as_array(single_face_image), glasses_path=tmp_path / 'nope.svg'
        )
        with pytest.raises(OSError):
            processor.call()


class TestDetectionDownscaling:
    """Detection runs on a shrunken copy; the glasses go on the original."""

    def test_a_small_image_is_detected_at_full_size(self, single_face_image):
        original = as_array(single_face_image)
        assert max(original.shape[:2]) < 1600

        processor = DealWithItProcessor(original, max_detection_size=1600)
        assert processor._detection_input() == (original, 1.0)

    def test_a_large_image_is_shrunk_for_detection(self, single_face_image):
        original = as_array(single_face_image)
        processor = DealWithItProcessor(original, max_detection_size=300)

        shrunk, upscale = processor._detection_input()
        assert max(shrunk.shape[:2]) == 300
        assert upscale == pytest.approx(max(original.shape[:2]) / 300)

    def test_disabled_by_default(self, single_face_image):
        original = as_array(single_face_image)
        assert DealWithItProcessor(original)._detection_input() == (original, 1.0)

    def test_coordinates_are_mapped_back_onto_the_original(self, single_face_image):
        """Glasses drawn from downscaled landmarks must land where they would
        have landed at full resolution -- this is the mapping's regression test."""
        original = as_array(single_face_image)

        def drawn_region(cap):
            processor = DealWithItProcessor(original, max_detection_size=cap)
            processor.call()
            changed = np.any(np.asarray(processor.output.convert('RGB')) != original, axis=2)
            rows, cols = np.nonzero(changed)
            return np.array([cols.mean(), rows.mean()]), changed.sum()

        full_centre, full_area = drawn_region(0)
        small_centre, small_area = drawn_region(300)

        longest = max(original.shape[:2])
        drift = np.hypot(*(full_centre - small_centre))
        assert drift < longest * 0.03, f'glasses moved {drift:.0f}px'
        assert small_area == pytest.approx(full_area, rel=0.25)


#: What the detector finds in each sample, at the size the repo ships it.
#: Counts are resolution-dependent -- the Night Watch gives nine at 1600px and
#: eight at 1280 -- so these are the numbers for the files as committed.
SAMPLE_FACES = {
    'me.jpg': 1,
    'apollo_11_crew.jpg': 3,
    'multiple_people.jpg': 8,
    'solvay_1927.jpg': 29,
    'mona_lisa.jpg': 1,
    'girl_with_a_pearl_earring.jpg': 1,
    'american_gothic.jpg': 2,
    'the_syndics.jpg': 6,
    'the_night_watch.jpg': 19,
    'dog_handler.jpg': 1,
    # Nelson is a dog, and so are the poker players. The detector cannot
    # tell, and the captions say what it draws on.
    'princess_mary_and_nelson.jpg': 2,
    'dogs_playing_poker.jpg': 3,
    'cat_nap.jpg': 0,
    'van_gogh_self_portrait.jpg': 0,
    'the_scream.jpg': 0,
    'socks_the_cat.jpg': 0,
}

#: The words the tiles use, so the two cannot drift apart.
IN_WORDS = {
    0: 'No faces', 1: 'One face', 2: 'Two faces', 3: 'Three faces',
    6: 'Six faces', 8: 'Eight faces', 19: 'Nineteen faces', 29: 'Twenty-nine faces',
}


@pytest.mark.parametrize('filename,claimed', sorted(SAMPLE_FACES.items()))
def test_every_sample_has_the_faces_its_caption_claims(filename, claimed):
    """The tiles say "Three faces", "Twenty-nine faces" and so on.

    Pinned because the images are replaceable and the captions are not
    generated: resize one, swap one, or upgrade the detector, and the page
    would quietly start lying. Anyone who changes a picture has to change the
    number in src/ui.py:SAMPLES to match.
    """
    from tests.conftest import STATIC_IMG

    array = as_array(STATIC_IMG / filename)
    small, _ = DealWithItProcessor(array, max_detection_size=1600)._detection_input()
    assert len(find_faces(small)) == claimed


def test_every_sample_has_a_tile_to_show():
    """The grid shows `tiles/<stem>.webp`, not the sample itself: sixteen
    full-size JPEGs was 4.0 MB inside ~92 px squares. Add a sample without
    running scripts/make_tiles.py and the grid gets a broken image.
    """
    from PIL import Image

    from src.ui import SAMPLES
    from tests.conftest import STATIC_IMG

    for sample in SAMPLES.values():
        tile = STATIC_IMG / 'tiles' / f'{Path(sample.filename).stem}.webp'
        assert tile.is_file(), f'no tile for {sample.filename}'
        assert tile.stat().st_size < (STATIC_IMG / sample.filename).stat().st_size
        with Image.open(tile) as image:
            assert image.format == 'WEBP'
            assert image.size[0] == image.size[1], 'the CSS crops it square anyway'


def test_every_sample_is_credited():
    """These are other people's pictures. Shipping one without an attribution
    line is the kind of omission nobody notices until it matters."""
    from src.ui import SAMPLES
    from tests.conftest import STATIC_IMG

    credits = (STATIC_IMG / 'CREDITS.md').read_text()
    uncredited = [s.filename for s in SAMPLES.values() if s.filename not in credits]
    assert not uncredited, f'no credit for {uncredited}'


def test_the_captions_say_what_the_counts_are():
    """The count is written out in words on the tile; this is the join."""
    from src.ui import SAMPLES

    shipped = {sample.filename: sample.title for sample in SAMPLES.values()}
    assert shipped.keys() == SAMPLE_FACES.keys(), 'a sample was added or removed'
    for filename, faces in SAMPLE_FACES.items():
        assert shipped[filename] == IN_WORDS[faces], filename


@pytest.mark.faces
class TestThreadSafety:
    """The worker runs jobs in threads. The YuNet instance is not safe to
    share (``setInputSize`` mutates it); the shape predictor is."""

    def test_concurrent_detection_matches_sequential(self):
        import threading

        from tests.conftest import STATIC_IMG

        names = ['me.jpg', 'multiple_people.jpg', 'apollo_11_crew.jpg', 'american_gothic.jpg']
        arrays = {name: as_array(STATIC_IMG / name) for name in names}

        def analyse(array):
            return [(face, landmarks_for(array, face)) for face in find_faces(array)]

        expected = {name: analyse(array) for name, array in arrays.items()}
        seen = {}

        def detect_one(name):
            seen[name] = analyse(arrays[name])

        threads = [threading.Thread(target=detect_one, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert seen == expected


@pytest.mark.faces
class TestLandmarks:
    def test_sixty_eight_points_inside_the_face(self, single_face_image):
        array = as_array(single_face_image)
        face = find_faces(array)[0]
        marks = landmarks_for(array, face)
        assert len(marks.points) == 68
        top, right, bottom, left = face.box
        xs = [x for x, _ in marks.points]
        assert left - 20 < min(xs) and max(xs) < right + 20

    def test_the_eye_corners_bracket_the_eyes(self, single_face_image):
        array = as_array(single_face_image)
        face = find_faces(array)[0]
        marks = landmarks_for(array, face)
        assert marks.outer_left[0] < face.left_eye[0] < face.right_eye[0] < marks.outer_right[0]
        assert abs(marks.outer_left[1] - face.left_eye[1]) < 15

    def test_the_processor_keeps_what_it_found(self, multiple_faces_image):
        processor = DealWithItProcessor(as_array(multiple_faces_image))
        processor.call()
        assert len(processor.faces) == 8
        assert processor.detection == 'plain'
        record = processor.faces[0].as_record()
        assert set(record) == {'box', 'score', 'points', 'landmarks'}
        assert len(record['landmarks']) == 68
        assert set(record['points']) == {'left_eye', 'right_eye', 'nose', 'mouth_left',
                                         'mouth_right'}

    def test_me_jpg_is_barely_tilted(self, single_face_image):
        """The reference picture: the original placement found 2 degrees."""
        from math import atan2, degrees

        processor = DealWithItProcessor(as_array(single_face_image))
        processor.call()
        marks = processor.faces[0].landmarks
        dx = marks.outer_right[0] - marks.outer_left[0]
        dy = marks.outer_right[1] - marks.outer_left[1]
        assert abs(-int(degrees(atan2(dy, dx))) - 2) <= 1


@pytest.mark.faces
class TestAugmentation:
    def test_a_tiny_face_is_found_on_a_later_pass(self, single_face_image):
        """Shrunk until the plain pass misses it, a harder look finds it."""
        from PIL import Image as PILImage

        original = PILImage.open(single_face_image)
        for side in (120, 100, 80, 64, 48):
            small = np.asarray(original.resize((side, side)), dtype=np.uint8)
            if not find_faces(small):
                faces, how = detect(small)
                assert how in ('upscaled', 'equalised')
                assert len(faces) == 1
                top, right, bottom, left = faces[0].box
                assert 0 <= left < right <= side and 0 <= top < bottom <= side
                return
        pytest.skip('the plain pass found the face at every size tried')

    def test_a_blank_image_is_still_empty(self):
        faces, how = detect(np.full((200, 200, 3), 128, dtype=np.uint8))
        assert faces == [] and how == 'plain'


class TestPlacement:
    """Where the frame lands, from three landmarks. No detector needed."""

    @staticmethod
    def processor():
        return DealWithItProcessor(np.zeros((400, 400, 3), dtype=np.uint8))

    def test_the_left_lens_sits_on_the_left_eyes_outer_corner(self):
        proc = self.processor()
        img, (x, y) = proc._place((120, 200), (280, 200), (200, 250))
        assert x + int(img.width * proc.lens_offset) == 120
        assert y + int(img.height / 2) == 200

    def test_the_width_follows_the_eye_span(self):
        proc = self.processor()
        wide, _ = proc._place((100, 200), (300, 200), (200, 250))
        narrow, _ = proc._place((175, 200), (225, 200), (200, 212))
        assert wide.width == round(200 * proc.width_per_span)
        assert narrow.width == round(50 * proc.width_per_span)

    def test_a_tilted_head_gets_a_rotated_frame(self):
        proc = self.processor()
        level, _ = proc._place((120, 200), (280, 200), (200, 250))
        tilted, _ = proc._place((120, 180), (280, 220), (200, 250))
        assert tilted.height > level.height, 'rotate(expand=True) grows the canvas'
        span = ((280 - 120) ** 2 + (220 - 180) ** 2) ** 0.5
        assert tilted.width == round(proc.width_per_span * span), 'the rotated width fits the face'

    def test_a_turned_head_tapers_the_far_side(self):
        proc = self.processor()
        frontal, _ = proc._place((120, 200), (280, 200), (200, 250))
        turned, _ = proc._place((120, 200), (280, 200), (150, 250))
        alpha = np.asarray(turned)[:, :, 3]
        left_span = np.flatnonzero(alpha[:, 2] > 0)
        right_span = np.flatnonzero(alpha[:, -3] > 0)
        assert turned.height > frontal.height
        assert left_span.size < right_span.size, 'the far (left) edge is the short one'

    def test_a_frontal_face_is_not_tapered(self):
        proc = self.processor()
        img, _ = proc._place((120, 200), (280, 200), (200, 250))
        rendered = proc._rendered(img.width)
        assert img.height == pytest.approx(rendered.height * img.width / rendered.width, abs=1)

    def test_scaling_scales_every_point(self):
        face = Face(box=(150, 300, 300, 100), left_eye=(150.0, 200.0), right_eye=(250.0, 200.0),
                    nose=(200.0, 240.0), mouth_left=(160.0, 280.0), mouth_right=(240.0, 280.0),
                    score=0.9).scaled(2.0)
        assert face.left_eye == (300.0, 400.0) and face.mouth_right == (480.0, 560.0)
        assert face.box == (300, 600, 600, 200)
        marks = Landmarks(((1.0, 2.0),) * 68).scaled(3.0)
        assert marks.points[0] == (3.0, 6.0)

    def test_a_cat_stays_a_cat(self, faceless_image):
        """Equalising the contrast finds "faces" in the fur, below the bar."""
        faces, how = detect(as_array(faceless_image))
        assert faces == [] and how == 'plain'


class TestPredictorBox:
    """The shape predictor was trained on dlib's own detector boxes. It gets
    a square built from YuNet's points, not YuNet's taller box: on the
    Vermeer the raw box put the eye corner 34 px off."""

    face = Face(box=(0, 400, 500, 0), left_eye=(100.0, 200.0), right_eye=(200.0, 200.0),
                nose=(150.0, 250.0), mouth_left=(110.0, 300.0), mouth_right=(190.0, 300.0),
                score=0.9)

    def test_it_is_a_square_of_the_eye_distance(self):
        top, right, bottom, left = predictor_box(self.face)
        assert right - left == bottom - top == 240

    def test_it_is_centred_between_the_eyes_and_the_mouth(self):
        top, right, bottom, left = predictor_box(self.face)
        assert (left + right) / 2 == 150
        assert (top + bottom) / 2 == 200 + 0.3 * 100

    def test_a_degenerate_face_does_not_divide_by_zero(self):
        squashed = Face(box=(0, 1, 1, 0), left_eye=(5.0, 5.0), right_eye=(5.0, 5.0),
                        nose=(5.0, 5.0), mouth_left=(5.0, 5.0), mouth_right=(5.0, 5.0), score=1)
        top, right, bottom, left = predictor_box(squashed)
        assert right > left and bottom > top
