"""Application settings, all overridable through ``DWI_``-prefixed env vars."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='DWI_', env_file='.env', extra='ignore')

    #: Absolute base URL, used only for social-card metadata. Relative
    #: URLs are emitted when it is unset.
    public_url: str = ''
    #: Signs the session cookie. FastHTML would otherwise generate one and
    #: write it to ./.sesskey, which breaks on a read-only or replicated FS.
    secret_key: str = 'insecure-development-key'

    #: Where the RQ broker lives. Overridden to the service name in compose.
    redis_url: str = 'redis://localhost:6379/0'
    #: Name of the queue both the API and the worker talk to.
    queue_name: str = 'deal_with_it'
    #: Hard ceiling on a job's runtime before RQ kills it.
    job_timeout: int = 120
    #: How long a finished result stays readable. Must outlive the UI's polling.
    result_ttl: int = 900
    #: How long a failed job stays readable, so we can report the error.
    #: RQ's own default is a year, which would hoard image payloads.
    failure_ttl: int = 900
    #: Worker processes per container. Face detection is CPU-bound, so
    #: this wants to be around the core count.
    worker_processes: int = 1

    #: Largest image we are willing to download or decode.
    max_image_bytes: int = 10 * 1024 * 1024
    #: Decompression-bomb guard: refuse images with more pixels than this.
    max_image_pixels: int = 40_000_000
    #: Longest side, in pixels, of the copy the detector actually looks at.
    #: Detection cost scales with area -- a 4641x3589 photo takes ~11s at full
    #: size -- while the glasses are still drawn on the original. 0 disables.
    max_detection_size: int = 1600
    #: Connect/read timeout for fetching a remote image.
    http_timeout: float = 10.0
    #: Redirects we are willing to follow, re-validating the target each hop.
    max_redirects: int = 3
    #: Allow fetching from loopback/private/link-local addresses. Off by default:
    #: leaving it on turns the API into an SSRF proxy into the container network.
    allow_private_addresses: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
