"""Translating iCloud assets into a concrete download plan.

This module decides *which* bytes of an asset we move to Google Photos and
*what to call them*. Both choices matter more than they look:

* Live Photos are two files (still + MOV). gotohp re-pairs them into one Google
  Photos item, but only if both land in the same upload queue with a matching
  Apple content identifier or filename stem — hence the shared stem.
* An asset edited in iCloud has an untouched ``resOriginal`` and a rendered
  ``resJPEGFull``. pyicloud requests the ``resJPEGFull*`` fields but does not
  expose them as a version, so we build that resource here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Literal

from pyicloud.services.photos_cloudkit.mappers import build_photo_resource, record_field_value
from pyicloud.services.photos_cloudkit.models import PhotoResource
from pyicloud.services.photos_cloudkit.service import PhotoAsset

from .config import Settings

LOGGER = logging.getLogger(__name__)

#: CloudKit field prefix holding the rendered version of an edited photo.
EDITED_PREFIX = "resJPEGFull"

#: Staging subtree each resource belongs to. Edited renders are uploaded in a
#: separate gotohp pass without Live Photo pairing, because they share a stem
#: with the still they were derived from and would otherwise look like a pair.
StagingRoot = Literal["media", "edited"]

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass(slots=True)
class PlannedResource:
    """One file to download for an asset."""

    key: str
    resource: PhotoResource
    filename: str
    staging_root: StagingRoot

    @property
    def size(self) -> int | None:
        """Expected byte size, when iCloud reports one."""
        return self.resource.size

    @property
    def url(self) -> str | None:
        """CloudKit download URL."""
        return self.resource.url


@dataclass(slots=True)
class PlannedAsset:
    """An iCloud asset plus the set of files we intend to move."""

    asset: PhotoAsset
    asset_id: str
    master_id: str
    filename: str
    stem: str
    item_type: str
    asset_date: datetime
    added_date: datetime | None
    local_date: datetime
    location: dict[str, Any]
    is_live_photo: bool
    has_adjustments: bool
    is_favorite: bool
    resources: list[PlannedResource]

    @property
    def total_bytes(self) -> int:
        """Sum of the reported sizes of every planned resource."""
        return sum(res.size or 0 for res in self.resources)

    def age_days(self, now: datetime | None = None) -> int:
        """Whole days between the capture date and ``now``."""
        reference = now or datetime.now(UTC)
        return (reference - self.asset_date.astimezone(reference.tzinfo)).days


def sanitize_stem(value: str) -> str:
    """Reduce a filename stem to something safe on both Windows and Linux.

    Leading and trailing dots, spaces and dashes are stripped: dots and spaces
    are invalid at the end of a Windows filename, and a leading dash makes the
    path look like an option to the gotohp CLI.
    """
    stem = PurePosixPath(value.replace("\\", "/")).name
    stem = _UNSAFE_CHARS.sub("-", stem).strip(" .-")
    return stem or "photo"


def _field(asset: PhotoAsset, name: str) -> Any:
    """Read a CloudKit field from the asset record, falling back to the master."""
    for record in (asset._asset_record, asset._master_record):  # noqa: SLF001
        value = record_field_value(record, name)
        if value is not None:
            return value
    return None


def has_adjustments(asset: PhotoAsset) -> bool:
    """True when the asset carries iCloud edit data."""
    return _field(asset, "adjustmentType") is not None


def build_edited_resource(asset: PhotoAsset) -> PhotoResource | None:
    """Build a resource for the rendered version of an edited photo, if present."""
    return build_photo_resource(
        key="edited",
        prefix=EDITED_PREFIX,
        master_record=asset._master_record,  # noqa: SLF001
        filename=asset.filename,
        item_type_extensions=PhotoAsset.FILE_TYPE_EXTENSIONS,
        is_live_photo=False,
        item_type_lookup=PhotoAsset.ITEM_TYPES,
    )


def local_capture_time(asset: PhotoAsset) -> datetime:
    """Return capture time in the *camera's* timezone.

    EXIF stores wall-clock time with no zone, so writing a UTC timestamp would
    shift every photo. iCloud gives us ``timeZoneOffset`` alongside ``assetDate``,
    which lets us reconstruct what the clock actually read.
    """
    utc = asset.asset_date
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=UTC)
    offset = _field(asset, "timeZoneOffset")
    if isinstance(offset, (int, float)):
        return utc.astimezone(timezone(timedelta(seconds=int(offset))))
    return utc


def extract_location(asset: PhotoAsset) -> dict[str, Any]:
    """Return the asset's GPS fix as a plain dict, or an empty dict."""
    from pyicloud.services.photos_cloudkit.materialize import _extract_location

    try:
        location = _extract_location(asset._asset_record)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - optional metadata must never break a run
        LOGGER.debug("Could not decode location for %s", asset.id, exc_info=True)
        return {}
    if location.get("latitude") is None or location.get("longitude") is None:
        return {}
    return location


