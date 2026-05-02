"""PipeWire backend protocol and domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PwNode:
    """A PipeWire node (audio source or sink)."""

    id: int
    name: str
    media_class: str  # "Audio/Sink", "Audio/Source", "Stream/Output/Audio"
    nick: str
    state: str  # "running", "idle", "suspended"
    # Optional extended info (populated for bluez5 nodes)
    bt_codec: str = ""  # "aptx", "sbc", "aac", "ldac"
    bt_address: str = ""  # "80:C3:BA:0A:08:C9"
    bt_profile: str = ""  # "a2dp-sink", "a2dp-source", "headset-head-unit"
    latency_ms: float = 0.0  # Buffer latency in milliseconds
    sample_rate: int = 0  # e.g. 48000
    channels: int = 0  # e.g. 2


@dataclass(frozen=True)
class PwPort:
    """A PipeWire port on a node."""

    id: int
    node_id: int
    name: str  # e.g. "playback_FL"
    direction: str  # "input" or "output"
    alias: str


@dataclass(frozen=True)
class PwLink:
    """A PipeWire link between two ports."""

    id: int
    output_port_id: int
    input_port_id: int
    state: str  # "active", "paused", "error"


class PipeWireBackend(Protocol):
    """Protocol for PipeWire audio graph management."""

    async def list_nodes(self) -> list[PwNode]: ...

    async def list_ports(self, node_id: int | None = None) -> list[PwPort]: ...

    async def list_links(self) -> list[PwLink]: ...

    async def create_link(self, output_port_id: int, input_port_id: int) -> PwLink: ...

    async def destroy_link(self, link_id: int) -> None: ...

    async def set_node_volume(self, node_id: int, volume_linear: float) -> None: ...
