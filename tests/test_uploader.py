"""Tests for parsing gotohp's JSON summary.

This is the evidence we accept before deleting from iCloud, so misreading it is
the most dangerous bug in the project. These tests pin the shape of gotohp's
output as of v0.8.1 (see ``cli.go``: ``uploadSummary`` / ``uploadResult``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from icloud_to_gphotos.uploader import (
    UploadError,
    UploadReport,
    extract_json_object,
    parse_summary,
    upload_directory,
)


def test_extract_json_object_from_pure_json() -> None:
    payload = {"total": 1, "succeeded": 1}
    assert extract_json_object(json.dumps(payload)) == payload


def test_extract_json_object_ignores_leading_log_lines() -> None:
    payload = {"total": 2, "results": [{"path": "a.jpg", "success": True}]}
    text = f"time=... level=INFO msg=starting\nsome noise\n{json.dumps(payload, indent=2)}\n"

    assert extract_json_object(text) == payload


def test_extract_json_object_prefers_outer_object_over_nested_ones() -> None:
    """The widest span wins, so a nested result object can never be mistaken
    for the summary."""
    payload = {
        "total": 1,
        "results": [{"path": "a.jpg", "success": True, "mediaKey": "k"}],
    }

    assert extract_json_object(json.dumps(payload)) == payload


def test_extract_json_object_returns_none_when_absent() -> None:
    assert extract_json_object("no json here at all") is None
    assert extract_json_object("") is None


def test_parse_summary_marks_successful_upload() -> None:
    report = parse_summary(
        {
            "total": 1,
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "results": [
                {"path": "/staging/media/IMG_1.HEIC", "success": True, "mediaKey": "abc"}
            ],
        }
    )

    assert report.uploaded_filenames == {"IMG_1.HEIC"}
    assert report.verdicts[0].media_key == "abc"


@pytest.mark.parametrize(
    "skip_code",
    ["remote-duplicate", "remote-live-photo-component-exists"],
)
def test_remote_duplicates_count_as_confirmed(skip_code: str) -> None:
    """Content already in Google Photos is the goal state, so it is safe to
    delete from iCloud even though this run did not upload it."""
    report = parse_summary(
        {
            "total": 1,
            "skipped": 1,
            "results": [
                {
                    "path": "/staging/media/IMG_1.HEIC",
                    "success": False,
                    "skipped": True,
                    "skipCode": skip_code,
                    "skipReason": "already exists remotely",
                }
            ],
        }
    )

    assert report.uploaded_filenames == {"IMG_1.HEIC"}


@pytest.mark.parametrize(
    "skip_code",
    ["incomplete-live-photo-skipped", "ambiguous-filename-stem", "unknown-future-code"],
)
def test_other_skip_codes_are_not_confirmed(skip_code: str) -> None:
    """Anything gotohp did not actually place in Google Photos must block
    deletion, including skip codes we have never seen before."""
    report = parse_summary(
        {
            "total": 1,
            "skipped": 1,
            "results": [
                {
                    "path": "/staging/media/IMG_1.HEIC",
                    "success": False,
                    "skipped": True,
                    "skipCode": skip_code,
                }
            ],
        }
    )

    assert report.uploaded_filenames == set()
    assert report.verdicts[0].uploaded is False


def test_failed_upload_is_not_confirmed_and_keeps_its_reason() -> None:
    report = parse_summary(
        {
            "total": 1,
            "failed": 1,
            "results": [
                {
                    "path": "/staging/media/IMG_1.HEIC",
                    "success": False,
                    "error": "quota exceeded",
                }
            ],
        }
    )

    assert report.uploaded_filenames == set()
    assert report.verdicts[0].reason == "quota exceeded"


def test_live_photo_pair_credits_both_components() -> None:
    """gotohp reports a paired Live Photo once but uploads two files; both
    ledger rows must be credited or the asset is never deletable."""
    report = parse_summary(
        {
            "total": 1,
            "succeeded": 1,
            "results": [
                {
                    "path": "/staging/media/IMG_1.HEIC",
                    "paths": ["/staging/media/IMG_1.HEIC", "/staging/media/IMG_1.MOV"],
                    "success": True,
                    "mediaKey": "pair-key",
                }
            ],
        }
    )

    assert report.uploaded_filenames == {"IMG_1.HEIC", "IMG_1.MOV"}
    assert all(v.media_key == "pair-key" for v in report.verdicts)


def test_parse_summary_tolerates_missing_and_malformed_fields() -> None:
    report = parse_summary({"results": [{"success": True}, "not-a-dict", {}]})

    # An entry with no path yields no verdict rather than crashing.
    assert report.uploaded_filenames == set()
    assert report.total == 0


def test_parse_summary_collects_warnings() -> None:
    report = parse_summary(
        {
            "total": 0,
            "warnings": [{"code": "ambiguous-filename-stem", "message": "two IMG_1"}],
        }
    )

    assert report.warnings[0]["code"] == "ambiguous-filename-stem"


def test_merge_accumulates_two_passes() -> None:
    first = parse_summary(
        {"total": 1, "succeeded": 1, "results": [{"path": "a.HEIC", "success": True}]}
    )
    second = parse_summary(
        {"total": 1, "succeeded": 1, "results": [{"path": "b_edited.JPG", "success": True}]}
    )

    first.merge(second)

    assert first.total == 2
    assert first.uploaded_filenames == {"a.HEIC", "b_edited.JPG"}


def test_upload_directory_skips_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "media"
    empty.mkdir()

    report = upload_directory(
        empty, binary=tmp_path / "does-not-exist", threads=1, pair_live_photos=True
    )

    assert report == UploadReport()


def test_upload_directory_ignores_partial_download_files(tmp_path: Path) -> None:
    """A leftover ``.part`` file must not make an otherwise-empty directory look
    like it has work in it."""
    staging = tmp_path / "media"
    staging.mkdir()
    (staging / ".IMG_1.HEIC.part").write_bytes(b"partial")

    report = upload_directory(
        staging, binary=tmp_path / "nope", threads=1, pair_live_photos=False
    )

    assert report.total == 0


def test_upload_directory_raises_when_binary_missing(tmp_path: Path) -> None:
    staging = tmp_path / "media"
    staging.mkdir()
    (staging / "IMG_1.HEIC").write_bytes(b"data")

    with pytest.raises(UploadError, match="not found"):
        upload_directory(
            staging,
            binary=tmp_path / "definitely-not-here",
            threads=1,
            pair_live_photos=False,
        )
