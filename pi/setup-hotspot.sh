#!/bin/bash
# KevinStream - Pi WiFi Hotspot Setup
# Creates a WiFi access point so you can always reach the dashboard
# even when internet drops.
#
# How it works:
#   - Pi creates WiFi network "KevinIRL" (password: kevinstream)
#   - Connect your phone to this WiFi
#   - Open http://192.168.4.1:8080 for the dashboard
#   - Internet comes via USB tethering (phone) or ethernet
#
# Usage: sudo bash setup-hotspot.sh [SSID] [PASSWORD]

set -euo pipefail

SSID="${1:-KevinIRL}"
PASSWORD="${2:-kevinstream}"
HOTSPOT_IP="192.168.4.1"
IFACE="wlan0"

# ── Colors ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       KevinStream - WiFi Hotspot Setup           ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── Check root ──
if [ "$EUID" -ne 0 ]; then
    fail "Run as root: sudo bash setup-hotspot.sh"
fi

# ── Check NetworkManager ──
if ! command -v nmcli &>/dev/null; then
    fail "NetworkManager (nmcli) not found. Is this Raspberry Pi OS Bookworm?"
fi

ok "NetworkManager found"

# ── Validate password ──
if [ ${#PASSWORD} -lt 8 ]; then
    fail "WiFi password must be at least 8 characters"
fi

# ── Remove existing hotspot if present ──
if nmcli con show hotspot &>/dev/null 2>&1; then
    info "Removing existing hotspot configuration..."
    nmcli con delete hotspot &>/dev/null || true
fi

# ── Create the access point ──
info "Creating WiFi hotspot '${SSID}' on ${IFACE}..."

nmcli con add \
    con-name hotspot \
    ifname "${IFACE}" \
    type wifi \
    ssid "${SSID}" \
    autoconnect yes \
    >/dev/null

ok "Connection created"

# ── Configure AP mode ──
info "Configuring AP mode (2.4GHz, channel 6)..."

nmcli con modify hotspot \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    802-11-wireless.channel 6 \
    ipv4.method shared \
    ipv4.addresses "${HOTSPOT_IP}/24" \
    ipv6.method disabled

ok "AP mode configured"

# ── Set WPA2 security ──
info "Setting WPA2-PSK security..."

nmcli con modify hotspot \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "${PASSWORD}"

ok "Security configured"

# ── Set connection priority (lower than ethernet/USB) ──
nmcli con modify hotspot \
    connection.autoconnect-priority -10

# ── Bring up the hotspot ──
info "Starting hotspot..."

if nmcli con up hotspot 2>&1; then
    ok "Hotspot is running!"
else
    warn "Hotspot may have failed to start. Check: nmcli con show hotspot"
fi

# ── Verify ──
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  WiFi Network:  ${CYAN}${SSID}${NC}"
echo -e "  Password:      ${CYAN}${PASSWORD}${NC}"
echo -e "  Dashboard:     ${CYAN}http://${HOTSPOT_IP}:8080${NC}"
echo ""
echo -e "  ${YELLOW}Connect your phone to '${SSID}' WiFi${NC}"
echo -e "  ${YELLOW}Then open http://${HOTSPOT_IP}:8080${NC}"
echo ""
echo -e "  The hotspot starts automatically on every boot."
echo -e "  Internet comes via USB tethering or ethernet."
echo ""
echo -e "${GREEN}════════════════════════════════════════════════════${NC}"
echo ""

# ── Show network status ──
info "Current network status:"
nmcli device status
echo ""
ip addr show "${IFACE}" | grep "inet "
