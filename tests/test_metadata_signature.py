"""CloudKit GPS timestamps must not crash or weaken persisted metadata receipts."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from icloud_to_gphotos.assets import plan_asset
from icloud_to_gphotos.metadata import preservation_signature

from .conftest import FakePhotoAsset, encode_location

# Binary plist dates decode as naive UTC datetimes on supported Python versions.
GPS_TIMESTAMP = datetime(2020, 5, 1, 12, 0, 0, 123456)


def _planned(settings, timestamp=GPS_TIMESTAMP):
    return plan_asset(FakePhotoAsset(
        "gps-timestamp", location=encode_location(1.0, 2.0, 3.0, timestamp=timestamp),
    ), settings, "IMG_0001")


def test_decoded_gps_timestamp_has_stable_signature_without_mutating_location(settings):
    planned = _planned(settings)
    assert planned.location["timestamp"] == GPS_TIMESTAMP
    assert isinstance(planned.location["timestamp"], datetime)
    original = planned.location.copy()

    signature = preservation_signature(planned, planned.resources[0], "first-run")
    decoded_again = _planned(settings)
    assert signature == preservation_signature(
        decoded_again, decoded_again.resources[0], "next-run",
    )
    assert planned.location == original
    assert isinstance(planned.location["timestamp"], datetime)


@pytest.mark.parametrize("field", ["latitude", "longitude", "altitude", "timestamp"])
def test_changed_gps_inputs_invalidate_timestamped_receipt(settings, field):
    planned = _planned(settings)
    signature = preservation_signature(planned, planned.resources[0], "run")
    location = planned.location.copy()
    location[field] += timedelta(microseconds=1) if field == "timestamp" else 1
    changed = replace(planned, location=location)
    assert signature != preservation_signature(changed, changed.resources[0], "run")


@pytest.mark.parametrize("has_location", [False, True])
def test_existing_json_safe_receipts_keep_their_signature(settings, has_location):
    planned = _planned(settings, timestamp=None)
    if not has_location:
        planned = replace(planned, location={})
    resource = planned.resources[0]
    # Compatibility with already-persisted v1 signatures, not a new hash format.
    old_identity = [1, resource.key, resource.filename, resource.resource.checksum, resource.size,
                    planned.local_date.isoformat(), planned.location, ""]
    previous = hashlib.sha256(json.dumps(old_identity, sort_keys=True).encode()).hexdigest()
    assert preservation_signature(planned, resource, "new-run") == previous


def test_timestamp_serialization_does_not_reuse_checksumless_receipts(settings):
    planned = _planned(settings)
    resource = planned.resources[0]
    resource.resource.checksum = None
    assert preservation_signature(planned, resource, "first-run") != preservation_signature(
        planned, resource, "next-run",
    )
