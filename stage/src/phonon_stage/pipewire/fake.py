"""Fake PipeWire backend for tests — in-memory audio graph."""

from __future__ import annotations

from phonon_stage.pipewire.backend import PwLink, PwNode, PwPort


class FakePipeWireBackend:
    """Test double for PipeWireBackend. Maintains an in-memory graph."""

    def __init__(
        self,
        nodes: list[PwNode] | None = None,
        ports: list[PwPort] | None = None,
    ) -> None:
        self.nodes = list(nodes or [])
        self.ports = list(ports or [])
        self.links: list[PwLink] = []
        self.volumes: dict[int, float] = {}
        # Per-channel volumes used by the mixer for L/R mutes.
        # Keyed by node id, value is [left, right] (or any length).
        self.channel_volumes: dict[int, list[float]] = {}
        self.mutes: dict[int, bool] = {}
        self.latency_offsets: dict[int, int] = {}
        # In-memory tracking of loaded modules. Key = synthetic module id,
        # value = (source, sink, latency_msec) tuple. Mirrors pactl's
        # module list for tests to inspect.
        self.loopbacks: dict[int, tuple[str, str, int]] = {}
        # Same idea for null-sinks loaded by source plugins. Loading
        # one also appends a synthetic node + 4 ports (FL/FR x
        # input/output) so list_nodes/list_ports see the same shape
        # as a real PW null-sink.
        self.null_sinks: dict[int, tuple[str, str]] = {}
        self.unloaded_modules: list[int] = []
        self._next_link_id = 100
        self._next_module_id = 536_870_912  # pactl convention for pulse-compat modules

    async def list_nodes(self) -> list[PwNode]:
        return list(self.nodes)

    async def list_ports(self, node_id: int | None = None) -> list[PwPort]:
        if node_id is not None:
            return [p for p in self.ports if p.node_id == node_id]
        return list(self.ports)

    async def list_links(self) -> list[PwLink]:
        return list(self.links)

    async def create_link(self, output_port_id: int, input_port_id: int) -> PwLink:
        link = PwLink(
            id=self._next_link_id,
            output_port_id=output_port_id,
            input_port_id=input_port_id,
            state="active",
        )
        self._next_link_id += 1
        self.links.append(link)
        return link

    async def destroy_link(self, link_id: int) -> None:
        self.links = [lk for lk in self.links if lk.id != link_id]

    async def set_node_volume(self, node_id: int, volume_linear: float) -> None:
        self.volumes[node_id] = volume_linear
        # Mirror to channel_volumes as a 2-channel (mono-equivalent)
        # set so the mixer tests can read either field uniformly.
        self.channel_volumes[node_id] = [volume_linear, volume_linear]

    async def set_node_channel_volumes(
        self, node_id: int, channels: list[float]
    ) -> None:
        self.channel_volumes[node_id] = list(channels)
        # Also write the average to `volumes` so legacy single-value
        # consumers (set_node_volume readers) still get something.
        if channels:
            self.volumes[node_id] = sum(channels) / len(channels)

    async def set_node_mute(self, node_id: int, muted: bool) -> None:
        self.mutes[node_id] = muted

    async def set_node_latency_offset(self, node_id: int, offset_ns: int) -> None:
        self.latency_offsets[node_id] = offset_ns

    async def load_loopback(self, source: str, sink: str, latency_msec: int) -> int | None:
        mid = self._next_module_id
        self._next_module_id += 1
        self.loopbacks[mid] = (source, sink, latency_msec)
        return mid

    async def list_loopback_modules(self) -> dict[int, str]:
        """Mirror the real backend's pactl parser output from the
        in-memory loopbacks dict, so the mixer's orphan-cleanup
        pass works identically in tests."""
        return {
            mid: f"source={src} sink={sink} latency_msec={lat}"
            for mid, (src, sink, lat) in self.loopbacks.items()
        }

    async def load_null_sink(self, name: str, description: str) -> int | None:
        mid = self._next_module_id
        self._next_module_id += 1
        # Record both for tests to inspect, and append a synthetic
        # Audio/Sink node + its monitor ports so list_nodes/list_ports
        # show the same shape as the real PW backend.
        self.null_sinks[mid] = (name, description)
        node_id = max((n.id for n in self.nodes), default=0) + 1
        self.nodes.append(
            PwNode(
                id=node_id,
                name=name,
                media_class="Audio/Sink",
                nick=description,
                state="suspended",
            )
        )
        # Monitor ports (direction=output → exposed as routable source in
        # the patch bay) + playback ports (direction=input → where the
        # source daemon writes its audio).
        next_port = max((p.id for p in self.ports), default=0) + 1
        self.ports.extend(
            [
                PwPort(
                    id=next_port,
                    node_id=node_id,
                    name="monitor_FL",
                    direction="output",
                    alias=f"{description}:monitor_FL",
                ),
                PwPort(
                    id=next_port + 1,
                    node_id=node_id,
                    name="monitor_FR",
                    direction="output",
                    alias=f"{description}:monitor_FR",
                ),
                PwPort(
                    id=next_port + 2,
                    node_id=node_id,
                    name="playback_FL",
                    direction="input",
                    alias=f"{description}:playback_FL",
                ),
                PwPort(
                    id=next_port + 3,
                    node_id=node_id,
                    name="playback_FR",
                    direction="input",
                    alias=f"{description}:playback_FR",
                ),
            ]
        )
        return mid

    async def unload_module(self, module_id: int) -> None:
        self.loopbacks.pop(module_id, None)
        if module_id in self.null_sinks:
            name, _ = self.null_sinks.pop(module_id)
            # Remove the synthetic node + its ports so the graph state
            # mirrors what `pactl unload-module` would actually do.
            removed_node_ids = {n.id for n in self.nodes if n.name == name}
            self.nodes = [n for n in self.nodes if n.name != name]
            self.ports = [p for p in self.ports if p.node_id not in removed_node_ids]
        self.unloaded_modules.append(module_id)
