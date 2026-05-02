"""Tests for PipeWire backend — models, parsers, fake backend."""

from __future__ import annotations

from phonon_stage.pipewire.backend import PwLink, PwNode, PwPort
from phonon_stage.pipewire.cli import parse_pw_dump_links, parse_pw_dump_nodes, parse_pw_dump_ports
from phonon_stage.pipewire.fake import FakePipeWireBackend

# Sample pw-dump output fragment
SAMPLE_PW_DUMP = [
    {
        "id": 30,
        "type": "PipeWire:Interface:Node",
        "info": {
            "state": "idle",
            "props": {
                "node.name": "alsa_output.bcm2835",
                "media.class": "Audio/Sink",
                "node.nick": "bcm2835 Headphones",
            },
        },
    },
    {
        "id": 40,
        "type": "PipeWire:Interface:Port",
        "info": {
            "direction": "input",
            "props": {
                "node.id": 30,
                "port.name": "playback_FL",
                "port.alias": "bcm2835:playback_FL",
            },
        },
    },
    {
        "id": 100,
        "type": "PipeWire:Interface:Link",
        "info": {
            "output-port-id": 42,
            "input-port-id": 40,
            "state": "active",
        },
    },
]


class TestPwDumpParsers:
    def test_parse_nodes(self) -> None:
        nodes = parse_pw_dump_nodes(SAMPLE_PW_DUMP)
        assert len(nodes) == 1
        assert nodes[0]["id"] == 30
        assert nodes[0]["name"] == "alsa_output.bcm2835"
        assert nodes[0]["media_class"] == "Audio/Sink"

    def test_parse_ports(self) -> None:
        ports = parse_pw_dump_ports(SAMPLE_PW_DUMP)
        assert len(ports) == 1
        assert ports[0]["id"] == 40
        assert ports[0]["node_id"] == 30
        assert ports[0]["direction"] == "input"

    def test_parse_links(self) -> None:
        links = parse_pw_dump_links(SAMPLE_PW_DUMP)
        assert len(links) == 1
        assert links[0]["output_port_id"] == 42
        assert links[0]["input_port_id"] == 40
        assert links[0]["state"] == "active"

    def test_parse_empty(self) -> None:
        assert parse_pw_dump_nodes([]) == []
        assert parse_pw_dump_ports([]) == []
        assert parse_pw_dump_links([]) == []


class TestPwModels:
    def test_node_frozen(self) -> None:
        node = PwNode(id=1, name="test", media_class="Audio/Sink", nick="Test", state="idle")
        assert node.id == 1

    def test_port_frozen(self) -> None:
        port = PwPort(id=1, node_id=2, name="FL", direction="input", alias="test:FL")
        assert port.node_id == 2

    def test_link_frozen(self) -> None:
        link = PwLink(id=1, output_port_id=10, input_port_id=20, state="active")
        assert link.state == "active"


class TestFakePipeWireBackend:
    async def test_create_and_list_links(self) -> None:
        backend = FakePipeWireBackend()
        link = await backend.create_link(42, 40)
        assert link.output_port_id == 42
        assert link.input_port_id == 40

        links = await backend.list_links()
        assert len(links) == 1

    async def test_destroy_link(self) -> None:
        backend = FakePipeWireBackend()
        link = await backend.create_link(42, 40)
        await backend.destroy_link(link.id)
        assert await backend.list_links() == []

    async def test_set_volume(self) -> None:
        backend = FakePipeWireBackend()
        await backend.set_node_volume(30, 0.75)
        assert backend.volumes[30] == 0.75

    async def test_list_nodes_with_data(self) -> None:
        nodes = [PwNode(id=1, name="test", media_class="Audio/Sink", nick="T", state="idle")]
        backend = FakePipeWireBackend(nodes=nodes)
        assert len(await backend.list_nodes()) == 1

    async def test_list_ports_filtered(self) -> None:
        ports = [
            PwPort(id=1, node_id=10, name="FL", direction="input", alias="a"),
            PwPort(id=2, node_id=20, name="FL", direction="input", alias="b"),
        ]
        backend = FakePipeWireBackend(ports=ports)
        filtered = await backend.list_ports(node_id=10)
        assert len(filtered) == 1
        assert filtered[0].node_id == 10
