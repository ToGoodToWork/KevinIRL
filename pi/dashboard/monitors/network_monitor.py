"""Network connectivity monitoring."""

import subprocess
import time

import psutil


def get_net_io() -> dict:
    """Get network I/O counters."""
    counters = psutil.net_io_counters()
    return {
        "bytes_sent": counters.bytes_sent,
        "bytes_recv": counters.bytes_recv,
        "packets_sent": counters.packets_sent,
        "packets_recv": counters.packets_recv,
    }


def ping(host: str = "1.1.1.1", count: int = 1, timeout: int = 3) -> float | None:
    """Ping a host and return RTT in ms, or None if unreachable."""
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), host],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        if result.returncode == 0:
            # Parse "time=XX.X ms" from output
            for line in result.stdout.splitlines():
                if "time=" in line:
                    ms = line.split("time=")[1].split(" ")[0]
                    return float(ms)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return None


def get_stats() -> dict:
    io = get_net_io()
    rtt = ping()
    return {
        "net_io": io,
        "internet_rtt_ms": rtt,
        "internet_connected": rtt is not None,
    }
