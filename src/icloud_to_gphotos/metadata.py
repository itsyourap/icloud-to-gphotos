"""Metadata preservation for downloaded media.

iCloud originals normally carry complete EXIF, and we upload the bytes untouched,
so most metadata survives for free. The gap is assets whose embedded metadata is
incomplete — screenshots, imported media, and many videos. Google Photos dates
those by upload time, which silently scrambles a migrated timeline.

We therefore *backfill only what is missing*, using authoritative values from
iCloud's CloudKit record (``assetDate`` + ``timeZoneOffset`` for capture time,
``locationEnc`` for GPS). Existing tags are never overwritten.

exiftool is used when available because it is the only practical way to write
HEIC and QuickTime metadata. Without it we fall back to pyicloud's JPEG-only
helper and log the reduced coverage.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .assets import PlannedAsset

LOGGER = logging.getLogger(__name__)

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".dng",
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".orf", ".pef", ".raf", ".rw2", ".arw",
}
VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v"}

#: Tags we read to decide whether anything needs backfilling.
_PROBE_TAGS = (
    "-EXIF:DateTimeOriginal",
    "-EXIF:CreateDate",
    "-XMP:DateCreated",
    "-QuickTime:CreateDate",
    "-Keys:CreationDate",
    "-GPSLatitude",
    "-GPSLongitude",
    "-QuickTime:GPSCoordinates",
    "-Keys:GPSCoordinates",
    "-UserData:GPSCoordinates",
)

_ZERO_DATES = {"", "0000:00:00 00:00:00", "    :  :     :  :  "}


@dataclass(slots=True)
class MetadataReport:
    """What the backfill pass did, for the run report."""

    files_examined: int = 0
    dates_written: int = 0
    gps_written: int = 0
    mtimes_set: int = 0
    errors: list[str] = field(default_factory=list)
    exiftool_available: bool = True

    def as_dict(self) -> dict[str, object]:
        """Serialise for the JSON run report."""
        return {
            "files_examined": self.files_examined,
            "dates_written": self.dates_written,
            "gps_written": self.gps_written,
            "mtimes_set": self.mtimes_set,
            "exiftool_available": self.exiftool_available,
            "errors": self.errors[:50],
        }


def find_exiftool(explicit: Path | None = None) -> Path | None:
    """Locate the exiftool binary, or return None if it is not installed."""
    if explicit is not None:
        return explicit if explicit.exists() else None
    found = shutil.which("exiftool")
    return Path(found) if found else None


def _exif_datetime(value: datetime) -> str:
    return value.strftime("%Y:%m:%d %H:%M:%S")


def _exif_offset(value: datetime) -> str:
    offset = value.strftime("%z")  # e.g. +0530
    return f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"


def _has_date(tags: dict[str, object], keys: tuple[str, ...]) -> bool:
    for key in keys:
        raw = tags.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text not in _ZERO_DATES and not text.startswith("0000"):
            return True
    return False


def _has_gps(tags: dict[str, object]) -> bool:
    if tags.get("GPSLatitude") is not None and tags.get("GPSLongitude") is not None:
        return True
    return any(
        tags.get(key)
        for key in ("GPSCoordinates", "QuickTime:GPSCoordinates", "Keys:GPSCoordinates")
    )


def _run_exiftool(
    binary: Path, args: list[str], *, timeout: int = 900
) -> subprocess.CompletedProcess:
    """Run exiftool with arguments supplied via a UTF-8 argfile."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".args", delete=False, encoding="utf-8", newline="\n"
    ) as handle:
        handle.write("\n".join(args))
        handle.write("\n")
        argfile = handle.name
    try:
        return subprocess.run(
            [str(binary), "-charset", "filename=UTF8", "-@", argfile],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    finally:
        Path(argfile).unlink(missing_ok=True)


def probe_files(binary: Path, paths: list[Path]) -> dict[str, dict[str, object]]:
    """Read the date and GPS tags of many files in a single exiftool process."""
    if not paths:
        return {}
    args = ["-j", "-n", "-G0:1", *_PROBE_TAGS, *(str(p) for p in paths)]
    result = _run_exiftool(binary, args)
    if not result.stdout.strip():
        if result.returncode != 0:
            LOGGER.warning(
                "exiftool probe failed (rc=%s): %s",
                result.returncode,
                result.stderr.strip(),
            )
        return {}
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        LOGGER.warning("Could not parse exiftool probe output: %s", result.stdout[:500])
        return {}

    out: dict[str, dict[str, object]] = {}
    for entry in entries:
        source = entry.pop("SourceFile", None)
        if source is None:
            continue
        # -G0:1 prefixes keys with "Group0:Group1"; index by both the qualified
        # and bare tag name so lookups stay simple.
        flat: dict[str, object] = {}
        for key, value in entry.items():
            flat[key] = value
            bare = key.split(":")[-1]
            flat.setdefault(bare, value)
            if ":" in key:
                # Also expose "Group1:Tag" (e.g. "Keys:CreationDate").
                parts = key.split(":")
                if len(parts) >= 3:
                    flat.setdefault(f"{parts[1]}:{parts[-1]}", value)
        out[str(Path(source))] = flat
    return out


def _date_args(path: Path, when: datetime) -> list[str]:
    """Return exiftool args writing capture time appropriate to the file type."""
    suffix = path.suffix.lower()
    stamp = _exif_datetime(when)
    offset = _exif_offset(when)

    if suffix in VIDEO_SUFFIXES:
        # QuickTime date atoms are defined as UTC by the spec, while
        # Keys:CreationDate is the offset-bearing tag Apple writes and Google
        # Photos prefers. Write both so either reader lands on the same instant.
        utc_stamp = _exif_datetime(when.astimezone(UTC))
        return [
            f"-QuickTime:CreateDate={utc_stamp}",
            f"-QuickTime:ModifyDate={utc_stamp}",
            f"-QuickTime:TrackCreateDate={utc_stamp}",
            f"-QuickTime:TrackModifyDate={utc_stamp}",
            f"-QuickTime:MediaCreateDate={utc_stamp}",
            f"-QuickTime:MediaModifyDate={utc_stamp}",
            f"-Keys:CreationDate={stamp}{offset}",
        ]

    return [
        f"-EXIF:DateTimeOriginal={stamp}",
        f"-EXIF:CreateDate={stamp}",
        f"-EXIF:ModifyDate={stamp}",
        f"-EXIF:OffsetTimeOriginal={offset}",
        f"-EXIF:OffsetTimeDigitized={offset}",
        f"-EXIF:OffsetTime={offset}",
        f"-XMP:DateCreated={stamp}{offset}",
    ]


def iso6709(latitude: float, longitude: float, altitude: float | None = None) -> str:
    """Format a GPS fix as ISO 6709, the encoding QuickTime's ``©xyz`` atom uses.

    exiftool rejects space-separated coordinates for ``GPSCoordinates`` — it emits
    only a warning and writes nothing — so the exact format matters. ISO 6709 is
    accepted with and without exiftool's ``-n`` flag, and the leading sign is what
    carries the hemisphere.
    """
    text = f"{latitude:+011.6f}{longitude:+012.6f}"
    if altitude is not None:
        text += f"{altitude:+011.4f}"
    return f"{text}/"


def _gps_args(path: Path, location: dict[str, object]) -> list[str]:
    """Return exiftool args writing a GPS fix appropriate to the file type."""
    latitude = float(location["latitude"])  # type: ignore[arg-type]
    longitude = float(location["longitude"])  # type: ignore[arg-type]
    altitude = location.get("altitude")

    if path.suffix.lower() in VIDEO_SUFFIXES:
        coords = iso6709(
            latitude,
            longitude,
            float(altitude) if isinstance(altitude, (int, float)) else None,
        )
        return [
            f"-Keys:GPSCoordinates={coords}",
            f"-UserData:GPSCoordinates={coords}",
        ]

    args = [
        f"-EXIF:GPSLatitude={abs(latitude)}",
        f"-EXIF:GPSLatitudeRef={'N' if latitude >= 0 else 'S'}",
        f"-EXIF:GPSLongitude={abs(longitude)}",
        f"-EXIF:GPSLongitudeRef={'E' if longitude >= 0 else 'W'}",
    ]
    if isinstance(altitude, (int, float)):
        args += [
            f"-EXIF:GPSAltitude={abs(altitude)}",
            f"-EXIF:GPSAltitudeRef={'0' if altitude >= 0 else '1'}",
        ]
    return args


def _set_mtime(path: Path, when: datetime) -> bool:
    try:
        stamp = when.timestamp()
        os.utime(path, (stamp, stamp))
    except OSError as exc:
        LOGGER.debug("Could not set mtime on %s: %s", path, exc)
        return False
    return True


def backfill_batch(
    items: list[tuple[PlannedAsset, Path]],
    *,
    exiftool: Path | None,
    enabled: bool = True,
) -> MetadataReport:
    """Backfill missing capture dates and GPS across a batch of downloaded files.

    Args:
        items: ``(planned_asset, downloaded_path)`` pairs.
        exiftool: Path to exiftool, or None to use the reduced fallback.
        enabled: When False, only file modification times are corrected.
    """
    report = MetadataReport(files_examined=len(items), exiftool_available=exiftool is not None)

    for planned, path in items:
        if _set_mtime(path, planned.local_date):
            report.mtimes_set += 1

    if not enabled or not items:
        return report

    if exiftool is None:
        _fallback_backfill(items, report)
        return report

    existing = probe_files(exiftool, [path for _, path in items])
    write_blocks: list[list[str]] = []

    for planned, path in items:
        suffix = path.suffix.lower()
        if suffix not in IMAGE_SUFFIXES and suffix not in VIDEO_SUFFIXES:
            continue
        tags = existing.get(str(path), {})
        args: list[str] = []

        date_keys = (
            ("QuickTime:CreateDate", "Keys:CreationDate", "CreationDate", "CreateDate")
            if suffix in VIDEO_SUFFIXES
            else ("DateTimeOriginal", "CreateDate", "DateCreated")
        )
        if not _has_date(tags, date_keys):
            args += _date_args(path, planned.local_date)
            report.dates_written += 1

        if planned.location and not _has_gps(tags):
            args += _gps_args(path, planned.location)
            report.gps_written += 1

        if args:
            write_blocks.append([*args, "-overwrite_original", "-m", str(path), "-execute"])

    if not write_blocks:
        return report

    flat = [arg for block in write_blocks for arg in block]
    result = _run_exiftool(exiftool, flat)
    if result.returncode not in (0, 1):
        message = f"exiftool write pass returned {result.returncode}: {result.stderr.strip()[:500]}"
        LOGGER.warning(message)
        report.errors.append(message)
    elif result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            if line.lower().startswith(("error", "warning: ")):
                report.errors.append(line.strip())

    # exiftool rewrites the file, which resets mtime; restore it.
    for planned, path in items:
        _set_mtime(path, planned.local_date)

    return report


def _fallback_backfill(items: list[tuple[PlannedAsset, Path]], report: MetadataReport) -> None:
    """Backfill JPEG dates using pyicloud, the only option without exiftool."""
    from pyicloud.services.photos_cloudkit.materialize import set_exif_datetime_if_missing

    skipped: set[str] = set()
    for planned, path in items:
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            try:
                set_exif_datetime_if_missing(path, planned.local_date)
                report.dates_written += 1
            except Exception as exc:  # noqa: BLE001 - best effort without exiftool
                report.errors.append(f"{path.name}: {exc}")
        elif suffix in IMAGE_SUFFIXES or suffix in VIDEO_SUFFIXES:
            skipped.add(suffix)

    if skipped:
        message = (
            "exiftool not found: cannot verify or repair capture dates for "
            f"{', '.join(sorted(skipped))} files. Install exiftool so HEIC and video "
            "timestamps reach Google Photos correctly."
        )
        LOGGER.warning(message)
        report.errors.append(message)
