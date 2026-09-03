"""Paste "Deal With It" glasses onto every face in an image."""

import functools
from io import BytesIO
from math import atan2, degrees
from pathlib import Path

import face_recognition as fr
import numpy as np
import resvg_py
from numpy.typing import NDArray
from PIL import Image

from src.errors import DealWithItError
from src.processors.base import BaseProcessor

GLASSES_PATH = Path(__file__).resolve().parents[1] / 'static' / 'img' / 'glasses.svg'

#: Rendered at this multiple of the final width, so the rotate has pixels
#: to spare before the resize.
SUPERSAMPLE = 2
#: Render widths are rounded up to a multiple of this, so similar faces
#: share one render.
RENDER_STEP = 32
MAX_RENDER = 4096


class NoFacesFound(DealWithItError):
    """Nothing to draw on. The message is shown to the caller."""


@functools.cache
def _glasses_source(path: Path) -> str:
    """The SVG, read from disk once per process."""
    return path.read_text()


@functools.lru_cache(maxsize=16)
def _render_glasses(source: str, width: int) -> Image.Image:
    """The glasses rasterised at ``width`` pixels across. Keyed on the SVG
    text and the width, never on a processor instance. Callers only rotate
    or resize the result, which both return new images."""
    png = bytes(resvg_py.svg_to_bytes(svg_string=source, width=width))
    return Image.open(BytesIO(png)).convert('RGBA')


def _scaled(point: tuple[int, int], factor: float) -> tuple[int, int]:
    """Map a coordinate found on the shrunken image back onto the original."""
    return (round(point[0] * factor), round(point[1] * factor))


class DealWithItProcessor(BaseProcessor):
    #: Horizontal position of the left lens within the glasses image, as a
    #: fraction of its width -- the anchor we line up with the left eye.
    lens_offset = 415 / 1024
    #: Glasses are drawn wider than the face by this fraction, so the frame
    #: overhangs the cheeks the way the meme does.
    width_increase = 0.3
    resample = Image.BILINEAR

    def __init__(self, image: NDArray[np.uint8], glasses_path: Path | None = None,
                 max_detection_size: int = 0, on_progress=None) -> None:
        super().__init__(image, on_progress=on_progress)
        self._glasses_path = glasses_path or GLASSES_PATH
        self._max_detection_size = max_detection_size

    def _detection_input(self) -> tuple[NDArray[np.uint8], float]:
        """The image the detector sees, and the factor to undo any shrinking.
        Detection cost grows with area; compositing stays full-resolution."""
        height, width = self.image.shape[:2]
        longest = max(height, width)
        if not self._max_detection_size or longest <= self._max_detection_size:
            return self.image, 1.0

        ratio = self._max_detection_size / longest
        smaller = Image.fromarray(self.image).resize(
            (max(1, round(width * ratio)), max(1, round(height * ratio))),
            resample=self.resample,
        )
        return np.asarray(smaller, dtype=np.uint8), 1 / ratio

    def _get_glasses(self, new_width: int, angle: float = 0) -> Image.Image:
        # The width the *rotated* result must end up at, which is what the
        # placement below is measured against.
        new_width = new_width + int(new_width * self.width_increase)
        rendered = -(-new_width * SUPERSAMPLE // RENDER_STEP) * RENDER_STEP
        img = _render_glasses(
            _glasses_source(self._glasses_path),
            min(max(rendered, RENDER_STEP), MAX_RENDER),
        )
        if angle != 0:
            img = img.rotate(angle, expand=True, resample=self.resample)
        width, height = img.size
        new_height = int(height * (new_width / width))
        return img.resize((new_width, new_height))

    def _get_final_position(self, img_size: tuple[int, int],
                            left_eye: tuple[int, int]) -> tuple[int, int]:
        x = left_eye[0] - int(img_size[0] * self.lens_offset)
        y = left_eye[1] - int(img_size[1] / 2)
        return (x, y)

    def _get_angle(self, left_eye: tuple[int, int], right_eye: tuple[int, int]) -> int:
        x_diff = right_eye[0] - left_eye[0]
        y_diff = right_eye[1] - left_eye[1]
        # Negated because image y grows downwards while rotate() goes anticlockwise.
        return -int(degrees(atan2(y_diff, x_diff)))

    def call(self) -> None:
        self.progress(35, 'Looking for faces')
        detection_image, upscale = self._detection_input()
        face_locations = fr.face_locations(detection_image)
        if not face_locations:
            raise NoFacesFound('No faces were found in this image.')

        # One batched call: per face, the library re-runs detection each time.
        all_landmarks = fr.face_landmarks(detection_image, face_locations=face_locations)

        self.progress(75, f'Drawing glasses on {len(face_locations)} '
                          f'{"face" if len(face_locations) == 1 else "faces"}')
        self.output = Image.fromarray(self.image)
        for face, landmarks in zip(face_locations, all_landmarks, strict=True):
            left_eye = _scaled(landmarks['left_eye'][0], upscale)
            right_eye = _scaled(landmarks['right_eye'][3], upscale)
            angle = self._get_angle(left_eye, right_eye)
            _, right, _, left = (round(edge * upscale) for edge in face)
            glasses = self._get_glasses(right - left, angle=angle)
            position = self._get_final_position(glasses.size, left_eye)
            self.output.paste(glasses, position, mask=glasses)
