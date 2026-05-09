"""System status endpoint — process health, versions, security overview."""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
from pathlib import Path
from typing import Any

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
    throttled: int = 0
    alerts: list[str] = []
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


def parse_throttled(raw: str) -> tuple[int, list[str]]:
    """Parse `vcgencmd get_throttled` output into (raw_int, list_of_alerts).

    Output format: "throttled=0x50000". The hex value is a bitmask:
      bit  0 (0x00001): under-voltage NOW
      bit  1 (0x00002): CPU frequency capped NOW
      bit  2 (0x00004): CPU throttled NOW
      bit  3 (0x00008): soft temp limit NOW (Pi 4+ only)
      bit 16 (0x10000): under-voltage occurred since boot
      bit 17 (0x20000): CPU frequency was capped since boot
      bit 18 (0x40000): CPU was throttled since boot
      bit 19 (0x80000): soft temp limit reached since boot

    "NOW" alerts override their "since boot" counterparts — if it's
    happening *right now*, we don't dilute the warning with the
    historical one.
    """
    val = 0
    if "=" in raw:
        with contextlib.suppress(ValueError):
            val = int(raw.split("=")[1], 16)

    alerts: list[str] = []
    if val & 0x1:
        alerts.append("UNDER-VOLTAGE NOW — change power supply!")
    elif val & 0x10000:
        alerts.append("Under-voltage occurred since boot")
    if val & 0x4:
        alerts.append("CPU THROTTLED NOW")
    elif val & 0x40000:
        alerts.append("CPU throttled since boot")
    if val & 0x2:
        alerts.append("CPU frequency capped NOW")
    elif val & 0x20000:
        alerts.append("CPU frequency was capped")
    return val, alerts


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

    # Throttle / voltage check
    throttled_raw = await _run("vcgencmd get_throttled 2>/dev/null")
    throttled_val, alerts = parse_throttled(throttled_raw)
    # Memory check
    if mem_total and mem_avail < mem_total * 0.15:
        alerts.append(f"Low memory: {mem_avail}MB available")
    # CPU check
    cpu_pct = round(os.getloadavg()[0] / (os.cpu_count() or 1) * 100, 1)
    if cpu_pct > 90:
        alerts.append(f"High CPU: {cpu_pct}%")
    # Process check
    for p in [pw, wp, bluez, avahi]:
        if not p.running:
            alerts.append(f"{p.name} is DOWN")
    # Temperature
    temp_raw = await _run("vcgencmd measure_temp 2>/dev/null")
    if "=" in temp_raw:
        try:
            temp = float(temp_raw.split("=")[1].replace("'C", ""))
            if temp > 80:
                alerts.append(f"OVERHEATING: {temp}°C")
            elif temp > 70:
                alerts.append(f"High temp: {temp}°C")
        except ValueError:
            pass

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
        throttled=throttled_val,
        alerts=alerts,
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


# ── Service registry + control ────────────────────────────────────────────
# Used by the new "Services" panel (rows with start/stop/restart buttons)
# and the WS dashboard push. The "kind" field decides whether systemctl
# runs as the daemon's user instance or via sudo against the system
# instance. For prod-mode hosts the sudoers grant is set up by install.sh;
# on dev hosts the system actions silently fail (caller marks the buttons
# as disabled in the UI).

SERVICES: dict[str, dict[str, str]] = {
    # User-instance services — no sudo
    "pipewire": {"kind": "user", "version_cmd": "pipewire --version 2>&1 | tail -1"},
    "wireplumber": {
        "kind": "user",
        "version_cmd": (
            "wireplumber --version 2>&1 | grep -o '[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+' | head -1"
        ),
    },
    "pipewire-pulse": {"kind": "user", "version_cmd": ""},
    # System-instance services — need NOPASSWD sudoers grants
    "bluetooth": {
        "kind": "system",
        "version_cmd": "bluetoothd --version 2>&1 | grep -o '[0-9]\\+\\.[0-9]\\+'",
    },
    "bluealsa": {"kind": "system", "version_cmd": ""},
    "avahi-daemon": {
        "kind": "system",
        "version_cmd": "avahi-daemon --version 2>&1 | grep -o '[0-9]\\+\\.[0-9]\\+\\.[0-9]\\+'",
    },
    "phonon-bt-agent": {"kind": "system", "version_cmd": ""},
    "phonon-bt-unblock": {"kind": "system", "version_cmd": ""},
    "phonon-ptp4l": {"kind": "system", "version_cmd": ""},
    "phonon-phc2sys": {"kind": "system", "version_cmd": ""},
}

