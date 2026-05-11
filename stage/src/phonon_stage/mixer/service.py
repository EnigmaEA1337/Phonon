"""Mixer service — compiles the strip-based model into PipeWire ops.

On every state mutation we re-compile from scratch: tear down the
links + loopbacks we own and rebuild from the current state. This
is simple and bullet-proof — the v1 strip count (~16) is way under
any threshold where the cost of a full rebuild would matter. We can
move to incremental updates later if needed.

What the service owns in PipeWire:
  * one null-sink named `phonon_master` (created at init, never
    destroyed for the lifetime of the daemon)
  * one module-loopback per output with `receives_master=True` and
    `mute=False`, latency_msec set to the output's delay_ms
  * one pw-link per source.monitor → master.playback channel pair
    for each source with `to_master=True` and `mute=False`
  * one pw-link per source.monitor → output.sink channel pair for
    each (source, output) in `direct_outputs`, when neither side is
    muted

Volumes and mutes:
  * Master.gain_db → set on the phonon_master sink node volume
  * Output.gain_db → set on the output sink node volume
  * Source.gain_db → set on the source node volume (works for
    null-sinks like airplay_in; capture-source volume is best-effort)
  * Mutes are structural: muted strips skip link creation entirely
    so no audio flows, rather than relying on PW volume=0.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from typing import TYPE_CHECKING

import structlog

from phonon_stage.mixer.models import (
    MasterBus,
    MixerState,
    Output,
    Source,
    replace,
    validate_delay_ms,
    validate_gain_db,
)

if TYPE_CHECKING:
    from phonon_stage.mixer.store import MixerStore
    from phonon_stage.pipewire.backend import PipeWireBackend, PwNode, PwPort

logger = structlog.get_logger()


# Name of the master-bus null-sink in PW. Fixed string — the UI
# resolves the bus by name, so changing this would require a
# coordinated UI update. Keep it stable.
MASTER_SINK_NAME = "phonon_master"
MASTER_SINK_DESCRIPTION = "Phonon-Master"


class MixerError(Exception):
    """Raised when an operation can't be satisfied — invalid input,
    capacity limit reached, or an unknown id."""


class MixerService:
    def __init__(self, pw_backend: PipeWireBackend, store: MixerStore) -> None:
        self._pw = pw_backend
        self._store = store
        # PW objects we own. Tracked so reconcile() can tear them down.
        self._owned_loopbacks: list[int] = []
        self._owned_links: list[int] = []

    # ── State accessors ─────────────────────────────────────────

    @property
    def state(self) -> MixerState:
        return self._store.state

    @property
    def master(self) -> MasterBus:
        return self._store.state.master

    @property
    def outputs(self) -> list[Output]:
        return list(self._store.state.outputs)

    @property
    def sources(self) -> list[Source]:
        return list(self._store.state.sources)

    def _output(self, output_id: str) -> Output:
        out = next((o for o in self._store.state.outputs if o.id == output_id), None)
        if out is None:
            msg = f"unknown output id: {output_id}"
            raise MixerError(msg)
        return out

    def _source(self, source_id: str) -> Source:
        s = next((s for s in self._store.state.sources if s.id == source_id), None)
        if s is None:
            msg = f"unknown source id: {source_id}"
            raise MixerError(msg)
        return s

    # ── Bootstrap ───────────────────────────────────────────────

    async def init(self) -> None:
        """Load persisted state, ensure phonon_master exists, apply
        the model to PipeWire. Idempotent — safe to call on every
        daemon startup.

        Critical: pactl modules survive across phonon-stage restarts
        as long as the user's pipewire session stays up (linger
        keeps it alive). So at every init we must scan for orphaned
        loopbacks left behind by the previous daemon — otherwise the
        first reconcile would load new loopbacks on top, and each
        output would receive the audio twice (once from old + once
        from new), producing audible doubles / comb filter."""
        self._store.load()
        await self._ensure_master_null_sink()
        await self._cleanup_orphan_loopbacks()
        await self._reconcile()

    async def _cleanup_orphan_loopbacks(self) -> None:
        """Find every module-loopback whose source argument targets
        phonon_master.monitor and unload it. We can't rely on the
        in-memory `_owned_loopbacks` here — that list is empty at
        boot — so we ask the backend for the live module list."""
        try:
            modules = await self._pw.list_loopback_modules()
        except Exception:
            logger.info("mixer.orphan_cleanup_list_failed", exc_info=False)
            return
        target = f"source={MASTER_SINK_NAME}.monitor"
        for mid, args in modules.items():
            if target not in args:
                continue
            try:
                await self._pw.unload_module(mid)
                logger.info("mixer.orphan_loopback_unloaded", module_id=mid)
            except Exception:
                logger.warning(
                    "mixer.orphan_loopback_unload_failed",
                    module_id=mid,
                    exc_info=True,
                )

    async def _ensure_master_null_sink(self) -> None:
        """phonon_master is the single shared bus null-sink. Created
        at first init, persists across phonon-stage restarts as long
        as the user's pipewire session is up. We never tear it down
        on disable — destroying the master would invalidate all
        loopbacks and break the audio for one tick on every reconcile.
        """
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            nodes = []
        if any(n.name == MASTER_SINK_NAME for n in nodes):
            return
        await self._pw.load_null_sink(MASTER_SINK_NAME, MASTER_SINK_DESCRIPTION)

    # ── Master mutations ────────────────────────────────────────

    async def update_master(
        self,
        gain_db: float | None = None,
        mute: bool | None = None,
        mute_left: bool | None = None,
        mute_right: bool | None = None,
    ) -> MasterBus:
        m = self._store.state.master
        # Track which kinds of fields are actually changing so we can
        # avoid full _reconcile when only volume-equivalent fields
        # move. A reconcile tears down + rebuilds the whole audio
        # graph (audible gap on every strip); for gain/mute_l/mute_r
        # we just need to push new channel volumes on one node.
        topology_change = False
        if gain_db is not None:
            validate_gain_db(gain_db)
            m = replace(m, gain_db=gain_db)
        if mute is not None:
            if mute != self._store.state.master.mute:
                topology_change = True
            m = replace(m, mute=mute)
        if mute_left is not None:
            m = replace(m, mute_left=mute_left)
        if mute_right is not None:
            m = replace(m, mute_right=mute_right)
        self._store.replace_state(replace(self._store.state, master=m))
        if topology_change:
            await self._reconcile()
        else:
            await self._apply_master_volume()
        return m

    async def _apply_master_volume(self) -> None:
        """Volume-only fast path for master changes — no graph teardown.
        Used when only gain_db / mute_left / mute_right move; mute (the
        global one) still tears down loopbacks so it falls back to a
        full reconcile in the caller."""
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            return
        master_node = next((n for n in nodes if n.name == MASTER_SINK_NAME), None)
        if master_node is None:
            return
        lin = self._db_to_linear(self.master.gain_db)
        try:
            await self._pw.set_node_channel_volumes(
                master_node.name,
                [
                    0.0 if self.master.mute_left else lin,
                    0.0 if self.master.mute_right else lin,
                ],
            )
        except Exception:
            logger.warning("mixer.master_volume_fast_path_failed", exc_info=True)

    # ── Output mutations ────────────────────────────────────────

    async def add_output(
        self,
        sink_node_name: str,
        label: str,
        delay_ms: float = 0.0,
        gain_db: float = 0.0,
        receives_master: bool = True,
    ) -> Output:
        from phonon_stage.mixer.models import MAX_OUTPUTS

        if len(self._store.state.outputs) >= MAX_OUTPUTS:
            msg = f"output limit reached ({MAX_OUTPUTS})"
            raise MixerError(msg)
        validate_gain_db(gain_db)
        validate_delay_ms(delay_ms)
        o = Output(
            id=uuid.uuid4().hex[:8],
            sink_node_name=sink_node_name,
            label=label,
            gain_db=gain_db,
            mute=False,
            delay_ms=delay_ms,
            receives_master=receives_master,
        )
        new_outputs = [*self._store.state.outputs, o]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        await self._reconcile()
        return o

    async def update_output(
        self,
        output_id: str,
        label: str | None = None,
        gain_db: float | None = None,
        mute: bool | None = None,
        mute_left: bool | None = None,
        mute_right: bool | None = None,
        solo: bool | None = None,
        delay_ms: float | None = None,
        receives_master: bool | None = None,
    ) -> Output:
        cur = self._output(output_id)
        new = cur
        # Classify the change so we can pick the cheapest PW update.
        # `delay_only` is a special intermediate path: it doesn't
        # need a full reconcile but it DOES need to recreate the
        # output's own loopback (pactl module-loopback latency_msec
        # is set at load time, can't be tuned live).
        topology_change = False
        delay_changed = False
        if label is not None:
            new = replace(new, label=label)
        if gain_db is not None:
            validate_gain_db(gain_db)
            new = replace(new, gain_db=gain_db)
        if mute is not None:
            if mute != cur.mute:
                topology_change = True
            new = replace(new, mute=mute)
        if mute_left is not None:
            new = replace(new, mute_left=mute_left)
        if mute_right is not None:
            new = replace(new, mute_right=mute_right)
        if solo is not None:
            if solo != cur.solo:
                topology_change = True
            new = replace(new, solo=solo)
        if delay_ms is not None:
            validate_delay_ms(delay_ms)
            if delay_ms != cur.delay_ms:
                delay_changed = True
            new = replace(new, delay_ms=delay_ms)
        if receives_master is not None:
            if receives_master != cur.receives_master:
                topology_change = True
            new = replace(new, receives_master=receives_master)
        new_outputs = [new if o.id == output_id else o for o in self._store.state.outputs]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        if topology_change:
            await self._reconcile()
        elif delay_changed:
            # Only this output's loopback needs rebuilding. Other
            # outputs / sources keep their audio flowing during the
            # ~50 ms it takes to swap one pactl module — no graph
            # teardown elsewhere.
            await self._rebuild_output_loopback(new)
            await self._apply_strip_volume_output(new)
        else:
            await self._apply_strip_volume_output(new)
        return new

    async def _apply_strip_volume_output(self, output: Output) -> None:
        """Set per-channel volume on a single output sink — no topology
        change. Used for gain_db / mute_left / mute_right moves where
        a full reconcile would needlessly silence every other strip."""
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            return
        sink_node = next((n for n in nodes if n.name == output.sink_node_name), None)
        if sink_node is None:
            return
        lin = self._db_to_linear(output.gain_db)
        try:
            await self._pw.set_node_channel_volumes(
                sink_node.name,
                [
                    0.0 if output.mute_left else lin,
                    0.0 if output.mute_right else lin,
                ],
            )
        except Exception:
            logger.warning(
                "mixer.output_volume_fast_path_failed",
                output_id=output.id,
                exc_info=True,
            )

    async def _rebuild_output_loopback(self, output: Output) -> None:
        """Targeted unload + reload of a single output's master loopback.
        Other outputs/links are untouched.

        Click-masking: unloading a pactl module-loopback drains its
        buffer instantly, producing an audible click on the
        destination sink (witnessed live as "petit bruit strident").
        We pre-mute the sink, wait one tick for the last buffer cycle
        to flush, swap the loopback, wait for the new one to fill,
        then restore the sink's volume. Net effect: a ~80 ms silence
        dip on this output during a delay change, but no click."""
        try:
            modules = await self._pw.list_loopback_modules()
        except Exception:
            return
        master_src = f"source={MASTER_SINK_NAME}.monitor"
        sink_match = f"sink={output.sink_node_name}"
        to_unload = [
            mid
            for mid, args in modules.items()
            if master_src in args and sink_match in args
        ]

        # Pre-mute the destination sink so the impending unload's
        # buffer-drain click doesn't make it to the speakers.
        muted_for_swap = False
        if to_unload:
            try:
                await self._pw.set_node_channel_volumes(
                    output.sink_node_name, [0.0, 0.0]
                )
                muted_for_swap = True
                # Give the last audio buffer ~30 ms to flush through
                # ALSA before the loopback disappears. Empirically
                # enough on USB Audio (DG60) at 48 kHz with the
                # default quantum.
                await asyncio.sleep(0.030)
            except Exception:
                # Best-effort: if the pre-mute fails (sink missing,
                # pactl unreachable) we proceed anyway. User hears
                # the click rather than the operation failing.
                pass

        for mid in to_unload:
            try:
                await self._pw.unload_module(mid)
            except Exception:
                continue
            if mid in self._owned_loopbacks:
                self._owned_loopbacks.remove(mid)

        out_solo_active = any(o.solo and not o.mute for o in self._store.state.outputs)
        if (
            output.receives_master
            and not output.mute
            and not self.master.mute
            and not (out_solo_active and not output.solo)
        ):
            new_mid = await self._pw.load_loopback(
                f"{MASTER_SINK_NAME}.monitor",
                output.sink_node_name,
                int(output.delay_ms),
            )
            if new_mid is not None:
                self._owned_loopbacks.append(new_mid)

        # Let the new loopback prime its buffer before un-muting so
        # the audio comes back cleanly, not mid-frame.
        if muted_for_swap:
            await asyncio.sleep(0.030)
            lin = self._db_to_linear(output.gain_db)
            try:
                await self._pw.set_node_channel_volumes(
                    output.sink_node_name,
                    [
                        0.0 if output.mute_left else lin,
                        0.0 if output.mute_right else lin,
                    ],
                )
            except Exception:
                logger.warning(
                    "mixer.swap_volume_restore_failed",
                    output_id=output.id,
                    exc_info=True,
                )

    async def remove_output(self, output_id: str) -> None:
        # Validate existence first.
        self._output(output_id)
        # Cascade: drop the id from every source's direct_outputs list
        # so we don't leave dangling references that would fail at
        # reconcile time.
        new_sources = [
            replace(
                s,
                direct_outputs=tuple(d for d in s.direct_outputs if d != output_id),
            )
            for s in self._store.state.sources
        ]
        new_outputs = [o for o in self._store.state.outputs if o.id != output_id]
        self._store.replace_state(
            replace(self._store.state, outputs=new_outputs, sources=new_sources)
        )
        await self._reconcile()

    # ── Source mutations ────────────────────────────────────────

    async def add_source(
        self,
        source_node_name: str,
        source_is_sink: bool,
        label: str,
        gain_db: float = 0.0,
        to_master: bool = True,
        direct_outputs: list[str] | None = None,
    ) -> Source:
        from phonon_stage.mixer.models import MAX_SOURCES

        if len(self._store.state.sources) >= MAX_SOURCES:
            msg = f"source limit reached ({MAX_SOURCES})"
            raise MixerError(msg)
        validate_gain_db(gain_db)
        if direct_outputs:
            known = {o.id for o in self._store.state.outputs}
            unknown = [d for d in direct_outputs if d not in known]
            if unknown:
                msg = f"direct_outputs references unknown id(s): {unknown}"
                raise MixerError(msg)
        s = Source(
            id=uuid.uuid4().hex[:8],
            source_node_name=source_node_name,
            source_is_sink=source_is_sink,
            label=label,
            gain_db=gain_db,
            mute=False,
            to_master=to_master,
            direct_outputs=tuple(direct_outputs or ()),
        )
        new_sources = [*self._store.state.sources, s]
        self._store.replace_state(replace(self._store.state, sources=new_sources))
        await self._reconcile()
        return s

    async def update_source(
        self,
        source_id: str,
        label: str | None = None,
        gain_db: float | None = None,
        mute: bool | None = None,
        mute_left: bool | None = None,
        mute_right: bool | None = None,
        solo: bool | None = None,
        to_master: bool | None = None,
        direct_outputs: list[str] | None = None,
    ) -> Source:
        cur = self._source(source_id)
        new = cur
        topology_change = False
        if label is not None:
            new = replace(new, label=label)
        if gain_db is not None:
            validate_gain_db(gain_db)
            new = replace(new, gain_db=gain_db)
        if mute is not None:
            if mute != cur.mute:
                topology_change = True
            new = replace(new, mute=mute)
        if mute_left is not None:
            new = replace(new, mute_left=mute_left)
        if mute_right is not None:
            new = replace(new, mute_right=mute_right)
        if solo is not None:
            if solo != cur.solo:
                topology_change = True
            new = replace(new, solo=solo)
        if to_master is not None:
            if to_master != cur.to_master:
                topology_change = True
            new = replace(new, to_master=to_master)
        if direct_outputs is not None:
            known = {o.id for o in self._store.state.outputs}
            unknown = [d for d in direct_outputs if d not in known]
            if unknown:
                msg = f"direct_outputs references unknown id(s): {unknown}"
                raise MixerError(msg)
            if tuple(direct_outputs) != cur.direct_outputs:
                topology_change = True
            new = replace(new, direct_outputs=tuple(direct_outputs))
        new_sources = [new if s.id == source_id else s for s in self._store.state.sources]
        self._store.replace_state(replace(self._store.state, sources=new_sources))
        if topology_change:
            await self._reconcile()
        else:
            await self._apply_strip_volume_source(new)
        return new

    async def _apply_strip_volume_source(self, source: Source) -> None:
        """Volume-only fast path on a source node. Same idea as the
        output variant — for gain_db / mute_left / mute_right we just
        push channel volumes on the source PW node, no graph change."""
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            return
        src_node = next((n for n in nodes if n.name == source.source_node_name), None)
        if src_node is None:
            return
        lin = self._db_to_linear(source.gain_db)
        try:
            await self._pw.set_node_channel_volumes(
                src_node.name,
                [
                    0.0 if source.mute_left else lin,
                    0.0 if source.mute_right else lin,
                ],
            )
        except Exception:
            logger.warning(
                "mixer.source_volume_fast_path_failed",
                source_id=source.id,
                exc_info=True,
            )

    async def remove_source(self, source_id: str) -> None:
        self._source(source_id)
        new_sources = [s for s in self._store.state.sources if s.id != source_id]
        self._store.replace_state(replace(self._store.state, sources=new_sources))
        await self._reconcile()

    # ── Reconciliation ──────────────────────────────────────────

    async def _reconcile(self) -> None:
        """Tear down every PW object we own and rebuild from the
        current MixerState. The master null-sink is never destroyed
        here — it's created once at init and persists."""
        # 1. Tear down what we created on the previous reconcile.
        for owned_mid in self._owned_loopbacks:
            with contextlib.suppress(Exception):
                await self._pw.unload_module(owned_mid)
        for lid in self._owned_links:
            with contextlib.suppress(Exception):
                await self._pw.destroy_link(lid)
        self._owned_loopbacks.clear()
        self._owned_links.clear()

        # 2. Lookups we'll need throughout the rebuild.
        try:
            nodes = await self._pw.list_nodes()
            ports = await self._pw.list_ports()
        except Exception:
            logger.warning("mixer.reconcile_lookup_failed", exc_info=True)
            return
        master_node = next((n for n in nodes if n.name == MASTER_SINK_NAME), None)
        if master_node is None:
            # Master sink doesn't exist yet — possible on a fresh
            # daemon before init() ran. We bail out gracefully; the
            # next reconcile (after init) will succeed.
            logger.info("mixer.reconcile_skip_no_master")
            return

        # 3. Determine solo state up front. A "solo group" is any
        #    set of strips with solo=True and mute=False — muting a
        #    solo'd strip cancels its solo intent (intuitive: the
        #    operator doesn't want silence everywhere just because
        #    they muted a solo'd strip). When the group is non-empty
        #    on a given side (sources / outputs), every non-member
        #    of that side gets silenced via link suppression.
        src_solo_active = any(s.solo and not s.mute for s in self._store.state.sources)
        out_solo_active = any(o.solo and not o.mute for o in self._store.state.outputs)

        # 4. Master volume (per-channel for L/R mutes) + global mute.
        master_lin = self._db_to_linear(self.master.gain_db)
        try:
            await self._pw.set_node_channel_volumes(
                master_node.name,
                [
                    0.0 if self.master.mute_left else master_lin,
                    0.0 if self.master.mute_right else master_lin,
                ],
            )
            await self._pw.set_node_mute(master_node.id, self.master.mute)
        except Exception:
            logger.warning("mixer.master_apply_failed", exc_info=True)

        # 5. For each output: per-channel volume on the sink, then
        #    the master→output loopback (skipped on mute / solo
        #    suppression / receives_master=False).
        for o in self._store.state.outputs:
            sink_node = next((n for n in nodes if n.name == o.sink_node_name), None)
            if sink_node is None:
                logger.info(
                    "mixer.output_sink_missing",
                    output_id=o.id,
                    sink_node_name=o.sink_node_name,
                )
                continue
            o_lin = self._db_to_linear(o.gain_db)
            try:
                await self._pw.set_node_channel_volumes(
                    sink_node.name,
                    [
                        0.0 if o.mute_left else o_lin,
                        0.0 if o.mute_right else o_lin,
                    ],
                )
            except Exception:
                logger.warning("mixer.output_volume_failed", output_id=o.id, exc_info=True)
            # Solo gating on the output side: when any output is
            # solo'd (and not muted), only the solo group keeps its
            # master loopback and its receive of direct sends. The
            # rest are silenced structurally (no link).
            output_silenced = out_solo_active and not o.solo
            if o.mute or self.master.mute or output_silenced:
                continue
            if o.receives_master:
                mid = await self._pw.load_loopback(
                    f"{MASTER_SINK_NAME}.monitor",
                    o.sink_node_name,
                    int(o.delay_ms),
                )
                if mid is not None:
                    self._owned_loopbacks.append(mid)

        # 6. For each source: per-channel volume + outbound links.
        for s in self._store.state.sources:
            src_node = next((n for n in nodes if n.name == s.source_node_name), None)
            if src_node is None:
                logger.info(
                    "mixer.source_node_missing",
                    source_id=s.id,
                    source_node_name=s.source_node_name,
                )
                continue
            s_lin = self._db_to_linear(s.gain_db)
            try:
                await self._pw.set_node_channel_volumes(
                    src_node.name,
                    [
                        0.0 if s.mute_left else s_lin,
                        0.0 if s.mute_right else s_lin,
                    ],
                )
            except Exception:
                logger.warning("mixer.source_volume_failed", source_id=s.id, exc_info=True)
            # Mute or solo gating: skip link creation entirely so
            # the audio doesn't even reach the destination.
            source_silenced = src_solo_active and not s.solo
            if s.mute or source_silenced:
                continue
            # Output ports of the source — for a null-sink that's its
            # monitor_FL/FR (direction=output); for a real Audio/Source
            # that's its capture_FL/FR.
            src_out_ports = self._ordered_output_ports(ports, src_node)
            if not src_out_ports:
                continue

            if s.to_master and not self.master.mute:
                master_in_ports = self._ordered_input_ports(ports, master_node)
                await self._link_pairs(src_out_ports, master_in_ports)

            for output_id in s.direct_outputs:
                output = next((o for o in self._store.state.outputs if o.id == output_id), None)
                if output is None or output.mute:
                    continue
                # Direct sends are also suppressed when the target
                # output is solo-silenced — keeps the solo invariant
                # consistent across both routing paths.
                if out_solo_active and not output.solo:
                    continue
                output_sink = next((n for n in nodes if n.name == output.sink_node_name), None)
                if output_sink is None:
                    continue
                output_in_ports = self._ordered_input_ports(ports, output_sink)
                await self._link_pairs(src_out_ports, output_in_ports)

        logger.info(
            "mixer.reconciled",
            outputs=len(self._store.state.outputs),
            sources=len(self._store.state.sources),
            links=len(self._owned_links),
            loopbacks=len(self._owned_loopbacks),
        )

    async def _link_pairs(self, src_ports: list[int], dst_ports: list[int]) -> None:
        """Zip-pair two pre-ordered port lists and create the links.
        Tracks the resulting link ids so reconcile can tear them
        down on the next pass."""
        for src, dst in zip(src_ports, dst_ports, strict=False):
            try:
                link = await self._pw.create_link(src, dst)
                self._owned_links.append(link.id)
            except Exception:
                logger.warning("mixer.create_link_failed", src=src, dst=dst, exc_info=True)

    @staticmethod
    def _ordered_output_ports(ports: list[PwPort], node: PwNode) -> list[int]:
        """Return the node's output port ids, sorted by port name so
        FL precedes FR. Same fix as MappingService._order_ports_by_name
        — PW assigns ids in graph-arrival order, not L/R convention."""
        ns = [p for p in ports if p.node_id == node.id and p.direction == "output"]
        ns.sort(key=lambda p: p.name)
        return [p.id for p in ns]

    @staticmethod
    def _ordered_input_ports(ports: list[PwPort], node: PwNode) -> list[int]:
        ns = [p for p in ports if p.node_id == node.id and p.direction == "input"]
        ns.sort(key=lambda p: p.name)
        return [p.id for p in ns]

    @staticmethod
    def _db_to_linear(db: float) -> float:
        """Convert a strip dB value to a linear volume in [0, ~4].
        0 dB = 1.0, -6 dB ≈ 0.5, -∞ → 0."""
        if db <= -60.0:
            return 0.0
        return float(10 ** (db / 20.0))


def looks_like_sink_source(node_name: str) -> bool:
    """Heuristic for whether a node name belongs to a null-sink-style
    source (consume via .monitor) or a real Audio/Source (read
    directly). Used by the UI when offering new sources; doesn't
    drive any routing decision directly."""
    return bool(re.match(r"^(bt_.+_in|airplay_in|spotify_in|mpd_in)$", node_name))


__all__ = [
    "MASTER_SINK_DESCRIPTION",
    "MASTER_SINK_NAME",
    "MixerError",
    "MixerService",
    "looks_like_sink_source",
]
