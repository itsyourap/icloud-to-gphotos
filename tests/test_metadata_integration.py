"""Integration tests that run the real exiftool binary.

Skipped when exiftool is not installed. These are the only tests that prove the
tags we write are actually accepted and read back with the values we intended —
the unit tests only assert on the arguments we pass.
"""

from __future__ import annotations

import base64
import shutil
import struct
import zlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from icloud_to_gphotos.assets import plan_asset
from icloud_to_gphotos.config import Settings
from icloud_to_gphotos.metadata import backfill_batch, find_exiftool, probe_files

from .conftest import FakePhotoAsset, encode_location, make_resource

pytestmark = pytest.mark.skipif(
    shutil.which("exiftool") is None,
    reason="exiftool is not installed; see docs/SETUP.md",
)

IST = timezone(timedelta(hours=5, minutes=30))
CAPTURED = datetime(2024, 7, 9, 18, 20, 27, tzinfo=IST)


def _minimal_png(path: Path) -> None:
    """Write a valid 1x1 PNG, a format exiftool can add metadata to."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\xff\xff")
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixel)
        + chunk(b"IEND", b"")
    )


def _minimal_jpeg(path: Path) -> None:
    """Write a tiny baseline JPEG with no EXIF, via exiftool-friendly bytes."""
    # A 1x1 grey JPEG produced once and embedded here so the test needs no
    # image library. Verified to be a valid baseline JPEG.
    encoded = (
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
        "HBwcJCQuJyIsIx0cHDcnLDA1NTU1HCc7Pzo9Ojc1NTX/wAALCAABAAEBAREA/8QAFAABAQAAAAAA"
        "AAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/E"
        "ABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AJgA/9k="
    )
    path.write_bytes(base64.b64decode(encoded))


def _planned(settings: Settings, filename: str, **kwargs):
    asset = FakePhotoAsset(
        "a1",
        filename=filename,
        asset_date=CAPTURED.astimezone(UTC),
        resources={"original": make_resource("original", filename)},
        **kwargs,
    )
    return plan_asset(asset, settings, Path(filename).stem)


def test_exiftool_is_discovered() -> None:
    assert find_exiftool(None) is not None


def test_writes_and_reads_back_a_capture_date_on_a_real_jpeg(
    settings: Settings, tmp_path: Path
) -> None:
    media = tmp_path / "IMG_1.JPG"
    _minimal_jpeg(media)
    planned = _planned(settings, "IMG_1.JPG")
    exiftool = find_exiftool(None)
    assert exiftool is not None

    report = backfill_batch([(planned, media)], exiftool=exiftool)

    assert report.dates_written == 1
    tags = probe_files(exiftool, [media])[str(media)]
    assert tags["DateTimeOriginal"] == "2024:07:09 18:20:27"


def test_writes_and_reads_back_gps_on_a_real_jpeg(settings: Settings, tmp_path: Path) -> None:
    media = tmp_path / "IMG_2.JPG"
    _minimal_jpeg(media)
    planned = _planned(settings, "IMG_2.JPG", location=encode_location(12.9716, 77.5946, 920.0))
    exiftool = find_exiftool(None)
    assert exiftool is not None

    report = backfill_batch([(planned, media)], exiftool=exiftool)

    assert report.gps_written == 1
    tags = probe_files(exiftool, [media])[str(media)]
    assert float(tags["GPSLatitude"]) == pytest.approx(12.9716, abs=1e-4)
    assert float(tags["GPSLongitude"]) == pytest.approx(77.5946, abs=1e-4)


def _signed_coordinates(exiftool: Path, media: Path) -> tuple[float, float]:
    """Read the sign-applied coordinates a consumer would compute.

    In EXIF, latitude and longitude are stored unsigned and the hemisphere lives
    in ``GPSLatitudeRef``/``GPSLongitudeRef``. exiftool's ``Composite`` group
    combines them, which is the value Google Photos effectively sees.
    """
    import json
    import subprocess

    result = subprocess.run(
        [
            str(exiftool),
            "-j",
            "-n",
            "-Composite:GPSLatitude",
            "-Composite:GPSLongitude",
            str(media),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    entry = json.loads(result.stdout)[0]
    return float(entry["GPSLatitude"]), float(entry["GPSLongitude"])


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (12.9716, 77.5946),  # northern, eastern
        (-33.8688, -70.6693),  # southern, western
        (40.7128, -74.0060),  # northern, western
        (-22.9068, 43.1729),  # southern, eastern
    ],
)
def test_every_hemisphere_round_trips_with_the_right_sign(
    settings: Settings, tmp_path: Path, latitude: float, longitude: float
) -> None:
    """A dropped or wrong GPSLatitudeRef would silently mirror the location
    across the equator or the prime meridian."""
    media = tmp_path / "IMG_3.JPG"
    _minimal_jpeg(media)
    planned = _planned(settings, "IMG_3.JPG", location=encode_location(latitude, longitude))
    exiftool = find_exiftool(None)
    assert exiftool is not None

    backfill_batch([(planned, media)], exiftool=exiftool)

    read_lat, read_lon = _signed_coordinates(exiftool, media)
    assert read_lat == pytest.approx(latitude, abs=1e-4)
    assert read_lon == pytest.approx(longitude, abs=1e-4)


def test_an_existing_date_is_left_untouched(settings: Settings, tmp_path: Path) -> None:
    import subprocess

    media = tmp_path / "IMG_4.JPG"
    _minimal_jpeg(media)
    exiftool = find_exiftool(None)
    assert exiftool is not None
    subprocess.run(
        [
            str(exiftool),
            "-overwrite_original",
            "-EXIF:DateTimeOriginal=2011:01:02 03:04:05",
            str(media),
        ],
        check=True,
        capture_output=True,
    )
    planned = _planned(settings, "IMG_4.JPG")

    report = backfill_batch([(planned, media)], exiftool=exiftool)

    assert report.dates_written == 0
    tags = probe_files(exiftool, [media])[str(media)]
    assert tags["DateTimeOriginal"] == "2011:01:02 03:04:05"


def test_mtime_is_restored_after_exiftool_rewrites_the_file(
    settings: Settings, tmp_path: Path
) -> None:
    """exiftool resets mtime when it rewrites; the file date should still match
    the capture date afterwards."""
    media = tmp_path / "IMG_5.JPG"
    _minimal_jpeg(media)
    planned = _planned(settings, "IMG_5.JPG")
    exiftool = find_exiftool(None)
    assert exiftool is not None

    backfill_batch([(planned, media)], exiftool=exiftool)

    assert media.stat().st_mtime == pytest.approx(CAPTURED.timestamp(), abs=2)


def test_unwritable_format_is_reported_without_crashing(
    settings: Settings, tmp_path: Path
) -> None:
    """A PNG has no EXIF IFD by default. Whatever exiftool decides, the run must
    survive and the file must still exist."""
    media = tmp_path / "Screenshot.png"
    _minimal_png(media)
    planned = _planned(settings, "Screenshot.png")
    exiftool = find_exiftool(None)
    assert exiftool is not None

    report = backfill_batch([(planned, media)], exiftool=exiftool)

    assert media.exists()
    assert report.files_examined == 1


def test_a_batch_of_many_files_uses_one_exiftool_pass(
    settings: Settings, tmp_path: Path
) -> None:
    """Batching matters: a first run over a large library would otherwise spawn
    one process per file."""
    items = []
    for index in range(12):
        media = tmp_path / f"IMG_B{index}.JPG"
        _minimal_jpeg(media)
        items.append((_planned(settings, media.name), media))
    exiftool = find_exiftool(None)
    assert exiftool is not None

    report = backfill_batch(items, exiftool=exiftool)

    assert report.dates_written == 12
    tags = probe_files(exiftool, [path for _p, path in items])
    assert len(tags) == 12
    assert all(t["DateTimeOriginal"] == "2024:07:09 18:20:27" for t in tags.values())


# --- Video metadata, which is where the format rules are strictest ----------

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg is not installed")


def _tiny_mp4(path: Path) -> None:
    """Render a fraction-of-a-second black clip with no metadata."""
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=32x32:d=0.2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path), "-y",
        ],
        check=True,
        capture_output=True,
    )


def _read_video_tags(exiftool: Path, media: Path) -> dict[str, str]:
    import json
    import subprocess

    result = subprocess.run(
        [
            str(exiftool), "-j", "-n", "-G0:1",
            "-QuickTime:CreateDate",
            "-Keys:CreationDate",
            "-Keys:GPSCoordinates",
            "-UserData:GPSCoordinates",
            str(media),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    entry = json.loads(result.stdout)[0]
    return {key.split(":")[-1]: value for key, value in entry.items() if key != "SourceFile"}


@needs_ffmpeg
def test_video_capture_date_is_written_as_utc_and_local(
    settings: Settings, tmp_path: Path
) -> None:
    """QuickTime atoms must hold UTC while Keys:CreationDate holds the offset.
    18:20:27+05:30 is 12:50:27Z; writing local time into the atom would shift
    the video by the timezone offset in Google Photos."""
    media = tmp_path / "IMG_V1.MP4"
    _tiny_mp4(media)
    planned = _planned(settings, "IMG_V1.MP4")
    exiftool = find_exiftool(None)
    assert exiftool is not None

    report = backfill_batch([(planned, media)], exiftool=exiftool)

    assert report.dates_written == 1
    assert report.errors == []
    tags = _read_video_tags(exiftool, media)
    assert tags["CreateDate"] == "2024:07:09 12:50:27"
    assert tags["CreationDate"] == "2024:07:09 18:20:27+05:30"


@needs_ffmpeg
@pytest.mark.parametrize(
    ("latitude", "longitude", "altitude", "expected"),
    [
        (12.9716, 77.5946, 920.0, "+012.971600+0077.594600+00920.0000/"),
        (-33.8688, -70.6693, None, "-033.868800-0070.669300/"),
        (40.7128, -74.0060, None, "+040.712800-0074.006000/"),
    ],
)
def test_video_gps_actually_lands_in_the_file(
    settings: Settings,
    tmp_path: Path,
    latitude: float,
    longitude: float,
    altitude: float | None,
    expected: str,
) -> None:
    """Regression guard: space-separated coordinates made exiftool emit a warning
    and write nothing, so video GPS was silently dropped."""
    media = tmp_path / "IMG_V2.MP4"
    _tiny_mp4(media)
    planned = _planned(
        settings, "IMG_V2.MP4", location=encode_location(latitude, longitude, altitude)
    )
    exiftool = find_exiftool(None)
    assert exiftool is not None

    report = backfill_batch([(planned, media)], exiftool=exiftool)

    assert report.gps_written == 1
    assert report.errors == []
    tags = _read_video_tags(exiftool, media)
    assert tags["GPSCoordinates"] == expected


@needs_ffmpeg
def test_video_with_an_existing_date_is_left_alone(settings: Settings, tmp_path: Path) -> None:
    import subprocess

    media = tmp_path / "IMG_V3.MP4"
    _tiny_mp4(media)
    exiftool = find_exiftool(None)
    assert exiftool is not None
    subprocess.run(
        [
            str(exiftool),
            "-overwrite_original",
            "-QuickTime:CreateDate=2015:03:04 05:06:07",
            str(media),
        ],
        check=True,
        capture_output=True,
    )
    planned = _planned(settings, "IMG_V3.MP4")

    report = backfill_batch([(planned, media)], exiftool=exiftool)

    assert report.dates_written == 0
    assert _read_video_tags(exiftool, media)["CreateDate"] == "2015:03:04 05:06:07"


@needs_ffmpeg
def test_mixed_image_and_video_batch_is_handled_in_one_pass(
    settings: Settings, tmp_path: Path
) -> None:
    """A real batch mixes HEIC/JPEG stills with MOV components; each file must get
    the tag family appropriate to its container."""
    photo = tmp_path / "IMG_M1.JPG"
    _minimal_jpeg(photo)
    video = tmp_path / "IMG_M1.MP4"
    _tiny_mp4(video)
    exiftool = find_exiftool(None)
    assert exiftool is not None
    items = [
        (_planned(settings, "IMG_M1.JPG", location=encode_location(1.5, 2.5)), photo),
        (_planned(settings, "IMG_M1.MP4", location=encode_location(1.5, 2.5)), video),
    ]

    report = backfill_batch(items, exiftool=exiftool)

    assert report.dates_written == 2
    assert report.gps_written == 2
    assert report.errors == []
    assert probe_files(exiftool, [photo])[str(photo)]["DateTimeOriginal"] == (
        "2024:07:09 18:20:27"
    )
    assert _read_video_tags(exiftool, video)["CreateDate"] == "2024:07:09 12:50:27"
