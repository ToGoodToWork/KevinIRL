"""Background device monitor.

Polls connected cameras + microphones every few seconds, detects plug/unplug
events, and auto-selects the right device into stream.conf when:

  - The user hasn't picked anything yet (VIDEO_DEVICE/AUDIO_DEVICE == "none"),
    OR
  - The currently-saved device is gone AND a new one is now available.

We never override an explicit, currently-plugged user choice. If the user
picked a generic USB webcam and the DJI Osmo gets plugged in later, we leave
their choice alone.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

# Allow imports from sibling modules and the stream/ directory.
_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "stream"))

import devices as device_helpers  # noqa: E402
from stream_manager import manager  # noqa: E402

log = logging.getLogger("device_monitor")

POLL_INTERVAL_SEC = 2.0


class DeviceMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {"cameras": [], "microphones": [], "errors": []}
        self._signature = None
        self._last_change_at = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="device-monitor")
        self._thread.start()
        log.info("DeviceMonitor started (poll=%.1fs)", POLL_INTERVAL_SEC)

    def stop(self):
        self._stop.set()

    def get_state(self) -> dict:
        with self._lock:
            return {
                "cameras": list(self._latest.get("cameras", [])),
                "microphones": list(self._latest.get("microphones", [])),
                "changed_at": self._last_change_at,
            }

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.exception("DeviceMonitor tick failed: %s", e)
            self._stop.wait(POLL_INTERVAL_SEC)

    def _tick(self):
        devs = device_helpers.enumerate_all()
        sig = device_helpers.device_signature(devs)

        first_tick = self._signature is None
        changed = sig != self._signature

        with self._lock:
            self._latest = devs
            if changed:
                self._last_change_at = time.time()
            self._signature = sig

        if not changed:
            return

        if first_tick:
            # Don't spam toasts on startup; just do the auto-select check.
            self._auto_select(devs, silent=True)
            return

        self._auto_select(devs, silent=False)

    def _auto_select(self, devs: dict, silent: bool):
        """Write VIDEO_DEVICE/AUDIO_DEVICE to stream.conf when the saved value
        is missing or "none" AND we have a sensible choice.
        """
        conf = manager.get_config()
        updates: dict[str, str] = {}

        # Camera
        current_video = conf.get("VIDEO_DEVICE", "")
        current_video_present = bool(current_video) and any(
            c.get("device") == current_video for c in devs.get("cameras", [])
        )
        needs_video_pick = (
            current_video in ("", "none")
            or not current_video_present
        )
        if needs_video_pick:
            pick = device_helpers.pick_auto_camera(devs.get("cameras", []))
            if pick:
                clean_name = pick["name"].split(" (platform:")[0].strip()
                if pick["device"] != current_video:
                    updates["VIDEO_DEVICE"] = pick["device"]
                    updates["VIDEO_DEVICE_NAME"] = clean_name
                    if not silent:
                        manager._add_log(
                            f"Auto-selected camera: {clean_name} ({pick['device']})"
                        )

        # Microphone
        current_audio = conf.get("AUDIO_DEVICE", "")
        mics = devs.get("microphones", [])
        current_mic = next((m for m in mics if m.get("device") == current_audio), None)
        current_audio_present = current_mic is not None
        best = device_helpers.pick_auto_microphone(mics)

        # Pick when:
        # - nothing saved yet, OR
        # - saved device is gone, OR
        # - a higher-priority mic just appeared (e.g. DJI Mic plugged on top of
        #   DJI Pocket) — only auto-upgrade between known/tagged mics, never
        #   override a user pick that's already at the top tier or that we
        #   never auto-selected (priority 0 = unknown/generic).
        needs_audio_pick = (
            current_audio in ("", "none")
            or not current_audio_present
        )
        if not needs_audio_pick and best and current_mic:
            cur_tier = current_mic.get("priority", 0)
            new_tier = best.get("priority", 0)
            # Only upgrade if both the current and best are tagged devices we
            # know about, and best is strictly better.
            if new_tier > cur_tier and cur_tier > 0 and new_tier > 0:
                needs_audio_pick = True

        if needs_audio_pick and best:
            if best["device"] != current_audio:
                updates["AUDIO_DEVICE"] = best["device"]
                updates["AUDIO_DEVICE_NAME"] = best.get("card_name") or best.get("name", "")
                channels = best.get("channels")
                if channels:
                    updates["AUDIO_CHANNELS"] = str(channels)
                if not silent:
                    manager._add_log(
                        f"Auto-selected microphone: {updates['AUDIO_DEVICE_NAME']} "
                        f"({best['device']}, {channels or '?'}ch)"
                    )

        if updates:
            try:
                manager.update_config(updates)
            except Exception as e:
                log.warning("Auto-select config write failed: %s", e)


monitor = DeviceMonitor()