# Allowed actions, mapped to systemctl verbs
SERVICE_ACTIONS = {"start", "stop", "restart", "enable", "disable"}

# Some services come with a paired .socket unit. Stopping/disabling
# the .service alone is pointless — socket activation respawns the
# daemon the next time anything touches the path. Stop/disable the
# socket too. Each socket needs its own sudoers rule on system kind
# (the rules are scoped to one command line each), so control_service
# runs them as separate systemctl calls.
SERVICE_SOCKETS: dict[str, list[str]] = {
    "pipewire": ["pipewire.socket"],
    "pipewire-pulse": ["pipewire-pulse.socket"],
    "avahi-daemon": ["avahi-daemon.socket"],
}


class ServiceState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str  # "user" | "system"
    active: bool
    enabled: bool
    pid: int = 0
    version: str = ""
    can_control: bool = True
    # False when systemctl can't find a unit file for this service —
    # typically a system unit on a dev workstation that hasn't run
    # install.sh yet. The UI hides the action buttons and the status
    # badge ignores such services.
    installed: bool = True
    # True for Type=oneshot units (phonon-bt-unblock — runs once at
    # boot then exits). is-active returns 'inactive' after a successful
    # run, which would otherwise make the status badge flag DEGRADED.
    # The UI excludes oneshots from the 'service is down' calc.
    oneshot: bool = False


async def _service_active_user(name: str) -> bool:
    """Check active state for a user-instance service (no sudo)."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "--user",
        "is-active",
        f"{name}.service",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() == "active"


async def _service_active_system(name: str) -> bool:
    """Check active state for a system service (no sudo, read-only)."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "is-active",
        f"{name}.service",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() == "active"


async def _service_enabled(name: str, kind: str) -> bool:
    """A service counts as 'enabled at boot' if its .service unit OR any
    paired .socket unit is enabled. On Ubuntu default avahi-daemon.service
    is `disabled` but avahi-daemon.socket is `enabled` — treating only
    the .service would mislead the topbar LED."""
    units = [f"{name}.service", *SERVICE_SOCKETS.get(name, [])]
    for unit in units:
        args = ["systemctl"]
        if kind == "user":
            args.append("--user")
        args.extend(["is-enabled", unit])
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        if out.decode().strip() in {"enabled", "static", "alias"}:
            return True
    return False


async def _service_installed(name: str, kind: str) -> bool:
    """True if a unit file is registered for this service. Cheap read-only
    check via `systemctl cat` — returns 0 if the unit exists, 1 with
    'No files found for …' on stderr if not."""
    args = ["systemctl"]
    if kind == "user":
        args.append("--user")
    args.extend(["cat", f"{name}.service"])
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.communicate()
    return proc.returncode == 0


_can_sudo_cache: dict[str, tuple[bool, float]] = {}
_CAN_SUDO_TTL = 60.0  # seconds


async def _can_sudo_systemctl(name: str) -> bool:
    """Cheap probe: does sudo -n systemctl is-active <name> work? If yes,
    the NOPASSWD entry is set up for this service; we'll accept control
    requests against it. If sudo fails (asks password), button is disabled.

    Cached for 60 s — the NOPASSWD grant is part of install.sh and
    doesn't flip mid-run. Without the cache, this probe ran on every
    /system/services tick for every system service (~14 sudo
    invocations per second on a 7-system-service host) and on a Pi 3
    the polkit auth path that fired around each sudo dominated CPU.
    """
    import time as _time

    now = _time.monotonic()
    cached = _can_sudo_cache.get(name)
    if cached is not None and now - cached[1] < _CAN_SUDO_TTL:
        return cached[0]
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        "/bin/systemctl",
        "is-active",
        f"{name}.service",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    # If the binary path or service is wrong sudo still returns 0 for the
    # privilege check; sudo asks for password only when no NOPASSWD match.
    result = b"password" not in err.lower() and b"a terminal is required" not in err.lower()
    _can_sudo_cache[name] = (result, now)
    return result


