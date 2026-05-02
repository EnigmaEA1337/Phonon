"""GET /health endpoint — liveness check with uptime and stage identity."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter()


class HealthResponse(BaseModel):
    """Response model for GET /health."""

    model_config = ConfigDict(extra="forbid")

    status: str
    uptime_seconds: float
    stage_id: str


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    state = request.app.state
    elapsed = state.clock.monotonic() - state.start_time
    return HealthResponse(
        status="ok",
        uptime_seconds=round(elapsed, 1),
        stage_id=state.config.stage_id,
    )


@router.get("/debug/pw-test")
async def debug_pw_test() -> dict[str, object]:
    """Temporary debug endpoint — test pw-dump directly."""
    import asyncio
    import os

    xdg = os.environ.get("XDG_RUNTIME_DIR", "UNSET")
    try:
        proc = await asyncio.create_subprocess_exec(
            "pw-dump",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        chunks: list[bytes] = []
        try:
            while True:
                chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=3.0)
                if not chunk:
                    break
                chunks.append(chunk)
        except TimeoutError:
            pass
        import contextlib

        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        data = b"".join(chunks)
        stderr = (await proc.stderr.read()).decode().strip() if not data else ""
        return {
            "xdg_runtime_dir": xdg,
            "bytes": len(data),
            "returncode": proc.returncode,
            "stderr": stderr,
            "first_100": data[:100].decode() if data else "",
        }
    except Exception as exc:
        return {"error": str(exc), "xdg_runtime_dir": xdg}
