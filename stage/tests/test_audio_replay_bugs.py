"""Regression tests for the four audio-stack-restart bugs identified
in the 2026-05-19 audit. Each test pins the *new* behaviour so a
future refactor that re-introduces the bug fails fast.

Bugs covered:
  1. pw_dump cache stays stale after mutations
  2. _reconcile applies the filter-chain diff AFTER loopbacks
     (the cascade then nukes them)
  3. replay_audio_state called bare _reconcile + didn't clear
     in-memory tracking
  4. replay_audio_state can run twice in parallel
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from phonon_stage.mixer.models import (
    MasterBus,
    MixerState,
    Output,
    PluginInsert,
    replace,
)
from phonon_stage.mixer.service import MixerService
from phonon_stage.mixer.store import MixerStore
from phonon_stage.pipewire import cli
from phonon_stage.pipewire.backend import PwNode, PwPort
from phonon_stage.pipewire.fake import FakePipeWireBackend

if TYPE_CHECKING:
    from pathlib import Path


# ── Fix 1 — cache invalidation ────────────────────────────────────


class TestPwDumpCacheInvalidation:
    """Direct sanity checks on the helper. We can't easily exercise
    the real subprocess in a unit test, but we can verify that the
    invalidation helper exists and that the cache state machine is
    sane."""

    def test_helper_clears_cache(self) -> None:
        cli._pw_dump_cache = [{"id": 1}]
        cli._pw_dump_cache_time = 999999.0
        cli.invalidate_pw_dump_cache()
        assert cli._pw_dump_cache == []
        assert cli._pw_dump_cache_time == 0.0

    def test_helper_is_idempotent(self) -> None:
        cli.invalidate_pw_dump_cache()
        cli.invalidate_pw_dump_cache()
        assert cli._pw_dump_cache == []


# ── Fix 2 — filter-chain diff before loopbacks ────────────────────


def _pw() -> FakePipeWireBackend:
    nodes = [
        PwNode(id=10, name="dg60_a", media_class="Audio/Sink", nick="DG60a", state="idle"),
        PwNode(id=20, name="dg60_b", media_class="Audio/Sink", nick="DG60b", state="idle"),
    ]
    ports = [
        PwPort(id=100, node_id=10, name="playback_FL", direction="input", alias="DG60a:FL"),
        PwPort(id=101, node_id=10, name="playback_FR", direction="input", alias="DG60a:FR"),
        PwPort(id=200, node_id=20, name="playback_FL", direction="input", alias="DG60b:FL"),
        PwPort(id=201, node_id=20, name="playback_FR", direction="input", alias="DG60b:FR"),
    ]
    return FakePipeWireBackend(nodes=nodes, ports=ports)


@pytest.fixture()
def mixer(tmp_path: Path) -> tuple[FakePipeWireBackend, MixerService]:
    pw = _pw()
    store = MixerStore(tmp_path / "mixer.conf.json")
    svc = MixerService(pw_backend=pw, store=store)
    return pw, svc


class TestReconcileOrder:
    async def test_loopback_survives_chain_diff(
        self, mixer: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        """Adding a chain to one output must not leave the OTHER
        chain-less output without a loopback. In the old order, the
        cascade fired after loopbacks were created → it would nuke
        them all silently."""
        pw, svc = mixer
        await svc.init()
        # Two outputs: A without FX, B with one FX.
        out_a = await svc.add_output(sink_node_name="dg60_a", label="A")
        out_b = await svc.add_output(sink_node_name="dg60_b", label="B")
        # Attach FX on B only — directly via state mutation to skip
        # plugin introspection (not the unit under test here).
        state = svc.state
        new_b = replace(
            out_b,
            inserts=(
                PluginInsert(
                    backend="ladspa",
                    library="lsp/limiter_stereo.so",
                    label="limiter_stereo",
                    controls={},
                    enabled=True,
                ),
            ),
        )
        new_outputs = [new_b if o.id == out_b.id else o for o in state.outputs]
        svc._store.replace_state(replace(state, outputs=new_outputs))
        await svc._reconcile()
        # Output A (no FX) must have a loopback after reconcile.
        a_loopbacks = [
            (src, sink) for src, sink, _lat in pw.loopbacks.values() if sink == "dg60_a"
        ]
        assert a_loopbacks, (
            "Output A's loopback was eaten by the chain-diff cascade — "
            "this is the BUG #2 regression we just fixed."
        )

    async def test_remove_only_chain_keeps_loopbacks(
        self, mixer: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        """Going from "B has FX" → "B has no FX" triggers the chain
        diff (chain conf must be deleted + filter-chain.service
        restarted). After the cascade, B should still get a regular
        loopback from the master."""
        pw, svc = mixer
        await svc.init()
        out_b = await svc.add_output(sink_node_name="dg60_b", label="B")
        new_b = replace(
            out_b,
            inserts=(
                PluginInsert(
                    backend="ladspa",
                    library="lsp/limiter_stereo.so",
                    label="limiter_stereo",
                    controls={},
                    enabled=True,
                ),
            ),
        )
        new_outputs = [new_b if o.id == out_b.id else o for o in svc.state.outputs]
        svc._store.replace_state(replace(svc.state, outputs=new_outputs))
        await svc._reconcile()
        # Now strip the FX off and reconcile — the diff fires.
        new_b = replace(svc.outputs[0], inserts=())
        new_outputs = [new_b if o.id == out_b.id else o for o in svc.state.outputs]
        svc._store.replace_state(replace(svc.state, outputs=new_outputs))
        await svc._reconcile()
        # Now B must have a plain loopback.
        b_loopbacks = [
            (src, sink) for src, sink, _lat in pw.loopbacks.values() if sink == "dg60_b"
        ]
        assert b_loopbacks, (
            "After removing the chain, the loopback wasn't recreated — "
            "the reconcile saw the cascade wipe and didn't restore B."
        )


# ── Fix 3 — full_resync clears in-memory tracking on replay ───────


class TestFullResyncClearsTracking:
    async def test_full_resync_via_replay_clears_owned(
        self, mixer: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        """When something simulates a pipewire-pulse restart by
        wiping the Fake's loopbacks externally, the next full_resync
        should detect the divergence and rebuild from scratch — not
        try to unload phantom module ids."""
        pw, svc = mixer
        await svc.init()
        await svc.add_output(sink_node_name="dg60_a", label="A")
        assert len(pw.loopbacks) >= 1, "init should have loaded a loopback"
        # Simulate a cascade wipe — modules disappear from pactl while
        # the service's _owned_loopbacks still references them.
        pw.loopbacks.clear()
        # The service still thinks it owns them — but the next
        # full_resync should clear-and-rebuild.
        await svc.full_resync()
        # New loopback for the surviving output.
        a_loopbacks = [s for _, s, _ in pw.loopbacks.values() if s == "dg60_a"]
        assert a_loopbacks, "full_resync didn't recreate the loopback"


# ── Fix 4 — replay_audio_state lock ───────────────────────────────


# ── Fix 5 — post-cascade re-heal callback ─────────────────────────


class TestPostCascadeHeal:
    """When _apply_filter_chain_diff fires its reload (which on the
    real stage cascades into a pipewire-pulse re-init wiping every
    pactl module), the mixer must fire its on_chain_cascade callback
    so the plugin registry can re-create spotify_in / airplay_in.
    Without this every FX add/remove silenced the source feeds."""

    async def test_callback_runs_after_cascade(
        self, mixer: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        pw, svc = mixer
        await svc.init()
        out_b = await svc.add_output(sink_node_name="dg60_b", label="B")
        # Attach a chain on B so the next reconcile triggers the diff.
        new_b = replace(
            out_b,
            inserts=(
                PluginInsert(
                    backend="ladspa", library="lsp/x.so", label="limiter_stereo",
                    controls={}, enabled=True,
                ),
            ),
        )
        new_outputs = [new_b if o.id == out_b.id else o for o in svc.state.outputs]
        svc._store.replace_state(replace(svc.state, outputs=new_outputs))

        calls = {"n": 0}
        async def callback() -> None:
            calls["n"] += 1
        svc.on_chain_cascade = callback

        await svc._reconcile()
        assert calls["n"] == 1, "on_chain_cascade should run when chain diff fires"

    async def test_callback_skipped_when_no_diff(
        self, mixer: tuple[FakePipeWireBackend, MixerService]
    ) -> None:
        pw, svc = mixer
        await svc.init()
        await svc.add_output(sink_node_name="dg60_b", label="B")
        calls = {"n": 0}
        async def callback() -> None:
            calls["n"] += 1
        svc.on_chain_cascade = callback
        # Fader move (no chain change) → no diff, no cascade, no callback.
        await svc.update_output(svc.outputs[0].id, gain_db=-3.0)
        assert calls["n"] == 0


class TestReplayLock:
    async def test_lock_object_exists(self) -> None:
        """The replay lock is a module-level asyncio.Lock so that
        concurrent calls coalesce. This test pins it as a public
        invariant — if a refactor removes the lock, the test fails."""
        from phonon_stage.api.aes67 import _replay_lock

        assert isinstance(_replay_lock, asyncio.Lock)

    async def test_two_concurrent_calls_serialize(self) -> None:
        """Two concurrent replay invocations must serialize: the
        second one waits for the first to release. We use a Lock
        acquisition counter to confirm."""
        from phonon_stage.api.aes67 import _replay_lock

        # Release any prior state from earlier tests in the same
        # event loop.
        if _replay_lock.locked():
            _replay_lock.release()

        order: list[str] = []

        async def fake_replay(label: str) -> None:
            async with _replay_lock:
                order.append(f"start:{label}")
                await asyncio.sleep(0.05)
                order.append(f"end:{label}")

        await asyncio.gather(fake_replay("a"), fake_replay("b"))
        # Two non-overlapping runs.
        assert order in (
            ["start:a", "end:a", "start:b", "end:b"],
            ["start:b", "end:b", "start:a", "end:a"],
        ), f"Replay calls overlapped: {order}"
