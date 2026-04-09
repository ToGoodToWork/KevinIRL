// KevinStream Dashboard V2

// ── State ──
let statsWs = null;
let obsWs = null;
let obsConnected = false;
let currentScene = "";
let obsRecording = false;
let obsStreaming = false;

const $ = (id) => document.getElementById(id);
const MAX_LOG_LINES = 200;

// ══════════════════════════════════════════════════════════════
// WebSocket: Pi Stats
// ══════════════════════════════════════════════════════════════

function connectStats() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    statsWs = new WebSocket(`${proto}//${location.host}/ws/stats`);

    statsWs.onopen = () => {
        $("wsStatus").textContent = "Dashboard connected";
        $("wsStatus").className = "ws-status connected";
    };

    statsWs.onmessage = (e) => {
        const data = JSON.parse(e.data);
        // Backend sends "stream_network" for SRT stats, "network" for wifi_manager status
        updateSystemUI(data);
        updateStreamUI(data);
        updateNetworkUI(data);
        if (data.logs && data.logs.length > 0) {
            appendLogs(data.logs);
        }
    };

    statsWs.onclose = () => {
        $("wsStatus").textContent = "Disconnected - reconnecting...";
        $("wsStatus").className = "ws-status disconnected";
        setTimeout(connectStats, 3000);
    };

    statsWs.onerror = () => statsWs.close();
}

// ══════════════════════════════════════════════════════════════
// UI Updates
// ══════════════════════════════════════════════════════════════

function updateSystemUI(data) {
    if (!data.system) return;

    const cpu = data.system.cpu_percent ?? 0;
    $("cpuPercent").textContent = `${cpu.toFixed(1)}%`;
    setBar("cpuBar", cpu);

    if (data.system.ram) {
        const ram = data.system.ram;
        $("ramValue").textContent = `${ram.used_mb} / ${ram.total_mb} MB`;
        setBar("ramBar", ram.percent);
    }

    const temp = data.system.temperature_c;
    if (temp != null) {
        $("tempValue").textContent = `${temp.toFixed(1)}\u00B0C`;
        const pct = Math.min(100, Math.max(0, ((temp - 30) / 55) * 100));
        setBar("tempBar", pct);
    }
}

function updateStreamUI(data) {
    if (!data.stream) return;

    const status = data.stream.status;
    const dot = $("streamDot");
    const label = $("streamStatus");

    if (status === "live") {
        dot.className = "status-dot live";
        label.textContent = "Live";
        $("btnStart").disabled = true;
        $("btnStop").disabled = false;
        $("btnRestart").disabled = false;
        if (data.stream.uptime_seconds) {
            $("streamUptime").textContent = formatDuration(data.stream.uptime_seconds);
        }
    } else {
        dot.className = status === "error" ? "status-dot error" : "status-dot";
        label.textContent = status === "error" ? "Error" : "Offline";
        $("btnStart").disabled = false;
        $("btnStop").disabled = true;
        $("btnRestart").disabled = true;
        $("streamUptime").textContent = "";
    }
}

function updateNetworkUI(data) {
    // Stream SRT stats
    if (data.stream_network) {
        $("bitrateValue").textContent = `${Math.round(data.stream_network.srt_bitrate_kbps)} kbps`;
        $("rttValue").textContent = `${data.stream_network.srt_rtt_ms.toFixed(1)} ms`;
        $("packetLoss").textContent = `${data.stream_network.srt_packet_loss_percent.toFixed(2)}%`;
    }

    // Network manager status (from wifi_manager)
    if (data.network) {
        updateNetManagerUI(data.network);
    }
}

function updateNetManagerUI(net) {
    // Internet badge
    const badge = $("netInternetBadge");
    if (net.internet) {
        badge.textContent = "Online";
        badge.className = "net-internet-badge online";
    } else {
        badge.textContent = "Offline";
        badge.className = "net-internet-badge offline";
    }

    // Interfaces
    const container = $("netInterfaces");
    container.innerHTML = "";
    if (net.interfaces) {
        net.interfaces.forEach((iface) => {
            const div = document.createElement("div");
            div.className = "net-iface" + (iface.connected ? " connected" : "");

            const icon = iface.type === "wifi" ? "\uD83D\uDCF6" : iface.type === "usb" ? "\uD83D\uDD0C" : "\uD83D\uDD17";
            const label = iface.type === "wifi" && iface.ssid ? iface.ssid : iface.name;
            const status = iface.connected ? (iface.ip || "connected") : "disconnected";
            const signal = iface.type === "wifi" && iface.signal != null ? ` (${iface.signal}%)` : "";

            div.innerHTML = `<span class="net-iface-icon">${icon}</span>
                <span class="net-iface-label">${escapeHtml(label)}</span>
                <span class="net-iface-status">${status}${signal}</span>`;
            container.appendChild(div);
        });
    }

    // AP mode bar
    const apBar = $("netApBar");
    if (net.ap_mode) {
        apBar.style.display = "flex";
        $("netApSsid").textContent = net.ap_ssid || "KevinIRL";
    } else {
        apBar.style.display = "none";
    }
}

