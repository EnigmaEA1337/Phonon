"""WebSocket endpoint for real-time push data (VU levels, mode, alerts)."""

import asyncio
import contextlib
import json
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = structlog.get_logger()

# Connected clients
clients: set[WebSocket] = set()

# Background task references (prevent GC)
_bg_tasks: list[asyncio.Task[None]] = []
_tasks_started = False


async def broadcast(msg_type: str, data: Any) -> None:
    """Push a message to all connected WebSocket clients."""
    global clients
    if not clients:
        return
    payload = json.dumps({"type": msg_type, "data": data})
    dead: set[WebSocket] = set()
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    clients -= dead


async def _levels_loop() -> None:
    """Background task: read VU levels and push to clients every 1 s.

    Each tick spawns one short-lived parec per active source. On a Pi 3
    the parec startup + WirePlumber re-routing it triggers is the
    dominant CPU cost (~25-44% on the sender). 1 Hz is enough for
    visual feedback without saturating the graph; we'll switch to a
    single persistent parec stream later for smoother UX.

    The loop runs whenever a UI client is connected — gating on
    `_active_bridges or _active_streams` is too narrow because it
    misses source-plugin null-sinks (AirPlay etc.) which carry their
    own audio independently of the BT/AES67 paths.
    """
    from phonon_stage.api.aes67 import _active_streams
    from phonon_stage.api.bluealsa_bridge import _active_bridges
    from phonon_stage.api.levels import _read_peak

    while True:
        if clients:
            levels: dict[str, float] = {}
            tasks: list[Any] = []
            keys: list[str] = []

            for key, bridge in _active_bridges.items():
                name = bridge.get("name", "")
                btype = bridge.get("type", "")
                # Playback bridges (Phonon -> BT speaker via aplay/bluealsa)
                # are skipped: spawning a parec on bt_<name>.monitor 4x/s
                # competes with the bridge's own parec for graph time on
                # a Pi 3, which under load creates jitter on the aplay
                # output and crackly audio at the JBL. The user can hear
                # the speaker — they don't need a UI VU for that side.
                if btype != "capture":
                    continue
                # Capture bridge is now a null-sink (`bt_<name>_in`) fed
                # by `arecord | pacat` — the readable PipeWire source is
                # the null-sink's MONITOR port (`bt_<name>_in.monitor`).
                source_name = f"bt_{name}_in.monitor"
                keys.append(key)
                tasks.append(_read_peak(source_name, duration_ms=20))

            # AES67 receivers expose an Audio/Source named
            # `aes67-recv-<stream_name>`. parec reads it directly — no
            # monitor suffix because rtp-source IS a source. We skip
            # send streams: their PipeWire node is Audio/Sink and the
            # interesting level is the upstream feeder, which is
            # already covered by the bluealsa-bridge case above (or by
            # the local non-BT input sources, which we don't meter yet).
            for sid, s in _active_streams.items():
                if s.get("kind") != "recv":
                    continue
                stream_name = str(s.get("name", ""))
                if not stream_name:
                    continue
                source_name = f"aes67-recv-{stream_name}"
                keys.append(f"aes67_{sid}")
                tasks.append(_read_peak(source_name, duration_ms=20))

            # Source plugins (AirPlay, …): each owns a null-sink whose
            # .monitor port exposes the audio coming from its upstream
            # daemon. Meter it just like the BT bridge monitors so the
            # UI can show a per-source VU in the patch bay. We key
            # entries by `node:<sink-name>` so the JS can join them by
            # node name (more stable than the synthetic per-bridge key
            # used above, which is opaque to the patch-bay rendering).
            from phonon_stage.plugins.airplay_v1 import NULL_SINK_NAME as _AIRPLAY_SINK

            for sink_name in (_AIRPLAY_SINK,):
                keys.append(f"node:{sink_name}")
                tasks.append(_read_peak(f"{sink_name}.monitor", duration_ms=20))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for k, result in zip(keys, results, strict=False):
                    levels[k] = result if isinstance(result, float) else 0.0

            await broadcast("levels", levels)

        await asyncio.sleep(1.0)


async def _mode_loop() -> None:
    """Background task: push mode changes to clients."""
    last_mode = ""
    while True:
        from phonon_stage.api.health import _ui_mode

        if _ui_mode != last_mode:
            last_mode = _ui_mode
            await broadcast("mode", {"mode": _ui_mode})
        await asyncio.sleep(0.5)


