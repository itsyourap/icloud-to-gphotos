"""Tests for the gotohp subprocess boundary: command construction and I/O.

Command construction is worth pinning precisely. Dropping
``--upload-incomplete-live-photos``, for example, would make gotohp silently
discard unpaired Live Photo components, which would then never be confirmed and
would sit in iCloud forever.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from icloud_to_gphotos import uploader
from icloud_to_gphotos.uploader import (
    IncompatibleGotohp,
    UploadError,
    check_credentials,
    missing_upload_flags,
    upload_directory,
    verify_compatible,
)

SUMMARY = {
    "total": 1,
    "succeeded": 1,
    "failed": 0,
    "skipped": 0,
    "results": [{"path": "IMG_1.HEIC", "success": True, "mediaKey": "k1"}],
}


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    """A staging directory containing one file."""
    directory = tmp_path / "media"
    directory.mkdir()
    (directory / "IMG_1.HEIC").write_bytes(b"data")
    return directory


@pytest.fixture
def recorded_run(monkeypatch: pytest.MonkeyPatch):
    """Capture the command gotohp would be launched with."""
    calls: list[list[str]] = []

    def install(stdout: str = json.dumps(SUMMARY), stderr: str = "", returncode: int = 0):
        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                args=command, returncode=returncode, stdout=stdout, stderr=stderr
            )

        monkeypatch.setattr(uploader.subprocess, "run", fake_run)
        return calls

    return install


def test_command_includes_the_core_flags(staged: Path, recorded_run) -> None:
    calls = recorded_run()

    upload_directory(staged, binary=Path("gotohp"), threads=5, pair_live_photos=False)

    command = calls[0]
    assert command[:3] == ["gotohp", "upload", str(staged)]
    assert "--recursive" in command
    assert "--no-tui" in command
    assert command[command.index("--threads") + 1] == "5"
    assert "--pair-live-photos" not in command


def test_live_photo_pairing_also_uploads_unpaired_components(
    staged: Path, recorded_run
) -> None:
    """An unpaired component must be uploaded standalone rather than dropped, or
    its asset can never be confirmed and never leaves iCloud."""
    calls = recorded_run()

    upload_directory(staged, binary=Path("gotohp"), threads=1, pair_live_photos=True)

    command = calls[0]
    assert "--pair-live-photos" in command
    assert "--upload-incomplete-live-photos" in command
    assert "--ignore-apple-metadata" not in command


def test_ignore_apple_metadata_is_opt_in(staged: Path, recorded_run) -> None:
    calls = recorded_run()

    upload_directory(
        staged,
        binary=Path("gotohp"),
        threads=1,
        pair_live_photos=True,
        ignore_apple_metadata=True,
    )

    assert "--ignore-apple-metadata" in calls[0]


def test_config_path_is_forwarded(staged: Path, recorded_run, tmp_path: Path) -> None:
    calls = recorded_run()
    config = tmp_path / "gotohp.config"

    upload_directory(
        staged, binary=Path("gotohp"), threads=1, pair_live_photos=False, config_path=config
    )

    command = calls[0]
    assert command[command.index("--config") + 1] == str(config)


def test_no_force_flag_so_duplicate_detection_stays_on(staged: Path, recorded_run) -> None:
    """Duplicate detection is what makes re-runs cheap and makes 'already in
    Google Photos' a valid confirmation."""
    calls = recorded_run()

    upload_directory(staged, binary=Path("gotohp"), threads=1, pair_live_photos=False)

    assert "--force" not in calls[0]
    assert "--delete" not in calls[0]


def test_summary_is_parsed_from_stdout(staged: Path, recorded_run) -> None:
    recorded_run(stdout=f"log line\n{json.dumps(SUMMARY)}")

    report = upload_directory(
        staged, binary=Path("gotohp"), threads=1, pair_live_photos=False
    )

    assert report.uploaded_filenames == {"IMG_1.HEIC"}


