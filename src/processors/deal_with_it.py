"""Paste "Deal With It" glasses onto every face in an image.

Detection is YuNet (fast, finds small and turned faces, and the odd animal);
the 68 landmarks that place the glasses come from dlib's shape predictor run
on each YuNet box. Both sets of points are kept on the processor, so the
worker can store them with the job.
"""

import functools
import threading
from dataclasses import dataclass
from io import BytesIO
from math import atan, atan2, degrees, hypot, sin
from pathlib import Path

import cv2
import dlib
import face_recognition_models
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

#: Below this the detector's answers are whiskers and wallpaper; every human
#: face in the samples scores higher, and so do the dogs.
SCORE_THRESHOLD = 0.65
NMS_THRESHOLD = 0.3
#: When the plain pass finds nothing, the detector gets another look at the
#: image blown up (small faces) and with its contrast equalised (dim ones).
#: Those passes also conjure faces out of fur and wallpaper, so they have to
#: be surer: equalising a cat gives two "faces" at 0.75.
UPSCALE = 2.0
FALLBACK_THRESHOLD = 0.8

#: dlib's 68-point layout: the outer corners of the eyes and the nose tip.
OUTER_LEFT, OUTER_RIGHT, NOSE_TIP = 36, 45, 30

# OpenCV's own thread pool would fight the worker's threads for cores.
cv2.setNumThreads(1)

Point = tuple[float, float]


class NoFacesFound(DealWithItError):
    """Nothing to draw on. The message is shown to the caller."""


@dataclass(frozen=True)
class Face:
    """One YuNet detection. ``left_eye`` is the eye on the left of the
    *picture*; YuNet itself names them from the subject's side."""

    box: tuple[int, int, int, int]  # top, right, bottom, left
    left_eye: Point
    right_eye: Point
    nose: Point
    mouth_left: Point
    mouth_right: Point
    score: float

    def scaled(self, factor: float) -> 'Face':
        def point(p: Point) -> Point:
            return (p[0] * factor, p[1] * factor)

        return Face(
            tuple(round(edge * factor) for edge in self.box),  # type: ignore[arg-type]
            point(self.left_eye), point(self.right_eye), point(self.nose),
            point(self.mouth_left), point(self.mouth_right), self.score,
        )

    def as_record(self) -> dict:
        return {
            'box': list(self.box), 'score': round(self.score, 3),
            'points': {
                'left_eye': self.left_eye, 'right_eye': self.right_eye, 'nose': self.nose,
                'mouth_left': self.mouth_left, 'mouth_right': self.mouth_right,
            },
        }


@dataclass(frozen=True)
class Landmarks:
    """dlib's 68 points for one face."""

    points: tuple[Point, ...]

    def scaled(self, factor: float) -> 'Landmarks':
        return Landmarks(tuple((x * factor, y * factor) for x, y in self.points))

    @property
    def outer_left(self) -> Point:
        return self.points[OUTER_LEFT]

    @property
    def outer_right(self) -> Point:
        return self.points[OUTER_RIGHT]

    @property
    def nose_tip(self) -> Point:
        return self.points[NOSE_TIP]


@dataclass(frozen=True)
class DetectedFace:
    face: Face
    landmarks: Landmarks

    def as_record(self) -> dict:
        return {**self.face.as_record(), 'landmarks': [list(p) for p in self.landmarks.points]}


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


@functools.cache
def _predictor() -> dlib.shape_predictor:
    """The 68-point shape predictor: ~100 MB, safe to share across threads."""
    return dlib.shape_predictor(face_recognition_models.pose_predictor_model_location())


