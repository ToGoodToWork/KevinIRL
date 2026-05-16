// KevinStream Dashboard V2

// ── State ──
let statsWs = null;
let obsWs = null;
let obsConnected = false;
let currentScene = "";
let obsRecording = false;
let obsStreaming = false;
let capabilities = null;

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
        // The DeviceMonitor only includes `devices` when the set has changed,
        // so we can re-apply unconditionally here.
        if (data.devices) {
            applyDeviceList(data.devices, { source: "monitor" });
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
    updateSaveModeHint();
}

function updateNetworkUI(data) {
    // Stream SRT stats
    if (data.stream_network) {
        const br = Math.round(data.stream_network.srt_bitrate_kbps);
        const target = data.stream_network.target_bitrate_kbps || 0;
        $("bitrateValue").textContent = target > 0 ? `${br} / ${target} kbps` : `${br} kbps`;
        $("bitrateValue").style.color = target > 0 && br > 0 && br < target * 0.5 ? "var(--red)" : target > 0 && br > 0 && br < target * 0.8 ? "var(--yellow)" : "";

        const rtt = data.stream_network.srt_rtt_ms;
        const rttEl = $("rttValue");
        rttEl.textContent = `${rtt.toFixed(1)} ms`;
        rttEl.style.color = rtt > 200 ? "var(--red)" : rtt > 100 ? "var(--yellow)" : "";

        const loss = data.stream_network.srt_packet_loss_percent;
        const lossEl = $("packetLoss");
        lossEl.textContent = `${loss.toFixed(2)}%`;
        lossEl.style.color = loss > 5 ? "var(--red)" : loss > 1 ? "var(--yellow)" : "";
    }

    // Encoding health
    if (data.encoding) {
        if (data.encoding.encoder) {
            $("encEncoder").textContent = data.encoding.encoder === "h264_v4l2m2m" ? "Hardware (Pi)" : "Software (x264)";
        }
        const fpsEl = $("encFps");
        const fps = data.encoding.fps;
        const targetFps = data.encoding.target_fps || 30;
        fpsEl.textContent = fps > 0 ? `${fps.toFixed(1)} / ${targetFps}` : "--";
        fpsEl.style.color = fps > 0 && fps < targetFps * 0.65 ? "var(--red)" : fps > 0 && fps < targetFps * 0.85 ? "var(--yellow)" : "";

        const speed = data.encoding.speed;
        const speedEl = $("encSpeed");
        speedEl.textContent = speed > 0 ? `${speed.toFixed(2)}x` : "--x";
        speedEl.style.color = speed > 0 && speed < 0.9 ? "var(--red)" : speed > 0 && speed < 0.95 ? "var(--yellow)" : "";

        const dropped = data.encoding.dropped_frames;
        const dropEl = $("encDropped");
        dropEl.textContent = dropped;
        dropEl.style.color = dropped > 50 ? "var(--red)" : dropped > 10 ? "var(--yellow)" : "";
    }

    // Drift / delay tracking
    if (data.drift) {
        const drift = data.drift.drift_seconds;
        const driftEl = $("encDrift");
        driftEl.textContent = `${drift.toFixed(1)}s`;
        driftEl.style.color = drift > 10 ? "var(--red)" : drift > 5 ? "var(--yellow)" : "";

        // Alert banner
        const banner = $("alertBanner");
        const health = data.drift.health;
        if (health === "critical" && data.stream && data.stream.status === "live") {
            banner.style.display = "flex";
            banner.className = "card card-wide alert-banner critical";
            $("alertIcon").textContent = "\u26A0";
            $("alertText").textContent = `Stream falling behind! Drift: ${drift.toFixed(1)}s \u2014 auto-restart at 15s`;
        } else if (health === "warning" && data.stream && data.stream.status === "live") {
            banner.style.display = "flex";
            banner.className = "card card-wide alert-banner warning";
            $("alertIcon").textContent = "\u26A0";
            $("alertText").textContent = `Stream delay building: ${drift.toFixed(1)}s \u2014 encoding may be too slow for this bitrate`;
        } else {
            banner.style.display = "none";
        }
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

// In-memory log buffer so filter chips can re-render without losing history.
const logBuffer = [];
let logFilter = "all";

function logMatchesFilter(entry) {
    if (logFilter === "all") return true;
    if (logFilter === "stats") return /^\[stream\]/.test(entry.text);
    return entry.level === logFilter;
}

function renderLogEntry(entry) {
    const div = document.createElement("div");
    div.className = "log-line";
    const cls = entry.level === "error" ? "log-error" : entry.level === "warn" ? "log-warn" : "log-info";
    div.innerHTML = `<span class="log-time">${entry.time}</span> <span class="${cls}">${escapeHtml(entry.text)}</span>`;
    return div;
}

function rerenderLogs() {
    const terminal = $("logTerminal");
    terminal.innerHTML = "";
    const visible = logBuffer.filter(logMatchesFilter);
    if (visible.length === 0) {
        terminal.innerHTML = '<div class="dim small">No log lines match this filter.</div>';
        return;
    }
    visible.forEach((entry) => terminal.appendChild(renderLogEntry(entry)));
    terminal.scrollTop = terminal.scrollHeight;
}

function setLogFilter(filter) {
    logFilter = filter;
    document.querySelectorAll("#logFilters .log-filter-chip").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.filter === filter);
    });
    rerenderLogs();
}

