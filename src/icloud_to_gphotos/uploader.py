"""Uploading staged media to Google Photos via the gotohp CLI.

``gotohp-cli upload --no-tui`` prints a JSON summary on stdout with a per-file
verdict. That summary is the only evidence we accept before deleting anything
from iCloud, so parsing it correctly is the safety-critical part of this module.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: gotohp skip codes that mean "Google Photos already has this content".
#: Treated as success: the goal is for the media to exist remotely, not for this
#: particular run to have been the one that put it there.
SUCCESSFUL_SKIP_CODES = frozenset(
    {"remote-duplicate", "remote-live-photo-component-exists"}
)

#: Flags this project depends on that do not exist in the v0.8.1 release.
#:
#: gotohp ignores unknown flags rather than rejecting them, and `version` still
#: reports "v0.8.1" on builds from main, so neither the exit code nor the
#: version string can tell an adequate binary from an inadequate one. Probing
#: `upload --help` is the only reliable signal.
#:
#: Without --no-tui, bubbletea opens /dev/tty for input. A systemd service has
#: no controlling terminal, so that fails with ENXIO and the whole batch is
#: lost after being downloaded:
#:     error running TUI: could not open a new TTY:
#:     open /dev/tty: no such device or address
REQUIRED_UPLOAD_FLAGS: tuple[str, ...] = (
    "--no-tui",
    "--pair-live-photos",
    "--upload-incomplete-live-photos",
)


class UploadError(RuntimeError):
    """gotohp could not be run, or produced no parseable summary."""


class IncompatibleGotohp(UploadError):
    """The gotohp binary is too old to be driven headlessly."""


@dataclass(slots=True)
class FileVerdict:
    """gotohp's verdict for a single staged file."""

    filename: str
    uploaded: bool
    media_key: str | None
    skip_code: str | None
    reason: str | None


@dataclass(slots=True)
class UploadReport:
    """Aggregated result of one gotohp invocation."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    verdicts: list[FileVerdict] = field(default_factory=list)
    warnings: list[dict[str, object]] = field(default_factory=list)

    def merge(self, other: UploadReport) -> None:
        """Fold another report into this one."""
        self.total += other.total
        self.succeeded += other.succeeded
        self.failed += other.failed
        self.skipped += other.skipped
        self.verdicts.extend(other.verdicts)
        self.warnings.extend(other.warnings)

    @property
    def uploaded_filenames(self) -> set[str]:
        """Basenames Google Photos has confirmed."""
        return {v.filename for v in self.verdicts if v.uploaded}

    def as_dict(self) -> dict[str, object]:
        """Serialise for the JSON run report."""
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "confirmed": len(self.uploaded_filenames),
            "warnings": self.warnings[:50],
            "failures": [
                {"file": v.filename, "reason": v.reason, "skip_code": v.skip_code}
                for v in self.verdicts
                if not v.uploaded
            ][:100],
        }


def extract_json_object(text: str) -> dict[str, object] | None:
    """Extract the last top-level JSON object from mixed CLI output.

    gotohp prints its summary last, but log lines or a stray TUI frame can share
    stdout, so we scan for the final balanced ``{...}`` rather than trusting that
    stdout is pure JSON.
    """
    decoder = json.JSONDecoder()
    found: dict[str, object] | None = None
    best_span = -1
    start = text.find("{")
    while start != -1:
        try:
            parsed, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pass
        else:
            # Objects nested inside the summary also parse, so prefer the widest
            # span; that is necessarily the outermost object.
            if isinstance(parsed, dict) and (end - start) >= best_span:
                found = parsed
                best_span = end - start
        start = text.find("{", start + 1)
    return found


def _as_int(value: object) -> int:
    """Coerce an untrusted JSON value to an int, defaulting to 0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def parse_summary(payload: dict[str, object]) -> UploadReport:
    """Convert gotohp's JSON summary into an :class:`UploadReport`."""
    report = UploadReport(
        total=_as_int(payload.get("total")),
        succeeded=_as_int(payload.get("succeeded")),
        failed=_as_int(payload.get("failed")),
        skipped=_as_int(payload.get("skipped")),
    )

    raw_warnings = payload.get("warnings")
    if isinstance(raw_warnings, list):
        report.warnings = [w for w in raw_warnings if isinstance(w, dict)]

    results = payload.get("results")
    if not isinstance(results, list):
        return report

    for entry in results:
        if not isinstance(entry, dict):
            continue
        success = bool(entry.get("success"))
        skipped = bool(entry.get("skipped"))
        skip_code = entry.get("skipCode")
        skip_code = str(skip_code) if skip_code else None
        media_key = entry.get("mediaKey")
        media_key = str(media_key) if media_key else None

        uploaded = success or (skipped and skip_code in SUCCESSFUL_SKIP_CODES)
        reason = entry.get("error") or entry.get("skipReason")

        # A Live Photo is reported once but covers two staged files; credit both.
        raw_paths = entry.get("paths")
        paths: list[str] = []
        if isinstance(raw_paths, list):
            paths = [str(p) for p in raw_paths if p]
        if not paths and entry.get("path"):
            paths = [str(entry["path"])]

        for path in paths:
            report.verdicts.append(
                FileVerdict(
                    filename=Path(path).name,
                    uploaded=uploaded,
                    media_key=media_key,
                    skip_code=skip_code,
                    reason=str(reason) if reason else None,
                )
            )

    return report


