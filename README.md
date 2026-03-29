# KevinStream - IRL Streaming System

A lightweight IRL streaming setup using a Raspberry Pi with a USB webcam. The Pi captures video, encodes it, and pushes it over [Tailscale](https://tailscale.com) to your home PC running OBS. A web dashboard on the Pi lets you control everything remotely from your phone.

## Architecture

```
┌─────────────────────────┐          Tailscale VPN          ┌─────────────────────────┐
│     Raspberry Pi        │ ──────── SRT stream ──────────> │     Home PC             │
│                         │                                 │                         │
│  USB Webcam + Mic       │                                 │  Receiver (ffmpeg)      │
│  FFmpeg encoder         │                                 │        ↓                │
│  Web Dashboard (:8080)  │ <── OBS WebSocket control ───── │  OBS Studio             │
│  WiFi / USB / Ethernet  │                                 │        ↓                │
│  Auto AP fallback       │                                 │  Twitch / YouTube       │
└─────────────────────────┘                                 └─────────────────────────┘
```

**Pi sends video** → **Home PC receives and streams to platform**

The Pi stays lightweight (just capture + encode). Your home PC handles OBS, overlays, chat, and the actual platform stream.

## Features

### Dashboard
- Real-time system monitoring (CPU, RAM, temperature)
- Stream control (start/stop/restart) with live FFmpeg logs
- Camera preview when idle (auto-hides during streaming)
- Protocol switcher (SRT / RTMP) with per-protocol settings
- Plug-and-play device detection (cameras + microphones)
- OBS remote control (scenes, streaming, recording)
- WiFi browser with scan/connect from the dashboard
- Auto AP fallback — if internet drops, Pi creates its own WiFi hotspot

### Streaming
- SRT protocol (low latency, handles packet loss)
- RTMP fallback for compatibility
- Software encoding (libx264) or hardware encoding (h264_v4l2m2m)
- Configurable bitrate, resolution, framerate
- Audio capture from USB microphones
- Auto-restart on crash with exponential backoff

### Networking
- Tailscale VPN for secure Pi-to-PC connection (no port forwarding)
- Multi-source internet: Ethernet, USB tethering, WiFi
- Automatic AP mode when all connections fail
- Dashboard accessible at `http://192.168.4.1:8080` via AP

## Quick Start

### 1. Flash the Pi

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to flash **Raspberry Pi OS Lite (64-bit)**.

In the imager settings (gear icon), set:
- Username and password
- WiFi network
- Enable SSH

### 2. Run the Setup Script

SSH into the Pi and run:

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/ToGoodToWork/KevinIRL/main/setup-pi.sh)"
```

The script will:
- Install all dependencies (ffmpeg, Python, Tailscale)
- Clone this repository
- Set up a Python virtual environment
- Prompt you for your home PC's Tailscale IP
- Install and start the systemd service
- Configure boot settings for optimal streaming

### 3. Set Up the Receiver (Home PC)

Install [Tailscale](https://tailscale.com/download) and sign in with the same account as the Pi.

**Option A: OBS Direct (Windows)**

Add a Media Source in OBS:
- Uncheck **Local File**
- Input: `srt://:9000?mode=listener`
- Input Format: `mpegts`

**Option B: Receiver Script (any platform)**

```bash
# Install ffmpeg with SRT support first
python receiver/receive.py
```

Then in OBS, add Media Source: `udp://127.0.0.1:9001` (format: `mpegts`)

The receiver auto-reconnects when the Pi stream drops and resumes.

### 4. Start Streaming

Open the dashboard at `http://<PI_TAILSCALE_IP>:8080` and click **Start**.

## Project Structure

```
pi/
  dashboard/
    app.py                  # Flask web server
    config.py               # Dashboard config
    static/                 # Frontend (HTML, CSS, JS)
    monitors/
      system_monitor.py     # CPU, RAM, temperature
      stream_monitor.py     # FFmpeg stream stats
      network_monitor.py    # Internet connectivity
      wifi_manager.py       # WiFi scan/connect, AP fallback
  stream/
    stream.sh               # FFmpeg launch script
    stream.conf             # Stream configuration
    stream_manager.py       # FFmpeg process manager
  systemd/
    kevinstream.service     # Systemd service file
  install.sh                # Dependency installer
  setup-hotspot.sh          # WiFi hotspot setup
receiver/
  receive.py                # Cross-platform SRT receiver
  start-receiver.bat        # Windows launcher
  start-receiver.sh         # macOS/Linux launcher
shared/
  tailscale-setup.md        # Tailscale setup guide
  config.example.env        # Example configuration
setup-pi.sh                 # Full Pi bootstrap script
```

## Configuration

All stream settings are in `pi/stream/stream.conf`:

| Setting | Default | Description |
|---------|---------|-------------|
| `PROTOCOL` | `srt` | `srt` or `rtmp` |
| `SRT_HOST` | `100.x.y.z` | Home PC Tailscale IP |
| `SRT_PORT` | `9000` | SRT port |
| `ENCODER` | `libx264` | `libx264` (compatible) or `h264_v4l2m2m` (hardware) |
| `BITRATE` | `2500k` | Video bitrate |
| `WIDTH` / `HEIGHT` | `1280` / `720` | Resolution |
| `FPS` | `30` | Framerate |
| `AUDIO_DEVICE` | `none` | ALSA device or `none` to disable |

All settings can be changed from the dashboard without SSH.

## Dashboard

Access at `http://<PI_IP>:8080`

The dashboard provides:
- **System**: CPU, RAM, temperature with progress bars
- **Network**: Interface status, WiFi browser, AP mode control
- **Stream Control**: Start/stop, protocol switching, device selection
- **Devices**: Auto-detect cameras and mics, select resolution
- **Encoding**: Bitrate and resolution settings
- **OBS Remote**: Connect to OBS WebSocket, switch scenes, start/stop streaming and recording
- **Logs**: Live FFmpeg output with color-coded errors

## OBS Remote Control

The dashboard connects to OBS via WebSocket (v5 protocol):

1. In OBS: **Tools > WebSocket Server Settings** > Enable
2. In the dashboard OBS Remote section, enter:
   - Your PC's Tailscale IP (or `127.0.0.1` if on same machine)
   - The WebSocket password
3. Click Connect

You can then switch scenes, start/stop streaming and recording remotely.

## WiFi & Network

The Pi supports three internet sources (in priority order):
1. **Ethernet** (most reliable)
2. **USB tethering** (phone hotspot via USB)
3. **WiFi**

If all connections fail, the Pi automatically creates a WiFi hotspot called **KevinIRL**. Connect your phone to it and access the dashboard at `http://192.168.4.1:8080` to configure a new WiFi network.

## Updating

On the Pi:

```bash
cd ~/KevinIRL && git pull
sudo cp -r pi/* /opt/kevinstream/pi/
sudo systemctl restart kevinstream
```

## Useful Commands

```bash
# Check service status
sudo systemctl status kevinstream

# View live logs
sudo journalctl -u kevinstream -f

# Restart service
sudo systemctl restart kevinstream

# Check Tailscale IP
tailscale ip -4

# Test camera
ffmpeg -f v4l2 -list_formats all -i /dev/video0

# Test microphone
arecord -l
```

## Requirements

### Raspberry Pi
- Raspberry Pi 4/5 (1GB+ RAM)
- Raspberry Pi OS Lite (64-bit, Bookworm)
- USB webcam
- Internet connection (WiFi, Ethernet, or USB tethering)

### Home PC
- OBS Studio 28+
- Tailscale
- ffmpeg with SRT support (for receiver script)

## License

MIT
