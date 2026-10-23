"""Download and synchronize cookies from S3-compatible storage.

Supports any S3-compatible backend (MinIO, Cloudflare R2, Ceph, Wasabi, AWS S3)
via custom endpoint_url, credentials and bucket/prefix configuration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.logging import get_logger

if TYPE_CHECKING:
    from core.config import Settings

LOGGER = get_logger("cookies.s3")


class S3CookiesSyncError(RuntimeError):
    """Raised when S3 cookie synchronization fails."""


def sync_cookies_from_s3_sync(settings: Settings) -> list[str]:
    """Synchronously download cookies from S3-compatible storage to local ``cookies_dir``.

    Returns a list of downloaded cookie filenames (e.g. ``["instagram.txt", "youtube.txt"]``).
    """
    if settings.cookies_storage_backend != "s3":
        return []

    bucket_name = settings.s3_cookies_bucket
    if not bucket_name:
        LOGGER.warning("s3_cookies_bucket_not_configured")
        return []

    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise S3CookiesSyncError(
            "boto3 is required for S3 cookie storage backend: pip install boto3"
        ) from exc

    dest_dir = Path(settings.cookies_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    client_kwargs: dict[str, Any] = {
        "service_name": "s3",
    }
    if settings.s3_endpoint_url:
        client_kwargs["endpoint_url"] = settings.s3_endpoint_url
    if settings.s3_region_name:
        client_kwargs["region_name"] = settings.s3_region_name
    if settings.s3_access_key_id:
        client_kwargs["aws_access_key_id"] = settings.s3_access_key_id
    if settings.s3_secret_access_key:
        client_kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
    if settings.s3_session_token:
        client_kwargs["aws_session_token"] = settings.s3_session_token

    # Use standard path-style or virtual-hosted style depending on endpoint
    client_config = Config(
        s3={"addressing_style": "path"} if settings.s3_endpoint_url else {},
        connect_timeout=10,
        read_timeout=30,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    client_kwargs["config"] = client_config

    s3_client = boto3.client(**client_kwargs)

    prefix = settings.s3_cookies_prefix.strip("/")
    if prefix:
        prefix = f"{prefix}/"

    downloaded: list[str] = []
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

        for page in pages:
            contents = page.get("Contents", [])
            for item in contents:
                key: str = item["Key"]
                filename = key[len(prefix) :] if prefix and key.startswith(prefix) else key
                # Skip subdirectories or empty keys
                if not filename or filename.endswith("/"):
                    continue

                local_path = dest_dir / Path(filename).name
                LOGGER.info("downloading_cookie_from_s3", bucket=bucket_name, key=key, target=str(local_path))
                s3_client.download_file(bucket_name, key, str(local_path))
                downloaded.append(local_path.name)

        LOGGER.info(
            "s3_cookies_sync_completed",
            bucket=bucket_name,
            prefix=prefix,
            count=len(downloaded),
            files=downloaded,
        )
        return downloaded

    except Exception as exc:
        LOGGER.error(
            "s3_cookies_sync_failed",
            bucket=bucket_name,
            error=str(exc),
        )
        raise S3CookiesSyncError(f"Failed to sync cookies from S3: {exc}") from exc


async def sync_cookies_from_s3(settings: Settings) -> list[str]:
    """Asynchronously download cookies from S3-compatible storage in an executor."""
    return await asyncio.to_thread(sync_cookies_from_s3_sync, settings)


__all__ = ["S3CookiesSyncError", "sync_cookies_from_s3", "sync_cookies_from_s3_sync"]
