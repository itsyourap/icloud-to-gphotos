"""Tests for metadata backfill.

The rules under test: never overwrite metadata that already exists, always use
the camera's local wall-clock time for EXIF, and always use UTC for QuickTime
atoms. Getting the timezone handling wrong would shift every migrated photo.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from icloud_to_gphotos import metadata
from icloud_to_gphotos.assets import plan_asset
from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.metadata import (
    _date_args,
    _gps_args,
    _has_date,
    _has_gps,
    backfill_batch,
    find_exiftool,
    iso6709,
    probe_files,
)

from .conftest import FakePhotoAsset, encode_location

IST = timezone(timedelta(hours=5, minutes=30))
CAPTURED = datetime(2024, 7, 9, 18, 20, 27, tzinfo=IST)


def _planned(settings: Settings, **kwargs):
    asset = FakePhotoAsset("a1", **kwargs)
    return plan_asset(asset, settings, "IMG_0001")


# --- Tag presence detection ------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"DateTimeOriginal": "2024:07:09 18:20:27"}, True),
        ({"DateTimeOriginal": "0000:00:00 00:00:00"}, False),
        ({"DateTimeOriginal": ""}, False),
        ({"DateTimeOriginal": None}, False),
        ({}, False),
        ({"CreateDate": "2020:01:01 00:00:00"}, True),
    ],
)
def test_has_date(tags: dict, expected: bool) -> None:
    assert _has_date(tags, ("DateTimeOriginal", "CreateDate", "DateCreated")) is expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"GPSLatitude": 12.9, "GPSLongitude": 77.5}, True),
        ({"GPSLatitude": 0.0, "GPSLongitude": 0.0}, True),  # 0,0 is a real fix
        ({"GPSLatitude": 12.9}, False),
        ({"GPSCoordinates": "12.9 77.5"}, True),
        ({}, False),
    ],
)
def test_has_gps(tags: dict, expected: bool) -> None:
    assert _has_gps(tags) is expected


# --- Argument construction -------------------------------------------------


def test_image_date_args_use_local_wall_clock_time() -> None:
    args = _date_args(Path("IMG_0001.HEIC"), CAPTURED)

    assert "-EXIF:DateTimeOriginal=2024:07:09 18:20:27" in args
    assert "-EXIF:OffsetTimeOriginal=+05:30" in args
    assert "-XMP:DateCreated=2024:07:09 18:20:27+05:30" in args
    assert not any("QuickTime" in arg for arg in args)


def test_video_date_args_write_utc_atoms_and_offset_bearing_keys() -> None:
    """QuickTime atoms are UTC by spec; Keys:CreationDate carries the offset.
    18:20:27+05:30 is 12:50:27Z."""
    args = _date_args(Path("IMG_0001.MOV"), CAPTURED)

    assert "-QuickTime:CreateDate=2024:07:09 12:50:27" in args
    assert "-QuickTime:MediaCreateDate=2024:07:09 12:50:27" in args
    assert "-Keys:CreationDate=2024:07:09 18:20:27+05:30" in args
    assert not any("EXIF" in arg for arg in args)


@pytest.mark.parametrize(
    ("latitude", "longitude", "lat_ref", "lon_ref"),
    [
        (12.9716, 77.5946, "N", "E"),
        (-33.8688, 151.2093, "S", "E"),
        (40.7128, -74.0060, "N", "W"),
        (-22.9068, -43.1729, "S", "W"),
    ],
)
def test_image_gps_args_set_hemisphere_refs(
    latitude: float, longitude: float, lat_ref: str, lon_ref: str
) -> None:
    args = _gps_args(Path("IMG_0001.HEIC"), {"latitude": latitude, "longitude": longitude})

    assert f"-EXIF:GPSLatitude={abs(latitude)}" in args
    assert f"-EXIF:GPSLatitudeRef={lat_ref}" in args
    assert f"-EXIF:GPSLongitude={abs(longitude)}" in args
    assert f"-EXIF:GPSLongitudeRef={lon_ref}" in args


def test_image_gps_args_encode_altitude_below_sea_level() -> None:
    args = _gps_args(
        Path("IMG_0001.HEIC"), {"latitude": 1.0, "longitude": 2.0, "altitude": -120.5}
    )

    assert "-EXIF:GPSAltitude=120.5" in args
    assert "-EXIF:GPSAltitudeRef=1" in args


def test_video_gps_args_use_iso6709() -> None:
    """Space-separated coordinates are rejected by exiftool with only a warning,
    so the format has to be ISO 6709 or GPS is silently lost."""
    args = _gps_args(
        Path("IMG_0001.MOV"), {"latitude": 12.9, "longitude": 77.5, "altitude": 920.0}
    )

    expected = "+012.900000+0077.500000+00920.0000/"
    assert f"-Keys:GPSCoordinates={expected}" in args
    assert f"-UserData:GPSCoordinates={expected}" in args


def test_video_gps_args_omit_altitude_when_unknown() -> None:
    args = _gps_args(Path("IMG_0001.MOV"), {"latitude": 12.9, "longitude": 77.5})

    assert "-Keys:GPSCoordinates=+012.900000+0077.500000/" in args


@pytest.mark.parametrize(
    ("latitude", "longitude", "altitude", "expected"),
    [
        (12.9716, 77.5946, 920.0, "+012.971600+0077.594600+00920.0000/"),
        (-33.8688, -70.6693, None, "-033.868800-0070.669300/"),
        (40.7128, -74.0060, 12.5, "+040.712800-0074.006000+00012.5000/"),
        (-22.9068, 43.1729, None, "-022.906800+0043.172900/"),
        (0.0, 0.0, None, "+000.000000+0000.000000/"),
        (-0.5, -0.5, -10.0, "-000.500000-0000.500000-00010.0000/"),
    ],
)
def test_iso6709_encodes_sign_and_field_widths(
    latitude: float, longitude: float, altitude: float | None, expected: str
) -> None:
    """Latitude is 3 integer digits, longitude 4, altitude 5, each sign-prefixed.
    The leading sign is what carries the hemisphere."""
    assert iso6709(latitude, longitude, altitude) == expected


# --- Batch behaviour with a stubbed exiftool -------------------------------


class _FakeExiftool:
    """Captures exiftool invocations so we can assert on the arguments."""

    def __init__(self, probe_result: list[dict] | None = None, returncode: int = 0) -> None:
        self.probe_result = probe_result if probe_result is not None else []
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, binary: Path, args: list[str], *, timeout: int = 900):  # noqa: ARG002
        self.calls.append(args)
        import subprocess

        stdout = json.dumps(self.probe_result) if "-j" in args else ""
        return subprocess.CompletedProcess(
            args=["exiftool"], returncode=self.returncode, stdout=stdout, stderr=""
        )

    @property
    def write_calls(self) -> list[list[str]]:
        """Invocations that wrote tags rather than reading them."""
        return [c for c in self.calls if "-overwrite_original" in c]


@pytest.fixture
def fake_exiftool(monkeypatch: pytest.MonkeyPatch):
    """Replace the exiftool subprocess with a recording stub."""

    def install(probe_result: list[dict] | None = None, returncode: int = 0) -> _FakeExiftool:
        stub = _FakeExiftool(probe_result, returncode)
        monkeypatch.setattr(metadata, "_run_exiftool", stub)
        return stub

    return install


def test_backfill_writes_date_when_missing(
    settings: Settings, tmp_path: Path, fake_exiftool
) -> None:
    media = tmp_path / "IMG_0001.HEIC"
    media.write_bytes(b"fake")
    planned = _planned(settings, asset_date=CAPTURED.astimezone(UTC))
    stub = fake_exiftool([{"SourceFile": str(media)}])  # no tags present

    report = backfill_batch([(planned, media)], exiftool=Path("exiftool"))

    assert report.dates_written == 1
    assert len(stub.write_calls) == 1
    assert "-EXIF:DateTimeOriginal=2024:07:09 18:20:27" in stub.write_calls[0]


def test_backfill_leaves_existing_date_alone(
    settings: Settings, tmp_path: Path, fake_exiftool
) -> None:
    """Never overwrite metadata the camera already wrote."""
    media = tmp_path / "IMG_0001.HEIC"
    media.write_bytes(b"fake")
    planned = _planned(settings, asset_date=CAPTURED.astimezone(UTC))
    stub = fake_exiftool(
        [{"SourceFile": str(media), "EXIF:ExifIFD:DateTimeOriginal": "2019:01:01 09:00:00"}]
    )

    report = backfill_batch([(planned, media)], exiftool=Path("exiftool"))

    assert report.dates_written == 0
    assert stub.write_calls == []


def test_backfill_writes_gps_only_when_icloud_has_a_fix(
    settings: Settings, tmp_path: Path, fake_exiftool
) -> None:
    media = tmp_path / "IMG_0001.HEIC"
    media.write_bytes(b"fake")
    planned = _planned(settings, location=encode_location(12.9716, 77.5946))
    stub = fake_exiftool(
        [{"SourceFile": str(media), "EXIF:ExifIFD:DateTimeOriginal": "2019:01:01 09:00:00"}]
    )

    report = backfill_batch([(planned, media)], exiftool=Path("exiftool"))

    assert report.gps_written == 1
    assert "-EXIF:GPSLatitude=12.9716" in stub.write_calls[0]


def test_backfill_skips_gps_when_file_already_geotagged(
    settings: Settings, tmp_path: Path, fake_exiftool
) -> None:
    media = tmp_path / "IMG_0001.HEIC"
    media.write_bytes(b"fake")
    planned = _planned(settings, location=encode_location(1.0, 2.0))
    stub = fake_exiftool(
        [
            {
                "SourceFile": str(media),
                "EXIF:ExifIFD:DateTimeOriginal": "2019:01:01 09:00:00",
                "EXIF:GPS:GPSLatitude": 9.9,
                "EXIF:GPS:GPSLongitude": 8.8,
            }
        ]
    )

    report = backfill_batch([(planned, media)], exiftool=Path("exiftool"))

    assert report.gps_written == 0
    assert stub.write_calls == []


def test_backfill_sets_file_mtime_to_capture_time(settings: Settings, tmp_path: Path) -> None:
    media = tmp_path / "IMG_0001.HEIC"
    media.write_bytes(b"fake")
    planned = _planned(settings, asset_date=CAPTURED.astimezone(UTC))

    report = backfill_batch([(planned, media)], exiftool=None, enabled=False)

    assert report.mtimes_set == 1
    assert media.stat().st_mtime == pytest.approx(CAPTURED.timestamp(), abs=2)


def test_backfill_reports_reduced_coverage_without_exiftool(
    settings: Settings, tmp_path: Path
) -> None:
    """A silent fallback would let HEIC and video dates rot; the report has to
    say so."""
    heic = tmp_path / "IMG_0001.HEIC"
    heic.write_bytes(b"fake")
    movie = tmp_path / "IMG_0002.MOV"
    movie.write_bytes(b"fake")
    planned = _planned(settings)

    report = backfill_batch(
        [(planned, heic), (planned, movie)], exiftool=None, enabled=True
    )

    assert report.exiftool_available is False
    assert any("exiftool not found" in err for err in report.errors)
    assert any(".heic" in err for err in report.errors)


def test_backfill_ignores_unknown_file_types(
    settings: Settings, tmp_path: Path, fake_exiftool
) -> None:
    sidecar = tmp_path / "notes.txt"
    sidecar.write_text("hello")
    planned = _planned(settings)
    stub = fake_exiftool([{"SourceFile": str(sidecar)}])

    report = backfill_batch([(planned, sidecar)], exiftool=Path("exiftool"))

    assert stub.write_calls == []
    assert report.dates_written == 0


def test_backfill_records_exiftool_failures(
    settings: Settings, tmp_path: Path, fake_exiftool
) -> None:
    media = tmp_path / "IMG_0001.HEIC"
    media.write_bytes(b"fake")
    planned = _planned(settings)
    fake_exiftool([{"SourceFile": str(media)}], returncode=2)

    report = backfill_batch([(planned, media)], exiftool=Path("exiftool"))

    assert any("returned 2" in err for err in report.errors)


def test_backfill_handles_empty_batch(settings: Settings) -> None:
    report = backfill_batch([], exiftool=Path("exiftool"))

    assert report.files_examined == 0


def test_probe_files_indexes_qualified_and_bare_tag_names(
    tmp_path: Path, fake_exiftool
) -> None:
    media = tmp_path / "IMG_0001.MOV"
    fake_exiftool(
        [{"SourceFile": str(media), "QuickTime:Keys:CreationDate": "2024:07:09 18:20:27+05:30"}]
    )

    tags = probe_files(Path("exiftool"), [media])

    entry = tags[str(media)]
    assert entry["QuickTime:Keys:CreationDate"] == "2024:07:09 18:20:27+05:30"
    assert entry["CreationDate"] == "2024:07:09 18:20:27+05:30"
    assert entry["Keys:CreationDate"] == "2024:07:09 18:20:27+05:30"


def test_probe_files_returns_empty_for_no_paths() -> None:
    assert probe_files(Path("exiftool"), []) == {}


def test_find_exiftool_honours_an_explicit_path(tmp_path: Path) -> None:
    present = tmp_path / "exiftool"
    present.write_text("#!/bin/sh")

    assert find_exiftool(present) == present
    assert find_exiftool(tmp_path / "missing") is None
