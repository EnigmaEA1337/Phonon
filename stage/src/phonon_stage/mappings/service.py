"""Mapping service — orchestrates PipeWire routing and persistence."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from phonon_stage.mappings.audio_math import db_to_linear, pan_to_stereo_gains, validate_gain
from phonon_stage.mappings.models import Mapping

if TYPE_CHECKING:
    from phonon_stage.clock import Clock
    from phonon_stage.mappings.store import MappingStore
    from phonon_stage.pipewire.backend import PipeWireBackend

logger = structlog.get_logger()


class MappingServiceError(Exception):
    """Raised on mapping operation failures."""


class MappingService:
    """Orchestrates audio routing: creates PipeWire links and persists mappings."""

    def __init__(
        self,
        pw_backend: PipeWireBackend,
        store: MappingStore,
        clock: Clock,
    ) -> None:
        self._pw = pw_backend
        self._store = store
        self._clock = clock

    @property
    def mappings(self) -> list[Mapping]:
        return self._store.mappings

    async def create_mapping(
        self,
        source_node_id: int,
        source_port_ids: list[int],
        sink_node_id: int,
        sink_port_ids: list[int],
        gain_db: float = 0.0,
        pan: float = 0.0,
        mute: bool = False,
        delay_ms: float = 0.0,
    ) -> Mapping:
        """Create a new audio mapping.

        Routing depends on delay_ms:
          * 0 ms  : direct PipeWire pw-link from source ports → sink ports
                    (zero-latency, the original path)
          * >0 ms : load a `module-loopback` with `latency_msec=delay_ms`
                    between source.<monitor> and sink. The loopback
                    buffers the audio for the requested ms → real
                    perceptible delay, as opposed to the
                    `latencyOffsetNsec` scheduling hint which doesn't
                    add buffering.
        """
        validate_gain(gain_db)

        mapping_id = uuid.uuid4().hex[:8]
        link_ids: list[int] = []
        loopback_id: int | None = None

        # Capture node names so we can re-resolve after a PW restart, AND
        # determine if the source is a sink (we'll need .monitor) or a
        # real Audio/Source (direct name).
        nodes = await self._pw.list_nodes()
        node_by_id = {n.id: n for n in nodes}
        src_node = node_by_id.get(source_node_id)
        sink_node = node_by_id.get(sink_node_id)
        src_name = src_node.name if src_node else ""
        sink_name = sink_node.name if sink_node else ""
        src_is_sink = bool(src_node and "Sink" in src_node.media_class)

        if not mute:
            if delay_ms > 0 and src_name and sink_name:
                loopback_id = await self._load_loopback_for(
                    src_name, sink_name, src_is_sink, int(delay_ms)
                )
            else:
                link_ids = await self._create_links(source_port_ids, sink_port_ids)

        # Apply volume on the sink. With a loopback in the path, the
        # sink volume still controls the final loudness.
        if not mute:
            volume = db_to_linear(gain_db)
            await self._apply_volume(sink_node_id, volume, pan)

        mapping = Mapping(
            id=mapping_id,
            source_node_id=source_node_id,
            source_port_ids=source_port_ids,
            sink_node_id=sink_node_id,
            sink_port_ids=sink_port_ids,
            link_ids=link_ids,
            gain_db=gain_db,
            pan=pan,
            mute=mute,
            delay_ms=delay_ms,
            loopback_module_id=loopback_id,
            created_at=self._clock.now().isoformat(),
            source_node_name=src_name,
            sink_node_name=sink_name,
        )

        self._store.add(mapping)
        logger.info(
            "mapping.created",
            mapping_id=mapping_id,
            source=source_node_id,
            sink=sink_node_id,
            delay_ms=delay_ms,
            via="loopback" if loopback_id is not None else "pw-link",
        )
        return mapping

    async def _load_loopback_for(
        self, src_name: str, sink_name: str, src_is_sink: bool, latency_msec: int
    ) -> int | None:
        """Build the PA-style names and call the backend. Encapsulates the
        `bt_X_in` (null-sink) vs `alsa_input.X` (real source) decision."""
        # For null-sinks the readable side is the monitor port; for real
        # capture sources (alsa_input.X, BT a2dp-sink exposed as source)
        # the node name itself reads directly.
        pa_source = f"{src_name}.monitor" if src_is_sink else src_name
        return await self._pw.load_loopback(pa_source, sink_name, latency_msec)

    async def delete_mapping(self, mapping_id: str) -> None:
        """Delete a mapping. Destroys whichever transport it was using —
        direct pw-links (delay=0) or the module-loopback (delay>0)."""
        mapping = self._store.get(mapping_id)
        if mapping is None:
            msg = f"Mapping {mapping_id} not found"
            raise MappingServiceError(msg)

        for link_id in mapping.link_ids:
            try:
                await self._pw.destroy_link(link_id)
            except Exception:
                logger.warning("mapping.link_destroy_failed", link_id=link_id, exc_info=True)

        if mapping.loopback_module_id is not None:
            try:
                await self._pw.unload_module(mapping.loopback_module_id)
            except Exception:
                logger.warning(
                    "mapping.loopback_unload_failed",
                    module_id=mapping.loopback_module_id,
                    exc_info=True,
                )

        self._store.remove(mapping_id)
        logger.info("mapping.deleted", mapping_id=mapping_id)

    async def update_mapping(
        self,
        mapping_id: str,
        gain_db: float | None = None,
        pan: float | None = None,
        mute: bool | None = None,
        delay_ms: float | None = None,
    ) -> Mapping:
        """Update gain/pan/mute on an existing mapping."""
        mapping = self._store.get(mapping_id)
        if mapping is None:
            msg = f"Mapping {mapping_id} not found"
            raise MappingServiceError(msg)

        updates: dict[str, object] = {}

        if gain_db is not None:
            validate_gain(gain_db)
            updates["gain_db"] = gain_db

        if pan is not None:
            updates["pan"] = pan

        if mute is not None and mute != mapping.mute:
            if mute:
                # Muting: destroy links
                for link_id in mapping.link_ids:
                    try:
                        await self._pw.destroy_link(link_id)
                    except Exception:
                        logger.warning("mapping.mute_destroy_failed", link_id=link_id)
                updates["link_ids"] = []
                updates["mute"] = True
            else:
                # Unmuting: recreate links
                new_link_ids = await self._create_links(
                    mapping.source_port_ids, mapping.sink_port_ids
                )
                updates["link_ids"] = new_link_ids
                updates["mute"] = False

        updated = self._store.update(mapping_id, **updates)

        # Apply volume if gain or pan changed and not muted
        if not updated.mute and (gain_db is not None or pan is not None):
            volume = db_to_linear(updated.gain_db)
            await self._apply_volume(updated.sink_node_id, volume, updated.pan)

        if delay_ms is not None and delay_ms != mapping.delay_ms:
            # Switch transport when crossing 0 ↔ >0, or just reload the
            # loopback with the new latency when staying >0.
            had_delay = mapping.delay_ms > 0
            wants_delay = delay_ms > 0

            if had_delay and updated.loopback_module_id is not None:
                # Tear down the current loopback before anything else.
                try:
                    await self._pw.unload_module(updated.loopback_module_id)
                except Exception:
                    logger.warning(
                        "mapping.update_loopback_unload_failed",
                        module_id=updated.loopback_module_id,
                        exc_info=True,
                    )

            new_loopback_id: int | None = None
            new_link_ids = list(updated.link_ids)

            if not updated.mute:
                if wants_delay:
                    # Make sure any direct pw-links are gone before adding
                    # the loopback path (otherwise audio plays twice — one
                    # direct, one delayed → comb filter).
                    if updated.link_ids:
                        for link_id in updated.link_ids:
                            try:
                                await self._pw.destroy_link(link_id)
                            except Exception:
                                logger.warning(
                                    "mapping.update_link_destroy_failed",
                                    link_id=link_id,
                                    exc_info=True,
                                )
                        new_link_ids = []
                    # Spawn the loopback with the new latency.
                    nodes = await self._pw.list_nodes()
                    src = next(
                        (n for n in nodes if n.id == updated.source_node_id),
                        None,
                    )
                    src_is_sink = bool(src and "Sink" in src.media_class)
                    new_loopback_id = await self._load_loopback_for(
                        updated.source_node_name,
                        updated.sink_node_name,
                        src_is_sink,
                        int(delay_ms),
                    )
                elif had_delay:
                    # Going from delayed → instant: recreate the direct
                    # pw-links since the loopback handled routing before.
                    new_link_ids = await self._create_links(
                        updated.source_port_ids, updated.sink_port_ids
                    )

            updated = self._store.update(
                mapping_id,
                delay_ms=delay_ms,
                loopback_module_id=new_loopback_id,
                link_ids=new_link_ids,
            )
            updates["delay_ms"] = delay_ms

        logger.info("mapping.updated", mapping_id=mapping_id, updates=list(updates.keys()))
        return updated

    async def restore_mappings(self) -> None:
        """Restore mappings from persistence on startup. Resolves nodes by
        NAME (not stale IDs from before the last PW restart) so links land
        on the right ports even if PipeWire has renumbered everything.

        For delayed mappings, the previous-run loopback module is gone
        (pactl modules don't persist across daemon restarts), so we
        recreate it. The stale `loopback_module_id` from disk is
        replaced by the new pactl module id."""
        mappings = self._store.load()
        restored = 0
        skipped = 0
        for mapping in mappings:
            if mapping.mute:
                restored += 1
                continue
            try:
                new_ids = await self._reresolve_mapping_ports(mapping)
                if new_ids is None:
                    skipped += 1
                    continue
                src_node_id, src_port_ids, sink_node_id, sink_port_ids = new_ids
                # Resolve src_is_sink from the fresh node list
                nodes = await self._pw.list_nodes()
                src = next((n for n in nodes if n.id == src_node_id), None)
                src_is_sink = bool(src and "Sink" in src.media_class)

                new_link_ids: list[int] = []
                new_loopback_id: int | None = None
                if mapping.delay_ms > 0 and mapping.source_node_name and mapping.sink_node_name:
                    new_loopback_id = await self._load_loopback_for(
                        mapping.source_node_name,
                        mapping.sink_node_name,
                        src_is_sink,
                        int(mapping.delay_ms),
                    )
                else:
                    new_link_ids = await self._create_links(src_port_ids, sink_port_ids)
                self._store.update(
                    mapping.id,
                    source_node_id=src_node_id,
                    source_port_ids=src_port_ids,
                    sink_node_id=sink_node_id,
                    sink_port_ids=sink_port_ids,
                    link_ids=new_link_ids,
                    loopback_module_id=new_loopback_id,
                )
                volume = db_to_linear(mapping.gain_db)
                await self._apply_volume(sink_node_id, volume, mapping.pan)
                restored += 1
            except Exception:
                logger.warning("mapping.restore_failed", mapping_id=mapping.id, exc_info=True)
                skipped += 1

        logger.info("mappings.restored", total=len(mappings), restored=restored, skipped=skipped)

    async def resync_mappings(self) -> dict[str, int]:
        """User-triggered resync — call after a PW restart or BT reconnect
        to recreate every persisted mapping using the current node IDs.

        Same logic as restore_mappings for the routing choice: delay>0
        means recreate the loopback (the previous one's pactl module is
        gone after a PW user-session restart), delay=0 means direct
        pw-links."""
        mappings = self._store.mappings
        ok = 0
        skipped = 0
        for mapping in mappings:
            if mapping.mute:
                continue
            try:
                new_ids = await self._reresolve_mapping_ports(mapping)
                if new_ids is None:
                    skipped += 1
                    continue
                src_node_id, src_port_ids, sink_node_id, sink_port_ids = new_ids
                nodes = await self._pw.list_nodes()
                src = next((n for n in nodes if n.id == src_node_id), None)
                src_is_sink = bool(src and "Sink" in src.media_class)

                new_link_ids: list[int] = []
                new_loopback_id: int | None = None
                if mapping.delay_ms > 0 and mapping.source_node_name and mapping.sink_node_name:
                    new_loopback_id = await self._load_loopback_for(
                        mapping.source_node_name,
                        mapping.sink_node_name,
                        src_is_sink,
                        int(mapping.delay_ms),
                    )
                else:
                    new_link_ids = await self._create_links(src_port_ids, sink_port_ids)
                self._store.update(
                    mapping.id,
                    source_node_id=src_node_id,
                    source_port_ids=src_port_ids,
                    sink_node_id=sink_node_id,
                    sink_port_ids=sink_port_ids,
                    link_ids=new_link_ids,
                    loopback_module_id=new_loopback_id,
                )
                volume = db_to_linear(mapping.gain_db)
                await self._apply_volume(sink_node_id, volume, mapping.pan)
                ok += 1
            except Exception:
                logger.warning("mapping.resync_failed", mapping_id=mapping.id, exc_info=True)
                skipped += 1
        logger.info("mappings.resynced", total=len(mappings), ok=ok, skipped=skipped)
        return {"total": len(mappings), "ok": ok, "skipped": skipped}

    async def _reresolve_mapping_ports(
        self, mapping: Mapping
    ) -> tuple[int, list[int], int, list[int]] | None:
        """Look up current node IDs by name, then current ports. Returns
        None if either node has disappeared (e.g., BT bridge not synced)."""
        if not mapping.source_node_name or not mapping.sink_node_name:
            # Legacy mapping created before name capture — try with the
            # stored IDs and let the link create fail if they're stale.
            return (
                mapping.source_node_id,
                mapping.source_port_ids,
                mapping.sink_node_id,
                mapping.sink_port_ids,
            )
        nodes = await self._pw.list_nodes()
        by_name = {n.name: n for n in nodes}
        src = by_name.get(mapping.source_node_name)
        sink = by_name.get(mapping.sink_node_name)
        if not src or not sink:
            logger.info(
                "mapping.resync_node_missing",
                mapping_id=mapping.id,
                source=mapping.source_node_name,
                sink=mapping.sink_node_name,
                source_found=src is not None,
                sink_found=sink is not None,
            )
            return None
        ports = await self._pw.list_ports()
        src_outs = sorted(p.id for p in ports if p.node_id == src.id and p.direction == "output")
        sink_ins = sorted(p.id for p in ports if p.node_id == sink.id and p.direction == "input")
        if not src_outs or not sink_ins:
            return None
        return (src.id, src_outs, sink.id, sink_ins)

    async def _create_links(
        self, source_port_ids: list[int], sink_port_ids: list[int]
    ) -> list[int]:
        """Create PipeWire links for each source→sink port pair."""
        link_ids: list[int] = []
        pairs = zip(source_port_ids, sink_port_ids, strict=False)
        for out_id, in_id in pairs:
            link = await self._pw.create_link(out_id, in_id)
            link_ids.append(link.id)
        return link_ids

    async def _apply_volume(self, node_id: int, volume: float, pan: float) -> None:
        """Apply volume and pan to a sink node, and ensure it's unmuted.

        WirePlumber starts USB sinks (e.g. a freshly-plugged Avantree
        DG60 receiver) in MUTED state by default — that left users
        with a perfectly-routed mapping that produced zero sound and
        no obvious culprit. Setting volume here without clearing the
        mute flag would silently fail. So we always unmute on every
        mapping mutation: create, update, restore, resync.
        """
        _left_gain, _right_gain = pan_to_stereo_gains(pan)
        await self._pw.set_node_volume(node_id, volume)
        await self._pw.set_node_mute(node_id, False)
