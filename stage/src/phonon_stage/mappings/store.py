"""Mapping persistence — JSON file storage for standalone mode."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

import structlog

from phonon_stage.mappings.models import MAX_MAPPINGS, Mapping

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()


class MappingStoreError(Exception):
    """Raised on persistence failures."""


class MappingStore:
    """Read/write mappings to standalone.conf.json."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mappings: list[Mapping] = []

    @property
    def mappings(self) -> list[Mapping]:
        return list(self._mappings)

    def load(self) -> list[Mapping]:
        """Load mappings from disk. Returns empty list if file absent."""
        if not self._path.exists():
            self._mappings = []
            return self._mappings

        try:
            raw = json.loads(self._path.read_text())
            mapping_list = raw.get("mappings", [])
            self._mappings = [Mapping.from_dict(m) for m in mapping_list]
            logger.info("mappings.loaded", count=len(self._mappings), path=str(self._path))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("mappings.load_failed", path=str(self._path), error=str(exc))
            self._mappings = []

        return list(self._mappings)

    def save(self) -> None:
        """Write current mappings to disk (mode 0600)."""
        data = {"mappings": [m.to_dict() for m in self._mappings]}
        self._path.write_text(json.dumps(data, indent=2))
        with contextlib.suppress(OSError):
            self._path.chmod(0o600)
        logger.info("mappings.saved", count=len(self._mappings), path=str(self._path))

    def add(self, mapping: Mapping) -> None:
        """Add a mapping and persist. Raises if at MAX_MAPPINGS."""
        if len(self._mappings) >= MAX_MAPPINGS:
            msg = f"Maximum {MAX_MAPPINGS} mappings reached"
            raise MappingStoreError(msg)
        self._mappings.append(mapping)
        self.save()

    def remove(self, mapping_id: str) -> None:
        """Remove a mapping by ID and persist."""
        self._mappings = [m for m in self._mappings if m.id != mapping_id]
        self.save()

    def update(self, mapping_id: str, **kwargs: object) -> Mapping:
        """Partial update of a mapping. Persists after update."""
        for mapping in self._mappings:
            if mapping.id == mapping_id:
                for key, value in kwargs.items():
                    if hasattr(mapping, key):
                        setattr(mapping, key, value)
                self.save()
                return mapping
        msg = f"Mapping {mapping_id} not found"
        raise MappingStoreError(msg)

    def get(self, mapping_id: str) -> Mapping | None:
        """Get a mapping by ID."""
        for mapping in self._mappings:
            if mapping.id == mapping_id:
                return mapping
        return None
