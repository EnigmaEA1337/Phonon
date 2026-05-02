"""Browse endpoint — discover other Stages on the network via mDNS."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/browse", tags=["browse"])


class DiscoveredStageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage_id: str
    host: str
    port: int
    version: str
    mode: str


@router.get("/stages", response_model=list[DiscoveredStageResponse])
async def browse_stages(request: Request, timeout: float = 3.0) -> list[DiscoveredStageResponse]:
    stages = await request.app.state.discovery_backend.browse(timeout)
    return [
        DiscoveredStageResponse(
            stage_id=s.stage_id,
            host=s.host,
            port=s.port,
            version=s.version,
            mode=s.mode,
        )
        for s in stages
    ]
