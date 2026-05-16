"""
KevinStream - Stream Manager
Manages the FFmpeg streaming process with auto-restart and stats parsing.
"""

import collections
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("stream_manager")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_SH = os.path.join(SCRIPT_DIR, "stream.sh")
CONF_FILE = os.path.join(SCRIPT_DIR, "stream.conf")
CONF_EXAMPLE = os.path.join(SCRIPT_DIR, "stream.conf.example")
# Persisted across dashboard restarts so we can detect ffmpeg orphans that
# survived because we put them in their own session (setsid) and then the
# dashboard restarted (update-pi.sh, crash, manual systemctl restart, etc.).
PID_FILE = "/tmp/kevinstream-ffmpeg.pid"


def _ensure_conf_exists():
    """Bootstrap stream.conf from stream.conf.example if missing.

    stream.conf is gitignored — it holds Pi-local runtime settings
    (SRT_HOST, passphrase, device picks). The example file is the
    tracked template; we copy it on first run after a fresh clone.
    """
    if os.path.exists(CONF_FILE):
        return
    if not os.path.exists(CONF_EXAMPLE):
        raise FileNotFoundError(
            f"Neither {CONF_FILE} nor {CONF_EXAMPLE} exists — "
            "config bootstrap impossible."
        )
    import shutil
    shutil.copy(CONF_EXAMPLE, CONF_FILE)


_ensure_conf_exists()

MAX_LOG_LINES = 200


def parse_conf(path: str) -> dict:
    """Parse shell-style KEY=VALUE config file.

    Values may be shlex-quoted (we write them that way to survive bash `source`
    when names contain spaces or parens, e.g. "Wireless Microphone RX" or
    "OsmoPocket3 (usb-...)"). shlex.split handles both quoted and bare values.
    """
    config = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, raw = line.partition("=")
            key = key.strip()
            raw = raw.strip()
            try:
                parts = shlex.split(raw, posix=True)
            except ValueError:
                parts = [raw]
            config[key] = parts[0] if parts else ""
    return config