function appendLogs(entries) {
    const terminal = $("logTerminal");

    // Remove placeholder on first log
    if (terminal.children.length === 1 && terminal.children[0].classList?.contains("dim")) {
        terminal.innerHTML = "";
    }

    entries.forEach((entry) => {
        logBuffer.push(entry);
        if (logMatchesFilter(entry)) {
            terminal.appendChild(renderLogEntry(entry));
        }
    });

    // Cap in-memory buffer at 4× the DOM cap so filter switches still have history.
    while (logBuffer.length > MAX_LOG_LINES * 4) {
        logBuffer.shift();
    }
    while (terminal.children.length > MAX_LOG_LINES) {
        terminal.removeChild(terminal.firstChild);
    }

    terminal.scrollTop = terminal.scrollHeight;
}

function clearLogs() {
    logBuffer.length = 0;
    $("logTerminal").innerHTML = '<div class="dim small">Logs cleared</div>';
    fetch("/api/logs/clear", { method: "POST" });
}

function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// ══════════════════════════════════════════════════════════════
// Connection Check
// ══════════════════════════════════════════════════════════════

async function checkTarget() {
    const btn = $("btnCheckTarget");
    const result = $("targetCheckResult");
    btn.disabled = true;
    btn.textContent = "Checking...";
    result.style.display = "block";
    result.innerHTML = '<span class="dim small">Testing connection to stream target...</span>';

    try {
        const res = await fetch("/api/network/check-target", { method: "POST" });
        const data = await res.json();

        let html = "";
        if (data.ok) {
            html += `<div class="stat-row"><span class="stat-label">Target</span><span class="stat-value" style="color:var(--green)">${data.host}:${data.port} OK</span></div>`;
        } else {
            html += `<div class="stat-row"><span class="stat-label">Target</span><span class="stat-value" style="color:var(--red)">${data.host}:${data.port} FAIL</span></div>`;
        }
        if (data.ping_ok && data.ping_avg_ms != null) {
            const color = data.ping_avg_ms > 100 ? "var(--yellow)" : "var(--green)";
            html += `<div class="stat-row"><span class="stat-label">Ping</span><span class="stat-value" style="color:${color}">${data.ping_avg_ms.toFixed(1)} ms (${data.ping_min_ms.toFixed(1)}-${data.ping_max_ms.toFixed(1)})</span></div>`;
        } else {
            html += `<div class="stat-row"><span class="stat-label">Ping</span><span class="stat-value" style="color:var(--red)">Unreachable</span></div>`;
        }
        if (data.port_open) {
            html += `<div class="stat-row"><span class="stat-label">Port ${data.port}</span><span class="stat-value" style="color:var(--green)">Open (${data.tcp_ms} ms)</span></div>`;
        } else {
            html += `<div class="stat-row"><span class="stat-label">Port ${data.port}</span><span class="stat-value" style="color:var(--red)">Closed</span></div>`;
        }
        result.innerHTML = html;
    } catch (e) {
        result.innerHTML = '<span class="dim small" style="color:var(--red)">Connection check failed</span>';
    } finally {
        btn.disabled = false;
        btn.textContent = "Check Connection";
    }
}

// ══════════════════════════════════════════════════════════════
// Stream Control
// ══════════════════════════════════════════════════════════════

