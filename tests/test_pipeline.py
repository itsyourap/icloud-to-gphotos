"""End-to-end pipeline tests with a fake iCloud and a fake gotohp.

These are the tests that matter most. Deletion from iCloud is irreversible in
practice (30 days in Recently Deleted, then gone), so the suite pins every
condition that must hold before an asset is removed, and every condition that
must prevent it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from icloud_to_gphotos import pipeline as pipeline_module
from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.ledger import MAX_UPLOAD_ATTEMPTS, Ledger
from icloud_to_gphotos.pipeline import Pipeline
from icloud_to_gphotos.uploader import IncompatibleGotohp, UploadError, UploadReport

from .conftest import (
    DEFAULT_PAYLOAD,
    FakeICloudSession,
    FakePhotoAsset,
    days_ago,
    make_resource,
)


@dataclass
class FakeGotohp:
    """Records upload calls and replays a scripted verdict for each file."""

    #: Basenames to fail. Everything else succeeds.
    fail: set[str] = field(default_factory=set)
    #: Basenames to report as an unhelpful skip (not confirmed remote).
    skip_unconfirmed: set[str] = field(default_factory=set)
    #: Basenames to report as already present in Google Photos.
    remote_duplicate: set[str] = field(default_factory=set)
    #: Basenames to omit from the summary entirely.
    omit: set[str] = field(default_factory=set)
    raise_error: bool = False
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, directory: Path, **kwargs: object):
        from icloud_to_gphotos.uploader import parse_summary

        if self.raise_error:
            raise UploadError("gotohp exploded")

        files = sorted(p for p in directory.rglob("*") if p.is_file())
        if not files:
            # Mirrors the real upload_directory, which short-circuits rather
            # than spawning gotohp for an empty staging tree.
            return UploadReport()

        self.calls.append(
            {
                "directory": directory.name,
                "files": [p.name for p in files],
                "pair_live_photos": kwargs.get("pair_live_photos"),
            }
        )

        results = []
        # Group Live Photo components so one verdict covers the pair, exactly as
        # gotohp reports them.
        stems: dict[str, list[Path]] = {}
        for path in files:
            stems.setdefault(path.stem, []).append(path)

        for paths in stems.values():
            names = [p.name for p in paths]
            if any(n in self.omit for n in names):
                continue
            if any(n in self.fail for n in names):
                results.append({"path": str(paths[0]), "success": False, "error": "rejected"})
            elif any(n in self.skip_unconfirmed for n in names):
                results.append(
                    {
                        "path": str(paths[0]),
                        "success": False,
                        "skipped": True,
                        "skipCode": "incomplete-live-photo-skipped",
                    }
                )
            elif any(n in self.remote_duplicate for n in names):
                results.append(
                    {
                        "path": str(paths[0]),
                        "paths": [str(p) for p in paths],
                        "success": False,
                        "skipped": True,
                        "skipCode": "remote-duplicate",
                    }
                )
            else:
                results.append(
                    {
                        "path": str(paths[0]),
                        "paths": [str(p) for p in paths],
                        "success": True,
                        "mediaKey": f"key-{paths[0].stem}",
                    }
                )

        return parse_summary(
            {
                "total": len(results),
                "succeeded": sum(1 for r in results if r.get("success")),
                "failed": sum(1 for r in results if not r.get("success") and not r.get("skipped")),
                "skipped": sum(1 for r in results if r.get("skipped")),
                "results": results,
            }
        )


@pytest.fixture
def make_pipeline(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a pipeline wired to a fake iCloud session and fake gotohp."""
    opened: list[Ledger] = []

    def build(
        assets: list[FakePhotoAsset],
        gotohp: FakeGotohp | None = None,
        *,
        dry_run: bool = False,
        ledger: Ledger | None = None,
    ) -> tuple[Pipeline, FakeICloudSession, FakeGotohp, Ledger]:
        stub = gotohp or FakeGotohp()
        monkeypatch.setattr(pipeline_module, "upload_directory", stub)
        binary = tmp_path / "gotohp-cli"
        binary.write_text("#!/bin/sh")
        monkeypatch.setattr(pipeline_module, "find_gotohp", lambda _: binary)
        monkeypatch.setattr(pipeline_module, "find_exiftool", lambda _: None)
        # The dummy binary above cannot be executed, and these tests exercise
        # pipeline logic rather than the probe. Tests that care about the
        # compatibility gate override this again.
        monkeypatch.setattr(pipeline_module, "verify_compatible", lambda _b: None)

        session = FakeICloudSession(assets)
        if ledger is None:
            state = Ledger(settings.ledger_path)
            opened.append(state)
        else:
            state = ledger
        return Pipeline(settings, session, state, dry_run=dry_run), session, stub, state

    yield build

    for state in opened:
        state.close()


