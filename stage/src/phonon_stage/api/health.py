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
