"""Tests for resource planning: which bytes we move and what we name them."""

from __future__ import annotations

import base64
import plistlib
from datetime import UTC, datetime, timedelta

import pytest

from icloud_to_gphotos.assets import (
    extract_location,
    has_adjustments,
    local_capture_time,
    plan_asset,
    sanitize_stem,
)
from icloud_to_gphotos.config import Settings

from .conftest import FakePhotoAsset, encode_location, make_resource


def _keys(planned) -> list[str]:
    return [res.key for res in planned.resources]


def _names(planned) -> list[str]:
    return [res.filename for res in planned.resources]


def test_plain_photo_plans_only_the_original(settings: Settings) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_0001.HEIC")

    planned = plan_asset(asset, settings, "IMG_0001")

    assert _keys(planned) == ["original"]
    assert _names(planned) == ["IMG_0001.HEIC"]
    assert planned.resources[0].staging_root == "media"


def test_live_photo_plans_still_and_video_with_a_shared_stem(settings: Settings) -> None:
    """gotohp pairs by content identifier or filename stem, so both components
    must share one stem to be re-assembled into a single Google Photos item."""
    asset = FakePhotoAsset("a1", filename="IMG_0001.HEIC", is_live_photo=True)

    planned = plan_asset(asset, settings, "IMG_0001")

    assert _keys(planned) == ["original", "original_video"]
    assert _names(planned) == ["IMG_0001.HEIC", "IMG_0001.MOV"]
    assert {r.staging_root for r in planned.resources} == {"media"}


def test_live_photo_video_can_be_excluded(settings: Settings) -> None:
    settings.include_live_photo_video = False
    asset = FakePhotoAsset("a1", is_live_photo=True)

    assert _keys(plan_asset(asset, settings, "IMG_0001")) == ["original"]


def test_movie_plans_only_the_original(settings: Settings) -> None:
    asset = FakePhotoAsset(
        "a1",
        filename="IMG_0002.MOV",
        item_type="movie",
        resources={
            "original": make_resource(
                "original", "IMG_0002.MOV", resource_type="com.apple.quicktime-movie"
            )
        },
    )

    planned = plan_asset(asset, settings, "IMG_0002")

    assert _keys(planned) == ["original"]
    assert _names(planned) == ["IMG_0002.MOV"]


def test_edited_photo_with_both_policy_plans_original_and_render(settings: Settings) -> None:
    settings.edited_policy = "both"
    asset = FakePhotoAsset("a1", adjustment_type="crop", edited_size=2048)

    planned = plan_asset(asset, settings, "IMG_0001")

    assert _keys(planned) == ["original", "edited"]
    assert _names(planned) == ["IMG_0001.HEIC", "IMG_0001_edited.JPG"]
    # The render goes to its own staging root; it shares a stem with the still
    # and would otherwise be mistaken for a Live Photo pair.
    assert planned.resources[1].staging_root == "edited"


def test_edited_policy_edited_drops_the_original(settings: Settings) -> None:
    settings.edited_policy = "edited"
    asset = FakePhotoAsset("a1", adjustment_type="crop", edited_size=2048)

    assert _keys(plan_asset(asset, settings, "IMG_0001")) == ["edited"]


def test_edited_policy_original_ignores_the_render(settings: Settings) -> None:
    settings.edited_policy = "original"
    asset = FakePhotoAsset("a1", adjustment_type="crop", edited_size=2048)

    assert _keys(plan_asset(asset, settings, "IMG_0001")) == ["original"]


def test_adjustments_without_a_render_still_keep_the_original(settings: Settings) -> None:
    """An adjustment record with no ``resJPEGFull`` is not actionable. In
    'edited' mode we must fall back to the original rather than plan nothing,
    which would leave the asset stuck in iCloud forever."""
    settings.edited_policy = "edited"
    asset = FakePhotoAsset("a1", adjustment_type="crop", edited_size=None)

    assert _keys(plan_asset(asset, settings, "IMG_0001")) == ["original"]


def test_unedited_photo_never_gets_an_edited_resource(settings: Settings) -> None:
    settings.edited_policy = "both"
    asset = FakePhotoAsset("a1", adjustment_type=None, edited_size=4096)

    assert _keys(plan_asset(asset, settings, "IMG_0001")) == ["original"]


