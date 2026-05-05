"""BlueALSA → PipeWire bridge management.

Creates PipeWire null sinks/sources for connected Bluetooth devices
so they appear in the Patch Bay and can be used in mappings.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

import structlog
from fastapi import APIRouter, Request

router = APIRouter(prefix="/bluealsa", tags=["bluealsa"])

logger = structlog.get_logger()

# Track active bridges: MAC -> { module_id, bridge_pid, type, name }
_active_bridges: dict[str, dict[str, object]] = {}


async def cleanup_stale_bridges() -> None:
    """Remove any null-sink modules and bridge processes left from a previous run."""
    # Clear in-memory registry
    _active_bridges.clear()
    # Unload all bt_ null-sink modules
    raw = await _run("pactl list modules short")
    for line in raw.splitlines():
        if "module-null-sink" in line and "bt_" in line:
            mid = line.split()[0]
            await _run(f"pactl unload-module {mid}")
            logger.info("bluealsa.stale_bridge_cleaned", module=mid)
    # Kill any orphan bridge processes
    await _run(
        "pkill -f 'parec.*bt_' 2>/dev/null; pkill -f 'aplay.*bluealsa' 2>/dev/null; "
        "pkill -f 'arecord.*bluealsa' 2>/dev/null; pkill -f 'pacat.*bt_' 2>/dev/null"
    )


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
    """List connected BlueALSA devices, deduplicated by MAC+type."""
    raw = await _run("bluealsa-aplay --list-pcms 2>&1")
    seen: dict[str, dict[str, str]] = {}

    current_mac = ""
    current_entry: dict[str, str] = {}

    for line in raw.splitlines():
        if line.startswith("bluealsa:"):
            # Save previous
            if current_mac and current_entry.get("type"):
                key = f"{current_mac}_{current_entry['type']}"
                if key not in seen:
                    seen[key] = current_entry

            current_entry = {"pcm": line}
            current_mac = ""
            for part in line.split(","):
                # bluez-alsa 4.0 emits SRV=...,DEV=... ; 4.1+ emits bluealsa:DEV=...
                # Match DEV= anywhere in the part (after stripping prefix)
                if "DEV=" in part:
                    current_mac = part.split("DEV=", 1)[1]
                    current_entry["mac"] = current_mac
        elif current_entry and line.strip():
            stripped = line.strip()
            if ", playback" in stripped:
                current_entry["type"] = "playback"
                current_entry["name"] = stripped.split(",")[0].strip()
            elif ", capture" in stripped:
                current_entry["type"] = "capture"
                current_entry["name"] = stripped.split(",")[0].strip()
            elif "A2DP" in stripped or "SCO" in stripped:
                # e.g. "A2DP (SBC): S16_LE 2 channels 48000 Hz"
                current_entry["codec_info"] = stripped
                if "(" in stripped and ")" in stripped:
                    current_entry["codec"] = stripped.split("(")[1].split(")")[0]
                if "channels" in stripped:
                    parts = stripped.split()
                    for i, p in enumerate(parts):
                        if p == "channels" and i > 0:
                            current_entry["channels"] = parts[i - 1]
                        elif p == "Hz" and i > 0:
                            current_entry["rate"] = parts[i - 1]

    # Don't forget the last one
    if current_mac and current_entry.get("type"):
        key = f"{current_mac}_{current_entry['type']}"
        if key not in seen:
            seen[key] = current_entry

    # Estimate latency per codec
    codec_latency_ms = {
        "SBC": 150,
        "AAC": 120,
        "aptX": 70,
        "aptX HD": 130,
        "aptX-LL": 32,
        "LDAC": 200,
        "LC3": 30,
    }
    for entry in seen.values():
        codec = entry.get("codec", "SBC")
        bt_latency = codec_latency_ms.get(codec, 150)
        # Total = codec latency + bridge buffer (estimated from active bridges)
        bridge_buf = 50  # default
        entry["codec_latency_ms"] = str(bt_latency)
        entry["bridge_buffer_ms"] = str(bridge_buf)
        entry["total_latency_ms"] = str(bt_latency + bridge_buf)

    return list(seen.values())


async def create_bridge(
    mac: str, name: str, device_type: str, buffer_ms: int = 50
) -> dict[str, object]:
    """Create a PipeWire null sink bridged to a BlueALSA device."""
    safe_name = name.replace(" ", "-").replace("(", "").replace(")", "")
    key = f"{mac}_{device_type}"

    if key in _active_bridges:
        # Check if bridge process is still alive
        existing = _active_bridges[key]
        pid = existing.get("bridge_pid")
        if pid is not None:
            try:
                os.kill(int(str(pid)), 0)  # Signal 0 = check if alive
                return {"status": "already_exists", "mac": mac, "name": safe_name}
            except (ProcessLookupError, OSError):
                # Process dead, clean up and recreate
                module_id = existing.get("module_id")
                if module_id is not None:
                    await _run(f"pactl unload-module {module_id}")
                del _active_bridges[key]
                logger.warning("bluealsa.bridge_dead_recreating", mac=mac, btype=device_type)

    # Clean up any existing pactl modules with same sink_name
    existing_mods = await _run("pactl list modules short")
    for line in existing_mods.splitlines():
        if f"bt_{safe_name}" in line and "module-null-sink" in line:
            old_mid = line.split()[0]
            await _run(f"pactl unload-module {old_mid}")
            logger.info("bluealsa.old_module_cleaned", name=safe_name, module=old_mid)

    if device_type == "playback":
        module_id = await _run(
            f"pactl load-module module-null-sink "
            f"sink_name=bt_{safe_name} "
            f"sink_properties=device.description={safe_name}-BT "
            f"format=s16le rate=48000 channels=2"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}

        period_48 = int(48000 * buffer_ms / 1000)
        bridge_cmd = (
            f"while true; do "
            f"parec --device=bt_{safe_name}.monitor --format=s16le --rate=48000 --channels=2 "
            f"--latency-msec={buffer_ms} "
            f"--property=node.dont-reconnect=true "
            f'| aplay -D "bluealsa:DEV={mac},PROFILE=a2dp" -f S16_LE -r 48000 -c 2 '
            f"--period-size={period_48} --buffer-size={period_48 * 2} - 2>/dev/null; "
            f"sleep 0.5; done"
        )
        proc = await asyncio.create_subprocess_shell(
            bridge_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _active_bridges[key] = {
            "module_id": int(module_id.strip()),
            "bridge_pid": proc.pid,
            "type": "playback",
            "name": safe_name,
        }
        logger.info("bluealsa.bridge_created", mac=mac, name=safe_name, btype="playback")

    elif device_type == "capture":
        # For capture (phone→Pi): create a pipe-source that exposes as Audio/Source
        # arecord from bluealsa → write to FIFO → pw-cat reads FIFO as source
        # Simpler: use module-null-sink but expose the MONITOR as the usable source
        # The monitor ports have direction "output" → they appear as sources in Patch Bay
        module_id = await _run(
            f"pactl load-module module-null-sink "
            f"sink_name=bt_{safe_name}_in "
            f"sink_properties=device.description={safe_name}-BT-In "
            f"format=s16le rate=44100 channels=2"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}

        # Bridge: bluealsa capture → pacat into the null sink
        # The null sink's MONITOR becomes the source in PipeWire
        period_44 = int(44100 * buffer_ms / 1000)
        # Use --duration=0 and let the while loop handle restart
        # sleep 0.5 for fast restart after stream ends
        bridge_cmd = (
            f"while true; do "
            f'arecord -D "bluealsa:DEV={mac},PROFILE=a2dp" '
            f"-f S16_LE -r 44100 -c 2 "
            f"--period-size={period_44} --buffer-size={period_44 * 2} - "
            f"2>/dev/null "
            f"| pacat --device=bt_{safe_name}_in --format=s16le --rate=44100 --channels=2 "
            f"--latency-msec={buffer_ms} "
            # Prevent WirePlumber session manager from auto-routing this stream
            # to the default sink (otherwise audio leaks to speakers in addition
            # to the intended null-sink target).
            f"--property=node.dont-reconnect=true "
            f"--property=node.passive=true; "
            f"sleep 0.5; done"
        )
        proc = await asyncio.create_subprocess_shell(
            bridge_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _active_bridges[key] = {
            "module_id": int(module_id.strip()),
            "bridge_pid": proc.pid,
            "type": "capture",
            "name": safe_name,
        }
        logger.info("bluealsa.bridge_created", mac=mac, name=safe_name, btype="capture")

    return {"status": "created", "mac": mac, "name": safe_name, "type": device_type}


async def destroy_bridge(mac: str, device_type: str) -> dict[str, str]:
    """Destroy a BlueALSA → PipeWire bridge."""
    key = f"{mac}_{device_type}"
    bridge = _active_bridges.pop(key, None)
    if not bridge:
        return {"status": "not_found", "mac": mac}

    pid = bridge.get("bridge_pid")
    if pid is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(str(pid)), signal.SIGTERM)

    module_id = bridge.get("module_id")
    if module_id is not None:
        await _run(f"pactl unload-module {module_id}")

    logger.info("bluealsa.bridge_destroyed", mac=mac, btype=device_type)
    return {"status": "destroyed", "mac": mac}


@router.post("/sync")
async def sync_bridges(request: Request, buffer_ms: int = 50) -> dict[str, object]:
    """Auto-create bridges for all connected BlueALSA devices."""
    pcms = await _list_bluealsa_pcms()
    results = []
    active_keys: set[str] = set()

    for pcm in pcms:
        mac = pcm.get("mac", "")
        name = pcm.get("name", mac)
        dtype = pcm.get("type", "")
        if not mac or not dtype:
            continue
        active_keys.add(f"{mac}_{dtype}")
        result = await create_bridge(mac, name, dtype, buffer_ms)
        results.append(result)

    # Destroy bridges for disconnected devices
    stale_keys = set(_active_bridges.keys()) - active_keys
    for key in stale_keys:
        parts = key.rsplit("_", 1)
        if len(parts) == 2:
            await destroy_bridge(parts[0], parts[1])
            results.append({"status": "removed_stale", "mac": parts[0], "type": parts[1]})

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


@router.get("/devices")
async def list_bt_audio_devices() -> list[dict[str, str]]:
    """List connected BT audio devices with codec/rate info from BlueALSA."""
    return await _list_bluealsa_pcms()