async def collect_services() -> list[ServiceState]:
    """Snapshot every registered service in parallel."""
    tasks: list[Any] = []
    names: list[str] = []
    for name, meta in SERVICES.items():
        names.append(name)

        async def _fetch(
            n: str = name,
            k: str = meta["kind"],
            ver_cmd: str = meta["version_cmd"],
        ) -> ServiceState:
            if not await _service_installed(n, k):
                return ServiceState(
                    name=n,
                    kind=k,
                    active=False,
                    enabled=False,
                    can_control=False,
                    installed=False,
                    version="not installed",
                )
            if k == "user":
                active = await _service_active_user(n)
                can_control = True
            else:
                active = await _service_active_system(n)
                can_control = await _can_sudo_systemctl(n)
            enabled = await _service_enabled(n, k)
            user_flag = "--user " if k == "user" else ""
            pid_str = await _run(
                f"systemctl {user_flag}show -p MainPID {n}.service 2>/dev/null | cut -d= -f2"
            )
            pid = int(pid_str) if pid_str.isdigit() and pid_str != "0" else 0
            type_str = await _run(
                f"systemctl {user_flag}show -p Type {n}.service 2>/dev/null | cut -d= -f2"
            )
            oneshot = type_str == "oneshot"
            version = ""
            if ver_cmd:
                version = await _run(ver_cmd)
            return ServiceState(
                name=n,
                kind=k,
                active=active,
                enabled=enabled,
                pid=pid,
                version=version,
                can_control=can_control,
                installed=True,
                oneshot=oneshot,
            )

        tasks.append(_fetch())
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[ServiceState] = []
    for r in results:
        if isinstance(r, ServiceState):
            out.append(r)
    return out


@router.get("/services", response_model=list[ServiceState])
async def list_services() -> list[ServiceState]:
    return await collect_services()


@router.post("/services/{name}/{action}")
async def control_service(name: str, action: str) -> dict[str, str]:
    if name not in SERVICES:
        return {"status": "unknown_service", "name": name}
    if action not in SERVICE_ACTIONS:
        return {"status": "unknown_action", "action": action}
    meta = SERVICES[name]
    if not await _service_installed(name, meta["kind"]):
        return {
            "status": "not_installed",
            "service": name,
            "output": (
                f"{name}.service has no unit file on this host — "
                "run install.sh or skip this service."
            ),
        }
    # When tearing the service down, also act on the paired socket
    # so it can't immediately reactivate it. start/restart/enable
    # don't need this — they pull the socket in via dependencies.
    extras: list[str] = []
    if action in {"stop", "disable"}:
        extras = SERVICE_SOCKETS.get(name, [])
    units = [f"{name}.service", *extras]

    # System-kind sudoers rules are scoped to a single unit per line,
    # so run one systemctl call per unit. User-kind has no such limit
    # but the same loop keeps the code uniform.
    rc = 0
    parts: list[str] = []
    for unit in units:
        if meta["kind"] == "user":
            cmd = ["systemctl", "--user", action, unit]
        else:
            cmd = ["sudo", "-n", "/bin/systemctl", action, unit]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        unit_rc = proc.returncode or 0
        unit_text = (out + err).decode(errors="ignore").strip()
        if unit_text:
            parts.append(f"{unit}: {unit_text}" if len(units) > 1 else unit_text)
        # First non-zero rc wins, but keep going so all units are
        # acted on even if one fails (matches systemctl's own behaviour).
        if unit_rc != 0 and rc == 0:
            rc = unit_rc
    text = " · ".join(parts)

    # If the user just restarted/started one of the audio user services,
    # rebuild bluealsa bridges + PipeWire mappings — otherwise every
    # routing the user had set up disappears silently. Stop/disable
    # don't replay (the user explicitly asked for a teardown).
    replay_summary: dict[str, int] | None = None
    sync_summary: dict[str, int] | None = None
    if rc == 0 and action in {"start", "restart"}:
        if name in AUDIO_USER_SERVICES:
            await asyncio.sleep(1.5)
            from phonon_stage.api.aes67 import replay_audio_state

            replay_summary = await replay_audio_state(reason=f"control.{name}.{action}")
        elif name in BT_RUNTIME_SERVICES:
            # Restarting bluealsa kills every `arecord -D bluealsa…` we
            # had piped into pacat. The wrapper `while true` re-spawns
            # both, but the fresh pacat registers a new PW node ID —
            # any user mapping built against the old one is orphan
            # until a resync. sync_bt_state runs both: bridge reconcile
            # + mapping re-attach.
            await asyncio.sleep(2.0)
            from phonon_stage.api.aes67 import sync_bt_state

            sync_summary = await sync_bt_state(reason=f"control.{name}.{action}")

    response: dict[str, Any] = {
        "status": "ok" if rc == 0 else "failed",
        "service": name,
        "action": action,
        "rc": str(rc),
        "output": text[:500],
    }
    if replay_summary is not None:
        response["replay"] = replay_summary
    if sync_summary is not None:
        response["bluealsa_sync"] = sync_summary
    return response


