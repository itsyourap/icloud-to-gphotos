# Verified metadata preservation

When `I2G_BACKFILL_METADATA=true` (the default), readable capture-date tags and
GPS tags when iCloud supplies a location are required before source deletion.
ExifTool reads back writes; a successful exit alone is not a preservation receipt.
An unreadable initial probe does not authorize overwriting existing metadata.

Each resource has a persistent verification receipt bound to its source content
fingerprint and date/GPS inputs. Legacy uploads without receipts are downloaded,
verified and reconfirmed once. Changed inputs invalidate the receipt. Unknown
content fingerprints require fresh per-run metadata verification. Failure counts
remain bounded, including checksumless failures.

CloudKit GPS timestamps decode to Python datetimes. Receipt signatures encode
these as ISO 8601 strings, preserving microseconds and any timezone offset without
mutating the source location. Existing receipts for locations without timestamps
remain valid. If an older version stopped with `datetime is not JSON serializable`,
upgrade and rerun with the same ledger; no reset or metadata opt-out is needed.

Unverified staged files are excluded from upload, and the iCloud asset is kept
for retry. A verified neighboring asset can still complete. Valid receipts survive
restarts, but old upload success cannot hide a metadata failure. The run report
includes `files_verified`; write counters count verified writes, not attempts.

ExifTool is required for verifiable preservation. The reduced JPEG fallback may
still attempt date repair but cannot authorize deletion without verification.
Install ExifTool and rerun; exhausted resources require the normal retry recovery.
Setting `I2G_BACKFILL_METADATA=false` explicitly opts out of this metadata gate.
It does not bypass the other resource, album, age or Live Photo deletion checks.
