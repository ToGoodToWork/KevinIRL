"""Probe what this Pi can actually do.

`h264_v4l2m2m` being compiled into ffmpeg is necessary but not sufficient — the
kernel has to expose a v4l2 stateful encoder device (`/dev/video1[0-9]`). On
some Pi OS kernels (especially newer Pi 5 builds) v4l2m2m is disabled and
hardware encoding silently doesn't work.

This module:
  - Detects which encoders are usable end-to-end.
  - Heuristically caps encoder × resolution × fps combos we know fail.
  - Returns a single capability matrix the UI can render and the API can
    validate against.

The probe is cached on disk so it runs once at app boot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)

import devices as device_helpers  # noqa: E402

log = logging.getLogger("capabilities")

CACHE_PATH = "/tmp/kevinstream-caps.json"
# Re-probe automatically if the cache is older than this.
CACHE_MAX_AGE_SEC = 6 * 3600


# Encoders we know about. Add to this list as we support more.
_KNOWN_ENCODERS = ("h264_v4l2m2m", "libx264")

# Sensible default ceilings. Hardware h264_v4l2m2m on Pi 4/5 is reliable up to
# 1080p30 in our tests; software libx264 ultrafast tops out around 720p30 on
# Pi 4 before drift sets in.
_ENCODER_LIMITS = {
    "h264_v4l2m2m": {
        "max_width": 1920,
        "max_height": 1080,
        "max_fps": 30,
        "max_bitrate_kbps": 10000,
    },
    "libx264": {
        "max_width": 1280,
        "max_height": 720,
        "max_fps": 30,
        "max_bitrate_kbps": 6000,
    },
}


def _ffmpeg_has_encoder(name: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return bool(re.search(rf"^\s*\S+\s+{re.escape(name)}\s", result.stdout, re.MULTILINE))


def _probe_v4l2_h264() -> bool:
    """h264_v4l2m2m needs both the ffmpeg encoder and a kernel encoder node."""
    if not _ffmpeg_has_encoder("h264_v4l2m2m"):
        return False
    # v4l2 stateful encoders live at /dev/video10..19 on Pi OS.
    for n in range(10, 20):
        path = f"/dev/video{n}"
        if not os.path.exists(path):
            continue
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", path, "--list-formats", "--device", path],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        # h264_v4l2m2m needs a CAPTURE plane that lists H264.
        if "H264" in result.stdout or "h264" in result.stdout.lower():
            return True
    return False


def _build_combo_matrix(encoders: dict, cameras: list[dict]) -> list[dict]:
    """Filter camera×encoder×res×fps combos by the encoder limit table.

    We don't actually launch ffmpeg per combo — that would take minutes. The
    table above represents what we've observed working; if a user hits a real
    limit we hadn't recorded, ffmpeg will fail loudly and the immediate-crash
    detector (M1.3) catches it.
    """
    matrix: list[dict] = []
    for cam in cameras:
        cam_path = cam.get("device", "")
        cam_name = cam.get("name", "")
        for enc_name, available in encoders.items():
            if not available:
                continue
            limits = _ENCODER_LIMITS.get(enc_name, {})
            for res in cam.get("resolutions", []):
                try:
                    w, h = (int(x) for x in res.split("x"))
                except ValueError:
                    continue
                if w > limits.get("max_width", 99999) or h > limits.get("max_height", 99999):
                    continue
                fps_list = cam.get("fps_by_resolution", {}).get(res, [])
                supported_fps = [int(f) for f in fps_list if f <= limits.get("max_fps", 999)]
                if not supported_fps:
                    continue
                matrix.append({
                    "camera": cam_path,
                    "camera_name": cam_name,
                    "encoder": enc_name,
                    "resolution": res,
                    "fps": supported_fps,
                })
    return matrix


def probe_capabilities(use_cache: bool = True) -> dict:
    """Probe the system and return the capability matrix. Caches to disk."""
    if use_cache and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                cached = json.load(f)
            age = time.time() - cached.get("probed_at", 0)
            if age < CACHE_MAX_AGE_SEC:
                return cached
        except (OSError, ValueError):
            pass

    encoders = {
        "h264_v4l2m2m": _probe_v4l2_h264(),
        "libx264": _ffmpeg_has_encoder("libx264"),
    }
    available_encoders = [name for name, ok in encoders.items() if ok]

    cameras = device_helpers.list_cameras()
    matrix = _build_combo_matrix(encoders, cameras)

    caps = {
        "probed_at": time.time(),
        "encoders": encoders,
        "available_encoders": available_encoders,
        "encoder_limits": _ENCODER_LIMITS,
        "tested_combos": matrix,
    }
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(caps, f, indent=2)
    except OSError as e:
        log.warning("Failed to cache capabilities to %s: %s", CACHE_PATH, e)

    return caps


def validate_combo(encoder: str, width: int, height: int, fps: int, bitrate_kbps: int) -> tuple[bool, str]:
    """Cheap config-time check against the static encoder limits.

    Returns (ok, error_message). Does NOT require the combo to be in the
    camera-specific matrix — the user might be configuring before plugging in
    the camera, and we don't want to block that.
    """
    if encoder not in _KNOWN_ENCODERS:
        return False, f"Unknown encoder '{encoder}'"
    limits = _ENCODER_LIMITS.get(encoder, {})
    if width > limits.get("max_width", 99999):
        return False, f"{encoder} doesn't support width {width} (max {limits['max_width']})"
    if height > limits.get("max_height", 99999):
        return False, f"{encoder} doesn't support height {height} (max {limits['max_height']})"
    if fps > limits.get("max_fps", 999):
        return False, f"{encoder} doesn't support {fps}fps (max {limits['max_fps']})"
    max_br = limits.get("max_bitrate_kbps", 99999)
    if bitrate_kbps > max_br:
        return False, f"{encoder} bitrate {bitrate_kbps}kbps exceeds cap {max_br}kbps"
    return True, ""
