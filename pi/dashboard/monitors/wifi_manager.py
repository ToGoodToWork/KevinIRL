"""
KevinStream - WiFi & Network Manager
Handles WiFi scanning, connecting, AP fallback, and interface monitoring.
Uses nmcli (NetworkManager) — default on Raspberry Pi OS Bookworm.
"""

import logging
import re
import subprocess
import threading
import time

log = logging.getLogger("wifi_manager")

AP_CON_NAME = "hotspot"
AP_SSID = "KevinIRL"
AP_PASSWORD = "kevinstream"
AP_IP = "192.168.4.1"
FALLBACK_CHECK_INTERVAL = 10  # seconds


def _run(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    """Run a command and return (success, stdout). stderr is logged separately
    so silent failures (rc!=0 with empty stdout) leave a trace in journalctl."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0 or not stdout:
            log.debug("cmd rc=%s: %s — stderr=%r", result.returncode, " ".join(cmd), stderr[:400])
        # Legacy contract: if stdout is empty but stderr has content, return stderr
        # as the "output" so callers can show *something* useful. Failures with
        # ok=False still propagate.
        out = stdout or stderr
        return result.returncode == 0, out
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("Command failed: %s — %s", " ".join(cmd), e)
        return False, ""


def _nmcli(args: list[str], timeout: int = 15) -> tuple[bool, str]:
    """Run an nmcli command."""
    return _run(["nmcli"] + args, timeout=timeout)


# ══════════════════════════════════════════════════════════════
# Interface & Connection Info
# ══════════════════════════════════════════════════════════════

def get_interfaces() -> list[dict]:
    """Get all network interfaces with status."""
    ok, output = _nmcli(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    if not ok:
        return []

    interfaces = []
    for line in output.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue

        dev, dev_type, state, connection = parts[0], parts[1], parts[2], parts[3]

        # Skip loopback and internal
        if dev in ("lo", "p2p-dev-wlan0") or dev_type == "bridge":
            continue

        iface = {
            "name": dev,
            "type": _classify_type(dev, dev_type),
            "connected": state == "connected",
            "connection": connection if connection else None,
            "ip": None,
            "ssid": None,
            "signal": None,
        }

        # Get IP address
        if iface["connected"]:
            iface["ip"] = _get_ip(dev)

        # Get WiFi details
        if dev_type == "wifi" and iface["connected"] and connection != AP_CON_NAME:
            iface["ssid"] = _get_active_ssid(dev)
            iface["signal"] = _get_signal_strength(dev)

        interfaces.append(iface)

    return interfaces


def _classify_type(dev: str, nmcli_type: str) -> str:
    """Classify interface type for display."""
    if nmcli_type == "wifi":
        return "wifi"
    if nmcli_type == "ethernet":
        if dev.startswith("usb") or dev.startswith("enx"):
            return "usb"
        return "ethernet"
    return nmcli_type


def _get_ip(device: str) -> str | None:
    """Get IPv4 address for a device."""
    ok, output = _run(
        ["ip", "-4", "-o", "addr", "show", device]
    )
    if ok and output:
        match = re.search(r"inet\s+([\d.]+)", output)
        if match:
            return match.group(1)
    return None


def _get_active_ssid(device: str = "wlan0") -> str | None:
    """Get currently connected SSID."""
    ok, output = _nmcli(["-t", "-f", "active,ssid", "device", "wifi", "list", "ifname", device])
    if not ok:
        return None
    for line in output.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1]
    return None


def _get_signal_strength(device: str = "wlan0") -> int | None:
    """Get signal strength of current connection (0-100)."""
    ok, output = _nmcli(["-t", "-f", "active,signal", "device", "wifi", "list", "ifname", device])
    if not ok:
        return None
    for line in output.splitlines():
        if line.startswith("yes:"):
            try:
                return int(line.split(":")[1])
            except (ValueError, IndexError):
                pass
    return None


# ══════════════════════════════════════════════════════════════
# WiFi Scanning
# ══════════════════════════════════════════════════════════════

_NMCLI_LIST_ARGS = [
    "-f", "SSID,SIGNAL,SECURITY,ACTIVE",
    "-t", "-e", "no",
    "device", "wifi", "list", "--rescan", "yes",
]
_NMCLI_RESCAN_ARGS = ["device", "wifi", "rescan"]


def _try_nmcli(args: list[str], timeout: int) -> tuple[bool, str]:
    """Run nmcli unprivileged first; if that fails, retry under `sudo -n`.

    -n = non-interactive: sudo fails fast if no password is cached / NOPASSWD
    isn't configured, instead of hanging. This way the service degrades to a
    clear error instead of timing out.
    """
    ok, out = _run(["nmcli"] + args, timeout=timeout)
    if ok:
        return True, out
    ok2, out2 = _run(["sudo", "-n", "nmcli"] + args, timeout=timeout)
    return ok2, (out2 or out)


def scan_wifi() -> dict:
    """Scan for available WiFi networks.

    Returns:
        {"networks": [...sorted by signal...], "error": None | str}

    Failure modes are surfaced to the frontend instead of returning an empty
    list silently. AP mode short-circuits with a clear explanation because
    NetworkManager cannot scan and broadcast on the same radio simultaneously.
    """
    if is_ap_mode():
        msg = "AP mode active — turn off the KevinIRL hotspot to scan for networks."
        log.info("wifi scan skipped: %s", msg)
        return {"networks": [], "error": msg}

    # Trigger a fresh scan. NM rate-limits to ~30s between rescans even with
    # --rescan yes, so back-to-back clicks return cached results — that's a
    # NetworkManager constraint, not a bug.
    rescan_ok, _ = _try_nmcli(_NMCLI_RESCAN_ARGS, timeout=10)
    time.sleep(3)  # NM hardware scan takes 1–3s depending on driver

    list_ok, output = _try_nmcli(_NMCLI_LIST_ARGS, timeout=15)
    log.info("wifi scan: rescan_ok=%s list_ok=%s output_bytes=%d",
             rescan_ok, list_ok, len(output))

    if not list_ok:
        snippet = output.splitlines()[0] if output else "(no output)"
        if "not authorized" in output.lower() or "password" in output.lower():
            err = "Permission denied running nmcli. Re-run update-pi.sh to install the sudoers drop-in."
        else:
            err = f"nmcli failed: {snippet[:160]}"
        return {"networks": [], "error": err}

    networks: dict[str, dict] = {}
    for line in output.splitlines():
        # Format: SSID:SIGNAL:SECURITY:ACTIVE. SSID may contain colons (they're
        # escaped by `-e no`, but defensive rsplit guards anyway).
        parts = line.rsplit(":", 3)
        if len(parts) < 4:
            continue
        ssid = parts[0].strip()
        if not ssid or ssid == "--":
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = parts[2] if parts[2] else "Open"
        active = parts[3].strip() == "yes"

        # Dedup by SSID, keep strongest signal.
        if ssid not in networks or signal > networks[ssid]["signal"]:
            networks[ssid] = {
                "ssid": ssid,
                "signal": signal,
                "security": security,
                "active": active,
                "saved": False,
            }

    saved_ssids = {n["ssid"] for n in get_saved_networks()}
    for net in networks.values():
        net["saved"] = net["ssid"] in saved_ssids

    sorted_nets = sorted(networks.values(), key=lambda n: n["signal"], reverse=True)
    log.info("wifi scan: parsed %d networks", len(sorted_nets))
    return {"networks": sorted_nets, "error": None}


# ══════════════════════════════════════════════════════════════
# WiFi Connect / Disconnect
# ══════════════════════════════════════════════════════════════

def connect_wifi(ssid: str, password: str = "") -> tuple[bool, str]:
    """Connect to a WiFi network."""
    if not ssid:
        log.warning("connect_wifi called with empty SSID")
        return False, "No SSID provided"

    log.info("Connecting to WiFi: '%s' (password: %s)", ssid, "yes" if password else "no")

    # If AP is active on wlan0, disable it first
    if is_ap_mode():
        log.info("Disabling AP mode to connect to WiFi...")
        disable_ap()
        time.sleep(2)

    # Try connecting (use sudo for privilege)
    cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]

    log.info("Running: %s", " ".join(cmd))
    ok, output = _run(cmd, timeout=30)

    if ok:
        log.info("Connected to WiFi: '%s'", ssid)
        return True, f"Connected to {ssid}"
    else:
        log.warning("Failed to connect to '%s': %s", ssid, output)
        return False, output or f"Failed to connect to {ssid}"


def disconnect_wifi() -> tuple[bool, str]:
    """Disconnect from current WiFi."""
    ok, output = _nmcli(["device", "disconnect", "wlan0"])
    if ok:
        log.info("WiFi disconnected")
        return True, "Disconnected"
    return False, output or "Failed to disconnect"


# ══════════════════════════════════════════════════════════════
# Saved Networks
# ══════════════════════════════════════════════════════════════

def get_saved_networks() -> list[dict]:
    """List saved/known WiFi networks."""
    ok, output = _nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"])
    if not ok:
        return []

    networks = []
    for line in output.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "802-11-wireless":
            name = parts[0]
            if name == AP_CON_NAME:
                continue  # Skip our hotspot
            networks.append({"ssid": name})

    return networks


def forget_network(ssid: str) -> tuple[bool, str]:
    """Remove a saved WiFi network."""
    ok, output = _nmcli(["connection", "delete", ssid])
    if ok:
        log.info("Forgot network: %s", ssid)
        return True, f"Forgot {ssid}"
    return False, output or f"Failed to forget {ssid}"


# ══════════════════════════════════════════════════════════════
# AP Mode (Hotspot)
# ══════════════════════════════════════════════════════════════

def is_ap_mode() -> bool:
    """Check if the hotspot AP is currently active."""
    ok, output = _nmcli(["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"])
    if not ok:
        return False
    for line in output.splitlines():
        if line.startswith(f"{AP_CON_NAME}:"):
            return True
    return False


def _ensure_ap_connection_exists():
    """Create the hotspot connection profile if it doesn't exist."""
    ok, _ = _nmcli(["-t", "connection", "show", AP_CON_NAME])
    if ok:
        return  # Already exists

    log.info("Creating AP connection profile '%s'...", AP_CON_NAME)
    _nmcli([
        "con", "add",
        "con-name", AP_CON_NAME,
        "ifname", "wlan0",
        "type", "wifi",
        "ssid", AP_SSID,
        "autoconnect", "no",
    ])
    _nmcli([
        "con", "modify", AP_CON_NAME,
        "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg",
        "802-11-wireless.channel", "6",
        "ipv4.method", "shared",
        "ipv4.addresses", f"{AP_IP}/24",
        "ipv6.method", "disabled",
    ])
    _nmcli([
        "con", "modify", AP_CON_NAME,
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", AP_PASSWORD,
    ])
    log.info("AP connection profile created")


def enable_ap() -> tuple[bool, str]:
    """Enable the WiFi access point."""
    _ensure_ap_connection_exists()

    # Disconnect any active WiFi first
    _nmcli(["device", "disconnect", "wlan0"])
    time.sleep(1)

    ok, output = _nmcli(["con", "up", AP_CON_NAME])
    if ok:
        log.info("AP mode enabled (SSID: %s)", AP_SSID)
        return True, f"AP enabled: {AP_SSID}"
    log.warning("Failed to enable AP: %s", output)
    return False, output or "Failed to enable AP"


def disable_ap() -> tuple[bool, str]:
    """Disable the WiFi access point."""
    ok, output = _nmcli(["con", "down", AP_CON_NAME])
    if ok:
        log.info("AP mode disabled")
        return True, "AP disabled"
    return False, output or "Failed to disable AP"


# ══════════════════════════════════════════════════════════════
# Internet Check
# ══════════════════════════════════════════════════════════════

def has_internet() -> bool:
    """Quick internet connectivity check."""
    ok, _ = _run(["ping", "-c", "1", "-W", "3", "1.1.1.1"])
    return ok


# ══════════════════════════════════════════════════════════════
# Network Status (for WebSocket)
# ══════════════════════════════════════════════════════════════

_cached_status = {}
_status_lock = threading.Lock()


def get_network_status() -> dict:
    """Get full network status for the dashboard."""
    with _status_lock:
        return dict(_cached_status) if _cached_status else _build_status()


def _build_status() -> dict:
    """Build current network status."""
    interfaces = get_interfaces()
    ap = is_ap_mode()
    internet = has_internet()

    return {
        "interfaces": interfaces,
        "ap_mode": ap,
        "ap_ssid": AP_SSID if ap else None,
        "ap_ip": AP_IP if ap else None,
        "internet": internet,
    }


# ══════════════════════════════════════════════════════════════
# Background Watchdog — Auto AP Fallback
# ══════════════════════════════════════════════════════════════

_watchdog_running = False


def start_watchdog():
    """Start the background connectivity watchdog."""
    global _watchdog_running
    if _watchdog_running:
        return
    _watchdog_running = True
    t = threading.Thread(target=_watchdog_loop, daemon=True)
    t.start()
    log.info("Network watchdog started")


def _watchdog_loop():
    """Monitor connectivity and manage AP fallback."""
    global _cached_status
    consecutive_failures = 0

    _ensure_ap_connection_exists()

    while True:
        try:
            status = _build_status()
            with _status_lock:
                _cached_status = status

            internet = status["internet"]
            ap_active = status["ap_mode"]

            # Check if any non-wifi interface has connectivity
            has_wired = any(
                i["connected"] and i["type"] in ("ethernet", "usb")
                for i in status["interfaces"]
            )

            if internet:
                consecutive_failures = 0
                # If AP is on but we have internet via wired, disable AP
                if ap_active and has_wired:
                    log.info("Internet restored via wired, disabling AP...")
                    disable_ap()
            else:
                consecutive_failures += 1
                # After 3 consecutive failures (30s), enable AP
                if consecutive_failures >= 3 and not ap_active:
                    log.warning("No internet for %ds, enabling AP fallback...",
                                consecutive_failures * FALLBACK_CHECK_INTERVAL)
                    enable_ap()

        except Exception as e:
            log.error("Watchdog error: %s", e)

        time.sleep(FALLBACK_CHECK_INTERVAL)
