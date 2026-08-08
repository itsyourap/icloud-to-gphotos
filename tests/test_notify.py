"""Tests for run reports, log pruning, and ntfy notifications."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.notify import (
    format_summary,
    human_bytes,
    new_run_id,
    notify,
    prune_old,
    write_report,
)


def test_write_report_persists_json(settings: Settings) -> None:
    path = write_report(settings, "run-1", {"status": "ok", "totals": {"downloaded": 3}})

    assert path == settings.report_dir / "run-1.json"
    assert json.loads(path.read_text(encoding="utf-8"))["totals"]["downloaded"] == 3


def test_write_report_serialises_non_json_types(settings: Settings) -> None:
    """Reports contain Paths and datetimes; they must not blow up the run."""
    path = write_report(settings, "run-1", {"log": Path("/tmp/x.log"), "n": {1, 2}})

    assert "x.log" in path.read_text(encoding="utf-8")


def test_prune_old_keeps_the_newest(tmp_path: Path) -> None:
    for index in range(5):
        target = tmp_path / f"run-{index}.log"
        target.write_text("x")
        os.utime(target, (time.time() + index, time.time() + index))

    prune_old(tmp_path, keep=2, pattern="*.log")

    remaining = sorted(p.name for p in tmp_path.glob("*.log"))
    assert remaining == ["run-3.log", "run-4.log"]


def test_prune_old_ignores_other_patterns(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("x")
    (tmp_path / "keep.json").write_text("x")

    prune_old(tmp_path, keep=0, pattern="*.log")

    assert (tmp_path / "keep.json").exists()
    assert not (tmp_path / "a.log").exists()


def test_prune_old_tolerates_a_missing_directory(tmp_path: Path) -> None:
    prune_old(tmp_path / "nope", keep=1)  # must not raise


def test_notify_is_a_no_op_without_a_topic(settings: Settings) -> None:
    assert notify(settings, title="t", message="m") is False


def test_notify_posts_to_the_configured_topic(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ntfy_topic = "my-topic"
    settings.ntfy_token = "secret"
    captured: dict[str, object] = {}

    def fake_post(url: str, content: bytes, headers: dict, timeout: float):
        captured.update(url=url, content=content, headers=headers)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    assert notify(settings, title="Done", message="all good", tags=["x"]) is True
    assert captured["url"] == "https://ntfy.sh/my-topic"
    assert captured["content"] == b"all good"
    headers = captured["headers"]
    assert headers["Title"] == "Done"
    assert headers["Authorization"] == "Bearer secret"
    assert headers["Tags"] == "x"


def test_notify_strips_a_trailing_slash_from_the_server(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ntfy_topic = "t"
    settings.ntfy_server = "https://ntfy.example.com/"
    captured: dict[str, object] = {}

    def fake_post(url: str, content: bytes, headers: dict, timeout: float):
        captured["url"] = url
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    notify(settings, title="t", message="m")

    assert captured["url"] == "https://ntfy.example.com/t"


def test_notification_failure_never_raises(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead ntfy server must not turn a successful migration into a failure."""
    settings.ntfy_topic = "t"

    def boom(*_args, **_kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", boom)

    assert notify(settings, title="t", message="m") is False


def test_notification_http_error_returns_false(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.ntfy_topic = "t"

    def fake_post(url: str, content: bytes, headers: dict, timeout: float):
        return httpx.Response(403, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    assert notify(settings, title="t", message="m") is False


def test_format_summary_covers_the_key_numbers() -> None:
    message = format_summary(
        {
            "status": "ok",
            "duration_seconds": 91.4,
            "batches": 2,
            "totals": {
                "downloaded": 12,
                "bytes_downloaded": 5 * 1024 * 1024,
                "uploaded": 12,
                "failed": 0,
                "purged_assets": 10,
            },
            "library_remaining": 43,
        }
    )

    assert "ok" in message
    assert "12 files" in message
    assert "5.0 MiB" in message
    assert "Deleted from iCloud: 10" in message
    assert "Remaining in iCloud: 43" in message


def test_format_summary_surfaces_blocked_items_and_errors() -> None:
    message = format_summary(
        {
            "status": "ok_with_blocked",
            "totals": {},
            "blocked": [{"asset_id": "a1"}, {"asset_id": "a2"}],
            "errors": ["disk full", "gotohp timeout"],
        }
    )

    assert "2 item(s) stuck" in message
    assert "disk full" in message


def test_format_summary_handles_an_empty_payload() -> None:
    assert "unknown" in format_summary({})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (5 * 1024**2, "5.0 MiB"),
        (3 * 1024**3, "3.0 GiB"),
        (2 * 1024**4, "2.0 TiB"),
        (None, "0 B"),
        ("not-a-number", "0 B"),
    ],
)
def test_human_bytes(value: object, expected: str) -> None:
    assert human_bytes(value) == expected


def test_new_run_id_is_sortable_and_filesystem_safe() -> None:
    run_id = new_run_id()

    assert run_id.endswith("Z")
    assert not set(run_id) & set('<>:"/\\|?*')
