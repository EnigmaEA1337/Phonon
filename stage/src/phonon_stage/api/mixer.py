"""Mix console REST endpoints — Sources / Master / Outputs / routing."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from phonon_stage.mixer.models import (
    MAX_DELAY_MS,
    MAX_GAIN_DB,
    MIN_GAIN_DB,
    PLUGIN_BACKENDS,
)
from phonon_stage.mixer.service import MixerError, MixerService

if TYPE_CHECKING:
    from phonon_stage.mixer.models import MasterBus, Output, PluginInsert, Source


router = APIRouter(prefix="/mixer", tags=["mixer"])


# ── Response models ─────────────────────────────────────────────────


class MasterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gain_db: float
    mute: bool
    mute_left: bool
    mute_right: bool


class PluginInsertResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str
    library: str
    label: str
    controls: dict[str, float]
    enabled: bool


class OutputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    sink_node_name: str
    label: str
    gain_db: float
    mute: bool
    mute_left: bool
    mute_right: bool
    solo: bool
    delay_ms: float
    receives_master: bool
    insert: PluginInsertResponse | None = None


class SourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_node_name: str
    source_is_sink: bool
    label: str
    gain_db: float
    mute: bool
    mute_left: bool
    mute_right: bool
    solo: bool
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
    mute_left: bool | None = None
    mute_right: bool | None = None


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
    mute_left: bool | None = None
    mute_right: bool | None = None
    solo: bool | None = None
    delay_ms: float | None = Field(default=None, ge=0.0, le=MAX_DELAY_MS)
    receives_master: bool | None = None


class InsertSet(BaseModel):
    """Body for PATCH /mixer/outputs/{id}/insert. All three fields
    `None` clears the insert; otherwise all three are required and
    a fresh PluginInsert is attached with defaults seeded from the
    LADSPA introspector."""

    model_config = ConfigDict(extra="forbid")
    backend: str | None = Field(default=None)
    library: str | None = Field(default=None)
    label: str | None = Field(default=None)


class InsertControlUpdate(BaseModel):
    """Body for PATCH /mixer/outputs/{id}/insert/controls/{name}. The
    name is in the URL so multi-word LSP control names ("Time (ms)")
    don't fight the JSON body schema."""

    model_config = ConfigDict(extra="forbid")
    value: float


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
    mute_left: bool | None = None
    mute_right: bool | None = None
    solo: bool | None = None
    to_master: bool | None = None
    direct_outputs: list[str] | None = None


# ── Helpers ─────────────────────────────────────────────────────────


def _to_master_resp(m: MasterBus) -> MasterResponse:
    return MasterResponse(
        gain_db=m.gain_db,
        mute=m.mute,
        mute_left=m.mute_left,
        mute_right=m.mute_right,
    )


def _to_insert_resp(ins: PluginInsert | None) -> PluginInsertResponse | None:
    if ins is None:
        return None
    return PluginInsertResponse(
        backend=ins.backend,
        library=ins.library,
        label=ins.label,
        controls=dict(ins.controls),
        enabled=ins.enabled,
    )


def _to_output_resp(o: Output) -> OutputResponse:
    return OutputResponse(
        id=o.id,
        sink_node_name=o.sink_node_name,
        label=o.label,
        gain_db=o.gain_db,
        mute=o.mute,
        mute_left=o.mute_left,
        mute_right=o.mute_right,
        solo=o.solo,
        delay_ms=o.delay_ms,
        receives_master=o.receives_master,
        insert=_to_insert_resp(o.insert),
    )


def _to_source_resp(s: Source) -> SourceResponse:
    return SourceResponse(
        id=s.id,
        source_node_name=s.source_node_name,
        source_is_sink=s.source_is_sink,
        label=s.label,
        gain_db=s.gain_db,
        mute=s.mute,
        mute_left=s.mute_left,
        mute_right=s.mute_right,
        solo=s.solo,
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
        m = await svc.update_master(
            gain_db=body.gain_db,
            mute=body.mute,
            mute_left=body.mute_left,
            mute_right=body.mute_right,
        )
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
            mute_left=body.mute_left,
            mute_right=body.mute_right,
            solo=body.solo,
            delay_ms=body.delay_ms,
            receives_master=body.receives_master,
        )
    except MixerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_output_resp(o)


@router.patch("/outputs/{output_id}/insert", response_model=OutputResponse)
async def patch_output_insert(request: Request, output_id: str, body: InsertSet) -> OutputResponse:
    """Attach a plugin to an output's master→sink path, or clear it.
    All-null body detaches. Defaults for controls are seeded from
    the LADSPA introspector when the host has one wired."""
    svc = _service(request)
    if body.backend is not None and body.backend not in PLUGIN_BACKENDS:
        raise HTTPException(
            status_code=400, detail=f"backend must be one of {list(PLUGIN_BACKENDS)}"
        )
    # Half-set is a coding mistake on the UI side; reject to keep
    # the surface unambiguous (clear = all null, set = all three).
    set_fields = [body.backend is not None, body.label is not None]
    if any(set_fields) and not all(set_fields):
        raise HTTPException(
            status_code=400,
            detail="insert set requires backend AND label (library is optional)",
        )
    try:
        out = await svc.set_output_insert(
            output_id,
            backend=body.backend,
            library=body.library,
            label=body.label,
        )
    except MixerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_output_resp(out)


