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

Plugin inserts (v1):
* Each Output optionally carries one PluginInsert — a single LADSPA
  plugin spliced into the master→output path via PipeWire's
  module-filter-chain. When the insert is present and enabled the
  service replaces the loopback with a filter-chain that pulls from
  phonon_master, runs the plugin, and writes to the output sink.
  Plugin controls (e.g. "Time (ms)" for LSP comp_delay_stereo) are
  introspected at runtime from the LADSPA descriptor and exposed to
  the UI for auto-rendering. Live parameter updates go through
  pw-cli set-param, no filter-chain reload — that's the whole point
  of the migration (zero-click delay change).

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
MAX_VCAS = 8  # control-plane groupings — 8 is plenty for a single show
MAX_BUSES = 8  # sub-mix buses — same ceiling as VCAs by design


# Plugin backend kinds we support in v1. PW 1.6.2 (Ubuntu Studio 26.04)
# is built without LV2 — filter-chain only knows builtin + ladspa here.
# Keeping the string form discoverable so the UI can render a kind
# badge alongside the plugin name.
PLUGIN_BACKENDS = ("ladspa",)


@dataclass(frozen=True)
class PluginInsert:
    """A single plugin spliced into an Output's master→sink path.

    Lives inside Output. When `enabled` is True the mixer service
    replaces the output's module-loopback with a module-filter-chain
    that runs this plugin between phonon_master.monitor and the
    output's sink. Live control updates go through pw-cli set-param
    against the filter-chain node so changes don't tear down the
    chain (the whole reason this exists — module-loopback's
    latency_msec can't be retuned live, plugin params can).

    Fields:
      backend:   "ladspa" (LV2 deferred; PW 1.6.2 here lacks LV2)
      library:   .so name for LADSPA (filter-chain resolves via
                 LADSPA_PATH; we pass the bare name e.g. "lsp-plugins-ladspa")
      label:     LADSPA plugin label (the unique URL string for LSP
                 plugins, e.g. "http://lsp-plug.in/plugins/ladspa/comp_delay_stereo")
      controls:  current values for the plugin's control ports, keyed
                 by control name as it appears in the LADSPA
                 descriptor (e.g. {"Time (ms)": 80.0, "Mode": 2,
                 "Ramping": 1, "Bypass": 0}). Defaults are filled in
                 by the service the first time the plugin is loaded.
      enabled:   when False the output reverts to the plain loopback
                 path (no plugin in line), but the controls dict is
                 kept so toggling back on restores the previous state.
    """

    backend: str  # one of PLUGIN_BACKENDS
    library: str  # e.g. "lsp-plugins-ladspa"
    label: str  # LADSPA label / LV2 URI
    controls: dict[str, float] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "library": self.library,
            "label": self.label,
            "controls": dict(self.controls),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PluginInsert:
        raw_controls = data.get("controls") or {}
        return cls(
            backend=str(data.get("backend", "ladspa")),
            library=str(data.get("library", "")),
            label=str(data.get("label", "")),
            controls={str(k): float(v) for k, v in raw_controls.items()},
            enabled=bool(data.get("enabled", True)),
        )


