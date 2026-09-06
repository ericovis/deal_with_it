"""The falling-glasses GIF: what one loop actually contains.

Driven with a stand-in placer rather than the detector -- a flat rectangle in
a colour nothing else uses, so a pixel says where the glasses are on a given
frame. What the real placement produces is `tests/test_processor.py`'s
business.
"""

from io import BytesIO

import pytest
from PIL import Image, ImageSequence

from src import animation

#: Nothing else in these pictures is anywhere near this.
SPRITE = (255, 0, 0)
BACKDROP = (40, 90, 140)
LANDING = (60.0, 70.0)


@pytest.fixture
def base():
    return Image.new('RGB', (200, 120), BACKDROP)


def placer(seen: list | None = None):
    """The processor's placement rule, stood in for: one flat sprite per
    face, landing with its top-left on the picture-left eye corner."""

    def place(outer_left, outer_right, nose_tip):
        if seen is not None:
            seen.append((outer_left, outer_right, nose_tip))
        return (Image.new('RGBA', (24, 10), (*SPRITE, 255)),
                (round(outer_left[0]), round(outer_left[1])))

    return place


def face(x: float = LANDING[0], y: float = LANDING[1]):
    """One face's three landmarks, all the placer above needs."""
    return ((x, y), (x + 40, y), (x + 20, y + 15))


def loop(data: bytes) -> list[Image.Image]:
    return [frame.convert('RGB') for frame in ImageSequence.Iterator(Image.open(BytesIO(data)))]


def redness(frame: Image.Image, at: tuple[int, int]) -> int:
    """How much more red than blue a pixel is. The palette moves colours
    slightly, so nothing here asks for an exact one."""
    red, _, blue = frame.getpixel(at)
    return red - blue


class TestTheLoop:
    def test_it_is_a_gif_the_size_of_the_picture_it_was_given(self, base):
        with Image.open(BytesIO(animation.render(base, [face()], placer(), 1.0))) as gif:
            assert gif.format == 'GIF'
            assert gif.size == base.size

    def test_it_repeats_for_ever(self, base):
        with Image.open(BytesIO(animation.render(base, [face()], placer(), 1.0))) as gif:
            assert gif.info['loop'] == 0

    def test_a_still_beat_is_one_frame_held_rather_than_thirty_drawn(self, base):
        """Three seconds at any honest frame rate would be sixty pictures.
        The pauses are duration, which is what keeps this a small file."""
        frames = loop(animation.render(base, [face()], placer(), 1.0))
        held = sum(frame.info['duration'] for frame in frames)
        assert held > 3000, 'the loop is a few seconds long'
        assert len(frames) < 25, f'but only {len(frames)} pictures'
        assert all(frame.info['duration'] > 0 for frame in frames), 'none of them flash past'

    def test_the_frames_after_the_first_are_stored_as_what_changed(self):
        """A whole picture per frame is what makes GIFs enormous. The frames
        share one palette so Pillow can write each as the rectangle that
        moved, which only shows up on a picture with real detail in it."""
        noisy = Image.effect_noise((200, 120), 60).convert('RGB')
        one = BytesIO()
        noisy.save(one, format='GIF')
        whole = animation.render(noisy, [face()], placer(), 1.0)
        assert len(whole) < 2 * len(one.getvalue()), (
            f'{len(whole)} bytes for the loop against {len(one.getvalue())} for one frame'
        )


class TestTheFall:
    def test_it_starts_with_the_picture_and_nothing_on_it(self, base):
        first = loop(animation.render(base, [face()], placer(), 1.0))[0]
        assert redness(first, (65, 75)) < 30, 'the glasses are still off the top edge'

    def test_it_ends_with_the_glasses_where_the_placer_put_them(self, base):
        last = loop(animation.render(base, [face()], placer(), 1.0))[-1]
        assert redness(last, (65, 75)) > 100
        assert redness(last, (65, 40)) < 30, 'and nowhere else'

    def test_the_landmarks_are_scaled_to_the_frame_before_they_are_placed(self, base):
        """The animation is drawn small; the landmarks are in the coordinates
        of the picture that was submitted."""
        seen: list = []
        animation.render(base, [face(120, 200)], placer(seen), 0.5)
        assert seen == [((60.0, 100.0), (80.0, 100.0), (70.0, 107.5))]

    def test_the_faces_land_left_to_right(self, base):
        """Whatever order the detector reported them in: a stagger that
        crosses the picture reads as choreography, one that hops does not."""
        frames = loop(animation.render(
            base, [face(150, 70), face(20, 70)], placer(), 1.0))
        # Halfway through the fall, the left pair is ahead of the right one.
        halfway = frames[len(frames) // 3]
        left = [y for y in range(120) if redness(halfway, (25, y)) > 100]
        right = [y for y in range(120) if redness(halfway, (155, y)) > 100]
        assert left and left[-1] > (right[-1] if right else 0), (
            'the left face is further down the picture'
        )

    def test_one_face_does_not_wait_for_a_stagger_it_is_alone_in(self, base):
        alone = loop(animation.render(base, [face()], placer(), 1.0))
        crowd = loop(animation.render(base, [face(20, 70), face(150, 70)], placer(), 1.0))
        assert len(alone) == len(crowd) - animation.SPREAD_STEPS


class TestTheCaption:
    def lettering(self, frame: Image.Image) -> Image.Image:
        """The white of the caption alone, along the bottom of the picture:
        the backdrop is mid-blue and the outline is black."""
        bottom = frame.crop((0, frame.height - 40, frame.width, frame.height))
        return bottom.convert('L').point(lambda value: 255 if value > 200 else 0)

    def test_it_arrives_once_the_glasses_have_landed(self, base):
        frames = loop(animation.render(base, [face()], placer(), 1.0))
        captioned = [index for index, frame in enumerate(frames)
                     if self.lettering(frame).histogram()[255] > 50]
        assert captioned, 'DEAL WITH IT never showed up'
        assert captioned == list(range(len(frames) - len(animation.CAPTION_POP), len(frames))), (
            'it belongs to the last frames only, and to all of them'
        )

    def test_it_snaps_in_rather_than_appearing_at_its_size(self, base):
        """The pop is what makes it read as a stamp. Its overshoot frame is
        the widest the caption ever is."""
        frames = loop(animation.render(base, [face()], placer(), 1.0))
        small, over, settled = (self.lettering(frames[index]).getbbox()
                                for index in (-3, -2, -1))
        assert (small[2] - small[0]) < (settled[2] - settled[0]) < (over[2] - over[0])