async function forceStop() {
    if (!confirm("Force-kill all ffmpeg streaming processes? This is a panic button — use only if Stop/Restart aren't working.")) return;
    try {
        const res = await fetch("/api/stream/force-stop", { method: "POST" });
        const data = await res.json();
        const n = (data.killed || []).length;
        showToast(n ? `Force-killed ${n} ffmpeg process(es)` : "No ffmpeg processes found", n ? "warn" : "info");
    } catch (e) {
        console.error("Force stop failed:", e);
        showToast("Force stop failed — see console", "error");
    }
}

async function streamControl(action) {
    try {
        const res = await fetch(`/api/stream/${action}`, { method: "POST" });
        const data = await res.json();
        if (!data.ok) {
            console.warn(`Stream ${action} failed:`, data.error);
            // Surface immediate-launch failures (config errors, missing devices,
            // bad encoder options) to the user. Anything more subtle will land in
            // the Logs panel via _add_log.
            if (action === "start" && data.error) {
                alert(`Stream failed to start:\n\n${data.error}\n\nSee the Logs panel for details.`);
            }
        }
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
        if (cfg.ENCODER) $("settingEncoder").value = cfg.ENCODER;
        if (cfg.BITRATE) $("settingBitrate").value = cfg.BITRATE;
        if (cfg.WIDTH && cfg.HEIGHT) {
            const r = `${cfg.WIDTH}x${cfg.HEIGHT}`;
            if ([...$("settingResolution").options].find(o => o.value === r)) {
                $("settingResolution").value = r;
            }
        }
        if (cfg.FPS) {
            const fpsSelect = $("settingFps");
            if ([...fpsSelect.options].find(o => o.value === cfg.FPS)) {
                fpsSelect.value = cfg.FPS;
            }
        }

        // Store current device values, then detect devices to populate dropdowns
        window._loadedVideoDevice = cfg.VIDEO_DEVICE || "/dev/video0";
        if (cfg.AUDIO_CHANNELS) $("settingAudioChannels").value = cfg.AUDIO_CHANNELS;
        window._loadedAudioDevice = cfg.AUDIO_DEVICE || "none";
        detectDevices();
    } catch (e) {
        console.error("Failed to load config:", e);
    }
}

// ══════════════════════════════════════════════════════════════
// Capabilities
// ══════════════════════════════════════════════════════════════

async function loadCapabilities() {
    try {
        const res = await fetch("/api/capabilities");
        capabilities = await res.json();
    } catch (e) {
        console.error("Failed to load capabilities:", e);
        capabilities = { encoders: {}, encoder_limits: {}, available_encoders: [] };
    }
    applyEncoderCapabilities();
    onEncoderChange();
}

function applyEncoderCapabilities() {
    if (!capabilities) return;
    const sel = $("settingEncoder");
    const note = $("encoderNote");
    const enc = capabilities.encoders || {};

    [...sel.options].forEach((opt) => {
        const available = !!enc[opt.value];
        opt.disabled = !available;
        opt.textContent = opt.textContent.replace(/\s*\(unavailable\)$/, "");
        if (!available) opt.textContent += " (unavailable)";
    });

    if (!enc.h264_v4l2m2m && enc.libx264) {
        note.textContent = "Hardware encoder unavailable on this Pi — using software (higher CPU).";
        note.style.color = "var(--yellow)";
    } else if (enc.h264_v4l2m2m && !enc.libx264) {
        note.textContent = "Software encoder unavailable — hardware encoding only.";
        note.style.color = "";
    } else if (!enc.h264_v4l2m2m && !enc.libx264) {
        note.textContent = "No encoders available — stream cannot start.";
        note.style.color = "var(--red)";
    } else {
        note.textContent = "";
    }

    // If the saved encoder is disabled, fall back to the first available.
    if (sel.options[sel.selectedIndex]?.disabled) {
        const firstOk = [...sel.options].find((o) => !o.disabled);
        if (firstOk) sel.value = firstOk.value;
    }
}

function onEncoderChange() {
    if (!capabilities) return;
    const enc = $("settingEncoder").value;
    const limits = (capabilities.encoder_limits || {})[enc] || {};
    capLimits(limits);
}

