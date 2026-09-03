"""Tests that exercise the real face detector against the repo's own images."""

import base64
from io import BytesIO

import numpy as np
import pytest
from PIL import Image, ImageOps

from src.processors.base import BaseProcessor
from src.processors.deal_with_it import DealWithItProcessor, NoFacesFound


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
        # Independent count from the detector itself, so the assertion below
        # does not just restate the implementation.
        import face_recognition as fr
        assert len(fr.face_locations(original)) > 1

    def test_raises_when_there_are_no_faces(self, faceless_image):
        processor = DealWithItProcessor(as_array(faceless_image))
        with pytest.raises(NoFacesFound, match='No faces were found'):
            processor.call()
        assert processor.output is None

    def test_reads_the_glasses_asset_once_per_process(self, single_face_image, monkeypatch):
        """The template used to be re-opened from disk for every face."""
        from src.processors import deal_with_it

        deal_with_it._glasses_template.cache_clear()
        opened = []
        real_open = Image.open

        def counting_open(path, *args, **kwargs):
            if str(path).endswith('glasses.png'):
                opened.append(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Image, 'open', counting_open)
        processor = DealWithItProcessor(as_array(single_face_image))
        processor.call()
        assert len(opened) == 1

    def test_missing_glasses_asset_is_an_error(self, single_face_image, tmp_path):
        processor = DealWithItProcessor(
            as_array(single_face_image), glasses_path=tmp_path / 'nope.png'
        )
        with pytest.raises(OSError):
            processor.call()


class TestAngle:
    @pytest.mark.parametrize('left,right,expected', [
        ((0, 0), (100, 0), 0),
        ((0, 100), (100, 0), 45),
        ((0, 0), (100, 100), -45),
    ])
    def test_angle_between_eyes(self, left, right, expected):
        processor = DealWithItProcessor(np.zeros((2, 2, 3), dtype=np.uint8))
        assert processor._get_angle(left, right) == expected

    def test_glasses_are_wider_than_the_face(self):
        processor = DealWithItProcessor(np.zeros((2, 2, 3), dtype=np.uint8))
        glasses = processor._get_glasses(100)
        assert glasses.size[0] == 130


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


@pytest.mark.parametrize('filename,claimed', [
    ('me.jpg', 1),
    ('apollo_11_crew.jpg', 3),
    ('multiple_people.jpg', 8),
    ('solvay_1927.jpg', 29),
    ('blue_marble.jpg', 0),
])
def test_every_sample_has_the_faces_its_caption_claims(filename, claimed):
    """The tiles say "Three faces", "Twenty-nine faces" and so on.

    Pinned because the images are replaceable and the captions are not
    generated: resize one, swap one, or upgrade the detector, and the page
    would quietly start lying. Anyone who changes a picture has to change the
    number in src/ui.py:SAMPLES to match.
    """
    import face_recognition as fr

    from tests.conftest import STATIC_IMG

    assert len(fr.face_locations(as_array(STATIC_IMG / filename))) == claimed
