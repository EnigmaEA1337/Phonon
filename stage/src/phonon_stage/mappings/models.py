"""Mapping domain model — a routed audio path between PipeWire ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_MAPPINGS = 8


@dataclass
class Mapping:
    """An active audio mapping between source and sink ports."""

    id: str  # UUID4 hex[:8]
    source_node_id: int
    source_port_ids: list[int]
    sink_node_id: int
    sink_port_ids: list[int]
    link_ids: list[int] = field(default_factory=list)
    gain_db: float = 0.0
    pan: float = 0.0
    mute: bool = False
    delay_ms: float = 0.0  # Latency offset for output sync (0-600 ms)
    # pactl module-loopback id when delay_ms > 0 (None when direct pw-link).
    # Real delay is implemented by inserting a loopback stream with
    # latency_msec=delay_ms in the chain, because PipeWire's per-node
    # `latencyOffsetNsec` is a scheduling hint that doesn't add audible
    # buffering. Stored to destroy/recreate the module on update + delete.
    loopback_module_id: int | None = None
    created_at: str = ""  # ISO 8601
    # Node names captured at create-time. PipeWire IDs change across
    # restarts; names don't, so a re-resolve by name lets us restore
    # mappings after a PW restart.
    source_node_name: str = ""
    sink_node_name: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dict for persistence."""
        return {
            "id": self.id,
            "source_node_id": self.source_node_id,
            "source_port_ids": self.source_port_ids,
            "sink_node_id": self.sink_node_id,
            "sink_port_ids": self.sink_port_ids,
            "link_ids": self.link_ids,
            "gain_db": self.gain_db,
            "pan": self.pan,
            "mute": self.mute,
            "delay_ms": self.delay_ms,
            "loopback_module_id": self.loopback_module_id,
            "created_at": self.created_at,
            "source_node_name": self.source_node_name,
            "sink_node_name": self.sink_node_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mapping:
        """Deserialize from a JSON-compatible dict."""
        return cls(
            id=str(data["id"]),
            source_node_id=int(data["source_node_id"]),
            source_port_ids=[int(p) for p in data["source_port_ids"]],
            sink_node_id=int(data["sink_node_id"]),
            sink_port_ids=[int(p) for p in data["sink_port_ids"]],
            link_ids=[int(lk) for lk in data.get("link_ids", [])],
            gain_db=float(data.get("gain_db", 0.0)),
            pan=float(data.get("pan", 0.0)),
            mute=bool(data.get("mute", False)),
            delay_ms=float(data.get("delay_ms", 0.0)),
            loopback_module_id=(
                int(data["loopback_module_id"])
                if data.get("loopback_module_id") is not None
                else None
            ),
            created_at=str(data.get("created_at", "")),
            source_node_name=str(data.get("source_node_name", "")),
            sink_node_name=str(data.get("sink_node_name", "")),
        )
