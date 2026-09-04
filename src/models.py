"""Request and response schemas. Shape only: no network I/O in a validator."""

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.images import is_data_uri


class ImageRequest(BaseModel):
    """Either a URL or a base64 data URI -- exactly one of them."""

    model_config = ConfigDict(extra='forbid')

    url: AnyHttpUrl | None = Field(
        default=None,
        description='Public http(s) URL of the image to process.',
        examples=['https://example.com/photo.jpg'],
    )
    base64: str | None = Field(
        default=None,
        description='The image as a data URI, e.g. "data:image/png;base64,iVBOR...".',
    )

    @field_validator('base64')
    @classmethod
    def base64_must_have_a_header(cls, value: str | None) -> str | None:
        if value is not None and not is_data_uri(value):
            raise ValueError('The base64 image must have a data URI header.')
        return value

    @model_validator(mode='after')
    def a_single_parameter_must_be_passed(self) -> Self:
        # Values, not key presence: clients send both keys with one null.
        if self.url is not None and self.base64 is not None:
            raise ValueError('An url OR a base64 string must be passed, not both.')
        if self.url is None and self.base64 is None:
            raise ValueError('An url or a base64 string must be passed.')
        return self

    def as_payload(self) -> dict[str, str | None]:
        """Plain, pickle-friendly form to hand to the queue."""
        return {'url': str(self.url) if self.url else None, 'base64': self.base64}


class JobState(StrEnum):
    """The subset of RQ states we expose, plus ``not_found``."""

    QUEUED = 'queued'
    STARTED = 'started'
    FINISHED = 'finished'
    FAILED = 'failed'
    STOPPED = 'stopped'
    CANCELED = 'canceled'
    DEFERRED = 'deferred'
    SCHEDULED = 'scheduled'

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobState.FINISHED,
            JobState.FAILED,
            JobState.STOPPED,
            JobState.CANCELED,
        }


class SourceKind(StrEnum):
    """How an image reached the queue. Only the UI cares."""

    UPLOAD = 'upload'
    URL = 'url'
    SAMPLE = 'sample'


class JobSource(BaseModel):
    """Where a job's image came from, so a card can label itself. Not part
    of :class:`JobResult`, which is the API's published contract."""

    kind: SourceKind = SourceKind.URL
    #: File name, or ``host/path`` for a URL.
    label: str = ''
    #: A remote URL or a static path the submission already had. An upload
    #: has neither until the worker has written it a thumbnail.
    thumb: str | None = None
    #: How many faces the worker drew on, once it knows.
    faces: int | None = None


class JobCreated(BaseModel):
    """202 response: the work was accepted, come back for the result."""

    job_id: str
    state: JobState
    status_url: str


class FaceRecord(BaseModel):
    """One face the worker found, in the coordinates of the submitted image."""

    box: tuple[int, int, int, int] = Field(description='top, right, bottom, left')
    score: float = Field(description="The detector's confidence, 0 to 1.")
    points: dict[str, tuple[float, float]] = Field(
        description="The detector's five points: left_eye, right_eye, nose, "
                    'mouth_left, mouth_right. Left and right as seen in the picture.',
    )
    landmarks: list[tuple[float, float]] = Field(
        description='The 68 landmarks the glasses were placed from, in the '
                    'usual dlib/iBUG order.',
    )


class JobImages(BaseModel):
    """Where to fetch a finished job's pictures.

    URLs, not bytes. The result used to come back as a base64 data URI, which
    put megabytes through Redis and out again in every poll; these point at
    files the reverse proxy serves off disk.
    """

    view: str = Field(description='The result at viewing size (WebP, 1600px). '
                                  'What the card and the full-screen view show.')
    thumb: str = Field(description='The result as a thumbnail (WebP, 160px).')
    before: str | None = Field(
        default=None, description='The submitted image at viewing size (WebP).')
    full: str = Field(description='The result at full resolution, in the format '
                                  'it was submitted in.')
    card: str | None = Field(
        default=None,
        description='A 1200px JPEG for link previews. JPEG because unfurlers '
                    'handle WebP poorly.',
    )


class JobResult(BaseModel):
    """Poll response. ``images`` is set once state is ``finished``."""

    job_id: str
    state: JobState
    images: JobImages | None = Field(
        default=None, description='Where to fetch the result, once finished.'
    )
    downloads: dict[str, str] | None = Field(
        default=None,
        description='Full-resolution downloads by format, e.g. {"jpg": "...", '
                    '"webp": "..."}. Which formats are offered depends on what '
                    'was submitted.',
    )
    share_url: str | None = Field(
        default=None, description='A page showing this result, safe to pass on.')
    expires_at: datetime | None = Field(
        default=None,
        description='When the pictures above stop being fetchable. After this '
                    'they are gone, and so is the share page.',
    )
    error: str | None = Field(
        default=None, description='Why the job failed, if it did.'
    )
    progress: int | None = Field(
        default=None, ge=0, le=100,
        description='How far along the job is. A stage checkpoint, not a measurement.',
    )
    step: str | None = Field(
        default=None, description='What the worker is doing right now.'
    )
    ahead: int | None = Field(
        default=None, ge=0,
        description='How many jobs are queued in front of this one; 0 means it '
                    'is next up. Null once the job has been picked up.',
    )
    faces: list[FaceRecord] | None = Field(
        default=None, description='Every face the glasses went on, once finished.'
    )
    detection: str | None = Field(
        default=None,
        description='Which pass found the faces: plain, upscaled or equalised.',
    )
