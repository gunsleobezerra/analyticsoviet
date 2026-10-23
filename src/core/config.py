"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
)


class Settings(BaseSettings):
    """Runtime configuration.

    Every field maps to an upper-case environment variable of the same name.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- app ----
    app_name: str = "analyticsoviet"
    app_version: str = "0.1.0"
    environment: Literal["dev", "prod", "test"] = "dev"
    log_level: str = "INFO"
    api_prefix: str = "/v1"
    # Optional absolute URL advertised as the first OpenAPI server entry.
    public_base_url: str | None = None

    # ---- http ----
    http_timeout_seconds: float = 20.0
    http_connect_timeout_seconds: float = 10.0
    http_max_connections: int = 50
    http_max_keepalive_connections: int = 20
    http_retries: int = 3
    http_retry_backoff_seconds: float = 0.6
    proxy_url: str | None = None
    user_agents: list[str] = Field(default_factory=lambda: list(DEFAULT_USER_AGENTS))

    # ---- cache ----
    cache_backend: Literal["memory", "redis"] = "memory"
    cache_ttl_profile_seconds: int = 900
    cache_ttl_posts_seconds: int = 900
    cache_ttl_post_seconds: int = 3600
    cache_max_entries: int = 5000
    redis_url: str = "redis://localhost:6379/0"

    # ---- rate limiting ----
    ratelimit_youtube_per_minute: int = 60
    ratelimit_tiktok_per_minute: int = 30
    ratelimit_instagram_per_minute: int = 20
    ratelimit_facebook_per_minute: int = 15

    # ---- concurrency ----
    concurrency_youtube: int = 8
    concurrency_tiktok: int = 4
    concurrency_instagram: int = 3
    concurrency_facebook: int = 2

    # ---- circuit breaker ----
    breaker_enabled: bool = False
    breaker_failure_threshold: int = 5
    breaker_cooldown_seconds: int = 10

    # ---- storage ----
    database_url: str = "sqlite+aiosqlite:///./data/analyticsoviet.db"
    enable_history: bool = True

    # ---- optional official APIs ----
    youtube_api_key: str | None = None
    instagram_app_id: str = "936619743392459"

    # ---- cookies storage backend ----
    # 'local' reads directly from local filesystem.
    # 's3' downloads cookies from any S3-compatible storage (MinIO, R2, Ceph, AWS S3)
    cookies_storage_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_cookies_bucket: str = ""
    s3_cookies_prefix: str = "cookies/"
    s3_region_name: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_session_token: str | None = None

    # ---- cookies files ----
    cookies_dir: Path = Path("cookies")
    cookies_youtube_file: Path | None = None
    cookies_tiktok_file: Path | None = None
    cookies_instagram_file: Path | None = None
    cookies_facebook_file: Path | None = None

    # ---- scraping limits ----
    scrape_max_urls: int = 20
    scrape_default_posts: int = 12

    # ---- feature toggles ----
    enable_ytdlp_fallback: bool = True

    def cookies_file_for(self, platform: str) -> Path | None:
        """Return the configured Netscape cookie file for a platform, if it exists."""
        explicit: Path | None = getattr(self, f"cookies_{platform}_file", None)
        candidate = explicit or (self.cookies_dir / f"{platform}.txt")
        return candidate if candidate.is_file() else None

    def ratelimit_for(self, platform: str) -> int:
        return int(getattr(self, f"ratelimit_{platform}_per_minute", 30))

    def concurrency_for(self, platform: str) -> int:
        return int(getattr(self, f"concurrency_{platform}", 3))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
