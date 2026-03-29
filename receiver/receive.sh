#!/bin/bash
# KevinStream - SRT Receiver
# Receives the SRT stream from the Pi and forwards to OBS via UDP.
# Handles audio/video resync and auto-reconnects on disconnect.
#
# Usage: ./receive.sh [PORT] [OBS_UDP_PORT]
#   PORT        - SRT listen port (default: 9000)
#   OBS_UDP_PORT - UDP port for OBS Media Source (default: 9001)
#
# OBS Media Source should be set to: udp://127.0.0.1:9001
# On Windows with SRT support in OBS, use: srt://:9000?mode=listener directly

set -uo pipefail

SRT_PORT="${1:-9000}"
OBS_PORT="${2:-9001}"

echo "╔═══════════════════════════════════════════════╗"
echo "║         KevinStream SRT Receiver              ║"
echo "╠═══════════════════════════════════════════════╣"
echo "║  SRT listening on port: ${SRT_PORT}                  ║"
echo "║  Forwarding to UDP: 127.0.0.1:${OBS_PORT}           ║"
echo "║                                               ║"
echo "║  OBS Media Source: udp://127.0.0.1:${OBS_PORT}      ║"
echo "║  (or srt://:${SRT_PORT}?mode=listener on Windows)   ║"
echo "║                                               ║"
echo "║  Press Ctrl+C to stop                         ║"
echo "╚═══════════════════════════════════════════════╝"
echo ""

while true; do
    echo "[$(date '+%H:%M:%S')] Waiting for SRT connection on port ${SRT_PORT}..."

    ffmpeg \
        -hide_banner \
        -loglevel warning \
        -i "srt://0.0.0.0:${SRT_PORT}?mode=listener&latency=800000" \
        -c:v copy \
        -c:a aac -ac 2 -ar 44100 -b:a 96k \
        -af "aresample=async=1000:first_pts=0" \
        -max_interleave_delta 500000 \
        -fflags +genpts+discardcorrupt \
        -flags +low_delay \
        -f mpegts \
        "udp://127.0.0.1:${OBS_PORT}?pkt_size=1316"

    EXIT_CODE=$?
    echo ""
    echo "[$(date '+%H:%M:%S')] Stream ended (code ${EXIT_CODE}). Reconnecting in 2s..."
    sleep 2
done