function setBar(id, percent) {
    const el = $(id);
    el.style.width = `${Math.min(100, percent)}%`;
    el.className = "progress-fill " + (percent > 85 ? "danger" : percent > 65 ? "warn" : "ok");
}

function formatDuration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

// ══════════════════════════════════════════════════════════════
// Logs
// ══════════════════════════════════════════════════════════════

function appendLogs(entries) {
    const terminal = $("logTerminal");

    // Remove placeholder on first log
    if (terminal.children.length === 1 && terminal.children[0].classList?.contains("dim")) {
        terminal.innerHTML = "";
    }

    entries.forEach((entry) => {
        const div = document.createElement("div");
        div.className = "log-line";
        const cls = entry.level === "error" ? "log-error" : entry.level === "warn" ? "log-warn" : "log-info";
        div.innerHTML = `<span class="log-time">${entry.time}</span> <span class="${cls}">${escapeHtml(entry.text)}</span>`;
        terminal.appendChild(div);
    });

    while (terminal.children.length > MAX_LOG_LINES) {
        terminal.removeChild(terminal.firstChild);
    }

    terminal.scrollTop = terminal.scrollHeight;
}

function clearLogs() {
    $("logTerminal").innerHTML = '<div class="dim small">Logs cleared</div>';
    fetch("/api/logs/clear", { method: "POST" });
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// ══════════════════════════════════════════════════════════════
// Stream Control
// ══════════════════════════════════════════════════════════════

async function streamControl(action) {
    try {
        const res = await fetch(`/api/stream/${action}`, { method: "POST" });
        const data = await res.json();
        if (!data.ok) console.warn(`Stream ${action} failed:`, data.error);
    } catch (e) {
        console.error(`Stream ${action} error:`, e);
    }
}

// ══════════════════════════════════════════════════════════════
// Settings & Protocol Switcher
// ══════════════════════════════════════════════════════════════

function onProtocolChange() {
    const proto = $("settingProtocol").value;
    $("rtmpSettings").style.display = proto === "rtmp" ? "block" : "none";
    $("srtSettings").style.display = proto === "srt" ? "block" : "none";
}

async function loadConfig() {
    try {
        const res = await fetch("/api/stream/config");
        const cfg = await res.json();

        if (cfg.PROTOCOL) {
            $("settingProtocol").value = cfg.PROTOCOL;
            onProtocolChange();
        }
        if (cfg.RTMP_URL) $("settingRtmpUrl").value = cfg.RTMP_URL;
        if (cfg.SRT_HOST) $("settingSrtHost").value = cfg.SRT_HOST;
        if (cfg.SRT_PORT) $("settingSrtPort").value = cfg.SRT_PORT;
        if (cfg.SRT_PASSPHRASE) $("settingSrtPass").value = cfg.SRT_PASSPHRASE;
        if (cfg.SRT_LATENCY) $("settingLatency").value = cfg.SRT_LATENCY;
        if (cfg.BITRATE) $("settingBitrate").value = cfg.BITRATE;
        if (cfg.WIDTH && cfg.HEIGHT) {
            const r = `${cfg.WIDTH}x${cfg.HEIGHT}`;
            if ([...$("settingResolution").options].find(o => o.value === r)) {
                $("settingResolution").value = r;
            }
        }

        // Store current device values, then detect devices to populate dropdowns
        window._loadedVideoDevice = cfg.VIDEO_DEVICE || "/dev/video0";
        window._loadedAudioDevice = cfg.AUDIO_DEVICE || "none";
        detectDevices();
    } catch (e) {
        console.error("Failed to load config:", e);
    }
}

// ══════════════════════════════════════════════════════════════
// Device Detection
// ══════════════════════════════════════════════════════════════

async function detectDevices() {
    try {
        const res = await fetch("/api/devices");
        const devices = await res.json();

        // Populate cameras
        const camSelect = $("settingCamera");
        camSelect.innerHTML = "";
        if (devices.cameras.length === 0) {
            camSelect.innerHTML = '<option value="/dev/video0">No cameras found</option>';
        } else {
            devices.cameras.forEach((cam) => {
                const opt = document.createElement("option");
                opt.value = cam.device;
                opt.textContent = `${cam.name} (${cam.device})`;
                if (cam.resolutions && cam.resolutions.length > 0) {
                    opt.dataset.resolutions = JSON.stringify(cam.resolutions);
                }
                camSelect.appendChild(opt);
            });
        }

        // Select current camera from config
        if (window._loadedVideoDevice) {
            const match = [...camSelect.options].find(o => o.value === window._loadedVideoDevice);
            if (match) camSelect.value = window._loadedVideoDevice;
        }

        // Update resolution options based on selected camera
        updateResolutionsForCamera();

        // Populate microphones
        const micSelect = $("settingAudio");
        micSelect.innerHTML = '<option value="none">Disabled</option>';
        devices.microphones.forEach((mic) => {
            const opt = document.createElement("option");
            opt.value = mic.device;
            opt.textContent = mic.name;
            micSelect.appendChild(opt);
        });

        // Select current audio device from config
        if (window._loadedAudioDevice) {
            const match = [...micSelect.options].find(o => o.value === window._loadedAudioDevice);
            if (match) micSelect.value = window._loadedAudioDevice;
        }
    } catch (e) {
        console.error("Device detection failed:", e);
    }
}

function updateResolutionsForCamera() {
    const camSelect = $("settingCamera");
    const resSelect = $("settingResolution");
    const selected = camSelect.options[camSelect.selectedIndex];
    if (!selected || !selected.dataset.resolutions) return;

    const resolutions = JSON.parse(selected.dataset.resolutions);
    const currentRes = resSelect.value;
    resSelect.innerHTML = "";

    // Map resolutions to friendly names
    const names = { "1920x1080": "1080p", "1280x720": "720p", "640x480": "480p", "800x600": "600p", "320x240": "240p", "1024x576": "576p" };

    resolutions.forEach((res) => {
        const opt = document.createElement("option");
        opt.value = res;
        opt.textContent = names[res] || res;
        resSelect.appendChild(opt);
    });

    // Re-select previous resolution if available
    if ([...resSelect.options].find(o => o.value === currentRes)) {
        resSelect.value = currentRes;
    }
}

async function saveSettings() {
    const proto = $("settingProtocol").value;
    const resolution = $("settingResolution").value.split("x");

    const updates = {
        PROTOCOL: proto,
        BITRATE: $("settingBitrate").value,
        WIDTH: resolution[0],
        HEIGHT: resolution[1],
        VIDEO_DEVICE: $("settingCamera").value,
        AUDIO_DEVICE: $("settingAudio").value,
    };

    if (proto === "rtmp") {
        updates.RTMP_URL = $("settingRtmpUrl").value;
    } else {
        updates.SRT_HOST = $("settingSrtHost").value;
        updates.SRT_PORT = $("settingSrtPort").value;
        updates.SRT_PASSPHRASE = $("settingSrtPass").value;
        updates.SRT_LATENCY = $("settingLatency").value;
    }

    try {
        const res = await fetch("/api/stream/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updates),
        });
        const data = await res.json();
        if (data.ok) {
            streamControl("restart");
        }
    } catch (e) {
        console.error("Save settings error:", e);
    }
}

