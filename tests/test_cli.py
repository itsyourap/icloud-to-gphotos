"""Tests for the CLI surface.

Exit codes matter more than output here: a scheduler (cron or Task Scheduler)
decides whether to alert based on them, and code 2 specifically means "a human
must run `i2g login`".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from icloud_to_gphotos import cli as cli_module
from icloud_to_gphotos.cli import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, EXIT_REAUTH, app
from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.icloud_client import ReauthRequired
from icloud_to_gphotos.ledger import Ledger
from icloud_to_gphotos.pipeline import RunResult

runner = CliRunner()


@pytest.fixture
def wired(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the CLI at temporary settings and stub out the network."""
    monkeypatch.setattr(cli_module, "load_settings", lambda **_: settings)
    binary = tmp_path / "gotohp-cli"
    binary.write_text("#!/bin/sh")
    monkeypatch.setattr(cli_module, "find_gotohp", lambda _: binary)
    monkeypatch.setattr(cli_module, "find_exiftool", lambda _: tmp_path / "exiftool")
    monkeypatch.setattr(cli_module, "check_credentials", lambda *_a, **_k: (True, "me@gmail.com"))
    monkeypatch.setattr(
        cli_module, "session_health", lambda _s: {"ok": True, "cookie_dir": str(tmp_path)}
    )
    return settings


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == EXIT_OK
    for command in ("login", "doctor", "run", "status", "report"):
        assert command in result.output


def test_missing_configuration_exits_with_the_config_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kwargs):
        raise ValueError("icloud_username is required")

    monkeypatch.setattr(cli_module, "load_settings", boom)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == EXIT_CONFIG
    assert ".env" in result.output


def test_doctor_passes_when_everything_is_present(wired: Settings) -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == EXIT_OK
    assert "Ready" in result.output


def test_doctor_fails_without_the_gotohp_binary(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "find_gotohp", lambda _: None)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == EXIT_FAILED
    assert "gotohp" in result.output


