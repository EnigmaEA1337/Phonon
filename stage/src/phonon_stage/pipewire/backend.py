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
    # ALSA backing — populated for nodes whose factory is api.alsa.pcm.{sink,source}
    # so the UI can join PipeWire-side stream volume with the underlying
    # hardware mixer state. Empty for non-ALSA nodes (BT, network streams).
    alsa_card: str = ""  # e.g. "DG60" (matches `aplay -l` card name)


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

    # Per-channel volume — needed for L/R independent muting in the
    # mix console. `node_name` is the PW node name (NOT the numeric
    # id — pactl maintains its own ID namespace and doesn't reliably
    # accept PW node IDs; the node name is what PW exposes to PA as
    # the sink/source name). `channels` is a list of linear volumes,
    # one per output channel (typically [left, right]).
    async def set_node_channel_volumes(self, node_name: str, channels: list[float]) -> None: ...

    async def set_node_mute(self, node_id: int, muted: bool) -> None: ...

    async def set_node_latency_offset(self, node_id: int, offset_ns: int) -> None: ...

    # Module-loopback management for delay-aware mappings. pactl's
    # module-loopback with latency_msec=N inserts a real audio buffer
    # of N milliseconds between a source and a sink — the proper
    # mechanism for per-mapping audible delay (as opposed to the
    # latencyOffsetNsec scheduling hint which doesn't buffer).
    async def load_loopback(self, source: str, sink: str, latency_msec: int) -> int | None: ...

    async def unload_module(self, module_id: int) -> None: ...

    # Module-null-sink management for source plugins. A plugin that
    # spawns an upstream daemon (shairport-sync, librespot…) wants the
    # daemon to write into a dedicated null-sink rather than the
    # default sink — that way the null-sink's monitor port becomes a
    # routable Audio/Source in the patch bay, and the user keeps full
    # control over where the audio goes (no auto-routing to the
    # default destination).
    async def load_null_sink(self, name: str, description: str) -> int | None: ...

    # Module enumeration — needed at startup to find orphan loopbacks
    # left behind by a previous daemon session. Returns a dict of
    # {module_id: argument_string} for loopback modules only (other
    # module types are noise for our use case). The argument string
    # is the raw pactl form, e.g.
    # "source=phonon_master.monitor sink=alsa_output.dg60_1 latency_msec=90".
    async def list_loopback_modules(self) -> dict[int, str]: ...

    # Same idea as list_loopback_modules but for module-null-sink.
    # Returns {module_id: sink_name_argument} so callers can find and
    # unload duplicate null-sinks (e.g. two `phonon_master` instances
    # left by a boot-time race).
    async def list_null_sink_modules(self) -> dict[int, str]: ...

    # ── Filter-chain (DSP plugin insert) ──────────────────────────
    #
    # Filter-chains are PipeWire's mechanism for running LADSPA/LV2
    # plugins inline between PW nodes. We use them as the master→output
    # path replacement when an Output has a plugin insert: capture from
    # `phonon_master` → through the plugin → playback into the output
    # sink. The chain is described in a conf fragment loaded by the
    # filter-chain.service systemd user unit. Adding/removing a chain
    # rewrites the conf and reloads the service. Live control changes
    # bypass the reload — they go through pw-cli set-param against the
    # running node so the audio doesn't glitch on every fader move.
    #
    # `write_filter_chain_conf` creates (or overwrites) one conf file
    # for the named chain. `delete_filter_chain_conf` removes it.
    # `reload_filter_chain` restarts the filter-chain.service so PW
    # picks up the change. `set_filter_node_control` updates one
    # control on a live chain without reload.
    async def write_filter_chain_conf(self, chain_name: str, conf_body: str) -> None: ...

    async def delete_filter_chain_conf(self, chain_name: str) -> None: ...

    # List the chain names whose conf files we already own on disk
    # (i.e. created by a previous phonon-stage session). Lets the
    # mixer prune orphans at init the same way it does for loopbacks.
    async def list_filter_chain_confs(self) -> list[str]: ...

    async def reload_filter_chain(self) -> None: ...

    async def set_filter_node_control(
        self, node_name: str, control_name: str, value: float
    ) -> None: ...