// ══════════════════════════════════════════════════════════════
// System Controls
// ══════════════════════════════════════════════════════════════

async function restartService() {
    if (!confirm("Restart the KevinStream service? Dashboard will briefly disconnect.")) return;
    try { await fetch("/api/system/restart-service", { method: "POST" }); } catch (e) {}
}

async function rebootPi() {
    if (!confirm("Reboot the Raspberry Pi? All streams will stop.")) return;
    if (!confirm("Are you sure?")) return;
    try { await fetch("/api/system/reboot", { method: "POST" }); } catch (e) {}
}

// ══════════════════════════════════════════════════════════════
// OBS WebSocket Control (v5 protocol, no library)
// ══════════════════════════════════════════════════════════════

function obsConnect() {
    const host = $("obsHost").value.trim();
    const password = $("obsPassword").value;
    if (!host) return;

    saveObsSettings();
    $("obsStatus").textContent = "Connecting...";
    $("obsStatus").className = "ws-status";

    obsWs = new WebSocket(`ws://${host}:4455`);
    obsWs.onopen = () => {};

    obsWs.onmessage = (e) => {
        handleObsMessage(JSON.parse(e.data), password);
    };

    obsWs.onclose = () => {
        obsConnected = false;
        $("obsStatus").textContent = "Disconnected";
        $("obsStatus").className = "ws-status disconnected";
        $("obsControls").style.display = "none";
    };

    obsWs.onerror = () => {
        $("obsStatus").textContent = "Connection failed";
        $("obsStatus").className = "ws-status disconnected";
    };
}

