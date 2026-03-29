"""KevinStream - Dashboard Configuration."""

import os

# Dashboard server
HOST = os.environ.get("DASH_HOST", "0.0.0.0")
PORT = int(os.environ.get("DASH_PORT", "8080"))

# Stats push interval (seconds)
STATS_INTERVAL = 2

# Stream config file path
STREAM_CONF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "stream", "stream.conf",
)
