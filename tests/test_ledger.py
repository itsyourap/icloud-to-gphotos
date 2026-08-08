"""Tests for the SQLite ledger, the gate that guards deletion."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from icloud_to_gphotos.ledger import MAX_UPLOAD_ATTEMPTS, Ledger


@pytest.fixture
def ledger(tmp_path: Path):
    """An open ledger backed by a temporary database."""
    with Ledger(tmp_path / "ledger.db") as instance:
        yield instance


def _add_asset(ledger: Ledger, asset_id: str, stem: str = "IMG_0001") -> None:
    ledger.upsert_asset(
        asset_id=asset_id,
        master_id=f"{asset_id}-m",
        filename=f"{stem}.HEIC",
        stem=stem,
        item_type="image",
        asset_date=datetime(2020, 1, 1, tzinfo=UTC),
        added_date=None,
        is_live_photo=False,
        has_adjustments=False,
    )


def _add_resource(ledger: Ledger, asset_id: str, key: str, filename: str) -> None:
    ledger.upsert_resource(
        asset_id=asset_id,
        resource_key=key,
        filename=filename,
        staging_root="media",
        size=100,
        checksum=None,
    )


def test_reserve_stem_returns_preferred_when_free(ledger: Ledger) -> None:
    assert ledger.reserve_stem("asset-1", "IMG_0001") == "IMG_0001"


def test_reserve_stem_disambiguates_collision_between_assets(ledger: Ledger) -> None:
    # Arrange: one asset already owns the stem "IMG_0001".
    first = ledger.reserve_stem("asset-aaaaaaaa11", "IMG_0001")
    _add_asset(ledger, "asset-aaaaaaaa11", stem=first)

    # Act: a different asset with the same camera filename asks for it.
    second = ledger.reserve_stem("asset-bbbbbbbb22", "IMG_0001")

    # Assert
    assert second != first
    assert second.startswith("IMG_0001_")


def test_reserve_stem_is_stable_for_the_same_asset(ledger: Ledger) -> None:
    _add_asset(ledger, "asset-1", stem="IMG_0001")
    assert ledger.reserve_stem("asset-1", "COMPLETELY_DIFFERENT") == "IMG_0001"


def test_asset_ready_to_purge_requires_every_resource_uploaded(ledger: Ledger) -> None:
    # Arrange: a Live Photo with a still and a video component.
    _add_asset(ledger, "asset-1")
    _add_resource(ledger, "asset-1", "original", "IMG_0001.HEIC")
    _add_resource(ledger, "asset-1", "original_video", "IMG_0001.MOV")

    ledger.mark_uploaded("asset-1", "original", "media-key-1")

    # Assert: the still alone is not enough.
    assert ledger.asset_ready_to_purge("asset-1") is False

    ledger.mark_uploaded("asset-1", "original_video", "media-key-2")
    assert ledger.asset_ready_to_purge("asset-1") is True


def test_asset_with_no_resources_is_never_ready_to_purge(ledger: Ledger) -> None:
    _add_asset(ledger, "asset-1")
    assert ledger.asset_ready_to_purge("asset-1") is False


def test_failed_resource_blocks_purge(ledger: Ledger) -> None:
    _add_asset(ledger, "asset-1")
    _add_resource(ledger, "asset-1", "original", "IMG_0001.HEIC")
    ledger.mark_failed("asset-1", "original", "upload rejected")

    assert ledger.asset_ready_to_purge("asset-1") is False


def test_resource_becomes_exhausted_after_max_attempts(ledger: Ledger) -> None:
    _add_asset(ledger, "asset-1")
    _add_resource(ledger, "asset-1", "original", "IMG_0001.HEIC")

    for _ in range(MAX_UPLOAD_ATTEMPTS):
        ledger.mark_failed("asset-1", "original", "boom")

    row = ledger.get_resource("asset-1", "original")
    assert row is not None
    assert row.attempts == MAX_UPLOAD_ATTEMPTS
    assert row.is_exhausted is True
    assert ledger.asset_blocked("asset-1") is True
    assert [r.asset_id for r in ledger.blocked_resources()] == ["asset-1"]


def test_upsert_resource_preserves_uploaded_state(ledger: Ledger) -> None:
    """Re-registering a resource on a later run must not reset its progress."""
    _add_asset(ledger, "asset-1")
    _add_resource(ledger, "asset-1", "original", "IMG_0001.HEIC")
    ledger.mark_uploaded("asset-1", "original", "media-key")

    _add_resource(ledger, "asset-1", "original", "IMG_0001.HEIC")

    row = ledger.get_resource("asset-1", "original")
    assert row is not None
    assert row.state == "uploaded"
    assert row.media_key == "media-key"


def test_mark_asset_purged_cascades_to_resources(ledger: Ledger) -> None:
    _add_asset(ledger, "asset-1")
    _add_resource(ledger, "asset-1", "original", "IMG_0001.HEIC")
    ledger.mark_uploaded("asset-1", "original", "key")

    ledger.mark_asset_purged("asset-1")

    asset = ledger.get_asset("asset-1")
    assert asset is not None
    assert asset.purged_at is not None
    assert ledger.get_resources("asset-1")[0].state == "purged"
    # A purged resource still counts as uploaded, so re-scanning is idempotent.
    assert ledger.asset_ready_to_purge("asset-1") is True


def test_purge_failure_is_recorded_without_marking_purged(ledger: Ledger) -> None:
    _add_asset(ledger, "asset-1")
    ledger.mark_asset_purge_failed("asset-1", "network down")

    asset = ledger.get_asset("asset-1")
    assert asset is not None
    assert asset.purged_at is None


def test_stats_counts_confirmed_bytes_only(ledger: Ledger) -> None:
    _add_asset(ledger, "asset-1")
    _add_resource(ledger, "asset-1", "original", "IMG_0001.HEIC")
    _add_resource(ledger, "asset-1", "original_video", "IMG_0001.MOV")
    ledger.mark_uploaded("asset-1", "original", "key")

    stats = ledger.stats()

    assert stats["assets_total"] == 1
    assert stats["resources_uploaded"] == 1
    assert stats["resources_pending"] == 1
    assert stats["bytes_uploaded"] == 100


def test_run_lifecycle_is_recorded(ledger: Ledger) -> None:
    ledger.start_run("run-1")
    ledger.finish_run(
        "run-1",
        status="ok",
        downloaded=3,
        uploaded=3,
        failed=0,
        purged=2,
        bytes_moved=999,
    )

    runs = ledger.recent_runs()
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["purged"] == 2


def test_transaction_rolls_back_on_error(ledger: Ledger) -> None:
    with pytest.raises(RuntimeError), ledger.transaction():
        _add_asset(ledger, "asset-1")
        raise RuntimeError("boom")

    assert ledger.get_asset("asset-1") is None