# Audio user services — restarting any one of them invalidates our
# runtime audio state (bridges, links). Used by control_service and
# the WS PID-delta watcher.
AUDIO_USER_SERVICES = {"pipewire", "wireplumber", "pipewire-pulse"}
# System services whose restart breaks our BT bridges and warrants an
# automatic /bluealsa/sync after a settle delay.
BT_RUNTIME_SERVICES = {"bluealsa", "bluetooth"}


@router.post("/audio-stack/restart")
async def restart_audio_stack() -> dict[str, Any]:
    """Convenience: restart pipewire → wireplumber → pipewire-pulse in
    that order, then replay our app-level audio state (bluealsa bridges,
    PipeWire links). Useful when audio breaks (no devices, hung XRUN
    bridge). All three are user services so no sudo needed."""
    seq = ["pipewire", "wireplumber", "pipewire-pulse"]
    results: list[str] = []
    for name in seq:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "restart",
            f"{name}.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
        results.append(f"{name}={rc}")
        await asyncio.sleep(0.5)

    from phonon_stage.api.aes67 import replay_audio_state

    await asyncio.sleep(1.5)
    replay = await replay_audio_state(reason="audio_stack_restart")
    return {"status": "ok", "sequence": " ".join(results), "replay": replay}


# ── Resource gauges (for the dashboard bars) ──────────────────────────────


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpu_pct: float
    cpu_count: int
    cpu_load_1m: float
    mem_total_mb: int
    mem_used_mb: int
    mem_pct: float
    swap_total_mb: int
    swap_used_mb: int
    swap_pct: float
    disk_total_gb: float
    disk_used_gb: float
    disk_pct: float
    temp_c: float | None
    net_rx_kbps: float
    net_tx_kbps: float
    xrun_total: int
    throttled: int
    alerts: list[str]


_net_last: dict[str, tuple[float, int, int]] = {}  # iface -> (timestamp, rx, tx)


async def _read_cpu_pct() -> float:
    """Real CPU usage % from /proc/stat deltas, not load average.

    Load average counts every R + D-state task on the run queue, so a
    host with many processes blocked on I/O reports load > cpu_count
    while the CPUs are idle. Two /proc/stat snapshots 200 ms apart
    give the accurate user+system / total ratio that top displays.
    """
    import time as _time

    def _read() -> tuple[int, int]:
        line = Path("/proc/stat").read_text().split("\n", 1)[0]
        parts = [int(x) for x in line.split()[1:]]
        # user nice system idle iowait irq softirq steal guest guest_nice
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle

    # Sample over 800 ms — short enough to keep the WS tick responsive
    # (system_loop fires every 5 s), long enough to smooth out the sub-
    # second bursts of polkit / systemd / wireplumber that on a Pi 3
    # would otherwise make cpu_pct flip between 1% and 90% between
    # consecutive ticks. Top uses 1 s for the same reason.
    t1, i1 = _read()
    await asyncio.sleep(0.8)
    t2, i2 = _read()
    dt, di = t2 - t1, i2 - i1
    if dt <= 0:
        return 0.0
    _ = _time  # keep import next to existing usage pattern
    return round((1.0 - di / dt) * 100.0, 1)