function capLimits(limits) {
    // Bitrate cap
    const brSel = $("settingBitrate");
    const maxBr = limits.max_bitrate_kbps || 99999;
    [...brSel.options].forEach((opt) => {
        const v = parseInt(opt.value);
        opt.disabled = !isNaN(v) && v > maxBr;
        opt.textContent = opt.textContent.replace(/\s*\(too high\)$/, "");
        if (opt.disabled) opt.textContent += " (too high)";
    });
    if (brSel.options[brSel.selectedIndex]?.disabled) {
        const firstOk = [...brSel.options].reverse().find((o) => !o.disabled);
        if (firstOk) {
            const prev = brSel.value;
            brSel.value = firstOk.value;
            showToast(`Bitrate adjusted ${prev} → ${firstOk.value} for selected encoder`, "info");
        }
    }

    // Resolution cap
    const resSel = $("settingResolution");
    [...resSel.options].forEach((opt) => {
        const [w, h] = opt.value.split("x").map((x) => parseInt(x));
        if (isNaN(w) || isNaN(h)) return;
        opt.disabled = w > (limits.max_width || 99999) || h > (limits.max_height || 99999);
        opt.textContent = opt.textContent.replace(/\s*\(too high\)$/, "");
        if (opt.disabled) opt.textContent += " (too high)";
    });
    if (resSel.options[resSel.selectedIndex]?.disabled) {
        const firstOk = [...resSel.options].find((o) => !o.disabled);
        if (firstOk) resSel.value = firstOk.value;
    }
    updateFpsForResolution();

    // FPS cap (re-applied after updateFpsForResolution rebuilt the list)
    const fpsSel = $("settingFps");
    const maxFps = limits.max_fps || 999;
    [...fpsSel.options].forEach((opt) => {
        const v = parseInt(opt.value);
        opt.disabled = !isNaN(v) && v > maxFps;
        opt.textContent = opt.textContent.replace(/\s*\(too high\)$/, "");
        if (opt.disabled) opt.textContent += " (too high)";
    });
    if (fpsSel.options[fpsSel.selectedIndex]?.disabled) {
        const firstOk = [...fpsSel.options].find((o) => !o.disabled);
        if (firstOk) fpsSel.value = firstOk.value;
    }
}

// ══════════════════════════════════════════════════════════════
// Device Detection
// ══════════════════════════════════════════════════════════════

async function detectDevices() {
    try {
        const res = await fetch("/api/devices");
        const devices = await res.json();
        applyDeviceList(devices, { source: "manual" });
    } catch (e) {
        console.error("Device detection failed:", e);
    }
}

/**
 * Render the device dropdowns from a device list, preserving the user's
 * current selection where possible and surfacing a toast when a previously
 * selected device is gone.
 *
 * Restore priority for each dropdown:
 *   1. Current DOM selection (what the user just picked)
 *   2. window._loaded{Video,Audio}Device (last persisted config)
 *   3. First DJI/Osmo entry, then first non-"none" entry
 *   4. "none"
 */
function applyDeviceList(devices, opts) {
    opts = opts || {};
    const camSelect = $("settingCamera");
    const micSelect = $("settingAudio");

    const previousCam = camSelect.value;
    const previousCamLabel = camSelect.options[camSelect.selectedIndex]?.dataset?.friendlyName || previousCam;
    const previousMic = micSelect.value;
    const previousMicLabel = micSelect.options[micSelect.selectedIndex]?.dataset?.friendlyName || previousMic;

    // Cameras
    camSelect.innerHTML = '<option value="none">Disabled</option>';
    devices.cameras.forEach((cam) => {
        const opt = document.createElement("option");
        opt.value = cam.device;
        const cleanName = cam.name.replace(/\s*\(platform:[^)]+\)\s*/g, "").trim();
        opt.textContent = `${cleanName} (${cam.device})`;
        opt.dataset.friendlyName = cleanName;
        if (cam.resolutions && cam.resolutions.length > 0) {
            opt.dataset.resolutions = JSON.stringify(cam.resolutions);
        }
        if (cam.fps_by_resolution) {
            opt.dataset.fpsByResolution = JSON.stringify(cam.fps_by_resolution);
        }
        camSelect.appendChild(opt);
    });
    const camResult = pickDeviceSelection(camSelect, previousCam, window._loadedVideoDevice);
    camSelect.value = camResult.value;
    if (camResult.lostDevice && previousCam !== "none") {
        showToast(`Camera disconnected: ${previousCamLabel} — falling back to ${camResult.label}`, "warn");
    }
    updateResolutionsForCamera();

    // Microphones
    micSelect.innerHTML = '<option value="none">Disabled</option>';
    devices.microphones.forEach((mic) => {
        const opt = document.createElement("option");
        opt.value = mic.device;
        opt.textContent = mic.name;
        opt.dataset.friendlyName = mic.card_name || mic.name;
        micSelect.appendChild(opt);
    });
    const micResult = pickDeviceSelection(micSelect, previousMic, window._loadedAudioDevice);
    micSelect.value = micResult.value;
    if (micResult.lostDevice && previousMic !== "none") {
        showToast(`Microphone disconnected: ${previousMicLabel} — falling back to ${micResult.label}`, "warn");
    }
}