class StreamManager:
    """Manages the FFmpeg streaming process."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stats = {
            "status": "stopped",
            "uptime_seconds": 0,
            "pid": None,
        }
        self._srt_stats = {
            "bitrate_kbps": 0,
            "rtt_ms": 0,
            "packet_loss_percent": 0,
            "send_buffer_ms": 0,
        }
        self._encoding_stats = {
            "fps": 0,
            "frame": 0,
            "speed": 0,
            "quality": 0,
            "dropped_frames": 0,
        }
        self._drift_stats = {
            "drift_seconds": 0.0,       # wall clock - stream time (positive = falling behind)
            "stream_time_seconds": 0.0,  # last parsed time= from FFmpeg
            "health": "ok",              # ok, warning, critical
        }
        # Rolling window of speed values (last 30 samples) for sustained slow detection
        self._speed_history = collections.deque(maxlen=30)
        self._last_stats_log_time = 0  # for periodic stats logging
        self._start_time: float | None = None
        self._stderr_thread: threading.Thread | None = None
        self._restart_backoff = 5
        self._max_backoff = 60
        self._should_run = False
        # Auto-restart when drift exceeds this (seconds). 0 = disabled.
        self.max_drift_restart = 15

        # Log ring buffer
        self._log_buffer = collections.deque(maxlen=MAX_LOG_LINES)
        self._log_counter = 0  # monotonic counter for tracking new logs

        # Last error captured from an immediate-crash launch — surfaced via API.
        self._last_error = ""
        # Consecutive crashes within ~3s of launch. Reset on success, capped to
        # stop tight crash-restart loops on bad config.
        self._immediate_crash_count = 0
        self._max_immediate_crashes = 3

        # Migrate any pre-existing unquoted stream.conf written by older versions.
        # Idempotent — re-quoting an already-quoted value is a no-op.
        self._rewrite_conf_quoted()

        # Reap any orphan ffmpeg left over from a previous dashboard process.
        # We `setsid` ffmpeg children so they survive parent death; combined
        # with a dashboard restart (update-pi.sh / systemctl / crash) this is
        # the mechanism that produces "Device or resource busy" on the next
        # Start click.
        self._reap_previous_ffmpeg()

    def _reap_previous_ffmpeg(self):
        """On boot, kill any ffmpeg recorded by a previous dashboard process."""
        try:
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return
        if not self._is_process_alive(pid):
            try:
                os.unlink(PID_FILE)
            except FileNotFoundError:
                pass
            return
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace")
        except (FileNotFoundError, PermissionError):
            cmdline = ""
        if "ffmpeg" not in cmdline:
            # PID was recycled to a non-ffmpeg process; don't touch it.
            try:
                os.unlink(PID_FILE)
            except FileNotFoundError:
                pass
            return
        log.warning("Reaping orphan ffmpeg PID %d from previous dashboard run", pid)
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(20):  # up to 2s for graceful exit
            if not self._is_process_alive(pid):
                break
            time.sleep(0.1)
        if self._is_process_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.unlink(PID_FILE)
        except FileNotFoundError:
            pass

    @staticmethod
    def _write_pid_file(pid: int):
        try:
            with open(PID_FILE, "w") as f:
                f.write(str(pid))
        except OSError as e:
            log.warning("Could not write PID file %s: %s", PID_FILE, e)

    @staticmethod
    def _clear_pid_file():
        try:
            os.unlink(PID_FILE)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove PID file %s: %s", PID_FILE, e)

    def _rewrite_conf_quoted(self):
        """Re-write stream.conf so every value goes through shlex.quote.

        Older versions wrote values like `AUDIO_DEVICE_NAME=Wireless Microphone RX`
        which bash refuses to source. parse_conf is lenient enough to read those
        broken lines, so we can recover by parsing then re-writing through the
        new quoted writer.
        """
        try:
            current = parse_conf(CONF_FILE)
            if not current:
                return
            self.update_config(current)
        except Exception as e:
            log.warning("stream.conf rewrite-on-boot failed: %s", e)

    @property
    def stats(self) -> dict:
        with self._lock:
            s = dict(self._stats)
            # Compute live status from the actual process state so the UI
            # reflects ffmpeg crashing within ~1s (before _watchdog notices).
            # If _stats says "error" or "stopped" already, don't overwrite — those
            # are intentional terminal states set by stop()/_launch() failure.
            if self._process is not None:
                if self._process.poll() is None:
                    s["status"] = "live"
                    s["pid"] = self._process.pid
                else:
                    # Process is gone but stats hasn't caught up yet.
                    if s.get("status") not in ("error", "stopped"):
                        s["status"] = "error"
                    s["pid"] = None
            if self._start_time and s["status"] == "live":
                s["uptime_seconds"] = int(time.time() - self._start_time)
            s["last_error"] = self._last_error
            return s

    @property
    def srt_stats(self) -> dict:
        with self._lock:
            return dict(self._srt_stats)

    @property
    def encoding_stats(self) -> dict:
        with self._lock:
            return dict(self._encoding_stats)

    @property
    def drift_stats(self) -> dict:
        with self._lock:
            return dict(self._drift_stats)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def _add_log(self, line: str, level: str = "info"):
        """Add a line to the in-memory UI ring buffer AND emit it to the
        Python logger so FFmpeg stderr ends up in the rotating log file
        (configured in app.py:_configure_logging)."""
        with self._lock:
            self._log_counter += 1
            self._log_buffer.append({
                "id": self._log_counter,
                "time": time.strftime("%H:%M:%S"),
                "text": line,
                "level": level,
        })
        # Mirror to the file/journald logger. Use a dedicated child logger so
        # these lines are clearly labeled in the log file.
        _logger = logging.getLogger("stream_manager.ffmpeg")
        if level == "error":
            _logger.error("%s", line)
        elif level == "warn":
            _logger.warning("%s", line)
        else:
            _logger.info("%s", line)

    def get_logs(self, since_id: int = 0) -> list:
        """Get log lines with id > since_id."""
        with self._lock:
            return [entry for entry in self._log_buffer if entry["id"] > since_id]

    def get_all_logs(self) -> list:
        """Get all log lines."""
        with self._lock:
            return list(self._log_buffer)

    def clear_logs(self):
        """Clear the log buffer."""
        with self._lock:
            self._log_buffer.clear()

    # ── Device & process utilities ──

    @staticmethod
    def _device_exists(device_path: str) -> bool:
        """Check if a device node exists."""
        return os.path.exists(device_path)

    @staticmethod
    def _audio_device_exists(plughw: str) -> bool:
        """Check whether a plughw:CARD,DEV ALSA device is currently present."""
        if not plughw or not plughw.startswith("plughw:"):
            return False
        try:
            card = plughw.split(":", 1)[1].split(",", 1)[0]
            int(card)  # ensure it's numeric
        except (ValueError, IndexError):
            return False
        return os.path.isdir(f"/proc/asound/card{card}")

    def _resolve_audio_device(self, plughw: str, friendly_name: str) -> str:
        """Resolve a saved plughw:CARD,DEV to the current card index.

        If the saved card index still exists, return plughw unchanged. Otherwise
        scan `arecord -l` for a card whose name contains friendly_name and
        return the new plughw:N,0. If nothing matches, return the original
        (ffmpeg will fail with a clear error that gets logged).
        """
        if not plughw or plughw == "none":
            return plughw
        if self._audio_device_exists(plughw):
            return plughw
        if not friendly_name:
            self._add_log(
                f"Audio device {plughw} not present and no friendly name saved — "
                "will let ffmpeg fail loudly", "warn",
            )
            return plughw

        try:
            result = subprocess.run(
                ["arecord", "-l"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self._add_log(f"arecord -l failed during device resolution: {e}", "warn")
            return plughw

        if result.returncode != 0:
            return plughw

        target = friendly_name.strip().lower()
        for line in result.stdout.splitlines():
            m = re.match(r"card (\d+):.*\[(.+?)\].*device (\d+):", line)
            if not m:
                continue
            card, card_name, device = m.groups()
            if target in card_name.strip().lower():
                resolved = f"plughw:{card},{device}"
                if resolved != plughw:
                    self._add_log(
                        f"Resolved {friendly_name!r} to {resolved} (was {plughw})",
                        "info",
                    )
                return resolved

        self._add_log(
            f"Audio device {plughw} ({friendly_name!r}) not found — ffmpeg will fail",
            "warn",
        )
        return plughw

    def _resolve_video_device(self, dev_path: str, friendly_name: str) -> str:
        """Resolve a saved /dev/videoN to the current device path by name.

        If the saved path exists, return unchanged. Otherwise scan
        `v4l2-ctl --list-devices` for a group whose label contains
        friendly_name and return the first /dev/videoN under it.
        """
        if not dev_path or dev_path == "none":
            return dev_path
        if self._device_exists(dev_path):
            return dev_path
        if not friendly_name:
            self._add_log(
                f"Video device {dev_path} not present and no friendly name saved — "
                "will let ffmpeg fail loudly", "warn",
            )
            return dev_path

        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True, text=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self._add_log(f"v4l2-ctl --list-devices failed: {e}", "warn")
            return dev_path

        if result.returncode != 0:
            return dev_path

        target = friendly_name.strip().lower()
        current_match = False
        for line in result.stdout.splitlines():
            if not line:
                continue
            if not line.startswith("\t") and not line.startswith(" "):
                current_match = target in line.strip().rstrip(":").lower()
            elif current_match and "/dev/video" in line:
                resolved = line.strip()
                if resolved != dev_path:
                    self._add_log(
                        f"Resolved {friendly_name!r} to {resolved} (was {dev_path})",
                        "info",
                    )
                return resolved

        self._add_log(
            f"Video device {dev_path} ({friendly_name!r}) not found — ffmpeg will fail",
            "warn",
        )
        return dev_path

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _find_pids_using_device(device_path: str) -> set[int]:
        """Find PIDs of processes holding `device_path` open, using every
        method available. Returns a set so multiple sources can be unioned
        without dedup work. Logs each method's result for diagnostics.

        We try in order:
          1. /proc/*/fd/* — directly readlink each fd, no external tools.
             Most reliable; only blind spot is processes owned by other users
             when running unprivileged.
          2. lsof <device>  — covers anything /proc missed (some kernel quirks).
          3. fuser <device> — last resort; needs `psmisc`.
        """
        found: set[int] = set()

        # ── Method 1: /proc/*/fd scan ──────────────────────────────────
        try:
            real_dev = os.path.realpath(device_path)
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                fd_dir = f"/proc/{entry}/fd"
                try:
                    fds = os.listdir(fd_dir)
                except (FileNotFoundError, PermissionError):
                    continue
                for fd in fds:
                    try:
                        target = os.readlink(f"{fd_dir}/{fd}")
                    except (FileNotFoundError, PermissionError):
                        continue
                    if target == device_path or target == real_dev:
                        found.add(int(entry))
                        break
        except Exception as e:
            log.debug("proc-scan for %s failed: %s", device_path, e)
        if found:
            log.info("proc-scan found PIDs holding %s: %s", device_path, sorted(found))

        # ── Method 2: lsof ─────────────────────────────────────────────
        try:
            result = subprocess.run(
                ["lsof", "-t", device_path],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.split():
                if line.isdigit():
                    found.add(int(line))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        except Exception as e:
            log.debug("lsof for %s failed: %s", device_path, e)

        # ── Method 3: fuser ────────────────────────────────────────────
        try:
            result = subprocess.run(
                ["fuser", device_path],
                capture_output=True, text=True, timeout=5,
            )
            raw = (result.stdout + result.stderr).strip()
            for p in raw.split():
                if p.isdigit():
                    found.add(int(p))
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        except Exception as e:
            log.debug("fuser for %s failed: %s", device_path, e)

        return found

    @staticmethod
    def _kill_orphan_ffmpeg(device_path: str) -> int:
        """Find and kill any ffmpeg processes holding a device open. Returns count killed."""
        if not device_path or not os.path.exists(device_path):
            return 0

        pids = StreamManager._find_pids_using_device(device_path)
        if not pids:
            log.info("orphan check: no processes found holding %s (proc/lsof/fuser all empty)", device_path)
            return 0

        # Filter to only ffmpeg processes (don't accidentally kill the dashboard
        # itself or any unrelated process that happens to share a device).
        ffmpeg_pids = []
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                if "ffmpeg" in cmdline:
                    ffmpeg_pids.append(pid)
                else:
                    log.warning("PID %d holds %s but isn't ffmpeg (cmdline=%r) — leaving alone",
                                pid, device_path, cmdline[:120])
            except (FileNotFoundError, PermissionError) as e:
                log.warning("PID %d holds %s but /proc unreadable (%s) — trying to kill anyway",
                            pid, device_path, e)
                ffmpeg_pids.append(pid)

        if not ffmpeg_pids:
            return 0
        log.info("orphan ffmpeg PIDs to kill on %s: %s", device_path, ffmpeg_pids)

        killed = 0
        for pid in ffmpeg_pids:
            # Phase 1: SIGTERM
            try:
                os.kill(pid, signal.SIGTERM)
                log.info("Sent SIGTERM to orphan ffmpeg PID %d", pid)
            except ProcessLookupError:
                killed += 1
                continue
            except Exception:
                continue

        if ffmpeg_pids:
            time.sleep(3)

        for pid in ffmpeg_pids:
            if not StreamManager._is_process_alive(pid):
                killed += 1
                continue
            # Phase 2: SIGKILL
            try:
                os.kill(pid, signal.SIGKILL)
                log.warning("Sent SIGKILL to orphan ffmpeg PID %d", pid)
            except ProcessLookupError:
                pass
            killed += 1

        if killed:
            time.sleep(1)  # Let kernel release device
        return killed

    def kill_orphans(self) -> dict:
        """Public: kill any orphaned ffmpeg processes on the configured video device."""
        conf = self.get_config()
        device = conf.get("VIDEO_DEVICE", "")
        if not device:
            return {"killed": 0, "device": ""}
        killed = self._kill_orphan_ffmpeg(device)
        if killed:
            self._add_log(f"Killed {killed} orphan ffmpeg process(es) on {device}", "warn")
        return {"killed": killed, "device": device}

    def get_config(self) -> dict:
        """Read current stream configuration."""
        return parse_conf(CONF_FILE)

    # Config keys that must never be empty/whitespace.
    _REQUIRED_KEYS = ("BITRATE", "WIDTH", "HEIGHT", "FPS", "SRT_HOST", "SRT_PORT", "ENCODER")

    # Keys with strict format requirements.
    _RE_BITRATE_VALUE = re.compile(r"^\d+k?$")
    _RE_POSITIVE_INT = re.compile(r"^\d+$")

    @classmethod
    def _validate_config_value(cls, key: str, value):
        """Raise ValueError if value is empty or malformed for the given key."""
        sval = "" if value is None else str(value).strip()
        if key in cls._REQUIRED_KEYS and not sval:
            raise ValueError(f"{key} cannot be empty")
        if not sval:
            return sval  # non-required key, empty is fine
        if key == "BITRATE" and not cls._RE_BITRATE_VALUE.match(sval):
            raise ValueError(f"BITRATE must be digits optionally followed by 'k' (got '{sval}')")
        if key in ("WIDTH", "HEIGHT", "FPS", "SRT_PORT") and not cls._RE_POSITIVE_INT.match(sval):
            raise ValueError(f"{key} must be a positive integer (got '{sval}')")
        if key in ("WIDTH", "HEIGHT", "FPS", "SRT_PORT") and int(sval) <= 0:
            raise ValueError(f"{key} must be > 0 (got '{sval}')")
        return sval

    def update_config(self, updates: dict) -> dict:
        """Update stream configuration values. Adds new keys if they don't exist.

        Raises ValueError if any value is empty/invalid for a required key.
        """
        # Validate everything before touching the file.
        old_config = self.get_config()
        validated = {}
        for key, value in updates.items():
            validated[key] = self._validate_config_value(key, value)

        with open(CONF_FILE) as f:
            lines = f.readlines()

        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in validated:
                    new_lines.append(f"{key}={shlex.quote(validated[key])}\n")
                    updated_keys.add(key)
                    continue
            new_lines.append(line)

        # Append any new keys that weren't already in the file
        missing = set(validated.keys()) - updated_keys
        if missing:
            new_lines.append("\n")
            for key in sorted(missing):
                new_lines.append(f"{key}={validated[key]}\n")

        with open(CONF_FILE, "w") as f:
            f.writelines(new_lines)

        # Log per-key, showing old → new where it actually changed.
        changes = []
        for k, v in validated.items():
            old = old_config.get(k, "")
            if old != v:
                changes.append(f"{k}={v} (was {old or '(unset)'})")
            else:
                changes.append(f"{k}={v}")
        summary = ", ".join(changes)
        log.info("Config updated: %s", summary)
        self._add_log(f"Config saved: {summary}")
        return self.get_config()

    def start(self) -> bool:
        """Start the FFmpeg streaming process."""
        with self._lock:
            if self._process and self._process.poll() is None:
                log.warning("Stream already running (PID %d)", self._process.pid)
                return False

        self._should_run = True
        self._restart_backoff = 5
        return self._launch()

    def _launch(self) -> bool:
        """Internal: launch FFmpeg subprocess."""
        try:
            conf = self.get_config()
            device = conf.get("VIDEO_DEVICE", "")
            video_name = conf.get("VIDEO_DEVICE_NAME", "")
            audio_device = conf.get("AUDIO_DEVICE", "none")
            audio_name = conf.get("AUDIO_DEVICE_NAME", "")

            # Pre-launch: resolve device paths by friendly name when the saved
            # node no longer exists (e.g. DJI Mic re-enumerated to a new card).
            resolved_video = self._resolve_video_device(device, video_name) if device else device
            resolved_audio = self._resolve_audio_device(audio_device, audio_name) if audio_device and audio_device != "none" else audio_device

            persisted = {}
            if resolved_video and resolved_video != device:
                persisted["VIDEO_DEVICE"] = resolved_video
                device = resolved_video
            if resolved_audio and resolved_audio != audio_device:
                persisted["AUDIO_DEVICE"] = resolved_audio
                audio_device = resolved_audio
            if persisted:
                try:
                    self.update_config(persisted)
                except Exception as e:
                    self._add_log(f"Failed to persist resolved device path: {e}", "warn")

            # Pre-launch: check device exists
            if device and not self._device_exists(device):
                msg = f"Video device {device} not found"
                self._add_log(msg, "error")
                log.error(msg)
                with self._lock:
                    self._stats["status"] = "error"
                    self._stats["pid"] = None
                return False

            # Pre-launch: kill any orphan ffmpeg processes on the device
            if device:
                killed = self._kill_orphan_ffmpeg(device)
                if killed:
                    self._add_log(f"Killed {killed} orphan process(es) on {device} before launch", "warn")
                    time.sleep(1)  # Let kernel release device

            config_summary = (
                f"Config: {conf.get('WIDTH')}x{conf.get('HEIGHT')}@{conf.get('FPS')}fps "
                f"bitrate={conf.get('BITRATE')} maxrate={conf.get('MAXRATE','N/A')} "
                f"bufsize={conf.get('BUFSIZE','N/A')} encoder={conf.get('ENCODER')} "
                f"device={device} protocol={conf.get('PROTOCOL')}"
            )
            self._add_log(config_summary)
            log.info(config_summary)
            self._add_log("Starting FFmpeg stream...")
            log.info("Starting FFmpeg stream...")
            proc = subprocess.Popen(
                ["bash", STREAM_SH],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )

            # Sanity check: did ffmpeg die immediately? (config error, missing
            # device, bad encoder option, etc.) Block briefly so the API caller
            # learns of the failure synchronously instead of via the watchdog.
            time.sleep(0.5)
            if proc.poll() is not None:
                exit_code = proc.returncode
                stderr_tail = self._drain_stderr_nonblocking(proc, max_bytes=8192)
                last_lines = [ln for ln in stderr_tail.splitlines() if ln.strip()][-3:]
                err_text = " | ".join(last_lines) if last_lines else "(no stderr output)"
                self._last_error = err_text
                self._immediate_crash_count += 1
                self._add_log(
                    f"FFmpeg exited immediately (code {exit_code}, "
                    f"attempt {self._immediate_crash_count}/{self._max_immediate_crashes}): "
                    f"{err_text}",
                    "error",
                )
                log.error("FFmpeg exited immediately (code %d): %s", exit_code, err_text)
                with self._lock:
                    self._stats["status"] = "error"
                    self._stats["pid"] = None
                    self._process = None
                    self._start_time = None
                if self._immediate_crash_count >= self._max_immediate_crashes:
                    self._should_run = False
                    self._add_log(
                        f"Stream failed {self._immediate_crash_count} times in a row — "
                        "manual restart required (fix config and click Start).",
                        "error",
                    )
                return False
            # Survived 500ms → not an instant-crash. Reset the counter.
            self._immediate_crash_count = 0

            with self._lock:
                self._process = proc
                self._start_time = time.time()
                self._stats["status"] = "live"
                self._stats["pid"] = proc.pid
                self._drift_stats = {"drift_seconds": 0.0, "stream_time_seconds": 0.0, "health": "ok"}
                self._speed_history.clear()
                self._encoding_stats = {"fps": 0, "frame": 0, "speed": 0, "quality": 0, "dropped_frames": 0}
            self._last_error = ""
            self._write_pid_file(proc.pid)

            self._add_log(f"FFmpeg started (PID {proc.pid})")
            log.info("FFmpeg started (PID %d)", proc.pid)

            # Start stderr reader for stats parsing
            self._stderr_thread = threading.Thread(
                target=self._read_stderr, daemon=True
            )
            self._stderr_thread.start()

            # Start watchdog
            threading.Thread(target=self._watchdog, daemon=True).start()

            return True
        except Exception as e:
            self._add_log(f"Failed to start FFmpeg: {e}", "error")
            log.error("Failed to start FFmpeg: %s", e)
            with self._lock:
                self._stats["status"] = "error"
                self._stats["pid"] = None
            return False

    @staticmethod
    def _drain_stderr_nonblocking(proc: subprocess.Popen, max_bytes: int = 8192) -> str:
        """Read whatever stderr bytes are currently buffered, without blocking.

        Safe to call once the process has exited (read returns EOF). For a
        still-running process, performs a non-blocking read of available bytes.
        """
        if not proc.stderr:
            return ""
        try:
            import fcntl
            fd = proc.stderr.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception:
            pass

        chunks = []
        remaining = max_bytes
        while remaining > 0:
            try:
                chunk = os.read(proc.stderr.fileno(), min(4096, remaining))
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            return b"".join(chunks).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def stop(self) -> bool:
        """Stop the FFmpeg streaming process. Guarantees process is dead before returning."""
        self._should_run = False
        with self._lock:
            if not self._process:
                return False
            proc = self._process

        pid = proc.pid
        self._add_log(f"Stopping FFmpeg (PID {pid})...")
        log.info("Stopping FFmpeg (PID %d)...", pid)

        # Phase 1: SIGTERM to process group
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass  # already dead
        except Exception as e:
            log.warning("SIGTERM failed: %s", e)

        # Phase 2: Wait for graceful exit
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # Phase 3: SIGKILL
            log.warning("FFmpeg PID %d did not stop after SIGTERM, sending SIGKILL...", pid)
            try:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
            # Phase 4: Final wait
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error("FFmpeg PID %d refused to die after SIGKILL", pid)

        # Phase 5: Verify death
        if self._is_process_alive(pid):
            log.error("FFmpeg PID %d still alive after stop()", pid)
            self._add_log(f"WARNING: FFmpeg PID {pid} could not be killed", "error")
        else:
            self._add_log("FFmpeg stopped")
            log.info("FFmpeg stopped")

        # Phase 6: Clean up any orphans holding the device
        try:
            conf = self.get_config()
            device = conf.get("VIDEO_DEVICE", "")
            if device:
                killed = self._kill_orphan_ffmpeg(device)
                if killed:
                    self._add_log(f"Cleaned up {killed} orphan process(es) on {device}", "warn")
        except Exception as e:
            log.warning("Orphan cleanup failed: %s", e)

        with self._lock:
            self._process = None
            self._stats["status"] = "stopped"
            self._stats["pid"] = None
            self._start_time = None
        self._clear_pid_file()

        return True

    def restart(self) -> bool:
        """Restart the stream."""
        self.stop()
        time.sleep(1)
        return self.start()

    # ── Compiled regexes (class-level, created once) ──
    _RE_BITRATE = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
    _RE_FPS = re.compile(r"fps=\s*([\d.]+)")
    _RE_FRAME = re.compile(r"frame=\s*(\d+)")
    _RE_SPEED = re.compile(r"speed=\s*([\d.]+)x")
    _RE_QUALITY = re.compile(r"q=\s*([\d.-]+)")
    _RE_DROP = re.compile(r"drop=\s*(\d+)")
    _RE_TIME = re.compile(r"time=\s*(\d+):(\d+):(\d+)\.(\d+)")
    _RE_SRT_RTT = re.compile(r"(?:msRTT|rtt)[=:]\s*([\d.]+)", re.IGNORECASE)
    _RE_SRT_BUF = re.compile(r"msSndBuf[=:]\s*([\d.]+)")
    _RE_SRT_LOSS_PCT = re.compile(r"loss[=:]?\s*([\d.]+)\s*%", re.IGNORECASE)
    _RE_SRT_PKT_LOSS = re.compile(r"pktSndLoss[=:]\s*(\d+)")
    _RE_SRT_PKT_SENT = re.compile(r"pktSent[=:]\s*(\d+)")

    def _read_stderr(self):
        """Read FFmpeg stderr in real time, parse stats, feed log buffer."""
        proc = self._process
        if not proc or not proc.stderr:
            return

        fd = proc.stderr.fileno()
        buf = b""

        try:
            while True:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk

                # Extract and process every complete line (split on \r or \n)
                while b"\r" in buf or b"\n" in buf:
                    r_pos = buf.find(b"\r")
                    n_pos = buf.find(b"\n")
                    if r_pos == -1:
                        pos = n_pos
                    elif n_pos == -1:
                        pos = r_pos
                    else:
                        pos = min(r_pos, n_pos)

                    raw = buf[:pos]
                    buf = buf[pos+2:] if buf[pos:pos+2] == b"\r\n" else buf[pos+1:]

                    try:
                        line = raw.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if not line:
                        continue

                    self._process_stderr_line(line)

        except Exception as e:
            log.error("stderr reader crashed: %s", e)
            self._add_log(f"Stats reader error: {e}", "error")

    def _process_stderr_line(self, line: str):
        """Parse a single stderr line: update stats and/or add to log."""

        # ── FFmpeg progress line ──
        if "frame=" in line and "bitrate=" in line:
            with self._lock:
                for regex, target, key, conv in (
                    (self._RE_BITRATE, self._srt_stats, "bitrate_kbps", float),
                    (self._RE_FPS, self._encoding_stats, "fps", float),
                    (self._RE_FRAME, self._encoding_stats, "frame", int),
                    (self._RE_SPEED, self._encoding_stats, "speed", float),
                    (self._RE_QUALITY, self._encoding_stats, "quality", float),
                    (self._RE_DROP, self._encoding_stats, "dropped_frames", int),
                ):
                    m = regex.search(line)
                    if m:
                        target[key] = conv(m.group(1))

                # Drift detection
                m = self._RE_TIME.search(line)
                if m and self._start_time:
                    h, mn, s, cs = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                    stream_secs = h * 3600 + mn * 60 + s + cs / 100.0
                    wall_secs = time.time() - self._start_time
                    drift = wall_secs - stream_secs
                    self._drift_stats["stream_time_seconds"] = stream_secs
                    self._drift_stats["drift_seconds"] = round(drift, 1)

                    self._speed_history.append(self._encoding_stats.get("speed", 1.0))
                    avg_speed = sum(self._speed_history) / len(self._speed_history) if self._speed_history else 1.0
                    if drift > 10 or avg_speed < 0.85:
                        self._drift_stats["health"] = "critical"
                    elif drift > 5 or avg_speed < 0.93:
                        self._drift_stats["health"] = "warning"
                    else:
                        self._drift_stats["health"] = "ok"

            # Log a stats summary every 10 seconds
            now = time.time()
            if now - self._last_stats_log_time >= 10:
                self._last_stats_log_time = now
                with self._lock:
                    fps = self._encoding_stats["fps"]
                    br = self._srt_stats["bitrate_kbps"]
                    spd = self._encoding_stats["speed"]
                    drp = self._encoding_stats["dropped_frames"]
                    dft = self._drift_stats["drift_seconds"]
                self._add_log(
                    f"[stream] fps={fps:.1f} bitrate={br:.0f}kbps speed={spd:.2f}x "
                    f"drop={drp} drift={dft:.1f}s"
                )

            # Auto-restart on excessive drift
            with self._lock:
                drift_val = self._drift_stats["drift_seconds"]
            if self.max_drift_restart > 0 and drift_val > self.max_drift_restart:
                self._add_log(
                    f"Drift too high ({drift_val:.1f}s > {self.max_drift_restart}s), auto-restarting...",
                    "warn",
                )
                threading.Thread(target=self.restart, daemon=True).start()
            return

        # ── SRT stats ──
        if "srt" in line.lower() or "msRTT" in line or "pktSnd" in line:
            with self._lock:
                m = self._RE_SRT_RTT.search(line)
                if m:
                    self._srt_stats["rtt_ms"] = float(m.group(1))
                m = self._RE_SRT_BUF.search(line)
                if m:
                    self._srt_stats["send_buffer_ms"] = float(m.group(1))
                m = self._RE_SRT_LOSS_PCT.search(line)
                if m:
                    self._srt_stats["packet_loss_percent"] = float(m.group(1))

        # ── All non-progress lines go to the log ──
        lower = line.lower()
        if "error" in lower or "fatal" in lower:
            level = "error"
        elif "warning" in lower or "drop" in lower:
            level = "warn"
        else:
            level = "info"

        self._add_log(line, level)

    def _watchdog(self):
        """Monitor FFmpeg process and auto-restart on crash."""
        proc = self._process
        if not proc:
            return

        start_time = self._start_time or time.time()
        proc.wait()
        exit_code = proc.returncode
        runtime = time.time() - start_time

        # Short-lived crash: probably a config/device error that won't fix
        # itself with a restart. Count consecutive ones and bail after a few.
        if runtime < 3 and exit_code != 0:
            self._immediate_crash_count += 1
            if self._immediate_crash_count >= self._max_immediate_crashes:
                with self._lock:
                    self._stats["status"] = "error"
                    self._stats["pid"] = None
                    self._process = None
                self._clear_pid_file()
                self._should_run = False
                self._add_log(
                    f"FFmpeg crashed {self._immediate_crash_count} times within {int(runtime)}s — "
                    "auto-restart disabled. Fix the config and click Start.",
                    "error",
                )
                return
        else:
            # Survived >3s — treat the next short crash as a fresh problem.
            self._immediate_crash_count = 0

        with self._lock:
            self._stats["status"] = "stopped"
            self._stats["pid"] = None
            self._process = None
        self._clear_pid_file()

        if not self._should_run:
            self._add_log(f"FFmpeg exited (code {exit_code}), not restarting")
            log.info("FFmpeg exited (code %d), not restarting (manual stop)", exit_code)
            return

        # Check if device still exists before attempting restart
        conf = self.get_config()
        device = conf.get("VIDEO_DEVICE", "")

        if device and not self._device_exists(device):
            self._add_log(
                f"FFmpeg crashed (code {exit_code}), device {device} is gone. "
                "Waiting for device to reappear...", "error"
            )
            log.warning("Device %s not found after crash, waiting for reappearance", device)
            waited = 0
            while self._should_run and waited < 300:  # Up to 5 minutes
                time.sleep(5)
                waited += 5
                if self._device_exists(device):
                    self._add_log(f"Device {device} reappeared after {waited}s")
                    log.info("Device %s reappeared after %ds", device, waited)
                    time.sleep(2)  # Let device fully initialize
                    break
            else:
                if self._should_run:
                    self._add_log(f"Device {device} did not reappear after 5 min, giving up", "error")
                    log.error("Device %s did not reappear, giving up auto-restart", device)
                    with self._lock:
                        self._stats["status"] = "error"
                    return

        self._add_log(
            f"FFmpeg crashed (code {exit_code}), restarting in {self._restart_backoff}s...",
            "error"
        )
        log.warning("FFmpeg crashed (code %d), restarting in %ds...", exit_code, self._restart_backoff)
        time.sleep(self._restart_backoff)

        if self._should_run:
            self._restart_backoff = min(self._restart_backoff * 2, self._max_backoff)
            self._launch()


# Singleton instance
manager = StreamManager()
