"""System status endpoint — process health, versions, security overview."""

from __future__ import annotations

import asyncio
import os
import platform
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from phonon_stage import __version__

router = APIRouter(prefix="/system", tags=["system"])


class ProcessStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    running: bool
    pid: int = 0
    version: str = ""
    details: str = ""


class UsbBusInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bus: str
    speed_mbps: int = 0
    device_count: int
    devices: list[str]


class SystemStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_version: str
    hostname: str
    os_name: str
    arch: str
    kernel: str
    python_version: str
    memory_total_mb: int
    memory_used_mb: int
    memory_available_mb: int
    memory_percent: float = 0.0
    cpu_count: int
    cpu_load_1m: float = 0.0
    cpu_load_5m: float = 0.0
    cpu_load_15m: float = 0.0
    cpu_percent: float = 0.0
    processes: list[ProcessStatus]
    usb_buses: list[UsbBusInfo] = []


class SecurityStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    firewall_active: bool
    firewall_rules: list[str]
    ssh_authorized_keys: int
    ssh_port: int
    api_bind_address: str
    api_port: int
    tls_enabled: bool
    config_file_permissions: str
    data_dir_permissions: str
    phonon_user_groups: list[str]


async def _run(cmd: str, timeout: float = 3.0) -> str:
    """Run a shell command and return stdout, empty string on failure."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode().strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


async def _check_process(name: str, service: str, version_cmd: str = "") -> ProcessStatus:
    """Check if a systemd service or process is running."""
    # Try systemd user service first, then system service
    pid_str = await _run(f"pgrep -x {name} -u $(id -u) | head -1")
    if not pid_str:
        pid_str = await _run(f"pgrep -x {name} | head -1")

    running = bool(pid_str)
    pid = int(pid_str) if pid_str.isdigit() else 0

    version = ""
    if version_cmd:
        version = await _run(version_cmd)

    return ProcessStatus(name=service, running=running, pid=pid, version=version)


_sys_cache: SystemStatusResponse | None = None
_sys_cache_time: float = 0


@router.get("/status", response_model=SystemStatusResponse)
async def system_status(request: Request) -> SystemStatusResponse:
    import time

    global _sys_cache, _sys_cache_time
    now = time.monotonic()
    if _sys_cache and (now - _sys_cache_time) < 10.0:
        return _sys_cache
    # Memory info
    mem_total = mem_used = mem_avail = 0
    meminfo = await _run("cat /proc/meminfo")
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1]) // 1024
        elif line.startswith("MemAvailable:"):
            mem_avail = int(line.split()[1]) // 1024
    mem_used = mem_total - mem_avail

    # Check processes
    pw = await _check_process("pipewire", "PipeWire", "pipewire --version 2>&1 | tail -1")
    wp = await _check_process(
        "wireplumber",
        "WirePlumber",
        "wireplumber --version 2>&1 | grep -o '[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+'",
    )
    bluez = await _check_process(
        "bluetoothd", "BlueZ", "bluetoothd --version 2>&1 | grep -o '[0-9]\\+\\.[0-9]\\+'"
    )
    avahi = await _check_process(
        "avahi-daemon",
        "Avahi",
        "avahi-daemon --version 2>&1 | grep -o '[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+'",
    )

    # PipeWire details
    if pw.running:
        pw_q_cmd = "pw-metadata -n settings 0 clock.quantum 2>/dev/null"
        quantum = await _run(f"{pw_q_cmd} | grep -o 'value:[0-9]*' | cut -d: -f2")
        pw_r_cmd = "pw-metadata -n settings 0 clock.rate 2>/dev/null"
        rate = await _run(f"{pw_r_cmd} | grep -o 'value:[0-9]*' | cut -d: -f2")
        pw = pw.model_copy(update={"details": f"quantum={quantum or '?'} rate={rate or '?'}Hz"})

    result = SystemStatusResponse(
        agent_version=__version__,
        hostname=platform.node(),
        os_name=await _run("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'")
        or platform.platform(),
        arch=platform.machine(),
        kernel=platform.release(),
        python_version=platform.python_version(),
        memory_total_mb=mem_total,
        memory_used_mb=mem_used,
        memory_available_mb=mem_avail,
        memory_percent=round(mem_used / mem_total * 100, 1) if mem_total else 0,
        cpu_count=os.cpu_count() or 1,
        cpu_load_1m=os.getloadavg()[0],
        cpu_load_5m=os.getloadavg()[1],
        cpu_load_15m=os.getloadavg()[2],
        cpu_percent=round(os.getloadavg()[0] / (os.cpu_count() or 1) * 100, 1),
        processes=[pw, wp, bluez, avahi],
        usb_buses=await _get_usb_buses(),
    )
    _sys_cache = result
    _sys_cache_time = now
    return result


async def _get_usb_buses() -> list[UsbBusInfo]:
    """Get USB bus info — device count and bandwidth usage."""
    raw = await _run("lsusb 2>/dev/null")
    buses: dict[str, list[str]] = {}
    for line in raw.splitlines():
        if not line.startswith("Bus"):
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        bus = line[:7]  # "Bus 001"
        desc = line.split("ID ")[1] if "ID " in line else line
        buses.setdefault(bus, []).append(desc.strip())
    # Get bus speed from lsusb -t
    bus_speeds: dict[str, int] = {}
    tree = await _run("lsusb -t 2>/dev/null")
    for line in tree.splitlines():
        if "Bus" in line and "root_hub" in line:
            parts = line.split("Bus ")
            if len(parts) >= 2:
                bus_num = parts[1].split(".")[0].strip()
                speed = 0
                if "480M" in line:
                    speed = 480
                elif "5000M" in line:
                    speed = 5000
                elif "12M" in line:
                    speed = 12
                bus_speeds[f"Bus {bus_num.zfill(3)}"] = speed

    return [
        UsbBusInfo(
            bus=bus,
            speed_mbps=bus_speeds.get(bus, 0),
            device_count=len(devs),
            devices=devs,
        )
        for bus, devs in sorted(buses.items())
    ]


@router.get("/security", response_model=SecurityStatusResponse)
async def security_status(request: Request) -> SecurityStatusResponse:
    cfg = request.app.state.config

    # Firewall
    fw_rules_raw = await _run("iptables -L -n --line-numbers 2>/dev/null | head -30")
    fw_active = bool(fw_rules_raw and "Chain" in fw_rules_raw)
    fw_rules = (
        [line.strip() for line in fw_rules_raw.splitlines() if line.strip()] if fw_active else []
    )

    # SSH
    ssh_keys = 0
    auth_keys_path = Path.home() / ".ssh" / "authorized_keys"
    if auth_keys_path.exists():
        ssh_keys = len(
            [
                line
                for line in auth_keys_path.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            ]
        )

    ssh_port = 22
    sshd_config = await _run("grep -E '^Port ' /etc/ssh/sshd_config 2>/dev/null")
    if sshd_config:
        parts = sshd_config.split()
        if len(parts) >= 2 and parts[1].isdigit():
            ssh_port = int(parts[1])

    # Config file permissions
    conf_perms = await _run(f"stat -c '%a' {cfg.standalone_conf_path} 2>/dev/null") or "N/A"
    data_perms = await _run("stat -c '%a' /var/lib/phonon 2>/dev/null") or "N/A"

    # User groups
    groups_raw = await _run("groups phonon 2>/dev/null")
    groups = groups_raw.split(":")[1].strip().split() if ":" in groups_raw else []

    return SecurityStatusResponse(
        firewall_active=fw_active,
        firewall_rules=fw_rules[:10],
        ssh_authorized_keys=ssh_keys,
        ssh_port=ssh_port,
        api_bind_address=cfg.bind_address,
        api_port=cfg.port,
        tls_enabled=False,
        config_file_permissions=conf_perms,
        data_dir_permissions=data_perms,
        phonon_user_groups=groups,
    )
