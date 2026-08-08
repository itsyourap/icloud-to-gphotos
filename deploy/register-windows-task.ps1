<#
.SYNOPSIS
    Register the nightly iCloud to Google Photos migration in Task Scheduler.

.DESCRIPTION
    Creates a daily task at 00:00 local time. Unlike the Linux systemd unit,
    this schedules in the machine's local timezone, so set the machine to IST or
    pass -At with the equivalent local time.

    Run from an elevated PowerShell prompt if you want the task to run whether
    or not you are logged in.

.EXAMPLE
    .\deploy\register-windows-task.ps1

.EXAMPLE
    .\deploy\register-windows-task.ps1 -At "18:30" -TaskName "i2g-nightly"
    Schedules for 18:30 local, which is 00:00 IST on a UTC machine.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "icloud-to-gphotos",
    [string]$At = "00:00",
    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [switch]$RunWhetherLoggedOnOrNot
)

$ErrorActionPreference = "Stop"

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Warn { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Yellow }

# --- Preconditions ---------------------------------------------------------
$uv = (Get-Command uv -ErrorAction SilentlyContinue)?.Source
if (-not $uv) {
    throw "uv is not on PATH. Install it from https://astral.sh/uv and re-run."
}
Write-Step "Using uv at $uv"

if (-not (Test-Path (Join-Path $ProjectDir "pyproject.toml"))) {
    throw "$ProjectDir does not look like the project root."
}

if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Write-Warn "No .env found. Copy .env.example to .env and fill it in before the first run."
}

if (-not (Get-Command exiftool -ErrorAction SilentlyContinue)) {
    Write-Warn "exiftool is not on PATH. HEIC and video capture dates cannot be repaired."
    Write-Warn "Install it with: winget install OliverBetz.ExifTool"
}

# --- Task definition -------------------------------------------------------
# Task Scheduler does not run through a shell, so invoke uv directly and let it
# resolve the locked environment.
$action = New-ScheduledTaskAction `
    -Execute $uv `
    -Argument "run --frozen i2g run" `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -Daily -At $At

# StartWhenAvailable catches up a run missed because the machine was asleep,
# matching Persistent=true on the systemd timer.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 20) `
    -RestartCount 0

# A first pass over a large library is long; do not let idle detection kill it.
$settings.RunOnlyIfIdle = $false
$settings.IdleSettings.StopOnIdleEnd = $false

$principalArgs = @{ UserId = "$env:USERDOMAIN\$env:USERNAME" }
if ($RunWhetherLoggedOnOrNot) {
    # S4U runs without storing a password, but needs elevation to register.
    $principalArgs.LogonType = "S4U"
    $principalArgs.RunLevel = "Limited"
} else {
    $principalArgs.LogonType = "Interactive"
}
$principal = New-ScheduledTaskPrincipal @principalArgs

# --- Register --------------------------------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Step "Replacing the existing '$TaskName' task"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Downloads iCloud Photos, uploads them to Google Photos, then deletes the originals from iCloud once Google confirms them." | Out-Null

Write-Step "Registered '$TaskName' to run daily at $At local time"

$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
if ($next) { Write-Host "    Next run: $next" }

Write-Host @"

Remaining manual steps, in order:

  1. Add your Google Photos credentials to gotohp (one time):
       .\bin\gotohp-cli-x64.exe creds add '<auth-string>'

  2. Establish the trusted iCloud session (interactive, needs the 2FA code):
       uv run i2g login

  3. Verify everything before trusting the schedule:
       uv run i2g doctor

  4. Do a rehearsal that changes nothing:
       uv run i2g run --dry-run

Useful afterwards:
  Start-ScheduledTask -TaskName '$TaskName'      # run now, out of schedule
  Get-ScheduledTaskInfo -TaskName '$TaskName'    # last result and next run
  uv run i2g status
  uv run i2g report

Note: this task fires at $At in the machine's LOCAL timezone. If the machine is
not on IST, pass -At with the local equivalent of 00:00 IST.
"@ -ForegroundColor Gray
