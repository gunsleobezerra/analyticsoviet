from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.config import Settings
from core.cookies_s3 import S3CookiesSyncError, sync_cookies_from_s3_sync


def test_cookies_storage_backend_defaults_to_local() -> None:
    settings = Settings()
    assert settings.cookies_storage_backend == "local"
    assert settings.s3_cookies_bucket == ""
    assert settings.s3_cookies_prefix == "cookies/"


def test_sync_cookies_skips_when_backend_is_local(tmp_path) -> None:
    settings = Settings(cookies_storage_backend="local", cookies_dir=tmp_path)
    result = sync_cookies_from_s3_sync(settings)
    assert result == []


def test_sync_cookies_warns_when_bucket_empty(tmp_path) -> None:
    settings = Settings(
        cookies_storage_backend="s3",
        s3_cookies_bucket="",
        cookies_dir=tmp_path,
    )
    result = sync_cookies_from_s3_sync(settings)
    assert result == []


def test_sync_cookies_downloads_files_from_s3(tmp_path) -> None:
    settings = Settings(
        cookies_storage_backend="s3",
        s3_cookies_bucket="test-bucket",
        s3_cookies_prefix="cookies/",
        s3_endpoint_url="https://s3.example.com",
        s3_access_key_id="dummy-key",
        s3_secret_access_key="dummy-secret",
        cookies_dir=tmp_path,
    )

    mock_boto3 = MagicMock()
    mock_s3_client = MagicMock()
    mock_boto3.client.return_value = mock_s3_client

    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": "cookies/instagram.txt"},
                {"Key": "cookies/youtube.txt"},
                {"Key": "cookies/"},  # Directory prefix, should be ignored
            ]
        }
    ]
    mock_s3_client.get_paginator.return_value = mock_paginator

    # Mock download_file to actually create the target file
    def fake_download(bucket, key, target):
        assert bucket == "test-bucket"
        with open(target, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")

    mock_s3_client.download_file.side_effect = fake_download

    with patch.dict("sys.modules", {"boto3": mock_boto3, "botocore.config": MagicMock()}):
        downloaded = sync_cookies_from_s3_sync(settings)

    assert set(downloaded) == {"instagram.txt", "youtube.txt"}
    assert (tmp_path / "instagram.txt").exists()
    assert (tmp_path / "youtube.txt").exists()


def test_sync_cookies_raises_on_failure(tmp_path) -> None:
    settings = Settings(
        cookies_storage_backend="s3",
        s3_cookies_bucket="test-bucket",
        cookies_dir=tmp_path,
    )

    mock_boto3 = MagicMock()
    mock_s3_client = MagicMock()
    mock_boto3.client.return_value = mock_s3_client
    mock_s3_client.get_paginator.side_effect = Exception("S3 access denied")

    with patch.dict("sys.modules", {"boto3": mock_boto3, "botocore.config": MagicMock()}):
        with pytest.raises(S3CookiesSyncError, match="Failed to sync cookies from S3"):
            sync_cookies_from_s3_sync(settings)
