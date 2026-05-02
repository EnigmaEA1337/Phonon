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
        self.latency_offsets: dict[int, int] = {}
        self._next_link_id = 100

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

    async def set_node_latency_offset(self, node_id: int, offset_ns: int) -> None:
        self.latency_offsets[node_id] = offset_ns
