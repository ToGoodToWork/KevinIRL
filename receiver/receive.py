#!/usr/bin/env python3
"""
KevinStream Receiver
Cross-platform SRT receiver that feeds into OBS.

Listens for the Pi's SRT stream and outputs to a local UDP port
that OBS can read via Media Source.

Usage:
    python receive.py                        # defaults: SRT on :9000, UDP to 127.0.0.1:9001
    python receive.py --port 9000 --obs-port 9001
    python receive.py --passphrase mysecretpass123

In OBS, add Media Source with:
    Input:  udp://127.0.0.1:9001
    Format: mpegts
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import time


def find_ffmpeg() -> str:
    """Find ffmpeg binary on the system."""
    path = shutil.which("ffmpeg")
    if path:
        return path

    # Common locations
    candidates = []
    if platform.system() == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe"),
            os.path.expandvars(r"%LocalAppData%\ffmpeg\bin\ffmpeg.exe"),
            r"C:\ffmpeg\bin\ffmpeg.exe",
        ]
    elif platform.system() == "Darwin":
        candidates = [
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
        ]

    for c in candidates:
        if os.path.isfile(c):
            return c

    return ""


def check_srt_support(ffmpeg: str) -> bool:
    """Check if ffmpeg was built with SRT support."""
    try:
        result = subprocess.run(
            [ffmpeg, "-protocols"],
            capture_output=True, text=True, timeout=5,
        )
        return "srt" in result.stdout.lower()
    except Exception:
        return False


def build_srt_url(port: int, passphrase: str) -> str:
    """Build the SRT listener URL."""
    url = f"srt://0.0.0.0:{port}?mode=listener"
    if passphrase:
        url += f"&passphrase={passphrase}"
    return url


def run_receiver(ffmpeg: str, srt_url: str, obs_port: int):
    """Run ffmpeg as SRT listener, outputting to local UDP for OBS.

    Uses -progress pipe:2 so we get stable key=value progress on stderr (easier
    to parse than the human-readable single line) alongside ffmpeg's own log
    lines about input format, etc.
    """
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "info",
        "-stats_period", "1",
        "-progress", "pipe:2",
        "-i", srt_url,
        "-c", "copy",
        "-f", "mpegts",
        f"udp://127.0.0.1:{obs_port}?pkt_size=1316",
    ]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=1,
    )


# Regex for parsing ffmpeg's "Input #0" line:
# "Stream #0:0: Video: mjpeg (Baseline), yuvj420p(...), 1920x1080 [...], 30 fps, ..."
_RE_VIDEO = re.compile(
    r"Stream #\d+[:\.]\d+.*Video:\s*(\w+).*?,\s*(\d+x\d+).*?(\d+(?:\.\d+)?)\s*fps",
    re.IGNORECASE,
)
_RE_AUDIO = re.compile(
    r"Stream #\d+[:\.]\d+.*Audio:\s*(\w+).*?(\d+)\s*Hz.*?(\w+)",
    re.IGNORECASE,
)


def main():
    parser = argparse.ArgumentParser(description="KevinStream SRT Receiver")
    parser.add_argument("--port", type=int, default=9000, help="SRT listen port (default: 9000)")
    parser.add_argument("--obs-port", type=int, default=9001, help="Local UDP port for OBS (default: 9001)")
    parser.add_argument(
        "--passphrase",
        type=str,
        default=os.environ.get("KEVINSTREAM_PASSPHRASE", ""),
        help="SRT passphrase (REQUIRED, min 10 chars). Must match SRT_PASSPHRASE in the Pi's stream.conf. "
             "Can also be set via KEVINSTREAM_PASSPHRASE env var.",
    )
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║           KevinStream Receiver                   ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # Find ffmpeg
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[ERROR] ffmpeg not found!")
        print()
        if platform.system() == "Windows":
            print("  Install: winget install ffmpeg")
            print("  Or download from https://ffmpeg.org/download.html")
        elif platform.system() == "Darwin":
            print("  Install: brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-srt")
        else:
            print("  Install: sudo apt install ffmpeg")
        sys.exit(1)

    print(f"  [OK] ffmpeg: {ffmpeg}")

    # Check SRT support
    if not check_srt_support(ffmpeg):
        print("[ERROR] ffmpeg does not have SRT support!")
        print()
        if platform.system() == "Darwin":
            print("  Reinstall with SRT:")
            print("    brew uninstall ffmpeg")
            print("    brew tap homebrew-ffmpeg/ffmpeg")
            print("    brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-srt")
        else:
            print("  Your ffmpeg build needs --enable-libsrt")
        sys.exit(1)

    print("  [OK] SRT support detected")

    # Validate passphrase (required, ≥10 chars).
    if not args.passphrase:
        print("[ERROR] --passphrase is required.")
        print()
        print("  Use the same value you set during setup-pi.sh on the Pi")
        print("  (look in /opt/kevinstream/pi/stream/stream.conf → SRT_PASSPHRASE).")
        print()
        print("  Pass it via --passphrase, the KEVINSTREAM_PASSPHRASE env var,")
        print("  or run ./start-receiver.sh which will prompt and save it.")
        sys.exit(1)
    if len(args.passphrase) < 10:
        print(f"[ERROR] Passphrase must be at least 10 characters (got {len(args.passphrase)})")
        sys.exit(1)

    srt_url = build_srt_url(args.port, args.passphrase)
    direct_srt_for_obs = f"srt://:{args.port}?mode=listener&passphrase={args.passphrase}"

    print()
    print("  Two ways to feed OBS — pick ONE:")
    print()
    print("  ┌─ Option A — OBS receives SRT directly (skip this script) ─────┐")
    print(f"  │  Media Source URL : {direct_srt_for_obs}")
    print("  │  Input Format     : mpegts")
    print("  └──────────────────────────────────────────────────────────────┘")
    print()
    print("  ┌─ Option B — Use THIS receiver as a relay (auto-reconnects) ──┐")
    print(f"  │  Media Source URL : udp://127.0.0.1:{args.obs_port}")
    print("  │  Input Format     : mpegts")
    print("  │  Keep this terminal open while streaming.")
    print("  └──────────────────────────────────────────────────────────────┘")
    print()
    print(f"  Listening on SRT port: {args.port}")
    print(f"  Passphrase           : {'*' * len(args.passphrase)} ({len(args.passphrase)} chars)")
    print()

    run_loop(ffmpeg, srt_url, args.obs_port)


def run_loop(ffmpeg: str, srt_url: str, obs_port: int):
    """Spawn ffmpeg, parse stats, restart on disconnect."""
    restart_count = 0
    backoff = 2

    while True:
        if restart_count > 0:
            print(f"  [RECONNECT] Attempt #{restart_count} (waiting {int(backoff)}s)...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
        else:
            print("  [WAITING] Listening for Pi stream...")

        proc = run_receiver(ffmpeg, srt_url, obs_port)
        connected = False
        progress = {"frame": "0", "fps": "0", "bitrate": "0kbits/s", "speed": "1x", "drop_frames": "0"}

        try:
            for raw in proc.stderr:
                text = raw.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue

                # ffmpeg -progress writes lines like "fps=30.0", "bitrate=4521.3kbits/s",
                # "drop_frames=0", "speed=1.00x", terminated by "progress=continue".
                if "=" in text and not text.startswith(" ") and not text.startswith("["):
                    key, _, value = text.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key in progress:
                        progress[key] = value
                    if key == "progress":
                        render_status(connected, progress)
                    continue

                # Structured ffmpeg log lines.
                if "Stream #" in text and "Video:" in text:
                    m = _RE_VIDEO.search(text)
                    if m and not connected:
                        codec, res, fps = m.group(1), m.group(2), m.group(3)
                        print(f"\n  [CONNECTED] {res} {codec} @ {fps}fps — receiving from Pi")
                        connected = True
                        restart_count = 0
                        backoff = 2
                    continue
                if "Stream #" in text and "Audio:" in text:
                    m = _RE_AUDIO.search(text)
                    if m:
                        codec, hz, layout = m.group(1), m.group(2), m.group(3)
                        print(f"  [AUDIO]    {codec} {hz}Hz {layout}")
                    continue

                # Useful info / errors get printed verbatim (with leading indent).
                lower = text.lower()
                if "error" in lower or "fatal" in lower:
                    print(f"  [ERROR] {text}")
                elif "warning" in lower:
                    print(f"  [WARN]  {text}")
                # Drop the rest — most "info" lines from ffmpeg are noise once
                # we've extracted Stream info above.

            proc.wait()

        except KeyboardInterrupt:
            print("\n\n  [STOPPED] Receiver shut down by user")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return

        exit_code = proc.returncode
        print(f"\n  [DISCONNECTED — waiting for Pi] (ffmpeg exit {exit_code})")
        restart_count += 1


def render_status(connected: bool, progress: dict):
    """Render a single in-place status line summarising the live stream."""
    if not connected:
        return
    bitrate = progress.get("bitrate", "0kbits/s").replace("kbits/s", " kb/s").strip()
    fps = progress.get("fps", "0")
    speed = progress.get("speed", "1x")
    drops = progress.get("drop_frames", "0")
    # Parse out_time_us if present for uptime — fallback to frame count.
    frame = progress.get("frame", "0")
    # Flag clock drift visually.
    drift_marker = ""
    try:
        spd = float(speed.rstrip("x"))
        if spd < 0.93 or spd > 1.07:
            drift_marker = "  ⚠ drift"
    except ValueError:
        pass
    line = f"  [LIVE] {fps}fps | {bitrate} | drops: {drops} | speed: {speed}{drift_marker} | frames: {frame}"
    sys.stdout.write("\r" + line + " " * 8)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
