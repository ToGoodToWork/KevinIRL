#!/bin/bash
# ============================================================
# KevinStream - Full Raspberry Pi Bootstrap
# Run this on a FRESH Raspberry Pi OS install.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/YOUR_USER/KevinStream/main/setup-pi.sh | bash
#   OR
#   wget -qO- https://raw.githubusercontent.com/YOUR_USER/KevinStream/main/setup-pi.sh | bash
#
# What this does:
#   1. Updates the system
#   2. Installs git, ffmpeg, python3, and other deps
#   3. Installs Tailscale VPN
#   4. Clones the KevinStream repo
#   5. Sets up Python venv and installs pip packages
#   6. Prompts you for stream config (home PC IP, passphrase)
#   7. Installs and starts the systemd service
#   8. Configures boot settings for optimal streaming
# ============================================================

set -euo pipefail

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERROR]${NC} $1"; }

INSTALL_DIR="/opt/kevinstream"
REPO_URL="https://github.com/YOUR_USER/KevinStream.git"  # <-- Change this!
VENV_DIR="${INSTALL_DIR}/venv"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║      KevinStream Pi Bootstrap        ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# ── Check: running as root or with sudo ──
if [ "$EUID" -ne 0 ]; then
    err "This script must be run as root."
    echo "  Run: sudo bash setup-pi.sh"
    exit 1
fi

# Detect the actual user (not root) for file ownership
ACTUAL_USER="${SUDO_USER:-pi}"
ACTUAL_HOME=$(eval echo "~${ACTUAL_USER}")
info "Installing for user: ${ACTUAL_USER}"

# ═══════════════════════════════════════════════════════════
# STEP 1: System Update
# ═══════════════════════════════════════════════════════════
echo ""
info "━━━ Step 1/7: Updating system packages ━━━"
apt-get update -qq
apt-get upgrade -y -qq
ok "System updated"

# ═══════════════════════════════════════════════════════════
# STEP 2: Install System Dependencies
# ═══════════════════════════════════════════════════════════
echo ""
info "━━━ Step 2/7: Installing dependencies ━━━"

PACKAGES=(
    git
    ffmpeg
    python3
    python3-pip
    python3-venv
    libraspberrypi-bin   # for vcgencmd (temperature monitoring)
    curl
    wget
)

apt-get install -y -qq "${PACKAGES[@]}"
ok "System packages installed"

# Verify ffmpeg has SRT support
if ffmpeg -protocols 2>/dev/null | grep -q srt; then
    ok "FFmpeg has SRT support"
else
    warn "FFmpeg may not have SRT support. Stream might not work."
    warn "You may need to build FFmpeg from source with --enable-libsrt"
fi

# Verify hardware encoder
if ffmpeg -encoders 2>/dev/null | grep -q h264_v4l2m2m; then
    ok "Hardware encoder (h264_v4l2m2m) available"
else
    warn "Hardware encoder not found. Will fall back to software encoding (slower)."
fi

# ═══════════════════════════════════════════════════════════
# STEP 3: Install Tailscale
# ═══════════════════════════════════════════════════════════
echo ""
info "━━━ Step 3/7: Installing Tailscale VPN ━━━"

if command -v tailscale &>/dev/null; then
    ok "Tailscale already installed"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    ok "Tailscale installed"
fi

# Check if already authenticated
if tailscale status &>/dev/null; then
    ok "Tailscale already connected"
    TS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
    info "Pi Tailscale IP: ${TS_IP}"
else
    info "Starting Tailscale authentication..."
    echo ""
    echo -e "${YELLOW}A browser link will appear below. Open it on any device to authenticate.${NC}"
    echo -e "${YELLOW}If you're headless (no monitor), copy the URL and open it on your phone/PC.${NC}"
    echo ""
    tailscale up
    TS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
    ok "Tailscale connected! Pi IP: ${TS_IP}"
fi

# ═══════════════════════════════════════════════════════════
# STEP 4: Clone Repository
# ═══════════════════════════════════════════════════════════
echo ""
info "━━━ Step 4/7: Setting up KevinStream ━━━"

if [ -d "${INSTALL_DIR}/.git" ]; then
    info "Repository already exists, pulling latest..."
    cd "$INSTALL_DIR"
    git pull --ff-only
    ok "Repository updated"
