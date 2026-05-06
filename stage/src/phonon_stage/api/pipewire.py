"""PipeWire introspection endpoints — list nodes, ports, links."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/pipewire", tags=["pipewire"])


class PwNodeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    media_class: str
    nick: str
    state: str
    bt_codec: str = ""
    bt_address: str = ""
    bt_profile: str = ""
    latency_ms: float = 0.0
    sample_rate: int = 0
    channels: int = 0


class PwPortResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    node_id: int
    name: str
    direction: str
    alias: str


class PwLinkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    output_port_id: int
    input_port_id: int
    state: str


@router.get("/nodes", response_model=list[PwNodeResponse])
async def list_nodes(request: Request) -> list[PwNodeResponse]:
    nodes = await request.app.state.pw_backend.list_nodes()
    return [
        PwNodeResponse(
            id=n.id,
            name=n.name,
            media_class=n.media_class,
            nick=n.nick,
            state=n.state,
            bt_codec=n.bt_codec,
            bt_address=n.bt_address,
            bt_profile=n.bt_profile,
            latency_ms=n.latency_ms,
            sample_rate=n.sample_rate,
            channels=n.channels,
        )
        for n in nodes
    ]


@router.get("/ports", response_model=list[PwPortResponse])
async def list_ports(request: Request, node_id: int | None = None) -> list[PwPortResponse]:
    ports = await request.app.state.pw_backend.list_ports(node_id)
    return [
        PwPortResponse(
            id=p.id, node_id=p.node_id, name=p.name, direction=p.direction, alias=p.alias
        )
        for p in ports
    ]


@router.get("/links", response_model=list[PwLinkResponse])
async def list_links(request: Request) -> list[PwLinkResponse]:
    links = await request.app.state.pw_backend.list_links()
    return [
        PwLinkResponse(
            id=lk.id,
            output_port_id=lk.output_port_id,
            input_port_id=lk.input_port_id,
            state=lk.state,
        )
        for lk in links
    ]


@router.get("/xruns")
async def list_xruns() -> dict[str, object]:
    """Per-node XRUN counters parsed from `pw-top -b`. Cached 3s.

    Returns: { total: int, nodes: [{id, name, err, state}] }
    """
    from phonon_stage.pipewire import cli

    table = await cli.pw_top_xruns()
    nodes = [
        {"id": nid, "name": v.get("name", ""), "err": v.get("err", 0), "state": v.get("state", "")}
        for nid, v in table.items()
    ]
    total = sum(int(n["err"]) for n in nodes)  # type: ignore[arg-type]
    return {"total": total, "nodes": nodes}
