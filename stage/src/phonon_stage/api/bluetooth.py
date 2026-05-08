"""Bluetooth operation endpoints — power, scan, pair, connect."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/bluetooth", tags=["bluetooth"])


class PowerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    powered: bool


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_address: str


class BluetoothDeviceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str
    name: str
    alias: str
    paired: bool
    connected: bool
    icon: str


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str  # "receiver" or "transmitter"


class PairingWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration: int = 60  # seconds


class AliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str


@router.post("/{controller_address}/alias", status_code=200)
async def set_alias(
    request: Request, controller_address: str, body: AliasRequest
) -> dict[str, object]:
    try:
        await request.app.state.bt_backend.set_alias(controller_address, body.alias)
        return {"controller": controller_address, "alias": body.alias}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{controller_address}/pairing", status_code=200)
async def open_pairing_window(
    request: Request, controller_address: str, body: PairingWindowRequest
) -> dict[str, object]:
    try:
        await request.app.state.bt_backend.open_pairing_window(controller_address, body.duration)
        return {
            "controller": controller_address,
            "pairing_open": True,
            "duration": body.duration,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{controller_address}/role", status_code=200)
async def set_role(
    request: Request, controller_address: str, body: RoleRequest
) -> dict[str, object]:
    try:
        await request.app.state.bt_backend.set_role(controller_address, body.role)
        return {"controller": controller_address, "role": body.role}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{controller_address}/power", status_code=200)
async def set_power(
    request: Request, controller_address: str, body: PowerRequest
) -> dict[str, object]:
    try:
        await request.app.state.bt_backend.set_power(controller_address, body.powered)
        return {"controller": controller_address, "powered": body.powered}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/quick-pair", status_code=200)
async def quick_pair(
    request: Request, body: DeviceRequest, controller_address: str
) -> dict[str, object]:
    """Scan + Trust + Pair + Connect in one robust sequence."""
    try:
        result = await request.app.state.bt_backend.quick_pair(
            controller_address, body.device_address
        )
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/scan", response_model=list[BluetoothDeviceResponse])
async def scan_devices(
    request: Request, controller_address: str, timeout: float = 10.0
) -> list[BluetoothDeviceResponse]:
    try:
        devices = await request.app.state.bt_backend.start_scan(controller_address, timeout)
        return [
            BluetoothDeviceResponse(
                address=d.address,
                name=d.name,
                alias=d.alias,
                paired=d.paired,
                connected=d.connected,
                icon=d.icon,
            )
            for d in devices
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/pair", status_code=200)
async def pair_device(request: Request, body: DeviceRequest) -> dict[str, str]:
    try:
        await request.app.state.bt_backend.pair(body.device_address)
        return {"status": "paired", "device": body.device_address}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/connect", status_code=200)
async def connect_device(request: Request, body: DeviceRequest) -> dict[str, str]:
    try:
        await request.app.state.bt_backend.connect(body.device_address)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Auto-sync — bluealsa exposes a fresh PCM for the device a moment
    # after BlueZ reports 'connected', so the user shouldn't have to
    # click 'Sync Inputs/Outputs' to start hearing audio. We do this
    # in the background (don't block the HTTP response).
    import asyncio as _asyncio

    from phonon_stage.api.bluealsa_bridge import sync_bridges_impl

    async def _delayed_sync() -> None:
        import contextlib as _ctx

        await _asyncio.sleep(2.0)
        with _ctx.suppress(Exception):
            await sync_bridges_impl(buffer_ms=50)

    request.app.state.background_tasks = getattr(request.app.state, "background_tasks", [])
    request.app.state.background_tasks.append(_asyncio.create_task(_delayed_sync()))
    return {"status": "connected", "device": body.device_address}


@router.delete("/disconnect", status_code=200)
async def disconnect_device(request: Request, body: DeviceRequest) -> dict[str, str]:
    try:
        # Destroy any active bluealsa bridges for this device
        from phonon_stage.api.bluealsa_bridge import destroy_bridge

        for dtype in ("playback", "capture"):
            await destroy_bridge(body.device_address, dtype)

        await request.app.state.bt_backend.disconnect(body.device_address)
        return {"status": "disconnected", "device": body.device_address}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/unpair", status_code=200)
async def unpair_device(request: Request, body: DeviceRequest) -> dict[str, str]:
    try:
        # Destroy any active bluealsa bridges for this device
        from phonon_stage.api.bluealsa_bridge import destroy_bridge

        for dtype in ("playback", "capture"):
            await destroy_bridge(body.device_address, dtype)

        await request.app.state.bt_backend.unpair(body.device_address)
        return {"status": "unpaired", "device": body.device_address}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/devices", response_model=list[BluetoothDeviceResponse])
async def list_paired_devices(
    request: Request, controller_address: str
) -> list[BluetoothDeviceResponse]:
    try:
        devices = await request.app.state.bt_backend.list_paired_devices(controller_address)
        return [
            BluetoothDeviceResponse(
                address=d.address,
                name=d.name,
                alias=d.alias,
                paired=d.paired,
                connected=d.connected,
                icon=d.icon,
            )
            for d in devices
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