# --- The happy path --------------------------------------------------------


def test_confirmed_upload_of_an_old_asset_is_deleted_from_icloud(make_pipeline) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, gotohp, ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.totals.uploaded == 1
    assert result.totals.purged_assets == 1
    assert asset.delete_calls == 1
    assert ledger.get_asset("a1").purged_at is not None
    assert gotohp.calls[0]["files"] == ["IMG_1.HEIC"]


def test_live_photo_is_deleted_only_after_both_components_confirm(make_pipeline) -> None:
    asset = FakePhotoAsset(
        "a1", filename="IMG_1.HEIC", is_live_photo=True, asset_date=days_ago(30)
    )
    pipe, _session, gotohp, ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert sorted(gotohp.calls[0]["files"]) == ["IMG_1.HEIC", "IMG_1.MOV"]
    assert gotohp.calls[0]["pair_live_photos"] is True
    assert result.totals.uploaded == 2
    assert asset.delete_calls == 1
    assert {r.state for r in ledger.get_resources("a1")} == {"purged"}


def test_edited_asset_uploads_in_two_unpaired_passes(make_pipeline, settings: Settings) -> None:
    """The edited render shares a stem with its still, so it must be uploaded in
    a separate pass with pairing off or gotohp would treat them as a pair."""
    settings.edited_policy = "both"
    asset = FakePhotoAsset(
        "a1", filename="IMG_1.HEIC", adjustment_type="crop", edited_size=16, asset_date=days_ago(30)
    )
    pipe, _session, gotohp, _ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    by_dir = {c["directory"]: c for c in gotohp.calls}
    assert by_dir["media"]["files"] == ["IMG_1.HEIC"]
    assert by_dir["media"]["pair_live_photos"] is True
    assert by_dir["edited"]["files"] == ["IMG_1_edited.JPG"]
    assert by_dir["edited"]["pair_live_photos"] is False
    assert result.totals.purged_assets == 1


def test_remote_duplicate_counts_as_confirmed_and_allows_deletion(make_pipeline) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    gotohp = FakeGotohp(remote_duplicate={"IMG_1.HEIC"})
    pipe, _session, _gotohp, _ledger = make_pipeline([asset], gotohp)

    result = pipe.run("run-1")

    assert result.totals.uploaded == 1
    assert asset.delete_calls == 1


# --- Deletion must not happen ---------------------------------------------


def test_recent_asset_is_uploaded_but_never_deleted(make_pipeline, settings: Settings) -> None:
    """The grace period exists so a photo still uploading from a phone is never
    removed from iCloud."""
    settings.delete_grace_days = 7
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(2))
    pipe, _session, _gotohp, ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.totals.uploaded == 1
    assert result.totals.skipped_recent == 1
    assert asset.delete_calls == 0
    assert ledger.get_asset("a1").purged_at is None


def test_failed_upload_prevents_deletion(make_pipeline) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, _gotohp, ledger = make_pipeline([asset], FakeGotohp(fail={"IMG_1.HEIC"}))

    result = pipe.run("run-1")

    assert result.totals.failed == 1
    assert asset.delete_calls == 0
    assert ledger.get_resources("a1")[0].state == "failed"


