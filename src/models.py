"""Request and response schemas. Shape only: no network I/O in a validator."""

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
    #: A remote URL or a static path. None for an upload, which has only a
    #: data URI, and that must not be re-sent on every poll.
    thumb: str | None = None
    #: What was actually submitted: the "before" half of a finished card.
    original: str | None = None
    #: How many faces the worker drew on, once it knows.
    faces: int | None = None


class JobCreated(BaseModel):
    """202 response: the work was accepted, come back for the result."""

    job_id: str
    state: JobState
    status_url: str


class JobResult(BaseModel):
    """Poll response. ``image`` is set once state is ``finished``."""

    job_id: str
    state: JobState
    image: str | None = Field(
        default=None, description='The processed image as a data URI, once finished.'
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
