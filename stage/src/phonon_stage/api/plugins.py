"""Source plugin REST endpoints — list, status, lifecycle, settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from phonon_stage.plugins.registry import PluginNotFoundError, PluginRegistry
from phonon_stage.plugins.system import SystemBackendError

if TYPE_CHECKING:
    from phonon_stage.plugins.backend import PluginInfo

router = APIRouter(prefix="/plugins", tags=["plugins"])


class PluginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    title: str
    description: str
    family: str
    enabled: bool
    running: bool
    pw_node_names: list[str]
    last_error: str


def _to_response(info: PluginInfo) -> PluginResponse:
    return PluginResponse(
        name=info.name,
        title=info.title,
        description=info.description,
        family=info.family,
        enabled=info.enabled,
        running=info.running,
        pw_node_names=info.pw_node_names,
        last_error=info.last_error,
    )


def _registry(request: Request) -> PluginRegistry:
    reg = getattr(request.app.state, "plugin_registry", None)
    if reg is None:
        # main.py forgot to wire it — fail loud so we don't silently
        # return [] from list and mask a deployment regression.
        raise HTTPException(status_code=503, detail="plugin registry not initialised")
    return reg  # type: ignore[no-any-return]


@router.get("", response_model=list[PluginResponse])
async def list_plugins(request: Request) -> list[PluginResponse]:
    reg = _registry(request)
    return [_to_response(i) for i in await reg.info_all()]


@router.get("/{name}", response_model=PluginResponse)
async def get_plugin(request: Request, name: str) -> PluginResponse:
    reg = _registry(request)
    try:
        info = await reg.info(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    return _to_response(info)


@router.post("/{name}/enable", response_model=PluginResponse)
async def enable_plugin(request: Request, name: str) -> PluginResponse:
    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    try:
        await plugin.enable()
    except SystemBackendError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        # Surface unexpected failures (unit file missing, conf dir
        # unwritable, etc.) instead of an opaque 500.
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return _to_response(await reg.info(name))


@router.post("/{name}/disable", response_model=PluginResponse)
async def disable_plugin(request: Request, name: str) -> PluginResponse:
    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    try:
        await plugin.disable()
    except SystemBackendError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return _to_response(await reg.info(name))


@router.post("/{name}/start", response_model=PluginResponse)
async def start_plugin(request: Request, name: str) -> PluginResponse:
    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    try:
        await plugin.start()
    except SystemBackendError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return _to_response(await reg.info(name))


@router.post("/{name}/stop", response_model=PluginResponse)
async def stop_plugin(request: Request, name: str) -> PluginResponse:
    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    try:
        await plugin.stop()
    except SystemBackendError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return _to_response(await reg.info(name))


@router.post("/{name}/restart", response_model=PluginResponse)
async def restart_plugin(request: Request, name: str) -> PluginResponse:
    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    try:
        await plugin.restart()
    except SystemBackendError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _to_response(await reg.info(name))


@router.get("/{name}/journal")
async def get_plugin_journal(request: Request, name: str, lines: int = 50) -> dict[str, str]:
    """Return the last N journalctl lines for the plugin's user unit.
    Pure diagnostic — used when a daemon is `running: true` but
    misbehaves (drops connections, crashes mid-session, silent
    audio path failure) and we can't see that from the runtime
    check alone."""
    import asyncio

    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    unit = getattr(plugin, "UNIT", None)
    if not unit:
        raise HTTPException(status_code=400, detail="plugin has no UNIT attr")
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl",
            "--user",
            "-u",
            unit,
            "-n",
            str(max(1, min(lines, 500))),
            "--no-pager",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=5)
        return {
            "unit": unit,
            "rc": str(proc.returncode),
            "log": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/admin/heal-null-sinks")
async def heal_null_sinks(request: Request) -> dict[str, str]:
    """Manually trigger the source-plugin null-sink heal. Same logic
    that runs at phonon-stage boot — useful when the user's pipewire
    session just restarted and you want airplay_in / spotify_in /
    etc. back without a daemon restart. Returns per-plugin status."""
    reg = _registry(request)
    return await reg.heal_null_sinks()


@router.get("/diag/clock")
async def get_clock_diag() -> dict[str, str]:
    """Diagnostic: `timedatectl status` + `chronyc tracking` if available.
    AirPlay's sync requires the Stage clock to track real time within
    ~10ms — large persistent shairport sync errors usually trace back
    to a broken NTP setup here."""
    import asyncio

    out: dict[str, str] = {}
    for label, args in (
        ("timedatectl", ("timedatectl", "status")),
        ("chronyc", ("chronyc", "tracking")),
        ("timesyncd", ("systemctl", "status", "--no-pager", "-n", "5", "systemd-timesyncd")),
        ("date_utc", ("date", "--utc", "+%Y-%m-%dT%H:%M:%S.%N")),
    ):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            sb, eb = await asyncio.wait_for(proc.communicate(), timeout=4)
            out[label] = sb.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0 and eb:
                out[label + "_stderr"] = eb.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            out[label + "_error"] = f"{type(exc).__name__}: {exc}"
    return out


@router.get("/diag/pactl-inputs")
async def get_pactl_inputs() -> dict[str, str]:
    """Diagnostic: `pactl list sink-inputs` — shows every PA client
    that's actively writing audio + which sink they're targeting.
    Used to verify shairport-sync / spotifyd land on our null-sinks
    rather than the default."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "list", "sink-inputs",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        sb, eb = await asyncio.wait_for(proc.communicate(), timeout=4)
        return {
            "rc": str(proc.returncode),
            "sink_inputs": sb.decode("utf-8", errors="replace"),
            "stderr": eb.decode("utf-8", errors="replace"),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/diag/pactl")
async def get_pactl_diag() -> dict[str, str]:
    """Diagnostic: dump `pactl list short sinks` + `pactl list short modules`
    as the phonon user sees them. Useful when shairport-sync / spotifyd
    can't find their target sink despite the null-sink being present in
    pw-dump (PA-compat layer can be out of sync with raw PW)."""
    import asyncio

    out: dict[str, str] = {}
    for cmd_label, args in (
        ("sinks", ("pactl", "list", "short", "sinks")),
        ("modules", ("pactl", "list", "short", "modules")),
        ("info", ("pactl", "info")),
    ):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            sb, eb = await asyncio.wait_for(proc.communicate(), timeout=4)
            out[cmd_label] = sb.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                out[cmd_label + "_stderr"] = eb.decode("utf-8", errors="replace")
        except Exception as exc:
            out[cmd_label + "_error"] = f"{type(exc).__name__}: {exc}"
    return out


@router.get("/{name}/settings")
async def get_settings(request: Request, name: str) -> dict[str, Any]:
    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    settings = await plugin.get_settings()
    # Returning a dict (not a typed response_model) because each plugin
    # has its own settings shape — the OpenAPI schema is still informative
    # via the per-plugin model documentation when needed.
    return settings.model_dump()


@router.put("/{name}/settings")
async def put_settings(request: Request, name: str, body: dict[str, Any]) -> dict[str, Any]:
    reg = _registry(request)
    try:
        plugin = reg.get(name)
    except PluginNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"unknown plugin: {name}") from exc
    try:
        # Each plugin validates against its own model — pydantic raises
        # ValidationError on bad input, FastAPI turns that into a 422.
        validated = plugin.settings_model(**body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        await plugin.put_settings(validated)
    except SystemBackendError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return validated.model_dump()