def _extension(resource: PhotoResource, fallback: str) -> str:
    suffix = PurePosixPath(resource.filename or "").suffix
    return suffix or fallback


def plan_asset(asset: PhotoAsset, settings: Settings, stem: str) -> PlannedAsset:
    """Decide which resources of ``asset`` to download and what to name them.

    ``stem`` is the caller-reserved, collision-free filename stem shared by every
    resource of this asset.
    """
    resources = asset.resources
    item_type = asset.item_type
    edited_render = build_edited_resource(asset)
    adjusted = has_adjustments(asset)
    # An adjustment record without a render is not actionable; treat as unedited
    # so we never drop the original in "edited"-only mode.
    edited_available = adjusted and edited_render is not None and bool(edited_render.url)

    planned: list[PlannedResource] = []

    want_original = not (settings.edited_policy == "edited" and edited_available)
    original = resources.get("original")
    if want_original and original is not None and original.url:
        planned.append(
            PlannedResource(
                key="original",
                resource=original,
                filename=f"{stem}{_extension(original, '.jpg')}",
                staging_root="media",
            )
        )

    if item_type != "movie":
        if (
            settings.include_live_photo_video
            and asset.is_live_photo
            and (live := resources.get("original_video")) is not None
            and live.url
        ):
            planned.append(
                PlannedResource(
                    key="original_video",
                    resource=live,
                    filename=f"{stem}{_extension(live, '.MOV')}",
                    staging_root="media",
                )
            )

        if (
            settings.include_alternative_original
            and (alt := resources.get("alternative")) is not None
            and alt.url
        ):
            planned.append(
                PlannedResource(
                    key="alternative",
                    resource=alt,
                    filename=f"{stem}_alt{_extension(alt, '.jpg')}",
                    staging_root="media",
                )
            )

        if edited_available and settings.edited_policy in ("both", "edited"):
            assert edited_render is not None
            planned.append(
                PlannedResource(
                    key="edited",
                    resource=edited_render,
                    filename=f"{stem}_edited{_extension(edited_render, '.JPG')}",
                    staging_root="edited",
                )
            )
    elif adjusted:
        # Trimmed/adjusted videos have no separate render we can fetch; the
        # original is what we move. Surfaced so it shows up in the run report.
        LOGGER.info(
            "Video %s (%s) has iCloud adjustments; uploading the unedited original.",
            asset.id,
            asset.filename,
        )

    if not planned:
        LOGGER.warning(
            "Asset %s (%s, type=%s) exposed no downloadable resource; skipping.",
            asset.id,
            asset.filename,
            item_type,
        )

    return PlannedAsset(
        asset=asset,
        asset_id=asset.id,
        master_id=asset.master_id,
        filename=asset.filename,
        stem=stem,
        item_type=item_type,
        asset_date=asset.asset_date,
        added_date=asset.added_date,
        local_date=local_capture_time(asset),
        location=extract_location(asset),
        is_live_photo=bool(asset.is_live_photo),
        has_adjustments=adjusted,
        is_favorite=_field(asset, "isFavorite") == 1,
        resources=planned,
    )
