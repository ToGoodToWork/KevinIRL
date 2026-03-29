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
    """Run ffmpeg as SRT listener, outputting to local UDP for OBS."""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-stats",
        "-i", srt_url,
        "-c", "copy",
        "-f", "mpegts",
        f"udp://127.0.0.1:{obs_port}?pkt_size=1316",
    ]

    print(f"  Command: {' '.join(cmd)}")
    print()

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def main():
    parser = argparse.ArgumentParser(description="KevinStream SRT Receiver")
    parser.add_argument("--port", type=int, default=9000, help="SRT listen port (default: 9000)")
    parser.add_argument("--obs-port", type=int, default=9001, help="Local UDP port for OBS (default: 9001)")
    parser.add_argument("--passphrase", type=str, default="", help="SRT passphrase (min 10 chars, or empty for none)")
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

    # Validate passphrase
    if args.passphrase and len(args.passphrase) < 10:
        print(f"[ERROR] Passphrase must be at least 10 characters (got {len(args.passphrase)})")
        sys.exit(1)

    srt_url = build_srt_url(args.port, args.passphrase)

    print()
    print(f"  Listening on SRT port: {args.port}")
    print(f"  Forwarding to OBS on:  udp://127.0.0.1:{args.obs_port}")
    if args.passphrase:
        print(f"  Passphrase: {'*' * len(args.passphrase)}")
    else:
        print("  Passphrase: none (Tailscale provides encryption)")
    print()
    print("  ┌─────────────────────────────────────────────┐")
    print(f"  │  OBS Media Source → udp://127.0.0.1:{args.obs_port}    │")
    print("  │  Input Format    → mpegts                  │")
    print("  │  Uncheck 'Local File'                      │")
    print("  └─────────────────────────────────────────────┘")
    print()

    # Auto-restart loop
    restart_count = 0
    backoff = 2

    while True:
        if restart_count > 0:
            print(f"  [RECONNECT] Attempt #{restart_count} (waiting {backoff}s)...")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 30)
        else:
            print("  [WAITING] Listening for Pi stream...")

        proc = run_receiver(ffmpeg, srt_url, args.obs_port)

        try:
            # Stream output line by line
            for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                # Reset backoff on successful data
                if "frame=" in text or "size=" in text:
                    if restart_count > 0:
                        print("  [CONNECTED] Receiving stream from Pi")
                        restart_count = 0
                        backoff = 2
                    # Print progress on same line
                    sys.stdout.write(f"\r  {text}    ")
                    sys.stdout.flush()
                else:
                    print(f"  {text}")

            proc.wait()

        except KeyboardInterrupt:
            print("\n\n  [STOPPED] Receiver shut down by user")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            break

        # Process ended — restart
        exit_code = proc.returncode
        print(f"\n  [DISCONNECTED] FFmpeg exited (code {exit_code})")
        restart_count += 1


if __name__ == "__main__":
    main()
