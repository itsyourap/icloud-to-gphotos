# ruff: noqa: F811
"""Metadata failures cannot become successful backups or disappear on restart."""

import json
import subprocess
from datetime import datetime
from pathlib import Path

from icloud_to_gphotos import metadata, pipeline
from icloud_to_gphotos.assets import plan_asset
from icloud_to_gphotos.metadata import MetadataReport

from .conftest import FakePhotoAsset, encode_location
from .test_pipeline import make_pipeline  # noqa: F401


def test_metadata_failure_blocks_upload_and_deletion_then_retries(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('metadata-failed')
    pipe, _, uploader, ledger = make_pipeline([asset])
    pipe.settings.backfill_metadata = True
    monkeypatch.setattr(pipeline, 'backfill_batch', lambda *a, **kw: MetadataReport(
        files_examined=1, errors=['synthetic metadata failure']))
    failed = pipe.run('failed')
    assert failed.status == 'partial' and failed.totals.purged_assets == 0
    assert not uploader.calls
    assert ledger.get_resource(asset.id, 'original').state == 'failed'
    # Recreate the pipeline around persisted evidence, not a per-run error flag.
    resumed, _, _, _ = make_pipeline([asset], ledger=ledger)
    monkeypatch.setattr(pipeline, 'backfill_batch', lambda items, **kw: MetadataReport(
        files_examined=len(items), verified_files=[str(path) for _, path in items]))
    assert resumed.run('repaired').totals.purged_assets == 1


def test_legacy_upload_requires_metadata_verification(make_pipeline, monkeypatch):
    asset = FakePhotoAsset('old-upload')
    pipe, _, _, _ = make_pipeline([asset])
    pipe.settings.delete_from_icloud = False
    assert pipe.run('old').totals.uploaded == 1
    pipe.settings.backfill_metadata = True
    monkeypatch.setattr(pipeline, 'backfill_batch', lambda items, **kw: MetadataReport(
        files_examined=len(items), verified_files=[str(path) for _, path in items]))
    assert pipe.run('verify-old').totals.downloaded == 1
    assert pipe.run('verified-unchanged').totals.downloaded == 0
    pipe.settings.delete_from_icloud = True
    assert pipe.run('eligible').totals.purged_assets == 1


def test_one_bad_metadata_file_does_not_block_verified_neighbor(make_pipeline, monkeypatch):
    good, bad = [FakePhotoAsset(x, filename=f'{x}.HEIC') for x in ('good', 'bad')]
    pipe, _, _, ledger = make_pipeline([good, bad])
    pipe.settings.backfill_metadata = True
    monkeypatch.setattr(pipeline, 'backfill_batch', lambda items, **kw: MetadataReport(
        files_examined=len(items), errors=['bad metadata'],
        verified_files=[str(p) for _, p in items if p.name == 'good.HEIC']))
    result = pipe.run('partial-metadata')
    assert result.totals.purged_assets == 1 and good.delete_calls == 1
    assert bad.delete_calls == 0
    assert ledger.get_resource(bad.id, 'original').state == 'failed'


def test_success_exit_without_written_tags_is_not_verification(settings, tmp_path, monkeypatch):
    path = tmp_path/'missing.HEIC'
    path.write_bytes(b'fake-media')
    def exiftool(binary, args):
        return subprocess.CompletedProcess([], 0,
            stdout=json.dumps([{'SourceFile': str(path)}]) if '-j' in args else '', stderr='')
    monkeypatch.setattr(metadata, '_run_exiftool', exiftool)
    planned = plan_asset(FakePhotoAsset('missing-tags'), settings, 'missing')
    report = metadata.backfill_batch([(planned, path)], exiftool=Path('fake-exiftool'))
    assert report.errors
    assert report.verified_files == []
    assert report.dates_written == 0


def test_failed_probe_does_not_overwrite_tags(settings, tmp_path, monkeypatch):
    path = tmp_path/'unreadable.HEIC'
    path.write_bytes(b'fake-media')
    calls = []
    def broken_probe(binary, args):
        calls.append(args)
        return subprocess.CompletedProcess([], 1, stdout='', stderr='probe failed')
    monkeypatch.setattr(metadata, '_run_exiftool', broken_probe)
    planned = plan_asset(FakePhotoAsset('probe'), settings, 'unreadable')
    report = metadata.backfill_batch([(planned, path)], exiftool=Path('fake-exiftool'))
    assert report.errors and not report.verified_files
    assert not any('-overwrite_original' in args for args in calls)


def test_checksumless_metadata_failure_does_not_reset_retry_budget(make_pipeline, monkeypatch):
    from icloud_to_gphotos.ledger import MAX_UPLOAD_ATTEMPTS

    asset = FakePhotoAsset('no-checksum')
    asset.resources['original'].checksum = None
    pipe, _, _, ledger = make_pipeline([asset])
    pipe.settings.backfill_metadata = True
    monkeypatch.setattr(pipeline, 'backfill_batch', lambda *a, **kw: MetadataReport(
        errors=['missing metadata']))
    for index in range(MAX_UPLOAD_ATTEMPTS + 1):
        pipe.run(str(index))
    row = ledger.get_resource(asset.id, 'original')
    assert row.is_exhausted and row.attempts == MAX_UPLOAD_ATTEMPTS
    assert asset.delete_calls == 0


def test_missing_exiftool_retains_heic_source(make_pipeline):
    asset = FakePhotoAsset('no-exiftool')
    pipe, _, _, _ = make_pipeline([asset])
    pipe.settings.backfill_metadata = True
    assert pipe.exiftool is None
    result = pipe.run('no-tool')
    assert result.status == 'partial'
    assert result.totals.purged_assets == 0


def test_gps_timestamp_receipt_survives_restart_and_still_requires_verification(
    make_pipeline, monkeypatch,
):
    timestamp = datetime(2020, 5, 1, 12, 0, 0, 123456)
    asset = FakePhotoAsset(
        'gps-timestamp', location=encode_location(1.0, 2.0, 3.0, timestamp=timestamp),
    )
    pipe, _, uploader, ledger = make_pipeline([asset])
    pipe.settings.backfill_metadata = True
    monkeypatch.setattr(pipeline, 'backfill_batch', lambda *a, **kw: MetadataReport(
        files_examined=1, errors=['synthetic metadata failure']))
    failed = pipe.run('unverified-gps')
    assert failed.status == 'partial'
    assert not uploader.calls and asset.delete_calls == 0
    assert ledger.get_resource(asset.id, 'original').state == 'failed'

    def verified(items, **kwargs):
        assert all(planned.location['timestamp'] == timestamp for planned, _ in items)
        return MetadataReport(verified_files=[str(path) for _, path in items])

    monkeypatch.setattr(pipeline, 'backfill_batch', verified)
    pipe.settings.delete_from_icloud = False
    repaired = pipe.run('verified-gps')
    assert repaired.status == 'ok' and repaired.totals.uploaded == 1

    restarted, _, uploader, _ = make_pipeline([asset], ledger=ledger)
    restarted.settings.delete_from_icloud = True
    final = restarted.run('persisted-gps')
    assert final.status == 'ok' and final.totals.purged_assets == 1
    assert final.totals.downloaded == 0 and not uploader.calls