def test_unconfirmed_skip_prevents_deletion(make_pipeline) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, _gotohp, _ledger = make_pipeline(
        [asset], FakeGotohp(skip_unconfirmed={"IMG_1.HEIC"})
    )

    pipe.run("run-1")

    assert asset.delete_calls == 0


def test_file_missing_from_the_summary_prevents_deletion(make_pipeline) -> None:
    """Silence from gotohp is not consent. An unreported file must block."""
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, _gotohp, ledger = make_pipeline([asset], FakeGotohp(omit={"IMG_1.HEIC"}))

    pipe.run("run-1")

    assert asset.delete_calls == 0
    row = ledger.get_resources("a1")[0]
    assert row.state == "failed"
    assert "not reported" in (row.error or "")


def test_partially_confirmed_live_photo_is_not_deleted(make_pipeline) -> None:
    """If only the still uploads, deleting would lose the motion component."""
    asset = FakePhotoAsset(
        "a1", filename="IMG_1.HEIC", is_live_photo=True, asset_date=days_ago(30)
    )
    pipe, _session, _gotohp, ledger = make_pipeline([asset], FakeGotohp(omit={"IMG_1.MOV"}))

    pipe.run("run-1")

    assert asset.delete_calls == 0
    states = {r.resource_key: r.state for r in ledger.get_resources("a1")}
    assert states["original_video"] == "failed"


def test_download_failure_prevents_upload_and_deletion(make_pipeline) -> None:
    asset = FakePhotoAsset(
        "a1",
        filename="IMG_1.HEIC",
        asset_date=days_ago(30),
        resources={"original": make_resource("original", "IMG_1.HEIC", size=999_999)},
    )
    pipe, _session, gotohp, ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.totals.downloaded == 0
    assert asset.delete_calls == 0
    assert ledger.get_resources("a1")[0].state == "failed"
    # Nothing was staged, so gotohp is never invoked.
    assert gotohp.calls == []


def test_deletion_disabled_reports_candidates_without_deleting(
    make_pipeline, settings: Settings
) -> None:
    settings.delete_from_icloud = False
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, _gotohp, _ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert asset.delete_calls == 0
    assert result.totals.purged_assets == 0
    assert any("a1" in entry for entry in result.would_delete)


def test_dry_run_touches_nothing(make_pipeline) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, gotohp, ledger = make_pipeline([asset], dry_run=True)

    result = pipe.run("run-1")

    assert gotohp.calls == []
    assert asset.delete_calls == 0
    assert result.totals.downloaded == 0
    # The asset is still registered so `--dry-run` reports a real plan.
    assert ledger.get_asset("a1") is not None
    assert result.totals.planned == 1


def test_upload_crash_leaves_everything_in_icloud(make_pipeline) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, _gotohp, _ledger = make_pipeline([asset], FakeGotohp(raise_error=True))

    result = pipe.run("run-1")

    assert asset.delete_calls == 0
    assert any("gotohp exploded" in err for err in result.errors)


def test_missing_gotohp_binary_aborts_before_downloading(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pipeline_module, "find_gotohp", lambda _: None)
    monkeypatch.setattr(pipeline_module, "find_exiftool", lambda _: None)
    asset = FakePhotoAsset("a1", asset_date=days_ago(30))

    with Ledger(settings.ledger_path) as ledger:
        pipe = Pipeline(settings, FakeICloudSession([asset]), ledger)
        result = pipe.run("run-1")

    assert result.status == "error"
    assert asset.delete_calls == 0
    assert any("gotohp" in err for err in result.errors)


# --- Resilience and resumption --------------------------------------------


