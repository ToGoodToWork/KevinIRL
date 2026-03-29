"""
KevinStream - Dashboard Server
Flask app serving the dashboard UI and providing WebSocket/REST APIs.
"""

import json
import logging
import os
import sys
import time
import threading

from flask import Flask, jsonify, request, send_from_directory
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
    """Push system + stream stats to connected clients."""
    log.info("WebSocket client connected")
    try:
        while True:
            sys_stats = system_monitor.get_stats()
            strm_stats = stream_monitor.get_stats()
            net_stats = network_monitor.get_stats()

            payload = {
                "system": sys_stats,
                **strm_stats,  # includes "stream" and "network" keys
                "connectivity": net_stats,
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


# ── Health check ──────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "stream": manager.stats["status"]})


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("KevinStream Dashboard starting on %s:%d", config.HOST, config.PORT)
    app.run(host=config.HOST, port=config.PORT, debug=False)
