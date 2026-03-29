"""System monitoring: CPU, RAM, temperature."""

import subprocess

import psutil


def get_cpu_percent() -> float:
    return psutil.cpu_percent(interval=None)


def get_ram() -> dict:
    mem = psutil.virtual_memory()
    return {
        "used_mb": round(mem.used / 1024 / 1024),
        "total_mb": round(mem.total / 1024 / 1024),
        "percent": mem.percent,
    }


def get_temperature() -> float | None:
    """Read SoC temperature via vcgencmd (Pi-specific)."""
    try:
        result = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=2,
        )
        # Output: "temp=52.3'C"
        temp_str = result.stdout.strip().replace("temp=", "").replace("'C", "")
        return float(temp_str)
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        # Fallback: try psutil thermal zones
        temps = psutil.sensors_temperatures()
        if temps:
            for entries in temps.values():
                if entries:
                    return entries[0].current
    return None


def get_stats() -> dict:
    return {
        "cpu_percent": get_cpu_percent(),
        "ram": get_ram(),
        "temperature_c": get_temperature(),
    }