else
    # Check if user wants to clone from git or copy local files
    if [ -d "$(dirname "$0")/pi" ] 2>/dev/null; then
        # Script is being run from within the repo
        info "Copying from local repository..."
        mkdir -p "$INSTALL_DIR"
        cp -r "$(dirname "$0")"/* "$INSTALL_DIR/" 2>/dev/null || true
        cp -r "$(dirname "$0")"/.git "$INSTALL_DIR/" 2>/dev/null || true
        ok "Files copied"
    else
        info "Cloning repository..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        ok "Repository cloned"
    fi
fi

# ═══════════════════════════════════════════════════════════
# STEP 5: Python Environment
# ═══════════════════════════════════════════════════════════
echo ""
info "━━━ Step 5/7: Setting up Python environment ━━━"

if [ -d "$VENV_DIR" ]; then
    info "Virtual environment exists, updating..."
else
    python3 -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/pi/requirements.txt"
ok "Python packages installed"

# ═══════════════════════════════════════════════════════════
# STEP 6: Configure Stream Settings
# ═══════════════════════════════════════════════════════════
echo ""
info "━━━ Step 6/7: Stream configuration ━━━"

CONF_FILE="${INSTALL_DIR}/pi/stream/stream.conf"

echo ""
echo -e "${CYAN}Configure your stream settings:${NC}"
echo -e "${YELLOW}(Press Enter to keep the default value shown in brackets)${NC}"
echo ""

# Home PC Tailscale IP
read -rp "Home PC Tailscale IP [100.x.y.z]: " INPUT_HOST
if [ -n "$INPUT_HOST" ]; then
    sed -i "s|^SRT_HOST=.*|SRT_HOST=${INPUT_HOST}|" "$CONF_FILE"
    ok "SRT host set to: ${INPUT_HOST}"
else
    warn "SRT_HOST not set - edit ${CONF_FILE} later"
fi

# SRT Passphrase
read -rp "SRT passphrase (min 10 chars) [changeme]: " INPUT_PASS
if [ -n "$INPUT_PASS" ]; then
    if [ ${#INPUT_PASS} -lt 10 ]; then
        warn "Passphrase should be at least 10 characters for SRT. Using it anyway."
    fi
    sed -i "s|^SRT_PASSPHRASE=.*|SRT_PASSPHRASE=${INPUT_PASS}|" "$CONF_FILE"
    ok "SRT passphrase set"
else
    warn "Using default passphrase 'changeme' - change this before going live!"
fi

# Bitrate
echo ""
echo "Recommended bitrates:"
echo "  - WiFi:       2500k-3500k"
echo "  - 4G/LTE:     1500k-2500k"
echo "  - 3G/Unstable: 800k-1500k"
read -rp "Stream bitrate [2500k]: " INPUT_BITRATE
if [ -n "$INPUT_BITRATE" ]; then
    sed -i "s|^BITRATE=.*|BITRATE=${INPUT_BITRATE}|" "$CONF_FILE"
    ok "Bitrate set to: ${INPUT_BITRATE}"
fi

# ═══════════════════════════════════════════════════════════
# STEP 7: System Configuration & Service Install
# ═══════════════════════════════════════════════════════════
echo ""
info "━━━ Step 7/7: Finalizing setup ━━━"

# Set permissions
chmod +x "${INSTALL_DIR}/pi/stream/stream.sh"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$INSTALL_DIR"

# Optimize boot config for streaming (reduce GPU memory since we use V4L2/CMA)
BOOT_CONFIG="/boot/config.txt"
# Try newer Pi OS location first
[ -f "/boot/firmware/config.txt" ] && BOOT_CONFIG="/boot/firmware/config.txt"

if grep -q "^gpu_mem=" "$BOOT_CONFIG" 2>/dev/null; then
    sed -i "s|^gpu_mem=.*|gpu_mem=64|" "$BOOT_CONFIG"
else
    echo "gpu_mem=64" >> "$BOOT_CONFIG"
fi
ok "Boot config optimized (gpu_mem=64)"

# Install systemd service
cp "${INSTALL_DIR}/pi/systemd/kevinstream.service" /etc/systemd/system/

# Update service file with correct user and paths
sed -i "s|User=pi|User=${ACTUAL_USER}|" /etc/systemd/system/kevinstream.service
sed -i "s|/opt/kevinstream/venv|${VENV_DIR}|" /etc/systemd/system/kevinstream.service

systemctl daemon-reload
systemctl enable kevinstream.service
systemctl start kevinstream.service
ok "KevinStream service installed and started"

# ═══════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║            KevinStream Setup Complete!                  ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

LOCAL_IP=$(hostname -I | awk '{print $1}')
TS_IP=$(tailscale ip -4 2>/dev/null || echo "<tailscale-ip>")

echo -e "  Dashboard (local):     ${CYAN}http://${LOCAL_IP}:8080${NC}"
echo -e "  Dashboard (Tailscale): ${CYAN}http://${TS_IP}:8080${NC}"
echo ""
echo -e "  Pi Tailscale IP:       ${CYAN}${TS_IP}${NC}"
echo ""
echo -e "${YELLOW}Next steps on your HOME PC:${NC}"
echo "  1. Install Tailscale: https://tailscale.com/download"
echo "  2. Open OBS Studio"
echo "  3. Add Media Source with input:"
echo -e "     ${CYAN}srt://:9000?mode=listener&passphrase=YOUR_PASSPHRASE${NC}"
echo "  4. Enable WebSocket server in OBS (Tools > WebSocket Server Settings)"
echo "  5. Open the dashboard and click 'Start' to begin streaming"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status kevinstream    # Check service status"
echo "  sudo journalctl -u kevinstream -f    # View live logs"
echo "  sudo systemctl restart kevinstream   # Restart dashboard"
echo ""

# Check if reboot needed for boot config changes
if [ -n "${INPUT_HOST:-}" ]; then
    echo -e "${YELLOW}A reboot is recommended to apply boot config changes.${NC}"
    read -rp "Reboot now? [y/N]: " REBOOT
    if [[ "$REBOOT" =~ ^[Yy]$ ]]; then
        info "Rebooting..."
        reboot
    fi
fi
