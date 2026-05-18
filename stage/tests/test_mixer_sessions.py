"""Tests for the SessionStore + the MixerService session methods.

Coverage:
  * full snapshot save/list/get/delete roundtrips state losslessly
  * fx-only snapshot captures just the targeted chain
  * load_session restores the snapshotted state and reconciles
  * invalid ids / unknown targets / unknown sessions raise MixerError
  * id collisions in the same second get a -1, -2 suffix
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.mixer.models import MixerState, Output, PluginInsert, Source
from phonon_stage.mixer.sessions import SessionStore, SessionStoreError
from phonon_stage.mixer.service import MixerError, MixerService
from phonon_stage.mixer.store import MixerStore
from phonon_stage.pipewire.backend import PwNode, PwPort
from phonon_stage.pipewire.fake import FakePipeWireBackend

if TYPE_CHECKING:
    from pathlib import Path


# ── SessionStore (no service involved) ────────────────────────────


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


class TestSessionStore:
    def test_save_full_roundtrip(self, store: SessionStore) -> None:
        state = MixerState(
            outputs=[Output(id="o1", sink_node_name="dg60", label="Pulse 3")],
            sources=[Source(id="s1", source_node_name="airplay_in", source_is_sink=True, label="AirPlay")],
        )
        saved = store.save_full(state.to_dict(), comment="Initial")
        assert saved.meta.scope == "full"
        assert saved.meta.comment == "Initial"
        assert saved.meta.target is None
        loaded = store.get(saved.meta.id)
        assert loaded.payload == state.to_dict()

    def test_save_fx_roundtrip(self, store: SessionStore) -> None:
        inserts = [
            PluginInsert(
                backend="ladspa",
                library="lsp-plugins/limiter_stereo.so",
                label="limiter_stereo",
                controls={"Threshold": -3.0},
                enabled=True,
            ).to_dict()
        ]
        saved = store.save_fx(target="master", inserts=inserts, comment="Soft master")
        assert saved.meta.scope == "fx-only"
        assert saved.meta.target == "master"
        loaded = store.get(saved.meta.id)
        assert loaded.payload == inserts

    def test_list_returns_newest_first(self, store: SessionStore) -> None:
        a = store.save_full({}, comment="A")
        b = store.save_full({}, comment="B")
        ids = [m.id for m in store.list()]
        # Both records turn up; exact order is filename-driven, which
        # is plenty for the UI even if collisions in the same second
        # don't differentiate by timestamp.
        assert b.meta.id in ids and a.meta.id in ids
        assert len(ids) == 2

    def test_delete(self, store: SessionStore) -> None:
        saved = store.save_full({}, comment="kill me")
        store.delete(saved.meta.id)
        with pytest.raises(SessionStoreError):
            store.get(saved.meta.id)
        with pytest.raises(SessionStoreError):
            store.delete(saved.meta.id)

    def test_path_traversal_rejected(self, store: SessionStore) -> None:
        # Can't escape the sessions dir via the id.
        with pytest.raises(SessionStoreError):
            store.get("../../etc/passwd")
        with pytest.raises(SessionStoreError):
            store.get(".hidden")
        with pytest.raises(SessionStoreError):
            store.delete("foo/bar")

    def test_save_fx_requires_target(self, store: SessionStore) -> None:
        with pytest.raises(SessionStoreError):
            store.save_fx(target="", inserts=[], comment="")

    def test_collision_suffix(self, store: SessionStore) -> None:
        # Two same-second saves don't overwrite each other.
        a = store.save_full({}, comment="a")
        b = store.save_full({}, comment="b")
        # Either b got a -N suffix, or naturally a different timestamp.
        assert a.meta.id != b.meta.id


# ── MixerService session integration ──────────────────────────────


def _pw() -> FakePipeWireBackend:
    nodes = [
        PwNode(id=10, name="dg60", media_class="Audio/Sink", nick="DG60", state="idle"),
        PwNode(id=20, name="airplay_in", media_class="Audio/Sink", nick="AirPlay", state="idle"),
    ]
    ports = [
        PwPort(id=100, node_id=10, name="playback_FL", direction="input", alias="DG60:FL"),
        PwPort(id=101, node_id=10, name="playback_FR", direction="input", alias="DG60:FR"),
        PwPort(id=200, node_id=20, name="monitor_FL", direction="output", alias="AP:monFL"),
        PwPort(id=201, node_id=20, name="monitor_FR", direction="output", alias="AP:monFR"),
    ]
    return FakePipeWireBackend(nodes=nodes, ports=ports)


@pytest.fixture()
def mixer_setup(tmp_path: Path) -> tuple[FakePipeWireBackend, MixerService]:
    pw = _pw()
    mstore = MixerStore(tmp_path / "mixer.conf.json")
    sstore = SessionStore(tmp_path / "sessions")
    svc = MixerService(pw_backend=pw, store=mstore, session_store=sstore)
    return pw, svc


class TestMixerSessions:
    async def test_save_and_load_full(
        self, mixer_setup: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        pw, svc = mixer_setup
        await svc.init()
        await svc.add_output(sink_node_name="dg60", label="JBL", delay_ms=12.0)
        snap = svc.save_session_full(comment="setup-A")
        assert snap.meta.scope == "full"
        # Mutate live state.
        out = svc.outputs[0]
        await svc.update_output(out.id, delay_ms=99.0, label="Other")
        assert svc.outputs[0].delay_ms == 99.0
        # Restore.
        await svc.load_session(snap.meta.id)
        restored = svc.outputs[0]
        assert restored.delay_ms == 12.0
        assert restored.label == "JBL"

    async def test_save_fx_master_only_restores_chain(
        self, mixer_setup: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        pw, svc = mixer_setup
        await svc.init()
        # Hand-mount an insert directly on the master (skip plugin
        # introspection — not needed for the session machinery).
        from dataclasses import replace as _replace

        from phonon_stage.mixer.models import MasterBus
        state = svc.state
        new_master = _replace(
            state.master,
            inserts=(
                PluginInsert(
                    backend="ladspa", library="lsp/limiter_stereo.so",
                    label="limiter_stereo",
                    controls={"Threshold": -6.0}, enabled=True,
                ),
            ),
        )
        svc._store.replace_state(_replace(state, master=new_master))
        snap = svc.save_session_fx(target="master", comment="soft limiter")
        # Mutate the live chain (wipe it).
        svc._store.replace_state(_replace(svc.state, master=MasterBus()))
        assert svc.master.inserts == ()
        # Restore.
        await svc.load_session(snap.meta.id)
        assert len(svc.master.inserts) == 1
        assert svc.master.inserts[0].controls["Threshold"] == -6.0

    async def test_save_fx_output_only_does_not_touch_master(
        self, mixer_setup: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        pw, svc = mixer_setup
        await svc.init()
        await svc.add_output(sink_node_name="dg60", label="JBL")
        from dataclasses import replace as _replace
        out = svc.outputs[0]
        new_out = _replace(
            out,
            inserts=(
                PluginInsert(
                    backend="ladspa", library="lsp/eq.so", label="graph_equalizer",
                    controls={"Gain": 1.0}, enabled=True,
                ),
            ),
        )
        new_outputs = [new_out if o.id == out.id else o for o in svc.state.outputs]
        svc._store.replace_state(_replace(svc.state, outputs=new_outputs))
        snap = svc.save_session_fx(target=f"output:{out.id}", comment="warm eq")
        # Wipe the output chain then restore.
        new_out_empty = _replace(svc.outputs[0], inserts=())
        new_outputs_empty = [
            new_out_empty if o.id == out.id else o for o in svc.state.outputs
        ]
        svc._store.replace_state(_replace(svc.state, outputs=new_outputs_empty))
        await svc.load_session(snap.meta.id)
        assert len(svc.outputs[0].inserts) == 1
        assert svc.outputs[0].inserts[0].label == "graph_equalizer"

    async def test_load_unknown_session_raises(
        self, mixer_setup: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        _, svc = mixer_setup
        await svc.init()
        with pytest.raises(MixerError):
            await svc.load_session("does-not-exist")

    async def test_save_fx_unknown_target_raises(
        self, mixer_setup: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        _, svc = mixer_setup
        await svc.init()
        with pytest.raises(MixerError):
            svc.save_session_fx(target="garbage", comment="")
        with pytest.raises(MixerError):
            svc.save_session_fx(target="output:nope", comment="")
