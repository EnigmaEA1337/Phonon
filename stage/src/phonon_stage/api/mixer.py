"""Mix console REST endpoints — Sources / Master / Outputs / routing."""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import struct
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
    # Back-compat: first insert in the chain (or None). Existing
    # `/insert/...` endpoints still drive it. The full chain lives
    # in `inserts` below; UI iterates that one for multi-plugin
    # rendering.
    insert: PluginInsertResponse | None = None
    inserts: list[PluginInsertResponse] = Field(default_factory=list)


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
    chain = [_to_insert_resp(i) for i in o.inserts]
    chain_resp: list[PluginInsertResponse] = [c for c in chain if c is not None]
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
        insert=chain_resp[0] if chain_resp else None,
        inserts=chain_resp,
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
    the LADSPA introspector when the host has one wired.

    Back-compat with the v1 single-insert API: clearing here clears
    the ENTIRE chain (every slot). Use POST/DELETE /inserts for
    multi-plugin add/remove."""
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


class InsertAppend(BaseModel):
    """Body for POST /mixer/outputs/{id}/inserts — append a new plugin
    to the chain. Distinct from PATCH /insert (back-compat single-slot
    replace) because append never clears existing slots."""

    model_config = ConfigDict(extra="forbid")
    backend: str
    library: str
    label: str


@router.post("/outputs/{output_id}/inserts", response_model=OutputResponse, status_code=201)
async def post_output_chain_insert(
    request: Request, output_id: str, body: InsertAppend
) -> OutputResponse:
    """Append a plugin to the end of an output's chain. Multi-plugin
    chains run in series (slot 0 closest to the master). Capped at
    MAX_CHAIN_DEPTH; the service raises MixerError → 409 here."""
    svc = _service(request)
    if body.backend not in PLUGIN_BACKENDS:
        raise HTTPException(
            status_code=400, detail=f"backend must be one of {list(PLUGIN_BACKENDS)}"
        )
    try:
        out = await svc.append_chain_insert(
            output_id, backend=body.backend, library=body.library, label=body.label
        )
    except MixerError as exc:
        # "chain depth at cap" → 409 (conflict on capacity); "unknown
        # output" → 404. Inspect message to split.
        status = 404 if "unknown output id" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _to_output_resp(out)


@router.delete("/outputs/{output_id}/inserts/{slot}", response_model=OutputResponse)
async def delete_output_chain_slot(
    request: Request, output_id: str, slot: int
) -> OutputResponse:
    """Remove the plugin at slot N. Slots > N shift down by one."""
    svc = _service(request)
    try:
        out = await svc.remove_chain_insert(output_id, slot=slot)
    except MixerError as exc:
        status = 404 if "unknown output id" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _to_output_resp(out)


@router.delete("/outputs/{output_id}/inserts", response_model=OutputResponse)
async def reset_output_chain(request: Request, output_id: str) -> OutputResponse:
    """Clear every plugin on this output's chain — output reverts to a
    plain loopback. Idempotent (noop when chain is already empty)."""
    svc = _service(request)
    try:
        out = await svc.reset_chain(output_id)
    except MixerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_output_resp(out)


class InsertEnabledUpdate(BaseModel):
    """Body for PATCH /outputs/{id}/inserts/{slot} — toggle the
    enabled flag on a chain slot. enabled=False renders the slot
    as a passthrough inside the filter-chain conf (no LADSPA
    plugin loaded) — true bypass without tearing down the chain's
    node identity."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


@router.patch("/outputs/{output_id}/inserts/{slot}", response_model=OutputResponse)
async def patch_output_chain_slot(
    request: Request, output_id: str, slot: int, body: InsertEnabledUpdate
) -> OutputResponse:
    """Update a single chain slot's flags. v1: only `enabled` is
    settable from here — wired to the strip-level bypass button."""
    svc = _service(request)
    try:
        out = await svc.set_insert_enabled(output_id, slot, enabled=body.enabled)
    except MixerError as exc:
        status = 404 if "unknown output id" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
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
    await svc._reconcile()
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


# ── Test tone injection (TEST AUDIO button) ─────────────────────────
#
# Generates a signal in-process and pipes it to `pacat` writing into
# phonon_master, so the signal travels the full Master → loopbacks /
# filter-chains → real outputs chain. Useful to verify whether audio
# is actually reaching the speakers when the user reports intermittent
# silence.

_TEST_TONE_KINDS = ("click", "tone", "pink")
_TEST_TONE_SR = 48000

_test_tone_proc: asyncio.subprocess.Process | None = None
_test_tone_kind: str | None = None
_test_tone_writer: asyncio.Task[None] | None = None


_CHUNK_FRAMES = 1024  # ~21 ms @ 48 kHz — keeps drain latency low
_CHUNK_BYTES = _CHUNK_FRAMES * 4  # stereo s16le


