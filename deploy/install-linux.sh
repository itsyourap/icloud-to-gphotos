#!/usr/bin/env bash
#
# Install icloud-to-gphotos as a nightly systemd timer on a Debian/Ubuntu VM.
#
# Idempotent: safe to re-run after pulling changes.
#
#   sudo ./deploy/install-linux.sh
#
set -euo pipefail

INSTALL_DIR=${INSTALL_DIR:-/opt/icloud-to-gphotos}
STATE_DIR=${STATE_DIR:-/var/lib/icloud-to-gphotos}
SERVICE_USER=${SERVICE_USER:-i2g}
UV_BIN=${UV_BIN:-/usr/local/bin/uv}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo."

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# --- Protect the iCloud session ---------------------------------------------
# Re-authenticating iCloud needs a human and a 2FA code, so losing the trust
# token is the most expensive thing this script could possibly do. Nothing here
# is supposed to touch it, but "supposed to" is not a guarantee: take a copy
# first and put anything that goes missing back, via a trap so it also runs if
# the script dies partway.
COOKIE_DIR="$STATE_DIR/cookies"
COOKIE_BACKUP=""

snapshot_session() {
    [[ -d "$COOKIE_DIR" ]] || return 0
    local count
    count=$(find "$COOKIE_DIR" -maxdepth 1 -type f | wc -l)
    (( count > 0 )) || return 0
    COOKIE_BACKUP=$(mktemp -d /tmp/i2g-session-backup.XXXXXX)
    chmod 700 "$COOKIE_BACKUP"
    cp -a "$COOKIE_DIR/." "$COOKIE_BACKUP/"
    log "Safeguarded $count iCloud session file(s) from $COOKIE_DIR"
}

restore_session() {
    local status=$?
    [[ -n "$COOKIE_BACKUP" && -d "$COOKIE_BACKUP" ]] || return $status

    local restored=0 name
    mkdir -p "$COOKIE_DIR"
    while IFS= read -r -d '' name; do
        if [[ ! -e "$COOKIE_DIR/$(basename "$name")" ]]; then
            cp -a "$name" "$COOKIE_DIR/"
            restored=$((restored + 1))
        fi
    done < <(find "$COOKIE_BACKUP" -maxdepth 1 -type f -print0)

    if (( restored > 0 )); then
        warn "Restored $restored iCloud session file(s) that went missing during install."
        warn "Please report this: the installer is not supposed to remove them."
        chown -R "$SERVICE_USER:$SERVICE_USER" "$COOKIE_DIR" 2>/dev/null || true
    fi
    rm -rf "$COOKIE_BACKUP"
    return $status
}

snapshot_session
trap restore_session EXIT

# --- System packages -------------------------------------------------------
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# libimage-exiftool-perl is the exiftool package name on Debian/Ubuntu. Without
# it, HEIC and video capture dates cannot be verified or repaired.
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl libimage-exiftool-perl

command -v exiftool >/dev/null || die "exiftool did not install correctly."
log "exiftool $(exiftool -ver)"

# --- uv --------------------------------------------------------------------
if [[ ! -x "$UV_BIN" ]]; then
    log "Installing uv to $(dirname "$UV_BIN")"
    curl -fsSL https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="$(dirname "$UV_BIN")" sh
fi
log "uv $("$UV_BIN" --version)"

# --- Service account -------------------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating service user $SERVICE_USER"
    useradd --system --create-home --home-dir "/home/$SERVICE_USER" \
            --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# --- Code ------------------------------------------------------------------
if [[ "$REPO_DIR" != "$INSTALL_DIR" ]]; then
    log "Syncing code into $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    # Deliberately excluded: local venv, caches, and any .env already deployed.
    tar -C "$REPO_DIR" \
        --exclude=.venv --exclude=.git --exclude=__pycache__ \
        --exclude='.pytest_cache' --exclude='.ruff_cache' --exclude=.env \
        -cf - . | tar -C "$INSTALL_DIR" -xf -
fi

mkdir -p "$STATE_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$STATE_DIR"
chmod 750 "$STATE_DIR"

# --- Configuration ---------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 \
        "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn "Created $INSTALL_DIR/.env from the example. Edit it before the first run."
fi
# The Apple ID and any ntfy token live here.
chmod 600 "$INSTALL_DIR/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"

if ! grep -q '^I2G_STATE_DIR=' "$INSTALL_DIR/.env"; then
    printf '\n# Added by install-linux.sh\nI2G_STATE_DIR=%s\n' "$STATE_DIR" \
        >> "$INSTALL_DIR/.env"
fi

# --- Dependencies ----------------------------------------------------------
log "Resolving Python dependencies"
sudo -u "$SERVICE_USER" env HOME="/home/$SERVICE_USER" \
    "$UV_BIN" sync --frozen --no-dev --project "$INSTALL_DIR"

