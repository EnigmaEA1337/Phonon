"""Tests for MappingService — create, delete, update, restore."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.mappings.store import MappingStoreError

if TYPE_CHECKING:
    from phonon_stage.mappings.service import MappingService
    from phonon_stage.mappings.store import MappingStore
    from phonon_stage.pipewire.fake import FakePipeWireBackend


class TestMappingService:
    async def test_create_mapping(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42, 43],
            sink_node_id=30,
            sink_port_ids=[40, 41],
        )
        assert m.id
        assert len(m.link_ids) == 2
        assert len(fake_pw.links) == 2

    async def test_create_mapping_muted(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
            mute=True,
        )
        assert m.mute
        assert len(m.link_ids) == 0
        assert len(fake_pw.links) == 0

    async def test_delete_mapping(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
        )
        await mapping_service.delete_mapping(m.id)
        assert len(mapping_service.mappings) == 0
        assert len(fake_pw.links) == 0

    async def test_update_gain(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
        )
        updated = await mapping_service.update_mapping(m.id, gain_db=-6.0)
        assert updated.gain_db == -6.0
        assert 30 in fake_pw.volumes

    async def test_toggle_mute(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
        )
        assert len(fake_pw.links) == 1

        muted = await mapping_service.update_mapping(m.id, mute=True)
        assert muted.mute
        assert len(fake_pw.links) == 0

        unmuted = await mapping_service.update_mapping(m.id, mute=False)
        assert not unmuted.mute
        assert len(fake_pw.links) == 1

    async def test_max_mappings(self, mapping_service: MappingService) -> None:
        for _i in range(8):
            await mapping_service.create_mapping(
                source_node_id=31,
                source_port_ids=[42],
                sink_node_id=30,
                sink_port_ids=[40],
            )
        with pytest.raises(MappingStoreError, match="Maximum"):
            await mapping_service.create_mapping(
                source_node_id=31,
                source_port_ids=[42],
                sink_node_id=30,
                sink_port_ids=[40],
            )

    async def test_invalid_gain(self, mapping_service: MappingService) -> None:
        with pytest.raises(ValueError, match="out of range"):
            await mapping_service.create_mapping(
                source_node_id=31,
                source_port_ids=[42],
                sink_node_id=30,
                sink_port_ids=[40],
                gain_db=15.0,
            )

    async def test_restore_mappings(
        self,
        mapping_service: MappingService,
        mapping_store: MappingStore,
        fake_pw: FakePipeWireBackend,
    ) -> None:
        # Create and persist a mapping
        await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
        )
        assert len(fake_pw.links) == 1

        # Clear PW links (simulating restart)
        fake_pw.links.clear()
        assert len(fake_pw.links) == 0

        # Restore re-resolves the mapping by node name and pairs every
        # output port of the source with every input port of the sink.
        # Node 31 has 2 output ports (FL+FR) and node 30 has 2 input ports
        # (FL+FR), so we get 2 links — one per channel.
        await mapping_service.restore_mappings()
        assert len(fake_pw.links) == 2
