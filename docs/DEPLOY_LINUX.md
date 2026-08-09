# Deploying to a Linux VM

Target: a Debian 12 / Ubuntu 22.04+ VM that runs the migration every night at
**00:00 IST** with no human involvement, apart from re-authenticating iCloud
roughly monthly.

## Automated install

```bash
git clone <your-repo> icloud-to-gphotos
cd icloud-to-gphotos
sudo ./deploy/install-linux.sh
```

The script installs system packages (including exiftool) and uv, creates an
unprivileged `i2g` service user, syncs the code to `/opt/icloud-to-gphotos`,
resolves dependencies, fetches the gotohp binary, and installs and enables the
systemd timer. It is idempotent — re-run it after pulling changes.

Override the defaults with environment variables:

```bash
sudo INSTALL_DIR=/srv/i2g STATE_DIR=/mnt/data/i2g SERVICE_USER=photos \
     ./deploy/install-linux.sh
```

### Then finish the manual steps

The installer prints these with the right paths substituted. All three must be
done **as the service user**, or the pipeline will not find the credentials:

```bash
# 1. Configure
sudo -u i2g nano /opt/icloud-to-gphotos/.env

# 2. Google Photos credentials (see SETUP.md for how to obtain the string)
sudo -u i2g /opt/icloud-to-gphotos/bin/gotohp-cli_amd64 creds add '<auth-string>'

# 3. iCloud session — interactive, needs the 2FA code from your Apple devices
cd /opt/icloud-to-gphotos
sudo -u i2g .venv/bin/i2g login

# 4. Verify, then rehearse
sudo -u i2g .venv/bin/i2g doctor
sudo -u i2g .venv/bin/i2g run --dry-run
```

> `cd /opt/icloud-to-gphotos` first: settings are read from `.env` in the current
> directory. And prefer `.venv/bin/i2g` over `uv run` here — `sudo -u i2g` leaves
> `HOME` pointing at *your* home, so uv would try to write its cache there as the
> wrong user and fail. The console script needs no cache at all.

## About the schedule

`deploy/icloud-to-gphotos.timer`:

```ini
OnCalendar=*-*-* 00:00:00 Asia/Kolkata
RandomizedDelaySec=10m
Persistent=true
```

Three things worth knowing:

- **The timezone is explicit**, so the schedule is correct even though cloud VMs
  are almost always set to UTC. You do not need to change the machine's timezone.
- **A timezone in `OnCalendar=` requires systemd 252+** (Debian 12, Ubuntu 23.04+).
  On Ubuntu 22.04 (systemd 249) the timer would fail to load, so the installer
  detects this and rewrites the schedule to `18:30 UTC` — the exact equivalent,
  since IST is UTC+05:30 with no daylight saving. Check which you got with
  `systemctl cat icloud-to-gphotos.timer`.
- **`Persistent=true`** means a run missed because the VM was rebooting or
  suspended happens as soon as it is back, rather than being skipped for a day.

The 10-minute jitter exists because every scheduled job in the world fires at
exactly midnight. Spreading the start reduces the chance of being rate limited by
Apple or Google.

To use a different time, edit the unit and reload:

```bash
sudo systemctl edit icloud-to-gphotos.timer     # creates a drop-in override
sudo systemctl daemon-reload
sudo systemctl restart icloud-to-gphotos.timer  # pick up the new schedule
systemctl list-timers icloud-to-gphotos.timer   # confirm the next fire time
```

## Day-to-day operation

These are read-only and work as any user:

```bash
systemctl list-timers icloud-to-gphotos.timer        # when does it next run
systemctl status icloud-to-gphotos.service           # last result
systemctl cat icloud-to-gphotos.timer                # the effective schedule

cd /opt/icloud-to-gphotos
sudo -u i2g .venv/bin/i2g status                     # ledger progress, stuck items
sudo -u i2g .venv/bin/i2g report                     # last run as JSON
```

Starting or stopping the unit changes system state, so it needs root:

```bash
sudo systemctl start icloud-to-gphotos.service       # run now, off-schedule
sudo systemctl stop icloud-to-gphotos.service        # cancel a running pass
```

> Without `sudo`, systemd tries to escalate through polkit and you get:
>
> ```text
> Failed to execute /usr/bin/pkttyagent: No such file or directory
> Failed to start icloud-to-gphotos.service: Access denied
> ```
>
> That means the command was refused, **not** that the service failed — it was
> never invoked. Use `sudo`. (The missing `pkttyagent` is just polkit's
> interactive prompt helper, from the `polkitd` package; installing it would let
> bare `systemctl` ask for a password instead, but `sudo` is simpler.)

Reading a unit's journal also needs privileges, unless your user is in the
`adm` or `systemd-journal` group:

```bash
sudo journalctl -u icloud-to-gphotos -f              # follow a running pass
sudo journalctl -u icloud-to-gphotos --since today   # today's log
```

Per-run logs and JSON reports are also written under
`/var/lib/icloud-to-gphotos/{logs,reports}`, kept for `I2G_LOG_RETENTION` runs
(default 30). File logs are always DEBUG level regardless of `I2G_LOG_LEVEL`, so
a failed unattended run leaves enough detail to diagnose afterwards.

## Why the unit does not use `uv run`