def test_nonzero_exit_with_a_summary_trusts_the_per_file_verdicts(
    staged: Path, recorded_run
) -> None:
    """gotohp can exit non-zero after partial success; the summary is still the
    authoritative record of what reached Google Photos."""
    recorded_run(returncode=1)

    report = upload_directory(
        staged, binary=Path("gotohp"), threads=1, pair_live_photos=False
    )

    assert report.uploaded_filenames == {"IMG_1.HEIC"}


def test_missing_summary_raises_with_diagnostics(staged: Path, recorded_run) -> None:
    """No summary means no evidence, so this must fail loudly rather than let the
    caller assume nothing was uploaded and move on."""
    recorded_run(stdout="", stderr="fatal: no credentials configured", returncode=1)

    with pytest.raises(UploadError, match="no credentials configured"):
        upload_directory(staged, binary=Path("gotohp"), threads=1, pair_live_photos=False)


def test_timeout_is_reported_as_an_upload_error(
    staged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 10)

    monkeypatch.setattr(uploader.subprocess, "run", timeout)

    with pytest.raises(UploadError, match="timed out"):
        upload_directory(
            staged, binary=Path("gotohp"), threads=1, pair_live_photos=False, timeout=10
        )


# --- Credential check ------------------------------------------------------


def test_check_credentials_accepts_a_stored_account(
    recorded_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded_run(stdout="active: someone@gmail.com\n")

    ok, detail = check_credentials(Path("gotohp"))

    assert ok is True
    assert "someone@gmail.com" in detail


def test_check_credentials_rejects_an_empty_list(recorded_run) -> None:
    recorded_run(stdout="no credentials stored\n")

    ok, detail = check_credentials(Path("gotohp"))

    assert ok is False
    assert "creds add" in detail


def test_check_credentials_reports_a_failing_binary(recorded_run) -> None:
    recorded_run(stdout="", stderr="config unreadable", returncode=2)

    ok, detail = check_credentials(Path("gotohp"))

    assert ok is False
    assert "config unreadable" in detail


def test_check_credentials_handles_a_missing_binary(tmp_path: Path) -> None:
    ok, detail = check_credentials(tmp_path / "not-here")

    assert ok is False
    assert "not found" in detail


# --- One real subprocess, to prove the boundary actually works -------------


def _write_launcher(tmp_path: Path, python_body: str) -> Path:
    """Write an executable stand-in for gotohp-cli for the current platform."""
    script = tmp_path / "fake_gotohp.py"
    script.write_text(python_body, encoding="utf-8")

    if sys.platform.startswith("win"):
        launcher = tmp_path / "gotohp-cli.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "gotohp-cli"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(0o755)
    return launcher


def test_real_subprocess_round_trip(tmp_path: Path, staged: Path) -> None:
    """Launch a stand-in binary for real, to prove argument passing, encoding and
    stdout capture work end to end on this platform."""
    body = (
        "import json, sys\n"
        "print('time=2026-01-01 level=INFO msg=\"starting upload\"', file=sys.stderr)\n"
        "print('a stray stdout log line')\n"
        "paths = [a for a in sys.argv[1:] if not a.startswith('-')]\n"
        "print(json.dumps({\n"
        "    'total': 1, 'succeeded': 1, 'failed': 0, 'skipped': 0,\n"
        "    'results': [{'path': 'IMG_1.HEIC', 'success': True, 'mediaKey': 'real-key'}],\n"
        "}, indent=2))\n"
    )
    launcher = _write_launcher(tmp_path, body)

    report = upload_directory(staged, binary=launcher, threads=1, pair_live_photos=True)

    assert report.succeeded == 1
    assert report.uploaded_filenames == {"IMG_1.HEIC"}
    assert report.verdicts[0].media_key == "real-key"


def test_real_subprocess_without_a_summary_raises(tmp_path: Path) -> None:
    staging = tmp_path / "media"
    staging.mkdir()
    (staging / "IMG_1.HEIC").write_bytes(b"data")
    body = "import sys\nprint('fatal: no credentials', file=sys.stderr)\nsys.exit(1)\n"
    launcher = _write_launcher(tmp_path, body)

    with pytest.raises(UploadError, match="no credentials"):
        upload_directory(staging, binary=launcher, threads=1, pair_live_photos=False)


# --- Capability detection ---------------------------------------------------
# Regression: the published v0.8.1 release has neither --no-tui nor
# --pair-live-photos. It ignores unknown flags instead of failing, and builds
# from main still report "v0.8.1", so the only reliable signal is the help text.
# Without --no-tui, bubbletea opens /dev/tty, which under systemd fails with
# ENXIO and loses the entire downloaded batch.

HELP_MODERN = """Usage: gotohp-cli upload <path> [<path> ...] [flags]

Flags:
  -r, --recursive              Include subdirectories
  -t, --threads <n>            Number of upload threads (default: 3)
      --pair-live-photos       Pair Apple Live Photo files
      --upload-incomplete-live-photos  Upload an unmatched member as a single file
      --ignore-apple-metadata  Match by filename stem
      --no-tui                 Disable the interactive progress UI
"""

HELP_V081 = """Usage: gotohp-cli upload <path> [<path> ...] [flags]

Flags:
  -r, --recursive              Include subdirectories
  -t, --threads <n>            Number of upload threads (default: 3)
  -f, --force                  Force upload even if file exists
  -a, --album <name>           Add to album
"""


def test_modern_binary_reports_no_missing_flags(recorded_run) -> None:
    recorded_run(stdout=HELP_MODERN)

    assert missing_upload_flags(Path("gotohp")) == []


def test_v081_release_is_detected_as_incompatible(recorded_run) -> None:
    recorded_run(stdout=HELP_V081)

    missing = missing_upload_flags(Path("gotohp"))

    assert "--no-tui" in missing
    assert "--pair-live-photos" in missing


def test_verify_compatible_passes_for_a_modern_binary(recorded_run) -> None:
    recorded_run(stdout=HELP_MODERN)

    verify_compatible(Path("gotohp"))  # must not raise


def test_verify_compatible_raises_with_actionable_guidance(recorded_run) -> None:
    recorded_run(stdout=HELP_V081)

    with pytest.raises(IncompatibleGotohp) as excinfo:
        verify_compatible(Path("gotohp"))

    message = str(excinfo.value)
    assert "--no-tui" in message
    assert "fetch_gotohp.py" in message


def test_help_on_stderr_is_also_read(recorded_run) -> None:
    """Some CLIs print usage to stderr; the probe must not depend on stdout."""
    recorded_run(stdout="", stderr=HELP_MODERN)

    assert missing_upload_flags(Path("gotohp")) == []


def test_unreadable_help_raises_rather_than_assuming_compatible(recorded_run) -> None:
    """Assuming success here would let an unusable binary through."""
    recorded_run(stdout="", stderr="segmentation fault", returncode=139)

    with pytest.raises(UploadError, match="Could not read"):
        missing_upload_flags(Path("gotohp"))


def test_missing_binary_raises(tmp_path: Path) -> None:
    with pytest.raises(UploadError, match="not found"):
        missing_upload_flags(tmp_path / "nope")


def test_real_v081_help_text_is_rejected(tmp_path: Path) -> None:
    """Guards the exact help text of the published release, captured verbatim
    from `gotohp-cli upload --help` at v0.8.1."""
    body = (
        "import sys\n"
        "print('''Usage: gotohp-cli upload <path> [<path> ...] [flags]\n"
        "\n"
        "Flags:\n"
        "  -r, --recursive              Include subdirectories\n"
        "  -t, --threads <n>            Number of upload threads (default: 3)\n"
        "  -f, --force                  Force upload even if file exists\n"
        "  -d, --delete                 Delete local files after upload\n"
        "  -df, --disable-filter        Allow unsupported file types\n"
        "      --date-from-filename     Parse date from filename\n"
        "  -e, --exclude <pattern>      Exclude directories\n"
        "  -l, --log-level <level>      Log level\n"
        "  -c, --config <path>          Config path\n"
        "  -a, --album <name>           Add to album''')\n"
    )
    launcher = _write_launcher(tmp_path, body)

    with pytest.raises(IncompatibleGotohp, match="--no-tui"):
        verify_compatible(launcher)