# Last-seen PIDs for the audio user services. When a PID changes
# between ticks we know the service restarted out-of-band (someone ran
# `systemctl --user restart pipewire` from a shell, or a crash) and we
# need to rebuild bridges + mappings even though the click never went
# through our endpoints.
_last_audio_pids: dict[str, int] = {}
# Same trick for bluealsa: a PID delta means every `arecord -D bluealsa`
# we'd piped into pacat is now a zombie reading from a dead service.
# Run /bluealsa/sync to rebuild bridges against the new daemon.
_last_bluealsa_pid: int = 0
# Throttle for the stale-node-id detector — only fires one resync at
# a time and waits a few seconds between attempts so a USB device
# that flaps doesn't spin us in tight resync loop.
_stale_resync_in_flight: bool = False
_stale_resync_last_attempt: float = 0.0


async def _system_loop() -> None:
    """Push system snapshot (services + resource gauges) every 5 s.

    Coalesces both reads (services state + resource gauges) into one
    payload so the UI updates atomically without flicker. Two parallel
    fetches per tick. Also detects out-of-band restarts of pipewire /
    wireplumber / pipewire-pulse (PID delta) and replays our audio
    state so user mappings + BT bridges survive.
    """
    from phonon_stage.api.system import (
        AUDIO_USER_SERVICES,
        _collect_resources,
        collect_services,
    )

    while True:
        try:
            services, resources = await asyncio.gather(
                collect_services(), _collect_resources(), return_exceptions=True
            )
            if clients:
                payload: dict[str, Any] = {}
                if not isinstance(services, BaseException):
                    payload["services"] = [s.model_dump() for s in services]
                if not isinstance(resources, BaseException):
                    payload["resources"] = resources.model_dump()
                if payload:
                    await broadcast("system", payload)

            # Out-of-band restart detection. Runs every tick whether
            # there are clients or not — we still need to replay state
            # if pipewire was restarted from a shell.
            if not isinstance(services, BaseException):
                changed: list[str] = []
                for s in services:
                    if s.name not in AUDIO_USER_SERVICES or not s.active or not s.pid:
                        # If the service is down, drop the cached PID so
                        # the next start counts as 'first seen' rather
                        # than a delta against a stale value.
                        _last_audio_pids.pop(s.name, None)
                        continue
                    prev = _last_audio_pids.get(s.name)
                    if prev is not None and prev != s.pid:
                        changed.append(s.name)
                    _last_audio_pids[s.name] = s.pid
                if changed:
                    logger.info("ws.audio_external_restart", services=changed)
                    from phonon_stage.api.aes67 import replay_audio_state

                    # Stash a reference so the GC can't reap the task
                    # before it finishes.
                    _bg_tasks.append(
                        asyncio.create_task(
                            replay_audio_state(reason=f"external:{','.join(changed)}")
                        )
                    )

                # bluealsa restart detection — PID delta on bluealsa.service
                # means every BT bridge is reading from a dead daemon.
                global _last_bluealsa_pid
                if not isinstance(services, BaseException):
                    bluealsa = next((s for s in services if s.name == "bluealsa"), None)
                    if bluealsa and bluealsa.active and bluealsa.pid:
                        if _last_bluealsa_pid and _last_bluealsa_pid != bluealsa.pid:
                            logger.info("ws.bluealsa_external_restart", pid=bluealsa.pid)
                            from phonon_stage.api.aes67 import sync_bt_state

                            async def _delayed_bluealsa_sync() -> None:
                                await asyncio.sleep(2.0)
                                try:
                                    await sync_bt_state(reason="external:bluealsa")
                                except Exception:
                                    logger.warning("ws.bluealsa_sync_failed", exc_info=True)

                            _bg_tasks.append(asyncio.create_task(_delayed_bluealsa_sync()))
                        _last_bluealsa_pid = bluealsa.pid
                    else:
                        _last_bluealsa_pid = 0

            # Stale node-id detector — covers USB hot-replug (DG60
            # unplugged/replugged, USB DAC cycle): PipeWire renumbers
            # the new device, the user's mappings keep pointing at the
            # old node IDs, links are GC'd, audio drops. Compare each
            # mapping's stored sink/source IDs against current pw-dump;
            # any miss → resync once. Throttled to one attempt per 8s.
            await _maybe_resync_stale_mappings()
        except Exception:
            logger.warning("ws.system_tick_failed", exc_info=True)
        await asyncio.sleep(5)