`ExecStart` calls `/opt/icloud-to-gphotos/.venv/bin/i2g` directly. That is
deliberate. The hardening below (`ProtectSystem=strict`, `ProtectHome=read-only`)
leaves only the state directory writable, and `uv run` wants to take a lock in
its cache under `$HOME`:

```text
error: Could not acquire lock
  Caused by: Read-only file system (os error 30) at path "/home/i2g/.cache/uv/.tmpXXXX"
```

The console script has an absolute shebang into the venv's Python, so it needs
no activation, no cache, and no writable home. `uv sync` at install time is what
keeps the venv current — so **re-run `install-linux.sh` after pulling code
changes**, or the service keeps running the old dependency set.

If you hit this on an already-deployed VM and do not want to re-run the
installer, override just that line:

```bash
sudo systemctl edit icloud-to-gphotos.service
```

```ini
[Service]
ExecStart=
ExecStart=/opt/icloud-to-gphotos/.venv/bin/i2g run
```

The empty `ExecStart=` is required; it clears the original before setting the
replacement.

## What the sandbox allows to be written

Anything the pipeline writes must be inside one of these, or the run fails:

| Path | Provided by | Used for |
| ---- | ----------- | -------- |
| `/var/lib/icloud-to-gphotos` | `ReadWritePaths=` | ledger, cookies, staging, logs, reports |
| `/tmp` | `PrivateTmp=yes` | exiftool argfiles (private to the unit) |

Everything else is read-only, which is fine for the rest: gotohp only *reads*
its credential file during `upload`, and Python bytecode writes are disabled
rather than failing silently. If you move staging to another disk, add it to
`ReadWritePaths=` — see [Sizing the VM](#sizing-the-vm).

## Sizing the VM

**Disk is the only real constraint, and it does not scale with library size.**
Work happens in batches capped by `I2G_BATCH_MAX_BYTES` (20 GiB default) and
staging is wiped between them, so a 2 TB library migrates on a 40 GB disk — it
just takes more nights.

Budget roughly:

```text
I2G_BATCH_MAX_BYTES + I2G_DISK_HEADROOM_BYTES + a few GB for the OS
```

The batch cap shrinks automatically to whatever the filesystem can actually spare
above the headroom, and the run stops cleanly rather than filling the disk. For a
small VM:

```ini
I2G_BATCH_MAX_BYTES=5368709120     # 5 GiB
I2G_DISK_HEADROOM_BYTES=2147483648 # 2 GiB
```

If you have a separate data volume, put staging there:

```ini
I2G_STAGING_DIR=/mnt/data/i2g-staging
```

and add it to the unit's `ReadWritePaths=`, since the service runs with
`ProtectSystem=strict`:

```bash
sudo systemctl edit icloud-to-gphotos.service
```

```ini
[Service]
ReadWritePaths=/mnt/data/i2g-staging
```

CPU and RAM are undemanding — downloads are streamed to disk in 1 MiB chunks, so
memory does not scale with file size. 1 vCPU / 1 GB is enough. Bandwidth is the
practical limit on how fast the backlog drains.

## The first run is the long one

A large library takes several nights. Two options:

**Let the timer work through it.** Each night moves up to
`I2G_MAX_BATCHES_PER_RUN` batches (unset = drain until the library is empty or
the 20-hour timeout hits). Progress is in the ledger, so nights compose.

**Or push it manually in a detached session:**

```bash
cd /opt/icloud-to-gphotos
sudo -u i2g screen -S i2g .venv/bin/i2g run
```

Safe to interrupt at any point — the ledger means the next run resumes rather
than restarting.

## Monthly maintenance

The only recurring task is iCloud re-authentication. You will be told when it is
needed: the run exits with code 2 and sends an urgent ntfy notification.

```bash
cd /opt/icloud-to-gphotos
sudo -u i2g .venv/bin/i2g login
```

To check proactively:

```bash
sudo -u i2g .venv/bin/i2g doctor
```

## Security notes

The service runs as an unprivileged user with `ProtectSystem=strict`,
`ProtectHome=read-only`, `NoNewPrivileges`, and a restricted syscall and address
family set. It can write only to its state directory.

Two files hold secrets and should stay `0600`, owned by the service user:

| File | Contains |
| ---- | -------- |
| `/opt/icloud-to-gphotos/.env` | Apple ID, optionally the password and ntfy token |
| `~i2g/.config/gotohp/gotohp.config` | Google Photos mobile-app credential |
| `/var/lib/icloud-to-gphotos/cookies/` | iCloud session cookies and trust token |

The installer sets these permissions. If you edit `.env` as root afterwards,
re-fix ownership:

```bash
sudo chown i2g:i2g /opt/icloud-to-gphotos/.env
sudo chmod 600 /opt/icloud-to-gphotos/.env
```

## Uninstalling

```bash
sudo systemctl disable --now icloud-to-gphotos.timer
sudo rm /etc/systemd/system/icloud-to-gphotos.{service,timer}
sudo rm -rf /etc/systemd/system/icloud-to-gphotos.timer.d
sudo systemctl daemon-reload
```

Leave `/var/lib/icloud-to-gphotos` in place if you might resume later — deleting
the ledger means the next run has no record of what already reached Google Photos.
It would not delete anything wrongly (it re-verifies before deleting), but it
would re-upload, and rely on gotohp's duplicate detection to avoid duplicates.
