"""BlueALSA → PipeWire bridge management.

Creates PipeWire null sinks/sources for connected Bluetooth devices
so they appear in the Patch Bay and can be used in mappings.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/bluealsa", tags=["bluealsa"])

logger = structlog.get_logger()

# Track active bridges: MAC -> { module_id, bridge_pid, type, name }
_active_bridges: dict[str, dict[str, object]] = {}

# -- Bridge per-channel settings store ---------------------------------------
# Persists user overrides for rate / period / channels / format / codec
# preference per (mac, dtype). Defaults are computed from the BT-negotiated
# values; the store only holds *deltas* the user explicitly set, so a Reset
# is just a removal.

_BRIDGE_SETTINGS_PATH = Path("/var/lib/phonon/bridge_settings.json")
_settings_cache: dict[str, dict[str, Any]] = {}

# Allowed values — keep narrow so the UI radio buttons map cleanly to the
# backend without us having to validate arbitrary strings every PUT.
_ALLOWED_RATES = (44100, 48000)
_ALLOWED_PERIOD_MS = (10, 25, 50, 100, 200)
_ALLOWED_CHANNELS = (1, 2)
_ALLOWED_FORMATS = ("s16le", "s24_3le", "s32le")

# arecord(1) wants its format names in a different style than pacat / pactl:
# 's16le' → 'S16_LE', 's24_3le' → 'S24_3LE', etc. Hardcoded map > parse-and-
# rebuild because the underscore placement isn't deterministic.
_ARECORD_FORMAT = {
    "s16le": "S16_LE",
    "s24_3le": "S24_3LE",
    "s32le": "S32_LE",
}


def _settings_key(mac: str, dtype: str) -> str:
    return f"{mac.upper()}_{dtype}"


def _load_settings_from_disk() -> dict[str, dict[str, Any]]:
    if not _BRIDGE_SETTINGS_PATH.exists():
        return {}
    try:
        raw = json.loads(_BRIDGE_SETTINGS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("bluealsa.settings_load_failed", path=str(_BRIDGE_SETTINGS_PATH))
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}


def _save_settings_to_disk(data: dict[str, dict[str, Any]]) -> None:
    _BRIDGE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _BRIDGE_SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    with contextlib.suppress(OSError):
        tmp.chmod(0o600)
    tmp.replace(_BRIDGE_SETTINGS_PATH)


def init_settings_store() -> None:
    """Load overrides from disk into the in-memory cache. Call once on startup."""
    global _settings_cache
    _settings_cache = _load_settings_from_disk()
    logger.info("bluealsa.settings_loaded", count=len(_settings_cache))


def get_bridge_override(mac: str, dtype: str) -> dict[str, Any]:
    """Return the override dict for one (mac, dtype). Empty dict = no override."""
    return dict(_settings_cache.get(_settings_key(mac, dtype), {}))


def set_bridge_override(mac: str, dtype: str, override: dict[str, Any]) -> None:
    """Persist override. Pass an empty dict to clear."""
    key = _settings_key(mac, dtype)
    if override:
        _settings_cache[key] = dict(override)
    else:
        _settings_cache.pop(key, None)
    _save_settings_to_disk(_settings_cache)


async def cleanup_stale_bridges() -> None:
    """Remove any leftover bridge modules and shell-wrapper processes from a
    previous run. Covers all three module flavors that have shipped:
      * module-null-sink — capture path (current) + legacy playback
      * module-alsa-sink — playback path (current)
      * module-alsa-source — capture path (transitional, no longer used)
    Then SIGTERMs every shell wrapper that could still be respawning a pipe."""
    # Clear in-memory registry
    _active_bridges.clear()
    raw = await _run("pactl list modules short")
    for line in raw.splitlines():
        is_bt_module = (
            ("module-null-sink" in line and "bt_" in line)
            or ("module-alsa-sink" in line and "bt_" in line)
            or ("module-alsa-source" in line and "bt_" in line)
        )
        if is_bt_module:
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


# -- bluealsa-cli codec helpers ----------------------------------------------
# We talk to bluealsa via its CLI (bluealsa-cli), which exposes the dbus
# control surface. Each PCM has a path like
#   /org/bluealsa/hci0/dev_AA_BB_CC_DD_EE_FF/a2dpsink/source
# `a2dpsink` (BT role) appears for capture (phone → us); `a2dpsource` for
# playback (us → speaker). The trailing component (`source`/`sink`) is the
# direction from bluealsa's POV. We resolve the path from MAC + dtype, then
# parse `bluealsa-cli info <path>` for `Selected codec` and `Available codecs`.


async def _bluealsa_pcm_path(mac: str, dtype: str) -> str:
    """Resolve the bluealsa dbus path for one (mac, dtype). Empty on failure."""
    raw = await _run("bluealsa-cli list-pcms 2>&1")
    target_mac = mac.replace(":", "_").upper()
    # capture (phone→us) = bluealsa role a2dp-sink, mode=source
    # playback (us→speaker) = bluealsa role a2dp-source, mode=sink
    role_token = "a2dpsink" if dtype == "capture" else "a2dpsource"
    mode_suffix = "/source" if dtype == "capture" else "/sink"
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("/org/bluealsa"):
            continue
        if target_mac in line.upper() and role_token in line and line.endswith(mode_suffix):
            return line
    return ""


async def get_codec_info(mac: str, dtype: str) -> dict[str, Any]:
    """Return {selected, available[]} for the BT codec. Empty on failure."""
    path = await _bluealsa_pcm_path(mac, dtype)
    if not path:
        return {"selected": "", "available": []}
    raw = await _run(f"bluealsa-cli info '{path}' 2>&1")
    selected = ""
    available: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(r"^Selected codec:\s*(\S+)", line)
        if m:
            selected = m.group(1)
            continue
        m = re.match(r"^Available codecs:\s*(.+)$", line)
        if m:
            # Tokens look like "SBC[*] AAC" — strip the [*] selection marker.
            for tok in m.group(1).split():
                clean = tok.split("[", 1)[0].strip()
                if clean:
                    available.append(clean)
    return {"selected": selected, "available": available}


async def set_codec(mac: str, dtype: str, codec: str) -> bool:
    """Force a specific codec for the (mac, dtype) PCM. Returns True on success."""
    path = await _bluealsa_pcm_path(mac, dtype)
    if not path:
        return False
    out = await _run(f"bluealsa-cli codec '{path}' '{codec}' 2>&1")
    # bluealsa-cli is mostly silent on success; non-empty output usually = error.
    if "error" in out.lower() or "fail" in out.lower():
        logger.warning("bluealsa.set_codec_failed", mac=mac, codec=codec, output=out)
        return False
    logger.info("bluealsa.codec_set", mac=mac, dtype=dtype, codec=codec)
    return True


def _resolve_effective_params(
    override: dict[str, Any],
    negotiated_rate: int,
    default_buffer_ms: int,
) -> dict[str, Any]:
    """Combine override + negotiated values into the params we'll pass to pactl."""
    rate_override = override.get("rate")
    if isinstance(rate_override, int) and rate_override in _ALLOWED_RATES:
        rate = rate_override
    else:
        rate = negotiated_rate if negotiated_rate > 0 else 48000

    period_override = override.get("period_ms")
    if isinstance(period_override, int) and period_override in _ALLOWED_PERIOD_MS:
        period_ms = period_override
    else:
        period_ms = default_buffer_ms

    channels_override = override.get("channels")
    if isinstance(channels_override, int) and channels_override in _ALLOWED_CHANNELS:
        channels = channels_override
    else:
        channels = 2

    format_override = override.get("format")
    if isinstance(format_override, str) and format_override in _ALLOWED_FORMATS:
        audio_format = format_override
    else:
        audio_format = "s16le"

    return {
        "rate": rate,
        "period_ms": period_ms,
        "channels": channels,
        "format": audio_format,
    }


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
        # Compare the bridge's stored rate against the rate we'd USE if we
        # were creating fresh right now (override > negotiated > 48000).
        # Naive "stored vs negotiated" compares apples to oranges: if the
        # user set an override forcing 48000 and the phone negotiates
        # 44100, every sync detects "stored 48000 ≠ negotiated 44100",
        # recreates with override applied → 48000 again → infinite recreate
        # loop. Resolving effective on both sides means the rate-change
        # path only fires when the bridge would actually come up different.
        stored_rate = int(existing.get("rate", 0) or 0)
        next_override = get_bridge_override(mac, device_type)
        next_rate = _resolve_effective_params(next_override, rate, buffer_ms)["rate"]
        if stored_rate > 0 and next_rate != stored_rate:
            logger.info(
                "bluealsa.bridge_rate_changed_recreating",
                mac=mac,
                btype=device_type,
                old_rate=stored_rate,
                new_rate=next_rate,
            )
            await destroy_bridge(mac, device_type)
        else:
            # Two liveness signals to check:
            #   * pactl module loaded (covers both playback module-alsa-sink
            #     AND capture null-sink — both go through pactl)
            #   * bridge_pid alive (capture wrapper only — None for sinks)
            # If EITHER signal is dead we must recreate. Old code only
            # looked at the module: when the wrapper crashed (or got
            # killed during diagnostics), the null-sink module stayed
            # loaded so 'already_exists' kept lying while the pacat that
            # actually feeds it was gone — bridge looked alive but
            # produced silence forever.
            module_id = existing.get("module_id")
            module_alive = False
            if module_id is not None:
                mods = await _run("pactl list modules short")
                module_alive = any(line.startswith(f"{module_id}\t") for line in mods.splitlines())
            pid_raw = existing.get("bridge_pid")
            pid_alive = True  # if no PID stored (sink path), don't fail on it
            if pid_raw is not None:
                pid_alive = False
                with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                    os.kill(int(str(pid_raw)), 0)
                    pid_alive = True
            if module_alive and pid_alive:
                return {"status": "already_exists", "mac": mac, "name": safe_name}
            # One side died — destroy_bridge cleans up whatever's left
            # (kills any stale process group, unloads the module) so we
            # start fresh below.
            await destroy_bridge(mac, device_type)
            logger.warning(
                "bluealsa.bridge_dead_recreating",
                mac=mac,
                btype=device_type,
                module_alive=module_alive,
                pid_alive=pid_alive,
            )

    # Sweep any leftover pactl module with this exact sink/source name —
    # protects against a stale entry from a previous daemon run that
    # left the module loaded but lost its in-memory tracking.
    sink_or_source = "source_name" if device_type == "capture" else "sink_name"
    name_token = f"bt_{safe_name}_in" if device_type == "capture" else f"bt_{safe_name}"
    existing_mods = await _run("pactl list modules short")
    for line in existing_mods.splitlines():
        # Match the exact name=token to avoid 'bt_1337' wiping 'bt_1337-2_in'.
        if ("module-alsa-source" in line or "module-alsa-sink" in line) and (
            f"{sink_or_source}={name_token}" in line
        ):
            old_mid = line.split()[0]
            await _run(f"pactl unload-module {old_mid}")
            logger.info("bluealsa.old_module_cleaned", name=safe_name, module=old_mid)

    bluealsa_pcm = f"bluealsa:DEV={mac},PROFILE=a2dp"

    override = get_bridge_override(mac, device_type)

    # Apply codec preference BEFORE the bridge is created — bluealsa
    # renegotiates the BT codec, which changes the negotiated rate / format.
    # We only call set_codec when the user picked something other than auto
    # AND it differs from the currently selected codec, to avoid pointless
    # BT renegotiation on every sync tick.
    codec_pref = str(override.get("codec_preference", "")).strip()
    if codec_pref and codec_pref.lower() != "auto":
        info = await get_codec_info(mac, device_type)
        if info.get("selected", "") != codec_pref:
            ok = await set_codec(mac, device_type, codec_pref)
            if ok:
                # Give bluealsa a moment to renegotiate before we read the new rate
                await asyncio.sleep(0.5)
                # Re-read negotiated rate from bluealsa-aplay (the user-supplied
                # `rate` arg is the rate at sync-time, possibly stale post-renego).
                fresh_pcms = await _list_bluealsa_pcms()
                for p in fresh_pcms:
                    if p.get("mac", "").upper() == mac.upper() and p.get("type") == device_type:
                        with contextlib.suppress(ValueError, TypeError):
                            rate = int(p.get("rate", rate) or rate)
                        break

    eff = _resolve_effective_params(override, rate, buffer_ms)

    if device_type == "playback":
        # Phonon → BT speaker. module-alsa-sink owns the read-from-monitor +
        # write-to-bluealsa loop natively, replacing the previous null-sink +
        # parec|aplay shell pipeline.
        period = max(64, int(eff["rate"] * eff["period_ms"] / 1000))
        module_id = await _run(
            "pactl load-module module-alsa-sink "
            f"sink_name=bt_{safe_name} "
            f'sink_properties=device.description="{safe_name}-BT" '
            f'device="{bluealsa_pcm}" '
            f"rate={eff['rate']} channels={eff['channels']} format={eff['format']} "
            f"fragment_size={period}"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl failed: {module_id}"}
        _active_bridges[key] = {
            "module_id": int(module_id.strip()),
            "type": "playback",
            "name": safe_name,
            "rate": eff["rate"],
            "buffer_ms": eff["period_ms"],
            "channels": eff["channels"],
            "format": eff["format"],
        }
        logger.info("bluealsa.bridge_created", mac=mac, name=safe_name, btype="playback", **eff)

    elif device_type == "capture":
        # BT phone → Phonon. We DELIBERATELY do NOT use module-alsa-source
        # here, despite it being the "modern" path that worked for the sink
        # direction. Empirically on Pi 3 with bluealsa as the underlying PCM:
        #   * module-alsa-source opens bluealsa via the ALSA plugin, which
        #     polls() on a fixed period. bluealsa delivers BT-driven bursts
        #     (~26 ms at 44.1 kHz SBC) so poll() boundaries don't align,
        #     buffer overflows, and snd_pcm_mmap_commit fails with EPIPE
        #     in a continuous storm. The source stays SUSPENDED forever
        #     (WirePlumber: "link failed: 2 of 2 PipeWire links failed to
        #     activate"), no audio reaches downstream.
        #   * We tested mmap=false, smaller fragments, tsched=0 — all the
        #     levers — and the EPIPE storm persists. The plugin layer is
        #     fundamentally too strict for bluealsa's bursty delivery.
        # The shell wrapper (arecord | pacat → null-sink) is tolerant
        # because arecord blocks on the bluealsa fd (no poll timing) and
        # pacat uses the PipeWire stream API (not ALSA) which absorbs jitter
        # via its own clock recovery. while-true respawns on disconnect.
        # The bt_<name>_in null-sink's monitor port is what mappings consume.
        arecord_format = _ARECORD_FORMAT.get(eff["format"], "S16_LE")
        period_frames = max(64, int(eff["rate"] * eff["period_ms"] / 1000))
        module_id = await _run(
            "pactl load-module module-null-sink "
            f"sink_name=bt_{safe_name}_in "
            f'sink_properties=device.description="{safe_name}-BT-In" '
            f"format={eff['format']} rate={eff['rate']} channels={eff['channels']}"
        )
        if not module_id or not module_id.strip().isdigit():
            return {"status": "error", "detail": f"pactl null-sink failed: {module_id}"}
        bridge_cmd = (
            f"while true; do "
            f'arecord -D "{bluealsa_pcm}" '
            f"-f {arecord_format} -r {eff['rate']} -c {eff['channels']} "
            f"--period-size={period_frames} --buffer-size={period_frames * 2} - "
            f"2>/dev/null "
            f"| pacat --device=bt_{safe_name}_in "
            f"--format={eff['format']} --rate={eff['rate']} --channels={eff['channels']} "
            f"--latency-msec={eff['period_ms']} "
            # Prevent WirePlumber from auto-routing this stream to the
            # default sink — its sole consumer is the null-sink we just
            # created, mappings hang off the .monitor port.
            f"--property=node.dont-reconnect=true "
            f"--property=node.passive=true 2>/dev/null; "
            f"sleep 0.5; done"
        )
        proc = await asyncio.create_subprocess_shell(
            bridge_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            # New session = new process group, so destroy_bridge can SIGTERM
            # the whole pipeline (parent shell + arecord + pacat) at once.
            start_new_session=True,
        )
        _active_bridges[key] = {
            "module_id": int(module_id.strip()),
            "bridge_pid": proc.pid,
            "type": "capture",
            "name": safe_name,
            "rate": eff["rate"],
            "buffer_ms": eff["period_ms"],
            "channels": eff["channels"],
            "format": eff["format"],
        }
        logger.info("bluealsa.bridge_created", mac=mac, name=safe_name, btype="capture", **eff)

    return {"status": "created", "mac": mac, "name": safe_name, "type": device_type}


async def destroy_bridge(mac: str, device_type: str) -> dict[str, str]:
    """Destroy a BlueALSA → PipeWire bridge."""
    key = f"{mac}_{device_type}"
    bridge = _active_bridges.pop(key, None)
    if not bridge:
        return {"status": "not_found", "mac": mac}

    # Two bridge variants coexist now:
    #   * capture: shell wrapper (`arecord | pacat`) feeding a null-sink
    #     → has both bridge_pid (process-group leader) and module_id
    #   * playback: module-alsa-sink only → just module_id
    # Kill the wrapper's whole process group (SIGTERM the leader → all
    # descendants get the signal) so arecord and pacat go down together.
    # Single-pid os.kill leaves orphan children that keep arecord-ing
    # the bluealsa PCM and produce ghost EPIPE storms.
    pid = bridge.get("bridge_pid")
    if pid is not None:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(int(str(pid)), signal.SIGTERM)

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
        line.split("\t", 1)[0] for line in loaded_modules_raw.splitlines() if line.strip()
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


# -- Per-bridge settings endpoints -------------------------------------------


class BridgeOverrideRequest(BaseModel):
    """User-set overrides. Any field None / missing means 'use auto'."""

    model_config = ConfigDict(extra="forbid")

    rate: int | None = Field(default=None, description="44100 or 48000; null = negotiated")
    period_ms: int | None = Field(default=None, description="10/25/50/100/200; null = 50")
    channels: int | None = Field(default=None, description="1 or 2; null = 2")
    format: str | None = Field(default=None, description="s16le/s24_3le/s32le; null = s16le")
    codec_preference: str | None = Field(
        default=None,
        description="SBC/AAC/aptX/...; null or 'auto' = let bluealsa negotiate",
    )


def _validate_override_or_400(override: dict[str, Any]) -> None:
    """Reject obviously bad values up front so the UI gets a clean 400."""
    rate = override.get("rate")
    if rate is not None and rate not in _ALLOWED_RATES:
        raise HTTPException(status_code=400, detail=f"rate must be one of {_ALLOWED_RATES}")
    period = override.get("period_ms")
    if period is not None and period not in _ALLOWED_PERIOD_MS:
        raise HTTPException(
            status_code=400, detail=f"period_ms must be one of {_ALLOWED_PERIOD_MS}"
        )
    channels = override.get("channels")
    if channels is not None and channels not in _ALLOWED_CHANNELS:
        raise HTTPException(status_code=400, detail=f"channels must be one of {_ALLOWED_CHANNELS}")
    fmt = override.get("format")
    if fmt is not None and fmt not in _ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"format must be one of {_ALLOWED_FORMATS}")
    # codec_preference is open-ended (depends on bluealsa build) — we only
    # validate it's a non-empty string. Bluealsa-cli will reject unknown
    # codecs at apply time and we surface the warning in logs.
    codec = override.get("codec_preference")
    if codec is not None and (not isinstance(codec, str) or not codec.strip()):
        raise HTTPException(status_code=400, detail="codec_preference must be a non-empty string")


