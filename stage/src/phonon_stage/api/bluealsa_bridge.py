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
    # Kill any orphan bridge processes — including the wrapping `while true`
    # shell, otherwise it just respawns arecord+pacat after the sleep 0.5
    # and we've leaked another bridge.
    await _run(
        "pkill -f 'while true.*arecord.*bluealsa' 2>/dev/null; "
        "pkill -f 'while true.*parec.*bt_' 2>/dev/null; "
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
    mac: str, name: str, device_type: str, buffer_ms: int = 50, rate: int = 0
) -> dict[str, object]:
    """Create a PipeWire null sink bridged to a BlueALSA device.

    `rate` is the A2DP-negotiated sample rate from bluealsa-aplay -L.
    When 0 (legacy callers), we fall back to 48 kHz for playback and
    44.1 kHz for capture — the previous hardcoded defaults. Matching
    the negotiated rate avoids a software resample inside bluealsa
    that on a Pi 3 turns into audible crackles + xruns under load."""
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

    # Clean up any existing pactl module with EXACTLY this sink_name. Use the
    # full token (with delimiters) — a substring match would also blow away
    # a bridge whose name is a prefix of ours, e.g. cleaning "bt_1337" also
    # killed "bt_1337-2_in" when two BT phones were paired.
    target_token = (
        f"sink_name=bt_{safe_name}_in"
        if device_type == "capture"
        else f"sink_name=bt_{safe_name} "
    )
    existing_mods = await _run("pactl list modules short")
    for line in existing_mods.splitlines():
        # pactl list modules short emits the args field as a tab/space-separated
        # blob — match against the exact sink_name=… token, not substring.
        if "module-null-sink" in line and target_token in line + " ":
            # Also ensure exact: the next char after sink_name=bt_<safe_name>
            # must be ' ' or end-of-args (for capture, '_in' is part of the token)
            old_mid = line.split()[0]
            await _run(f"pactl unload-module {old_mid}")
            logger.info("bluealsa.old_module_cleaned", name=safe_name, module=old_mid)

    if device_type == "playback":
        # Use the A2DP-negotiated rate when known. JBL Xtreme 3 / Pulse 3
        # negotiate 44.1 kHz; UE Boom and most newer speakers negotiate 48
        # kHz. Wrong rate → bluealsa resamples in software and audio
        # melts on a Pi 3.
        play_rate = rate if rate > 0 else 48000
        module_id = await _run(
            f"pactl load-module module-null-sink "
            f"sink_name=bt_{safe_name} "
            f"sink_properties=device.description={safe_name}-BT "
            f"format=s16le rate={play_rate} channels=2"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}

        period = int(play_rate * buffer_ms / 1000)
        bridge_cmd = (
            f"while true; do "
            f"parec --device=bt_{safe_name}.monitor "
            f"--format=s16le --rate={play_rate} --channels=2 "
            f"--latency-msec={buffer_ms} "
            f"--property=node.dont-reconnect=true "
            f'| aplay -D "bluealsa:DEV={mac},PROFILE=a2dp" -f S16_LE -r {play_rate} -c 2 '
            f"--period-size={period} --buffer-size={period * 2} - 2>/dev/null; "
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
        # Capture bridges feed PipeWire / AES67 send, which run at 48 kHz.
        # Forcing the null-sink to 48 kHz means bluealsa does the
        # 44.1->48 resample internally inside arecord (well-tested
        # codepath), and the rest of the chain is rate-pure. Letting
        # the null-sink track the negotiated rate (44.1 kHz for SBC
        # phones) instead pushed the resample into PipeWire's graph,
        # which on a Pi 3 audibly crackles when AES67 is also active.
        cap_rate = 48000
        # For capture (phone→Pi): create a pipe-source that exposes as Audio/Source
        # arecord from bluealsa → write to FIFO → pw-cat reads FIFO as source
        # Simpler: use module-null-sink but expose the MONITOR as the usable source
        # The monitor ports have direction "output" → they appear as sources in Patch Bay
        module_id = await _run(
            f"pactl load-module module-null-sink "
            f"sink_name=bt_{safe_name}_in "
            f"sink_properties=device.description={safe_name}-BT-In "
            f"format=s16le rate={cap_rate} channels=2"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}

        # Bridge: bluealsa capture → pacat into the null sink
        # The null sink's MONITOR becomes the source in PipeWire
        period = int(cap_rate * buffer_ms / 1000)
        bridge_cmd = (
            f"while true; do "
            f'arecord -D "bluealsa:DEV={mac},PROFILE=a2dp" '
            f"-f S16_LE -r {cap_rate} -c 2 "
            f"--period-size={period} --buffer-size={period * 2} - "
            f"2>/dev/null "
            f"| pacat --device=bt_{safe_name}_in --format=s16le --rate={cap_rate} --channels=2 "
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


async def sync_bridges_impl(buffer_ms: int = 50) -> dict[str, object]:
    """Reconcile bluealsa bridges with the current BlueALSA PCM list:
    create missing bridges, drop stale ones. Public so other modules
    (e.g. system.control_service after a bluealsa restart, BT connect)
    can trigger a sync without going through HTTP."""
    pcms = await _list_bluealsa_pcms()
    results = []
    active_keys: set[str] = set()

    for pcm in pcms:
        mac = pcm.get("mac", "")
        name = pcm.get("name", mac)
        dtype = pcm.get("type", "")
        if not mac or not dtype:
            continue
        # bluealsa-aplay -L parser stores rate as a string ('44100' / '48000');
        # fall back to 0 so create_bridge picks the legacy default per type.
        try:
            negotiated_rate = int(pcm.get("rate", 0) or 0)
        except (TypeError, ValueError):
            negotiated_rate = 0
        active_keys.add(f"{mac}_{dtype}")
        result = await create_bridge(mac, name, dtype, buffer_ms, rate=negotiated_rate)
        results.append(result)

    stale_keys = set(_active_bridges.keys()) - active_keys
    for key in stale_keys:
        parts = key.rsplit("_", 1)
        if len(parts) == 2:
            await destroy_bridge(parts[0], parts[1])
            results.append({"status": "removed_stale", "mac": parts[0], "type": parts[1]})

    return {"bridges": results, "active": len(_active_bridges)}


@router.post("/sync")
async def sync_bridges(request: Request, buffer_ms: int = 50) -> dict[str, object]:
    """Auto-create bridges for all connected BlueALSA devices."""
    return await sync_bridges_impl(buffer_ms=buffer_ms)


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


@router.get("/health")
async def bridge_health() -> dict[str, object]:
    """Per-active-bridge health snapshot — codec, sample rate, PipeWire
    xruns on the bt_<name> sink, and bridge process aliveness. Used by
    the UI to debug audio crackles: if xruns climb on a given bridge
    you've got buffer starvation; if codec is SBC at low bitpool you
    can hint at lossy a2dp; if pid_alive is False the bridge shell
    crashed and audio's just silently dead."""
    import contextlib
    import os

    from phonon_stage.pipewire.cli import pw_top_xruns

    # Snapshot of negotiated codec & rate per (MAC, type)
    pcms = await _list_bluealsa_pcms()
    by_key = {f"{p['mac']}_{p['type']}": p for p in pcms}

    # Snapshot of pw-top xrun counters keyed by node name. The bridge
    # creates a null-sink named `bt_<safe_name>` (or `_in` for capture)
    # that the user routes via mappings — that's the node that takes
    # the hit when the audio buffer underflows.
    xruns_by_name: dict[str, int] = {}
    try:
        for entry in (await pw_top_xruns()).values():
            n = str(entry.get("name", ""))
            if n.startswith("bt_"):
                xruns_by_name[n] = int(entry.get("err", 0))
    except Exception:
        pass

    bridges_health: list[dict[str, object]] = []
    for key, br in _active_bridges.items():
        mac, dtype = key.rsplit("_", 1)
        sink_name = f"bt_{br.get('name', '')}" + ("_in" if dtype == "capture" else "")
        pcm = by_key.get(key, {})
        pid_raw = br.get("bridge_pid")
        alive = False
        if pid_raw is not None:
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                os.kill(int(str(pid_raw)), 0)
                alive = True
        bridges_health.append(
            {
                "mac": mac,
                "type": dtype,
                "name": br.get("name", ""),
                "sink_name": sink_name,
                "codec": pcm.get("codec", ""),
                "rate": pcm.get("rate", ""),
                "channels": pcm.get("channels", ""),
                "bridge_buffer_ms": int(str(br.get("buffer_ms", 50))),
                "codec_latency_ms": int(pcm.get("codec_latency_ms", 0) or 0),
                "xruns_total": xruns_by_name.get(sink_name, 0),
                "pid_alive": alive,
                "pid": int(str(pid_raw)) if pid_raw is not None else 0,
            }
        )

    return {"bridges": bridges_health, "count": len(bridges_health)}
