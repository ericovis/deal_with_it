"""Application settings, all overridable through ``DWI_``-prefixed env vars."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='DWI_', env_file='.env', extra='ignore')

    #: Absolute base URL, used for social-card metadata and the API example.
    public_url: str = ''
    #: Signs the FastHTML session cookie. Set it in production.
    secret_key: str = 'insecure-development-key'

    redis_url: str = 'redis://localhost:6379/0'
    queue_name: str = 'deal_with_it'
    #: Hard ceiling on a job's runtime before RQ kills it.
    job_timeout: int = 120
    #: How long a finished result stays readable. Must outlive the UI's polling.
    result_ttl: int = 900
    #: How long a failed job stays readable. RQ's own default is a year.
    failure_ttl: int = 900
    #: Threads in the one worker process. Detection releases the GIL, so
    #: this scales like processes. Two is plenty for a small deployment;
    #: docs/LOAD.md has the numbers for more.
    worker_threads: int = 2

    #: Refuse new submissions while more than this many jobs are waiting.
    max_queue_depth: int = 50
    #: Submissions one client may make per ``rate_window`` seconds. 0 disables.
    rate_limit: int = 30
    rate_window: int = 60

    #: Where a job's pictures are written. Both tiers mount the same
    #: directory and the reverse proxy serves it. Empty means ``.data/blobs``
    #: beside the checkout, which is gitignored and fine for development.
    blob_dir: str = ''
    #: How long a result stays fetchable, and therefore shareable. This is
    #: the number the card counts down to.
    blob_ttl: int = 3600
    #: Ceiling on the whole store. Under a flood this, not the TTL, is what
    #: keeps a host with no swap alive: the oldest jobs go first.
    blob_max_bytes: int = 1024 ** 3
    #: How often the sweep runs, as a job on the ordinary queue.
    blob_sweep_interval: int = 30

    #: Largest image we are willing to download or decode.
    max_image_bytes: int = 10 * 1024 * 1024
    #: Decompression-bomb guard: refuse images with more pixels than this.
    max_image_pixels: int = 40_000_000
    #: Longest side, in pixels, of the copy the detector looks at. Compositing
    #: stays at full resolution. 0 disables.
    max_detection_size: int = 1600
    #: Connect/read timeout for fetching a remote image.
    http_timeout: float = 10.0
    #: Wall-clock ceiling on the whole download, redirects included. The
    #: read timeout alone lets a server that drips a byte every few seconds
    #: hold a worker until the job timeout.
    fetch_deadline: float = 30.0
    #: Redirects we are willing to follow, re-validating the target each hop.
    max_redirects: int = 3
    #: Allow fetching from loopback/private/link-local addresses. Off by
    #: default: on, the API is an SSRF proxy into the container network.
    allow_private_addresses: bool = False

    @model_validator(mode='after')
    def a_result_must_outlive_the_job_that_reports_it(self) -> 'Settings':
        """A finished job whose pictures have been swept is a card full of
        broken images. The store has to outlast the job record, not the other
        way round."""
        if self.blob_ttl < self.result_ttl:
            raise ValueError(
                f'DWI_BLOB_TTL ({self.blob_ttl}) must be at least '
                f'DWI_RESULT_TTL ({self.result_ttl}), or a job still reported '
                'as finished would point at pictures that are already gone.'
            )
        return self

    @property
    def max_request_bytes(self) -> int:
        """Ceiling on a request body: the image limit, base64-inflated, plus
        room for the JSON or multipart envelope."""
        return self.max_image_bytes * 4 // 3 + 64 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
