"""Tests for the mixer console service.

Validates the compile-to-PipeWire pipeline: master null-sink lifecycle,
per-output loopback with delay, master/direct routing, mute semantics,
validation, and persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.mixer.models import MasterBus, MixerState, Output
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
        await fake_pw.load_loopback(
            f"{MASTER_SINK_NAME}.monitor", "alsa_output.dg60_1", 90
        )
        await fake_pw.load_loopback(
            f"{MASTER_SINK_NAME}.monitor", "alsa_output.dg60_2", 0
        )
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
        await service.add_source(
            source_node_name="bt_phone_in", source_is_sink=True, label="BT"
        )
        # Solo A, then mute A → both must continue routing (solo gone).
        await service.update_source(s_a.id, solo=True)
        assert len(fake_pw.links) == 2  # only A
        await service.update_source(s_a.id, mute=True)
        # A is muted (its links gone), but B is back (no active solo).
        assert len(fake_pw.links) == 2  # only B now

    async def test_output_solo_suppresses_others(
        self, service: MixerService, fake_pw: FakePipeWireBackend
    ) -> None:
        o_a = await service.add_output(
            sink_node_name="alsa_output.dg60_1", label="A"
        )
        o_b = await service.add_output(
            sink_node_name="alsa_output.dg60_2", label="B"
        )
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
        o_a = await service.add_output(
            sink_node_name="alsa_output.dg60_1", label="A"
        )
        o_b = await service.add_output(
            sink_node_name="alsa_output.dg60_2", label="B"
        )
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
        await service.add_output(
            sink_node_name="alsa_output.dg60_1", label="A", delay_ms=50.0
        )
        await service.add_source(
            source_node_name="airplay_in", source_is_sink=True, label="AP"
        )
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
            mid for mid, (_, sink, _) in fake_pw.loopbacks.items()
            if sink == "alsa_output.dg60_2"
        )
        a_loopback_id = next(
            mid for mid, (_, sink, _) in fake_pw.loopbacks.items()
            if sink == "alsa_output.dg60_1"
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
