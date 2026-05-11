"""GET /capabilities endpoint — enumerate hardware on this Stage."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

router = APIRouter()


class AudioDeviceResponse(BaseModel):
    """An audio device as exposed by the API."""

    model_config = ConfigDict(extra="forbid")

    card_index: int
    name: str
    id: str
    driver: str
    playback: bool
    capture: bool


class BluetoothControllerResponse(BaseModel):
    """A Bluetooth controller as exposed by the API."""

    model_config = ConfigDict(extra="forbid")

    address: str
    name: str
    alias: str
    powered: bool
    discovering: bool
    discoverable: bool = False
    pairable: bool = False
    hw_name: str = ""


class CapabilitiesResponse(BaseModel):
    """Response model for GET /capabilities."""

    model_config = ConfigDict(extra="forbid")

    stage_id: str
    mode: str
    audio_devices: list[AudioDeviceResponse]
    bluetooth_controllers: list[BluetoothControllerResponse]
    # True when this Stage can host DSP plugin inserts on outputs
    # (LSP via LADSPA filter-chain). Currently gated on x86_64 +
    # presence of a wired LadspaIntrospector — Pi 3B builds keep
    # this false so the UI hides the FX surface entirely.
    plugins_available: bool = False


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities(request: Request) -> CapabilitiesResponse:
    from phonon_stage.api.aes67 import current_mode

    state = request.app.state
    audio_devices = await state.audio_backend.list_devices()
    bt_controllers = await state.bt_backend.list_controllers()

    return CapabilitiesResponse(
        stage_id=state.config.stage_id,
        mode=current_mode(),
        plugins_available=getattr(state, "ladspa_introspector", None) is not None,
        audio_devices=[
            AudioDeviceResponse(
                card_index=d.card_index,
                name=d.name,
                id=d.id,
                driver=d.driver,
                playback=d.playback,
                capture=d.capture,
            )
            for d in audio_devices
        ],
        bluetooth_controllers=[
            BluetoothControllerResponse(
                address=c.address,
                name=c.name,
                alias=c.alias,
                powered=c.powered,
                discovering=c.discovering,
                discoverable=c.discoverable,
                pairable=c.pairable,
                hw_name=c.hw_name,
            )
            for c in bt_controllers
        ],
    )