async def _collect_resources() -> Resources:
    import time

    # CPU — real usage from /proc/stat deltas (NOT load average, which
    # includes D-state processes and is wildly misleading on a Pi 3
    # whose USB-Ethernet adapter occasionally puts kernel threads in
    # iowait without touching CPU).
    cpu_count = os.cpu_count() or 1
    load_1m = os.getloadavg()[0]
    cpu_pct = await _read_cpu_pct()

    # Memory
    mem_total = mem_avail = swap_total = swap_used = 0
    meminfo = Path("/proc/meminfo").read_text() if Path("/proc/meminfo").exists() else ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1]) // 1024
        elif line.startswith("MemAvailable:"):
            mem_avail = int(line.split()[1]) // 1024
        elif line.startswith("SwapTotal:"):
            swap_total = int(line.split()[1]) // 1024
        elif line.startswith("SwapFree:"):
            swap_used = swap_total - int(line.split()[1]) // 1024
    mem_used = mem_total - mem_avail
    mem_pct = round(mem_used / mem_total * 100, 1) if mem_total else 0
    swap_pct = round(swap_used / swap_total * 100, 1) if swap_total else 0

    # Disk
    disk_total = disk_used = disk_pct = 0.0
    with contextlib.suppress(OSError):
        st = os.statvfs("/")
        disk_total = round(st.f_blocks * st.f_frsize / 1e9, 1)
        disk_used = round((st.f_blocks - st.f_bfree) * st.f_frsize / 1e9, 1)
        disk_pct = round(disk_used / disk_total * 100, 1) if disk_total else 0

    # Temperature (Pi)
    temp: float | None = None
    temp_raw = await _run("vcgencmd measure_temp 2>/dev/null")
    if "=" in temp_raw:
        with contextlib.suppress(ValueError):
            temp = float(temp_raw.split("=")[1].replace("'C", ""))
    if temp is None:
        # x86: thermal_zone0
        tz = Path("/sys/class/thermal/thermal_zone0/temp")
        if tz.is_file():
            with contextlib.suppress(OSError, ValueError):
                temp = round(int(tz.read_text().strip()) / 1000, 1)

    # Network throughput on the default route's interface
    rx_kbps = tx_kbps = 0.0
    iface = ""
    route = await _run("ip -4 route show default 2>/dev/null | head -1")
    if "dev" in route:
        parts = route.split()
        if "dev" in parts:
            i = parts.index("dev")
            if i + 1 < len(parts):
                iface = parts[i + 1]
    if iface:
        proc_dev = Path("/proc/net/dev")
        if proc_dev.is_file():
            for line in proc_dev.read_text().splitlines():
                if line.lstrip().startswith(f"{iface}:"):
                    cols = line.split()
                    rx_bytes = int(cols[1])
                    tx_bytes = int(cols[9])
                    now = time.monotonic()
                    prev = _net_last.get(iface)
                    if prev:
                        dt = now - prev[0]
                        if dt > 0:
                            rx_kbps = round((rx_bytes - prev[1]) * 8 / 1000 / dt, 1)
                            tx_kbps = round((tx_bytes - prev[2]) * 8 / 1000 / dt, 1)
                    _net_last[iface] = (now, rx_bytes, tx_bytes)
                    break

    # Throttle
    throttled_val, t_alerts = parse_throttled(await _run("vcgencmd get_throttled 2>/dev/null"))

    alerts = list(t_alerts)
    if mem_total and mem_avail < mem_total * 0.10:
        alerts.append(f"Low memory: {mem_avail} MB available")
    # Pair the instantaneous cpu_pct with the 1-min load average so a
    # single 800 ms burst doesn't flip the badge to DEGRADED. Alert
    # only when both are sustained.
    if cpu_pct > 90 and load_1m / cpu_count > 0.85:
        alerts.append(f"High CPU: {cpu_pct}%")
    if temp is not None and temp > 75:
        alerts.append(f"High temp: {temp}°C")
    if disk_pct > 90:
        alerts.append(f"Disk almost full: {disk_pct}%")

    # XRUN total — read pw-top cache so we don't double-spawn pw-top here
    xrun_total = 0
    try:
        from phonon_stage.pipewire import cli as _cli

        if _cli._pw_top_cache:
            xrun_total = sum(int(v.get("err", 0)) for v in _cli._pw_top_cache.values())
    except Exception:
        pass

    return Resources(
        cpu_pct=cpu_pct,
        cpu_count=cpu_count,
        cpu_load_1m=load_1m,
        mem_total_mb=mem_total,
        mem_used_mb=mem_used,
        mem_pct=mem_pct,
        swap_total_mb=swap_total,
        swap_used_mb=swap_used,
        swap_pct=swap_pct,
        disk_total_gb=disk_total,
        disk_used_gb=disk_used,
        disk_pct=disk_pct,
        temp_c=temp,
        net_rx_kbps=rx_kbps,
        net_tx_kbps=tx_kbps,
        xrun_total=xrun_total,
        throttled=throttled_val,
        alerts=alerts,
    )


@router.get("/resources", response_model=Resources)
async def get_resources() -> Resources:
    return await _collect_resources()
