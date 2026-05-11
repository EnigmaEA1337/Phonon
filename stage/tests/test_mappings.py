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


class TestMappingLoopbackDelay:
    """delay_ms > 0 routes via pactl module-loopback (real buffered delay)
    instead of direct pw-links. These tests pin the transport-switching
    logic so a future refactor doesn't silently regress to the broken
    `latencyOffsetNsec` path."""

    async def test_create_with_delay_loads_loopback(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42, 43],
            sink_node_id=32,
            sink_port_ids=[44, 45],
            delay_ms=150.0,
        )
        # Loopback path: NO direct pw-links, but ONE module-loopback loaded.
        assert m.delay_ms == 150.0
        assert m.loopback_module_id is not None
        assert len(m.link_ids) == 0
        assert len(fake_pw.links) == 0
        assert len(fake_pw.loopbacks) == 1
        (src, sink, latency) = next(iter(fake_pw.loopbacks.values()))
        # Source is Audio/Source (alsa_input.usb-DG60) → name used directly,
        # no .monitor suffix.
        assert src == "alsa_input.usb-DG60"
        assert sink == "alsa_output.usb-DG60"
        assert latency == 150

    async def test_create_with_delay_zero_uses_links(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42, 43],
            sink_node_id=32,
            sink_port_ids=[44, 45],
            delay_ms=0.0,
        )
        # delay==0 keeps the cheap direct-link path
        assert m.loopback_module_id is None
        assert len(m.link_ids) == 2
        assert len(fake_pw.loopbacks) == 0

    async def test_update_delay_zero_to_positive_swaps_transport(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=32,
            sink_port_ids=[44],
        )
        assert len(fake_pw.links) == 1
        assert len(fake_pw.loopbacks) == 0

        # Move from 0 → 200 ms: pw-link must go, loopback must appear.
        updated = await mapping_service.update_mapping(m.id, delay_ms=200.0)
        assert updated.delay_ms == 200.0
        assert updated.loopback_module_id is not None
        assert len(updated.link_ids) == 0
        assert len(fake_pw.links) == 0  # original link destroyed
        assert len(fake_pw.loopbacks) == 1

    async def test_update_delay_positive_to_zero_swaps_transport(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42, 43],
            sink_node_id=32,
            sink_port_ids=[44, 45],
            delay_ms=100.0,
        )
        assert m.loopback_module_id is not None
        old_module = m.loopback_module_id

        # Move from 100 → 0 ms: loopback must go, pw-links must appear.
        updated = await mapping_service.update_mapping(m.id, delay_ms=0.0)
        assert updated.delay_ms == 0.0
        assert updated.loopback_module_id is None
        assert len(updated.link_ids) == 2
        assert old_module in fake_pw.unloaded_modules
        assert len(fake_pw.loopbacks) == 0

    async def test_update_delay_positive_to_positive_reloads_loopback(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=32,
            sink_port_ids=[44],
            delay_ms=100.0,
        )
        first_module = m.loopback_module_id
        assert first_module is not None
        assert fake_pw.loopbacks[first_module][2] == 100

        # 100 → 300 ms: old module unloaded, new one loaded with new latency.
        updated = await mapping_service.update_mapping(m.id, delay_ms=300.0)
        assert updated.delay_ms == 300.0
        assert updated.loopback_module_id != first_module
        assert first_module in fake_pw.unloaded_modules
        assert fake_pw.loopbacks[updated.loopback_module_id][2] == 300

    async def test_delete_unloads_loopback(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        m = await mapping_service.create_mapping(
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=32,
            sink_port_ids=[44],
            delay_ms=80.0,
        )
        mod_id = m.loopback_module_id
        assert mod_id is not None
        await mapping_service.delete_mapping(m.id)
        assert mod_id in fake_pw.unloaded_modules
        assert mod_id not in fake_pw.loopbacks

    async def test_source_sink_uses_monitor_suffix(
        self, mapping_service: MappingService, fake_pw: FakePipeWireBackend
    ) -> None:
        """When the source is itself a Sink (null-sink whose monitor is
        the readable side — our BT-capture bridge), the loopback must be
        wired to <sink>.monitor, not bare <sink>."""
        # node 30 is Audio/Sink in the fixtures — treat it as a null-sink
        # source for this test by using it as the source.
        await mapping_service.create_mapping(
            source_node_id=30,
            source_port_ids=[40],
            sink_node_id=32,
            sink_port_ids=[44],
            delay_ms=50.0,
        )
        (src, _sink, _lat) = next(iter(fake_pw.loopbacks.values()))
        assert src == "alsa_output.bcm2835.monitor"