def _generate_chunk(kind: str, pos: int) -> bytes:
    """Generate one chunk of _CHUNK_FRAMES stereo s16le samples.

    `pos` is the running sample-count since stream start, so phase /
    rhythm continue smoothly across chunks. Streaming generation
    (vs a pre-built looped buffer) avoids two issues we saw on the
    BT chain:
      * pink noise was a fixed 1 s pattern repeated identically →
        the ear heard a 1 Hz cadence instead of true white noise.
      * tone had a tiny discontinuity at every loop boundary that
        the SBC codec amplified into an audible tick.
    """
    sr = _TEST_TONE_SR
    buf = bytearray(_CHUNK_BYTES)
    if kind == "click":
        # 120 BPM = 2 ticks/sec. Each tick = 50 ms of 1 kHz sine,
        # cosine-windowed so the burst's own envelope doesn't click.
        period = sr // 2
        tick = int(sr * 0.05)
        for i in range(_CHUNK_FRAMES):
            p = (pos + i) % period
            if p < tick:
                env = math.sin(math.pi * p / tick)
                s = int(0.5 * 32767 * env * math.sin(2 * math.pi * 1000 * p / sr))
            else:
                s = 0
            struct.pack_into("<hh", buf, i * 4, s, s)
    elif kind == "tone":
        # Continuous 440 Hz sine at -10 dBFS, phase derived from the
        # absolute sample index so chunk boundaries are seamless.
        amp = int(32767 * 0.316)
        k = 2 * math.pi * 440 / sr
        for i in range(_CHUNK_FRAMES):
            s = int(amp * math.sin(k * (pos + i)))
            struct.pack_into("<hh", buf, i * 4, s, s)
    elif kind == "pink":
        # Cheap white noise at -14 dBFS. Generated fresh per chunk —
        # never repeats. "Pink" is a UI label (real pink filtering
        # isn't worth the cost here).
        amp_f = 32767 * 0.2
        for i in range(_CHUNK_FRAMES):
            s = int(amp_f * (random.random() * 2 - 1))
            struct.pack_into("<hh", buf, i * 4, s, s)
    else:
        raise ValueError(f"unknown kind: {kind}")
    return bytes(buf)


async def _feed_test_tone(proc: asyncio.subprocess.Process, kind: str) -> None:
    """Stream freshly-generated chunks to pacat's stdin until cancelled
    or pacat exits. `pos` carries phase/rhythm continuity across
    chunk writes."""
    pos = 0
    try:
        while True:
            if proc.stdin is None or proc.returncode is not None:
                return
            proc.stdin.write(_generate_chunk(kind, pos))
            pos += _CHUNK_FRAMES
            try:
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return
    except asyncio.CancelledError:
        return


async def _stop_test_tone_locked() -> None:
    """Tear down the running tone, if any. Caller holds no lock — we
    just zero the module-level state. Single-worker app so it's fine."""
    global _test_tone_proc, _test_tone_kind, _test_tone_writer
    writer = _test_tone_writer
    proc = _test_tone_proc
    _test_tone_writer = None
    _test_tone_proc = None
    _test_tone_kind = None
    if writer is not None and not writer.done():
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
    # Final safety net: sweep any pacat with our stream-name still alive.
    # Covers the case where pipewire-pulse was restarted in between and
    # pacat auto-reconnected, leaving our Python proc handle stale.
    with contextlib.suppress(Exception):
        sweep = await asyncio.create_subprocess_exec(
            "pkill",
            "-f",
            "pacat.*phonon-test-tone",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await sweep.wait()


@router.post("/admin/test-tone/start")
async def start_test_tone(kind: str = "click") -> dict[str, str]:
    """Inject a test signal into phonon_master so the operator can
    hear whether audio is reaching the outputs. Kinds:
      - click: 120 BPM metronome (1 kHz windowed bursts)
      - tone:  steady 440 Hz sine
      - pink:  white-noise bed
    Calling start while already running stops the previous one first."""
    global _test_tone_proc, _test_tone_kind, _test_tone_writer
    if kind not in _TEST_TONE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {_TEST_TONE_KINDS}",
        )
    await _stop_test_tone_locked()
    # Also sweep any orphan pacat from a prior session — if pipewire-pulse
    # was restarted while a test tone was running, pacat reconnects on its
    # own (the PA shim default) and our handle is lost. The stream-name is
    # a literal we own, so pkill on it is safe.
    with contextlib.suppress(Exception):
        sweep = await asyncio.create_subprocess_exec(
            "pkill",
            "-f",
            "pacat.*phonon-test-tone",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await sweep.wait()
    try:
        proc = await asyncio.create_subprocess_exec(
            "pacat",
            "--playback",
            "--device=phonon_master",
            "--rate=48000",
            "--format=s16le",
            "--channels=2",
            "--stream-name=phonon-test-tone",
            # Without this, pacat auto-reconnects when pipewire-pulse
            # restarts (e.g. audio-stack/restart) and survives our
            # /stop call because the Python proc handle is stale.
            "--property=node.dont-reconnect=true",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail="pacat not installed (apt install pulseaudio-utils)",
        ) from exc
    _test_tone_proc = proc
    _test_tone_kind = kind
    _test_tone_writer = asyncio.create_task(_feed_test_tone(proc, kind))
    return {"status": "started", "kind": kind}


@router.post("/admin/test-tone/stop")
async def stop_test_tone() -> dict[str, str]:
    await _stop_test_tone_locked()
    return {"status": "stopped"}


@router.get("/admin/test-tone/status")
async def status_test_tone() -> dict[str, object]:
    running = _test_tone_proc is not None and _test_tone_proc.returncode is None
    return {"running": running, "kind": _test_tone_kind if running else None}