def test_doctor_fails_without_google_credentials(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module, "check_credentials", lambda *_a, **_k: (False, "no credentials stored")
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == EXIT_FAILED


def test_doctor_fails_when_the_icloud_session_is_stale(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli_module, "session_health", lambda _s: {"ok": False, "reason": "trust token expired"}
    )

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == EXIT_FAILED
    assert "i2g login" in result.output


def test_doctor_warns_but_passes_without_exiftool_when_backfill_is_off(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    wired.backfill_metadata = False
    monkeypatch.setattr(cli_module, "find_exiftool", lambda _: None)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == EXIT_OK
    assert "warning" in result.output.lower()


def test_doctor_fails_without_exiftool_when_backfill_is_on(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silently degrading metadata quality is worse than refusing to start."""
    wired.backfill_metadata = True
    monkeypatch.setattr(cli_module, "find_exiftool", lambda _: None)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == EXIT_FAILED


def test_doctor_states_the_deletion_policy(wired: Settings) -> None:
    """The irreversible setting should never be a surprise."""
    result = runner.invoke(app, ["doctor"])

    assert "delete_from_icloud" in result.output


def test_run_reports_reauth_with_its_own_exit_code(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stale(_settings):
        raise ReauthRequired("session is not trusted")

    monkeypatch.setattr(cli_module, "connect", stale)
    sent: list[dict] = []
    monkeypatch.setattr(
        cli_module.notify, "notify", lambda _s, **kwargs: sent.append(kwargs) or True
    )

    result = runner.invoke(app, ["run"])

    assert result.exit_code == EXIT_REAUTH
    assert sent and sent[0]["priority"] == "urgent"
    assert "i2g login" in sent[0]["message"]


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, result: RunResult) -> None:
    monkeypatch.setattr(cli_module, "connect", lambda _s: object())

    class StubPipeline:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, _run_id: str) -> RunResult:
            return result

    monkeypatch.setattr(cli_module, "Pipeline", StubPipeline)


def test_run_writes_a_report_and_exits_zero_on_success(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = RunResult(run_id="run-x", status="ok")
    outcome.totals.uploaded = 4
    outcome.totals.purged_assets = 4
    _stub_pipeline(monkeypatch, outcome)
    monkeypatch.setattr(cli_module.notify, "notify", lambda *_a, **_k: True)

    result = runner.invoke(app, ["run"])

    assert result.exit_code == EXIT_OK
    reports = list(wired.report_dir.glob("*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text(encoding="utf-8"))["totals"]["purged_assets"] == 4


def test_run_exits_nonzero_when_the_pipeline_errors(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = RunResult(run_id="run-x", status="error", errors=["disk full"])
    _stub_pipeline(monkeypatch, outcome)
    sent: list[dict] = []
    monkeypatch.setattr(
        cli_module.notify, "notify", lambda _s, **kwargs: sent.append(kwargs) or True
    )

    result = runner.invoke(app, ["run"])

    assert result.exit_code == EXIT_FAILED
    assert sent and sent[0]["priority"] == "high"


def test_run_records_the_run_in_the_ledger(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = RunResult(run_id="run-x", status="ok")
    outcome.totals.downloaded = 2
    _stub_pipeline(monkeypatch, outcome)
    monkeypatch.setattr(cli_module.notify, "notify", lambda *_a, **_k: True)

    runner.invoke(app, ["run"])

    with Ledger(wired.ledger_path) as ledger:
        runs = ledger.recent_runs()
    assert len(runs) == 1
    assert runs[0]["downloaded"] == 2
    assert runs[0]["status"] == "ok"


def test_no_delete_flag_disables_deletion(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "connect", lambda _s: object())

    class StubPipeline:
        def __init__(self, settings, *_args, **kwargs) -> None:
            captured["delete"] = settings.delete_from_icloud
            captured["dry_run"] = kwargs.get("dry_run")

        def run(self, _run_id: str) -> RunResult:
            return RunResult(run_id="run-x", status="ok")

    monkeypatch.setattr(cli_module, "Pipeline", StubPipeline)
    monkeypatch.setattr(cli_module.notify, "notify", lambda *_a, **_k: True)

    runner.invoke(app, ["run", "--no-delete"])

    assert captured["delete"] is False


def test_dry_run_flag_reaches_the_pipeline(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "connect", lambda _s: object())

    class StubPipeline:
        def __init__(self, _settings, *_args, **kwargs) -> None:
            captured["dry_run"] = kwargs.get("dry_run")

        def run(self, _run_id: str) -> RunResult:
            return RunResult(run_id="run-x", status="ok", dry_run=True)

    monkeypatch.setattr(cli_module, "Pipeline", StubPipeline)
    monkeypatch.setattr(cli_module.notify, "notify", lambda *_a, **_k: True)

    runner.invoke(app, ["run", "--dry-run"])

    assert captured["dry_run"] is True


def test_max_batches_flag_overrides_the_setting(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "connect", lambda _s: object())

    class StubPipeline:
        def __init__(self, settings, *_args, **_kwargs) -> None:
            captured["max_batches"] = settings.max_batches_per_run

        def run(self, _run_id: str) -> RunResult:
            return RunResult(run_id="run-x", status="ok")

    monkeypatch.setattr(cli_module, "Pipeline", StubPipeline)
    monkeypatch.setattr(cli_module.notify, "notify", lambda *_a, **_k: True)

    runner.invoke(app, ["run", "--max-batches", "3"])

    assert captured["max_batches"] == 3


def test_status_reports_an_empty_ledger(wired: Settings) -> None:
    result = runner.invoke(app, ["status"])

    assert result.exit_code == EXIT_OK
    assert "Assets seen" in result.output


def test_status_summarises_progress_and_blocked_items(wired: Settings) -> None:
    from datetime import UTC, datetime

    with Ledger(wired.ledger_path) as ledger:
        ledger.upsert_asset(
            asset_id="a1",
            master_id="m1",
            filename="IMG_1.HEIC",
            stem="IMG_1",
            item_type="image",
            asset_date=datetime(2020, 1, 1, tzinfo=UTC),
            added_date=None,
            is_live_photo=False,
            has_adjustments=False,
        )
        ledger.upsert_resource(
            asset_id="a1",
            resource_key="original",
            filename="IMG_1.HEIC",
            staging_root="media",
            size=2048,
            checksum=None,
        )
        ledger.mark_uploaded("a1", "original", "key")
        ledger.mark_asset_purged("a1")
        ledger.start_run("run-1")
        ledger.finish_run(
            "run-1", status="ok", downloaded=1, uploaded=1, failed=0, purged=1, bytes_moved=2048
        )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == EXIT_OK
    assert "run-1" in result.output
    assert "trusted" in result.output


def test_report_says_so_when_there_is_nothing_yet(wired: Settings) -> None:
    result = runner.invoke(app, ["report"])

    assert result.exit_code == EXIT_OK
    assert "No reports" in result.output


def test_report_prints_the_latest_run(wired: Settings) -> None:
    (wired.report_dir / "20260101T000000Z.json").write_text('{"status": "old"}', encoding="utf-8")
    (wired.report_dir / "20260202T000000Z.json").write_text(
        '{"status": "newest"}', encoding="utf-8"
    )

    result = runner.invoke(app, ["report"])

    assert result.exit_code == EXIT_OK
    assert "newest" in result.output


def test_report_can_select_a_specific_run(wired: Settings) -> None:
    (wired.report_dir / "run-42.json").write_text('{"status": "picked"}', encoding="utf-8")

    result = runner.invoke(app, ["report", "--run-id", "run-42"])

    assert result.exit_code == EXIT_OK
    assert "picked" in result.output


def test_report_fails_for_an_unknown_run(wired: Settings) -> None:
    (wired.report_dir / "run-1.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(app, ["report", "--run-id", "nope"])

    assert result.exit_code == EXIT_FAILED


def test_login_reports_a_rejected_two_factor_code(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_settings, _password, _provider):
        raise ReauthRequired("The two-factor code was rejected by Apple.")

    monkeypatch.setattr(cli_module, "interactive_login", refuse)

    result = runner.invoke(app, ["login", "--password", "pw", "--code", "000000"])

    assert result.exit_code == EXIT_REAUTH
    assert "rejected" in result.output


def test_login_stores_a_trusted_session(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def succeed(_settings, password, provider):
        captured["password"] = password
        captured["code"] = provider()
        return object()

    monkeypatch.setattr(cli_module, "interactive_login", succeed)

    result = runner.invoke(app, ["login", "--password", "pw", "--code", "123456"])

    assert result.exit_code == EXIT_OK
    assert captured == {"password": "pw", "code": "123456"}
    assert "Trusted iCloud session stored" in result.output


def test_login_prompts_for_the_password_when_omitted(
    wired: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli_module,
        "interactive_login",
        lambda _s, password, provider: captured.update(password=password, code=provider()),
    )

    result = runner.invoke(app, ["login"], input="typed-password\n654321\n")

    assert result.exit_code == EXIT_OK
    assert captured["password"] == "typed-password"
    assert captured["code"] == "654321"