async def _settings_snapshot(mac: str, dtype: str) -> dict[str, Any]:
    """Build the GET response: negotiated + override + effective + runtime."""
    pcms = await _list_bluealsa_pcms()
    pcm = next(
        (p for p in pcms if p.get("mac", "").upper() == mac.upper() and p.get("type") == dtype),
        {},
    )
    try:
        nego_rate = int(pcm.get("rate", 0) or 0)
    except (TypeError, ValueError):
        nego_rate = 0
    try:
        nego_channels = int(pcm.get("channels", 0) or 0)
    except (TypeError, ValueError):
        nego_channels = 0

    codec_info = await get_codec_info(mac, dtype)
    override = get_bridge_override(mac, dtype)
    eff = _resolve_effective_params(override, nego_rate, default_buffer_ms=50)

    key = f"{mac}_{dtype}"
    br = _active_bridges.get(key, {})
    module_id = br.get("module_id")
    alive = False
    if module_id is not None:
        loaded = await _run("pactl list modules short")
        alive = any(line.startswith(f"{module_id}\t") for line in loaded.splitlines())

    # Live xrun count for the corresponding PipeWire node
    xruns = 0
    if br:
        from phonon_stage.pipewire.cli import pw_top_xruns

        sink_name = f"bt_{br.get('name', '')}" + ("_in" if dtype == "capture" else "")
        with contextlib.suppress(Exception):
            for entry in (await pw_top_xruns()).values():
                if str(entry.get("name", "")) == sink_name:
                    xruns = int(entry.get("err", 0))
                    break

    return {
        "mac": mac.upper(),
        "type": dtype,
        "negotiated": {
            "rate": nego_rate,
            "channels": nego_channels,
            "codec": codec_info.get("selected", "") or pcm.get("codec", ""),
            "available_codecs": codec_info.get("available", []),
        },
        "override": override,
        "effective": {
            "rate": eff["rate"],
            "period_ms": eff["period_ms"],
            "channels": eff["channels"],
            "format": eff["format"],
            "codec": codec_info.get("selected", "") or pcm.get("codec", ""),
        },
        "runtime": {
            "module_id": int(module_id) if module_id is not None else 0,
            "alive": alive,
            "xruns_total": xruns,
        },
    }


