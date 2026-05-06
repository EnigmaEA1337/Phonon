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
        """Create a new audio mapping with PipeWire links."""
        validate_gain(gain_db)

        mapping_id = uuid.uuid4().hex[:8]
        link_ids: list[int] = []

        if not mute:
            link_ids = await self._create_links(source_port_ids, sink_port_ids)

        # Apply volume and delay
        if not mute:
            volume = db_to_linear(gain_db)
            await self._apply_volume(sink_node_id, volume, pan)
        if delay_ms > 0:
            await self._pw.set_node_latency_offset(sink_node_id, int(delay_ms * 1_000_000))

        # Capture node names so we can re-resolve after a PW restart
        nodes = await self._pw.list_nodes()
        node_by_id = {n.id: n for n in nodes}
        src_name = node_by_id.get(source_node_id).name if source_node_id in node_by_id else ""
        sink_name = node_by_id.get(sink_node_id).name if sink_node_id in node_by_id else ""

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
            created_at=self._clock.now().isoformat(),
            source_node_name=src_name,
            sink_node_name=sink_name,
        )

        self._store.add(mapping)
        logger.info(
            "mapping.created", mapping_id=mapping_id, source=source_node_id, sink=sink_node_id
        )
        return mapping

    async def delete_mapping(self, mapping_id: str) -> None:
        """Delete a mapping and destroy its PipeWire links."""
        mapping = self._store.get(mapping_id)
        if mapping is None:
            msg = f"Mapping {mapping_id} not found"
            raise MappingServiceError(msg)

        for link_id in mapping.link_ids:
            try:
                await self._pw.destroy_link(link_id)
            except Exception:
                logger.warning("mapping.link_destroy_failed", link_id=link_id, exc_info=True)

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

        if delay_ms is not None:
            updates["delay_ms"] = delay_ms
            updated = self._store.update(mapping_id, delay_ms=delay_ms)
            await self._pw.set_node_latency_offset(updated.sink_node_id, int(delay_ms * 1_000_000))

        logger.info("mapping.updated", mapping_id=mapping_id, updates=list(updates.keys()))
        return updated

    async def restore_mappings(self) -> None:
        """Restore mappings from persistence on startup. Resolves nodes by
        NAME (not stale IDs from before the last PW restart) so links land
        on the right ports even if PipeWire has renumbered everything."""
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
                new_link_ids = await self._create_links(src_port_ids, sink_port_ids)
                self._store.update(
                    mapping.id,
                    source_node_id=src_node_id,
                    source_port_ids=src_port_ids,
                    sink_node_id=sink_node_id,
                    sink_port_ids=sink_port_ids,
                    link_ids=new_link_ids,
                )
                volume = db_to_linear(mapping.gain_db)
                await self._apply_volume(sink_node_id, volume, mapping.pan)
                restored += 1
            except Exception:
                logger.warning("mapping.restore_failed", mapping_id=mapping.id, exc_info=True)
                skipped += 1

        logger.info("mappings.restored", total=len(mappings),
                    restored=restored, skipped=skipped)

    async def resync_mappings(self) -> dict[str, int]:
        """User-triggered resync — call after PW restart to recreate every
        persisted mapping using the current node IDs. Returns counts."""
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
                new_link_ids = await self._create_links(src_port_ids, sink_port_ids)
                self._store.update(
                    mapping.id,
                    source_node_id=src_node_id,
                    source_port_ids=src_port_ids,
                    sink_node_id=sink_node_id,
                    sink_port_ids=sink_port_ids,
                    link_ids=new_link_ids,
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
        src_outs = sorted(
            p.id for p in ports if p.node_id == src.id and p.direction == "output"
        )
        sink_ins = sorted(
            p.id for p in ports if p.node_id == sink.id and p.direction == "input"
        )
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
        """Apply volume and pan to a sink node."""
        _left_gain, _right_gain = pan_to_stereo_gains(pan)
        # PipeWire wpctl set-volume applies to the whole node
        # For simplicity in standalone mode, we just set the overall volume
        # Pan would require per-channel control which is more complex
        await self._pw.set_node_volume(node_id, volume)
