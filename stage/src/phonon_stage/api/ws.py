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
    """Background task: read VU levels and push to clients every 250ms.

    250ms = 4 Hz update rate, smooth enough for a UI VU meter while
    keeping the per-tick parec subprocess spawns reasonable. At 10 Hz
    we were saturating CPU on slower hosts (and even noticeable on dev).
    """
    from phonon_stage.api.bluealsa_bridge import _active_bridges
    from phonon_stage.api.levels import _read_peak

    while True:
        if clients and _active_bridges:
            levels: dict[str, float] = {}
            tasks: list[Any] = []
            keys: list[str] = []

            for key, bridge in _active_bridges.items():
                name = bridge.get("name", "")
                btype = bridge.get("type", "")
                if btype == "playback":
                    sink_name = f"bt_{name}"
                elif btype == "capture":
                    sink_name = f"bt_{name}_in"
                else:
                    continue
                keys.append(key)
                tasks.append(_read_peak(sink_name, duration_ms=20))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for k, result in zip(keys, results, strict=False):
                    levels[k] = result if isinstance(result, float) else 0.0

            await broadcast("levels", levels)

        await asyncio.sleep(0.25)


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
        except Exception:
            logger.warning("ws.system_tick_failed", exc_info=True)
        await asyncio.sleep(5)


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
