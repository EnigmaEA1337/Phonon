"""Tests for Pydantic response models and domain dataclasses."""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from phonon_stage.api.capabilities import (
    AudioDeviceResponse,
    BluetoothControllerResponse,
    CapabilitiesResponse,
)
from phonon_stage.api.health import HealthResponse
from phonon_stage.audio.backend import AudioDevice


class TestHealthResponseModel:
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            HealthResponse(
                status="ok",
                uptime_seconds=1.0,
                stage_id="stage-abc12345",
                extra="nope",  # type: ignore[call-arg]
            )

    def test_serialization_roundtrip(self) -> None:
        resp = HealthResponse(status="ok", uptime_seconds=42.5, stage_id="stage-abc12345")
        data = resp.model_dump()
        assert data == {
            "status": "ok",
            "uptime_seconds": 42.5,
            "stage_id": "stage-abc12345",
        }
        assert HealthResponse(**data) == resp


class TestCapabilitiesResponseModel:
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            CapabilitiesResponse(
                stage_id="stage-abc12345",
                mode="STANDALONE",
                audio_devices=[],
                bluetooth_controllers=[],
                bonus="nope",  # type: ignore[call-arg]
            )

    def test_nested_serialization(self) -> None:
        resp = CapabilitiesResponse(
            stage_id="stage-abc12345",
            mode="STANDALONE",
            audio_devices=[
                AudioDeviceResponse(
                    card_index=0,
                    name="Test Card",
                    id="test",
                    driver="snd_test",
                    playback=True,
                    capture=False,
                )
            ],
            bluetooth_controllers=[
                BluetoothControllerResponse(
                    address="AA:BB:CC:DD:EE:FF",
                    name="hci0",
                    alias="test",
                    powered=True,
                    discovering=False,
                )
            ],
        )
        data = resp.model_dump()
        assert len(data["audio_devices"]) == 1
        assert len(data["bluetooth_controllers"]) == 1


class TestAudioDeviceFrozen:
    def test_frozen_dataclass_immutable(self) -> None:
        device = AudioDevice(
            card_index=0,
            name="Test",
            id="test",
            driver="snd_test",
            playback=True,
            capture=False,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            device.name = "mutated"  # type: ignore[misc]
