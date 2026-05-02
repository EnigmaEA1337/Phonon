"""Discovery backend protocol for mDNS-SD service registration."""

from __future__ import annotations

from typing import Protocol


class DiscoveryBackend(Protocol):
    """Protocol for mDNS-SD service announcement."""

    async def register(self, stage_id: str, host: str, port: int) -> None: ...

    async def unregister(self) -> None: ...
