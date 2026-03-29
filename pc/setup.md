# Home PC Setup - OBS SRT Receiver

## Prerequisites
- OBS Studio 28+ (has built-in obs-websocket v5)
- Tailscale installed and connected to your tailnet

## 1. Install Tailscale

Download from https://tailscale.com/download and sign in with the same account as your Pi.

Note your Tailscale IP (shown in the Tailscale app or via `tailscale ip`).

## 2. Configure OBS to Receive SRT Stream

1. Open OBS Studio
2. In **Sources**, click **+** > **Media Source**
3. Name it "Pi Stream" and click OK
4. Configure:
   - **Local File**: Unchecked
   - **Input**: `srt://:9000?mode=listener&passphrase=YOUR_SRT_PASSPHRASE`
   - **Input Format**: `mpegts`
   - **Reconnect Delay**: `2`
   - **Buffering (MB)**: `1`
5. Click OK

OBS is now listening on port 9000 for the Pi's SRT stream.

## 3. Enable OBS WebSocket (for remote control)

OBS 28+ has WebSocket built-in:

1. Go to **Tools** > **WebSocket Server Settings**
2. Check **Enable WebSocket Server**
3. Set a **Server Password** (you'll enter this in the dashboard)
4. Port default is **4455** (keep this unless it conflicts)
5. Click OK

## 4. Firewall (Tailscale handles this)

Since both devices are on Tailscale, no port forwarding is needed.
Tailscale traffic is already encrypted and authenticated.

If Windows Firewall blocks the connection, allow OBS through:
- Windows Settings > Firewall > Allow an app > Add OBS Studio
- Or allow ports 9000 (SRT) and 4455 (WebSocket) for Tailscale interface only

## 5. Verify

1. Start the stream on the Pi (via dashboard or SSH)
2. The OBS Media Source should show the webcam feed within a few seconds
3. Open the Pi dashboard and connect to OBS - you should see scene list populate

## Troubleshooting

**No video in OBS**: Check that Tailscale is connected on both devices. Verify the SRT passphrase matches. Try `srt-live-transmit srt://:9000?mode=listener file://con` on PC to test reception.

**High latency**: Lower the SRT latency in `stream.conf` (try 500000 for WiFi, 800000 for mobile data). Also reduce OBS Media Source buffering.

**OBS WebSocket won't connect**: Ensure the WebSocket server is enabled in OBS. Check the password matches. Verify port 4455 is accessible via Tailscale.
