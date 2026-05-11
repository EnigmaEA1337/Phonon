"""Mix console REST endpoints — Sources / Master / Outputs / routing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from phonon_stage.mixer.models import (
    MAX_DELAY_MS,
    MAX_GAIN_DB,
    MIN_GAIN_DB,
)
from phonon_stage.mixer.service import MixerError, MixerService

if TYPE_CHECKING:
    from phonon_stage.mixer.models import MasterBus, Output, Source


router = APIRouter(prefix="/mixer", tags=["mixer"])


# ── Response models ─────────────────────────────────────────────────


class MasterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gain_db: float
    mute: bool


class OutputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    sink_node_name: str
    label: str
    gain_db: float
    mute: bool
    delay_ms: float
    receives_master: bool


class SourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_node_name: str
    source_is_sink: bool
    label: str
    gain_db: float
    mute: bool
    to_master: bool
    direct_outputs: list[str]


class MixerSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    master: MasterResponse
    outputs: list[OutputResponse]
    sources: list[SourceResponse]


# ── Request models ──────────────────────────────────────────────────


class MasterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gain_db: float | None = Field(default=None, ge=MIN_GAIN_DB, le=MAX_GAIN_DB)
    mute: bool | None = None


class OutputCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sink_node_name: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=128)
    gain_db: float = Field(default=0.0, ge=MIN_GAIN_DB, le=MAX_GAIN_DB)
    delay_ms: float = Field(default=0.0, ge=0.0, le=MAX_DELAY_MS)
    receives_master: bool = True


class OutputUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=128)
    gain_db: float | None = Field(default=None, ge=MIN_GAIN_DB, le=MAX_GAIN_DB)
    mute: bool | None = None
    delay_ms: float | None = Field(default=None, ge=0.0, le=MAX_DELAY_MS)
    receives_master: bool | None = None


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_node_name: str = Field(min_length=1)
    source_is_sink: bool
    label: str = Field(min_length=1, max_length=128)
    gain_db: float = Field(default=0.0, ge=MIN_GAIN_DB, le=MAX_GAIN_DB)
    to_master: bool = True
    direct_outputs: list[str] = Field(default_factory=list)


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=128)
    gain_db: float | None = Field(default=None, ge=MIN_GAIN_DB, le=MAX_GAIN_DB)
    mute: bool | None = None
    to_master: bool | None = None
    direct_outputs: list[str] | None = None


# ── Helpers ─────────────────────────────────────────────────────────


def _to_master_resp(m: MasterBus) -> MasterResponse:
    return MasterResponse(gain_db=m.gain_db, mute=m.mute)


def _to_output_resp(o: Output) -> OutputResponse:
    return OutputResponse(
        id=o.id,
        sink_node_name=o.sink_node_name,
        label=o.label,
        gain_db=o.gain_db,
        mute=o.mute,
        delay_ms=o.delay_ms,
        receives_master=o.receives_master,
    )


def _to_source_resp(s: Source) -> SourceResponse:
    return SourceResponse(
        id=s.id,
        source_node_name=s.source_node_name,
        source_is_sink=s.source_is_sink,
        label=s.label,
        gain_db=s.gain_db,
        mute=s.mute,
        to_master=s.to_master,
        direct_outputs=list(s.direct_outputs),
    )


def _service(request: Request) -> MixerService:
    svc = getattr(request.app.state, "mixer_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="mixer service not initialised")
    return svc  # type: ignore[no-any-return]


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("", response_model=MixerSnapshot)
async def get_snapshot(request: Request) -> MixerSnapshot:
    """Full mixer state in a single response — the UI fetches this
    on every refresh and re-renders the three columns from it."""
    svc = _service(request)
    return MixerSnapshot(
        master=_to_master_resp(svc.master),
        outputs=[_to_output_resp(o) for o in svc.outputs],
        sources=[_to_source_resp(s) for s in svc.sources],
    )


@router.patch("/master", response_model=MasterResponse)
async def patch_master(request: Request, body: MasterUpdate) -> MasterResponse:
    svc = _service(request)
    try:
        m = await svc.update_master(gain_db=body.gain_db, mute=body.mute)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_master_resp(m)


@router.post("/outputs", response_model=OutputResponse, status_code=201)
async def post_output(request: Request, body: OutputCreate) -> OutputResponse:
    svc = _service(request)
    try:
        o = await svc.add_output(
            sink_node_name=body.sink_node_name,
            label=body.label,
            gain_db=body.gain_db,
            delay_ms=body.delay_ms,
            receives_master=body.receives_master,
        )
    except MixerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_output_resp(o)


@router.patch("/outputs/{output_id}", response_model=OutputResponse)
async def patch_output(request: Request, output_id: str, body: OutputUpdate) -> OutputResponse:
    svc = _service(request)
    try:
        o = await svc.update_output(
            output_id=output_id,
            label=body.label,
            gain_db=body.gain_db,
            mute=body.mute,
            delay_ms=body.delay_ms,
            receives_master=body.receives_master,
        )
    except MixerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_output_resp(o)


@router.delete("/outputs/{output_id}", status_code=204)
async def delete_output(request: Request, output_id: str) -> None:
    svc = _service(request)
    try:
        await svc.remove_output(output_id)
    except MixerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sources", response_model=SourceResponse, status_code=201)
async def post_source(request: Request, body: SourceCreate) -> SourceResponse:
    svc = _service(request)
    try:
        s = await svc.add_source(
            source_node_name=body.source_node_name,
            source_is_sink=body.source_is_sink,
            label=body.label,
            gain_db=body.gain_db,
            to_master=body.to_master,
            direct_outputs=list(body.direct_outputs),
        )
    except MixerError as exc:
        # 409 when references are bad (unknown direct_outputs id);
        # 400 for value-range violations on gain.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_source_resp(s)


@router.patch("/sources/{source_id}", response_model=SourceResponse)
async def patch_source(request: Request, source_id: str, body: SourceUpdate) -> SourceResponse:
    svc = _service(request)
    try:
        s = await svc.update_source(
            source_id=source_id,
            label=body.label,
            gain_db=body.gain_db,
            mute=body.mute,
            to_master=body.to_master,
            direct_outputs=body.direct_outputs,
        )
    except MixerError as exc:
        # Source not found OR direct_outputs references something
        # bad. Use 404 vs 409 by inspecting the message — pragmatic
        # for one route, refactor if it grows.
        status = 404 if "unknown source id" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_source_resp(s)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(request: Request, source_id: str) -> None:
    svc = _service(request)
    try:
        await svc.remove_source(source_id)
    except MixerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
