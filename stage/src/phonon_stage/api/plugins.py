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
    return validated.model_dump()
