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


@router.post("/{controller_address}/power", status_code=200)
async def set_power(
    request: Request, controller_address: str, body: PowerRequest
) -> dict[str, object]:
    try:
        await request.app.state.bt_backend.set_power(controller_address, body.powered)
        return {"controller": controller_address, "powered": body.powered}
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
        return {"status": "connected", "device": body.device_address}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/disconnect", status_code=200)
async def disconnect_device(request: Request, body: DeviceRequest) -> dict[str, str]:
    try:
        await request.app.state.bt_backend.disconnect(body.device_address)
        return {"status": "disconnected", "device": body.device_address}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/unpair", status_code=200)
async def unpair_device(request: Request, body: DeviceRequest) -> dict[str, str]:
    try:
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
