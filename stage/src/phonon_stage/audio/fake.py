"""Fake audio backend for tests — returns injectable device list."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phonon_stage.audio.backend import AudioDevice


class FakeAudioBackend:
    """Test double for AudioBackend. Inject devices at construction."""

    def __init__(self, devices: list[AudioDevice] | None = None) -> None:
        self._devices = devices or []

    async def list_devices(self) -> list[AudioDevice]:
        return list(self._devices)
