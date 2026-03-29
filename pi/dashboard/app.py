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

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_sock import Sock

import config
from monitors import system_monitor, stream_monitor, network_monitor

# Add stream directory to path for stream_manager import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "stream"))
from stream_manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("dashboard")

app = Flask(__name__, static_folder="static", static_url_path="/static")
sock = Sock(app)

# Initialize CPU percent tracking (first call always returns 0)
import psutil
psutil.cpu_percent(interval=None)


# ── Static / SPA ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ── WebSocket: real-time stats ────────────────────────────────

@sock.route("/ws/stats")
def ws_stats(ws):
    """Push system + stream stats + logs to connected clients."""
    log.info("WebSocket client connected")
    last_log_id = 0
    try:
        while True:
            sys_stats = system_monitor.get_stats()
            strm_stats = stream_monitor.get_stats()
            net_stats = network_monitor.get_stats()

            # Get new log lines since last push
            new_logs = manager.get_logs(since_id=last_log_id)
            if new_logs:
                last_log_id = new_logs[-1]["id"]

            payload = {
                "system": sys_stats,
                **strm_stats,
                "connectivity": net_stats,
                "logs": new_logs,
                "timestamp": time.time(),
            }
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
    return jsonify({"ok": ok})


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


@app.route("/api/stream/config", methods=["GET"])
def stream_config_get():
    return jsonify(manager.get_config())


@app.route("/api/stream/config", methods=["PUT"])
def stream_config_update():
    updates = request.get_json()
    if not updates:
        return jsonify({"ok": False, "error": "No data"}), 400
    new_config = manager.update_config(updates)
    return jsonify({"ok": True, "config": new_config})


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


# ── REST: camera snapshot ─────────────────────────────────────

@app.route("/api/snapshot")
def snapshot():
    """Capture a single JPEG frame from the webcam."""
    conf = manager.get_config()
    device = conf.get("VIDEO_DEVICE", "/dev/video0")

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "v4l2",
                "-input_format", "mjpeg",
                "-video_size", "640x480",
                "-frames:v", "1",
                "-i", device,
                "-f", "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return Response(result.stdout, mimetype="image/jpeg")
        return jsonify({"error": "Failed to capture snapshot"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Snapshot timeout"}), 500
    except FileNotFoundError:
        return jsonify({"error": "ffmpeg not found"}), 500


# ── REST: system controls ────────────────────────────────────

@app.route("/api/system/restart-service", methods=["POST"])
def restart_service():
    """Restart the kevinstream systemd service."""
    try:
        subprocess.Popen(
            ["sudo", "systemctl", "restart", "kevinstream"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "message": "Service restarting..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/system/reboot", methods=["POST"])
def reboot_pi():
    """Reboot the Raspberry Pi."""
    try:
        subprocess.Popen(
            ["sudo", "reboot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "message": "Rebooting..."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Health check ──────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "stream": manager.stats["status"]})


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("KevinStream Dashboard starting on %s:%d", config.HOST, config.PORT)
    app.run(host=config.HOST, port=config.PORT, debug=False)
