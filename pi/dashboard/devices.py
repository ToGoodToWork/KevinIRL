"""Shared device enumeration helpers.

Used by:
  - The `/api/devices` HTTP endpoint (manual Detect button + initial load).
  - The DeviceMonitor background thread that polls for plug/unplug events
    and auto-selects the right device when the user hasn't picked one.

Single source of truth so the dedup / format-parsing logic doesn't drift.
"""

from __future__ import annotations

import re
import subprocess
import time


# Pi internal v4l2 nodes that aren't real cameras.
_SKIP_DEVICES = {"bcm2835-codec", "bcm2835-isp", "rpi-hevc", "rpivid"}


def list_cameras() -> list[dict]:
    """Return list of cameras with their supported resolutions/fps."""
    cameras: list[dict] = []
    try:
        result = None
        for attempt in range(2):
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                break
            if attempt == 0:
                time.sleep(1)
        if not result or result.returncode != 0:
            return cameras
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return cameras

    current_name = ""
    skip_group = False
    group_found = False
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if not line.startswith("\t") and not line.startswith(" "):
            current_name = line.rstrip(":")
            name_lower = current_name.lower()
            skip_group = any(s in name_lower for s in _SKIP_DEVICES)
            group_found = False
        elif "/dev/video" in line and not skip_group and not group_found:
            dev = line.strip()
            try:
                fmt_result = subprocess.run(
                    ["v4l2-ctl", "-d", dev, "--list-formats-ext"],
                    capture_output=True, text=True, timeout=5,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            output = fmt_result.stdout
            if "Video Capture" not in output and "mjpeg" not in output.lower() and "yuyv" not in output.lower():
                continue

            res_fps: dict[str, list[float]] = {}
            current_res = None
            for fmt_line in output.splitlines():
                fmt_line = fmt_line.strip()
                if "Size:" in fmt_line and "x" in fmt_line:
                    for p in fmt_line.split():
                        if "x" in p and p[0].isdigit():
                            current_res = p
                            res_fps.setdefault(current_res, [])
                elif "fps" in fmt_line and current_res:
                    fps_match = re.search(r"([\d.]+)\s*fps", fmt_line)
                    if fps_match:
                        fps_val = float(fps_match.group(1))
                        if fps_val not in res_fps[current_res]:
                            res_fps[current_res].append(fps_val)

            resolutions = sorted(
                res_fps.keys(),
                key=lambda r: int(r.split("x")[0]),
                reverse=True,
            )
            cameras.append({
                "device": dev,
                "name": current_name,
                "resolutions": resolutions,
                "fps_by_resolution": {r: sorted(f, reverse=True) for r, f in res_fps.items()},
            })
            group_found = True
    return cameras


def list_microphones() -> list[dict]:
    """Return ALSA capture devices."""
    mics: list[dict] = []
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return mics

    if result.returncode != 0:
        return mics

    for line in result.stdout.splitlines():
        m = re.match(r"card (\d+):.*\[(.+?)\].*device (\d+):.*\[(.+?)\]", line)
        if not m:
            continue
        card, card_name, device, dev_name = m.groups()
        mics.append({
            "device": f"plughw:{card},{device}",
            "name": f"{card_name} - {dev_name}",
            "card_name": card_name,
            "card": int(card),
            "channels": _guess_channels(card_name, dev_name),
            "priority": _mic_priority(card_name, dev_name),
        })
    return mics


# Mic priority tiers — higher wins when multiple mics are connected.
# We prefer a dedicated external mic (DJI Wireless) over a camera's built-in
# pinhole (DJI Pocket), and either over an unknown USB mic.
_MIC_TIER_WIRELESS = 30   # DJI Wireless Mic RX
_MIC_TIER_CAMERA   = 20   # DJI Pocket / Osmo built-in
_MIC_TIER_USB_MIC  = 10   # generic USB mic
_MIC_TIER_FALLBACK = 0


def _mic_priority(card_name: str, dev_name: str) -> int:
    blob = f"{card_name} {dev_name}".lower()
    if "wireless microphone" in blob or "dji mic" in blob or " rx" in f" {blob}":
        return _MIC_TIER_WIRELESS
    if "dji" in blob or "osmo" in blob or "pocket" in blob:
        return _MIC_TIER_CAMERA
    if "usb" in blob and "mic" in blob:
        return _MIC_TIER_USB_MIC
    return _MIC_TIER_FALLBACK


def _guess_channels(card_name: str, dev_name: str) -> int:
    """Best-effort channel count by device family. DJI Wireless Mic RX is mono;
    DJI Pocket camera mic is stereo. Default to 2 for unknowns."""
    blob = f"{card_name} {dev_name}".lower()
    if "wireless microphone" in blob or "dji mic" in blob:
        return 1
    return 2


def enumerate_all() -> dict:
    """Return the combined cameras + microphones list (same shape as /api/devices)."""
    return {
        "cameras": list_cameras(),
        "microphones": list_microphones(),
        "errors": [],
    }


def device_signature(devices: dict) -> tuple:
    """Hashable signature of a device list — used to detect plug/unplug events."""
    cams = tuple(sorted((c.get("device", ""), c.get("name", "")) for c in devices.get("cameras", [])))
    mics = tuple(sorted((m.get("device", ""), m.get("card_name", "")) for m in devices.get("microphones", [])))
    return (cams, mics)


def pick_auto_camera(cameras: list[dict]) -> dict | None:
    """Choose the best camera to auto-select. Prefer DJI/Osmo by name."""
    if not cameras:
        return None
    for cam in cameras:
        name = (cam.get("name") or "").lower()
        if "dji" in name or "osmo" in name:
            return cam
    # Prefer MJPEG-capable cameras (DJI Osmo registers MJPEG explicitly).
    for cam in cameras:
        res = cam.get("resolutions") or []
        if res:
            return cam
    return cameras[0]


def pick_auto_microphone(mics: list[dict]) -> dict | None:
    """Choose the best mic to auto-select. Highest priority tier wins;
    ties broken by lowest card index (deterministic across reboots)."""
    if not mics:
        return None
    return max(
        mics,
        key=lambda m: (m.get("priority", 0), -int(m.get("card", 99))),
    )
