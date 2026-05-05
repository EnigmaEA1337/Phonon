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
