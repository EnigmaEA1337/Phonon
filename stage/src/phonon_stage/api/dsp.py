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


# Curated catalog for v1. Each entry is the minimal set of fields a
# `set_output_insert` request needs (backend + library + label), plus
# a friendly name for the UI to render in the FX picker. The schema
# endpoint resolves the rest dynamically via the introspector.
#
# The label strings below are what `analyseplugin lsp-plugins-ladspa`
# emits for each plugin. They MUST match exactly — filter-chain uses
# the same string when instantiating the plugin.
_V1_CATALOG: list[dict[str, str]] = [
    {
        "backend": "ladspa",
        "library": "lsp-plugins-ladspa",
        "label": "http://lsp-plug.in/plugins/ladspa/comp_delay_stereo",
        "name": "Delay Compensator (Stereo)",
        "category": "Time",
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
async def list_plugins() -> list[PluginListEntry]:
    """v1 curated catalog. The UI uses this to populate the FX
    picker. v2 will replace this with a scan of LADSPA_PATH."""
    return [PluginListEntry(**e) for e in _V1_CATALOG]


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
