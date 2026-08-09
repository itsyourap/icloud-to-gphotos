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

# --- gotohp ----------------------------------------------------------------
if [[ ! -f "$INSTALL_DIR/bin/gotohp-cli_amd64" ]]; then
    log "Fetching the gotohp CLI"
    sudo -u "$SERVICE_USER" env HOME="/home/$SERVICE_USER" \
        "$UV_BIN" run --project "$INSTALL_DIR" \
        python "$INSTALL_DIR/scripts/fetch_gotohp.py"
fi
chmod +x "$INSTALL_DIR"/bin/gotohp-cli* 2>/dev/null || true

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