function pickDeviceSelection(selectEl, previousValue, loadedValue) {
    const options = [...selectEl.options];
    const optByValue = (v) => v && options.find((o) => o.value === v);

    // Priority 1: current DOM selection still present.
    if (previousValue && previousValue !== "none") {
        const m = optByValue(previousValue);
        if (m) return { value: m.value, label: m.dataset.friendlyName || m.value, lostDevice: false };
    }
    const lostDevice = !!(previousValue && previousValue !== "none" && !optByValue(previousValue));

    // Priority 2: last-loaded config value.
    if (loadedValue) {
        const m = optByValue(loadedValue);
        if (m) return { value: m.value, label: m.dataset.friendlyName || m.value, lostDevice };
    }

    // Priority 3: prefer DJI/Osmo, then first available.
    const firstReal = options.find((o) => o.value !== "none");
    if (!firstReal) return { value: "none", label: "Disabled", lostDevice };
    const dji = options.find((o) => o.value !== "none" && /dji|osmo/i.test(o.dataset.friendlyName || o.textContent));
    const chosen = dji || firstReal;
    return { value: chosen.value, label: chosen.dataset.friendlyName || chosen.value, lostDevice };
}

async function onAudioDeviceChange() {
    const sel = $("settingAudio");
    const opt = sel.options[sel.selectedIndex];
    window._loadedAudioDevice = sel.value;
    try {
        await fetch("/api/stream/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                AUDIO_DEVICE: sel.value,
                AUDIO_DEVICE_NAME: (opt && opt.dataset.friendlyName) || "",
            }),
        });
    } catch (e) {
        console.error("Failed to persist audio device selection:", e);
    }
}

// ── Toasts ──
function showToast(text, level) {
    const container = $("toastContainer");
    if (!container) return;
    const div = document.createElement("div");
    div.className = `toast toast-${level || "info"}`;
    div.textContent = text;
    container.appendChild(div);
    setTimeout(() => {
        div.classList.add("toast-fade");
        setTimeout(() => div.remove(), 400);
    }, 4000);
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

    updateFpsForResolution();
}

function updateFpsForResolution() {
    const camSelect = $("settingCamera");
    const resSelect = $("settingResolution");
    const fpsSelect = $("settingFps");
    const selected = camSelect.options[camSelect.selectedIndex];
    const currentFps = fpsSelect.value;

    // Standard FPS options always available
    const standardFps = [60, 30, 25, 20, 15];
    // Get detected FPS for this resolution
    let detectedFps = [];
    if (selected && selected.dataset.fpsByResolution) {
        const fpsByRes = JSON.parse(selected.dataset.fpsByResolution);
        detectedFps = (fpsByRes[resSelect.value] || []).map(f => Math.round(f));
    }

    // Merge: all standard + any detected that aren't standard
    const allFps = [...new Set([...standardFps, ...detectedFps])].sort((a, b) => b - a);

    fpsSelect.innerHTML = "";
    allFps.forEach((fps) => {
        const opt = document.createElement("option");
        opt.value = String(fps);
        const supported = detectedFps.includes(fps);
        opt.textContent = supported ? `${fps} fps` : `${fps} fps (manual)`;
        fpsSelect.appendChild(opt);
    });

    // Re-select previous FPS if available
    if ([...fpsSelect.options].find(o => o.value === currentFps)) {
        fpsSelect.value = currentFps;
    }
}