async function handleObsMessage(msg, password) {
    const op = msg.op;

    if (op === 0) {
        const auth = msg.d.authentication;
        const identify = { rpcVersion: 1, eventSubscriptions: 0xFFFF };
        if (auth) {
            const secret = await sha256(password + auth.salt);
            identify.authentication = await sha256(secret + auth.challenge);
        }
        obsWs.send(JSON.stringify({ op: 1, d: identify }));
    }

    if (op === 2) {
        obsConnected = true;
        $("obsStatus").textContent = "Connected to OBS";
        $("obsStatus").className = "ws-status connected";
        $("obsControls").style.display = "block";
        $("btnObsConnect").textContent = "Disconnect";
        $("btnObsConnect").onclick = obsDisconnect;
        obsSend("GetSceneList");
        obsSend("GetStreamStatus");
        obsSend("GetRecordStatus");
    }

    if (op === 7) {
        const type = msg.d.requestType;
        const data = msg.d.responseData;
        if (type === "GetSceneList" && data) renderScenes(data.scenes, data.currentProgramSceneName);
        if (type === "GetStreamStatus" && data) { obsStreaming = data.outputActive; updateObsInfo(); }
        if (type === "GetRecordStatus" && data) { obsRecording = data.outputActive; updateObsInfo(); }
    }

    if (op === 5) {
        const evt = msg.d.eventType;
        const ed = msg.d.eventData || {};
        if (evt === "CurrentProgramSceneChanged") { currentScene = ed.sceneName; highlightScene(currentScene); }
        if (evt === "StreamStateChanged") { obsStreaming = ed.outputActive; updateObsInfo(); }
        if (evt === "RecordStateChanged") { obsRecording = ed.outputActive; updateObsInfo(); }
    }
}

function updateObsInfo() {
    const el = $("obsStreamInfo");
    const parts = [];
    if (obsStreaming) parts.push('<span class="live">STREAMING</span>');
    if (obsRecording) parts.push('<span class="rec">REC</span>');
    if (!parts.length) parts.push("Idle");
    el.innerHTML = parts.join(" &middot; ");
}

let obsRequestId = 0;

function obsSend(requestType, requestData) {
    if (!obsWs || !obsConnected) return;
    obsRequestId++;
    obsWs.send(JSON.stringify({
        op: 6,
        d: { requestType, requestId: String(obsRequestId), ...(requestData ? { requestData } : {}) }
    }));
}

function obsAction(action) { obsSend(action); }

function obsDisconnect() {
    if (obsWs) obsWs.close();
    $("btnObsConnect").textContent = "Connect";
    $("btnObsConnect").onclick = obsConnect;
}

function renderScenes(scenes, current) {
    currentScene = current;
    const container = $("obsScenes");
    container.innerHTML = "";
    [...scenes].reverse().forEach((scene) => {
        const div = document.createElement("div");
        div.className = "scene-item" + (scene.sceneName === current ? " active" : "");
        div.textContent = scene.sceneName;
        div.dataset.name = scene.sceneName;
        div.onclick = () => obsSend("SetCurrentProgramScene", { sceneName: scene.sceneName });
        container.appendChild(div);
    });
}

function highlightScene(name) {
    document.querySelectorAll(".scene-item").forEach((el) => {
        el.classList.toggle("active", el.dataset.name === name);
    });
}

async function sha256(message) {
    const data = new TextEncoder().encode(message);
    const hash = await crypto.subtle.digest("SHA-256", data);
    return btoa(String.fromCharCode(...new Uint8Array(hash)));
}

function loadObsSettings() {
    const host = localStorage.getItem("obs_host");
    const pwd = localStorage.getItem("obs_password");
    if (host) $("obsHost").value = host;
    if (pwd) $("obsPassword").value = pwd;
}

function saveObsSettings() {
    localStorage.setItem("obs_host", $("obsHost").value);
    localStorage.setItem("obs_password", $("obsPassword").value);
}

// ══════════════════════════════════════════════════════════════
// WiFi Browser
// ══════════════════════════════════════════════════════════════

let pendingWifiSsid = "";

