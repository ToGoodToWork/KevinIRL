"""
KevinStream - Stream Manager
Manages the FFmpeg streaming process with auto-restart and stats parsing.
"""

import collections
import logging
import os
import re
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

MAX_LOG_LINES = 200


def parse_conf(path: str) -> dict:
    """Parse shell-style KEY=VALUE config file."""
    config = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip()
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

    @property
    def stats(self) -> dict:
        with self._lock:
            s = dict(self._stats)
            if self._start_time and s["status"] == "live":
                s["uptime_seconds"] = int(time.time() - self._start_time)
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
        """Add a line to the log buffer (thread-safe)."""
        with self._lock:
            self._log_counter += 1
            self._log_buffer.append({
                "id": self._log_counter,
                "time": time.strftime("%H:%M:%S"),
                "text": line,
                "level": level,
        })

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
    def _is_process_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _kill_orphan_ffmpeg(device_path: str) -> int:
        """Find and kill any ffmpeg processes holding a device open. Returns count killed."""
        if not device_path or not os.path.exists(device_path):
            return 0

        # Get PIDs using the device via fuser
        try:
            result = subprocess.run(
                ["fuser", device_path],
                capture_output=True, text=True, timeout=5,
            )
            raw = (result.stdout + result.stderr).strip()
            pids = [int(p) for p in raw.split() if p.isdigit()]
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            return 0

        if not pids:
            return 0

        # Filter to only ffmpeg processes
        ffmpeg_pids = []
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                if "ffmpeg" in cmdline:
                    ffmpeg_pids.append(pid)
            except (FileNotFoundError, PermissionError):
                pass

        if not ffmpeg_pids:
            return 0

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

    def update_config(self, updates: dict) -> dict:
        """Update stream configuration values. Adds new keys if they don't exist."""
        with open(CONF_FILE) as f:
            lines = f.readlines()

        updated_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}\n")
                    updated_keys.add(key)
                    continue
            new_lines.append(line)

        # Append any new keys that weren't already in the file
        missing = set(updates.keys()) - updated_keys
        if missing:
            new_lines.append("\n")
            for key in sorted(missing):
                new_lines.append(f"{key}={updates[key]}\n")

        with open(CONF_FILE, "w") as f:
            f.writelines(new_lines)

        log.info("Config updated: %s", ", ".join(f"{k}={updates[k]}" for k in updates))
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

            with self._lock:
                self._process = proc
                self._start_time = time.time()
                self._stats["status"] = "live"
                self._stats["pid"] = proc.pid
                self._drift_stats = {"drift_seconds": 0.0, "stream_time_seconds": 0.0, "health": "ok"}
                self._speed_history.clear()
                self._encoding_stats = {"fps": 0, "frame": 0, "speed": 0, "quality": 0, "dropped_frames": 0}

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

        proc.wait()
        exit_code = proc.returncode

        with self._lock:
            self._stats["status"] = "stopped"
            self._stats["pid"] = None
            self._process = None

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
