#!/bin/bash
# KevinStream - Quick Install (repo already cloned)
# For full fresh-Pi setup, use ../setup-pi.sh instead.

set -euo pipefail

INSTALL_DIR="/opt/kevinstream"
VENV_DIR="${INSTALL_DIR}/venv"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash install.sh"
    exit 1
fi

ACTUAL_USER="${SUDO_USER:-pi}"

echo "[1/5] Installing system packages..."
apt-get update -qq
apt-get install -y -qq ffmpeg python3-pip python3-venv libraspberrypi-bin

echo "[2/5] Copying project files..."
mkdir -p "${INSTALL_DIR}/pi"
cp -r "${PROJECT_DIR}/pi/"* "${INSTALL_DIR}/pi/"

echo "[3/5] Setting up Python environment..."
python3 -m venv "$VENV_DIR"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/pi/requirements.txt"

echo "[4/5] Setting permissions..."
chmod +x "${INSTALL_DIR}/pi/stream/stream.sh"
chown -R "${ACTUAL_USER}:${ACTUAL_USER}" "$INSTALL_DIR"

echo "[5/5] Installing systemd service..."
cp "${INSTALL_DIR}/pi/systemd/kevinstream.service" /etc/systemd/system/
sed -i "s|User=pi|User=${ACTUAL_USER}|" /etc/systemd/system/kevinstream.service
systemctl daemon-reload
systemctl enable kevinstream.service
systemctl start kevinstream.service

echo ""
echo "=== Done! Dashboard: http://$(hostname -I | awk '{print $1}'):8080 ==="
echo "Edit config: ${INSTALL_DIR}/pi/stream/stream.conf"
