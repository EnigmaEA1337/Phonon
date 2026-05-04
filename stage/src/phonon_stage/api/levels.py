"""Real-time audio level metering from PipeWire null-sink monitors."""

from __future__ import annotations

import asyncio
import math
import struct
import time

import structlog
from fastapi import APIRouter

router = APIRouter(prefix="/levels", tags=["levels"])

logger = structlog.get_logger()

# Cache levels for 500ms to avoid hammering parec
_levels_cache: dict[str, float] = {}
_levels_cache_time: float = 0
_LEVELS_CACHE_TTL = 0.5


async def _read_peak(sink_name: str, duration_ms: int = 50) -> float:
    """Read RMS level from a PipeWire sink's monitor port. Returns 0.0-1.0."""
    rate = 48000
    channels = 2
    bytes_needed = int(rate * channels * 2 * duration_ms / 1000)

    try:
        proc = await asyncio.create_subprocess_exec(
            "parec",
            f"--device={sink_name}.monitor",
            "--format=s16le",
            f"--rate={rate}",
            f"--channels={channels}",
            f"--latency-msec={duration_ms}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None

        import contextlib

        data = b""
        with contextlib.suppress(TimeoutError):
            data = await asyncio.wait_for(proc.stdout.read(bytes_needed), timeout=0.3)

        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

        if len(data) < 4:
            return 0.0

        samples = struct.unpack(f"<{len(data) // 2}h", data)
        rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
        return min(rms * 2.0, 1.0)  # Scale up for visibility

    except Exception:
        return 0.0


@router.get("")
async def get_levels() -> dict[str, float]:
    """Get real-time audio levels for all active bluealsa bridge sinks."""
    global _levels_cache, _levels_cache_time

    now = time.monotonic()
    if _levels_cache and (now - _levels_cache_time) < _LEVELS_CACHE_TTL:
        return _levels_cache

    from phonon_stage.api.bluealsa_bridge import _active_bridges

    levels: dict[str, float] = {}
    tasks = []
    keys = []

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
        tasks.append(_read_peak(sink_name, duration_ms=30))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for k, result in zip(keys, results, strict=False):
            levels[k] = result if isinstance(result, float) else 0.0

    _levels_cache = levels
    _levels_cache_time = now
    return levels
