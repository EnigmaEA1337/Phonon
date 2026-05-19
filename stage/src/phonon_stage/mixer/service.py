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

from phonon_stage.mixer.filter_chain import (
    MASTER_CHAIN_NAME,
    MASTER_POST_SINK_NAME,
    chain_name_for,
    render_filter_chain_conf,
    render_master_filter_chain_conf,
)
from phonon_stage.mixer.models import (
    MasterBus,
    MixerState,
    Output,
    PluginInsert,
    Source,
    replace,
    validate_delay_ms,
    validate_gain_db,
)

if TYPE_CHECKING:
    from phonon_stage.dsp.ladspa import LadspaIntrospector
    from phonon_stage.mixer.sessions import Session, SessionMeta, SessionStore
    from phonon_stage.mixer.store import MixerStore
    from phonon_stage.pipewire.backend import PipeWireBackend, PwNode, PwPort

logger = structlog.get_logger()


# Name of the master-bus null-sink in PW. Fixed string — the UI
# resolves the bus by name, so changing this would require a
# coordinated UI update. Keep it stable.
MASTER_SINK_NAME = "phonon_master"
MASTER_SINK_DESCRIPTION = "Phonon-Master"
# Post-master null-sink: only created when master.inserts is non-empty.
# Outputs read from this sink's monitor instead of phonon_master.monitor
# when a master FX chain is active. See filter_chain.MASTER_POST_SINK_NAME
# (kept in sync — both names point at the same string).
MASTER_POST_DESCRIPTION = "Phonon-Master-Post"


def _effective_master_source(master: MasterBus) -> str:
    """Name of the PW node whose .monitor port outputs should read
    from. When the master has 0 plugins → phonon_master (current
    behaviour). When master FX is active → phonon_master_post (sits
    downstream of the master chain). Outputs don't care which one;
    they just consume from .monitor of whichever this returns."""
    return MASTER_POST_SINK_NAME if master.inserts else MASTER_SINK_NAME


class MixerError(Exception):
    """Raised when an operation can't be satisfied — invalid input,
    capacity limit reached, or an unknown id."""