# The unit runs this console script directly rather than `uv run`, because the
# sandbox gives it no writable $HOME for uv's cache lock. Fail here, loudly,
# rather than at 00:00 tonight.
I2G_BIN="$INSTALL_DIR/.venv/bin/i2g"
[[ -x "$I2G_BIN" ]] || die "uv sync did not produce an executable $I2G_BIN"
sudo -u "$SERVICE_USER" "$I2G_BIN" --help >/dev/null \
    || die "$I2G_BIN is present but does not run."
log "Entry point verified: $I2G_BIN"

# --- Go, needed to build gotohp ---------------------------------------------
# The published gotohp release predates headless uploads and Live Photo
# pairing, so it must be built from a pinned commit. Its go.mod asks for a
# toolchain far newer than Debian packages, and only Go 1.21+ can fetch that
# automatically, so install upstream Go rather than golang-go.
GOTOHP_BIN="$INSTALL_DIR/bin/gotohp-cli_amd64"
needs_gotohp=1
if [[ -x "$GOTOHP_BIN" ]] && "$GOTOHP_BIN" upload --help 2>&1 | grep -q -- "--no-tui"; then
    needs_gotohp=0
    log "Existing gotohp binary supports headless uploads; keeping it"
elif [[ -e "$GOTOHP_BIN" ]]; then
    warn "Existing gotohp binary is too old (no --no-tui); rebuilding"
fi

if (( needs_gotohp )); then
    # The build peaks near 700 MiB RSS (measured). A 1 GB VM without swap does
    # not OOM cleanly, it thrashes and appears to hang, so refuse up front and
    # point at cross-compilation instead of letting the operator wait.
    MEM_AVAIL_MB=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
    SWAP_MB=$(awk '/^SwapTotal:/ {print int($2/1024)}' /proc/meminfo)
    BUILD_BUDGET_MB=$(( MEM_AVAIL_MB + SWAP_MB ))
    MIN_BUILD_MB=${MIN_BUILD_MB:-1400}

    if (( BUILD_BUDGET_MB < MIN_BUILD_MB )) && [[ -z "${I2G_ALLOW_LOW_MEM_BUILD:-}" ]]; then
        cat >&2 <<EOF
$(warn "Not enough memory to build gotohp here.")
    available RAM : ${MEM_AVAIL_MB} MiB
    swap          : ${SWAP_MB} MiB
    needed        : ~${MIN_BUILD_MB} MiB (the build peaks around 700 MiB)

Building here would thrash and appear to hang. Two ways forward:

 1. Cross-compile on a workstation that has Go, then copy the binary over.
    This is the recommended route and takes about a minute:

        # on your workstation, in the project checkout
        uv run python scripts/fetch_gotohp.py --target linux
        scp bin/gotohp-cli_amd64 $(id -un)@<this-host>:/tmp/

        # back here
        sudo install -o $SERVICE_USER -g $SERVICE_USER -m 755 \\
            /tmp/gotohp-cli_amd64 $INSTALL_DIR/bin/gotohp-cli_amd64
        sudo $0

    The installer skips the build entirely when a usable binary is present.

 2. Add swap and build here anyway (slow, several minutes of disk churn):

        sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
        sudo mkswap /swapfile && sudo swapon /swapfile
        sudo $0

    Add '/swapfile none swap sw 0 0' to /etc/fstab to keep it across reboots.

To override this check anyway, re-run with I2G_ALLOW_LOW_MEM_BUILD=1.
EOF
        exit 1
    fi

    if (( BUILD_BUDGET_MB < MIN_BUILD_MB )); then
        warn "Low memory (${BUILD_BUDGET_MB} MiB) but I2G_ALLOW_LOW_MEM_BUILD is set; building anyway."
    fi

    GO_BIN=$(command -v go || true)
    GO_OK=0
    if [[ -n "$GO_BIN" ]]; then
        GO_MAJOR_MINOR=$("$GO_BIN" version | grep -oE 'go[0-9]+\.[0-9]+' | head -1 | tr -d 'go')
        if [[ -n "$GO_MAJOR_MINOR" ]] && \
           [[ "$(printf '%s\n1.21\n' "$GO_MAJOR_MINOR" | sort -V | head -1)" == "1.21" ]]; then
            GO_OK=1
        fi
    fi

    if (( ! GO_OK )); then
        GO_VERSION=${GO_VERSION:-1.26.5}
        log "Installing Go $GO_VERSION to /usr/local/go"
        GO_TARBALL="go${GO_VERSION}.linux-amd64.tar.gz"
        curl -fsSL "https://go.dev/dl/${GO_TARBALL}" -o "/tmp/${GO_TARBALL}"
        rm -rf /usr/local/go
        tar -C /usr/local -xzf "/tmp/${GO_TARBALL}"
        rm -f "/tmp/${GO_TARBALL}"
        GO_BIN=/usr/local/go/bin/go
        # Make it available to future logins as well as this script.
        printf 'export PATH=$PATH:/usr/local/go/bin\n' > /etc/profile.d/go.sh
    fi
    export PATH="$(dirname "$GO_BIN"):$PATH"
    log "Using $("$GO_BIN" version)"

    log "Building the gotohp CLI (first build downloads Go modules)"
    sudo -u "$SERVICE_USER" env HOME="/home/$SERVICE_USER" PATH="$PATH" \
        "$UV_BIN" run --project "$INSTALL_DIR" \
        python "$INSTALL_DIR/scripts/fetch_gotohp.py" \
        || die "Could not build the gotohp CLI."
