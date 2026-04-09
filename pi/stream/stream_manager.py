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
        self._start_time: float | None = None
        self._stderr_thread: threading.Thread | None = None
        self._restart_backoff = 5
        self._max_backoff = 60
        self._should_run = False

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
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def _add_log(self, line: str, level: str = "info"):
        """Add a line to the log buffer."""
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

    def get_config(self) -> dict:
        """Read current stream configuration."""
        return parse_conf(CONF_FILE)

    def update_config(self, updates: dict) -> dict:
        """Update stream configuration values."""
        lines = []
        with open(CONF_FILE) as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}\n")
                    continue
            new_lines.append(line)

        with open(CONF_FILE, "w") as f:
            f.writelines(new_lines)

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
        """Stop the FFmpeg streaming process."""
        self._should_run = False
        with self._lock:
            if not self._process:
                return False

            proc = self._process

        self._add_log(f"Stopping FFmpeg (PID {proc.pid})...")
        log.info("Stopping FFmpeg (PID %d)...", proc.pid)
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("FFmpeg did not stop, killing...")
            proc.kill()
            proc.wait(timeout=5)
        except Exception as e:
            log.error("Error stopping FFmpeg: %s", e)

        with self._lock:
            self._process = None
            self._stats["status"] = "stopped"
            self._stats["pid"] = None
            self._start_time = None

        self._add_log("FFmpeg stopped")
        log.info("FFmpeg stopped")
        return True

    def restart(self) -> bool:
        """Restart the stream."""
        self.stop()
        time.sleep(1)
        return self.start()

    def _read_stderr(self):
        """Read FFmpeg stderr for stats and log buffer."""
        proc = self._process
        if not proc or not proc.stderr:
            return

        # FFmpeg progress line patterns
        bitrate_re = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
        fps_re = re.compile(r"fps=\s*([\d.]+)")
        frame_re = re.compile(r"frame=\s*(\d+)")
        speed_re = re.compile(r"speed=\s*([\d.]+)x")
        quality_re = re.compile(r"q=\s*([\d.-]+)")
        drop_re = re.compile(r"drop=\s*(\d+)")

        # SRT stats patterns (libsrt periodic output)
        srt_rtt_re = re.compile(r"msRTT[=:]\s*([\d.]+)")
        srt_loss_re = re.compile(r"pktSndLoss[=:]\s*(\d+)")
        srt_total_re = re.compile(r"pktSent[=:]\s*(\d+)")
        srt_buf_re = re.compile(r"msSndBuf[=:]\s*([\d.]+)")
        # Alternative SRT stats format
        srt_rtt_alt = re.compile(r"rtt[=:]\s*([\d.]+)\s*ms", re.IGNORECASE)
        srt_loss_pct_re = re.compile(r"loss[=:]?\s*([\d.]+)\s*%", re.IGNORECASE)

        total_srt_sent = 0
        total_srt_lost = 0

        for line_bytes in proc.stderr:
            try:
                line = line_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                continue

            if not line:
                continue

            # Parse FFmpeg progress lines (frame= fps= q= size= time= bitrate= speed=)
            is_progress = "frame=" in line and "bitrate=" in line
            if is_progress:
                with self._lock:
                    m = bitrate_re.search(line)
                    if m:
                        self._srt_stats["bitrate_kbps"] = float(m.group(1))
                    m = fps_re.search(line)
                    if m:
                        self._encoding_stats["fps"] = float(m.group(1))
                    m = frame_re.search(line)
                    if m:
                        self._encoding_stats["frame"] = int(m.group(1))
                    m = speed_re.search(line)
                    if m:
                        self._encoding_stats["speed"] = float(m.group(1))
                    m = quality_re.search(line)
                    if m:
                        self._encoding_stats["quality"] = float(m.group(1))
                    m = drop_re.search(line)
                    if m:
                        self._encoding_stats["dropped_frames"] = int(m.group(1))
                # Don't spam logs with progress lines
                continue

            # Parse SRT stats (libsrt outputs these periodically)
            is_srt = "srt" in line.lower() or "msRTT" in line or "pktSnd" in line
            if is_srt:
                with self._lock:
                    m = srt_rtt_re.search(line) or srt_rtt_alt.search(line)
                    if m:
                        self._srt_stats["rtt_ms"] = float(m.group(1))
                    m = srt_buf_re.search(line)
                    if m:
                        self._srt_stats["send_buffer_ms"] = float(m.group(1))
                    m = srt_loss_pct_re.search(line)
                    if m:
                        self._srt_stats["packet_loss_percent"] = float(m.group(1))
                    else:
                        # Calculate loss % from packet counts
                        m_lost = srt_loss_re.search(line)
                        m_sent = srt_total_re.search(line)
                        if m_lost:
                            total_srt_lost = int(m_lost.group(1))
                        if m_sent:
                            total_srt_sent = int(m_sent.group(1))
                        if total_srt_sent > 0:
                            self._srt_stats["packet_loss_percent"] = round(
                                (total_srt_lost / total_srt_sent) * 100, 2
                            )

            # Determine log level
            lower = line.lower()
            if "error" in lower or "fatal" in lower:
                level = "error"
            elif "warning" in lower or "drop" in lower:
                level = "warn"
            else:
                level = "info"

            # Add non-progress output to log buffer
            self._add_log(line, level)

            if level in ("error", "warn"):
                log.warning("FFmpeg: %s", line)

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

        self._add_log(f"FFmpeg crashed (code {exit_code}), restarting in {self._restart_backoff}s...", "error")
        log.warning("FFmpeg crashed (code %d), restarting in %ds...", exit_code, self._restart_backoff)
        time.sleep(self._restart_backoff)

        if self._should_run:
            self._restart_backoff = min(self._restart_backoff * 2, self._max_backoff)
            self._launch()


# Singleton instance
manager = StreamManager()
