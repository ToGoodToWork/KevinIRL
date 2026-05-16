"""
KevinStream - Dashboard Server
Flask app serving the dashboard UI and providing WebSocket/REST APIs.
"""

import json
import logging
import os
import subprocess
import sys
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock

import config
import devices as devices_helper
import capabilities as capabilities_helper
from monitors import system_monitor, stream_monitor, network_monitor
from monitors import wifi_manager
from monitors.device_monitor import monitor as device_monitor

# Add stream directory to path for stream_manager import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stream"))
from stream_manager import manager

def _configure_logging():
    """Root logger: stderr (for systemd/journalctl) + rotating file at
    /var/log/kevinstream/kevinstream.log so logs survive reboots and the
    journald retention window. Falls back to stderr-only if the log dir
    isn't writable (e.g. running outside the systemd unit context)."""
    from logging.handlers import RotatingFileHandler

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Idempotent: clear any handlers basicConfig might have installed on import.
    for h in list(root.handlers):
        root.removeHandler(h)

    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    root.addHandler(stream_h)

    log_path = "/var/log/kevinstream/kevinstream.log"
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        # 10 MB × 10 files = ~100 MB retention.
        file_h = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=10)
        file_h.setFormatter(fmt)
        root.addHandler(file_h)
    except (PermissionError, OSError) as e:
        # Don't crash if the log dir isn't writable — journald still captures
        # everything via the stderr handler.
        sys.stderr.write(f"[dashboard] file logging disabled: {e}\n")


_configure_logging()
log = logging.getLogger("dashboard")

app = Flask(__name__, static_folder="static", static_url_path="/static")
sock = Sock(app)

# Initialize CPU percent tracking (first call always returns 0)
import psutil
psutil.cpu_percent(interval=None)

# Start network watchdog (auto AP fallback)
wifi_manager.start_watchdog()

# Start device monitor (plug/unplug events + DJI auto-select)
device_monitor.start()

# Probe encoder/camera capabilities once at startup and log the result so the
# user can troubleshoot why hardware encoding might be unavailable.
try:
    _initial_caps = capabilities_helper.probe_capabilities(use_cache=False)
    _enc_states = ", ".join(
        f"{name}={'available' if ok else 'unavailable'}"
        for name, ok in _initial_caps.get("encoders", {}).items()
    )
    manager._add_log(f"Capabilities probed: {_enc_states}")
    log.info("Capabilities: %s", _enc_states)
except Exception as e:
    log.warning("Capability probe at startup failed: %s", e)


# ── Static / SPA ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ── WebSocket: real-time stats ────────────────────────────────

@sock.route("/ws/stats")
def ws_stats(ws):
    """Push system + stream stats + logs + network to connected clients."""
    log.info("WebSocket client connected")
    last_log_id = 0
    last_device_change = 0.0
    try:
        while True:
            sys_stats = system_monitor.get_stats()
            strm_stats = stream_monitor.get_stats()
            net_stats = network_monitor.get_stats()
            net_status = wifi_manager.get_network_status()

            # Get new log lines since last push
            new_logs = manager.get_logs(since_id=last_log_id)
            if new_logs:
                last_log_id = new_logs[-1]["id"]

            # Include device list on every change. Skip the constant
            # devices payload on idle ticks to keep messages small.
            dev_state = device_monitor.get_state()
            devices_payload = None
            if dev_state["changed_at"] > last_device_change:
                last_device_change = dev_state["changed_at"]
                devices_payload = {
                    "cameras": dev_state["cameras"],
                    "microphones": dev_state["microphones"],
                    "changed_at": dev_state["changed_at"],
                }

            payload = {
                "system": sys_stats,
                **strm_stats,
                "connectivity": net_stats,
                "network": net_status,
                "logs": new_logs,
                "timestamp": time.time(),
            }
            if devices_payload is not None:
                payload["devices"] = devices_payload
            ws.send(json.dumps(payload))
            time.sleep(config.STATS_INTERVAL)
    except Exception:
        log.info("WebSocket client disconnected")


# ── REST: stream control ──────────────────────────────────────

@app.route("/api/stream/start", methods=["POST"])
def stream_start():
    if manager.is_running:
        return jsonify({"ok": False, "error": "Stream already running"}), 409
    ok = manager.start()
    if not ok:
        return jsonify({
            "ok": False,
            "error": manager._last_error or "FFmpeg failed to start — see logs",
        }), 500
    return jsonify({"ok": True})


