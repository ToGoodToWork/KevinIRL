#!/bin/bash
# KevinStream — Pi updater
#
# Pulls the latest code from origin/master, hard-resets the working tree so
# any drift (mode bits, untracked junk, half-applied merges) is wiped out,
# and restarts the systemd service.
#
# stream.conf is gitignored and lives outside the source tree from git's
# perspective — this script preserves it across the reset by copying it to a
# safe location, then putting it back. The tracked stream.conf.example is
# never overwritten on top of an existing stream.conf.
#
# Usage (on the Pi):
#   sudo bash /opt/kevinstream/update-pi.sh
# Or remotely (fresh fetch):
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/ToGoodToWork/KevinIRL/master/update-pi.sh)"

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

INSTALL_DIR="/opt/kevinstream"
CONF_FILE="${INSTALL_DIR}/pi/stream/stream.conf"
CONF_EXAMPLE="${INSTALL_DIR}/pi/stream/stream.conf.example"
BACKUP_DIR="/var/backups/kevinstream"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

if [ "$EUID" -ne 0 ]; then
    err "Must be run as root (use sudo)."
    exit 1
fi

if [ ! -d "$INSTALL_DIR/.git" ]; then
    err "$INSTALL_DIR is not a git checkout. Run setup-pi.sh first."
    exit 1
fi

mkdir -p "$BACKUP_DIR"

# ── 1. Preserve the runtime config ────────────────────────────────────────
echo ""
info "━━━ Preserving local stream.conf ━━━"

if [ -f "$CONF_FILE" ]; then
    cp "$CONF_FILE" "${BACKUP_DIR}/stream.conf.${TIMESTAMP}"
    ok "Backed up to ${BACKUP_DIR}/stream.conf.${TIMESTAMP}"
    SAVED_CONF=1
else
    warn "No existing stream.conf — will be bootstrapped from template."
    SAVED_CONF=0
fi

# ── 2. Stop the service so a half-pulled state can't crash-loop ──────────
echo ""
info "━━━ Stopping kevinstream service ━━━"
systemctl stop kevinstream 2>/dev/null || true
ok "Service stopped (or wasn't running)."

# ── 3. Hard-reset the working tree to origin/master ──────────────────────
echo ""
info "━━━ Fetching and resetting to origin/master ━━━"

cd "$INSTALL_DIR"
git fetch origin --prune
# Discard tracked-file edits (stream.sh mode bits, half-applied stashes, etc.)
git reset --hard origin/master
# Wipe untracked junk (but keep gitignored files like stream.conf if it survived
# the reset — git clean -fd does not remove ignored files unless -x is added).
git clean -fd
ok "Working tree matches origin/master."

# ── 4. Restore stream.conf (or bootstrap from template) ──────────────────
echo ""
info "━━━ Restoring stream.conf ━━━"

if [ "$SAVED_CONF" = "1" ]; then
    cp "${BACKUP_DIR}/stream.conf.${TIMESTAMP}" "$CONF_FILE"
    ok "Restored stream.conf from backup."
elif [ -f "$CONF_EXAMPLE" ]; then
    cp "$CONF_EXAMPLE" "$CONF_FILE"
    ok "Bootstrapped stream.conf from template (you'll need to set SRT_HOST/passphrase via the dashboard)."
else
    err "No stream.conf and no template found — install is broken."
    exit 1
fi

# ── 5. Keep only the 10 most recent backups ──────────────────────────────
find "$BACKUP_DIR" -name "stream.conf.*" -type f | sort -r | tail -n +11 | xargs -r rm -f

# ── 5.5. Reset ownership so the dashboard (non-root) can write stream.conf ─
SERVICE_USER=$(systemctl show -p User kevinstream --value 2>/dev/null || echo "")
if [ -z "$SERVICE_USER" ] || [ "$SERVICE_USER" = "root" ]; then
    # Fall back to inspecting the unit file directly.
    SERVICE_USER=$(grep -E '^User=' /etc/systemd/system/kevinstream.service 2>/dev/null | head -1 | cut -d= -f2 || echo "")
