"""DSP plugin introspection endpoints.

Lets the UI fetch a plugin's full control schema (port names, ranges,
hints, defaults) so the DSP panel can auto-render without knowing
anything plugin-specific. v1 ships a small curated catalog (just LSP
comp_delay_stereo for now); v2 will scan /usr/lib/ladspa.

When the host has no introspector wired (Pi Stages) the endpoints
return 503 — that's distinct from 404 to signal "not supported on
this hardware" rather than "this specific plugin doesn't exist".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from phonon_stage.dsp.ladspa import LadspaIntrospector, PluginDescriptor


router = APIRouter(prefix="/dsp", tags=["dsp"])


# Catalog cache. Populated lazily on first /dsp/plugins hit and on
# every restart. listplugins shells out to a subprocess and parses
# 200+ lines, so we don't want to do it on every request. The TTL
# is generous (10 min) — operators don't install LADSPA plugins
# mid-session, and the introspector still works for arbitrary
# library+label pairs not in the catalog.
_CATALOG_TTL_SECONDS = 600.0
_catalog_cache: list[dict[str, object]] = []
_catalog_cache_time: float = 0.0

# Fallback catalog used when the host has no scanner (Pi 3 without
# ladspa-sdk) or the scan returns empty. Keeps the picker functional
# in the most common standalone install — a delay plugin is what 99%
# of operators reach for first when wiring multi-output codec compensation.
_FALLBACK_CATALOG: list[dict[str, object]] = [
    {
        "backend": "ladspa",
        "library": "lsp-plugins-ladspa",
        "label": "http://lsp-plug.in/plugins/ladspa/comp_delay_stereo",
        "name": "Delay Compensator (Stereo)",
        "category": "Time",
        "is_stereo": True,
        "validated": True,
    },
]


# ── Response models ─────────────────────────────────────────────────


class PluginListEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str
    library: str
    label: str
    name: str
    category: str
    # Catalog scans tag stereo-vs-mono since only stereo plugins are
    # usable in the v1 master→output chain. UI shows them all but
    # marks mono ones so the operator doesn't trip.
    is_stereo: bool = True
    # True when the plugin is in VALIDATED_PLUGINS — we've tested it
    # with the master chain and signed off on it. Surfaces a "Phonon
    # Native" badge in the picker + the API sorts validated plugins
    # to the top of the list.
    validated: bool = False


class PluginControlSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    direction: str  # "input" | "output" | "" for audio ports
    toggled: bool
    integer: bool
    logarithmic: bool
    minimum: float | None
    maximum: float | None
    default: float | None


class PluginSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library: str
    label: str
    name: str
    maker: str
    controls: list[PluginControlSchema]


# ── Helpers ─────────────────────────────────────────────────────────


def _introspector(request: Request) -> LadspaIntrospector:
    intr = getattr(request.app.state, "ladspa_introspector", None)
    if intr is None:
        raise HTTPException(
            status_code=503,
            detail="DSP introspection not available on this host (no LADSPA SDK)",
        )
    return intr  # type: ignore[no-any-return]


def _to_schema(desc: PluginDescriptor) -> PluginSchema:
    return PluginSchema(
        library=desc.library,
        label=desc.label,
        name=desc.name,
        maker=desc.maker,
        controls=[
            PluginControlSchema(
                name=c.name,
                direction=c.direction,
                toggled=c.toggled,
                integer=c.integer,
                logarithmic=c.logarithmic,
                minimum=c.minimum,
                maximum=c.maximum,
                default=c.default,
            )
            for c in desc.controls
        ],
    )


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/plugins", response_model=list[PluginListEntry])
async def list_plugins(request: Request) -> list[PluginListEntry]:
    """Dynamic plugin catalog. Shells out to `listplugins` (LADSPA SDK)
    once, caches the result for _CATALOG_TTL_SECONDS, and returns
    insert-suitable entries (stereo plugins from LADSPA_PATH, with
    "Test"/"Analysis"/"Generator" categories filtered out).

    Falls back to the curated _FALLBACK_CATALOG when the host has no
    introspector wired (Pi Stages) or the scan returns empty — keeps
    the picker functional everywhere."""
    import time as _time

    global _catalog_cache, _catalog_cache_time

    now = _time.monotonic()
    if _catalog_cache and (now - _catalog_cache_time) < _CATALOG_TTL_SECONDS:
        return [PluginListEntry(**e) for e in _catalog_cache]

    intr = getattr(request.app.state, "ladspa_introspector", None)
    fresh: list[dict[str, object]] = list(_FALLBACK_CATALOG)
    if intr is not None:
        try:
            entries = await intr.scan_catalog()
        except Exception:
            entries = []
        if entries:
            # Sort key: validated plugins first (descending), then by
            # category, then by name. Operators almost always want the
            # vetted ones at the top — exploration of the long tail
            # happens further down.
            entries = sorted(
                entries, key=lambda e: (not e.validated, e.category, e.name)
            )
            fresh = [{"backend": "ladspa", **e.to_dict()} for e in entries]
    _catalog_cache = fresh
    _catalog_cache_time = now
    return [PluginListEntry(**e) for e in fresh]


@router.get("/plugins/schema", response_model=PluginSchema)
async def get_plugin_schema(request: Request, library: str, label: str) -> PluginSchema:
    """Introspect one plugin's control ports. Returns the full schema
    so the UI can render every control without prior knowledge.

    library + label come in via query string (not path) because LSP
    labels are URL-shaped (`http://lsp-plug.in/...`) and would fight
    FastAPI's path parsing — query params side-step the issue."""
    intr = _introspector(request)
    try:
        desc = await intr.describe(library, label)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"introspection failed: {exc}") from exc
    return _to_schema(desc)


# ── Live spectrum ───────────────────────────────────────────────────


class SpectrumResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str
    bands: list[float]  # one dBFS value per requested band center, in order


@router.get("/spectrum", response_model=SpectrumResponse)
async def get_spectrum(request: Request, node: str, bands: str) -> SpectrumResponse:
    """Sample the latest ~85 ms of audio from a PipeWire monitor node
    and return one dBFS magnitude per requested band center frequency.

    Args:
      node:  PW node name to capture (e.g. `phonon_master.monitor`).
             First call spawns a persistent parec; idle nodes are
             reaped after 5 s without a request.
      bands: comma-separated band center frequencies in Hz, e.g.
             "16,20,25,31.5,...,20000". Each one gets one dBFS
             reading peak-picked across the ±1/6-octave window
             around it (matches a 1/3-octave RTA).

    Returns 503 when numpy isn't installed (Pi hosts without
    ladspa-sdk by project policy don't get a spectrum overlay).
    """
    from phonon_stage.dsp.spectrum import LiveSpectrum, SpectrumUnavailable

    svc = getattr(request.app.state, "live_spectrum", None)
    if svc is None:
        # Lazy-instantiate so we don't spawn the reaper task at
        # startup when nothing's asked for a spectrum yet.
        svc = LiveSpectrum()
        request.app.state.live_spectrum = svc
        await svc.start()
    try:
        centers = [float(p) for p in bands.split(",") if p.strip()]
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"bands must be comma-separated numbers: {exc}",
        ) from exc
    if not centers:
        raise HTTPException(status_code=400, detail="bands is empty")
    try:
        values = await svc.get_spectrum_db(node, centers)
    except SpectrumUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail=f"parec not installed on host: {exc}"
        ) from exc
    return SpectrumResponse(node=node, bands=values)