async function wifiScan() {
    const btn = $("btnWifiScan");
    const list = $("wifiList");
    btn.disabled = true;
    btn.textContent = "Scanning...";
    list.innerHTML = '<div class="dim small">Scanning...</div>';

    try {
        const res = await fetch("/api/network/wifi/scan");
        const networks = await res.json();

        list.innerHTML = "";
        if (!networks.length) {
            list.innerHTML = '<div class="dim small">No networks found</div>';
            return;
        }

        networks.forEach((net) => {
            const div = document.createElement("div");
            div.className = "wifi-item" + (net.active ? " active" : "");
            const bars = signalBars(net.signal);
            const lock = net.security && net.security !== "Open" && net.security !== "--" ? "\uD83D\uDD12" : "";
            const saved = net.saved ? '<span class="wifi-saved">saved</span>' : "";

            div.innerHTML = `<div class="wifi-item-info">
                    <span class="wifi-ssid">${escapeHtml(net.ssid)}</span>
                    ${saved} ${lock}
                </div>
                <div class="wifi-item-right">
                    <span class="wifi-signal">${bars}</span>
                    <span class="wifi-pct">${net.signal}%</span>
                </div>`;

            if (net.active) {
                const dcBtn = document.createElement("button");
                dcBtn.className = "btn btn-sm btn-stop";
                dcBtn.textContent = "Disconnect";
                dcBtn.style.marginLeft = "8px";
                dcBtn.onclick = (e) => { e.stopPropagation(); wifiDisconnect(); };
                div.querySelector(".wifi-item-right").appendChild(dcBtn);
            } else {
                div.onclick = () => openWifiModal(net.ssid, net.security, net.saved);
            }

            list.appendChild(div);
        });
    } catch (e) {
        list.innerHTML = '<div class="dim small">Scan failed</div>';
        console.error("WiFi scan error:", e);
    } finally {
        btn.disabled = false;
        btn.textContent = "Scan";
    }
}

function signalBars(signal) {
    const bars = Math.ceil(signal / 25);
    let html = "";
    for (let i = 1; i <= 4; i++) {
        const h = 4 + i * 3;
        const active = i <= bars;
        html += `<span class="signal-bar${active ? " active" : ""}" style="height:${h}px"></span>`;
    }
    return `<span class="signal-bars">${html}</span>`;
}

function openWifiModal(ssid, security, saved) {
    pendingWifiSsid = ssid;
    $("wifiModalTitle").textContent = `Connect to "${ssid}"`;
    $("wifiModalPassword").value = "";
    $("wifiModalStatus").textContent = "";

    const needsPassword = security && security !== "Open" && security !== "--" && !saved;
    $("wifiModalPassword").parentElement.style.display = needsPassword ? "flex" : "none";

    if (saved) {
        // Saved network — connect directly without modal
        wifiConnect(ssid, "");
        return;
    }

    $("wifiModal").style.display = "flex";
    if (needsPassword) $("wifiModalPassword").focus();
}

function closeWifiModal() {
    $("wifiModal").style.display = "none";
    pendingWifiSsid = "";
}

async function wifiConnectConfirm() {
    const ssid = pendingWifiSsid;
    const password = $("wifiModalPassword").value;
    closeWifiModal();
    await wifiConnect(ssid, password);
}

async function wifiConnect(ssid, password) {
    const list = $("wifiList");
    list.innerHTML = `<div class="dim small">Connecting to ${escapeHtml(ssid)}...</div>`;

    try {
        const res = await fetch("/api/network/wifi/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ssid, password }),
        });
        const data = await res.json();
        if (data.ok) {
            list.innerHTML = `<div class="dim small" style="color: var(--green);">Connected to ${escapeHtml(ssid)}</div>`;
        } else {
            list.innerHTML = `<div class="dim small" style="color: var(--red);">Failed: ${escapeHtml(data.message)}</div>`;
        }
        // Refresh scan after a moment
        setTimeout(wifiScan, 3000);
    } catch (e) {
        list.innerHTML = '<div class="dim small" style="color: var(--red);">Connection error</div>';
    }
}

async function wifiDisconnect() {
    try {
        await fetch("/api/network/wifi/disconnect", { method: "POST" });
        setTimeout(wifiScan, 2000);
    } catch (e) {
        console.error("WiFi disconnect error:", e);
    }
}

async function apDisable() {
    try {
        await fetch("/api/network/ap/disable", { method: "POST" });
    } catch (e) {}
}

// ══════════════════════════════════════════════════════════════
// Init
// ══════════════════════════════════════════════════════════════

loadObsSettings();
loadConfig();
connectStats();
