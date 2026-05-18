"""Tests for the PluginInsert path in the mixer service.

Covers: model round-trip, filter-chain conf generation, fake backend's
filter-chain bookkeeping, the no-reload guarantee on live control
changes, orphan cleanup at init, and the introspector-driven defaults."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.dsp.ladspa import (
    FakeLadspaIntrospector,
    PluginControl,
    PluginDescriptor,
)
from phonon_stage.mixer.filter_chain import (
    CHAIN_NAME_PREFIX,
    chain_name_for,
    render_filter_chain_conf,
)
from phonon_stage.mixer.models import Output, PluginInsert
from phonon_stage.mixer.service import MixerError, MixerService
from phonon_stage.mixer.store import MixerStore
from phonon_stage.pipewire.backend import PwNode, PwPort
from phonon_stage.pipewire.fake import FakePipeWireBackend

if TYPE_CHECKING:
    from pathlib import Path

LSP_LABEL = "http://lsp-plug.in/plugins/ladspa/comp_delay_stereo"
LSP_LIBRARY = "lsp-plugins-ladspa"


def _lsp_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        library=LSP_LIBRARY,
        label=LSP_LABEL,
        name="LSP Compressor Delay Stereo",
        maker="Vladimir Sadovnikov",
        controls=(
            PluginControl(name="Input L", direction=""),
            PluginControl(name="Input R", direction=""),
            PluginControl(name="Output L", direction=""),
            PluginControl(name="Output R", direction=""),
            PluginControl(name="Bypass", direction="input", toggled=True, default=0.0),
            PluginControl(
                name="Mode",
                direction="input",
                integer=True,
                minimum=0.0,
                maximum=2.0,
                default=2.0,
            ),
            PluginControl(name="Ramping", direction="input", toggled=True, default=1.0),
            PluginControl(
                name="Time (ms)",
                direction="input",
                minimum=0.0,
                maximum=1000.0,
                default=5.0,
            ),
            PluginControl(
                name="Delay time (ms)",  # output (read-only) port
                direction="output",
                default=None,
            ),
        ),
    )


def _sample_pw() -> FakePipeWireBackend:
    nodes = [
        PwNode(
            id=10,
            name="alsa_output.dg60_1",
            media_class="Audio/Sink",
            nick="DG60 #1",
            state="idle",
        ),
    ]
    ports = [
        PwPort(id=100, node_id=10, name="playback_FL", direction="input", alias="dg60:FL"),
        PwPort(id=101, node_id=10, name="playback_FR", direction="input", alias="dg60:FR"),
    ]
    return FakePipeWireBackend(nodes=nodes, ports=ports)


@pytest.fixture()
def fake_pw() -> FakePipeWireBackend:
    return _sample_pw()


@pytest.fixture()
def store(tmp_path: Path) -> MixerStore:
    return MixerStore(tmp_path / "mixer.conf.json")


@pytest.fixture()
def introspector() -> FakeLadspaIntrospector:
    return FakeLadspaIntrospector({(LSP_LIBRARY, LSP_LABEL): _lsp_descriptor()})


@pytest.fixture()
async def service(
    fake_pw: FakePipeWireBackend,
    store: MixerStore,
    introspector: FakeLadspaIntrospector,
) -> MixerService:
    svc = MixerService(pw_backend=fake_pw, store=store, introspector=introspector)
    await svc.init()
    return svc


# ── Model ──────────────────────────────────────────────────────────────


class TestPluginInsertModel:
    def test_roundtrip(self) -> None:
        ins = PluginInsert(
            backend="ladspa",
            library=LSP_LIBRARY,
            label=LSP_LABEL,
            controls={"Time (ms)": 80.0, "Mode": 2.0, "Ramping": 1.0},
            enabled=True,
        )
        assert PluginInsert.from_dict(ins.to_dict()) == ins

    def test_attached_to_output(self) -> None:
        o = Output(
            id="abc12345",
            sink_node_name="alsa_output.dg60_1",
            label="DG60 #1",
            inserts=(PluginInsert(backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL),),
        )
        assert Output.from_dict(o.to_dict()) == o

    def test_output_without_insert_roundtrips(self) -> None:
        o = Output(id="x", sink_node_name="y", label="L")
        d = o.to_dict()
        assert d["inserts"] == []
        assert Output.from_dict(d).inserts == ()
        # Back-compat shim: .insert returns None when chain is empty.
        assert Output.from_dict(d).insert is None

    def test_output_reads_legacy_insert_field(self) -> None:
        # Before the multi-plugin migration, persistence wrote a single
        # `insert: dict | None` field. from_dict must keep reading it
        # so old state.json files don't break on upgrade.
        legacy = {
            "id": "x",
            "sink_node_name": "y",
            "label": "L",
            "insert": {
                "backend": "ladspa",
                "library": LSP_LIBRARY,
                "label": LSP_LABEL,
                "controls": {},
                "enabled": True,
            },
        }
        o = Output.from_dict(legacy)
        assert len(o.inserts) == 1
        assert o.inserts[0].label == LSP_LABEL


# ── Filter-chain conf rendering ────────────────────────────────────────


class TestConfRendering:
    def test_chain_name_is_stable_per_output_id(self) -> None:
        o1 = Output(id="abc12345", sink_node_name="x", label="L1")
        o2 = Output(id="abc12345", sink_node_name="different", label="L2")
        assert chain_name_for(o1) == chain_name_for(o2)
        assert chain_name_for(o1).startswith(CHAIN_NAME_PREFIX)

    def test_conf_contains_plugin_metadata(self) -> None:
        o = Output(id="abc12345", sink_node_name="alsa_output.dg60_1", label="DG60")
        ins = PluginInsert(
            backend="ladspa",
            library=LSP_LIBRARY,
            label=LSP_LABEL,
            controls={"Time (ms)": 80.0, "Mode": 2.0},
        )
        body = render_filter_chain_conf(o, ins, "phonon_master")
        assert "libpipewire-module-filter-chain" in body
        assert LSP_LABEL in body
        assert LSP_LIBRARY in body
        assert "phonon_master" in body
        assert "alsa_output.dg60_1" in body
        # Control names with spaces must be quoted
        assert '"Time (ms)" = 80' in body
        # Bare-identifier names stay unquoted
        assert "Mode = 2" in body

    def test_controls_are_sorted_for_deterministic_diffs(self) -> None:
        o = Output(id="x", sink_node_name="y", label="L")
        ins = PluginInsert(
            backend="ladspa",
            library=LSP_LIBRARY,
            label=LSP_LABEL,
            controls={"Zulu": 0, "Alpha": 0, "Mike": 0},
        )
        body = render_filter_chain_conf(o, ins, "phonon_master")
        pos_alpha = body.index("Alpha = 0")
        pos_mike = body.index("Mike = 0")
        pos_zulu = body.index("Zulu = 0")
        assert pos_alpha < pos_mike < pos_zulu


# ── Fake backend filter-chain bookkeeping ─────────────────────────────


class TestFakeBackendFilterChain:
    @pytest.mark.asyncio()
    async def test_write_then_list(self, fake_pw: FakePipeWireBackend) -> None:
        await fake_pw.write_filter_chain_conf("chain_a", "body a")
        await fake_pw.write_filter_chain_conf("chain_b", "body b")
        assert sorted(await fake_pw.list_filter_chain_confs()) == ["chain_a", "chain_b"]

    @pytest.mark.asyncio()
    async def test_delete_drops_conf(self, fake_pw: FakePipeWireBackend) -> None:
        await fake_pw.write_filter_chain_conf("chain_a", "body")
        await fake_pw.delete_filter_chain_conf("chain_a")
        assert await fake_pw.list_filter_chain_confs() == []

    @pytest.mark.asyncio()
    async def test_reload_synthesizes_chain_node(self, fake_pw: FakePipeWireBackend) -> None:
        await fake_pw.write_filter_chain_conf("phonon_fx_abc", "...")
        await fake_pw.reload_filter_chain()
        nodes = await fake_pw.list_nodes()
        assert any(n.name == "phonon_fx_abc" for n in nodes)

    @pytest.mark.asyncio()
    async def test_set_control_records_value(self, fake_pw: FakePipeWireBackend) -> None:
        await fake_pw.set_filter_node_control("phonon_fx_x", "Time (ms)", 80.0)
        assert fake_pw.filter_chain_controls[("phonon_fx_x", "Time (ms)")] == 80.0


# ── Service: attach plugin, reconcile, live control update ────────────


class TestServicePluginInsert:
    @pytest.mark.asyncio()
    async def test_attach_plugin_seeds_defaults_from_introspector(
        self, service: MixerService
    ) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        result = await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        assert result.insert is not None
        # Audio ports + output controls are dropped, only user-set
        # input controls remain — and they get the descriptor defaults.
        assert result.insert.controls["Time (ms)"] == 5.0
        assert result.insert.controls["Mode"] == 2.0
        assert result.insert.controls["Ramping"] == 1.0
        assert "Input L" not in result.insert.controls
        assert "Delay time (ms)" not in result.insert.controls

    @pytest.mark.asyncio()
    async def test_attach_writes_filter_chain_conf(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        prior_reload_count = fake_pw.filter_chain_reload_count
        await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        chain = chain_name_for(out)
        assert chain in fake_pw.filter_chain_confs
        # Reload happened exactly once for the attach.
        assert fake_pw.filter_chain_reload_count == prior_reload_count + 1

    @pytest.mark.asyncio()
    async def test_attach_skips_loopback_for_that_output(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        # Initially the master→output loopback exists.
        assert any(
            sink == "alsa_output.dg60_1" for (_src, sink, _lat) in fake_pw.loopbacks.values()
        )
        await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        # After attaching the plugin the loopback is gone — chain
        # takes its place.
        assert not any(
            sink == "alsa_output.dg60_1" for (_src, sink, _lat) in fake_pw.loopbacks.values()
        )

    @pytest.mark.asyncio()
    async def test_detach_restores_loopback(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        await service.set_output_insert(out.id, backend=None, library=None, label=None)
        # Conf gone, loopback back.
        assert chain_name_for(out) not in fake_pw.filter_chain_confs
        assert any(
            sink == "alsa_output.dg60_1" for (_src, sink, _lat) in fake_pw.loopbacks.values()
        )

    @pytest.mark.asyncio()
    async def test_live_control_update_does_not_reload(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        reloads_before = fake_pw.filter_chain_reload_count
        await service.update_output_insert_control(out.id, "Time (ms)", 80.0)
        # Live update goes through pw-cli set-param, not a reload.
        assert fake_pw.filter_chain_reload_count == reloads_before
        assert fake_pw.filter_chain_controls[(chain_name_for(out), "Time (ms)")] == 80.0

    @pytest.mark.asyncio()
    async def test_live_control_persists_to_state(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        await service.update_output_insert_control(out.id, "Time (ms)", 80.0)
        # And it's persisted so a daemon restart picks it up.
        refreshed = service.outputs[0]
        assert refreshed.insert is not None
        assert refreshed.insert.controls["Time (ms)"] == 80.0

    @pytest.mark.asyncio()
    async def test_live_control_unknown_name_raises(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        with pytest.raises(MixerError, match="unknown control"):
            await service.update_output_insert_control(out.id, "Nonexistent", 1.0)

    @pytest.mark.asyncio()
    async def test_live_control_without_insert_raises(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        with pytest.raises(MixerError, match="no plugin insert"):
            await service.update_output_insert_control(out.id, "Time (ms)", 80.0)

    @pytest.mark.asyncio()
    async def test_no_reload_on_unrelated_fader_move(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """A fader move on a chain'd output triggers reconcile but the
        wanted-chains set is unchanged → no filter-chain.service
        reload. Critical for live-mix responsiveness."""
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.set_output_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        reloads_after_attach = fake_pw.filter_chain_reload_count
        # Volume-only change → fast path, no reconcile at all.
        await service.update_output(out.id, gain_db=-6.0)
        assert fake_pw.filter_chain_reload_count == reloads_after_attach
        # Mute toggle → full reconcile. Chain stays in wanted set,
        # body unchanged → still no reload.
        await service.update_output(out.id, mute=True)
        await service.update_output(out.id, mute=False)
        assert fake_pw.filter_chain_reload_count == reloads_after_attach


# ── Orphan cleanup at init ─────────────────────────────────────────────


class TestOrphanChainCleanup:
    @pytest.mark.asyncio()
    async def test_init_clears_stale_chain_confs(
        self,
        fake_pw: FakePipeWireBackend,
        store: MixerStore,
        introspector: FakeLadspaIntrospector,
    ) -> None:
        # Simulate a chain left behind by a previous daemon — no
        # matching output in the persisted state.
        await fake_pw.write_filter_chain_conf("phonon_fx_dead", "stale body")
        svc = MixerService(pw_backend=fake_pw, store=store, introspector=introspector)
        await svc.init()
        # Cleanup wipes the stale conf and reloads once at init.
        assert "phonon_fx_dead" not in fake_pw.filter_chain_confs

    @pytest.mark.asyncio()
    async def test_init_no_chains_no_reload(
        self,
        fake_pw: FakePipeWireBackend,
        store: MixerStore,
        introspector: FakeLadspaIntrospector,
    ) -> None:
        """When the disk has no stale chains, init should not reload
        — keeps Pi startup time bounded for the no-plugin case."""
        svc = MixerService(pw_backend=fake_pw, store=store, introspector=introspector)
        await svc.init()
        assert fake_pw.filter_chain_reload_count == 0


# ── Multi-plugin chain ─────────────────────────────────────────────────


class TestMultiPluginChain:
    """Coverage for the v1 socle: append / remove / reset / cap."""

    @pytest.mark.asyncio()
    async def test_append_grows_chain(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        result = await service.append_chain_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        assert len(result.inserts) == 1
        result = await service.append_chain_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        assert len(result.inserts) == 2
        # Both slots independent (different defaults dicts in memory).
        assert result.inserts[0] is not result.inserts[1]

    @pytest.mark.asyncio()
    async def test_append_caps_at_max_chain_depth(self, service: MixerService) -> None:
        from phonon_stage.mixer.models import MAX_CHAIN_DEPTH

        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        for _ in range(MAX_CHAIN_DEPTH):
            await service.append_chain_insert(
                out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
            )
        with pytest.raises(MixerError, match="chain depth at cap"):
            await service.append_chain_insert(
                out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
            )

    @pytest.mark.asyncio()
    async def test_remove_specific_slot(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.append_chain_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        await service.append_chain_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        result = await service.remove_chain_insert(out.id, slot=0)
        assert len(result.inserts) == 1

    @pytest.mark.asyncio()
    async def test_remove_invalid_slot_raises(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        with pytest.raises(MixerError, match="out of range"):
            await service.remove_chain_insert(out.id, slot=0)

    @pytest.mark.asyncio()
    async def test_reset_chain_clears_everything(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.append_chain_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        await service.append_chain_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        result = await service.reset_chain(out.id)
        assert result.inserts == ()

    @pytest.mark.asyncio()
    async def test_reset_empty_chain_is_noop(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        result = await service.reset_chain(out.id)
        # No exception, no change.
        assert result.inserts == ()

    @pytest.mark.asyncio()
    async def test_set_insert_enabled_toggle(self, service: MixerService) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        await service.append_chain_insert(
            out.id, backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL
        )
        # Disable slot 0 → renders passthrough conf.
        result = await service.set_insert_enabled(out.id, slot=0, enabled=False)
        assert result.inserts[0].enabled is False
        # Re-enable → back to normal chain.
        result = await service.set_insert_enabled(out.id, slot=0, enabled=True)
        assert result.inserts[0].enabled is True

    @pytest.mark.asyncio()
    async def test_set_insert_enabled_invalid_slot_raises(
        self, service: MixerService
    ) -> None:
        out = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60 #1")
        with pytest.raises(MixerError, match="out of range"):
            await service.set_insert_enabled(out.id, slot=0, enabled=False)

    def test_render_multi_plugin_conf_chains_them_in_series(self) -> None:
        from phonon_stage.mixer.models import Output, PluginInsert

        o = Output(
            id="abc12345",
            sink_node_name="alsa_output.dg60_1",
            label="DG60",
            inserts=(
                PluginInsert(backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL),
                PluginInsert(backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL),
            ),
        )
        body = render_filter_chain_conf(o, o.inserts, "phonon_master")
        # Two distinct plugin nodes in the chain — fx_0 and fx_1.
        assert "name = fx_0" in body
        assert "name = fx_1" in body
        # Explicit links block wires fx_0:Output → fx_1:Input on both
        # channels — without this, fx_1 gets no audio and the chain
        # is silent past slot 0.
        assert "links = [" in body
        assert 'output = "fx_0:Output L"' in body
        assert 'input = "fx_1:Input L"' in body
        assert 'output = "fx_0:Output R"' in body
        assert 'input = "fx_1:Input R"' in body

    def test_render_single_plugin_no_links_block(self) -> None:
        # One plugin = capture/playback auto-route, no explicit links
        # needed. Skipping the block avoids a noisy empty section.
        from phonon_stage.mixer.models import Output, PluginInsert

        o = Output(
            id="abc12345",
            sink_node_name="alsa_output.dg60_1",
            label="DG60",
            inserts=(
                PluginInsert(backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL),
            ),
        )
        body = render_filter_chain_conf(o, o.inserts, "phonon_master")
        assert "links = [" not in body

    def test_render_three_plugin_chain_links_consecutive(self) -> None:
        # 3 plugins → 2 link pairs (fx_0→fx_1, fx_1→fx_2).
        from phonon_stage.mixer.models import Output, PluginInsert

        o = Output(
            id="abc12345",
            sink_node_name="alsa_output.dg60_1",
            label="DG60",
            inserts=tuple(
                PluginInsert(backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL)
                for _ in range(3)
            ),
        )
        body = render_filter_chain_conf(o, o.inserts, "phonon_master")
        assert 'input = "fx_1:Input L"' in body
        assert 'input = "fx_2:Input L"' in body
        # No phantom fx_3.
        assert "fx_3:Input L" not in body

    def test_render_with_all_bypassed_emits_passthrough(self) -> None:
        from phonon_stage.mixer.models import Output, PluginInsert

        o = Output(
            id="abc12345",
            sink_node_name="alsa_output.dg60_1",
            label="DG60",
            inserts=(
                PluginInsert(
                    backend="ladspa", library=LSP_LIBRARY, label=LSP_LABEL, enabled=False
                ),
            ),
        )
        body = render_filter_chain_conf(o, o.inserts, "phonon_master")
        # Bypassed → builtin copy node, no actual plugin loaded.
        assert "label = copy" in body
        assert "(bypassed)" in body
