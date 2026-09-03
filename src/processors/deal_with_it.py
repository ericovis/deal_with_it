"""Paste "Deal With It" glasses onto every face in an image."""

import functools
import threading
from dataclasses import dataclass
from io import BytesIO
from math import atan, atan2, cos, degrees, hypot, radians, sin
from pathlib import Path

import cv2
import numpy as np
import resvg_py
from numpy.typing import NDArray
from PIL import Image

from src.errors import DealWithItError
from src.processors.base import BaseProcessor

STATIC = Path(__file__).resolve().parents[1] / 'static'
GLASSES_PATH = STATIC / 'img' / 'glasses.svg'
MODEL_PATH = Path(__file__).resolve().parents[1] / 'models' / 'face_detection_yunet_2023mar.onnx'

#: Rendered at this multiple of the final width, so the warp and rotate
#: have pixels to spare before the resize.
SUPERSAMPLE = 2
#: Render widths are rounded up to a multiple of this, so similar faces
#: share one render.
RENDER_STEP = 32
MAX_RENDER = 4096

#: Below this the detector's answers are dogs, whiskers and wallpaper; every
#: human face in the samples scores higher.
SCORE_THRESHOLD = 0.65
NMS_THRESHOLD = 0.3

# OpenCV's own thread pool would fight the worker's threads for cores.
cv2.setNumThreads(1)

Point = tuple[float, float]


class NoFacesFound(DealWithItError):
    """Nothing to draw on. The message is shown to the caller."""


@dataclass(frozen=True)
class Face:
    """One detection, in the coordinates of the image it was found on.
    ``left_eye`` is the eye on the left of the *picture*."""

    box: tuple[int, int, int, int]  # top, right, bottom, left
    left_eye: Point
    right_eye: Point
    nose: Point
    score: float

    def scaled(self, factor: float) -> 'Face':
        def point(p: Point) -> Point:
            return (p[0] * factor, p[1] * factor)

        return Face(
            tuple(round(edge * factor) for edge in self.box),  # type: ignore[arg-type]
            point(self.left_eye), point(self.right_eye), point(self.nose), self.score,
        )


_thread = threading.local()


def _detector(width: int, height: int) -> cv2.FaceDetectorYN:
    """One YuNet per thread: ``setInputSize`` mutates the object."""
    detector = getattr(_thread, 'detector', None)
    if detector is None:
        detector = _thread.detector = cv2.FaceDetectorYN.create(
            str(MODEL_PATH), '', (width, height),
            score_threshold=SCORE_THRESHOLD, nms_threshold=NMS_THRESHOLD,
        )
    detector.setInputSize((width, height))
    return detector