def test_icloud_delete_failure_is_recorded_and_retried_next_run(make_pipeline) -> None:
    asset = FakePhotoAsset(
        "a1", filename="IMG_1.HEIC", asset_date=days_ago(30), delete_error=RuntimeError("HTTP 503")
    )
    pipe, _session, _gotohp, ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.totals.purge_failures == 1
    assert ledger.get_asset("a1").purged_at is None
    # Resources stay confirmed-uploaded, so the next run retries only the delete.
    assert ledger.asset_ready_to_purge("a1") is True


def test_delete_returning_false_is_treated_as_failure(make_pipeline) -> None:
    asset = FakePhotoAsset(
        "a1", filename="IMG_1.HEIC", asset_date=days_ago(30), delete_result=False
    )
    pipe, _session, _gotohp, ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.totals.purge_failures == 1
    assert ledger.get_asset("a1").purged_at is None


def test_second_run_deletes_an_asset_that_aged_past_the_grace_period(
    make_pipeline, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uploaded but too-new assets must be picked up later without re-downloading."""
    settings.delete_grace_days = 7
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(2))

    with Ledger(settings.ledger_path) as ledger:
        pipe, _s, gotohp, _l = make_pipeline([asset], ledger=ledger)
        pipe.run("run-1")
        assert asset.delete_calls == 0
        first_upload_calls = len(gotohp.calls)

        # Time passes: the same asset is now old enough.
        asset.asset_date = days_ago(30)
        pipe2, _s2, gotohp2, _l2 = make_pipeline([asset], ledger=ledger)
        result = pipe2.run("run-2")

    assert asset.delete_calls == 1
    assert result.totals.already_uploaded == 1
    assert result.totals.downloaded == 0
    # No second upload: the ledger already had confirmation.
    assert gotohp2.calls == []
    assert first_upload_calls == 1


def test_exhausted_retries_block_the_asset_and_are_reported(
    make_pipeline, settings: Settings
) -> None:
    """A permanently failing file must not be retried forever, and must show up
    in the report so the operator can intervene."""
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))

    with Ledger(settings.ledger_path) as ledger:
        result = None
        for index in range(MAX_UPLOAD_ATTEMPTS + 1):
            pipe, _s, _g, _l = make_pipeline(
                [asset], FakeGotohp(fail={"IMG_1.HEIC"}), ledger=ledger
            )
            result = pipe.run(f"run-{index}")

    assert asset.delete_calls == 0
    assert result is not None
    assert result.status == "ok_with_blocked"
    assert result.blocked[0]["asset_id"] == "a1"


# --- Batching -------------------------------------------------------------


def test_batching_respects_the_item_cap_and_drains_the_library(
    make_pipeline, settings: Settings
) -> None:
    settings.batch_max_items = 2
    settings.batch_max_bytes = 10**9
    assets = [
        FakePhotoAsset(f"a{i}", filename=f"IMG_{i}.HEIC", asset_date=days_ago(30 + i))
        for i in range(5)
    ]
    pipe, _session, gotohp, _ledger = make_pipeline(assets)

    result = pipe.run("run-1")

    assert result.batches == 3  # 2 + 2 + 1
    assert result.totals.purged_assets == 5
    # Each media pass saw at most the cap, proving staging was cleared between
    # batches rather than accumulating.
    assert all(len(c["files"]) <= 2 for c in gotohp.calls if c["directory"] == "media")


def test_batching_respects_the_byte_cap(make_pipeline, settings: Settings) -> None:
    settings.batch_max_items = 100
    settings.batch_max_bytes = len(DEFAULT_PAYLOAD)  # exactly one asset per batch
    assets = [
        FakePhotoAsset(f"a{i}", filename=f"IMG_{i}.HEIC", asset_date=days_ago(30))
        for i in range(3)
    ]
    pipe, _session, gotohp, _ledger = make_pipeline(assets)

    result = pipe.run("run-1")

    assert result.batches == 3
    assert result.totals.purged_assets == 3
    assert all(len(c["files"]) == 1 for c in gotohp.calls)


def test_byte_cap_is_a_soft_cap(make_pipeline, settings: Settings) -> None:
    """The budget is checked after adding an asset, not before. Otherwise a
    single file larger than the cap could never be downloaded at all."""
    settings.batch_max_items = 100
    settings.batch_max_bytes = 1  # smaller than any real asset
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, gotohp, _ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.batches == 1
    assert gotohp.calls[0]["files"] == ["IMG_1.HEIC"]
    assert result.totals.purged_assets == 1


def test_max_batches_stops_early_and_reports_partial(make_pipeline, settings: Settings) -> None:
    settings.batch_max_items = 1
    settings.max_batches_per_run = 2
    assets = [
        FakePhotoAsset(f"a{i}", filename=f"IMG_{i}.HEIC", asset_date=days_ago(30))
        for i in range(5)
    ]
    pipe, _session, _gotohp, _ledger = make_pipeline(assets)

    result = pipe.run("run-1")

    assert result.batches == 2
    assert result.status == "partial"
    assert result.totals.purged_assets == 2


def test_insufficient_disk_stops_the_run(
    make_pipeline, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.disk_headroom_bytes = 10**12
    asset = FakePhotoAsset("a1", asset_date=days_ago(30))
    pipe, _session, _gotohp, _ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.status == "partial"
    assert asset.delete_calls == 0
    assert any("headroom" in err for err in result.errors)


def test_empty_library_completes_cleanly(make_pipeline) -> None:
    pipe, _session, gotohp, _ledger = make_pipeline([])

    result = pipe.run("run-1")

    assert result.status == "ok"
    assert result.batches == 0
    assert gotohp.calls == []


def test_two_assets_with_the_same_camera_filename_do_not_collide(make_pipeline) -> None:
    """Both are named IMG_1.HEIC in iCloud; they must land as distinct files or
    one would silently overwrite the other before upload."""
    assets = [
        FakePhotoAsset("aaaaaaaa11", filename="IMG_1.HEIC", asset_date=days_ago(40)),
        FakePhotoAsset("bbbbbbbb22", filename="IMG_1.HEIC", asset_date=days_ago(30)),
    ]
    pipe, _session, gotohp, ledger = make_pipeline(assets)

    result = pipe.run("run-1")

    staged = gotohp.calls[0]["files"]
    assert len(staged) == 2
    assert len(set(staged)) == 2
    assert ledger.get_asset("aaaaaaaa11").stem != ledger.get_asset("bbbbbbbb22").stem
    assert result.totals.purged_assets == 2


def test_asset_with_no_downloadable_resource_is_skipped(make_pipeline) -> None:
    asset = FakePhotoAsset(
        "a1",
        asset_date=days_ago(30),
        resources={"original": make_resource("original", "IMG_1.HEIC", url=None)},
    )
    pipe, _session, gotohp, ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert gotohp.calls == []
    assert asset.delete_calls == 0
    assert ledger.get_asset("a1") is None
    assert result.status == "ok"


def test_report_serialises_to_json(make_pipeline) -> None:
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, _gotohp, _ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    payload = json.loads(json.dumps(result.as_dict(), default=str))
    assert payload["totals"]["purged_assets"] == 1
    assert payload["run_id"] == "run-1"


def test_planning_error_on_one_asset_does_not_stop_the_run(
    make_pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = FakePhotoAsset("good", filename="GOOD.HEIC", asset_date=days_ago(30))
    broken = FakePhotoAsset("broken", filename="BAD.HEIC", asset_date=days_ago(30))
    real_plan = pipeline_module.plan_asset

    def flaky(asset, settings, stem):
        if asset.id == "broken":
            raise ValueError("unreadable CloudKit record")
        return real_plan(asset, settings, stem)

    monkeypatch.setattr(pipeline_module, "plan_asset", flaky)
    pipe, _session, _gotohp, _ledger = make_pipeline([broken, good])

    result = pipe.run("run-1")

    assert good.delete_calls == 1
    assert broken.delete_calls == 0
    assert any("unreadable CloudKit record" in err for err in result.errors)


def test_purge_refuses_when_the_ledger_disagrees(
    make_pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final gate re-reads the ledger. If anything reset a resource between
    verification and deletion, the delete must not proceed."""
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _session, _gotohp, ledger = make_pipeline([asset])

    original_gate = ledger.asset_ready_to_purge
    calls = {"n": 0}

    def flaky_gate(asset_id: str) -> bool:
        calls["n"] += 1
        # Allow the asset onto the purge list, then withdraw consent.
        return original_gate(asset_id) and calls["n"] < 2

    monkeypatch.setattr(ledger, "asset_ready_to_purge", flaky_gate)

    result = pipe.run("run-1")

    assert asset.delete_calls == 0
    assert result.totals.purged_assets == 0


def test_asset_dates_far_in_the_future_are_not_deleted(make_pipeline) -> None:
    """A clock-skewed or bogus capture date yields a negative age, which must
    fail the grace check rather than wrap around."""
    future = datetime(2099, 1, 1, tzinfo=UTC)
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=future)
    pipe, _session, _gotohp, _ledger = make_pipeline([asset])

    result = pipe.run("run-1")

    assert result.totals.uploaded == 1
    assert asset.delete_calls == 0
    assert result.totals.skipped_recent == 1


