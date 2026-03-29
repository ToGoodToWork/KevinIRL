// KevinStream Dashboard - Main JavaScript

// ── State ──
let statsWs = null;
let obsWs = null;
let obsConnected = false;
let currentScene = "";

// ── DOM refs ──
const $ = (id) => document.getElementById(id);

// ── WebSocket: Pi Stats ──

function connectStats() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    statsWs = new WebSocket(`${proto}//${location.host}/ws/stats`);

    statsWs.onopen = () => {
        $("wsStatus").textContent = "Dashboard connected";
        $("wsStatus").className = "ws-status connected";
    };

    statsWs.onmessage = (e) => {
        const data = JSON.parse(e.data);
        updateUI(data);
    };

    statsWs.onclose = () => {
        $("wsStatus").textContent = "Dashboard disconnected - reconnecting...";
        $("wsStatus").className = "ws-status disconnected";
        setTimeout(connectStats, 3000);
    };

    statsWs.onerror = () => statsWs.close();
}

// ── UI Updates ──

function updateUI(data) {
    // System stats
    if (data.system) {
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
            $("tempValue").textContent = `${temp.toFixed(1)}°C`;
            // Map 30-85°C to 0-100%
            const pct = Math.min(100, Math.max(0, ((temp - 30) / 55) * 100));
            setBar("tempBar", pct);
        }
    }

    // Stream status
    if (data.stream) {
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
            dot.className = "status-dot";
            label.textContent = status === "error" ? "Error" : "Offline";
            $("btnStart").disabled = false;
            $("btnStop").disabled = true;
            $("btnRestart").disabled = true;
            $("streamUptime").textContent = "";
        }
    }

    // Network / SRT stats
    if (data.network) {
        $("bitrateValue").textContent = `${Math.round(data.network.srt_bitrate_kbps)} kbps`;
        $("rttValue").textContent = `${data.network.srt_rtt_ms.toFixed(1)} ms`;
        $("packetLoss").textContent = `${data.network.srt_packet_loss_percent.toFixed(2)}%`;
    }

    // Internet connectivity
    if (data.connectivity) {
        const inet = $("internetStatus");
        if (data.connectivity.internet_connected) {
            inet.textContent = `OK (${data.connectivity.internet_rtt_ms?.toFixed(0) ?? "?"}ms)`;
            inet.style.color = "var(--green)";
        } else {
            inet.textContent = "Offline";
            inet.style.color = "var(--red)";
        }
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

// ── Stream Control ──

async function streamControl(action) {
    try {
        const res = await fetch(`/api/stream/${action}`, { method: "POST" });
        const data = await res.json();
        if (!data.ok) {
            console.warn(`Stream ${action} failed:`, data.error);
        }
    } catch (e) {
        console.error(`Stream ${action} error:`, e);
    }
}

async function saveSettings() {
    const resolution = $("settingResolution").value.split("x");
    const updates = {
        BITRATE: $("settingBitrate").value,
        WIDTH: resolution[0],
        HEIGHT: resolution[1],
        SRT_LATENCY: $("settingLatency").value,
    };

    try {
        const res = await fetch("/api/stream/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updates),
        });
        const data = await res.json();
        if (data.ok) {
            alert("Settings saved. Restart the stream to apply.");
        }
    } catch (e) {
        console.error("Save settings error:", e);
    }
}

// ── OBS WebSocket Control ──
// Uses obs-websocket v5 protocol directly (no library dependency)

function obsConnect() {
    const host = $("obsHost").value.trim();
    const password = $("obsPassword").value;
    const port = 4455;

    if (!host) return;

    $("obsStatus").textContent = "Connecting...";
    $("obsStatus").className = "ws-status";

    obsWs = new WebSocket(`ws://${host}:${port}`);

    obsWs.onopen = () => {
        // Wait for Hello message from OBS
    };

    obsWs.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        handleObsMessage(msg, password);
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

    // op 0 = Hello
    if (op === 0) {
        const auth = msg.d.authentication;
        if (auth) {
            // Authenticate with challenge-response
            const secret = await sha256(password + auth.salt);
            const authResponse = await sha256(secret + auth.challenge);
            obsWs.send(JSON.stringify({
                op: 1, // Identify
                d: {
                    rpcVersion: 1,
                    authentication: authResponse,
                }
            }));
        } else {
            // No auth required
            obsWs.send(JSON.stringify({
                op: 1,
                d: { rpcVersion: 1 }
            }));
        }
    }

    // op 2 = Identified (auth success)
    if (op === 2) {
        obsConnected = true;
        $("obsStatus").textContent = "Connected to OBS";
        $("obsStatus").className = "ws-status connected";
        $("obsControls").style.display = "block";
        $("btnObsConnect").textContent = "Disconnect";
        $("btnObsConnect").onclick = obsDisconnect;

        // Fetch scenes
        obsSend("GetSceneList");
    }

    // op 7 = RequestResponse
    if (op === 7) {
        const type = msg.d.requestType;
        const data = msg.d.responseData;

        if (type === "GetSceneList" && data) {
            renderScenes(data.scenes, data.currentProgramSceneName);
        }
    }

    // op 5 = Event
    if (op === 5) {
        const eventType = msg.d.eventType;
        if (eventType === "CurrentProgramSceneChanged") {
            currentScene = msg.d.eventData.sceneName;
            highlightScene(currentScene);
        }
    }
}

let obsRequestId = 0;

function obsSend(requestType, requestData) {
    if (!obsWs || !obsConnected) return;
    obsRequestId++;
    obsWs.send(JSON.stringify({
        op: 6, // Request
        d: {
            requestType,
            requestId: String(obsRequestId),
            ...(requestData ? { requestData } : {}),
        }
    }));
}

function obsAction(action) {
    obsSend(action);
}

function obsDisconnect() {
    if (obsWs) obsWs.close();
    $("btnObsConnect").textContent = "Connect";
    $("btnObsConnect").onclick = obsConnect;
}

function renderScenes(scenes, current) {
    currentScene = current;
    const container = $("obsScenes");
    container.innerHTML = "";

    // OBS returns scenes in reverse order
    const sorted = [...scenes].reverse();

    sorted.forEach((scene) => {
        const div = document.createElement("div");
        div.className = "scene-item" + (scene.sceneName === current ? " active" : "");
        div.textContent = scene.sceneName;
        div.dataset.name = scene.sceneName;
        div.onclick = () => {
            obsSend("SetCurrentProgramScene", { sceneName: scene.sceneName });
        };
        container.appendChild(div);
    });
}

function highlightScene(name) {
    document.querySelectorAll(".scene-item").forEach((el) => {
        el.classList.toggle("active", el.dataset.name === name);
    });
}

// ── Crypto helper for OBS auth ──

async function sha256(message) {
    const encoder = new TextEncoder();
    const data = encoder.encode(message);
    const hash = await crypto.subtle.digest("SHA-256", data);
    return btoa(String.fromCharCode(...new Uint8Array(hash)));
}

// ── Load saved OBS settings from localStorage ──

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

// Save settings when connecting
const origConnect = obsConnect;
obsConnect = function () {
    saveObsSettings();
    origConnect();
};

// ── Init ──
loadObsSettings();
connectStats();
