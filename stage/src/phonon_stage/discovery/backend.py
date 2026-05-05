"""Discovery backend protocol for mDNS-SD service registration and browsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DiscoveredStage:
    """A Phonon Stage discovered on the network via mDNS-SD."""

    stage_id: str
    host: str
    port: int
    version: str
    mode: str


class DiscoveryBackend(Protocol):
    """Protocol for mDNS-SD service announcement and browsing."""

    async def register(self, stage_id: str, host: str, port: int) -> None: ...

    async def unregister(self) -> None: ...

    async def update_mode(self, mode: str) -> None: ...

    async def browse(self, timeout: float = 3.0) -> list[DiscoveredStage]: ...