# --- gotohp compatibility gate ---------------------------------------------


def test_incompatible_gotohp_aborts_before_downloading(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v0.8.1 release cannot upload headlessly. Discovering that after a
    batch has been fetched wastes the bandwidth and marks every file failed, so
    the check must happen first."""
    binary = tmp_path / "gotohp-cli"
    binary.write_text("#!/bin/sh")
    monkeypatch.setattr(pipeline_module, "find_gotohp", lambda _: binary)
    monkeypatch.setattr(pipeline_module, "find_exiftool", lambda _: None)

    def incompatible(_binary):
        raise IncompatibleGotohp("gotohp does not support --no-tui")

    monkeypatch.setattr(pipeline_module, "verify_compatible", incompatible)
    downloaded: list[object] = []
    monkeypatch.setattr(
        pipeline_module, "download_batch", lambda *a, **k: downloaded.append(a)
    )

    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    with Ledger(settings.ledger_path) as ledger:
        result = Pipeline(settings, FakeICloudSession([asset]), ledger).run("run-1")

    assert result.status == "error"
    assert downloaded == [], "nothing should have been downloaded"
    assert result.totals.downloaded == 0
    assert asset.delete_calls == 0
    assert any("--no-tui" in err for err in result.errors)


def test_compatibility_is_checked_once_per_run(make_pipeline, monkeypatch) -> None:
    """Probing per batch would spawn a subprocess for every batch of a long run."""
    assets = [
        FakePhotoAsset(f"a{i}", filename=f"IMG_{i}.HEIC", asset_date=days_ago(30))
        for i in range(3)
    ]
    pipe, _s, _g, _l = make_pipeline(assets)
    pipe.settings.batch_max_items = 1
    # Patched after the fixture, which installs its own no-op stub.
    calls: list[object] = []
    monkeypatch.setattr(pipeline_module, "verify_compatible", calls.append)

    result = pipe.run("run-1")

    assert result.batches == 3
    assert len(calls) == 1, "the probe should not run per batch"


def test_dry_run_skips_the_compatibility_probe(make_pipeline, monkeypatch) -> None:
    """A dry run never invokes gotohp, so planning should still work even with an
    inadequate binary installed."""
    asset = FakePhotoAsset("a1", filename="IMG_1.HEIC", asset_date=days_ago(30))
    pipe, _s, _g, _l = make_pipeline([asset], dry_run=True)

    def incompatible(_binary):
        raise IncompatibleGotohp("too old")

    monkeypatch.setattr(pipeline_module, "verify_compatible", incompatible)

    result = pipe.run("run-1")

    assert result.status == "ok"
    assert result.totals.planned == 1
