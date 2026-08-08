# Deploying on Windows

Windows is the development and testing target here; the Linux VM is where this is
meant to live long-term ([DEPLOY_LINUX.md](DEPLOY_LINUX.md)). But the whole
pipeline runs natively on Windows and can be scheduled with Task Scheduler if you
want it on a desktop or Windows VM.

## Prerequisites

```powershell
winget install astral-sh.uv
winget install OliverBetz.ExifTool
```

`winget` installs exiftool to `%LOCALAPPDATA%\Programs\ExifTool\ExifTool.exe`.
If that is not on `PATH`, either add it or point at it directly in `.env`:

```ini
I2G_EXIFTOOL_BINARY=C:\Users\you\AppData\Local\Programs\ExifTool\ExifTool.exe
```

## Install

```powershell
cd X:\Work\Personal\icloud-to-gphotos
uv sync
Copy-Item .env.example .env
notepad .env                                    # set I2G_ICLOUD_USERNAME

uv run python scripts/fetch_gotohp.py           # downloads bin\gotohp-cli-x64.exe
.\bin\gotohp-cli-x64.exe creds add '<auth-string>'   # see SETUP.md
uv run i2g login                                # iCloud, prompts for 2FA

uv run i2g doctor                               # verify
uv run i2g run --dry-run                        # rehearse
```

## Schedule

```powershell
.\deploy\register-windows-task.ps1
```

Registers a daily task at 00:00 that runs `uv run --frozen i2g run` in the project
directory.

### The timezone caveat

Unlike the systemd timer, **Task Scheduler triggers fire in the machine's local
timezone** — it has no per-trigger timezone setting. So:

- **If the machine is on IST**, the default `-At "00:00"` is already what you want.
- **If it is not**, pass the local equivalent of 00:00 IST. On a UTC machine that
  is 18:30 the previous day:

```powershell
.\deploy\register-windows-task.ps1 -At "18:30"
```

Check the machine's timezone with `Get-TimeZone`.

### Running when logged out

By default the task runs only while your user is logged on. For a Windows VM that
should run unattended, register with `-RunWhetherLoggedOnOrNot` from an **elevated**
prompt:

```powershell
.\deploy\register-windows-task.ps1 -RunWhetherLoggedOnOrNot
```

This uses an S4U principal, so no password is stored. Note that the credentials
in `%APPDATA%\gotohp\gotohp.config` and the iCloud cookies under
`%LOCALAPPDATA%\icloud-to-gphotos\cookies` belong to the user profile the task
runs as — register the task as the same user that ran `creds add` and `i2g login`.

## Day-to-day operation

```powershell
Start-ScheduledTask   -TaskName 'icloud-to-gphotos'    # run now, off-schedule
Get-ScheduledTaskInfo -TaskName 'icloud-to-gphotos'    # last result, next run
Stop-ScheduledTask    -TaskName 'icloud-to-gphotos'    # cancel a running pass

uv run i2g status
uv run i2g report
```

`LastTaskResult` maps to the CLI exit codes:

| Code | Meaning |
| ---- | ------- |
| 0 | Ran cleanly |
| 1 | The run failed; see the log and report |
| 2 | iCloud re-authentication needed — run `uv run i2g login` |
| 3 | Configuration error |

Logs and JSON reports are under `%LOCALAPPDATA%\icloud-to-gphotos\{logs,reports}`.
File logs are always DEBUG regardless of `I2G_LOG_LEVEL`.

```powershell
# Tail the newest log
Get-ChildItem "$env:LOCALAPPDATA\icloud-to-gphotos\logs" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 |
    Get-Content -Wait
```

## Windows-specific notes

**Long paths.** Staging filenames are short by construction (a sanitised stem
plus extension), so the 260-character limit is not normally reachable. If you set
a deeply nested `I2G_STAGING_DIR`, enable long paths:

```powershell
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
    -Name LongPathsEnabled -Value 1
```

**Antivirus.** Real-time scanning of a staging directory that churns tens of GB
per night is slow and occasionally locks files mid-write. Consider excluding the
staging path.

**Sleep.** The task is registered with `StartWhenAvailable`, so a run missed
because the machine was asleep happens once it wakes. It will not wake the machine
by itself. For a desktop that sleeps overnight, either allow wake timers or accept
the catch-up behaviour.

**Power.** Registered with `AllowStartIfOnBatteries` and
`DontStopIfGoingOnBatteries`, so a laptop will not abandon a pass mid-batch. An
interrupted run is safe regardless — the ledger resumes it.

## Moving to the Linux VM later

The state directory is portable, which lets you migrate without re-uploading
anything. Copy `%LOCALAPPDATA%\icloud-to-gphotos` to the VM's state directory
(`/var/lib/icloud-to-gphotos` by default) and `chown` it to the service user.

The `ledger.db` records what already reached Google Photos, so the VM picks up
exactly where Windows left off.

The iCloud cookies are tied to a client identifier rather than the machine, so
they usually keep working. If the first run on the VM exits with code 2, just run
`i2g login` there once.

The gotohp credential does **not** transfer with the state directory — re-run
`creds add` as the service user on the VM.
