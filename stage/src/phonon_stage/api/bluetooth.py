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
    # RSSI dBm from the latest advertisement (negative when live).
    # 0 = cached entry, the real device isn't transmitting right now.
    rssi: int = 0


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
                rssi=getattr(d, "rssi", 0),
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
    # click 'Sync Inputs/Outputs' or 'Resync mappings'. sync_bt_state
    # runs both: bridge reconcile + mapping re-attach, in background
    # so the HTTP response doesn't block on the 3+ s settle delay.
    import asyncio as _asyncio

    from phonon_stage.api.aes67 import sync_bt_state

    async def _delayed_sync() -> None:
        import contextlib as _ctx

        await _asyncio.sleep(2.0)
        with _ctx.suppress(Exception):
            await sync_bt_state(reason="bt.connect")

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


@router.post("/factory-reset", status_code=202)
async def factory_reset(body: dict[str, str] | None = None) -> dict[str, str]:
    """Wipe every paired device and discovery cache from BlueZ, restart
    the BT stack. Caller must include {"confirm": "BT_RESET"} in the body
    to avoid accidental clicks. Runs phonon-bt-reset (NOPASSWD sudo)
    in the background; result is in /var/log/phonon/bt-reset.log."""
    import asyncio as _asyncio
    from pathlib import Path as _Path

    if not body or body.get("confirm") != "BT_RESET":
        raise HTTPException(
            status_code=400,
            detail='Missing confirmation — POST {"confirm": "BT_RESET"} to proceed',
        )
    script = _Path("/usr/local/sbin/phonon-bt-reset")
    if not (script.is_file() or script.is_symlink()):
        raise HTTPException(
            status_code=503,
            detail="phonon-bt-reset script not installed — run install.sh on this host",
        )
    proc = await _asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        str(script),
        stdin=_asyncio.subprocess.DEVNULL,
        stdout=_asyncio.subprocess.DEVNULL,
        stderr=_asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "status": "started",
        "pid": str(proc.pid),
        "log": "/var/log/phonon/bt-reset.log",
    }


async def _list_disk_bonds(controller_address: str) -> list[dict]:
    """Read every bonded device record under /var/lib/bluetooth/<adapter>/.

    BlueZ unloads paired devices from D-Bus when they've been offline for
    a while (typical for a BT speaker that goes to standby). The bond
    record stays on disk and can still serve a `connect` call. We surface
    those offline-bonded entries so the user keeps seeing the device with
    a Connect button — same UX as for a phone that BlueZ keeps loaded.

    Reads via the NOPASSWD-sudo helper /usr/local/sbin/phonon-bt-list-bonds
    (the daemon doesn't have read access on /var/lib/bluetooth itself).
    """
    import asyncio as _asyncio
    import json as _json
    from pathlib import Path as _Path

    helper = _Path("/usr/local/sbin/phonon-bt-list-bonds")
    if not (helper.is_file() or helper.is_symlink()):
        return []
    proc = await _asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        str(helper),
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await _asyncio.wait_for(proc.communicate(), timeout=2.0)
    except TimeoutError:
        proc.kill()
        return []
    if proc.returncode != 0:
        return []
    bonds: list[dict] = []
    target = controller_address.upper()
    for line in out.decode(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if str(entry.get("adapter", "")).upper() != target:
            continue
        bonds.append(entry)
    return bonds


@router.get("/devices", response_model=list[BluetoothDeviceResponse])
async def list_paired_devices(
    request: Request, controller_address: str
) -> list[BluetoothDeviceResponse]:
    try:
        devices = await request.app.state.bt_backend.list_paired_devices(controller_address)
        live = [
            BluetoothDeviceResponse(
                address=d.address,
                name=d.name,
                alias=d.alias,
                paired=d.paired,
                connected=d.connected,
                icon=d.icon,
                rssi=getattr(d, "rssi", 0),
            )
            for d in devices
        ]
        # Merge in offline bonds — D-Bus list takes precedence (it has
        # current Connected/RSSI), disk bonds fill the gap when BlueZ has
        # let a paired device fall off D-Bus.
        seen = {d.address.upper() for d in live}
        for bond in await _list_disk_bonds(controller_address):
            mac = str(bond.get("address", "")).upper()
            if not mac or mac in seen:
                continue
            live.append(
                BluetoothDeviceResponse(
                    address=mac,
                    name=str(bond.get("name", "")) or mac,
                    alias=str(bond.get("name", "")) or mac,
                    paired=True,
                    connected=False,
                    icon="audio-card",
                    rssi=0,
                )
            )
        return live
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
