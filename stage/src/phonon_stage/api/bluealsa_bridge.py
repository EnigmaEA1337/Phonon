"""BlueALSA → PipeWire bridge management.

Creates PipeWire null sinks/sources for connected Bluetooth devices
so they appear in the Patch Bay and can be used in mappings.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from fastapi import APIRouter, Request

router = APIRouter(prefix="/bluealsa", tags=["bluealsa"])

logger = structlog.get_logger()

# Track active bridges: MAC -> { module_id, bridge_pid, type }
_active_bridges: dict[str, dict[str, object]] = {}


async def _run(cmd: str, timeout: float = 5.0) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode().strip()
    except Exception:
        return ""


async def _list_bluealsa_pcms() -> list[dict[str, str]]:
    """List connected BlueALSA devices with their type (playback/capture)."""
    raw = await _run("bluealsa-aplay --list-pcms 2>&1")
    devices: list[dict[str, str]] = []
    current: dict[str, str] = {}

    for line in raw.splitlines():
        if line.startswith("bluealsa:"):
            if current:
                devices.append(current)
            # Parse: bluealsa:SRV=org.bluealsa,DEV=40:C1:F6:FC:5A:02,PROFILE=a2dp
            current = {"pcm": line}
            parts = line.split(",")
            for p in parts:
                if p.startswith("DEV="):
                    current["mac"] = p.split("=")[1]
                elif p.startswith("PROFILE="):
                    current["profile"] = p.split("=")[1]
        elif current and line.strip():
            stripped = line.strip()
            if ", playback" in stripped:
                current["type"] = "playback"
                current["name"] = stripped.split(",")[0].strip()
            elif ", capture" in stripped:
                current["type"] = "capture"
                current["name"] = stripped.split(",")[0].strip()
            elif "A2DP" in stripped:
                current["codec_info"] = stripped

    if current:
        devices.append(current)
    return devices


async def create_bridge(mac: str, name: str, device_type: str) -> dict[str, object]:
    """Create a PipeWire null sink/source bridged to a BlueALSA device."""
    safe_name = name.replace(" ", "-").replace("(", "").replace(")", "")
    key = f"{mac}_{device_type}"

    if key in _active_bridges:
        return {"status": "already_exists", "mac": mac, "name": safe_name}

    if device_type == "playback":
        # Create null sink → parec monitor → aplay bluealsa
        module_id = await _run(
            f"pactl load-module module-null-sink "
            f"sink_name=bt_{safe_name} "
            f"sink_properties=device.description={safe_name}-BT "
            f"format=s16le rate=48000 channels=2"
        )
        if not module_id or not module_id.isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}

        # Start bridge process
        bridge_cmd = (
            f"parec --device=bt_{safe_name}.monitor --format=s16le --rate=48000 --channels=2 "
            f'| aplay -D "bluealsa:DEV={mac},PROFILE=a2dp" -f S16_LE -r 48000 -c 2 -'
        )
        proc = await asyncio.create_subprocess_shell(
            bridge_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _active_bridges[key] = {
            "module_id": int(module_id),
            "bridge_pid": proc.pid,
            "type": "playback",
            "name": safe_name,
        }
        logger.info(
            "bluealsa.bridge_created", mac=mac, name=safe_name, type="playback", module=module_id
        )

    elif device_type == "capture":
        # Create null source ← arecord bluealsa → pacat
        module_id = await _run(
            f"pactl load-module module-null-sink "
            f"sink_name=bt_{safe_name}_raw "
            f"sink_properties=device.description={safe_name}-BT "
            f"format=s16le rate=44100 channels=2"
        )
        if not module_id or not module_id.isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}

        # Bridge: arecord from bluealsa → pacat to the null sink
        bridge_cmd = (
            f'arecord -D "bluealsa:DEV={mac},PROFILE=a2dp" -f S16_LE -r 44100 -c 2 - '
            f"| pacat --device=bt_{safe_name}_raw --format=s16le --rate=44100 --channels=2"
        )
        proc = await asyncio.create_subprocess_shell(
            bridge_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _active_bridges[key] = {
            "module_id": int(module_id),
            "bridge_pid": proc.pid,
            "type": "capture",
            "name": safe_name,
        }
        logger.info(
            "bluealsa.bridge_created", mac=mac, name=safe_name, type="capture", module=module_id
        )

    return {"status": "created", "mac": mac, "name": safe_name, "type": device_type}


async def destroy_bridge(mac: str, device_type: str) -> dict[str, str]:
    """Destroy a BlueALSA → PipeWire bridge."""
    import os
    import signal

    key = f"{mac}_{device_type}"
    bridge = _active_bridges.pop(key, None)
    if not bridge:
        return {"status": "not_found", "mac": mac}

    # Kill bridge process
    pid = bridge.get("bridge_pid")
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(str(pid)), signal.SIGTERM)

    # Unload PipeWire module
    module_id = bridge.get("module_id")
    if module_id:
        await _run(f"pactl unload-module {module_id}")

    logger.info("bluealsa.bridge_destroyed", mac=mac, type=device_type)
    return {"status": "destroyed", "mac": mac}


@router.post("/sync")
async def sync_bridges(request: Request) -> dict[str, object]:
    """Auto-create bridges for all connected BlueALSA devices."""
    pcms = await _list_bluealsa_pcms()
    results = []
    active_keys = set()

    for pcm in pcms:
        mac = pcm.get("mac", "")
        name = pcm.get("name", mac)
        dtype = pcm.get("type", "")
        if not mac or not dtype:
            continue
        active_keys.add(f"{mac}_{dtype}")
        result = await create_bridge(mac, name, dtype)
        results.append(result)

    # Destroy bridges for disconnected devices
    stale_keys = set(_active_bridges.keys()) - active_keys
    for key in stale_keys:
        mac, dtype = key.rsplit("_", 1)
        await destroy_bridge(mac, dtype)
        results.append({"status": "removed_stale", "mac": mac, "type": dtype})

    return {"bridges": results, "active": len(_active_bridges)}


@router.get("/bridges")
async def list_bridges() -> dict[str, object]:
    """List active BlueALSA bridges."""
    return {
        "bridges": [
            {"mac": k.rsplit("_", 1)[0], "type": v["type"], "name": v["name"]}
            for k, v in _active_bridges.items()
        ]
    }
