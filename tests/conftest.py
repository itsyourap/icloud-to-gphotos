"""Shared fixtures and fakes.

The fakes duck-type pyicloud's ``PhotoAsset`` closely enough to exercise the real
production code paths — including ``build_photo_resource`` and
``record_field_value``, which accept the raw-dict record shape used here.
"""

from __future__ import annotations

import base64
import plistlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pyicloud.services.photos_cloudkit.models import PhotoResource

from icloud_to_gphotos.config import Settings


def encode_location(
    latitude: float, longitude: float, altitude: float | None = None
) -> bytes:
    """Encode a GPS fix the way CloudKit delivers ``locationEnc``.

    Apple sends ENCRYPTED_BYTES fields as base64-wrapped binary plists, so the
    fake has to match or the decoding path under test is not really exercised.
    """
    payload: dict[str, Any] = {"lat": latitude, "lon": longitude}
    if altitude is not None:
        payload["alt"] = altitude
    return base64.b64encode(plistlib.dumps(payload, fmt=plistlib.FMT_BINARY))


def make_record(record_name: str, record_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Build a raw CloudKit record in the shape ``record_field_value`` expects."""
    return {
        "recordName": record_name,
        "recordType": record_type,
        "fields": {key: {"value": value} for key, value in fields.items()},
    }


#: Body returned for any URL a fake session was not given an explicit payload
#: for. Resource sizes default to its length so the fakes stay self-consistent
#: with the downloader's size verification.
DEFAULT_PAYLOAD = b"fake-media-bytes"


def make_resource(
    key: str,
    filename: str,
    *,
    size: int | None = None,
    url: str | None = "https://cloudkit.invalid/asset",
    resource_type: str | None = "public.heic",
) -> PhotoResource:
    """Build a downloadable resource variant."""
    return PhotoResource(
        key=key,
        filename=filename,
        url=url,
        size=len(DEFAULT_PAYLOAD) if size is None else size,
        type=resource_type,
        checksum=f"sum-{key}",
    )


class FakeSession:
    """Stands in for pyicloud's requests session."""

    def __init__(self, payloads: dict[str, bytes] | None = None) -> None:
        self.payloads = payloads or {}
        self.requested: list[str] = []

    def get(self, url: str, stream: bool = False, timeout: Any = None) -> Any:  # noqa: ARG002
        """Return a fake streaming response for ``url``."""
        self.requested.append(url)
        body = self.payloads.get(url, DEFAULT_PAYLOAD)
        return FakeResponse(body)


@dataclass
class FakeResponse:
    """Minimal stand-in for a streamed ``requests`` response."""

    body: bytes
    status: int = 200

    def raise_for_status(self) -> None:
        """Raise if the fake response is an error."""
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def iter_content(self, chunk_size: int = 8192) -> Any:
        """Yield the body in chunks."""
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


@dataclass
class FakeService:
    """Stands in for ``PhotosService``."""

    session: FakeSession = field(default_factory=FakeSession)


class FakePhotoAsset:
    """Duck-typed stand-in for ``pyicloud`` ``PhotoAsset``."""

    def __init__(
        self,
        asset_id: str,
        *,
        filename: str = "IMG_0001.HEIC",
        item_type: str = "image",
        asset_date: datetime | None = None,
        resources: dict[str, PhotoResource] | None = None,
        is_live_photo: bool = False,
        adjustment_type: str | None = None,
        edited_size: int | None = None,
        timezone_offset: int | None = 19800,  # +05:30, matching an IST device
        location: bytes | None = None,
        favorite: bool = False,
        service: FakeService | None = None,
        delete_result: bool = True,
        delete_error: Exception | None = None,
    ) -> None:
        self.id = asset_id
        self.master_id = f"{asset_id}-master"
        self.filename = filename
        self.item_type = item_type
        self.asset_date = asset_date or datetime(2020, 5, 1, 12, 0, tzinfo=UTC)
        self.added_date = self.asset_date
        self.is_live_photo = is_live_photo
        self._service = service or FakeService()
        self._delete_result = delete_result
        self._delete_error = delete_error
        self.delete_calls = 0

        default = {"original": make_resource("original", filename)}
        if is_live_photo:
            stem = Path(filename).stem
            default["original_video"] = make_resource(
                "original_video",
                f"{stem}.MOV",
                resource_type="com.apple.quicktime-movie",
            )
        self.resources = default if resources is None else resources

        asset_fields: dict[str, Any] = {}
        if adjustment_type is not None:
            asset_fields["adjustmentType"] = adjustment_type
        if timezone_offset is not None:
            asset_fields["timeZoneOffset"] = timezone_offset
        if location is not None:
            asset_fields["locationEnc"] = location
        if favorite:
            asset_fields["isFavorite"] = 1
        self._asset_record = make_record(asset_id, "CPLAsset", asset_fields)

        master_fields: dict[str, Any] = {}
        if edited_size is not None:
            master_fields["resJPEGFullRes"] = {
                "downloadURL": "https://cloudkit.invalid/edited",
                "size": edited_size,
            }
            master_fields["resJPEGFullFileType"] = "public.jpeg"
        self._master_record = make_record(self.master_id, "CPLMaster", master_fields)

    def delete(self) -> bool:
        """Record the deletion attempt and return the configured outcome."""
        self.delete_calls += 1
        if self._delete_error is not None:
            raise self._delete_error
        return self._delete_result


class FakeICloudSession:
    """Stands in for :class:`icloud_to_gphotos.icloud_client.ICloudSession`."""

    def __init__(self, assets: list[FakePhotoAsset]) -> None:
        self._assets = assets
        self.iterations = 0

    def iter_all_assets(self) -> Any:
        """Yield assets that have not been deleted, oldest first."""
        self.iterations += 1
        return iter([a for a in self._assets if a.delete_calls == 0])

    def library_size(self) -> int:
        """Count assets still present."""
        return len([a for a in self._assets if a.delete_calls == 0])


@pytest.fixture(autouse=True)
def _instant_retries() -> Any:
    """Strip the download retry backoff so tests do not sleep for real.

    The retry policy is applied at import time, so it has to be mutated on the
    live ``Retrying`` object rather than patched on the module.
    """
    from tenacity import wait_none

    from icloud_to_gphotos.downloader import _stream_to_disk

    original = _stream_to_disk.retry.wait
    _stream_to_disk.retry.wait = wait_none()
    yield
    _stream_to_disk.retry.wait = original


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed entirely at a temporary directory."""
    result = Settings(
        icloud_username="test@example.com",
        state_dir=tmp_path / "state",
        gotohp_binary=None,
        batch_max_bytes=10_000,
        batch_max_items=10,
        disk_headroom_bytes=0,
        delete_grace_days=7,
        backfill_metadata=False,
        ntfy_topic=None,
        _env_file=None,  # type: ignore[call-arg]
    )
    result.ensure_dirs()
    return result


@pytest.fixture
def staging_dirs(settings: Settings) -> dict[str, Path]:
    """The media and edited staging directories."""
    return {"media": settings.media_staging_dir, "edited": settings.edited_staging_dir}


def days_ago(days: int) -> datetime:
    """A UTC timestamp ``days`` in the past."""
    return datetime.now(UTC) - timedelta(days=days)