async function saveSettings(doRestart) {
    // doRestart=true  → write config + restart ffmpeg (apply immediately)
    // doRestart=false → write config only (takes effect on next manual start)
    const btn = doRestart ? $("btnSaveRestart") : $("btnSaveOnly");
    const otherBtn = doRestart ? $("btnSaveOnly") : $("btnSaveRestart");
    const origText = btn.textContent;
    btn.disabled = true;
    if (otherBtn) otherBtn.disabled = true;
    btn.textContent = "Saving...";

    const proto = $("settingProtocol").value;
    const resolution = $("settingResolution").value.split("x");

    const bitrate = $("settingBitrate").value;
    // Auto-calculate rate control: maxrate = 1.2x bitrate, bufsize = 2x bitrate
    const bitrateNum = parseInt(bitrate);
    const maxrate = Math.round(bitrateNum * 1.2) + "k";
    const bufsize = Math.round(bitrateNum * 2) + "k";

    const fps = $("settingFps").value;

    const camOpt = $("settingCamera").options[$("settingCamera").selectedIndex];
    const micOpt = $("settingAudio").options[$("settingAudio").selectedIndex];

    const updates = {
        PROTOCOL: proto,
        ENCODER: $("settingEncoder").value,
        BITRATE: bitrate,
        MAXRATE: maxrate,
        BUFSIZE: bufsize,
        WIDTH: resolution[0],
        HEIGHT: resolution[1],
        FPS: fps,
        GOP_SIZE: String(parseInt(fps)),
        VIDEO_DEVICE: $("settingCamera").value,
        VIDEO_DEVICE_NAME: (camOpt && camOpt.dataset.friendlyName) || "",
        AUDIO_DEVICE: $("settingAudio").value,
        AUDIO_DEVICE_NAME: (micOpt && micOpt.dataset.friendlyName) || "",
        AUDIO_CHANNELS: $("settingAudioChannels").value,
    };

    if (proto === "rtmp") {
        updates.RTMP_URL = $("settingRtmpUrl").value;
    } else {
        updates.SRT_HOST = $("settingSrtHost").value;
        updates.SRT_PORT = $("settingSrtPort").value;
        updates.SRT_PASSPHRASE = $("settingSrtPass").value;
        updates.SRT_LATENCY = $("settingLatency").value;
    }

    const resetButton = (label, color) => {
        setTimeout(() => {
            btn.textContent = origText;
            btn.style.background = "";
            btn.style.color = "";
            btn.disabled = false;
            if (otherBtn) otherBtn.disabled = false;
        }, color === "var(--red)" ? 3000 : 2000);
    };

    try {
        const res = await fetch("/api/stream/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updates),
        });
        const data = await res.json();
        if (!data.ok) {
            btn.textContent = "Failed!";
            btn.style.background = "var(--red)";
            btn.style.color = "#fff";
            console.error("Save failed:", data.error);
            alert("Save failed: " + (data.error || "Unknown error"));
            resetButton(origText, "var(--red)");
            return;
        }
        if (doRestart) {
            btn.textContent = "Saved! Restarting...";
            btn.style.background = "var(--green)";
            btn.style.color = "#000";
            await streamControl("restart");
        } else {
            btn.textContent = "Saved";
            btn.style.background = "var(--green)";
            btn.style.color = "#000";
            // If a stream is currently live, the new values aren't active yet.
            const live = $("streamStatus").textContent === "Live";
            showToast(
                live
                    ? "Config saved — new values take effect on next Restart."
                    : "Config saved.",
                "info",
            );
        }
        resetButton(origText, "var(--green)");
    } catch (e) {
        console.error("Save settings error:", e);
        btn.textContent = "Error!";
        btn.style.background = "var(--red)";
        alert("Could not save settings: " + e.message);
        resetButton(origText, "var(--red)");
    }
}

// Update the hint under the Save buttons based on current stream state.
function updateSaveModeHint() {
    const hint = $("saveModeHint");
    if (!hint) return;
    const live = $("streamStatus").textContent === "Live";
    if (live) {
        hint.textContent = "Stream is live — 'Save' writes the config without interrupting (applies on next Restart). 'Save & Restart' applies immediately (brief gap).";
    } else {
        hint.textContent = "'Save' writes the config. 'Save & Restart' writes and then starts the stream.";
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
loadCapabilities().then(loadConfig);
connectStats();
