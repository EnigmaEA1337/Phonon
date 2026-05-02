"""Fake discovery backend for tests — records registration calls."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeDiscoveryBackend:
    """Test double for DiscoveryBackend. Exposes state for assertions."""

    registered: bool = False
    last_stage_id: str = ""
    last_host: str = ""
    last_port: int = 0
    call_log: list[str] = field(default_factory=list)

    async def register(self, stage_id: str, host: str, port: int) -> None:
        self.registered = True
        self.last_stage_id = stage_id
        self.last_host = host
        self.last_port = port
        self.call_log.append("register")

    async def unregister(self) -> None:
        self.registered = False
        self.call_log.append("unregister")
