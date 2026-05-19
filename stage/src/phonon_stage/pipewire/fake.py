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
        # Per-channel volumes used by the mixer for L/R mutes. Keyed
        # by NODE NAME (not id) — mirrors the real backend which uses
        # pactl with the sink name because PA's ID namespace doesn't
        # match PW's. Value is [left, right] (or any length).
        self.channel_volumes: dict[str, list[float]] = {}
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
        # Filter-chain state — keyed by chain name (matching the real
        # backend's conf filename). Each entry is the raw conf body
        # the service generated so tests can assert on its content.
        self.filter_chain_confs: dict[str, str] = {}
        # New (post-2026-05-19): filter-chain modules loaded individually
        # via pactl load-module. Map module_id → chain_name. Tests can
        # assert on this to verify the no-cascade path is exercised.
        self.filter_chain_modules: dict[int, str] = {}
        # Live control values per chain node, keyed by (node_name,
        # control_name). Mirrors what `pw-cli set-param Props` would
        # leave inside the running filter-chain.
        self.filter_chain_controls: dict[tuple[str, str], float] = {}
        # How many times reload_filter_chain has been called. Lets
        # tests check that the service doesn't reload on every
        # control tweak (the whole point of the live-param path).
        self.filter_chain_reload_count: int = 0

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
        # Also record by-name (resolved from the nodes list) so the
        # mixer tests that introspect channel_volumes by name find
        # it regardless of which write path the service took.
        name = next((n.name for n in self.nodes if n.id == node_id), None)
        if name is not None:
            self.channel_volumes[name] = [volume_linear, volume_linear]

    async def set_node_channel_volumes(self, node_name: str, channels: list[float]) -> None:
        """Mirror the real backend's name-based API. Tests keyed by
        node_name read this directly via `fake_pw.channel_volumes`."""
        self.channel_volumes[node_name] = list(channels)
        # Maintain the legacy by-id `volumes` dict as well so any
        # existing reader looking up the average via node id still
        # gets a reasonable value.
        node = next((n for n in self.nodes if n.name == node_name), None)
        if node is not None and channels:
            self.volumes[node.id] = sum(channels) / len(channels)

    async def set_node_mute(self, node_id: int, muted: bool) -> None:
        self.mutes[node_id] = muted

    async def set_node_latency_offset(self, node_id: int, offset_ns: int) -> None:
        self.latency_offsets[node_id] = offset_ns

    async def load_loopback(self, source: str, sink: str, latency_msec: int) -> int | None:
        mid = self._next_module_id
        self._next_module_id += 1
        self.loopbacks[mid] = (source, sink, latency_msec)
        return mid

    async def list_null_sink_modules(self) -> dict[int, str]:
        """Mirror the real backend output for tests — {mid: sink_name}."""
        return {mid: name for mid, (name, _desc) in self.null_sinks.items()}

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
        if module_id in self.filter_chain_modules:
            chain_name = self.filter_chain_modules.pop(module_id)
            # Drop the synthesised node + the conf entry so the graph
            # mirrors a real pactl unload-module of a filter-chain.
            self.nodes = [n for n in self.nodes if n.name != chain_name]
            self.filter_chain_confs.pop(chain_name, None)
        self.unloaded_modules.append(module_id)

    # ── Filter-chain (DSP plugin insert) ──────────────────────────

    async def write_filter_chain_conf(self, chain_name: str, conf_body: str) -> None:
        self.filter_chain_confs[chain_name] = conf_body
        # Simulate the chain showing up in the graph after a reload.
        # We don't synthesize ports here — the mixer service doesn't
        # need them (it doesn't link to the filter-chain node, the
        # chain handles its own capture/playback). Tests that need
        # the node visible can call `_simulate_filter_chain_node`.

    async def delete_filter_chain_conf(self, chain_name: str) -> None:
        self.filter_chain_confs.pop(chain_name, None)
        # Drop any control values tied to this chain — keyed by node
        # name though, not conf name, so this is a no-op unless the
        # caller used matching names (which is what the service does).

    async def list_filter_chain_confs(self) -> list[str]:
        return list(self.filter_chain_confs.keys())

    async def reload_filter_chain(self) -> None:
        self.filter_chain_reload_count += 1
        # After a reload, the synthesized nodes for each conf should
        # exist in `self.nodes`. We add them lazily here so tests
        # see the same shape they'd see on a real Stage post-reload.
        for chain_name in self.filter_chain_confs:
            if not any(n.name == chain_name for n in self.nodes):
                node_id = max((n.id for n in self.nodes), default=0) + 1
                self.nodes.append(
                    PwNode(
                        id=node_id,
                        name=chain_name,
                        media_class="Audio/Sink",
                        nick=chain_name,
                        state="running",
                    )
                )
        # Conversely, drop synthesized nodes whose conf has gone.
        chain_names = set(self.filter_chain_confs)
        self.nodes = [
            n
            for n in self.nodes
            if not (n.name.startswith("phonon_fx_") and n.name not in chain_names)
        ]

    # ── pactl-based filter-chain (new path, no service restart) ────

    async def load_filter_chain(self, args: list[str]) -> int | None:
        """Mirror of RealPipeWireBackend.load_filter_chain. Parses the
        node.name + media.name out of the args list to keep track of
        which chains are loaded by id."""
        # Extract node.name (used as the synthesized PwNode name).
        chain_name = ""
        for a in args:
            if a.startswith("node.name="):
                chain_name = a.split("=", 1)[1]
                break
        if not chain_name:
            return None
        mid = self._next_module_id
        self._next_module_id += 1
        # Record under the existing filter_chain_confs map (re-uses the
        # tests' inspection surface — they assert on the chain being
        # present) but stash the args list too for richer assertions.
        self.filter_chain_confs[chain_name] = " ".join(args)
        self.filter_chain_modules[mid] = chain_name
        # Synthesize a node so list_nodes() shows the chain — same as
        # the conf+reload path does after reload_filter_chain.
        if not any(n.name == chain_name for n in self.nodes):
            node_id = max((n.id for n in self.nodes), default=0) + 1
            self.nodes.append(
                PwNode(
                    id=node_id,
                    name=chain_name,
                    media_class="Audio/Sink",
                    nick=chain_name,
                    state="running",
                )
            )
        return mid

    async def list_filter_chain_modules(self) -> dict[int, str]:
        return dict(self.filter_chain_modules)

    async def set_filter_node_control(
        self, node_name: str, control_name: str, value: float
    ) -> None:
        self.filter_chain_controls[(node_name, control_name)] = float(value)

    async def read_filter_node_controls(self, node_name: str) -> dict[str, float]:
        """Return everything the test has set on this chain via
        set_filter_node_control. Tests that need to assert on monitoring
        behaviour pre-populate `filter_chain_controls` themselves."""
        return {ctl: v for (n, ctl), v in self.filter_chain_controls.items() if n == node_name}