@app.route("/api/stream/stop", methods=["POST"])
def stream_stop():
    if not manager.is_running:
        return jsonify({"ok": False, "error": "Stream not running"}), 409
    ok = manager.stop()
    return jsonify({"ok": ok})


@app.route("/api/stream/restart", methods=["POST"])
def stream_restart():
    ok = manager.restart()
    return jsonify({"ok": ok})


@app.route("/api/stream/force-stop", methods=["POST"])
def stream_force_stop():
    """Panic button: stop the manager AND kill any stray ffmpeg processes
    running stream.sh. Used when the regular Stop button can't catch up.
    """
    try:
        manager.stop()
    except Exception as e:
        log.warning("manager.stop() during force-stop raised: %s", e)

    killed_pids: list[int] = []
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ffmpeg.*stream.sh"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            killed_pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        for pid in killed_pids:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("pgrep/kill during force-stop failed: %s", e)

    # Reset transient state so the UI re-enables Start.
    manager._should_run = False
    manager._immediate_crash_count = 0
    with manager._lock:
        manager._stats["status"] = "stopped"
        manager._stats["pid"] = None
        manager._process = None

    if killed_pids:
        manager._add_log(f"Force-killed {len(killed_pids)} ffmpeg process(es): {killed_pids}", "warn")
    else:
        manager._add_log("Force stop: no stray ffmpeg processes found")
    return jsonify({"ok": True, "killed": killed_pids})


@app.route("/api/stream/kill-orphans", methods=["POST"])
def stream_kill_orphans():
    """Kill any orphaned ffmpeg processes holding the video device."""
    result = manager.kill_orphans()
    return jsonify({"ok": True, **result})


@app.route("/api/stream/config", methods=["GET"])
def stream_config_get():
    return jsonify(manager.get_config())