fi
if [ -z "$SERVICE_USER" ]; then
    # Last resort: assume the directory's current owner (set during setup-pi.sh).
    SERVICE_USER=$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo "")
fi
if [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != "root" ]; then
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
    chmod 664 "$CONF_FILE"
    ok "Ownership restored to ${SERVICE_USER}; stream.conf writable by dashboard."
else
    warn "Could not determine service user — leave ownership as-is. Dashboard config writes may fail."
fi

# ── 6. Refresh Python deps if requirements.txt changed ───────────────────
if [ -d "${INSTALL_DIR}/venv" ] && [ -f "${INSTALL_DIR}/pi/requirements.txt" ]; then
    echo ""
    info "━━━ Updating Python packages ━━━"
    "${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/pi/requirements.txt" || warn "pip install had issues — service may still run."
    ok "Packages up to date."
fi

# ── 6.4. Ensure log directory exists for the dashboard's rotating log ────
echo ""
info "━━━ Ensuring /var/log/kevinstream exists ━━━"
LOG_DIR="/var/log/kevinstream"
mkdir -p "$LOG_DIR"
if [ -n "${SERVICE_USER:-}" ] && [ "$SERVICE_USER" != "root" ]; then
    chown "${SERVICE_USER}:${SERVICE_USER}" "$LOG_DIR"
fi
chmod 0755 "$LOG_DIR"
ok "Log dir at $LOG_DIR (owned by ${SERVICE_USER:-unknown})"

# ── 6.5. Ensure sudoers drop-in for nmcli (wifi scan needs it) ───────────
# The dashboard service runs as the install user and calls `sudo nmcli` for
# wifi scan/connect. Without NOPASSWD, sudo fails silently in the systemd
# non-interactive context and the scan returns empty. Setup-pi.sh writes
# this file; for already-installed Pis we ensure it exists here.
echo ""
info "━━━ Ensuring sudoers drop-in for nmcli ━━━"
SUDOERS_FILE="/etc/sudoers.d/kevinstream-nmcli"
NMCLI_PATH="$(command -v nmcli || echo /usr/bin/nmcli)"
EXPECTED_LINE="${SERVICE_USER:-$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null)} ALL=(root) NOPASSWD: ${NMCLI_PATH}"
if [ -z "$SERVICE_USER" ] || [ "$SERVICE_USER" = "root" ]; then
    warn "Could not determine service user — skipping sudoers update."
elif [ -f "$SUDOERS_FILE" ] && grep -qF "$EXPECTED_LINE" "$SUDOERS_FILE"; then
    ok "Sudoers drop-in already correct."
else
    cat > "${SUDOERS_FILE}.tmp" <<EOF
# Managed by KevinStream update-pi.sh / setup-pi.sh — DO NOT EDIT
${EXPECTED_LINE}
EOF
    chmod 0440 "${SUDOERS_FILE}.tmp"
    if visudo -c -f "${SUDOERS_FILE}.tmp" >/dev/null 2>&1; then
        mv "${SUDOERS_FILE}.tmp" "${SUDOERS_FILE}"
        ok "Sudoers drop-in installed (${SERVICE_USER} may run nmcli without password)."
    else
        rm -f "${SUDOERS_FILE}.tmp"
        warn "Sudoers validation failed — wifi scan may not work until fixed manually."
    fi
fi

# ── 7. Start the service again ────────────────────────────────────────────
echo ""
info "━━━ Starting kevinstream service ━━━"
systemctl daemon-reload
systemctl start kevinstream
sleep 1
if systemctl is-active --quiet kevinstream; then
    ok "Service is running."
else
    err "Service failed to start — see: sudo journalctl -u kevinstream -e"
    exit 1
fi

echo ""
ok "Update complete. Current commit:"
git log --oneline -1
echo ""
info "Tail the logs with: sudo journalctl -u kevinstream -f"