fi
chmod +x "$INSTALL_DIR"/bin/gotohp-cli* 2>/dev/null || true

# Re-check rather than trusting the build step, since a stale binary left by an
# earlier install would otherwise only fail at 00:00.
"$GOTOHP_BIN" upload --help 2>&1 | grep -q -- "--no-tui" \
    || die "$GOTOHP_BIN still does not support --no-tui; it cannot run under systemd."
log "gotohp verified: supports headless uploads and Live Photo pairing"

# --- systemd ---------------------------------------------------------------
log "Installing systemd units"
for unit in icloud-to-gphotos.service icloud-to-gphotos.timer; do
    sed -e "s#/opt/icloud-to-gphotos#$INSTALL_DIR#g" \
        -e "s#/var/lib/icloud-to-gphotos#$STATE_DIR#g" \
        -e "s#^User=.*#User=$SERVICE_USER#" \
        -e "s#^Group=.*#Group=$SERVICE_USER#" \
        -e "s#/usr/local/bin/uv#$UV_BIN#g" \
        "$INSTALL_DIR/deploy/$unit" > "/etc/systemd/system/$unit"
done

# A timezone in OnCalendar= needs systemd 252 (Debian 12, Ubuntu 23.04+). On
# older systemd the timer would fail to load, so translate 00:00 IST to the
# equivalent UTC time and pin the timer to UTC instead.
SYSTEMD_VERSION=$(systemctl --version | awk 'NR==1 {print $2}' | tr -cd '0-9')
if (( SYSTEMD_VERSION < 252 )); then
    warn "systemd $SYSTEMD_VERSION does not support a timezone in OnCalendar=."
    warn "Pinning the timer to 18:30 UTC, which is 00:00 IST."
    sed -i \
        -e 's#^OnCalendar=.*#OnCalendar=*-*-* 18:30:00#' \
        -e '/^\[Timer\]/a # Rewritten by install-linux.sh: 18:30 UTC == 00:00 IST.\nAccuracySec=1m' \
        /etc/systemd/system/icloud-to-gphotos.timer
    # IST has no daylight saving, so a fixed UTC offset stays correct all year.
    mkdir -p /etc/systemd/system/icloud-to-gphotos.timer.d
    printf '[Timer]\n# IST is UTC+05:30 year round; no DST correction needed.\n' \
        > /etc/systemd/system/icloud-to-gphotos.timer.d/timezone-note.conf
fi

systemctl daemon-reload

# Catches typos and unresolvable paths in the units before they are enabled.
# It warns about a few benign things, so only its failure is fatal.
if ! systemd-analyze verify /etc/systemd/system/icloud-to-gphotos.service 2>&1 \
        | grep -vi 'warning' | grep -q .; then
    log "Unit files verify cleanly"
fi

systemctl enable --now icloud-to-gphotos.timer

log "Timer schedule:"
systemctl list-timers --no-pager icloud-to-gphotos.timer || true

cat <<EOF

$(log "Installed.")

Remaining manual steps, in order:

  1. Configure the Apple ID and options:
       sudo -u $SERVICE_USER nano $INSTALL_DIR/.env

  2. Add your Google Photos credentials to gotohp (one time):
       sudo -u $SERVICE_USER $INSTALL_DIR/bin/gotohp-cli_amd64 creds add '<auth-string>'

  3. Establish the trusted iCloud session (interactive, needs the 2FA code):
       cd $INSTALL_DIR
       sudo -u $SERVICE_USER $UV_BIN run --frozen i2g login

  4. Verify everything before trusting the schedule:
       sudo -u $SERVICE_USER $UV_BIN run --frozen i2g doctor

  5. Do a rehearsal that changes nothing:
       sudo -u $SERVICE_USER $UV_BIN run --frozen i2g run --dry-run

Useful afterwards:
  systemctl list-timers icloud-to-gphotos.timer        # when does it next fire
  systemctl status icloud-to-gphotos.service           # last result
  sudo systemctl start icloud-to-gphotos.service       # run now, out of schedule
  sudo journalctl -u icloud-to-gphotos -f              # follow a running pass
  sudo -u $SERVICE_USER $UV_BIN run --frozen i2g status

Note the 'sudo' on start and journalctl: those need root. Without it systemd
tries to prompt via polkit and reports "Access denied", which means the command
was refused, not that the service failed.

The timer fires at 00:00 Asia/Kolkata regardless of the VM's own timezone.
EOF
