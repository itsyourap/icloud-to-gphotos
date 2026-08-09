# icloud-to-gphotos

Nightly one-way migration of iCloud Photos into Google Photos, with metadata
preserved and **deletion gated on confirmation**.

Every night it downloads the oldest remaining originals from iCloud, uploads them
to Google Photos, and only then deletes them from iCloud — never before Google
has confirmed each file, and never for anything captured in the last week.

Built on [timlaing/pyicloud](https://github.com/timlaing/pyicloud) for the iCloud
side and [xob0t/gotohp](https://github.com/xob0t/gotohp) for the Google Photos side.

---

## What it actually does

```
                        ┌─────────────────────────────────────────┐
                        │  one batch, bounded by size and count   │
                        └─────────────────────────────────────────┘
  iCloud Photos                                                    Google Photos
  (oldest first)                                                          ▲
        │                                                                 │
        │  1. collect      plan resources per asset                        │
        ▼                                                                 │
   ┌──────────┐   2. download    ┌───────────┐   4. upload    ┌────────────┴───┐
   │ pyicloud │ ───────────────► │  staging  │ ─────────────► │  gotohp-cli    │
   └──────────┘   streamed,      └───────────┘  --no-tui,     └────────┬───────┘
        ▲         atomic               │        JSON summary           │
        │                              │                              │
        │                 3. backfill missing date/GPS      5. verify per file
        │                    with exiftool                             │
        │                                                              ▼
        │                                                      ┌───────────────┐
        └──────── 6. delete, only if verified ─────────────────│    ledger     │
                     and older than the grace period           └───────────────┘
```

Deletion requires **all three** of these, checked independently:

1. Every resource of the asset is recorded as confirmed by Google Photos —
   gotohp returned a media key, or reported it as an existing remote duplicate.
2. The asset is at least `I2G_DELETE_GRACE_DAYS` old (default 7), so a photo
   still uploading from your phone is never removed.
3. `I2G_DELETE_FROM_ICLOUD` is on and the run is not a dry run.

The ledger re-checks condition 1 immediately before the delete call, so a crash
mid-batch can never leave an asset deleted-but-not-uploaded. iCloud deletions
land in **Recently Deleted for 30 days**, so there is a recovery window even if
something does go wrong.

## Metadata handling

iCloud originals are uploaded byte-for-byte, so embedded EXIF survives untouched.
The gap is assets whose metadata is *incomplete* — screenshots, imported media,
and many videos. Google Photos dates those by upload time, which silently
scrambles a migrated timeline.

So the pipeline **backfills only what is missing**, from iCloud's own CloudKit
record, and never overwrites a tag that already exists:

| Source field                    | Written to (images)                    | Written to (videos)                            |
| ------------------------------- | -------------------------------------- | ---------------------------------------------- |
| `assetDate` + `timeZoneOffset`  | `EXIF:DateTimeOriginal` + `OffsetTime` | `QuickTime:CreateDate` (UTC), `Keys:CreationDate` (with offset) |
| `locationEnc`                   | `EXIF:GPSLatitude/Longitude/Altitude` + refs | `Keys:GPSCoordinates`, `UserData:GPSCoordinates` (ISO 6709) |
| `assetDate`                     | file mtime                             | file mtime                                     |

Two details that are easy to get wrong and are covered by integration tests
against the real exiftool binary:

- **EXIF timestamps carry no timezone.** Writing UTC would shift every photo, so
  the pipeline reconstructs the camera's wall-clock time from `timeZoneOffset`.
  QuickTime atoms, by contrast, are UTC by spec — so both are written, in their
  own reference frames.
- **`GPSCoordinates` must be ISO 6709.** Space-separated coordinates make
  exiftool emit a warning and write *nothing*, which would silently drop GPS from
  every video.

Also handled:

- **Live Photos** — the still and its `resOriginalVidCompl` MOV are downloaded
  with a shared filename stem and uploaded in one pass with `--pair-live-photos`,
  so Google Photos reassembles them into a single motion photo. The asset is only
  deletable once *both* components are confirmed.
- **Edited photos** — an asset adjusted in iCloud has an untouched `resOriginal`
  and a rendered `resJPEGFull`. pyicloud requests those fields but does not expose
  the render as a version, so this project builds it. With the default
  `I2G_EDITED_POLICY=both`, you get the pristine original *and* the edit as you
  see it today. Renders upload in a separate pass with pairing off, since they
  share a stem with the still they came from.

Not preserved, because Google Photos has no equivalent this API can reach:
iCloud album membership, favourites, hidden status, and keywords.

## Quick start

```bash
uv sync
cp .env.example .env          # set I2G_ICLOUD_USERNAME at minimum
uv run python scripts/fetch_gotohp.py   # builds gotohp; needs Go 1.21+

# one-time credential setup
./bin/gotohp-cli_amd64 creds add '<google-photos-auth-string>'   # see docs/SETUP.md
uv run i2g login                                                 # iCloud, prompts for 2FA

uv run i2g doctor             # verify every dependency and credential
uv run i2g run --dry-run      # rehearsal: downloads nothing, deletes nothing
uv run i2g run                # the real thing
```

Then schedule it:

| Platform | Command |
| -------- | ------- |
| Linux VM | `sudo ./deploy/install-linux.sh` — systemd timer at 00:00 IST |
| Windows  | `.\deploy\register-windows-task.ps1` — Task Scheduler, daily |

Full instructions: [docs/SETUP.md](docs/SETUP.md),
[docs/DEPLOY_LINUX.md](docs/DEPLOY_LINUX.md),
[docs/DEPLOY_WINDOWS.md](docs/DEPLOY_WINDOWS.md).

## Commands

| Command | Purpose |
| ------- | ------- |
| `i2g login` | Establish the trusted iCloud session. Interactive; needed once, then roughly monthly. |
| `i2g doctor` | Check binaries, credentials, session, disk, and state the deletion policy. Run before trusting the schedule. |
| `i2g run` | One migration pass. `--dry-run`, `--no-delete`, `--max-batches N`. |
| `i2g status` | Ledger progress, session health, recent runs, anything stuck. |
| `i2g report` | The last run's JSON report. `--run-id` for a specific one. |

Exit codes: `0` ok, `1` failed, `2` iCloud re-authentication needed, `3` config error.

## Operational notes

**Disk stays bounded.** Work is done in batches capped at 20 GiB / 500 items,
and the cap shrinks automatically to whatever the filesystem can spare above
`I2G_DISK_HEADROOM_BYTES`. Staging is wiped between batches, so a 2 TB library
migrates fine on a 40 GB VM — it just takes more nights.

**Resumable by construction.** All progress lives in a SQLite ledger. Kill it
mid-run and the next run picks up where it stopped, re-downloading nothing that
was already confirmed.

**Nothing retries forever.** A file that fails 5 upload attempts is marked
blocked, reported, and skipped, so one bad asset cannot stall the queue. Its
asset stays in iCloud. `i2g status` lists them.

**iCloud 2FA expires.** Apple invalidates trust tokens roughly monthly. When
that happens the run exits with code 2 and sends an urgent notification; you run
`i2g login` once on the host. There is no way to automate this away.

**Oldest-first is deliberate.** The grace period protects the newest items, so
ascending order steadily drains the backlog instead of repeatedly re-examining
photos that are too recent to touch.

## Configuration

Every setting is an `I2G_`-prefixed environment variable or `.env` entry. See
[.env.example](.env.example) for the annotated full list. The ones that matter
most:

| Variable | Default | Why you would change it |
| -------- | ------- | ----------------------- |
| `I2G_ICLOUD_USERNAME` | — | Required. |
| `I2G_DELETE_FROM_ICLOUD` | `true` | Set `false` to download and upload only, and just report what *would* be deleted. |
| `I2G_DELETE_GRACE_DAYS` | `7` | Larger is safer; `0` deletes as soon as Google confirms. |
| `I2G_EDITED_POLICY` | `both` | `edited` or `original` if you would rather not have duplicate pairs for edited photos. |
| `I2G_BATCH_MAX_BYTES` | 20 GiB | Lower on a small disk. |
| `I2G_STAGING_DIR` | `<state>/staging` | Point at your largest disk. |
| `I2G_NTFY_TOPIC` | unset | Push run summaries and re-auth alerts to your phone. |

## Development

```bash
uv run pytest                 # 273 tests
uv run ruff check src tests
uv run mypy
```

The exiftool integration tests skip themselves when exiftool (or ffmpeg, for the
video cases) is absent. Install both to run the full suite — they are the only
tests that prove the tags written are actually accepted and read back correctly.

## Caveats

- **gotohp is an unofficial client.** It authenticates with mobile-app
  credentials, not OAuth, which is why uploads do not consume Google storage
  quota. It could break if Google changes the endpoint. The safety design means a
  breakage stops deletion rather than losing anything.
- Shared Photo Library is deliberately out of scope; deleting from it would
  affect other participants.
- Hidden and Recently Deleted albums are not migrated.

## Licence

MIT
