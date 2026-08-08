"""Tests for streaming downloads.

The invariant: a file that exists at its final path is complete. A truncated or
empty download must never be left where the uploader would find it.
"""

from __future__ import annotations

from pathlib import Path

from icloud_to_gphotos.assets import plan_asset
from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.downloader import download_asset, download_batch, resource_path

from .conftest import (
    DEFAULT_PAYLOAD,
    FakePhotoAsset,
    FakeResponse,
    FakeService,
    FakeSession,
    make_resource,
)

URL = "https://cloudkit.invalid/asset"


def _asset(payload: bytes, *, size: int | None = None, **kwargs) -> FakePhotoAsset:
    session = FakeSession({URL: payload})
    return FakePhotoAsset(
        "a1",
        service=FakeService(session=session),
        resources={
            "original": make_resource(
                "original", "IMG_0001.HEIC", size=len(payload) if size is None else size
            )
        },
        **kwargs,
    )


def test_download_writes_the_full_payload(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    payload = b"x" * 4096
    planned = plan_asset(_asset(payload), settings, "IMG_0001")

    files, failures = download_asset(planned, staging_dirs)

    assert failures == []
    assert files[0].size == len(payload)
    assert files[0].path.read_bytes() == payload


def test_download_rejects_a_size_mismatch_and_leaves_nothing_behind(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    """A short read must not produce a file the uploader would treat as valid."""
    planned = plan_asset(_asset(b"only-10-b!", size=99999), settings, "IMG_0001")

    files, failures = download_asset(planned, staging_dirs)

    assert files == []
    assert "size mismatch" in failures[0].error
    target = resource_path(staging_dirs, planned.resources[0])
    assert not target.exists()
    assert list(staging_dirs["media"].iterdir()) == []


def test_download_rejects_an_empty_response(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    planned = plan_asset(_asset(b"", size=None), settings, "IMG_0001")

    files, failures = download_asset(planned, staging_dirs)

    assert files == []
    assert "empty response" in failures[0].error


def test_download_records_a_missing_url_as_a_failure(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    """A resource whose URL vanished between planning and download must be
    recorded, not silently dropped, or the asset could look fully migrated."""
    asset = FakePhotoAsset("a1")
    planned = plan_asset(asset, settings, "IMG_0001")
    planned.resources[0].resource.url = None

    files, failures = download_asset(planned, staging_dirs)

    assert files == []
    assert failures[0].error == "no download URL"


def test_download_retries_then_propagates_a_persistent_http_error(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    attempts = {"count": 0}

    class FailingSession(FakeSession):
        def get(self, url: str, stream: bool = False, timeout=None):  # noqa: ARG002
            attempts["count"] += 1
            return FakeResponse(b"", status=500)

    asset = FakePhotoAsset("a1", service=FakeService(session=FailingSession()))
    planned = plan_asset(asset, settings, "IMG_0001")
    files, failures = download_asset(planned, staging_dirs)

    assert files == []
    assert failures and "HTTP 500" in failures[0].error
    assert attempts["count"] > 1, "a transient HTTP error should have been retried"


def test_download_skips_resources_the_predicate_rejects(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    """Already-uploaded resources must not be re-fetched on a later run."""
    planned = plan_asset(_asset(b"data"), settings, "IMG_0001")

    files, failures = download_asset(
        planned, staging_dirs, skip_keys=frozenset({("a1", "original")})
    )

    assert files == []
    assert failures == []


def test_live_photo_components_both_land(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    session = FakeSession()
    asset = FakePhotoAsset(
        "a1", is_live_photo=True, service=FakeService(session=session)
    )
    planned = plan_asset(asset, settings, "IMG_0001")

    files, failures = download_asset(planned, staging_dirs)

    assert failures == []
    assert sorted(f.path.name for f in files) == ["IMG_0001.HEIC", "IMG_0001.MOV"]


def test_download_batch_isolates_a_failing_asset(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    """One bad asset must not take the whole batch down."""
    good = FakePhotoAsset("good", filename="GOOD.HEIC")
    bad = FakePhotoAsset(
        "bad",
        filename="BAD.HEIC",
        resources={"original": make_resource("original", "BAD.HEIC", size=999999)},
    )
    batch = [
        plan_asset(good, settings, "GOOD"),
        plan_asset(bad, settings, "BAD"),
    ]

    outcome = download_batch(batch, staging_dirs, workers=2)

    assert [f.asset_id for f in outcome.files] == ["good"]
    assert [f.asset_id for f in outcome.failures] == ["bad"]
    assert outcome.bytes_written == len(DEFAULT_PAYLOAD)


def test_download_batch_reports_bytes_written(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    batch = [plan_asset(_asset(b"y" * 100), settings, "IMG_0001")]

    outcome = download_batch(batch, staging_dirs, workers=1)

    assert outcome.bytes_written == 100


def test_resource_path_routes_edited_renders_to_their_own_tree(
    settings: Settings, staging_dirs: dict[str, Path]
) -> None:
    settings.edited_policy = "both"
    asset = FakePhotoAsset("a1", adjustment_type="crop", edited_size=2048)
    planned = plan_asset(asset, settings, "IMG_0001")

    original, edited = (resource_path(staging_dirs, r) for r in planned.resources)

    assert original.parent == staging_dirs["media"]
    assert edited.parent == staging_dirs["edited"]
