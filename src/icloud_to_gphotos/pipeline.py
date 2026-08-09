"""The migration pipeline.

One run drains the iCloud library in bounded batches:

    collect -> download -> backfill metadata -> upload -> verify -> delete

Deletion is the only irreversible step, and it is gated on three independent
conditions, all of which must hold:

1. Every planned resource of the asset is recorded ``uploaded`` in the ledger,
   which only happens when gotohp reported a media key or a remote duplicate.
2. The asset is at least ``delete_grace_days`` old, so an item still uploading
   from a phone is never removed.
3. ``delete_from_icloud`` is enabled and the run is not a dry run.

Assets are visited oldest-first. That ordering matters: the grace period
protects the newest items, so ascending order steadily drains the backlog
instead of re-examining photos that are too recent to touch.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assets import PlannedAsset, plan_asset, sanitize_stem
from .binaries import find_gotohp
from .config import Settings
from .downloader import download_batch, resource_path
from .icloud_client import ICloudSession
from .ledger import Ledger
from .metadata import MetadataReport, backfill_batch, find_exiftool
from .uploader import UploadError, UploadReport, upload_directory, verify_compatible

LOGGER = logging.getLogger(__name__)


@dataclass
class RunTotals:
    """Cumulative counters for one run."""

    scanned: int = 0
    planned: int = 0
    downloaded: int = 0
    bytes_downloaded: int = 0
    uploaded: int = 0
    failed: int = 0
    purged_assets: int = 0
    purge_failures: int = 0
    skipped_recent: int = 0
    already_uploaded: int = 0

    def as_dict(self) -> dict[str, int]:
        """Serialise for the JSON run report."""
        return {
            "scanned": self.scanned,
            "planned": self.planned,
            "downloaded": self.downloaded,
            "bytes_downloaded": self.bytes_downloaded,
            "uploaded": self.uploaded,
            "failed": self.failed,
            "purged_assets": self.purged_assets,
            "purge_failures": self.purge_failures,
            "skipped_recent": self.skipped_recent,
            "already_uploaded": self.already_uploaded,
        }


@dataclass
class RunResult:
    """Everything a run produced, ready to serialise into a report."""

    run_id: str
    status: str = "ok"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = 0.0
    batches: int = 0
    dry_run: bool = False
    totals: RunTotals = field(default_factory=RunTotals)
    metadata: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    library_remaining: int | None = None
    would_delete: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the JSON run report."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 1),
            "batches": self.batches,
            "dry_run": self.dry_run,
            "totals": self.totals.as_dict(),
            "metadata": self.metadata,
            "uploads": self.uploads,
            "errors": self.errors,
            "blocked": self.blocked,
            "library_remaining": self.library_remaining,
            "would_delete": self.would_delete[:200],
        }


@dataclass(slots=True)
class _Batch:
    """A bounded unit of work: assets to download plus assets ready to delete."""

    to_download: list[PlannedAsset] = field(default_factory=list)
    ready_to_purge: list[PlannedAsset] = field(default_factory=list)
    planned_bytes: int = 0

    def __bool__(self) -> bool:
        return bool(self.to_download or self.ready_to_purge)


class Pipeline:
    """Coordinates one full migration run."""

    def __init__(
        self,
        settings: Settings,
        session: ICloudSession,
        ledger: Ledger,
        *,
        dry_run: bool = False,
    ) -> None:
        self.settings = settings
        self.session = session
        self.ledger = ledger
        self.dry_run = dry_run
        self.exiftool = find_exiftool(settings.exiftool_binary)
        self.gotohp = find_gotohp(settings.gotohp_binary)
        self._staging = {
            "media": settings.media_staging_dir,
            "edited": settings.edited_staging_dir,
        }
        self._last_upload = UploadReport()

    # --- Public entry point -------------------------------------------------

    def run(self, run_id: str) -> RunResult:
        """Execute a full run and return its result."""
        result = RunResult(run_id=run_id, dry_run=self.dry_run)
        started = time.monotonic()

        if self.gotohp is None:
            result.status = "error"
            result.errors.append(
                "gotohp CLI not found. Set I2G_GOTOHP_BINARY or place it in ./bin. "
                "See docs/SETUP.md."
            )
            result.duration_seconds = time.monotonic() - started
            return result

        # Checked before any download: an unusable gotohp would otherwise only
        # surface after a whole batch has been fetched, wasting the bandwidth
        # and leaving every file recorded as failed.
        if not self.dry_run:
            try:
                verify_compatible(self.gotohp)
            except UploadError as exc:
                result.status = "error"
                result.errors.append(str(exc))
                LOGGER.error("%s", exc)
                result.duration_seconds = time.monotonic() - started
                return result

        if self.exiftool is None and self.settings.backfill_metadata:
            LOGGER.warning(
                "exiftool not found; HEIC and video capture dates cannot be verified "
                "or repaired. See docs/SETUP.md."
            )

        self._clear_staging()
        assets = self.session.iter_all_assets()

        try:
            while True:
                if self.settings.max_batches_per_run is not None and (
                    result.batches >= self.settings.max_batches_per_run
                ):
                    LOGGER.info("Reached max_batches_per_run=%s", self.settings.max_batches_per_run)
                    result.status = "partial"
                    break

                budget = self._byte_budget()
                if budget <= 0:
                    message = (
                        "Free disk is at or below the configured headroom of "
                        f"{_human(self.settings.disk_headroom_bytes)}; stopping early."
                    )
                    LOGGER.error(message)
                    result.errors.append(message)
                    result.status = "partial"
                    break

                batch = self._collect_batch(assets, result, budget)
                if not batch:
                    break

                result.batches += 1
                LOGGER.info(
                    "Batch %d: %d asset(s) to download (~%s), %d ready to delete",
                    result.batches,
                    len(batch.to_download),
                    _human(batch.planned_bytes),
                    len(batch.ready_to_purge),
                )
                self._process_batch(batch, result)
                self._clear_staging()
        except KeyboardInterrupt:
            result.status = "interrupted"
            result.errors.append("Run interrupted by operator.")
            LOGGER.warning("Interrupted; state is preserved in the ledger.")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            result.status = "error"
            result.errors.append(f"{type(exc).__name__}: {exc}")
            LOGGER.exception("Run failed")

        result.blocked = [
            {
                "asset_id": row.asset_id,
                "resource": row.resource_key,
                "file": row.filename,
                "attempts": row.attempts,
                "error": row.error,
            }
            for row in self.ledger.blocked_resources()
        ]
        if result.blocked and result.status == "ok":
            result.status = "ok_with_blocked"

        try:
            result.library_remaining = self.session.library_size()
        except Exception as exc:  # noqa: BLE001 - informational only
            LOGGER.debug("Could not read library size: %s", exc)

        result.duration_seconds = time.monotonic() - started
        return result

    # --- Batch collection ---------------------------------------------------

    def _collect_batch(
        self, assets: Iterator[Any], result: RunResult, byte_budget: int
    ) -> _Batch:
        """Pull assets off the library iterator until a batch budget is reached.

        The iterator is shared across batches for the whole run, so each batch
        continues where the last stopped and the run terminates when the library
        is exhausted.
        """
        batch = _Batch()
        now = datetime.now(UTC)

        for asset in assets:
            result.totals.scanned += 1
            try:
                planned = self._plan(asset)
            except Exception as exc:  # noqa: BLE001 - one bad asset must not stop the run
                LOGGER.warning("Could not plan asset %s: %s", getattr(asset, "id", "?"), exc)
                result.errors.append(f"plan failed for {getattr(asset, 'id', '?')}: {exc}")
                continue

            if not planned.resources:
                continue

            self._register(planned)
            rows = {row.resource_key: row for row in self.ledger.get_resources(planned.asset_id)}
            outstanding = [
                res
                for res in planned.resources
                if not (row := rows.get(res.key))
                or (not row.is_uploaded and not row.is_exhausted)
            ]

            if outstanding:
                batch.to_download.append(planned)
                batch.planned_bytes += sum(res.size or 0 for res in outstanding)
                result.totals.planned += len(outstanding)
            elif self.ledger.asset_ready_to_purge(planned.asset_id):
                # Uploaded on an earlier run but not yet deletable then.
                result.totals.already_uploaded += 1
                if self._purge_allowed(planned, now):
                    batch.ready_to_purge.append(planned)
                else:
                    result.totals.skipped_recent += 1
            elif self.ledger.asset_blocked(planned.asset_id):
                LOGGER.debug("Asset %s is blocked by exhausted retries.", planned.asset_id)

            if (
                len(batch.to_download) >= self.settings.batch_max_items
                or batch.planned_bytes >= byte_budget
            ):
                break

        return batch

    def _plan(self, asset: Any) -> PlannedAsset:
        preferred = sanitize_stem(Path(asset.filename).stem)
        stem = self.ledger.reserve_stem(asset.id, preferred)
        return plan_asset(asset, self.settings, stem)

    def _register(self, planned: PlannedAsset) -> None:
        with self.ledger.transaction():
            self.ledger.upsert_asset(
                asset_id=planned.asset_id,
                master_id=planned.master_id,
                filename=planned.filename,
                stem=planned.stem,
                item_type=planned.item_type,
                asset_date=planned.asset_date,
                added_date=planned.added_date,
                is_live_photo=planned.is_live_photo,
                has_adjustments=planned.has_adjustments,
            )
            for res in planned.resources:
                self.ledger.upsert_resource(
                    asset_id=planned.asset_id,
                    resource_key=res.key,
                    filename=res.filename,
                    staging_root=res.staging_root,
                    size=res.size,
                    checksum=res.resource.checksum,
                )

    # --- Batch processing ---------------------------------------------------

    def _process_batch(self, batch: _Batch, result: RunResult) -> None:
        if batch.to_download:
            self._download(batch, result)
            self._backfill(batch, result)
            self._upload(result)
            self._verify(batch, result)

        self._purge(batch, result)

    def _download(self, batch: _Batch, result: RunResult) -> None:
        if self.dry_run:
            LOGGER.info("[dry-run] would download %d asset(s)", len(batch.to_download))
            return

        # Resolved here, on the main thread: the ledger's SQLite connection
        # cannot be read from the download workers.
        skip_keys = frozenset(
            (planned.asset_id, row.resource_key)
            for planned in batch.to_download
            for row in self.ledger.get_resources(planned.asset_id)
            if row.is_uploaded or row.is_exhausted
        )

        outcome = download_batch(
            batch.to_download,
            self._staging,
            workers=self.settings.download_workers,
            skip_keys=skip_keys,
        )
        with self.ledger.transaction():
            for item in outcome.files:
                self.ledger.mark_downloaded(item.asset_id, item.resource_key, item.size)
            for failure in outcome.failures:
                self.ledger.mark_failed(
                    failure.asset_id, failure.resource_key, f"download: {failure.error}"
                )

        result.totals.downloaded += len(outcome.files)
        result.totals.bytes_downloaded += outcome.bytes_written
        result.totals.failed += len(outcome.failures)
        LOGGER.info(
            "Downloaded %d file(s), %s; %d failure(s)",
            len(outcome.files),
            _human(outcome.bytes_written),
            len(outcome.failures),
        )

    def _backfill(self, batch: _Batch, result: RunResult) -> None:
        if self.dry_run:
            return
        items: list[tuple[PlannedAsset, Path]] = []
        for planned in batch.to_download:
            for res in planned.resources:
                path = resource_path(self._staging, res)
                if path.exists():
                    items.append((planned, path))
        if not items:
            return
        report: MetadataReport = backfill_batch(
            items, exiftool=self.exiftool, enabled=self.settings.backfill_metadata
        )
        LOGGER.info(
            "Metadata: %d date(s) and %d GPS tag(s) backfilled across %d file(s)",
            report.dates_written,
            report.gps_written,
            report.files_examined,
        )
        result.metadata.append(report.as_dict())
        result.errors.extend(report.errors[:10])

    def _upload(self, result: RunResult) -> None:
        if self.dry_run:
            LOGGER.info("[dry-run] would upload staged media")
            return

        combined = UploadReport()
        # Two passes: originals and Live Photo components need pairing, edited
        # renders must not be paired (they share a stem with their still).
        passes = (
            (self._staging["media"], self.settings.pair_live_photos),
            (self._staging["edited"], False),
        )
        for directory, pair in passes:
            try:
                report = upload_directory(
                    directory,
                    binary=self.gotohp,  # type: ignore[arg-type]
                    threads=self.settings.upload_threads,
                    pair_live_photos=pair,
                    ignore_apple_metadata=self.settings.ignore_apple_metadata,
                    config_path=self.settings.gotohp_config,
                )
            except UploadError as exc:
                LOGGER.error("Upload pass for %s failed: %s", directory.name, exc)
                result.errors.append(f"upload({directory.name}): {exc}")
                continue
            combined.merge(report)

        result.uploads.append(combined.as_dict())
        self._last_upload = combined

    def _verify(self, batch: _Batch, result: RunResult) -> None:
        """Reconcile gotohp's verdicts back onto ledger rows."""
        if self.dry_run:
            return

        combined: UploadReport = getattr(self, "_last_upload", UploadReport())
        confirmed = combined.uploaded_filenames
        reasons = {v.filename: v for v in combined.verdicts}
        now = datetime.now(UTC)

        with self.ledger.transaction():
            for planned in batch.to_download:
                for res in planned.resources:
                    row = self.ledger.get_resource(planned.asset_id, res.key)
                    if row is not None and row.is_uploaded:
                        continue
                    path = resource_path(self._staging, res)
                    if res.filename in confirmed:
                        verdict = reasons.get(res.filename)
                        self.ledger.mark_uploaded(
                            planned.asset_id,
                            res.key,
                            verdict.media_key if verdict else None,
                        )
                        result.totals.uploaded += 1
                    elif row is not None and row.state == "downloaded":
                        verdict = reasons.get(res.filename)
                        reason = (
                            verdict.reason
                            if verdict and verdict.reason
                            else "not reported by gotohp"
                        )
                        self.ledger.mark_failed(
                            planned.asset_id, res.key, f"upload: {reason}"
                        )
                        result.totals.failed += 1
                        LOGGER.warning(
                            "Not confirmed remote: %s (%s)", res.filename, reason
                        )
                    elif not path.exists() and row is not None and row.state == "failed":
                        # Download already failed; nothing further to record.
                        pass

        for planned in batch.to_download:
            if self.ledger.asset_ready_to_purge(planned.asset_id):
                if self._purge_allowed(planned, now):
                    batch.ready_to_purge.append(planned)
                else:
                    result.totals.skipped_recent += 1

    # --- Deletion -----------------------------------------------------------

    def _purge_allowed(self, planned: PlannedAsset, now: datetime) -> bool:
        """True when the asset satisfies the age grace period."""
        return planned.age_days(now) >= self.settings.delete_grace_days

    def _purge(self, batch: _Batch, result: RunResult) -> None:
        """Delete verified assets from iCloud, then their local copies."""
        if not batch.ready_to_purge:
            return

        if not self.settings.delete_from_icloud or self.dry_run:
            label = "dry-run" if self.dry_run else "deletion disabled"
            LOGGER.info(
                "[%s] %d asset(s) verified in Google Photos and eligible for deletion",
                label,
                len(batch.ready_to_purge),
            )
            result.would_delete.extend(
                f"{p.asset_id} ({p.filename})" for p in batch.ready_to_purge
            )
            return

        for planned in batch.ready_to_purge:
            # Re-check against the ledger rather than trusting the batch list;
            # this is the last gate before an irreversible action.
            if not self.ledger.asset_ready_to_purge(planned.asset_id):
                LOGGER.error(
                    "Refusing to delete %s: ledger no longer reports it fully uploaded.",
                    planned.asset_id,
                )
                continue
            try:
                deleted = planned.asset.delete()
            except Exception as exc:  # noqa: BLE001 - per-asset failure is recoverable
                LOGGER.warning("iCloud delete failed for %s: %s", planned.asset_id, exc)
                self.ledger.mark_asset_purge_failed(planned.asset_id, str(exc))
                result.totals.purge_failures += 1
                continue

            if not deleted:
                LOGGER.warning("iCloud reported no deletion for %s", planned.asset_id)
                self.ledger.mark_asset_purge_failed(planned.asset_id, "delete returned false")
                result.totals.purge_failures += 1
                continue

            self.ledger.mark_asset_purged(planned.asset_id)
            result.totals.purged_assets += 1
            self._remove_local(planned)

        LOGGER.info(
            "Deleted %d asset(s) from iCloud (%d failure(s))",
            result.totals.purged_assets,
            result.totals.purge_failures,
        )

    def _remove_local(self, planned: PlannedAsset) -> None:
        for res in planned.resources:
            path = resource_path(self._staging, res)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # noqa: PERF203 - staging is wiped anyway
                LOGGER.debug("Could not remove %s: %s", path, exc)

    # --- Housekeeping -------------------------------------------------------

    def _clear_staging(self) -> None:
        """Empty the staging directories between batches."""
        for directory in self._staging.values():
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)

    def _byte_budget(self) -> int:
        """Bytes this batch may download.

        The configured cap, shrunk to whatever the filesystem can actually spare
        above the headroom. Returns 0 or less when there is no room to work.
        """
        assert self.settings.staging_dir is not None
        try:
            free = shutil.disk_usage(self.settings.staging_dir).free
        except OSError:  # Unmounted or unreadable; trust the configured cap.
            return self.settings.batch_max_bytes
        return min(self.settings.batch_max_bytes, free - self.settings.disk_headroom_bytes)


def _human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"