@dataclass(frozen=True)
class MasterBus:
    gain_db: float = 0.0
    mute: bool = False
    # Per-channel mutes — applied as zero volume on the corresponding
    # channel of the master sink. Independent of `mute` (which tears
    # down link topology entirely). The master has no solo by spec.
    mute_left: bool = False
    mute_right: bool = False
    # Master FX chain (limiter / glue comp / mastering EQ etc).
    # When non-empty + at least one slot is enabled, the mixer service
    # creates a phonon_master_post null-sink, inserts a filter-chain
    # between phonon_master.monitor and phonon_master_post, and
    # re-points every Output to read from phonon_master_post.monitor
    # instead of phonon_master.monitor. Per-Output chains still run
    # downstream of that, in series.
    inserts: tuple[PluginInsert, ...] = ()

    @property
    def insert(self) -> PluginInsert | None:
        """Back-compat shim, mirrors Output.insert. Returns the
        chain's first plugin or None when the chain is empty."""
        return self.inserts[0] if self.inserts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gain_db": self.gain_db,
            "mute": self.mute,
            "mute_left": self.mute_left,
            "mute_right": self.mute_right,
            "inserts": [i.to_dict() for i in self.inserts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MasterBus:
        raw_inserts = data.get("inserts") or []
        inserts: tuple[PluginInsert, ...] = tuple(
            PluginInsert.from_dict(item) for item in raw_inserts if item
        )
        return cls(
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
            mute_left=bool(data.get("mute_left", False)),
            mute_right=bool(data.get("mute_right", False)),
            inserts=inserts,
        )


# Hard cap on chain depth — prevents the operator from stacking 40
# plugins by accident and turning the master→output path into a CPU
# bonfire. 8 is enough for the typical "EQ → compressor → delay →
# limiter" chain a working engineer would build.
MAX_CHAIN_DEPTH = 8


@dataclass(frozen=True)
class Output:
    id: str
    sink_node_name: str  # PW node name of the destination sink
    label: str  # user-facing name, free-form
    gain_db: float = 0.0
    mute: bool = False
    mute_left: bool = False
    mute_right: bool = False
    # Solo: when any output has solo=True, every other output (without
    # solo) is silenced at reconcile time. Lets the engineer audition
    # a single destination without manually muting the others.
    solo: bool = False
    delay_ms: float = 0.0
    receives_master: bool = True
    # Plugin chain in the master→output path. Ordered, first element
    # closest to the master (input side). Empty tuple → plain loopback,
    # no filter-chain conf rendered. Capped at MAX_CHAIN_DEPTH.
    inserts: tuple[PluginInsert, ...] = ()

    @property
    def insert(self) -> PluginInsert | None:
        """Back-compat shim — the v1 single-insert API surface still
        speaks `output.insert`. Returns the chain's first plugin or
        None when the chain is empty. New code should iterate
        `output.inserts` instead."""
        return self.inserts[0] if self.inserts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sink_node_name": self.sink_node_name,
            "label": self.label,
            "gain_db": self.gain_db,
            "mute": self.mute,
            "mute_left": self.mute_left,
            "mute_right": self.mute_right,
            "solo": self.solo,
            "delay_ms": self.delay_ms,
            "receives_master": self.receives_master,
            "inserts": [i.to_dict() for i in self.inserts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Output:
        # Migration: the old shape had a single `insert: dict | None`
        # field. New shape is `inserts: list[dict]`. Read whichever is
        # present, prefer the new one. Persistence always writes the
        # new shape from to_dict above.
        raw_inserts = data.get("inserts")
        if raw_inserts is None:
            raw_legacy = data.get("insert")
            raw_inserts = [raw_legacy] if raw_legacy else []
        inserts: tuple[PluginInsert, ...] = tuple(
            PluginInsert.from_dict(item) for item in raw_inserts if item
        )
        return cls(
            id=str(data["id"]),
            sink_node_name=str(data["sink_node_name"]),
            label=str(data.get("label", "")),
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
            mute_left=bool(data.get("mute_left", False)),
            mute_right=bool(data.get("mute_right", False)),
            solo=bool(data.get("solo", False)),
            delay_ms=float(data.get("delay_ms", 0.0)),
            receives_master=bool(data.get("receives_master", True)),
            inserts=inserts,
        )


@dataclass(frozen=True)
class BusSend:
    """A post-fader send from a Source into a sub-mix Bus.

    The Source's audio reaches the named bus with a per-send gain
    delta (independent of the source's own fader and of the bus's
    own fader). `enabled` is the structural on/off — when False the
    reconcile skips creating the source→bus link entirely. Sends
    are additive on top of the source's master / direct_outputs
    routing: a source can simultaneously feed master, an output
    direct, and any number of buses.
    """

    bus_id: str
    gain_db: float = 0.0
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "bus_id": self.bus_id,
            "gain_db": self.gain_db,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BusSend:
        return cls(
            bus_id=str(data["bus_id"]),
            gain_db=float(data.get("gain_db", 0.0)),
            enabled=bool(data.get("enabled", True)),
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
    mute_left: bool = False
    mute_right: bool = False
    # Solo: when any source has solo=True, every other source (without
    # solo) gets its outbound links suppressed at reconcile, so only
    # the solo'd source(s) feed downstream. Multiple solos work as a
    # group (all solo'd sources play).
    solo: bool = False
    to_master: bool = True
    direct_outputs: tuple[str, ...] = ()
    # Post-fader sends into one or more buses. Each entry names a bus
    # by id and carries its own gain. Order is meaningful only for the
    # UI (left-to-right in the picker); reconcile treats sends as a
    # set. Sends referring to a non-existent bus_id are silently
    # ignored at reconcile and pruned by remove_bus().
    bus_sends: tuple[BusSend, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_node_name": self.source_node_name,
            "source_is_sink": self.source_is_sink,
            "label": self.label,
            "gain_db": self.gain_db,
            "mute": self.mute,
            "mute_left": self.mute_left,
            "mute_right": self.mute_right,
            "solo": self.solo,
            "to_master": self.to_master,
            "direct_outputs": list(self.direct_outputs),
            "bus_sends": [s.to_dict() for s in self.bus_sends],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        direct_raw = data.get("direct_outputs") or []
        sends_raw = data.get("bus_sends") or []
        return cls(
            id=str(data["id"]),
            source_node_name=str(data["source_node_name"]),
            source_is_sink=bool(data.get("source_is_sink", False)),
            label=str(data.get("label", "")),
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
            mute_left=bool(data.get("mute_left", False)),
            mute_right=bool(data.get("mute_right", False)),
            solo=bool(data.get("solo", False)),
            to_master=bool(data.get("to_master", True)),
            direct_outputs=tuple(str(x) for x in direct_raw),
            bus_sends=tuple(BusSend.from_dict(s) for s in sends_raw if s),
        )


@dataclass(frozen=True)
class Bus:
    """A sub-mix bus — a virtual sink that sums one or more Source
    sends and feeds the master, optionally through its own DSP chain.

    Topology parallels MasterBus / Output:
      * A null-sink (`phonon_bus_<id>`) is the structural endpoint
        every source send writes to.
      * If `inserts` is non-empty + at least one slot is enabled, the
        reconcile inserts a filter-chain between the bus null-sink's
        monitor and a `phonon_bus_<id>_post` null-sink, then loopbacks
        that into the master. Same pattern as MasterBus's post-chain.
      * Gain / mute / mute_left / mute_right behave like MasterBus.
      * Solo (when any bus has solo=True) silences every other bus at
        reconcile so the operator can audition a single sub-mix.

    v1 constraint: every bus feeds the master. No bus→output direct
    routing in this release — kept for v2 to avoid blowing up the
    reconcile surface area.
    """

    id: str  # short uuid hex
    label: str  # operator-visible name, e.g. "Drums"
    gain_db: float = 0.0
    mute: bool = False
    mute_left: bool = False
    mute_right: bool = False
    solo: bool = False
    # DSP chain on the bus → master path. Same shape as Output.inserts.
    inserts: tuple[PluginInsert, ...] = ()

    @property
    def insert(self) -> PluginInsert | None:
        """Back-compat shim — mirrors Output/MasterBus.insert."""
        return self.inserts[0] if self.inserts else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "gain_db": self.gain_db,
            "mute": self.mute,
            "mute_left": self.mute_left,
            "mute_right": self.mute_right,
            "solo": self.solo,
            "inserts": [i.to_dict() for i in self.inserts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Bus:
        raw_inserts = data.get("inserts") or []
        inserts: tuple[PluginInsert, ...] = tuple(
            PluginInsert.from_dict(item) for item in raw_inserts if item
        )
        return cls(
            id=str(data["id"]),
            label=str(data.get("label", "")),
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
            mute_left=bool(data.get("mute_left", False)),
            mute_right=bool(data.get("mute_right", False)),
            solo=bool(data.get("solo", False)),
            inserts=inserts,
        )


@dataclass(frozen=True)
class Vca:
    """A Variable Channel Adjuster — pure control-plane grouping with
    no audio path of its own. A VCA's gain and mute fold into the
    effective gain and mute of every assigned member strip at
    reconcile time, so one fader can pull several sources or outputs
    at once.

    Convention: gain is additive in dB (VCA at -3 dB pushes every
    member 3 dB further down). Mute is OR-ed: muting the VCA mutes
    every member, but unmuting the VCA does NOT unmute a member that
    was already muted on its own — each strip keeps its own mute
    independent of any group it belongs to.

    Members are strip ids drawn from either sources or outputs. A
    strip can belong to multiple VCAs; the gains add and the mutes
    OR together. Members that don't resolve to any current strip are
    silently ignored at reconcile (and pruned by the service when
    detected).
    """

    id: str  # short uuid hex
    label: str  # operator-visible name, e.g. "Backline"
    gain_db: float = 0.0
    mute: bool = False
    # Tuple of strip ids (source.id or output.id). Frozen so the
    # dataclass stays hashable.
    members: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "gain_db": self.gain_db,
            "mute": self.mute,
            "members": list(self.members),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Vca:
        return cls(
            id=str(data["id"]),
            label=str(data.get("label", "")),
            gain_db=float(data.get("gain_db", 0.0)),
            mute=bool(data.get("mute", False)),
            members=tuple(str(x) for x in (data.get("members") or [])),
        )


@dataclass
class MixerState:
    """Aggregate state of the mixer. Mutable so service can swap
    individual entries via dataclasses.replace, but persistence goes
    through `to_dict` / `from_dict` for a stable JSON shape."""

    master: MasterBus = field(default_factory=MasterBus)
    outputs: list[Output] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    buses: list[Bus] = field(default_factory=list)
    vcas: list[Vca] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "master": self.master.to_dict(),
            "outputs": [o.to_dict() for o in self.outputs],
            "sources": [s.to_dict() for s in self.sources],
            "buses": [b.to_dict() for b in self.buses],
            "vcas": [v.to_dict() for v in self.vcas],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MixerState:
        return cls(
            master=MasterBus.from_dict(data.get("master") or {}),
            outputs=[Output.from_dict(o) for o in (data.get("outputs") or [])],
            sources=[Source.from_dict(s) for s in (data.get("sources") or [])],
            buses=[Bus.from_dict(b) for b in (data.get("buses") or [])],
            vcas=[Vca.from_dict(v) for v in (data.get("vcas") or [])],
        )

    def vcas_containing(self, strip_id: str) -> list[Vca]:
        """Return every VCA whose members include this strip id.

        Used by reconcile to fold per-VCA gain/mute into a strip's
        effective values. Caller is expected to combine via:
            effective_gain_db = strip.gain_db + sum(v.gain_db for v in ...)
            effective_mute = strip.mute or any(v.mute for v in ...)
        """
        return [v for v in self.vcas if strip_id in v.members]


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
    "MAX_BUSES",
    "MAX_DELAY_MS",
    "MAX_GAIN_DB",
    "MAX_OUTPUTS",
    "MAX_SOURCES",
    "MAX_VCAS",
    "MIN_GAIN_DB",
    "PLUGIN_BACKENDS",
    "Bus",
    "BusSend",
    "MasterBus",
    "MixerState",
    "Output",
    "PluginInsert",
    "Source",
    "Vca",
    "replace",
    "validate_delay_ms",
    "validate_gain_db",
]
