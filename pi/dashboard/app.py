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
from monitors import system_monitor, stream_monitor, network_monitor
from monitors import wifi_manager

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

# Start network watchdog (auto AP fallback)
wifi_manager.start_watchdog()


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

            payload = {
                "system": sys_stats,
                **strm_stats,
                "connectivity": net_stats,
                "network": net_status,
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
    try:
        new_config = manager.update_config(updates)
        return jsonify({"ok": True, "config": new_config})
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
    """List connected cameras and microphones."""
    import re as _re
    devices = {"cameras": [], "microphones": []}

    # Cameras: parse v4l2 devices (Linux only)
    # Skip Pi internal hardware nodes (codecs, ISP, decoders) — they're not real cameras
    _SKIP_DEVICES = {"bcm2835-codec", "bcm2835-isp", "rpi-hevc", "rpivid"}

    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            current_name = ""
            skip_group = False
            for line in result.stdout.splitlines():
                line = line.rstrip()
                if not line:
                    continue
                if not line.startswith("\t") and not line.startswith(" "):
                    current_name = line.rstrip(":")
                    # Check if this device group is an internal Pi node
                    name_lower = current_name.lower()
                    skip_group = any(s in name_lower for s in _SKIP_DEVICES)
                elif "/dev/video" in line and not skip_group:
                    dev = line.strip()
                    try:
                        fmt_result = subprocess.run(
                            ["v4l2-ctl", "-d", dev, "--list-formats-ext"],
                            capture_output=True, text=True, timeout=3,
                        )
                        output = fmt_result.stdout
                        if "Video Capture" in output or "mjpeg" in output.lower() or "yuyv" in output.lower():
                            # Parse resolutions and their framerates
                            # Format: "Size: Discrete WxH" followed by "Interval: Discrete Xs (Yfps)"
                            res_fps = {}  # "WxH" -> [fps1, fps2, ...]
                            current_res = None
                            for fmt_line in output.splitlines():
                                fmt_line = fmt_line.strip()
                                if "Size:" in fmt_line and "x" in fmt_line:
                                    for p in fmt_line.split():
                                        if "x" in p and p[0].isdigit():
                                            current_res = p
                                            if current_res not in res_fps:
                                                res_fps[current_res] = []
                                elif "fps" in fmt_line and current_res:
                                    # Parse "Interval: Discrete 0.033s (30.000 fps)"
                                    fps_match = _re.search(r"([\d.]+)\s*fps", fmt_line)
                                    if fps_match:
                                        fps_val = float(fps_match.group(1))
                                        if fps_val not in res_fps[current_res]:
                                            res_fps[current_res].append(fps_val)

                            resolutions = sorted(res_fps.keys(),
                                key=lambda r: int(r.split("x")[0]), reverse=True)
                            devices["cameras"].append({
                                "device": dev,
                                "name": current_name,
                                "resolutions": resolutions,
                                "fps_by_resolution": {r: sorted(f, reverse=True) for r, f in res_fps.items()},
                            })
                    except Exception:
                        pass
    except Exception:
        pass

    # Microphones: parse ALSA capture devices
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                m = _re.match(r"card (\d+):.*\[(.+?)\].*device (\d+):.*\[(.+?)\]", line)
                if m:
                    card, card_name, device, dev_name = m.groups()
                    devices["microphones"].append({
                        "device": f"plughw:{card},{device}",
                        "name": f"{card_name} - {dev_name}",
                        "card": int(card),
                    })
    except Exception:
        pass

    return jsonify(devices)


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
    networks = wifi_manager.scan_wifi()
    return jsonify(networks)


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