def find_faces(image: NDArray[np.uint8]) -> list[Face]:
    """Every face YuNet is confident about in this image, as is."""
    height, width = image.shape[:2]
    _, rows = _detector(width, height).detect(cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    faces = []
    for row in ([] if rows is None else rows):
        x, y, w, h, x_re, y_re, x_le, y_le, x_n, y_n, x_rm, y_rm, x_lm, y_lm = (
            float(v) for v in row[:14])
        faces.append(Face(
            box=(max(round(y), 0), min(round(x + w), width),
                 min(round(y + h), height), max(round(x), 0)),
            left_eye=(x_re, y_re), right_eye=(x_le, y_le), nose=(x_n, y_n),
            mouth_left=(x_rm, y_rm), mouth_right=(x_lm, y_lm),
            score=float(row[14]),
        ))
    return faces


def _equalised(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)
    lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def detect(image: NDArray[np.uint8]) -> tuple[list[Face], str]:
    """Faces in the image, trying harder when the plain pass finds none.

    Returns the faces and which pass found them: ``plain``, ``upscaled``
    (the image doubled, for faces too small to register) or ``equalised``
    (contrast stretched, for faces lost in shadow).
    """
    faces = find_faces(image)
    if faces:
        return faces, 'plain'
    height, width = image.shape[:2]
    bigger = cv2.resize(image, (round(width * UPSCALE), round(height * UPSCALE)),
                        interpolation=cv2.INTER_CUBIC)
    faces = [face.scaled(1 / UPSCALE) for face in find_faces(bigger)
             if face.score >= FALLBACK_THRESHOLD]
    if faces:
        return faces, 'upscaled'
    faces = [face for face in find_faces(_equalised(image)) if face.score >= FALLBACK_THRESHOLD]
    return faces, 'equalised' if faces else 'plain'


#: The box handed to the shape predictor, built from YuNet's points: a
#: square of this many eye distances, centred between the eyes and the
#: mouth, this far down from the eye line. The predictor was trained on
#: dlib's own detector boxes, and YuNet's taller box shifts its answers (34
#: px on the Vermeer); this shape puts the eye corners within a pixel of
#: what dlib's detector would have given, over every sample face.
PREDICTOR_BOX_SIDE = 2.4
PREDICTOR_BOX_DROP = 0.3


def predictor_box(face: Face) -> tuple[int, int, int, int]:
    """``(top, right, bottom, left)`` of the box the landmark model sees."""
    (lx, ly), (rx, ry) = face.left_eye, face.right_eye
    eye_x, eye_y = (lx + rx) / 2, (ly + ry) / 2
    mouth_x = (face.mouth_left[0] + face.mouth_right[0]) / 2
    mouth_y = (face.mouth_left[1] + face.mouth_right[1]) / 2
    side = PREDICTOR_BOX_SIDE * max(hypot(rx - lx, ry - ly), 1.0)
    cx = (eye_x + mouth_x) / 2
    cy = eye_y + PREDICTOR_BOX_DROP * (mouth_y - eye_y)
    return (round(cy - side / 2), round(cx + side / 2), round(cy + side / 2), round(cx - side / 2))


def landmarks_for(image: NDArray[np.uint8], face: Face) -> Landmarks:
    top, right, bottom, left = predictor_box(face)
    shape = _predictor()(image, dlib.rectangle(left, top, right, bottom))
    return Landmarks(tuple((float(p.x), float(p.y)) for p in shape.parts()))


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
    #: Width of the (rotated) frame as a multiple of the distance between
    #: the outer corners of the eyes. Fitted to the original box-based
    #: placement over the sample faces, which it reproduces on average
    #: without the detector's quantisation noise.
    width_per_span = 2.35
    #: Horizontal position of the left lens within the artwork, as a
    #: fraction of its width: the anchor that goes on the outer corner of
    #: the picture-left eye. Unchanged from the original placement.
    lens_offset = 415 / 1024
    #: How far the nose tip protrudes, relative to the outer-corner span,
    #: which turns its sideways offset into a yaw angle.
    nose_depth = 0.32
    #: Vertical taper of the far side at full turn, mimicking perspective.
    taper = 0.25
    resample = Image.BILINEAR

    def __init__(self, image: NDArray[np.uint8], glasses_path: Path | None = None,
                 max_detection_size: int = 0, on_progress=None) -> None:
        super().__init__(image, on_progress=on_progress)
        self._glasses_path = glasses_path or GLASSES_PATH
        self._max_detection_size = max_detection_size
        #: What was found, in the coordinates of the original image.
        self.faces: list[DetectedFace] = []
        self.detection: str = ''

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

        Foreshortening of the width is already in the eye span; this adds
        the vertical squeeze a turned frame shows, so the far lens reads as
        further away instead of merely narrower.
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

    def _place(self, outer_left: Point, outer_right: Point,
               nose_tip: Point) -> tuple[Image.Image, tuple[int, int]]:
        """The glasses drawn for one face, and where their top-left goes.

        The rule is the original one: the frame is rotated by the angle
        between the outer eye corners, sized so its rotated width fits the
        face, and anchored with the left lens on the left eye's outer corner
        and its vertical centre on that corner's line. Only the width's
        source changed, from the detector's box to the eye span.
        """
        dx, dy = outer_right[0] - outer_left[0], outer_right[1] - outer_left[1]
        span = hypot(dx, dy)
        if span < 1:
            span, dx, dy = 1.0, 1.0, 0.0
        angle = -int(degrees(atan2(dy, dx)))
        mid = ((outer_left[0] + outer_right[0]) / 2, (outer_left[1] + outer_right[1]) / 2)
        # The nose tip's offset along the eye line, in spans: zero for a
        # frontal face, growing with the turn.
        along = ((nose_tip[0] - mid[0]) * dx + (nose_tip[1] - mid[1]) * dy) / span ** 2
        yaw = atan(along / self.nose_depth)

        width = max(RENDER_STEP // SUPERSAMPLE, round(self.width_per_span * span))
        img = self._lean(self._rendered(width), yaw)
        if angle:
            img = img.rotate(angle, expand=True, resample=self.resample)
        scale = width / img.width
        img = img.resize((width, max(1, round(img.height * scale))), self.resample)

        x = round(outer_left[0]) - int(img.width * self.lens_offset)
        y = round(outer_left[1]) - int(img.height / 2)
        return img, (x, y)

    def call(self) -> None:
        self.progress(35, 'Looking for faces')
        detection_image, upscale = self._detection_input()
        faces, self.detection = detect(detection_image)
        if not faces:
            raise NoFacesFound('No faces were found in this image.')

        self.progress(75, f'Drawing glasses on {len(faces)} '
                          f'{"face" if len(faces) == 1 else "faces"}')
        self.output = Image.fromarray(self.image)
        for face in faces:
            landmarks = landmarks_for(detection_image, face).scaled(upscale)
            self.faces.append(DetectedFace(face.scaled(upscale), landmarks))
            glasses, position = self._place(
                landmarks.outer_left, landmarks.outer_right, landmarks.nose_tip)
            self.output.paste(glasses, position, mask=glasses)
