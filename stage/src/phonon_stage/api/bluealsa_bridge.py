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
    """Bridge a BlueALSA PCM into PipeWire as a native ALSA-backed node.

    Replaces the previous shell-wrapper approach (`while true; arecord
    | pacat`) with a single PipeWire-managed module-alsa-source / -sink
    that wraps the bluealsa: PCM directly. Eliminates:
      - the shell wrapper process and its `sleep 0.5` respawn loop
      - the per-bridge fork/exec of arecord+pacat (or parec+aplay)
      - the WirePlumber graph re-routing every time those clients
        connect/disconnect — the dominant CPU cost on the Pi 3 sender

    `rate` is the A2DP-negotiated rate from bluealsa-aplay -L; when 0
    we fall back to 48 kHz playback / 48 kHz capture (capture is
    pinned at 48 kHz so PipeWire's graph stays rate-pure for the
    AES67 send hop).
    """
    safe_name = name.replace(" ", "-").replace("(", "").replace(")", "")
    key = f"{mac}_{device_type}"

    if key in _active_bridges:
        existing = _active_bridges[key]
        # The module-id is enough to verify the bridge is still loaded —
        # there's no shell wrapper process to ping anymore. If pactl
        # reports the module gone, recreate.
        module_id = existing.get("module_id")
        if module_id is not None:
            mods = await _run("pactl list modules short")
            if any(line.startswith(f"{module_id}\t") for line in mods.splitlines()):
                return {"status": "already_exists", "mac": mac, "name": safe_name}
            del _active_bridges[key]
            logger.warning("bluealsa.bridge_module_gone_recreating", mac=mac, btype=device_type)

    # Sweep any leftover pactl module with this exact sink/source name —
    # protects against a stale entry from a previous daemon run that
    # left the module loaded but lost its in-memory tracking.
    sink_or_source = "source_name" if device_type == "capture" else "sink_name"
    name_token = f"bt_{safe_name}_in" if device_type == "capture" else f"bt_{safe_name}"
    existing_mods = await _run("pactl list modules short")
    for line in existing_mods.splitlines():
        # Match the exact name=token to avoid 'bt_1337' wiping 'bt_1337-2_in'.
        if (
            ("module-alsa-source" in line or "module-alsa-sink" in line)
            and (f"{sink_or_source}={name_token}" in line)
        ):
            old_mid = line.split()[0]
            await _run(f"pactl unload-module {old_mid}")
            logger.info("bluealsa.old_module_cleaned", name=safe_name, module=old_mid)

    bluealsa_pcm = f"bluealsa:DEV={mac},PROFILE=a2dp"

    if device_type == "playback":
        # Phonon → BT speaker. The A2DP-negotiated rate matters: JBL
        # Xtreme 3 / Pulse 3 = 44.1 kHz, UE Boom = 48 kHz. Wrong rate
        # forces bluealsa to resample in software and on a Pi 3 the
        # CPU spike crackles. module-alsa-sink replaces both the
        # null-sink AND the parec|aplay shell pipeline — PipeWire owns
        # the read-from-monitor + write-to-bluealsa loop natively.
        play_rate = rate if rate > 0 else 48000
        period = max(64, int(play_rate * buffer_ms / 1000))
        module_id = await _run(
            "pactl load-module module-alsa-sink "
            f'sink_name=bt_{safe_name} '
            f'sink_properties=device.description="{safe_name}-BT" '
            f'device="{bluealsa_pcm}" '
            f"rate={play_rate} channels=2 format=s16le "
            f"fragment_size={period}"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}
        _active_bridges[key] = {
            "module_id": int(module_id.strip()),
            "type": "playback",
            "name": safe_name,
            "rate": play_rate,
            "buffer_ms": buffer_ms,
        }
        logger.info("bluealsa.bridge_created", mac=mac, name=safe_name, btype="playback")

    elif device_type == "capture":
        # BT phone → Phonon. Pin to 48 kHz so PipeWire graph (and the
        # AES67 send module reading from this source) stays rate-pure.
        # bluealsa's internal arecord-driven 44.1 → 48 resample is well-
        # tested. module-alsa-source emits a regular Audio/Source node
        # whose ports plug straight into mappings.
        cap_rate = 48000
        period = max(64, int(cap_rate * buffer_ms / 1000))
        module_id = await _run(
            "pactl load-module module-alsa-source "
            f'source_name=bt_{safe_name}_in '
            f'source_properties=device.description="{safe_name}-BT-In" '
            f'device="{bluealsa_pcm}" '
            f"rate={cap_rate} channels=2 format=s16le "
            f"fragment_size={period}"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}
        _active_bridges[key] = {
            "module_id": int(module_id.strip()),
            "type": "capture",
            "name": safe_name,
            "rate": cap_rate,
            "buffer_ms": buffer_ms,
        }
        logger.info("bluealsa.bridge_created", mac=mac, name=safe_name, btype="capture")

    return {"status": "created", "mac": mac, "name": safe_name, "type": device_type}


async def destroy_bridge(mac: str, device_type: str) -> dict[str, str]:
    """Destroy a BlueALSA → PipeWire bridge."""
    key = f"{mac}_{device_type}"
    bridge = _active_bridges.pop(key, None)
    if not bridge:
        return {"status": "not_found", "mac": mac}

    # Legacy bridges had a 'bridge_pid' from the shell wrapper era.
    # New module-alsa-source/sink bridges only own a pactl module id.
    # Kill the wrapper if present (no-op for new bridges) AND unload
    # the pactl module — covers both formats during the transition.
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

    # New bridges (module-alsa-source/sink) are alive iff their pactl
    # module id is still loaded — no shell wrapper PID to ping. Snapshot
    # loaded modules once and look up by id.
    loaded_modules_raw = await _run("pactl list modules short")
    loaded_module_ids = {
        line.split("\t", 1)[0]
        for line in loaded_modules_raw.splitlines()
        if line.strip()
    }

    bridges_health: list[dict[str, object]] = []
    for key, br in _active_bridges.items():
        mac, dtype = key.rsplit("_", 1)
        sink_name = f"bt_{br.get('name', '')}" + ("_in" if dtype == "capture" else "")
        pcm = by_key.get(key, {})
        # Aliveness: legacy = wrapper shell pid still running; new =
        # pactl module still loaded. Either signal proves the bridge
        # is wired.
        pid_raw = br.get("bridge_pid")
        module_id = br.get("module_id")
        alive = False
        if pid_raw is not None:
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                os.kill(int(str(pid_raw)), 0)
                alive = True
        if not alive and module_id is not None:
            alive = str(module_id) in loaded_module_ids
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
