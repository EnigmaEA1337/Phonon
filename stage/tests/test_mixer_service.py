"""Tests for the mixer console service.

Validates the compile-to-PipeWire pipeline: master null-sink lifecycle,
per-output loopback with delay, master/direct routing, mute semantics,
validation, and persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.mixer.models import Bus, BusSend, MasterBus, MixerState, Output, Source, Vca
from phonon_stage.mixer.service import (
    MASTER_SINK_NAME,
    MixerError,
    MixerService,
)
from phonon_stage.mixer.store import MixerStore
from phonon_stage.pipewire.backend import PwNode, PwPort
from phonon_stage.pipewire.fake import FakePipeWireBackend

if TYPE_CHECKING:
    from pathlib import Path


# ── Fixtures ──────────────────────────────────────────────────────────


def _sample_pw() -> FakePipeWireBackend:
    """A PW graph with two DG60 outputs + airplay_in null-sink + a BT
    null-sink, each with their FL/FR ports. Mirrors the production
    topology the mixer is designed for."""
    nodes = [
        PwNode(
            id=10,
            name="alsa_output.dg60_1",
            media_class="Audio/Sink",
            nick="DG60 #1",
            state="idle",
        ),
        PwNode(
            id=20,
            name="alsa_output.dg60_2",
            media_class="Audio/Sink",
            nick="DG60 #2",
            state="idle",
        ),
        PwNode(
            id=30,
            name="airplay_in",
            media_class="Audio/Sink",
            nick="AirPlay-In",
            state="idle",
        ),
        PwNode(
            id=40,
            name="bt_phone_in",
            media_class="Audio/Sink",
            nick="bt_phone-BT-In",
            state="idle",
        ),
    ]
    ports = [
        # DG60 #1 — playback inputs
        PwPort(id=100, node_id=10, name="playback_FL", direction="input", alias="DG60#1:FL"),
        PwPort(id=101, node_id=10, name="playback_FR", direction="input", alias="DG60#1:FR"),
        # DG60 #2 — playback inputs
        PwPort(id=200, node_id=20, name="playback_FL", direction="input", alias="DG60#2:FL"),
        PwPort(id=201, node_id=20, name="playback_FR", direction="input", alias="DG60#2:FR"),
        # airplay_in — monitor outputs (the readable side) + playback inputs
        PwPort(id=300, node_id=30, name="monitor_FL", direction="output", alias="AP:monFL"),
        PwPort(id=301, node_id=30, name="monitor_FR", direction="output", alias="AP:monFR"),
        PwPort(id=302, node_id=30, name="playback_FL", direction="input", alias="AP:pbFL"),
        PwPort(id=303, node_id=30, name="playback_FR", direction="input", alias="AP:pbFR"),
        # bt_phone_in — monitor outputs + playback inputs
        PwPort(id=400, node_id=40, name="monitor_FL", direction="output", alias="BT:monFL"),
        PwPort(id=401, node_id=40, name="monitor_FR", direction="output", alias="BT:monFR"),
        PwPort(id=402, node_id=40, name="playback_FL", direction="input", alias="BT:pbFL"),
        PwPort(id=403, node_id=40, name="playback_FR", direction="input", alias="BT:pbFR"),
    ]
    return FakePipeWireBackend(nodes=nodes, ports=ports)


@pytest.fixture()
def fake_pw() -> FakePipeWireBackend:
    return _sample_pw()


@pytest.fixture()
def store(tmp_path: Path) -> MixerStore:
    return MixerStore(tmp_path / "mixer.conf.json")


@pytest.fixture()
async def service(fake_pw: FakePipeWireBackend, store: MixerStore) -> MixerService:
    svc = MixerService(pw_backend=fake_pw, store=store)
    await svc.init()
    return svc


# ── Init / master null-sink lifecycle ─────────────────────────────────


class TestInit:
    async def test_init_creates_master_null_sink(
        self, fake_pw: FakePipeWireBackend, store: MixerStore
    ) -> None:
        assert MASTER_SINK_NAME not in {n.name for n in fake_pw.nodes}
        svc = MixerService(pw_backend=fake_pw, store=store)
        await svc.init()
        assert MASTER_SINK_NAME in {n.name for n in fake_pw.nodes}

    async def test_init_is_idempotent(
        self, fake_pw: FakePipeWireBackend, store: MixerStore
    ) -> None:
        """Calling init twice (or across daemon restarts) must not
        stack duplicate master null-sinks."""
        svc = MixerService(pw_backend=fake_pw, store=store)
        await svc.init()
        await svc.init()
        masters = [n for n in fake_pw.nodes if n.name == MASTER_SINK_NAME]
        assert len(masters) == 1

    async def test_init_unloads_orphan_loopbacks_from_prior_session(
        self, fake_pw: FakePipeWireBackend, store: MixerStore
    ) -> None:
        """Regression: pactl modules survive across phonon-stage
        restarts as long as the user pipewire session is up. The
        previous daemon's loopbacks are stranded — init must clear
        them before reconcile, otherwise the new ones stack on top
        and every output ends up receiving the audio twice (one of
        the worst real-world bugs we shipped on the 3070)."""
        # Stage a previous session: master null-sink + two loopbacks
        # pointing at it. The fake backend's `unload_module` knows
        # about null_sinks, but for plain loopbacks we just stash
        # entries in `loopbacks` so the cleanup pass can find them.
        await fake_pw.load_null_sink(MASTER_SINK_NAME, "Phonon-Master")
        # Pre-existing orphan loopbacks (simulating leftover state).
        await fake_pw.load_loopback(f"{MASTER_SINK_NAME}.monitor", "alsa_output.dg60_1", 90)
        await fake_pw.load_loopback(f"{MASTER_SINK_NAME}.monitor", "alsa_output.dg60_2", 0)
        assert len(fake_pw.loopbacks) == 2

        # And one Output persisted to disk so reconcile will try to
        # create its own loopback after cleanup.
        store.replace_state(
            MixerState(
                master=MasterBus(),
                outputs=[
                    Output(
                        id="o1",
                        sink_node_name="alsa_output.dg60_1",
                        label="DG60 #1",
                        delay_ms=115.0,
                    ),
                ],
                sources=[],
            )
        )

        svc = MixerService(pw_backend=fake_pw, store=store)
        await svc.init()

        # The fake backend's `_cleanup_orphan_loopbacks` is a no-op
        # (it depends on pactl CLI which the fake doesn't simulate),
        # so we verify a slightly different invariant: after init,
        # the live loopbacks must NOT exceed the count implied by
        # the model (1 receives_master output → 1 loopback).
        # The orphan-cleanup path is exercised via the real backend
        # in production; here we assert the in-memory accounting is
        # right end-to-end.
        active = list(fake_pw.loopbacks.values())
        # Filter to only loopbacks coming from our master.
        ours = [t for t in active if t[0] == f"{MASTER_SINK_NAME}.monitor"]
        # 1 output in the model means we expect exactly 1 loopback
        # for our master, regardless of how many orphans were there
        # before init.
        assert len(ours) == 1, f"Expected 1 master loopback, got {len(ours)}: {ours}"

    async def test_init_restores_state_from_disk(
        self, fake_pw: FakePipeWireBackend, store: MixerStore
    ) -> None:
        """A persisted state on disk must be applied to PW on init."""
        # Pre-populate the store
        store.replace_state(
            MixerState(
                master=MasterBus(gain_db=-3.0, mute=False),
                outputs=[
                    Output(
                        id="o1",
                        sink_node_name="alsa_output.dg60_1",
                        label="Pulse 3",
                        delay_ms=115.0,
                    ),
                ],
                sources=[],
            )
        )
        svc = MixerService(pw_backend=fake_pw, store=store)
        await svc.init()
        assert len(svc.outputs) == 1
        # The output with receives_master=True (default) should have
        # spawned a loopback during init.
        assert len(fake_pw.loopbacks) == 1


# ── Output mutations ──────────────────────────────────────────────────


class TestOutputs:
    async def test_add_output_creates_master_loopback(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """A receives_master output spawns a loopback from
        phonon_master.monitor → its sink with its delay."""
        o = await service.add_output(
            sink_node_name="alsa_output.dg60_1",
            label="Pulse 3",
            delay_ms=115.0,
        )
        assert o.delay_ms == 115.0
        assert len(fake_pw.loopbacks) == 1
        (src, sink, latency) = next(iter(fake_pw.loopbacks.values()))
        assert src == f"{MASTER_SINK_NAME}.monitor"
        assert sink == "alsa_output.dg60_1"
        assert latency == 115

    async def test_add_output_with_receives_master_false_no_loopback(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """An output that doesn't receive the master mustn't get a
        loopback — useful for an FOH/monitor split where one device
        is fed only by direct sends."""
        await service.add_output(
            sink_node_name="alsa_output.dg60_1",
            label="DG60 #1",
            receives_master=False,
        )
        assert len(fake_pw.loopbacks) == 0

    async def test_update_output_delay_recreates_loopback_with_new_latency(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        o = await service.add_output(sink_node_name="alsa_output.dg60_1", label="A", delay_ms=50.0)
        old_module = next(iter(fake_pw.loopbacks.keys()))
        assert fake_pw.loopbacks[old_module][2] == 50

        await service.update_output(o.id, delay_ms=200.0)
        # Old module unloaded, new one with new latency
        assert old_module in fake_pw.unloaded_modules
        assert len(fake_pw.loopbacks) == 1
        new_module = next(iter(fake_pw.loopbacks.keys()))
        assert fake_pw.loopbacks[new_module][2] == 200

    async def test_mute_output_destroys_its_loopback(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        o = await service.add_output(sink_node_name="alsa_output.dg60_1", label="A", delay_ms=10.0)
        assert len(fake_pw.loopbacks) == 1
        await service.update_output(o.id, mute=True)
        assert len(fake_pw.loopbacks) == 0
        # Un-mute brings it back.
        await service.update_output(o.id, mute=False)
        assert len(fake_pw.loopbacks) == 1

    async def test_remove_output_cleans_up_loopback_and_drops_direct_refs(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """When an output is removed, every source that direct-routed to
        it must have that id pruned — otherwise the next reconcile
        would log "unknown output" forever."""
        o1 = await service.add_output(sink_node_name="alsa_output.dg60_1", label="A")
        o2 = await service.add_output(sink_node_name="alsa_output.dg60_2", label="B")
        s = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AP",
            direct_outputs=[o2.id],
            to_master=False,
        )
        assert s.direct_outputs == (o2.id,)
        await service.remove_output(o2.id)
        updated = next(x for x in service.sources if x.id == s.id)
        assert updated.direct_outputs == ()
        assert len(service.outputs) == 1
        assert service.outputs[0].id == o1.id

    async def test_invalid_delay_rejected(self, service: MixerService) -> None:
        with pytest.raises(ValueError, match="out of range"):
            await service.add_output(sink_node_name="x", label="x", delay_ms=10_000.0)

    async def test_invalid_gain_rejected(self, service: MixerService) -> None:
        with pytest.raises(ValueError, match="out of range"):
            await service.add_output(sink_node_name="x", label="x", gain_db=99.0)


# ── Source mutations + routing ────────────────────────────────────────


class TestSources:
    async def test_source_to_master_creates_links(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """A source with to_master=True must produce one link per
        channel pair from its monitor to phonon_master.playback."""
        await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AirPlay",
        )
        # 2 channels = 2 links
        assert len(fake_pw.links) == 2
        master_node = next(n for n in fake_pw.nodes if n.name == MASTER_SINK_NAME)
        master_inputs = {
            p.id for p in fake_pw.ports if p.node_id == master_node.id and p.direction == "input"
        }
        # Each link's input port is on the master sink
        for lk in fake_pw.links:
            assert lk.input_port_id in master_inputs

    async def test_source_direct_output_creates_links_bypassing_master(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """A source with to_master=False + direct_outputs=[o1] sends
        only direct, no master link."""
        o = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60")
        # Drop the master loopback links so we can count fresh.
        loopback_count = len(fake_pw.loopbacks)
        await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AirPlay",
            to_master=False,
            direct_outputs=[o.id],
        )
        # 2 channels of direct routing
        assert len(fake_pw.links) == 2
        # Loopback unchanged (master path still goes to that output —
        # we only suppressed the source's master send).
        assert len(fake_pw.loopbacks) == loopback_count

    async def test_source_to_master_and_direct_cumulative(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Cumulative routing: a source going via master AND direct to
        one output produces TWO sets of links (master send + direct
        send). User-requested behaviour for layering."""
        o = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60")
        await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AP",
            to_master=True,
            direct_outputs=[o.id],
        )
        # 2 links to master + 2 direct = 4 total
        assert len(fake_pw.links) == 4

    async def test_source_mute_skips_link_creation(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        s = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        assert len(fake_pw.links) == 2
        await service.update_source(s.id, mute=True)
        assert len(fake_pw.links) == 0
        await service.update_source(s.id, mute=False)
        assert len(fake_pw.links) == 2

    async def test_master_mute_kills_all_master_paths(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Master.mute=True must drop every receives_master loopback
        AND every source-to-master link. Direct routes survive — the
        master is a bus, not a hard kill."""
        o = await service.add_output(sink_node_name="alsa_output.dg60_1", label="DG60")
        s = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AP",
            to_master=True,
            direct_outputs=[o.id],
        )
        assert len(fake_pw.links) == 4  # master + direct, both x2 channels
        assert len(fake_pw.loopbacks) == 1

        await service.update_master(mute=True)
        # Master-side path gone: no loopback, no source→master link.
        # Direct path survives: 2 links remain.
        assert len(fake_pw.loopbacks) == 0
        assert len(fake_pw.links) == 2
        # Re-enable: full topology restored.
        await service.update_master(mute=False)
        assert len(fake_pw.loopbacks) == 1
        assert len(fake_pw.links) == 4
        assert s.id  # unused-var guard

    async def test_unknown_direct_output_rejected_on_add(self, service: MixerService) -> None:
        with pytest.raises(MixerError, match="unknown id"):
            await service.add_source(
                source_node_name="airplay_in",
                source_is_sink=True,
                label="AP",
                direct_outputs=["does-not-exist"],
            )

    async def test_unknown_direct_output_rejected_on_update(self, service: MixerService) -> None:
        s = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        with pytest.raises(MixerError, match="unknown id"):
            await service.update_source(s.id, direct_outputs=["nope"])


# ── End-to-end realistic scenario ─────────────────────────────────────


class TestPerChannelMutes:
    """L/R mutes apply as zero on the corresponding channel of the
    node's per-channel volume. The global mute remains independent
    (still tears down link topology). These tests pin the channel
    semantics so a future refactor doesn't silently invert L/R or
    forget the gain factor."""

    async def test_source_mute_left_zeroes_only_left_channel(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        s = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AP",
            gain_db=0.0,
        )
        await service.update_source(s.id, mute_left=True)
        ap_node = next(n for n in fake_pw.nodes if n.name == "airplay_in")
        vols = fake_pw.channel_volumes[ap_node.name]
        # Left channel silenced, right at unity (gain_db=0 → linear 1.0).
        assert vols[0] == 0.0
        assert abs(vols[1] - 1.0) < 1e-6

    async def test_master_mute_right_zeroes_only_right_channel(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        await service.update_master(gain_db=-6.0, mute_right=True)
        master_node = next(n for n in fake_pw.nodes if n.name == MASTER_SINK_NAME)
        vols = fake_pw.channel_volumes[master_node.name]
        # Left at -6 dB linear ≈ 0.501, right at zero.
        assert abs(vols[0] - 0.501) < 0.01
        assert vols[1] == 0.0

    async def test_output_mute_both_channels_via_lr(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Toggling BOTH L and R on the same strip is equivalent to a
        full mute on the channel-volume side — but the master loopback
        is still created (mute_l/r doesn't affect topology, only
        volume). That matches DAW intuition: channel mutes are an
        attenuation layer, not a routing layer."""
        o = await service.add_output(
            sink_node_name="alsa_output.dg60_1",
            label="DG60",
            gain_db=0.0,
        )
        await service.update_output(o.id, mute_left=True, mute_right=True)
        sink_node = next(n for n in fake_pw.nodes if n.name == "alsa_output.dg60_1")
        assert fake_pw.channel_volumes[sink_node.name] == [0.0, 0.0]
        # Loopback is still created because mute (global) is False.
        assert len(fake_pw.loopbacks) == 1


class TestSolo:
    """Solo gates link creation. When any source/output has solo=True
    (and isn't muted), every non-solo'd entity on that side gets its
    routing suppressed. Muting a solo'd entity cancels its solo
    intent (matches operator expectation: mute always wins)."""

    async def test_source_solo_suppresses_other_sources_to_master(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        s_a = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        s_b = await service.add_source(
            source_node_name="bt_phone_in", source_is_sink=True, label="BT"
        )
        # Before solo: both sources → master, so 2x2 = 4 links.
        assert len(fake_pw.links) == 4

        await service.update_source(s_a.id, solo=True)
        # Only A still routes. B's links are gone.
        assert len(fake_pw.links) == 2
        assert s_b.id  # quiet pylint

    async def test_solo_then_mute_solo_cancels_silencing(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """A solo'd-but-muted source must NOT trigger solo behavior —
        otherwise muting the solo'd strip would silence everyone, which
        is a footgun. Mute wins, solo is treated as inactive."""
        s_a = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        await service.add_source(source_node_name="bt_phone_in", source_is_sink=True, label="BT")
        # Solo A, then mute A → both must continue routing (solo gone).
        await service.update_source(s_a.id, solo=True)
        assert len(fake_pw.links) == 2  # only A
        await service.update_source(s_a.id, mute=True)
        # A is muted (its links gone), but B is back (no active solo).
        assert len(fake_pw.links) == 2  # only B now

    async def test_output_solo_suppresses_others(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        o_a = await service.add_output(sink_node_name="alsa_output.dg60_1", label="A")
        o_b = await service.add_output(sink_node_name="alsa_output.dg60_2", label="B")
        # Both receive master → 2 loopbacks.
        assert len(fake_pw.loopbacks) == 2

        await service.update_output(o_a.id, solo=True)
        # Only A's loopback survives.
        assert len(fake_pw.loopbacks) == 1
        assert o_b.id  # quiet

    async def test_solo_group_multiple_solos_all_play(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Solo'ing two outputs means both stay live — solo is a group,
        not a singleton. Useful when auditioning a stereo pair while
        muting other zones."""
        o_a = await service.add_output(sink_node_name="alsa_output.dg60_1", label="A")
        o_b = await service.add_output(sink_node_name="alsa_output.dg60_2", label="B")
        await service.update_output(o_a.id, solo=True)
        await service.update_output(o_b.id, solo=True)
        # Both solos active → both loopbacks survive.
        assert len(fake_pw.loopbacks) == 2


class TestReconcileFastPath:
    """The naive reconcile tears down every link + loopback on every
    state change, which audibly cuts the audio on EVERY strip. These
    tests pin the fast paths that avoid the full rebuild when only
    volume-equivalent or single-output-delay fields move.

    Witnessed live on the 3070: bumping one output's delay silenced
    every other output for ~200 ms — these regressions are exactly
    what these tests prevent from coming back."""

    async def test_gain_change_does_not_destroy_links(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        await service.add_output(sink_node_name="alsa_output.dg60_1", label="A", delay_ms=50.0)
        await service.add_source(source_node_name="airplay_in", source_is_sink=True, label="AP")
        loopbacks_before = set(fake_pw.loopbacks.keys())
        links_before = {lk.id for lk in fake_pw.links}

        await service.update_master(gain_db=-6.0)

        # Fast path: same loopbacks, same links — only volume changed.
        assert set(fake_pw.loopbacks.keys()) == loopbacks_before
        assert {lk.id for lk in fake_pw.links} == links_before

    async def test_mute_left_does_not_destroy_links(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        o = await service.add_output(sink_node_name="alsa_output.dg60_1", label="A")
        s = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        loopbacks_before = set(fake_pw.loopbacks.keys())
        links_before = {lk.id for lk in fake_pw.links}

        await service.update_source(s.id, mute_left=True)
        await service.update_output(o.id, mute_right=True)

        # Topology untouched.
        assert set(fake_pw.loopbacks.keys()) == loopbacks_before
        assert {lk.id for lk in fake_pw.links} == links_before

    async def test_delay_change_only_rebuilds_that_output_loopback(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Delay-only change on output A must NOT tear down output B's
        loopback. That was the bug: a delay tweak silenced every
        output during the rebuild gap. With the fast path, B keeps
        its loopback intact."""
        o_a = await service.add_output(
            sink_node_name="alsa_output.dg60_1", label="A", delay_ms=50.0
        )
        o_b = await service.add_output(
            sink_node_name="alsa_output.dg60_2", label="B", delay_ms=0.0
        )
        b_loopback_id = next(
            mid for mid, (_, sink, _) in fake_pw.loopbacks.items() if sink == "alsa_output.dg60_2"
        )
        a_loopback_id = next(
            mid for mid, (_, sink, _) in fake_pw.loopbacks.items() if sink == "alsa_output.dg60_1"
        )

        await service.update_output(o_a.id, delay_ms=200.0)

        # B's loopback survived (no audio interruption on that output).
        assert b_loopback_id in fake_pw.loopbacks
        # A's loopback was swapped — old id gone, new one in with new latency.
        assert a_loopback_id not in fake_pw.loopbacks
        a_loopback_now = next(
            t for t in fake_pw.loopbacks.values() if t[1] == "alsa_output.dg60_1"
        )
        assert a_loopback_now[2] == 200
        assert o_b.id  # quiet

    async def test_label_only_change_is_pure_persistence(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Renaming a strip (the scotch label) must not touch PW at
        all — pure metadata change."""
        s = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        loopbacks_before = set(fake_pw.loopbacks.keys())
        links_before = {lk.id for lk in fake_pw.links}
        vols_before = dict(fake_pw.channel_volumes)

        await service.update_source(s.id, label="iPhone Salon")

        assert set(fake_pw.loopbacks.keys()) == loopbacks_before
        assert {lk.id for lk in fake_pw.links} == links_before
        # Volumes also untouched — pure label PATCH.
        assert fake_pw.channel_volumes == vols_before


class TestProductionScenario:
    async def test_two_dg60_outputs_two_sources_via_master(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """The user's exact case: AirPlay + BT both via master, each
        DG60 has its own codec-compensation delay. The mixer should
        produce one loopback per output (with its own delay) and two
        master-feeding links per source. Phasing configured ONCE
        per output, not per (source x output) pair."""
        dg60_1 = await service.add_output(
            sink_node_name="alsa_output.dg60_1",
            label="Pulse 3",
            delay_ms=115.0,
        )
        dg60_2 = await service.add_output(
            sink_node_name="alsa_output.dg60_2",
            label="Xtreme 4",
            delay_ms=0.0,
        )
        await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AirPlay"
        )
        await service.add_source(
            source_node_name="bt_phone_in", source_is_sink=True, label="BT Phone"
        )

        # 2 outputs x 1 loopback each (with respective delays)
        assert len(fake_pw.loopbacks) == 2
        delays = {lat for (_src, _sink, lat) in fake_pw.loopbacks.values()}
        assert delays == {115, 0}

        # 2 sources x 2 channels = 4 links to master
        assert len(fake_pw.links) == 4

        # If we change DG60 #2's delay later, the rebuild affects only
        # ITS loopback. We don't have to touch any source.
        await service.update_output(dg60_2.id, delay_ms=80.0)
        delays_after = {lat for (_src, _sink, lat) in fake_pw.loopbacks.values()}
        assert delays_after == {115, 80}
        assert dg60_1.id  # quiet pylint


# ── VCAs (control-plane groupings) ──────────────────────────────────


class TestVca:
    async def test_add_vca_starts_empty(self, service: MixerService) -> None:
        v = await service.add_vca(label="Backline", gain_db=-2.0)
        assert v.label == "Backline"
        assert v.gain_db == -2.0
        assert v.mute is False
        assert v.members == ()
        assert len(service.vcas) == 1

    async def test_assign_unassign_strip(self, service: MixerService) -> None:
        src = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AirPlay"
        )
        v = await service.add_vca(label="Backline")
        v2 = await service.assign_vca_member(v.id, src.id)
        assert v2.members == (src.id,)
        # Idempotent — re-assigning the same id leaves the membership unchanged.
        v3 = await service.assign_vca_member(v.id, src.id)
        assert v3.members == (src.id,)
        v4 = await service.unassign_vca_member(v.id, src.id)
        assert v4.members == ()

    async def test_assign_unknown_strip_rejected(self, service: MixerService) -> None:
        v = await service.add_vca(label="Voices")
        with pytest.raises(MixerError, match="unknown strip id"):
            await service.assign_vca_member(v.id, "nope")

    async def test_effective_gain_folds_vca(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        # source.gain_db=-6, vca.gain_db=-3 → effective=-9 dB linear=~0.355
        src = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AirPlay",
            gain_db=-6.0,
        )
        v = await service.add_vca(label="Group", gain_db=-3.0)
        await service.assign_vca_member(v.id, src.id)
        # Read whatever value was last pushed to the fake PW for the source.
        ch = fake_pw.channel_volumes.get("airplay_in")
        assert ch is not None
        expected_lin = 10 ** (-9.0 / 20.0)
        assert ch[0] == pytest.approx(expected_lin, rel=1e-3)
        assert ch[1] == pytest.approx(expected_lin, rel=1e-3)

    async def test_vca_mute_silences_member(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        src = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AirPlay"
        )
        v = await service.add_vca(label="Group")
        await service.assign_vca_member(v.id, src.id)
        await service.patch_vca(v.id, mute=True)
        ch = fake_pw.channel_volumes.get("airplay_in")
        assert list(ch or []) == [0.0, 0.0]

    async def test_remove_vca_restores_member_volume(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        src = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AirPlay",
            gain_db=-6.0,
        )
        v = await service.add_vca(label="Group", gain_db=-3.0)
        await service.assign_vca_member(v.id, src.id)
        # Under the VCA — gain = -9 dB
        ch_with = fake_pw.channel_volumes.get("airplay_in")
        assert ch_with is not None
        assert ch_with[0] == pytest.approx(10 ** (-9.0 / 20.0), rel=1e-3)
        # Drop the VCA — the source returns to its own gain (-6 dB).
        await service.remove_vca(v.id)
        ch_after = fake_pw.channel_volumes.get("airplay_in")
        assert ch_after is not None
        assert ch_after[0] == pytest.approx(10 ** (-6.0 / 20.0), rel=1e-3)

    async def test_removing_source_prunes_vca_members(self, service: MixerService) -> None:
        src = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AirPlay"
        )
        v = await service.add_vca(label="Group")
        await service.assign_vca_member(v.id, src.id)
        await service.remove_source(src.id)
        v_after = service.vcas[0]
        assert v_after.members == ()

    async def test_vca_persists_across_reload(
        self, service: MixerService, store: MixerStore
    ) -> None:
        src = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AirPlay"
        )
        v = await service.add_vca(label="Backline", gain_db=-2.0)
        await service.assign_vca_member(v.id, src.id)
        # Build a fresh service from the persisted JSON — VCA + member
        # membership must survive a daemon restart.
        store2 = MixerStore(store._path)
        svc2 = MixerService(pw_backend=service._pw, store=store2)
        await svc2.init()
        assert len(svc2.vcas) == 1
        assert svc2.vcas[0].label == "Backline"
        assert svc2.vcas[0].gain_db == -2.0
        assert svc2.vcas[0].members == (src.id,)

    async def test_vca_cap_enforced(self, service: MixerService) -> None:
        from phonon_stage.mixer.models import MAX_VCAS

        for i in range(MAX_VCAS):
            await service.add_vca(label=f"V{i}")
        with pytest.raises(MixerError, match="limit reached"):
            await service.add_vca(label="overflow")


# ── Bus (sub-mix) reconcile ───────────────────────────────────────────


class TestBuses:
    """Bus-side reconcile: null-sink lifecycle, bus→master loopback,
    source→bus sends, mute / solo / cap. The Fake backend synthesizes
    a node + ports per null-sink, so source→bus links resolve through
    the same code path as source→output direct sends."""

    async def test_add_bus_creates_null_sink_and_master_loopback(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Adding a Bus stamps a `phonon_bus_<id>` null-sink and a
        loopback from that sink's monitor into phonon_master, so audio
        eventually summed into the bus reaches the master."""
        b = await service.add_bus(label="Drums", gain_db=-3.0)
        names = {name for _, (name, _) in fake_pw.null_sinks.items()}
        assert f"phonon_bus_{b.id}" in names
        # Exactly one bus→master loopback for this bus.
        sources = [src for (src, sink, _) in fake_pw.loopbacks.values()]
        assert f"phonon_bus_{b.id}.monitor" in sources
        # And no spurious phonon_master_post (master has no inserts).
        assert "phonon_master_post" not in names

    async def test_bus_cap_enforced(self, service: MixerService) -> None:
        from phonon_stage.mixer.models import MAX_BUSES

        for i in range(MAX_BUSES):
            await service.add_bus(label=f"B{i}")
        with pytest.raises(MixerError, match="limit reached"):
            await service.add_bus(label="overflow")

    async def test_bus_gain_volume_fast_path(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Patching only gain_db / mute_left / mute_right takes the
        fast volume path — channel volumes shift on the bus sink but
        the loopback isn't torn down + rebuilt."""
        b = await service.add_bus(label="Drums", gain_db=0.0)
        old_loopbacks = dict(fake_pw.loopbacks)
        await service.update_bus(b.id, gain_db=-6.0)
        assert fake_pw.loopbacks == old_loopbacks  # untouched
        sink_name = f"phonon_bus_{b.id}"
        chans = fake_pw.channel_volumes[sink_name]
        # -6 dB → ~0.501 linear, allow a wide tolerance.
        assert 0.4 < chans[0] < 0.6
        assert 0.4 < chans[1] < 0.6

    async def test_bus_mute_silences_loopback(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """When a bus is muted its bus→master loopback is dropped, so
        no audio leaves the bus sink. Un-muting brings it back."""
        b = await service.add_bus(label="Drums")
        assert any(
            src == f"phonon_bus_{b.id}.monitor" for (src, _, _) in fake_pw.loopbacks.values()
        )
        await service.update_bus(b.id, mute=True)
        assert not any(
            src == f"phonon_bus_{b.id}.monitor" for (src, _, _) in fake_pw.loopbacks.values()
        )
        await service.update_bus(b.id, mute=False)
        assert any(
            src == f"phonon_bus_{b.id}.monitor" for (src, _, _) in fake_pw.loopbacks.values()
        )

    async def test_bus_solo_silences_other_buses(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """When one bus is soloed, every other (non-soloed) bus's
        loopback to master is suppressed — same invariant as
        output-side solo."""
        b1 = await service.add_bus(label="Drums")
        b2 = await service.add_bus(label="FX")
        await service.update_bus(b1.id, solo=True)
        live = {src for (src, _, _) in fake_pw.loopbacks.values()}
        assert f"phonon_bus_{b1.id}.monitor" in live
        assert f"phonon_bus_{b2.id}.monitor" not in live
        # Clearing solo brings b2 back.
        await service.update_bus(b1.id, solo=False)
        live = {src for (src, _, _) in fake_pw.loopbacks.values()}
        assert f"phonon_bus_{b2.id}.monitor" in live

    async def test_set_source_bus_send_creates_link(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """A source whose enabled BusSend points at a known bus must
        end up linked source.monitor → bus_sink.playback on reconcile."""
        b = await service.add_bus(label="Drums")
        s = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AP",
            to_master=False,
        )
        before_links = len(fake_pw.links)
        await service.set_source_bus_send(s.id, b.id, gain_db=0.0, enabled=True)
        # Two new links (FL + FR) into the bus sink.
        assert len(fake_pw.links) >= before_links + 2

    async def test_set_source_bus_send_disabled_creates_no_link(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """An `enabled=False` send keeps the model entry (the UI shows
        a greyed-out send fader) but produces no PW links."""
        b = await service.add_bus(label="Drums")
        s = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AP",
            to_master=False,
        )
        await service.set_source_bus_send(s.id, b.id, enabled=False)
        # No new source→bus links should appear.
        bus_sink_id = next(n.id for n in fake_pw.nodes if n.name == f"phonon_bus_{b.id}")
        new_links = [
            lk
            for lk in fake_pw.links
            if lk.dst_port_id in {p.id for p in fake_pw.ports if p.node_id == bus_sink_id}
        ]
        assert new_links == []
        # And the model entry is persisted.
        updated = next(x for x in service.sources if x.id == s.id)
        assert any(snd.bus_id == b.id and not snd.enabled for snd in updated.bus_sends)

    async def test_set_source_bus_send_unknown_bus_rejected(self, service: MixerService) -> None:
        s = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        with pytest.raises(MixerError, match="unknown bus id"):
            await service.set_source_bus_send(s.id, "nope", enabled=True)

    async def test_remove_source_bus_send_idempotent(self, service: MixerService) -> None:
        s = await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
        # Calling remove on a non-existing send is a silent no-op.
        result = await service.remove_source_bus_send(s.id, "bus-never-existed")
        assert result.id == s.id

    async def test_remove_bus_tears_down_and_prunes_sends(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """Deleting a bus unloads its null-sink, drops the bus→master
        loopback, and prunes every source.bus_sends that referenced
        it — no phantom entries left in state."""
        b = await service.add_bus(label="Drums")
        s = await service.add_source(
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AP",
            to_master=False,
        )
        await service.set_source_bus_send(s.id, b.id, enabled=True)
        await service.remove_bus(b.id)
        # Bus removed from state.
        assert all(x.id != b.id for x in service.buses)
        # Null-sink unloaded from PW.
        names = {name for _, (name, _) in fake_pw.null_sinks.items()}
        assert f"phonon_bus_{b.id}" not in names
        # Source's bus_sends pruned.
        updated = next(x for x in service.sources if x.id == s.id)
        assert all(snd.bus_id != b.id for snd in updated.bus_sends)

    async def test_bus_persists_across_init(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        """A bus written to disk in one daemon lifetime must reload
        and re-create its null-sink + master loopback on the next."""
        b = await service.add_bus(label="Drums", gain_db=-2.0)
        # Spin up a fresh service against the same backend + store.
        store2 = MixerStore(service._store._path)
        svc2 = MixerService(pw_backend=fake_pw, store=store2)
        await svc2.init()
        assert len(svc2.buses) == 1
        assert svc2.buses[0].label == "Drums"
        assert svc2.buses[0].gain_db == -2.0
        # And the null-sink is still loaded.
        names = {name for _, (name, _) in fake_pw.null_sinks.items()}
        assert f"phonon_bus_{b.id}" in names


# ── Bus / BusSend dataclass round-trips ───────────────────────────────


class TestBusModels:
    """Pure-model tests for the bus data structures. The service layer
    consumes these in commit C2; here we lock down the serialization
    contract so persistence stays stable across releases."""

    def test_bus_send_roundtrip(self) -> None:
        s = BusSend(bus_id="bus-drums", gain_db=-3.5, enabled=True)
        assert BusSend.from_dict(s.to_dict()) == s

    def test_bus_send_defaults(self) -> None:
        s = BusSend(bus_id="bus-x")
        assert s.gain_db == 0.0
        assert s.enabled is True

    def test_bus_send_disabled_roundtrip(self) -> None:
        s = BusSend(bus_id="bus-x", gain_db=-6.0, enabled=False)
        assert BusSend.from_dict(s.to_dict()) == s

    def test_bus_roundtrip_empty_chain(self) -> None:
        b = Bus(id="bus-drums", label="Drums")
        assert Bus.from_dict(b.to_dict()) == b

    def test_bus_roundtrip_full(self) -> None:
        from phonon_stage.mixer.models import PluginInsert

        b = Bus(
            id="bus-fx",
            label="FX Send",
            gain_db=-2.0,
            mute=True,
            mute_left=False,
            mute_right=True,
            solo=False,
            inserts=(
                PluginInsert(
                    backend="ladspa",
                    library="lsp-plugins-ladspa",
                    label="http://lsp-plug.in/plugins/ladspa/comp",
                    controls={"Threshold (dB)": -12.0},
                    enabled=True,
                ),
            ),
        )
        assert Bus.from_dict(b.to_dict()) == b

    def test_bus_insert_back_compat_property(self) -> None:
        """Mirrors Output.insert / MasterBus.insert — first slot or None."""
        from phonon_stage.mixer.models import PluginInsert

        empty = Bus(id="b", label="L")
        assert empty.insert is None
        ins = PluginInsert(backend="ladspa", library="x", label="y")
        with_chain = Bus(id="b", label="L", inserts=(ins,))
        assert with_chain.insert == ins

    def test_source_bus_sends_roundtrip(self) -> None:
        src = Source(
            id="src-airplay",
            source_node_name="airplay_in",
            source_is_sink=True,
            label="AirPlay-In",
            bus_sends=(
                BusSend(bus_id="bus-a", gain_db=0.0),
                BusSend(bus_id="bus-b", gain_db=-6.0, enabled=False),
            ),
        )
        assert Source.from_dict(src.to_dict()) == src

    def test_source_legacy_json_has_no_bus_sends(self) -> None:
        """A persisted Source from before this commit lacks the
        bus_sends key; from_dict must accept that and default it to
        the empty tuple."""
        legacy = {
            "id": "src-legacy",
            "source_node_name": "airplay_in",
            "source_is_sink": True,
            "label": "Legacy",
            "gain_db": 0.0,
            "mute": False,
            "mute_left": False,
            "mute_right": False,
            "solo": False,
            "to_master": True,
            "direct_outputs": [],
        }
        src = Source.from_dict(legacy)
        assert src.bus_sends == ()

    def test_mixer_state_roundtrip_with_buses(self) -> None:
        state = MixerState(
            master=MasterBus(gain_db=-1.0),
            outputs=[],
            sources=[
                Source(
                    id="src-1",
                    source_node_name="airplay_in",
                    source_is_sink=True,
                    label="AP",
                    bus_sends=(BusSend(bus_id="bus-1", gain_db=-3.0),),
                ),
            ],
            buses=[
                Bus(id="bus-1", label="Subs", gain_db=-2.0, solo=False),
                Bus(id="bus-2", label="Drums", mute=True),
            ],
            vcas=[Vca(id="vca-1", label="Backline", members=("src-1",))],
        )
        restored = MixerState.from_dict(state.to_dict())
        assert restored.buses == state.buses
        assert restored.sources[0].bus_sends == state.sources[0].bus_sends
        assert restored.vcas == state.vcas

    def test_mixer_state_legacy_json_has_no_buses(self) -> None:
        """State persisted before this commit lacks the `buses` key.
        from_dict must default to an empty list, not blow up."""
        legacy = {
            "master": {
                "gain_db": 0.0,
                "mute": False,
                "mute_left": False,
                "mute_right": False,
                "inserts": [],
            },
            "outputs": [],
            "sources": [],
            "vcas": [],
        }
        state = MixerState.from_dict(legacy)
        assert state.buses == []
