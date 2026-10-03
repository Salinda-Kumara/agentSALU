"""Local system statistics (shared by the system_status tool and the UI)."""

from __future__ import annotations

import platform
import socket
import time

import psutil

_net_cache: dict = {"t": 0.0, "online": None}
NET_CHECK_HOST = ("1.1.1.1", 443)   # Cloudflare DNS — only a TCP connect, no data sent
NET_CHECK_TTL = 30
psutil.cpu_percent(interval=None)  # prime: the first non-blocking reading is always 0.0


def network_online() -> bool:
    now = time.time()
    if _net_cache["online"] is not None and now - _net_cache["t"] < NET_CHECK_TTL:
        return _net_cache["online"]
    try:
        with socket.create_connection(NET_CHECK_HOST, timeout=1.5):
            online = True
    except OSError:
        online = False
    _net_cache.update(t=now, online=online)
    return online


def quick_stats() -> dict:
    vm = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": vm.percent,
        "ram_used_gb": round(vm.used / 1e9, 1),
        "ram_total_gb": round(vm.total / 1e9, 1),
        "network_online": network_online(),
    }


def full_status() -> dict:
    vm = psutil.virtual_memory()
    disks = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append({"mount": p.mountpoint, "used_percent": u.percent,
                      "free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)})
    procs = []
    for pr in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((pr.info["memory_info"].rss, pr.info["name"]))
        except (psutil.Error, TypeError, AttributeError):
            continue
    procs.sort(reverse=True)
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    return {
        "host": platform.node(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_percent": vm.percent,
        "ram_used_gb": round(vm.used / 1e9, 1),
        "ram_total_gb": round(vm.total / 1e9, 1),
        "disks": disks,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "process_count": len(procs),
        "top_memory_processes": [{"name": n, "rss_mb": round(r / 1e6)} for r, n in procs[:5]],
        "network_online": network_online(),
        "battery_percent": round(battery.percent) if battery else None,
    }
