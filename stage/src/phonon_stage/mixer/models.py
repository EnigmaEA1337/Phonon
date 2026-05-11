"""Mix console domain model.

Three entity types form a small console-style mixer:

  Sources   ──▶   Master bus   ──▶   Outputs

* Source = a PipeWire-side audio producer (AirPlay null-sink monitor,
  BT bridge null-sink monitor, a real ALSA input). Has gain / mute /
  per-source label, plus the routing flags `to_master` and
  `direct_outputs`.
* MasterBus = a single null-sink (`phonon_master`) every via-master
  source feeds into and every receives_master output pulls from.
  Has gain / mute. There is exactly one MasterBus per Stage — no
  sub-busses in this v1.
* Output = a physical sink (ALSA device, e.g. an Avantree DG60).
  Carries the per-output delay (codec compensation), gain, mute, and
  a `receives_master` flag. The delay is applied **once** here, not
  per source — that's the whole point of the redesign: any source
  routed via the master inherits the right delay for whichever
  output it's heading to.

Routing is additive:
  * `Source.to_master=True` AND `direct_outputs=[]` → flows via the
    master to every output that has receives_master=True.
  * `Source.to_master=False` AND `direct_outputs=[o1]` → direct to
    o1 only, bypassing the master entirely.
  * Both at once → the same source's audio reaches o1 twice (master
    path + direct path); cumulative on purpose. Lets the engineer
    layer an instant direct send on top of a delayed master send if
    they want.

This module defines pure dataclasses + a couple of validators. The
service (mixer/service.py) compiles them into PipeWire links /
loopbacks / sink volumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

# Per-strip gain range — same shape as the legacy mappings code so the
# UI's fader widget can be reused without translation.
MIN_GAIN_DB = -90.0
MAX_GAIN_DB = 12.0

# Per-output delay range — used for codec-latency compensation between
# two BT chains. Anything above ~600 ms is slap-delay territory and
# would interact with shairport-sync's own sync recovery.
MAX_DELAY_MS = 600.0

# Hard ceilings on strip count. Production Stages won't get anywhere
# near these — we cap to keep the UI legible and the PW graph sane.
MAX_SOURCES = 16
MAX_OUTPUTS = 16


@dataclass(frozen=True)
class MasterBus:
    gain_db: float = 0.0
    mute: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"gain_db": self.gain_db, "mute": self.mute}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MasterBus:
        return cls(
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
        )


@dataclass(frozen=True)
class Output:
    id: str
    sink_node_name: str  # PW node name of the destination sink
    label: str  # user-facing name, free-form
    gain_db: float = 0.0
    mute: bool = False
    delay_ms: float = 0.0
    receives_master: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sink_node_name": self.sink_node_name,
            "label": self.label,
            "gain_db": self.gain_db,
            "mute": self.mute,
            "delay_ms": self.delay_ms,
            "receives_master": self.receives_master,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Output:
        return cls(
            id=str(data["id"]),
            sink_node_name=str(data["sink_node_name"]),
            label=str(data.get("label", "")),
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
            delay_ms=float(data.get("delay_ms", 0.0)),
            receives_master=bool(data.get("receives_master", True)),
        )


@dataclass(frozen=True)
class Source:
    id: str
    source_node_name: str  # PW name of the source-side node
    # source_is_sink: True for null-sinks like airplay_in / bt_X_in
    # whose monitor port is the readable side. False for real
    # capture sources (alsa_input.*, AES67 recv).
    source_is_sink: bool
    label: str
    gain_db: float = 0.0
    mute: bool = False
    to_master: bool = True
    direct_outputs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_node_name": self.source_node_name,
            "source_is_sink": self.source_is_sink,
            "label": self.label,
            "gain_db": self.gain_db,
            "mute": self.mute,
            "to_master": self.to_master,
            "direct_outputs": list(self.direct_outputs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        direct_raw = data.get("direct_outputs") or []
        return cls(
            id=str(data["id"]),
            source_node_name=str(data["source_node_name"]),
            source_is_sink=bool(data.get("source_is_sink", False)),
            label=str(data.get("label", "")),
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
            to_master=bool(data.get("to_master", True)),
            direct_outputs=tuple(str(x) for x in direct_raw),
        )


@dataclass
class MixerState:
    """Aggregate state of the mixer. Mutable so service can swap
    individual entries via dataclasses.replace, but persistence goes
    through `to_dict` / `from_dict` for a stable JSON shape."""

    master: MasterBus = field(default_factory=MasterBus)
    outputs: list[Output] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "master": self.master.to_dict(),
            "outputs": [o.to_dict() for o in self.outputs],
            "sources": [s.to_dict() for s in self.sources],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MixerState:
        return cls(
            master=MasterBus.from_dict(data.get("master") or {}),
            outputs=[Output.from_dict(o) for o in (data.get("outputs") or [])],
            sources=[Source.from_dict(s) for s in (data.get("sources") or [])],
        )


def validate_gain_db(value: float) -> float:
    """Raise ValueError if outside the accepted strip-gain range."""
    if not (MIN_GAIN_DB <= value <= MAX_GAIN_DB):
        msg = f"gain_db {value} out of range [{MIN_GAIN_DB}, {MAX_GAIN_DB}]"
        raise ValueError(msg)
    return value


def validate_delay_ms(value: float) -> float:
    """Raise ValueError if delay is negative or beyond the hard ceiling."""
    if not (0.0 <= value <= MAX_DELAY_MS):
        msg = f"delay_ms {value} out of range [0, {MAX_DELAY_MS}]"
        raise ValueError(msg)
    return value


__all__ = [
    "MAX_DELAY_MS",
    "MAX_GAIN_DB",
    "MAX_OUTPUTS",
    "MAX_SOURCES",
    "MIN_GAIN_DB",
    "MasterBus",
    "MixerState",
    "Output",
    "Source",
    "replace",
    "validate_delay_ms",
    "validate_gain_db",
]