def find_faces(image: NDArray[np.uint8]) -> list[Face]:
    """Every face YuNet is confident about, with its eye and nose landmarks."""
    height, width = image.shape[:2]
    _, rows = _detector(width, height).detect(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    faces = []
    for row in ([] if rows is None else rows):
        x, y, w, h, x_re, y_re, x_le, y_le, x_n, y_n = (float(v) for v in row[:10])
        faces.append(Face(
            box=(max(round(y), 0), min(round(x + w), width),
                 min(round(y + h), height), max(round(x), 0)),
            # YuNet names eyes from the subject's side; the picture's left
            # eye is the subject's right.
            left_eye=(x_re, y_re), right_eye=(x_le, y_le), nose=(x_n, y_n),
            score=float(row[14]),
        ))
    return faces


@functools.cache
def _glasses_source(path: Path) -> str:
    return path.read_text()


@functools.lru_cache(maxsize=16)
def _render_glasses(source: str, width: int) -> Image.Image:
    """The glasses rasterised at ``width`` pixels across. Keyed on the SVG
    text and the width, never on a processor instance. Callers only warp,
    rotate or resize the result, which all return new images."""
    png = bytes(resvg_py.svg_to_bytes(svg_string=source, width=width))
    return Image.open(BytesIO(png)).convert('RGBA')


def _perspective_coefficients(source: list[Point], target: list[Point]) -> list[float]:
    """Coefficients for ``Image.transform(PERSPECTIVE)`` mapping the four
    ``target`` corners of the output back onto ``source`` corners."""
    matrix = []
    for (x, y), (u, v) in zip(target, source, strict=True):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(matrix, dtype=float)
    b = np.array([c for point in source for c in point], dtype=float)
    return list(np.linalg.solve(a, b))


class DealWithItProcessor(BaseProcessor):
    #: Glasses width as a multiple of the distance between the eyes. The
    #: meme's frame overhangs the face, so this is wider than the lenses
    #: alone would need.
    width_per_eye_distance = 2.6
    #: Where the bridge and the lens line sit in the artwork, as fractions
    #: of its width and height. The frame is not symmetric.
    bridge_x = 0.549
    lens_y = 0.42
    #: How far the nose tip protrudes, relative to the distance between the
    #: eyes: what turns the nose's sideways offset into a yaw angle.
    nose_depth = 0.5
    #: Vertical taper of the far side at full turn, mimicking perspective.
    taper = 0.25
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

    def _rendered(self, width: int) -> Image.Image:
        rendered = -(-width * SUPERSAMPLE // RENDER_STEP) * RENDER_STEP
        return _render_glasses(
            _glasses_source(self._glasses_path),
            min(max(rendered, RENDER_STEP), MAX_RENDER),
        )

    def _lean(self, img: Image.Image, yaw: float) -> Image.Image:
        """Taper the side of the frame that is further from the camera.

        Foreshortening of the width is already in the eye distance; this
        adds the vertical squeeze a turned frame shows, so the far lens
        reads as further away instead of merely narrower.
        """
        amount = self.taper * abs(sin(yaw))
        if amount < 0.01:
            return img
        w, h = img.size
        H = h * (1 + amount)
        near, far = (0, H), ((H - h * (1 - amount)) / 2, (H + h * (1 - amount)) / 2)
        left, right = (far, near) if yaw < 0 else (near, far)
        target = [(0, left[0]), (w, right[0]), (w, right[1]), (0, left[1])]
        source = [(0, 0), (w, 0), (w, h), (0, h)]
        return img.transform((w, round(H)), Image.PERSPECTIVE,
                             _perspective_coefficients(source, target), self.resample)

    def _place(self, face: Face) -> tuple[Image.Image, tuple[int, int]]:
        """The glasses drawn for one face, and where their top-left goes.

        The frame is sized from the distance between the eyes, which is
        already foreshortened when the head is turned, and anchored with its
        bridge on the midpoint between the eyes rather than on one eye.
        """
        (lx, ly), (rx, ry) = face.left_eye, face.right_eye
        dx, dy = rx - lx, ry - ly
        distance = hypot(dx, dy)
        if distance < 1:
            distance, dx, dy = 1.0, 1.0, 0.0
        mid = ((lx + rx) / 2, (ly + ry) / 2)
        roll = atan2(dy, dx)
        # The nose tip's offset along the eye line, in eye distances, is
        # zero for a frontal face and grows with the turn.
        along = ((face.nose[0] - mid[0]) * dx + (face.nose[1] - mid[1]) * dy) / distance ** 2
        yaw = atan(along / self.nose_depth)

        width = max(RENDER_STEP // SUPERSAMPLE, round(self.width_per_eye_distance * distance))
        img = self._rendered(width)
        img = self._lean(img, yaw)
        scale = width / img.width
        img = img.resize((width, max(1, round(img.height * scale))), self.resample)

        # The anchor's offset from the centre, which is what rotate() pivots on.
        ox = (self.bridge_x - 0.5) * img.width
        oy = (self.lens_y - 0.5) * img.height
        angle = -degrees(roll)
        if angle:
            img = img.rotate(angle, expand=True, resample=self.resample)
        a = radians(angle)
        rotated = (ox * cos(a) + oy * sin(a), -ox * sin(a) + oy * cos(a))
        position = (round(mid[0] - img.width / 2 - rotated[0]),
                    round(mid[1] - img.height / 2 - rotated[1]))
        return img, position

    def call(self) -> None:
        self.progress(35, 'Looking for faces')
        detection_image, upscale = self._detection_input()
        faces = find_faces(detection_image)
        if not faces:
            raise NoFacesFound('No faces were found in this image.')

        self.progress(75, f'Drawing glasses on {len(faces)} '
                          f'{"face" if len(faces) == 1 else "faces"}')
        self.output = Image.fromarray(self.image)
        for face in faces:
            glasses, position = self._place(face.scaled(upscale))
            self.output.paste(glasses, position, mask=glasses)