def test_alternative_original_is_opt_in(settings: Settings) -> None:
    resources = {
        "original": make_resource("original", "IMG_0001.DNG", resource_type="com.adobe.raw-image"),
        "alternative": make_resource("alternative", "IMG_0001.JPG", resource_type="public.jpeg"),
    }
    asset = FakePhotoAsset("a1", filename="IMG_0001.DNG", resources=resources)

    assert _keys(plan_asset(asset, settings, "IMG_0001")) == ["original"]

    settings.include_alternative_original = True
    planned = plan_asset(asset, settings, "IMG_0001")
    assert _keys(planned) == ["original", "alternative"]
    assert _names(planned) == ["IMG_0001.DNG", "IMG_0001_alt.JPG"]


def test_resources_without_a_url_are_skipped(settings: Settings) -> None:
    asset = FakePhotoAsset(
        "a1", resources={"original": make_resource("original", "IMG_0001.HEIC", url=None)}
    )

    assert plan_asset(asset, settings, "IMG_0001").resources == []


def test_planned_bytes_sums_reported_sizes(settings: Settings) -> None:
    asset = FakePhotoAsset(
        "a1",
        filename="IMG_0001.HEIC",
        is_live_photo=True,
        resources={
            "original": make_resource("original", "IMG_0001.HEIC", size=3_000_000),
            "original_video": make_resource("original_video", "IMG_0001.MOV", size=1_500_000),
        },
    )

    assert plan_asset(asset, settings, "IMG_0001").total_bytes == 4_500_000


def test_local_capture_time_uses_the_camera_timezone() -> None:
    """EXIF timestamps carry no zone, so we must reconstruct the wall-clock time
    the camera recorded, not UTC."""
    asset = FakePhotoAsset(
        "a1",
        asset_date=datetime(2024, 7, 9, 12, 50, 27, tzinfo=UTC),
        timezone_offset=19800,  # +05:30
    )

    local = local_capture_time(asset)

    assert local.utcoffset() == timedelta(hours=5, minutes=30)
    assert local.strftime("%Y:%m:%d %H:%M:%S") == "2024:07:09 18:20:27"


def test_local_capture_time_falls_back_to_utc_without_an_offset() -> None:
    asset = FakePhotoAsset(
        "a1",
        asset_date=datetime(2024, 7, 9, 12, 50, 27, tzinfo=UTC),
        timezone_offset=None,
    )

    assert local_capture_time(asset).utcoffset() == timedelta(0)


def test_extract_location_decodes_the_plist_fix() -> None:
    asset = FakePhotoAsset("a1", location=encode_location(12.9716, 77.5946, 920.0))

    location = extract_location(asset)

    assert location["latitude"] == pytest.approx(12.9716)
    assert location["longitude"] == pytest.approx(77.5946)
    assert location["altitude"] == pytest.approx(920.0)


def test_extract_location_returns_empty_when_absent() -> None:
    assert extract_location(FakePhotoAsset("a1", location=None)) == {}


def test_extract_location_rejects_a_half_fix() -> None:
    """Latitude without longitude is unusable; writing it would geotag the photo
    on the prime meridian."""
    half = base64.b64encode(plistlib.dumps({"lat": 12.9716}, fmt=plistlib.FMT_BINARY))

    assert extract_location(FakePhotoAsset("a1", location=half)) == {}


def test_extract_location_survives_corrupt_bytes() -> None:
    asset = FakePhotoAsset("a1", location=b"definitely-not-a-plist")

    assert extract_location(asset) == {}


def test_has_adjustments_reads_the_cloudkit_field() -> None:
    assert has_adjustments(FakePhotoAsset("a1", adjustment_type="crop")) is True
    assert has_adjustments(FakePhotoAsset("a2", adjustment_type=None)) is False


def test_age_days_measures_from_the_capture_date() -> None:
    asset = FakePhotoAsset("a1", asset_date=datetime(2024, 1, 1, tzinfo=UTC))
    planned = plan_asset(asset, Settings(icloud_username="x@y.z", _env_file=None), "IMG_0001")  # type: ignore[call-arg]

    now = datetime(2024, 1, 11, tzinfo=UTC)
    assert planned.age_days(now) == 10


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IMG_0001", "IMG_0001"),
        ("../../etc/passwd", "passwd"),
        ("bad:name*here?", "bad-name-here"),
        ("", "photo"),
        ("...", "photo"),
        ("  spaced  ", "spaced"),
        ("sub/dir/IMG_2", "IMG_2"),
        ("back\\slash\\IMG_3", "IMG_3"),
    ],
)
def test_sanitize_stem(raw: str, expected: str) -> None:
    assert sanitize_stem(raw) == expected
