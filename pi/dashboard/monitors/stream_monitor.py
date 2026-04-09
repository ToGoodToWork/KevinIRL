"""Stream status monitoring."""

import sys
import os

# Add parent paths so we can import stream_manager
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "stream"))

from stream_manager import manager


def get_stats() -> dict:
    """Get combined stream, SRT, and encoding stats."""
    stream = manager.stats
    srt = manager.srt_stats
    enc = manager.encoding_stats
    return {
        "stream": stream,
        "stream_network": {
            "srt_bitrate_kbps": srt.get("bitrate_kbps", 0),
            "srt_rtt_ms": srt.get("rtt_ms", 0),
            "srt_packet_loss_percent": srt.get("packet_loss_percent", 0),
            "srt_send_buffer_ms": srt.get("send_buffer_ms", 0),
        },
        "encoding": {
            "fps": enc.get("fps", 0),
            "frame": enc.get("frame", 0),
            "speed": enc.get("speed", 0),
            "quality": enc.get("quality", 0),
            "dropped_frames": enc.get("dropped_frames", 0),
        },
    }