@app.route("/api/stream/config", methods=["PUT"])
def stream_config_update():
    updates = request.get_json()
    if not updates:
        return jsonify({"ok": False, "error": "No data"}), 400
    try:
        # If the caller touched any of (ENCODER, WIDTH, HEIGHT, FPS, BITRATE),
        # validate the resulting combination against the encoder limits.
        relevant = {"ENCODER", "WIDTH", "HEIGHT", "FPS", "BITRATE"}
        if relevant & set(updates.keys()):
            current = manager.get_config()
            effective = {k: updates.get(k, current.get(k, "")) for k in relevant}
            try:
                br_int = int(str(effective["BITRATE"]).replace("k", "").replace("K", ""))
                w_int = int(effective["WIDTH"])
                h_int = int(effective["HEIGHT"])
                fps_int = int(effective["FPS"])
            except (ValueError, TypeError):
                # Empty/invalid scalar values are already rejected by
                # update_config's _validate_config_value — let that raise.
                pass
            else:
                ok, err = capabilities_helper.validate_combo(
                    effective["ENCODER"], w_int, h_int, fps_int, br_int,
                )
                if not ok:
                    manager._add_log(f"Config rejected: {err}", "warn")
                    return jsonify({"ok": False, "error": err}), 400
        new_config = manager.update_config(updates)
        return jsonify({"ok": True, "config": new_config})
    except ValueError as e:
        manager._add_log(f"Config rejected: {e}", "warn")
        return jsonify({"ok": False, "error": str(e)}), 400
    except PermissionError:
        log.error("Cannot write stream.conf - permission denied")
        return jsonify({"ok": False, "error": "Permission denied writing config. Run: sudo chmod 666 /opt/kevinstream/pi/stream/stream.conf"}), 500
    except Exception as e:
        log.error("Config update failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ── REST: logs ────────────────────────────────────────────────

@app.route("/api/logs")
def get_logs():
    since = request.args.get("since", 0, type=int)
    if since:
        return jsonify(manager.get_logs(since_id=since))
    return jsonify(manager.get_all_logs())


@app.route("/api/logs/clear", methods=["POST"])
def clear_logs():
    manager.clear_logs()
    return jsonify({"ok": True})


# ── REST: system controls ────────────────────────────────────

# ── REST: device detection ────────────────────────────────────

@app.route("/api/devices")
def list_devices():
    """List connected cameras and microphones (force a fresh enumeration)."""
    return jsonify(devices_helper.enumerate_all())


@app.route("/api/capabilities")
def get_capabilities():
    """Return cached encoder/camera capability matrix."""
    return jsonify(capabilities_helper.probe_capabilities(use_cache=True))


@app.route("/api/capabilities/refresh", methods=["POST"])
def refresh_capabilities():
    """Force a fresh capability probe."""
    caps = capabilities_helper.probe_capabilities(use_cache=False)
    _enc_states = ", ".join(
        f"{name}={'available' if ok else 'unavailable'}"
        for name, ok in caps.get("encoders", {}).items()
    )
    manager._add_log(f"Capabilities re-probed: {_enc_states}")
    return jsonify(caps)


@app.route("/api/system/restart-service", methods=["POST"])
def restart_service():
    try:
        subprocess.Popen(
            ["sudo", "systemctl", "restart", "kevinstream"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "message": "Service restarting..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/system/reboot", methods=["POST"])
def reboot_pi():
    try:
        subprocess.Popen(
            ["sudo", "reboot"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "message": "Rebooting..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── REST: network management ─────────────────────────────────

@app.route("/api/network/status")
def network_status():
    return jsonify(wifi_manager.get_network_status())


@app.route("/api/network/wifi/scan")
def wifi_scan():
    # Returns {"networks": [...], "error": None|str} — frontend renders error
    # when present instead of falling back to a generic "no networks found".
    return jsonify(wifi_manager.scan_wifi())


@app.route("/api/network/wifi/connect", methods=["POST"])
def wifi_connect():
    data = request.get_json()
    if not data or "ssid" not in data:
        return jsonify({"ok": False, "error": "Missing SSID"}), 400
    ok, msg = wifi_manager.connect_wifi(data["ssid"], data.get("password", ""))
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/network/wifi/disconnect", methods=["POST"])
def wifi_disconnect():
    ok, msg = wifi_manager.disconnect_wifi()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/network/wifi/saved")
def wifi_saved():
    return jsonify(wifi_manager.get_saved_networks())


@app.route("/api/network/wifi/forget", methods=["POST"])
def wifi_forget():
    data = request.get_json()
    if not data or "ssid" not in data:
        return jsonify({"ok": False, "error": "Missing SSID"}), 400
    ok, msg = wifi_manager.forget_network(data["ssid"])
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/network/ap/enable", methods=["POST"])
def ap_enable():
    ok, msg = wifi_manager.enable_ap()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/network/ap/disable", methods=["POST"])
def ap_disable():
    ok, msg = wifi_manager.disable_ap()
    return jsonify({"ok": ok, "message": msg})


# ── REST: connection check ───────────────────────────────────

@app.route("/api/network/check-target", methods=["POST"])
def check_target():
    """Test connectivity to the stream target host."""
    conf = manager.get_config()
    protocol = conf.get("PROTOCOL", "srt")
    if protocol == "srt":
        host = conf.get("SRT_HOST", "")
        port = conf.get("SRT_PORT", "9000")
    else:
        # Extract host from RTMP URL
        url = conf.get("RTMP_URL", "")
        host = url.replace("rtmp://", "").split("/")[0].split(":")[0] if url else ""
        port = "1935"

    if not host:
        return jsonify({"ok": False, "error": "No target host configured"}), 400

    import socket

    results = {"host": host, "port": int(port), "protocol": protocol}

    # Ping test (3 pings for avg RTT)
    try:
        ping_result = subprocess.run(
            ["ping", "-c", "3", "-W", "3", host],
            capture_output=True, text=True, timeout=12,
        )
        if ping_result.returncode == 0:
            # Parse avg RTT from "min/avg/max/mdev = X/X/X/X ms"
            for line in ping_result.stdout.splitlines():
                if "avg" in line and "/" in line:
                    parts = line.split("=")[-1].strip().split("/")
                    results["ping_min_ms"] = float(parts[0])
                    results["ping_avg_ms"] = float(parts[1])
                    results["ping_max_ms"] = float(parts[2])
            results["ping_ok"] = True
        else:
            results["ping_ok"] = False
    except Exception:
        results["ping_ok"] = False

    # TCP port connectivity test
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        start = time.time()
        sock.connect((host, int(port)))
        results["tcp_ms"] = round((time.time() - start) * 1000, 1)
        results["port_open"] = True
        sock.close()
    except Exception:
        results["port_open"] = False

    results["ok"] = results.get("ping_ok", False) and results.get("port_open", False)
    return jsonify(results)


# ── Health check ──────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "stream": manager.stats["status"]})


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("KevinStream Dashboard starting on %s:%d", config.HOST, config.PORT)
    app.run(host=config.HOST, port=config.PORT, debug=False)
