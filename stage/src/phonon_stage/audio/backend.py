"""Audio backend protocol and domain model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AudioDevice:
    """An audio device detected on the host (ALSA card)."""

    card_index: int
    name: str
    id: str
    driver: str
    playback: bool
    capture: bool


class AudioBackend(Protocol):
    """Protocol for audio device enumeration."""

    async def list_devices(self) -> list[AudioDevice]: ...