def _has_files(directory: Path) -> bool:
    return any(p.is_file() and not p.name.startswith(".") for p in directory.rglob("*"))


def upload_directory(
    directory: Path,
    *,
    binary: Path,
    threads: int,
    pair_live_photos: bool,
    ignore_apple_metadata: bool = False,
    config_path: Path | None = None,
    log_level: str = "info",
    timeout: int = 6 * 60 * 60,
) -> UploadReport:
    """Upload everything under ``directory`` and return the parsed verdicts.

    Raises:
        UploadError: if gotohp cannot be run or emits no usable summary.
    """
    if not directory.exists() or not _has_files(directory):
        LOGGER.debug("Nothing staged in %s; skipping upload.", directory)
        return UploadReport()

    command: list[str] = [
        str(binary),
        "upload",
        str(directory),
        "--recursive",
        "--no-tui",
        "--threads",
        str(threads),
        "--log-level",
        log_level,
    ]
    if pair_live_photos:
        command.append("--pair-live-photos")
        # Without this, an unpaired component is dropped silently. We would
        # rather upload it standalone than leave it stuck in iCloud forever.
        command.append("--upload-incomplete-live-photos")
        if ignore_apple_metadata:
            command.append("--ignore-apple-metadata")
    if config_path is not None:
        command += ["--config", str(config_path)]

    LOGGER.info("Uploading %s via gotohp (%d threads)", directory, threads)
    LOGGER.debug("gotohp command: %s", " ".join(command))

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UploadError(f"gotohp binary not found at {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UploadError(f"gotohp timed out after {timeout}s") from exc

    payload = extract_json_object(completed.stdout or "")
    if payload is None:
        detail = (completed.stderr or completed.stdout or "").strip()[:1000]
        raise UploadError(
            f"gotohp produced no JSON summary (exit code {completed.returncode}). Output: {detail}"
        )

    report = parse_summary(payload)
    if completed.returncode != 0:
        LOGGER.warning(
            "gotohp exited %s but produced a summary; treating per-file verdicts as authoritative.",
            completed.returncode,
        )
    LOGGER.info(
        "gotohp: %d succeeded, %d skipped, %d failed (%d files confirmed remote)",
        report.succeeded,
        report.skipped,
        report.failed,
        len(report.uploaded_filenames),
    )
    return report


def missing_upload_flags(binary: Path, timeout: int = 60) -> list[str]:
    """Return the required upload flags this binary does not support.

    Raises:
        UploadError: if the binary cannot be run or its help cannot be read.
    """
    try:
        completed = subprocess.run(
            [str(binary), "upload", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UploadError(f"gotohp binary not found at {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise UploadError(f"gotohp upload --help timed out after {timeout}s") from exc
    except OSError as exc:
        # Not executable, wrong architecture, or not a valid binary at all.
        raise UploadError(f"Could not execute {binary}: {exc}") from exc

    help_text = f"{completed.stdout}\n{completed.stderr}"
    if "upload" not in help_text.lower():
        raise UploadError(
            f"Could not read `gotohp upload --help` (exit {completed.returncode}): "
            f"{help_text.strip()[:300]}"
        )
    return [flag for flag in REQUIRED_UPLOAD_FLAGS if flag not in help_text]


def verify_compatible(binary: Path) -> None:
    """Raise :class:`IncompatibleGotohp` if the binary cannot be driven headlessly.

    Called before any download work, so an unusable binary costs nothing rather
    than being discovered after gigabytes have been fetched.
    """
    missing = missing_upload_flags(binary)
    if not missing:
        return
    raise IncompatibleGotohp(
        f"{binary} does not support {', '.join(missing)}. The published v0.8.1 "
        "release predates headless uploads and Live Photo pairing, and it "
        "ignores unknown flags instead of failing, so it cannot be used here. "
        "Rebuild it with `python scripts/fetch_gotohp.py` (see docs/SETUP.md)."
    )


def check_credentials(binary: Path, config_path: Path | None = None) -> tuple[bool, str]:
    """Return whether gotohp has usable Google Photos credentials configured."""
    command = [str(binary), "creds", "list"]
    if config_path is not None:
        command += ["--config", str(config_path)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return False, f"gotohp binary not found at {binary}"
    except subprocess.TimeoutExpired:
        return False, "gotohp creds list timed out"

    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0:
        return False, output[:500] or f"exit code {completed.returncode}"
    if "@" not in output:
        return False, f"no credentials stored. Run: {binary} creds add <auth-string>"
    return True, output[:500]