@router.post("/admin/reconcile")
async def reconcile(request: Request) -> dict[str, str]:
    """Force a full mixer reconcile — tear down + rebuild every link,
    loopback, and filter-chain from the persisted state.

    Use case: PW restart, a `pactl unload-module` cascade, or any
    out-of-band edit nuked the live audio graph. The `Resync` button
    in the Mix Console points here. Previously it patched master
    gain_db hoping that would trigger reconcile, but the server's
    PATCH /master fast-paths volume-only changes (no topology touch),
    so the reconcile never ran — links stayed broken.

    Idempotent. Safe to call repeatedly; each run snaps the live PW
    state to whatever the store says.
    """
    svc: MixerService = request.app.state.mixer_service
    await svc._reconcile()  # noqa: SLF001 — service exposes no public hook
    return {"status": "reconciled"}


@router.post("/admin/cleanup-orphan-chain")
async def cleanup_orphan_chain(request: Request, filename: str) -> dict[str, object]:
    """Operator escape hatch: delete any file in the filter-chain
    conf drop-in directory (not restricted to `phonon-*.conf`) and
    reload filter-chain.service. Useful for clearing test confs
    left over from manual experiments that aren't tracked by the
    mixer's own state. After the reload we re-ensure phonon_master
    + reconcile so the legitimate chains come back."""
    backend = request.app.state.pw_backend
    fc_dir = getattr(backend, "_fc_dir", None)
    if fc_dir is None:
        raise HTTPException(status_code=503, detail="filter-chain dir unknown")
    safe = "".join(c for c in filename if c.isalnum() or c in ("-", "_", "."))
    if safe != filename or "/" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    target = fc_dir / safe
    deleted = False
    try:
        if target.exists():
            target.unlink()
            deleted = True
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unlink failed: {exc}") from exc
    with contextlib.suppress(Exception):
        await backend.reload_filter_chain()
    # filter-chain.service reload cascades into pipewire-pulse and
    # may nuke our null-sinks. Re-run the mixer's reconcile so
    # phonon_master gets recreated and chains come back.
    svc = _service(request)
    try:
        await svc._ensure_master_null_sink()
        await svc._reconcile()
    except Exception as exc:
        return {"deleted": deleted, "reconcile_error": str(exc)}
    return {"deleted": deleted, "filename": safe}


@router.get(
    "/outputs/{output_id}/insert/monitoring",
)
async def get_output_insert_monitoring(
    request: Request, output_id: str, debug: int = 0
) -> dict[str, object]:
    """Return the live values of the filter-chain plugin's control
    ports — input AND output. Used by the DSP panel's Monitoring
    block: the UI polls this every ~500ms and updates only the
    readout cells in-place (no panel re-render → drag/wheel stay
    smooth).

    `?debug=1` also returns the raw `pw-cli enum-params Props`
    output stashed by the Real backend — only useful for figuring
    out what the LSP plugin actually exposes when parsing comes
    back empty."""
    svc = _service(request)
    try:
        values = await svc.read_output_insert_live_controls(output_id)
    except MixerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if debug:
        backend = getattr(request.app.state, "pw_backend", None)
        raw = getattr(backend, "_last_filter_dump", "") or ""
        last_set = getattr(backend, "_last_set_param", None)
        mute_log = getattr(backend, "_set_mute_log", {}) or {}
        return {
            "values": values,
            "raw_pw_cli_output": raw,
            "last_set_param": last_set,
            "set_mute_log": {str(k): v for k, v in mute_log.items()},
        }
    # Mypy: the debug branch returns a dict[str, object] but the no-debug
    # branch returns a dict[str, float]; both are valid for the endpoint's
    # declared response (`dict[str, object]`). Cast keeps both shapes flat.
    return values  # type: ignore[return-value]


@router.patch(
    "/outputs/{output_id}/insert/controls/{control_name:path}",
    response_model=OutputResponse,
)
async def patch_output_insert_control(
    request: Request,
    output_id: str,
    control_name: str,
    body: InsertControlUpdate,
) -> OutputResponse:
    """Live-update one plugin control. Goes through pw-cli set-param
    against the running filter-chain node — no service reload, no
    audio glitch. control_name is URL-path-encoded so LSP names with
    spaces and parens (e.g. `Time%20(ms)`) round-trip cleanly."""
    svc = _service(request)
    try:
        out = await svc.update_output_insert_control(output_id, control_name, body.value)
    except MixerError as exc:
        # 404 for unknown output / control, 400 for bad shape.
        msg = str(exc)
        status = 404 if "unknown" in msg or "no plugin" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    return _to_output_resp(out)


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
            mute_left=body.mute_left,
            mute_right=body.mute_right,
            solo=body.solo,
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
