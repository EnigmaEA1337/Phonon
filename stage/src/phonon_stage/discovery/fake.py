"""Fake discovery backend for tests — records registration and browse results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phonon_stage.discovery.backend import DiscoveredStage


@dataclass
class FakeDiscoveryBackend:
    """Test double for DiscoveryBackend. Exposes state for assertions."""

    registered: bool = False
    last_stage_id: str = ""
    last_host: str = ""
    last_port: int = 0
    call_log: list[str] = field(default_factory=list)
    browse_results: list[DiscoveredStage] = field(default_factory=list)

    async def register(self, stage_id: str, host: str, port: int) -> None:
        self.registered = True
        self.last_stage_id = stage_id
        self.last_host = host
        self.last_port = port
        self.call_log.append("register")

    async def unregister(self) -> None:
        self.registered = False
        self.call_log.append("unregister")

    async def update_mode(self, mode: str) -> None:
        self.call_log.append(f"update_mode:{mode}")

    async def browse(self, timeout: float = 3.0) -> list[DiscoveredStage]:
        self.call_log.append("browse")
        return list(self.browse_results)
