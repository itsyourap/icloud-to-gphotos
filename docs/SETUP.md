# Setup

Four things need to be in place before the pipeline can run unattended:

1. [Python dependencies](#1-python-dependencies)
2. [The gotohp CLI and Google Photos credentials](#2-google-photos-credentials) ← the fiddly one
3. [exiftool](#3-exiftool)
4. [A trusted iCloud session](#4-icloud-session)

Then [verify](#5-verify) before scheduling anything.

---

## 1. Python dependencies

Requires Python 3.11–3.13 and [uv](https://docs.astral.sh/uv/).

```bash
cd icloud-to-gphotos
uv sync
cp .env.example .env
```

Edit `.env` and set at minimum:

```ini
I2G_ICLOUD_USERNAME=you@example.com
```

Everything else has a working default. `.env.example` documents each option and
why you might change it.

---

## 2. Google Photos credentials

This is the only genuinely awkward step, and it is a one-time cost.

### Why it works this way

gotohp does not use OAuth. It authenticates as the **Google Photos Android app**,
which is what lets uploads bypass your Google storage quota. There is no way to
obtain that credential from a web console — it has to be captured from a real
Android app during login.

The alternative (Google's official Photos Library API) needs a GCP project, counts
uploads against your quota, and — in Testing mode — expires refresh tokens every
7 days, which breaks unattended runs.

### Get the CLI

```bash
uv run python scripts/fetch_gotohp.py
```

This downloads the correct release binary for your platform into `./bin`:

| Platform | Asset |
| -------- | ----- |
| Linux    | `gotohp-cli_amd64` |
| Windows  | `gotohp-cli-x64.exe` |
| macOS    | `gotohp-cli-macos-universal` |

### Get the credential string

Follow the upstream instructions, which are authoritative and kept current:
**<https://github.com/xob0t/gotohp#getting-credentials>**

Two routes exist. Summarised so you know what you are in for:

**ReVanced (no root)** — install a ReVanced-patched Google Photos on an Android
device or emulator, connect over ADB, watch the logs while you sign in, and pull
the `androidId=` parameter out of the auth log line.

**Official APK (root required)** — intercept the Google Photos login request with
HTTP Toolkit on a rooted device and capture the request body.

Either way you end up with one long credential string.

### Register it

```bash
# Linux
./bin/gotohp-cli_amd64 creds add '<auth-string>'
./bin/gotohp-cli_amd64 creds list

# Windows
.\bin\gotohp-cli-x64.exe creds add '<auth-string>'
.\bin\gotohp-cli-x64.exe creds list
```

`creds list` should print the associated Google account. gotohp stores this in its
own config file:

| Platform | Location |
| -------- | -------- |
| Linux    | `$XDG_CONFIG_HOME/gotohp/gotohp.config` (usually `~/.config/gotohp/`) |
| Windows  | `%APPDATA%\gotohp\gotohp.config` |

If you need it somewhere else — a service account's home, for instance — set
`I2G_GOTOHP_CONFIG` to the path and it is passed through with `--config`.

> **Deploying to a VM:** run `creds add` **as the service user**, or the pipeline
> will not find the credential. `deploy/install-linux.sh` prints the exact command
> with the right user substituted.

---

## 3. exiftool

Strongly recommended. Without it, HEIC and video files whose embedded capture
date is missing cannot be repaired, and Google Photos will file them by upload
time. `i2g doctor` fails if `I2G_BACKFILL_METADATA` is on and exiftool is absent,
rather than letting metadata quality degrade silently.

```bash
# Debian / Ubuntu
sudo apt install libimage-exiftool-perl

# Windows
winget install OliverBetz.ExifTool

# macOS
brew install exiftool
```

Verify with `exiftool -ver`. If it is installed somewhere unusual, set
`I2G_EXIFTOOL_BINARY`.

> On Windows, winget installs to
> `%LOCALAPPDATA%\Programs\ExifTool\ExifTool.exe`. If that is not on `PATH`,
> either add it or point `I2G_EXIFTOOL_BINARY` at it directly.

---

## 4. iCloud session

```bash
uv run i2g login
```

It prompts for your Apple ID password, then the 6-digit code sent to your Apple
devices. On success it stores session cookies and Apple's **trust token** under
`<state_dir>/cookies`, which is what lets later runs work with no interaction.

The password is never written to disk by this project. Once a trusted session
exists it is not read again, so you can leave `I2G_ICLOUD_PASSWORD` unset.

### This will need repeating

Apple invalidates trust tokens after roughly a month. There is no way around it —
2FA is designed to require a human. When it happens:

- `i2g run` exits with code **2** and sends an urgent notification (if ntfy is
  configured) saying re-authentication is needed.
- `i2g doctor` reports the session as failed.
- No photos are downloaded, uploaded, or deleted. Nothing is lost; the run simply
  does not happen.

Fix it by running `uv run i2g login` again on the host. Worth doing proactively if
you are going to be away.

---

## 5. Verify

```bash
uv run i2g doctor
```

Checks the gotohp binary, Google Photos credentials, exiftool, the iCloud
session, free disk against your batch cap, notifications, and echoes back the
deletion policy so it is never a surprise. Exit code 0 means ready.

Then rehearse:

```bash
uv run i2g run --dry-run
```

This connects to iCloud, plans real work against your real library, and reports
what it *would* do — without downloading, uploading, or deleting anything.

A good next step is one real but bounded pass:

```bash
uv run i2g run --max-batches 1 --no-delete
```

That moves one batch to Google Photos and deletes nothing. Check the results in
Google Photos — confirm dates, locations, and that Live Photos animate — then
drop `--no-delete` once you are satisfied.

Finally, schedule it: [DEPLOY_LINUX.md](DEPLOY_LINUX.md) or
[DEPLOY_WINDOWS.md](DEPLOY_WINDOWS.md).

---

## Notifications (optional)

ntfy needs no account. Pick an **unguessable** topic name — anyone who knows it
can read your notifications.

```ini
I2G_NTFY_TOPIC=icloud-sync-3f9a2c7b
```

Install the [ntfy app](https://ntfy.sh/app) and subscribe to the same topic. You
will get a summary after each run, and an urgent alert when iCloud
re-authentication is needed.

Self-hosting? Set `I2G_NTFY_SERVER` and, if it is access-controlled,
`I2G_NTFY_TOKEN`.

To only be notified about problems:

```ini
I2G_NOTIFY_ON_SUCCESS=false
```

---

## Troubleshooting

**`doctor` says "gotohp CLI not found"**
Run `uv run python scripts/fetch_gotohp.py`, or set `I2G_GOTOHP_BINARY` to its
path. On Linux, check it is executable: `chmod +x bin/gotohp-cli_amd64`.

**`doctor` says "no credentials stored"**
`creds add` was run as a different user than the one running the pipeline. Re-run
it as the service user, or set `I2G_GOTOHP_CONFIG` to the config file that does
have the credential.

**Run exits 2 immediately**
The iCloud session expired. Run `uv run i2g login`.

**Photos land in Google Photos with today's date**
exiftool is missing or the source had no embedded date and iCloud had no
`assetDate`. Check `i2g report` — the `metadata` section reports
`exiftool_available` and how many dates were backfilled.

**Live Photos arrive as separate photo and video**
Pairing failed. Usually the MOV's Apple content identifier is missing. Try
`I2G_IGNORE_APPLE_METADATA=true`, which pairs by filename stem instead. Note the
trade-off: on exports with duplicate stems this can mis-pair, so it is off by
default.

**"Free disk is at or below the configured headroom"**
The run stopped early on purpose. Lower `I2G_BATCH_MAX_BYTES`, lower
`I2G_DISK_HEADROOM_BYTES`, or point `I2G_STAGING_DIR` at a bigger disk.

**Items stuck after repeated failures**
`i2g status` lists them with the recorded error. They have exhausted their retry
budget and are being skipped so they cannot stall the queue; their assets remain
in iCloud. Investigate the error, then delete the ledger rows for those resources
to have them retried:

```bash
sqlite3 "$I2G_STATE_DIR/ledger.db" \
  "DELETE FROM resources WHERE state = 'failed' AND attempts >= 5;"
```