class MixerService:
    # How long to wait after applying a filter-chain diff before
    # poking PipeWire again. The diff cascades into a pipewire-pulse
    # re-init that takes ~500-1500 ms to settle on this stage. Tests
    # override to 0 — the FakePipeWireBackend doesn't cascade.
    POST_CHAIN_DIFF_SLEEP_S: float = 1.5

    def __init__(
        self,
        pw_backend: PipeWireBackend,
        store: MixerStore,
        introspector: LadspaIntrospector | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self._pw = pw_backend
        self._store = store
        self._introspector = introspector
        self._sessions = session_store
        # PW objects we own. Tracked so reconcile() can tear them down.
        self._owned_loopbacks: list[int] = []
        self._owned_links: list[int] = []
        # Filter-chain confs we wrote on the previous reconcile, mapped
        # chain_name → conf_body. Diffing against the next reconcile's
        # "wanted" set is what lets us skip reload_filter_chain unless
        # there's an actual change — restarting filter-chain.service
        # glitches every running chain so we avoid it on no-op moves.
        self._owned_chains: dict[str, str] = {}

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
        await self.full_resync()

    async def full_resync(self) -> None:
        """Full from-scratch sync: ensure phonon_master, sweep
        orphan loopbacks + filter-chains, then reconcile.

        Used by init() at boot, and by POST /mixer/admin/reconcile
        when the operator presses Resync. The plain _reconcile()
        alone isn't enough after a PipeWire restart: the master
        null-sink may have disappeared with the pactl modules, and
        loopbacks left over from the previous daemon would
        otherwise double-up the audio when we load fresh ones."""
        await self._ensure_master_null_sink()
        await self._cleanup_orphan_loopbacks()
        await self._cleanup_orphan_chains()
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

    async def _cleanup_orphan_chains(self) -> None:
        """Delete only the filter-chain confs that the persisted state
        no longer wants — anything the next reconcile will re-create
        is left alone, so we don't reload filter-chain.service unless
        we actually have to.

        Critical: on this stage's setup `systemctl --user restart
        filter-chain.service` cascades into a pipewire-pulse re-init
        that wipes every pactl-loaded module (phonon_master null-sink,
        airplay_in, AES67 send sinks...). Reloading needlessly here
        was nuking our own audio graph at every boot. So: compute
        the wanted set first, delete the diff, reload only if there
        IS a diff."""
        try:
            on_disk = await self._pw.list_filter_chain_confs()
        except Exception:
            logger.info("mixer.orphan_chain_list_failed", exc_info=False)
            return
        # Build the body we'd generate for each wanted chain so the
        # first _reconcile's diff sees the on-disk state as already
        # in-sync — no spurious rewrite + reload.
        master = self._store.state.master
        master_src = _effective_master_source(master)
        wanted_bodies: dict[str, str] = {}
        # Master chain conf if master has any inserts (enabled or not —
        # passthrough is still rendered to keep the node.name present).
        if master.inserts:
            wanted_bodies[MASTER_CHAIN_NAME] = render_master_filter_chain_conf(
                master.inserts, MASTER_SINK_NAME, MASTER_POST_SINK_NAME
            )
        for o in self._store.state.outputs:
            if o.inserts and any(i.enabled for i in o.inserts) and o.receives_master:
                wanted_bodies[chain_name_for(o)] = render_filter_chain_conf(
                    o, o.inserts, master_src
                )
        stale = [c for c in on_disk if c not in wanted_bodies]
        if not stale:
            self._owned_chains = dict(wanted_bodies)
            return
        for chain in stale:
            try:
                await self._pw.delete_filter_chain_conf(chain)
            except Exception:
                logger.warning("mixer.orphan_chain_delete_failed", chain=chain, exc_info=True)
        try:
            await self._pw.reload_filter_chain()
            logger.info("mixer.orphan_chains_cleared", count=len(stale))
        except Exception:
            logger.warning("mixer.orphan_chain_reload_failed", exc_info=True)
        # Same as _apply_filter_chain_diff: re-push persisted control
        # values so the engine state matches what the UI shows.
        await self._resync_all_chain_controls()
        self._owned_chains = dict(wanted_bodies)

    async def _ensure_master_null_sink(self) -> None:
        """phonon_master is the single shared bus null-sink. Created
        at first init, persists across phonon-stage restarts as long
        as the user's pipewire session is up. We never tear it down
        on disable — destroying the master would invalidate all
        loopbacks and break the audio for one tick on every reconcile.

        Also: if the previous self-heal pass loaded a duplicate
        (boot-time race with pactl), keep ONE instance and unload
        the rest. Two null-sinks with the same name confuse PW link
        routing — sources write to one, filter-chains capture from
        the other, master appears silent even though audio is being
        received.
        """
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            nodes = []
        master_count = sum(1 for n in nodes if n.name == MASTER_SINK_NAME)
        if master_count == 0:
            await self._pw.load_null_sink(MASTER_SINK_NAME, MASTER_SINK_DESCRIPTION)
            return
        if master_count == 1:
            return
        # Duplicate detected — find pactl module IDs for every
        # null-sink named `phonon_master` and unload all but the
        # oldest (lowest module id) so the existing source→master
        # links stay intact.
        try:
            modules = await self._pw.list_null_sink_modules()
        except Exception:
            logger.warning("mixer.master_dedupe_list_failed", exc_info=True)
            return
        master_mids = sorted(mid for mid, name in modules.items() if name == MASTER_SINK_NAME)
        if len(master_mids) < 2:
            # PW sees two nodes but pactl can only attribute one to
            # us — second instance might be wireplumber-owned or
            # something we shouldn't touch. Leave it alone.
            logger.info(
                "mixer.master_dedupe_skip",
                pw_count=master_count,
                pactl_count=len(master_mids),
            )
            return
        keep = master_mids[0]
        for mid in master_mids[1:]:
            try:
                await self._pw.unload_module(mid)
                logger.info("mixer.master_duplicate_unloaded", module_id=mid, kept=keep)
            except Exception:
                logger.warning("mixer.master_dedupe_unload_failed", module_id=mid, exc_info=True)

    async def _ensure_master_post_null_sink(self) -> None:
        """phonon_master_post is the SECOND null-sink, created ONLY
        when the master has at least one plugin in its chain. The
        master filter-chain reads from phonon_master.monitor and
        writes to phonon_master_post.playback; every Output then
        reads from phonon_master_post.monitor instead of phonon_master.

        When master.inserts is empty, this sink is torn down via
        _unload_master_post_null_sink and Outputs go back to reading
        from phonon_master directly.
        """
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            nodes = []
        existing = sum(1 for n in nodes if n.name == MASTER_POST_SINK_NAME)
        if existing == 0:
            await self._pw.load_null_sink(MASTER_POST_SINK_NAME, MASTER_POST_DESCRIPTION)

    async def _unload_master_post_null_sink(self) -> None:
        """Tear down phonon_master_post + every loopback pointing at it.
        Called when the master chain becomes empty so we revert to the
        single-master topology."""
        try:
            modules = await self._pw.list_null_sink_modules()
        except Exception:
            logger.info("mixer.master_post_unload_list_failed", exc_info=False)
            return
        for mid, name in modules.items():
            if name == MASTER_POST_SINK_NAME:
                try:
                    await self._pw.unload_module(mid)
                    logger.info("mixer.master_post_unloaded", module_id=mid)
                except Exception:
                    logger.warning(
                        "mixer.master_post_unload_failed",
                        module_id=mid,
                        exc_info=True,
                    )

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

    async def set_output_insert(
        self,
        output_id: str,
        backend: str | None,
        library: str | None,
        label: str | None,
    ) -> Output:
        """Attach (or detach) a plugin to an output's master path.

        Pass `backend=None` (or `label=None`) to clear the insert; the
        output reverts to a plain loopback. Pass a fresh
        (backend, library, label) triple to attach a plugin — the
        service introspects the plugin to seed sensible defaults
        (whatever the LADSPA descriptor declares as `default`) so the
        user gets a working plugin from the first reconcile without
        having to fill every control by hand.

        Triggers a full _reconcile so the chain conf is generated
        and filter-chain.service reloaded.
        """
        cur = self._output(output_id)
        if backend is None or label is None:
            # Clear the whole chain. v1 single-insert API semantics:
            # "set insert=None" means "no plugins on this output".
            new = replace(cur, inserts=())
        else:
            defaults = await self._introspect_defaults(backend, library or "", label)
            # Preserve any prior controls the user had set for this
            # exact plugin — if they're swapping plugins, defaults
            # replace; if they're re-enabling the same plugin after a
            # detach, prior values would already have been wiped at
            # detach time (clearing the chain drops controls).
            new = replace(
                cur,
                inserts=(
                    PluginInsert(
                        backend=backend,
                        library=library or "",
                        label=label,
                        controls=defaults,
                        enabled=True,
                    ),
                ),
            )
        new_outputs = [new if o.id == output_id else o for o in self._store.state.outputs]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        await self._reconcile()
        return new

    async def append_chain_insert(
        self, output_id: str, backend: str, library: str, label: str
    ) -> Output:
        """Append a plugin to an output's chain. Each plugin's `node.name`
        in the rendered conf is unique (fx_0, fx_1, …) so PW links them
        in series automatically. Caps at MAX_CHAIN_DEPTH to stop the
        operator from stacking an unreasonable load.
        """
        from phonon_stage.mixer.models import MAX_CHAIN_DEPTH

        cur = self._output(output_id)
        if len(cur.inserts) >= MAX_CHAIN_DEPTH:
            msg = f"chain depth at cap ({MAX_CHAIN_DEPTH}); remove a plugin before adding"
            raise MixerError(msg)
        defaults = await self._introspect_defaults(backend, library, label)
        new_insert = PluginInsert(
            backend=backend,
            library=library,
            label=label,
            controls=defaults,
            enabled=True,
        )
        new = replace(cur, inserts=(*cur.inserts, new_insert))
        new_outputs = [new if o.id == output_id else o for o in self._store.state.outputs]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        await self._reconcile()
        return new

    async def remove_chain_insert(self, output_id: str, slot: int) -> Output:
        """Remove the plugin at `slot` from the chain. Other slots
        shift down (slot 3 becomes 2 if you remove slot 2)."""
        cur = self._output(output_id)
        if not (0 <= slot < len(cur.inserts)):
            msg = f"slot {slot} out of range (chain has {len(cur.inserts)} plugins)"
            raise MixerError(msg)
        remaining = tuple(i for idx, i in enumerate(cur.inserts) if idx != slot)
        new = replace(cur, inserts=remaining)
        new_outputs = [new if o.id == output_id else o for o in self._store.state.outputs]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        await self._reconcile()
        return new

    async def reset_chain(self, output_id: str) -> Output:
        """Clear the whole chain — output reverts to a plain loopback.
        Equivalent to remove_chain_insert in a loop, but a single
        reconcile so the user-visible reload happens once."""
        cur = self._output(output_id)
        if not cur.inserts:
            return cur
        new = replace(cur, inserts=())
        new_outputs = [new if o.id == output_id else o for o in self._store.state.outputs]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        await self._reconcile()
        return new

    async def set_insert_enabled(self, output_id: str, slot: int, enabled: bool) -> Output:
        """Flip the .enabled flag on a chain slot. When all slots in
        the chain are disabled the filter-chain conf renders a
        passthrough (builtin copy node) so the audio passes through
        unprocessed without tearing the chain down — same node.name
        in PW, just no LADSPA plugin loaded inside.

        Triggers a reconcile so the conf gets rewritten + the
        filter-chain.service reloaded. Cost ~100-200ms on stage-x99.
        Used by the strip-level bypass toggle (more reliable than
        the LSP `Bypass` continuous crossfade param)."""
        cur = self._output(output_id)
        if not (0 <= slot < len(cur.inserts)):
            msg = f"slot {slot} out of range (chain has {len(cur.inserts)} plugins)"
            raise MixerError(msg)
        target = cur.inserts[slot]
        if target.enabled == enabled:
            return cur
        new_insert = replace(target, enabled=enabled)
        new_inserts = tuple(new_insert if i == slot else ins for i, ins in enumerate(cur.inserts))
        new = replace(cur, inserts=new_inserts)
        new_outputs = [new if o.id == output_id else o for o in self._store.state.outputs]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        await self._reconcile()
        return new

    # ── Master chain mutations ─────────────────────────────────────
    # Mirrors the Output chain methods above but operates on the
    # single MasterBus.inserts tuple. Each call triggers a reconcile
    # which ensures (or tears down) phonon_master_post + the master
    # filter-chain conf so the routing matches the new state.

    async def append_master_insert(self, backend: str, library: str, label: str) -> MasterBus:
        from phonon_stage.mixer.models import MAX_CHAIN_DEPTH

        master = self._store.state.master
        if len(master.inserts) >= MAX_CHAIN_DEPTH:
            msg = f"master chain depth at cap ({MAX_CHAIN_DEPTH}); remove a plugin before adding"
            raise MixerError(msg)
        defaults = await self._introspect_defaults(backend, library, label)
        new_insert = PluginInsert(
            backend=backend,
            library=library,
            label=label,
            controls=defaults,
            enabled=True,
        )
        new_master = replace(master, inserts=(*master.inserts, new_insert))
        self._store.replace_state(replace(self._store.state, master=new_master))
        await self._reconcile()
        return new_master

    async def remove_master_insert(self, slot: int) -> MasterBus:
        master = self._store.state.master
        if not (0 <= slot < len(master.inserts)):
            msg = f"slot {slot} out of range (master chain has {len(master.inserts)} plugins)"
            raise MixerError(msg)
        remaining = tuple(i for idx, i in enumerate(master.inserts) if idx != slot)
        new_master = replace(master, inserts=remaining)
        self._store.replace_state(replace(self._store.state, master=new_master))
        await self._reconcile()
        return new_master

    async def reset_master_chain(self) -> MasterBus:
        master = self._store.state.master
        if not master.inserts:
            return master
        new_master = replace(master, inserts=())
        self._store.replace_state(replace(self._store.state, master=new_master))
        await self._reconcile()
        return new_master

    async def set_master_insert_enabled(self, slot: int, enabled: bool) -> MasterBus:
        master = self._store.state.master
        if not (0 <= slot < len(master.inserts)):
            msg = f"slot {slot} out of range (master chain has {len(master.inserts)} plugins)"
            raise MixerError(msg)
        target = master.inserts[slot]
        if target.enabled == enabled:
            return master
        new_insert = replace(target, enabled=enabled)
        new_inserts = tuple(
            new_insert if i == slot else ins for i, ins in enumerate(master.inserts)
        )
        new_master = replace(master, inserts=new_inserts)
        self._store.replace_state(replace(self._store.state, master=new_master))
        await self._reconcile()
        return new_master

    async def update_master_insert_control(
        self, control_name: str, value: float, slot: int = 0
    ) -> MasterBus:
        """Live-update one master plugin control value. Same shape as
        update_output_insert_control: writes to the persisted state,
        keeps the in-memory chain body cache in sync, and pushes a
        live set-param to the engine so the change is heard
        immediately without a chain reload."""
        master = self._store.state.master
        if not (0 <= slot < len(master.inserts)):
            msg = f"slot {slot} out of range (master chain has {len(master.inserts)} plugins)"
            raise MixerError(msg)
        target = master.inserts[slot]
        if control_name not in target.controls:
            valid = await self._introspect_control_exists(
                target.backend, target.library, target.label, control_name
            )
            if not valid:
                msg = (
                    f"unknown control {control_name!r} on master slot {slot} "
                    f"plugin {target.label!r}"
                )
                raise MixerError(msg)
        new_controls = dict(target.controls)
        new_controls[control_name] = float(value)
        new_insert = replace(target, controls=new_controls)
        new_inserts = tuple(
            new_insert if i == slot else ins for i, ins in enumerate(master.inserts)
        )
        new_master = replace(master, inserts=new_inserts)
        self._store.replace_state(replace(self._store.state, master=new_master))
        # In-memory body cache sync so the next non-control reconcile
        # doesn't reload filter-chain.service for nothing.
        if MASTER_CHAIN_NAME in self._owned_chains and new_master.inserts:
            self._owned_chains[MASTER_CHAIN_NAME] = render_master_filter_chain_conf(
                new_master.inserts, MASTER_SINK_NAME, MASTER_POST_SINK_NAME
            )
        try:
            await self._pw.set_filter_node_control(MASTER_CHAIN_NAME, control_name, value)
        except Exception:
            logger.warning(
                "mixer.master_insert_control_set_failed",
                control=control_name,
                exc_info=True,
            )
        return new_master

    async def _introspect_control_exists(
        self, backend: str, library: str, label: str, control_name: str
    ) -> bool:
        """Best-effort check whether a control with `control_name` is
        declared by the plugin's LADSPA descriptor. Returns False if
        we have no introspector wired (Pi Stages) or the plugin is
        unknown — in which case the caller falls back to the strict
        "unknown control" error."""
        if self._introspector is None or backend != "ladspa":
            return False
        try:
            desc = await self._introspector.describe(library, label)
        except Exception:
            return False
        return any(c.name == control_name for c in desc.controls)

    async def _introspect_defaults(
        self, backend: str, library: str, label: str
    ) -> dict[str, float]:
        """Ask the LadspaIntrospector for the plugin's default values.
        Returns {} if no introspector was wired (Pi Stages) or if the
        plugin is unknown — the chain still loads, just with whatever
        the LADSPA library's own internal defaults are."""
        if self._introspector is None or backend != "ladspa":
            return {}
        try:
            desc = await self._introspector.describe(library, label)
        except Exception:
            logger.warning(
                "mixer.introspect_failed",
                library=library,
                label=label,
                exc_info=True,
            )
            return {}
        defaults: dict[str, float] = {}
        for c in desc.controls:
            if c.direction != "input" or c.default is None:
                continue
            defaults[c.name] = float(c.default)
        return defaults

    async def read_output_insert_live_controls(self, output_id: str) -> dict[str, float]:
        """Live read of an output's filter-chain control values from
        PW — gives the Monitoring UI the engine's exact numbers
        (LADSPA output ports are computed by the plugin in real-time)
        instead of approximating in JS. Returns {} when the output
        has no insert or the chain isn't currently loaded."""
        cur = self._output(output_id)
        if cur.insert is None:
            return {}
        chain = chain_name_for(cur)
        try:
            return await self._pw.read_filter_node_controls(chain)
        except Exception:
            logger.info("mixer.read_insert_live_failed", output_id=output_id, exc_info=False)
            return {}

    async def read_master_insert_live_controls(self) -> dict[str, float]:
        """Live read of the master chain's control values — same role
        as read_output_insert_live_controls but reads the single
        phonon_master_fx node. Returns {} when the master chain is
        empty / unloaded."""
        if not self._store.state.master.inserts:
            return {}
        try:
            return await self._pw.read_filter_node_controls(MASTER_CHAIN_NAME)
        except Exception:
            logger.info("mixer.read_master_insert_live_failed", exc_info=False)
            return {}

    async def update_output_insert_control(
        self, output_id: str, control_name: str, value: float, slot: int = 0
    ) -> Output:
        """Live-update one plugin control value. The whole point of
        the filter-chain migration: this path does NOT reload the
        filter-chain service and does NOT reconcile — just pw-cli
        set-param against the running node. With LSP's Ramping=1 on
        comp_delay_stereo, retuning Time (ms) is click-free.

        `slot` selects which plugin in the chain to address (default 0
        keeps v1 single-plugin callers untouched). The DSP panel sends
        dspState.focusedSlot from the UI so editing slot 1's EQ writes
        to slot 1's controls, not slot 0's delay.

        Raises MixerError if the slot is out of range, the slot has
        no plugin, or the control isn't valid for that plugin."""
        cur = self._output(output_id)
        if not (0 <= slot < len(cur.inserts)):
            msg = (
                f"slot {slot} out of range "
                f"(output {output_id} chain has {len(cur.inserts)} plugins)"
            )
            raise MixerError(msg)
        target = cur.inserts[slot]
        if control_name not in target.controls:
            # Auto-heal: if introspection had failed at the time the
            # plugin was first attached, controls is empty (or partial).
            # Reconsult the introspector now — if the control IS valid
            # for this plugin, accept the write and let it land in the
            # dict. The first user move silently rebuilds the missing
            # defaults.
            valid = await self._introspect_control_exists(
                target.backend, target.library, target.label, control_name
            )
            if not valid:
                msg = (
                    f"unknown control {control_name!r} on output {output_id}'s "
                    f"slot {slot} plugin {target.label!r}"
                )
                raise MixerError(msg)
        new_controls = dict(target.controls)
        new_controls[control_name] = float(value)
        new_insert = replace(target, controls=new_controls)
        new_inserts = tuple(new_insert if i == slot else ins for i, ins in enumerate(cur.inserts))
        new = replace(cur, inserts=new_inserts)
        new_outputs = [new if o.id == output_id else o for o in self._store.state.outputs]
        self._store.replace_state(replace(self._store.state, outputs=new_outputs))
        # Keep the in-memory chain-body cache in sync with the new
        # control value — otherwise the next non-control reconcile
        # would see a "wanted vs owned" diff just because of this
        # control move and reload the service for nothing.
        chain = chain_name_for(new)
        if chain in self._owned_chains and new.inserts:
            master_src = _effective_master_source(self._store.state.master)
            self._owned_chains[chain] = render_filter_chain_conf(new, new.inserts, master_src)
        try:
            await self._pw.set_filter_node_control(chain, control_name, value)
        except Exception:
            logger.warning(
                "mixer.insert_control_set_failed",
                output_id=output_id,
                control=control_name,
                exc_info=True,
            )
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

        Click-masking strategy: unloading a pactl module-loopback
        drains its sink-input buffer instantly, producing an audible
        click/strident burst on the destination sink. To suppress it
        we (a) hard-mute the sink via wpctl set-mute (direct PW IPC,
        applies inside one quantum unlike pactl volume which has to
        traverse the PA-compat layer), (b) let the in-flight audio
        in the ALSA buffer flush (~120 ms — covers USB Audio at the
        default quantum + the JBL's own BT receive buffer settle),
        (c) swap the loopback, (d) let the new one prime its
        internal buffer to the target latency, (e) un-mute. Net
        result: a silent dip of ~200 ms on this output during a
        delay change instead of a click. Acceptable: delay-change
        is an engineering action, not a live mix gesture."""
        try:
            modules = await self._pw.list_loopback_modules()
        except Exception:
            return
        master_src = f"source={MASTER_SINK_NAME}.monitor"
        sink_match = f"sink={output.sink_node_name}"
        to_unload = [
            mid for mid, args in modules.items() if master_src in args and sink_match in args
        ]

        # Find the destination sink's PW node id so we can call
        # set_node_mute (wpctl-based, instant). pactl volume changes
        # via PA-compat have inertia we can't predict.
        sink_node_id: int | None = None
        try:
            nodes = await self._pw.list_nodes()
            sn = next((n for n in nodes if n.name == output.sink_node_name), None)
            if sn is not None:
                sink_node_id = sn.id
        except Exception:
            pass

        muted_for_swap = False
        if to_unload and sink_node_id is not None:
            try:
                await self._pw.set_node_mute(sink_node_id, True)
                muted_for_swap = True
                # Pre-swap flush window. The destination ALSA sink
                # may already have ~80-100 ms of audio buffered
                # downstream of the PW mute point; we wait it out
                # so the in-flight audio drains BEFORE the loopback
                # is yanked, otherwise the buffer-drain transient
                # reaches the speakers as a click.
                await asyncio.sleep(0.120)
            except Exception:
                # Best-effort: if mute fails (sink missing or PW
                # transient) proceed anyway — operator hears the
                # click but the delay change still applies.
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

        # Wait for the new loopback to prime its buffer to the target
        # latency before un-muting — otherwise the un-mute lands on
        # an empty/partial sink-input buffer and we'd hear a brief
        # burst as the new latency_msec settles.
        if muted_for_swap and sink_node_id is not None:
            await asyncio.sleep(0.080)
            try:
                await self._pw.set_node_mute(sink_node_id, False)
            except Exception:
                logger.warning(
                    "mixer.swap_unmute_failed",
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

    # ── Sessions (snapshot / restore) ───────────────────────────

    def _require_sessions(self) -> SessionStore:
        if self._sessions is None:
            msg = "session store not configured on this MixerService"
            raise MixerError(msg)
        return self._sessions

    def list_sessions(self) -> list[SessionMeta]:
        return self._require_sessions().list()

    def get_session(self, session_id: str) -> Session:
        try:
            return self._require_sessions().get(session_id)
        except Exception as exc:
            raise MixerError(str(exc)) from exc

    def save_session_full(self, comment: str = "") -> Session:
        """Snapshot the entire MixerState (sources + master + outputs
        + every FX chain) as a new session. Returns the freshly
        created Session record."""
        store = self._require_sessions()
        return store.save_full(self._store.state.to_dict(), comment)

    def save_session_fx(self, target: str, comment: str = "") -> Session:
        """Snapshot just the inserts on a single chain target.
        `target` = "master" or "output:<id>". Anything else raises."""
        store = self._require_sessions()
        inserts = self._resolve_target_inserts(target)
        body = [i.to_dict() for i in inserts]
        return store.save_fx(target, body, comment)

    async def load_session(self, session_id: str) -> Session:
        """Apply a saved session. Full sessions overwrite the entire
        state; fx-only sessions overwrite only the targeted chain.
        Either path ends in a full_resync so PipeWire follows."""
        session = self.get_session(session_id)
        if session.meta.scope == "full":
            self._apply_full_session(session.payload or {})
        elif session.meta.scope == "fx-only":
            target = session.meta.target or ""
            self._apply_fx_session(target, session.payload or [])
        else:
            msg = f"unknown session scope: {session.meta.scope!r}"
            raise MixerError(msg)
        await self.full_resync()
        return session

    def delete_session(self, session_id: str) -> None:
        try:
            self._require_sessions().delete(session_id)
        except Exception as exc:
            raise MixerError(str(exc)) from exc

    def _resolve_target_inserts(self, target: str) -> tuple[PluginInsert, ...]:
        if target == "master":
            return tuple(self._store.state.master.inserts)
        if target.startswith("output:"):
            output_id = target[len("output:") :]
            return tuple(self._output(output_id).inserts)
        msg = f"unknown session target: {target!r}"
        raise MixerError(msg)

    def _apply_full_session(self, payload: dict[str, object]) -> None:
        try:
            new_state = MixerState.from_dict(payload)
        except Exception as exc:
            msg = f"session payload could not be parsed: {exc}"
            raise MixerError(msg) from exc
        self._store.replace_state(new_state)

    def _apply_fx_session(self, target: str, payload: list[dict[str, object]]) -> None:
        new_inserts = tuple(PluginInsert.from_dict(p) for p in payload)
        state = self._store.state
        if target == "master":
            new_master = replace(state.master, inserts=new_inserts)
            self._store.replace_state(replace(state, master=new_master))
            return
        if target.startswith("output:"):
            output_id = target[len("output:") :]
            cur = self._output(output_id)
            new_output = replace(cur, inserts=new_inserts)
            new_outputs = [
                new_output if o.id == output_id else o for o in state.outputs
            ]
            self._store.replace_state(replace(state, outputs=new_outputs))
            return
        msg = f"unknown session target: {target!r}"
        raise MixerError(msg)

    # ── Reconciliation ──────────────────────────────────────────

    async def _reconcile(self) -> None:
        """Tear down every PW object we own and rebuild from the
        current MixerState.

        Order matters: the filter-chain diff has to fire BEFORE we
        create loopbacks and source→master links. Reloading
        filter-chain.service on this stage cascades into a
        pipewire-pulse re-init that wipes every pactl-loaded module
        (phonon_master null-sink, source plugin null-sinks, every
        loopback). Doing the diff afterwards would mean we'd just
        created loopbacks that the cascade then deletes — leaving
        the live graph silent until the next user action.
        """
        # 1. Tear down what we created on the previous reconcile.
        for owned_mid in self._owned_loopbacks:
            with contextlib.suppress(Exception):
                await self._pw.unload_module(owned_mid)
        for lid in self._owned_links:
            with contextlib.suppress(Exception):
                await self._pw.destroy_link(lid)
        self._owned_loopbacks.clear()
        self._owned_links.clear()

        # 2. Compute the wanted filter-chain set BEFORE we touch
        #    anything else. We need it to decide whether step 4 will
        #    cascade (and therefore whether we have to re-list nodes
        #    afterwards before creating loopbacks/links).
        master = self._store.state.master
        master_src = _effective_master_source(master)
        wanted_chains: dict[str, str] = {}
        if master.inserts:
            wanted_chains[MASTER_CHAIN_NAME] = render_master_filter_chain_conf(
                master.inserts, MASTER_SINK_NAME, MASTER_POST_SINK_NAME
            )
        for o in self._store.state.outputs:
            if o.receives_master and o.inserts and any(i.enabled for i in o.inserts):
                wanted_chains[chain_name_for(o)] = render_filter_chain_conf(
                    o, o.inserts, master_src
                )

        # 3. Apply the chain diff EARLY. The cascade (when it fires)
        #    nukes phonon_master, all loopbacks, and any source-plugin
        #    null-sink that lives as a pactl module — so anything we
        #    rebuild has to wait until after this step.
        chain_diff_fired = wanted_chains != self._owned_chains
        if chain_diff_fired:
            await self._apply_filter_chain_diff(wanted_chains)
            self._owned_chains = wanted_chains
            # Cascade just wiped pactl-side state — clear our in-memory
            # tracking so we don't try to unload phantom module ids on
            # the next reconcile.
            self._owned_loopbacks.clear()
            self._owned_links.clear()
            # Give pipewire-pulse a moment to re-stabilise after the
            # cascade. filter-chain.service restart triggers a re-init
            # that takes ~500-1500 ms on this stage; jumping ahead to
            # load_loopback while pipewire-pulse is still spinning up
            # results in "No such entity" errors. Tests override
            # POST_CHAIN_DIFF_SLEEP_S to 0 because the FakePipeWireBackend
            # doesn't cascade.
            if self.POST_CHAIN_DIFF_SLEEP_S > 0:
                await asyncio.sleep(self.POST_CHAIN_DIFF_SLEEP_S)

        # 4. Now the world is settled: ensure phonon_master exists
        #    (cascade may have killed it) and grab fresh node/port
        #    lists. The cache was invalidated by reload_filter_chain
        #    in Fix 1, so list_nodes() actually re-runs pw-dump here.
        try:
            await self._ensure_master_null_sink()
        except Exception:
            logger.warning("mixer.reconcile_master_recreate_failed", exc_info=True)
            return
        if master.inserts:
            try:
                await self._ensure_master_post_null_sink()
            except Exception:
                logger.warning("mixer.master_post_ensure_failed", exc_info=True)
        else:
            try:
                await self._unload_master_post_null_sink()
            except Exception:
                logger.info("mixer.master_post_unload_skipped", exc_info=False)

        try:
            nodes = await self._pw.list_nodes()
            ports = await self._pw.list_ports()
        except Exception:
            logger.warning("mixer.reconcile_lookup_failed", exc_info=True)
            return
        master_node = next((n for n in nodes if n.name == MASTER_SINK_NAME), None)
        if master_node is None:
            # Poll a few times in case load_module returned before
            # pw-dump caught up (slow CPU / Pi USB-shared Ethernet).
            for attempt in range(20):
                await asyncio.sleep(0.1)
                try:
                    nodes = await self._pw.list_nodes()
                    ports = await self._pw.list_ports()
                except Exception:
                    continue
                master_node = next((n for n in nodes if n.name == MASTER_SINK_NAME), None)
                if master_node is not None:
                    if attempt > 0:
                        logger.info("mixer.reconcile_master_visible", attempts=attempt + 1)
                    break
            if master_node is None:
                logger.warning("mixer.reconcile_skip_no_master", attempted=20)
                return

        # 5. Determine solo state up front. A "solo group" is any
        #    set of strips with solo=True and mute=False — muting a
        #    solo'd strip cancels its solo intent (intuitive: the
        #    operator doesn't want silence everywhere just because
        #    they muted a solo'd strip). When the group is non-empty
        #    on a given side (sources / outputs), every non-member
        #    of that side gets silenced via link suppression.
        src_solo_active = any(s.solo and not s.mute for s in self._store.state.sources)
        out_solo_active = any(o.solo and not o.mute for o in self._store.state.outputs)

        # 6. Master volume (per-channel for L/R mutes) + global mute.
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

        # 7. For each output: per-channel volume on the sink, then the
        #    master→output bridge. The bridge is either a plain pactl
        #    module-loopback (legacy path, latency_msec carries delay
        #    but can't be retuned live) OR a PipeWire filter-chain
        #    (when the output has an enabled PluginInsert — replaces
        #    the loopback entirely so the plugin sits inline). The
        #    chain owns its own buffering; output.delay_ms is ignored
        #    while a chain is in place, the user manages delay via
        #    the plugin's controls instead.
        for o in self._store.state.outputs:
            sink_node = next((n for n in nodes if n.name == o.sink_node_name), None)
            if sink_node is None:
                logger.info(
                    "mixer.output_sink_missing",
                    output_id=o.id,
                    sink_node_name=o.sink_node_name,
                )
                continue
            # Combined silence state for this output — global mute /
            # output mute / solo'd-out. Used to pick the path AND to
            # drive the silencing strategy (channel volume = 0).
            output_silenced = out_solo_active and not o.solo
            silenced = o.mute or self.master.mute or output_silenced
            o_lin = self._db_to_linear(o.gain_db)
            # Channel volume = 0 is the definitive mute on this Stage:
            # `wpctl set-mute` on hardware ALSA sinks returns rc=0 but
            # doesn't actually silence audio reaching the speaker.
            # pactl set-sink-volume <name> 0% 0% always works because
            # it hits the post-mix gain stage — same path mute_left /
            # mute_right already use, just extended to the full mute.
            try:
                await self._pw.set_node_channel_volumes(
                    sink_node.name,
                    [
                        0.0 if (o.mute_left or silenced) else o_lin,
                        0.0 if (o.mute_right or silenced) else o_lin,
                    ],
                )
            except Exception:
                logger.warning("mixer.output_volume_failed", output_id=o.id, exc_info=True)
            # Belt-and-braces: also flag the sink as PW-muted (cheap,
            # silently ignored when wpctl can't enforce it on this
            # hardware — the channel-volume trick above already did
            # the actual silencing).
            try:
                await self._pw.set_node_mute(sink_node.id, silenced)
            except Exception:
                logger.warning("mixer.output_mute_failed", output_id=o.id, exc_info=True)
            if o.receives_master and o.inserts and any(i.enabled for i in o.inserts):
                # Chain already wired by filter-chain (step 3) — no
                # loopback needed, the plugin's input/output streams
                # do the routing.
                continue
            if silenced:
                continue
            if o.receives_master:
                mid = await self._pw.load_loopback(
                    f"{master_src}.monitor",
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

    async def _resync_chain_controls_for_output(self, output: Output) -> None:
        """Push every persisted control on an output's plugin insert
        back into the running chain. Necessary after any reload of
        filter-chain.service — the chain comes back with the plugin's
        LADSPA defaults, losing whatever the user had set. Without
        this, persisted state ("Mode=2", "Wet=0.5"...) and the live
        engine drift apart after every reload.

        Chain bring-up after `systemctl restart filter-chain.service`
        can take 200-1500ms depending on host load — polling for the
        chain's input.<name> node to appear is more reliable than a
        flat sleep, and bails after ~3s rather than hanging."""
        if not output.inserts or not any(i.enabled for i in output.inserts):
            return
        chain = chain_name_for(output)
        candidates = (chain, f"input.{chain}", f"output.{chain}")
        for _attempt in range(15):
            await asyncio.sleep(0.2)
            try:
                nodes = await self._pw.list_nodes()
            except Exception:
                continue
            if any(n.name in candidates for n in nodes):
                break
        else:
            logger.warning("mixer.chain_resync_timeout", output_id=output.id, chain=chain)
            return
        # v1 multi-plugin scope: push back the first slot's controls only.
        # Slots > 0 keep whatever PW loaded them with (their persisted
        # control values made it into the conf at render time anyway,
        # so the steady state matches; only live-edits since the last
        # reload could drift, and the v1 UI doesn't expose live edits
        # for slots > 0 yet).
        first = output.inserts[0]
        if not first.enabled:
            return
        for ctl_name, value in first.controls.items():
            try:
                await self._pw.set_filter_node_control(chain, ctl_name, value)
            except Exception:
                logger.warning(
                    "mixer.chain_resync_control_failed",
                    output_id=output.id,
                    control=ctl_name,
                    exc_info=True,
                )

    async def _resync_all_chain_controls(self) -> None:
        for o in self._store.state.outputs:
            await self._resync_chain_controls_for_output(o)

    async def _apply_filter_chain_diff(self, wanted: dict[str, str]) -> None:
        """Reconcile the on-disk filter-chain confs against `wanted`.
        Writes new/changed confs, deletes stale ones, then reloads
        filter-chain.service exactly once. Only invoked when the
        wanted set actually differs from the previous reconcile.

        Errors on individual writes/deletes are logged and skipped —
        a single bad conf shouldn't stop the rest of the reconcile.
        """
        stale = set(self._owned_chains) - set(wanted)
        for chain in stale:
            try:
                await self._pw.delete_filter_chain_conf(chain)
            except Exception:
                logger.warning("mixer.filter_chain_delete_failed", chain=chain, exc_info=True)
        for chain, body in wanted.items():
            if self._owned_chains.get(chain) == body:
                continue  # unchanged — skip the write
            try:
                await self._pw.write_filter_chain_conf(chain, body)
            except Exception:
                logger.warning("mixer.filter_chain_write_failed", chain=chain, exc_info=True)
        try:
            await self._pw.reload_filter_chain()
            logger.info(
                "mixer.filter_chain_reconciled",
                wanted=len(wanted),
                deleted=len(stale),
            )
        except Exception:
            logger.warning("mixer.filter_chain_reload_failed", exc_info=True)
        # Reload reset every chain to LADSPA defaults; push our
        # persisted control values back so engine ↔ state agree.
        await self._resync_all_chain_controls()

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