async def _maybe_resync_stale_mappings() -> None:
    """Fire mapping_service.resync_mappings() if any persisted mapping
    references a sink/source node ID that no longer exists in PipeWire.
    Throttled + serialized so a flapping USB device can't loop us."""
    global _stale_resync_in_flight, _stale_resync_last_attempt
    if _stale_resync_in_flight:
        return
    now = asyncio.get_event_loop().time()
    if now - _stale_resync_last_attempt < 8.0:
        return

    from phonon_stage.api.aes67 import _mapping_service

    if _mapping_service is None:
        return
    mappings = getattr(_mapping_service, "mappings", None) or getattr(
        getattr(_mapping_service, "_store", None), "mappings", None
    )
    if not mappings:
        return

    try:
        from phonon_stage.pipewire import cli as _cli

        dump = await _cli.pw_dump()
        live_node_ids = {
            int(item.get("id", 0)) for item in dump if item.get("type", "").endswith("Node")
        }
        live_link_ids = {
            int(item.get("id", 0)) for item in dump if item.get("type", "").endswith("Link")
        }
    except Exception:
        return

    # A mapping is stale when one of its node IDs is gone (USB
    # hot-replug) or when any of its stored link IDs has been GC'd
    # by PipeWire. The link case happens after a brief node suspend
    # (WirePlumber re-routing a freshly plugged sink, autoswitch on
    # default sink change…) — the nodes survive but the links don't,
    # so the audio path is broken even though node-id checks pass.
    stale = False
    for m in mappings:
        if getattr(m, "mute", False):
            continue
        sid = getattr(m, "sink_node_id", 0)
        src = getattr(m, "source_node_id", 0)
        link_ids = getattr(m, "link_ids", []) or []
        if (sid and sid not in live_node_ids) or (src and src not in live_node_ids):
            stale = True
            break
        if link_ids and any(lid not in live_link_ids for lid in link_ids):
            stale = True
            break
    if not stale:
        return

    _stale_resync_in_flight = True
    _stale_resync_last_attempt = now
    logger.info("ws.stale_mapping_detected_resync")
    try:
        resync = getattr(_mapping_service, "resync_mappings", None)
        if resync is not None:
            from phonon_stage.pipewire import cli as _cli2

            _cli2._pw_dump_cache = []
            _cli2._pw_dump_cache_time = 0.0
            await resync()
    except Exception:
        logger.warning("ws.stale_mapping_resync_failed", exc_info=True)
    finally:
        _stale_resync_in_flight = False


async def _alerts_loop() -> None:
    """Background task: push system alerts to clients every 10s."""
    last_alerts: list[str] = []
    while True:
        if clients:
            try:
                proc = await asyncio.create_subprocess_shell(
                    "vcgencmd get_throttled 2>/dev/null",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                throttled_raw = out.decode().strip()
                throttled = 0
                if "=" in throttled_raw:
                    with contextlib.suppress(ValueError):
                        throttled = int(throttled_raw.split("=")[1], 16)

                alerts: list[str] = []
                if throttled & 0x1:
                    alerts.append("UNDER-VOLTAGE NOW!")
                elif throttled & 0x10000:
                    alerts.append("Under-voltage occurred")
                if throttled & 0x4:
                    alerts.append("CPU THROTTLED NOW")

                if alerts != last_alerts:
                    last_alerts = alerts[:]
                    await broadcast("alerts", alerts)
            except Exception:
                pass

        await asyncio.sleep(10)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    global _tasks_started

    await ws.accept()
    clients.add(ws)
    logger.info("ws.connected", client_count=len(clients))

    # Start background loops on first connection
    if not _tasks_started:
        _tasks_started = True
        _bg_tasks.append(asyncio.create_task(_levels_loop()))
        _bg_tasks.append(asyncio.create_task(_mode_loop()))
        _bg_tasks.append(asyncio.create_task(_alerts_loop()))
        _bg_tasks.append(asyncio.create_task(_system_loop()))

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "mode":
                    from phonon_stage.api import health

                    health._ui_mode = msg.get("data", {}).get("mode", "live")
                    await broadcast("mode", {"mode": health._ui_mode})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        logger.info("ws.disconnected", client_count=len(clients))