def _normalize_dtype(dtype: str) -> str:
    if dtype not in ("capture", "playback"):
        raise HTTPException(status_code=400, detail="type must be 'capture' or 'playback'")
    return dtype


@router.get("/bridges/{mac}/{dtype}/settings")
async def get_bridge_settings(mac: str, dtype: str) -> dict[str, Any]:
    """Read current settings (negotiated + override + effective + runtime) for one bridge."""
    return await _settings_snapshot(mac, _normalize_dtype(dtype))


@router.put("/bridges/{mac}/{dtype}/settings")
async def put_bridge_settings(mac: str, dtype: str, body: BridgeOverrideRequest) -> dict[str, Any]:
    """Persist user overrides and recreate the bridge so changes take effect immediately."""
    dtype = _normalize_dtype(dtype)
    override = {k: v for k, v in body.model_dump().items() if v is not None}
    _validate_override_or_400(override)
    set_bridge_override(mac, dtype, override)
    # Force a sync so the bridge gets recreated with new params. We destroy the
    # current one first so create_bridge doesn't short-circuit on already_exists.
    await destroy_bridge(mac, dtype)
    await sync_bridges_impl()
    return await _settings_snapshot(mac, dtype)


@router.delete("/bridges/{mac}/{dtype}/settings")
async def delete_bridge_settings(mac: str, dtype: str) -> dict[str, Any]:
    """Clear overrides for a bridge — next sync uses negotiated values."""
    dtype = _normalize_dtype(dtype)
    set_bridge_override(mac, dtype, {})
    await destroy_bridge(mac, dtype)
    await sync_bridges_impl()
    return await _settings_snapshot(mac, dtype)
