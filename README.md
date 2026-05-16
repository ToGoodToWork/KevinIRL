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
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/ToGoodToWork/KevinIRL/master/setup-pi.sh)"
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

The Pi pulls from `origin/master` on GitHub — there's no direct push from your
dev machine. The flow is always:

1. On your dev machine: `git push` to `origin/master`.
2. On the Pi, run the remote updater (this `curl`s the latest `update-pi.sh`
   itself, so it works even if the local copy on the Pi has drifted):

   ```bash
   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/ToGoodToWork/KevinIRL/master/update-pi.sh)"
   ```

   Equivalent local form if you've already SSH'd in and want to use the
   on-disk script:

   ```bash
   sudo bash /opt/kevinstream/update-pi.sh
   ```

The updater:

1. Backs up your `pi/stream/stream.conf` to `/var/backups/kevinstream/`.
2. Stops the service so a half-pulled state can't crash-loop.
3. **Hard-resets** the repo to `origin/master` — wipes any local edits or
   untracked junk that accumulated in the source tree.
4. Restores your `stream.conf` (or bootstraps from `stream.conf.example` if
   there wasn't one), then chowns it back to the dashboard user so non-root
   config saves still work.
5. Reinstalls Python packages if `requirements.txt` changed.
6. Starts the service back up.

The dashboard reads/writes `stream.conf` through `shlex.quote` / `shlex.split`,
so device names with spaces or parens (`Wireless Microphone RX`,
`OsmoPocket3 (usb-...)`) survive being bash-`source`d in `stream.sh`. On boot,
`StreamManager` re-quotes any pre-existing unquoted values, so upgrading from
an older version self-heals on first restart.

`pi/stream/stream.conf` is gitignored — your SRT host, passphrase, device
selections, and bitrate live there and are never touched by pulls. The
tracked template at `pi/stream/stream.conf.example` is only used to
bootstrap a fresh install. If a future release adds new keys, compare:

```bash
sudo diff /opt/kevinstream/pi/stream/stream.conf /opt/kevinstream/pi/stream/stream.conf.example
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

## Equipment notes

Tested primary rig:

- **Camera:** DJI Osmo Pocket 3 (or Osmo Action) as USB webcam — standard UVC,
  MJPEG 1920×1080 @ 30fps. Registers multiple `/dev/videoN` nodes; the
  dashboard auto-picks the right capture node. Hardware encoder
  (`h264_v4l2m2m`) handles this at 2500–3500 kbps comfortably on a Pi 4.
- **Microphone:** DJI Wireless Mic RX *or* the camera's built-in mic — both
  expose themselves as standard UAC (USB Audio Class) devices to ALSA. No
  drivers needed; mainline `uvcvideo` and `snd-usb-audio` cover both.

The ALSA card index for a USB audio device can shift between reboots — the
Pi resolves it back to the right `plughw:CARD,0` automatically using the
friendly name saved in `VIDEO_DEVICE_NAME` / `AUDIO_DEVICE_NAME`.

### Auto-detect priority

When multiple mics are plugged in, the dashboard picks one using priority
tiers (`pi/dashboard/devices.py::_mic_priority`):

| Tier | Match | Example | Channels |
|-----:|-------|---------|---------:|
| 30 | `wireless microphone`, `dji mic`, `rx` | DJI Wireless Mic RX | 1 (mono) |
| 20 | `dji`, `osmo`, `pocket` | DJI Pocket 3 built-in | 2 (stereo) |
| 10 | `usb` + `mic` | Generic USB mic | 2 |
| 0  | anything else | unknown card | 2 |

If you have both a DJI Pocket and a DJI Wireless Mic plugged in, the Wireless
Mic wins. The monitor will also **upgrade** between known tiers automatically
— plugging in the Wireless Mic on top of an already-running Pocket pick will
swap to the Wireless Mic and set `AUDIO_CHANNELS=1` without a manual save.
A generic / unknown user pick is never overridden.

The normal workflow is: plug everything in, open the dashboard, click **Start**.

### SRT passphrase length

If you set an `SRT_PASSPHRASE`, the SRT protocol requires it to be **10–79
characters**. Outside that range, `stream.sh` skips the passphrase with a
warning in the log and streams **unencrypted** — make sure the receiver side
also has no passphrase set when this happens, or it will refuse the
connection. Set a 10+ char passphrase on both ends for an encrypted stream.

## License

MIT
