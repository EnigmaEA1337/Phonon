"""Tests for MappingStore — JSON persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.mappings.models import Mapping
from phonon_stage.mappings.store import MappingStore, MappingStoreError

if TYPE_CHECKING:
    from pathlib import Path


class TestMappingStore:
    def test_load_missing_file(self, tmp_path: Path) -> None:
        store = MappingStore(tmp_path / "missing.json")
        mappings = store.load()
        assert mappings == []

    def test_add_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "store.json"
        store = MappingStore(path)
        m = Mapping(
            id="abc12345",
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
        )
        store.add(m)

        store2 = MappingStore(path)
        loaded = store2.load()
        assert len(loaded) == 1
        assert loaded[0].id == "abc12345"

    def test_remove(self, tmp_path: Path) -> None:
        path = tmp_path / "store.json"
        store = MappingStore(path)
        m = Mapping(
            id="abc12345",
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
        )
        store.add(m)
        store.remove("abc12345")
        assert len(store.mappings) == 0

    def test_update(self, tmp_path: Path) -> None:
        path = tmp_path / "store.json"
        store = MappingStore(path)
        m = Mapping(
            id="abc12345",
            source_node_id=31,
            source_port_ids=[42],
            sink_node_id=30,
            sink_port_ids=[40],
            gain_db=0.0,
        )
        store.add(m)
        updated = store.update("abc12345", gain_db=-6.0)
        assert updated.gain_db == -6.0

    def test_max_mappings(self, tmp_path: Path) -> None:
        path = tmp_path / "store.json"
        store = MappingStore(path)
        for i in range(8):
            store.add(
                Mapping(
                    id=f"m{i}",
                    source_node_id=31,
                    source_port_ids=[42],
                    sink_node_id=30,
                    sink_port_ids=[40],
                )
            )
        with pytest.raises(MappingStoreError, match="Maximum"):
            store.add(
                Mapping(
                    id="m8",
                    source_node_id=31,
                    source_port_ids=[42],
                    sink_node_id=30,
                    sink_port_ids=[40],
                )
            )

    def test_update_not_found(self, tmp_path: Path) -> None:
        store = MappingStore(tmp_path / "store.json")
        with pytest.raises(MappingStoreError, match="not found"):
            store.update("nonexistent", gain_db=-6.0)

    def test_corrupted_file(self, tmp_path: Path) -> None:
        path = tmp_path / "store.json"
        path.write_text("not json at all")
        store = MappingStore(path)
        assert store.load() == []
