# Tailscale Setup Guide

Tailscale creates a secure VPN mesh between your Pi and home PC so they can communicate over the internet without port forwarding.

## Install on Raspberry Pi

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the link to authenticate. Note the Pi's Tailscale IP (100.x.y.z).

## Install on Home PC (Windows)

Download from https://tailscale.com/download/windows and sign in with the same account.

Note the PC's Tailscale IP (shown in the Tailscale tray icon or via `tailscale ip` in PowerShell).

## Verify Connectivity

From the Pi:
```bash
ping <PC_TAILSCALE_IP>
```

From the PC:
```bash
ping <PI_TAILSCALE_IP>
```

Both should respond with low latency.

## Configure KevinStream

1. Edit `pi/stream/stream.conf`:
   - Set `SRT_HOST` to your PC's Tailscale IP

2. In OBS, the SRT listener binds to all interfaces including Tailscale, so no extra config needed.

3. In the dashboard's OBS control, enter the PC's Tailscale IP.

## Optional: Public Dashboard Access

To access the dashboard from any device (not just Tailscale):

```bash
# On the Pi
sudo tailscale funnel 8080
```

This gives you a public HTTPS URL like `https://pi.tailnet-name.ts.net`.

Note: OBS control from outside Tailscale requires the Pi to proxy WebSocket connections (not implemented in v1).

## Troubleshooting

**Can't connect**: Ensure both devices are on the same Tailscale account. Check `tailscale status` on both.

**Slow connection**: Tailscale uses direct connections when possible (DERP relay as fallback). Check `tailscale netcheck` for relay quality.

**Pi on mobile data**: Tailscale handles carrier-grade NAT. If using a mobile hotspot, ensure the hotspot allows UDP traffic.
