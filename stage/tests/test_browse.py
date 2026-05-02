"""Tests for browse stages endpoint and discovery backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from phonon_stage.discovery.backend import DiscoveredStage
from phonon_stage.discovery.fake import FakeDiscoveryBackend

if TYPE_CHECKING:
    from httpx import AsyncClient


class TestFakeDiscoveryBrowse:
    async def test_browse_empty(self) -> None:
        backend = FakeDiscoveryBackend()
        assert await backend.browse() == []
        assert "browse" in backend.call_log

    async def test_browse_with_results(self) -> None:
        backend = FakeDiscoveryBackend(
            browse_results=[
                DiscoveredStage(
                    stage_id="stage-other01",
                    host="10.100.0.20",
                    port=8401,
                    version="0.1.0",
                    mode="STANDALONE",
                ),
            ]
        )
        results = await backend.browse()
        assert len(results) == 1
        assert results[0].stage_id == "stage-other01"


class TestBrowseApi:
    async def test_browse_endpoint(
        self, client: AsyncClient, fake_discovery: FakeDiscoveryBackend
    ) -> None:
        fake_discovery.browse_results = [
            DiscoveredStage(
                stage_id="stage-peer01",
                host="10.100.0.50",
                port=8401,
                version="0.1.0",
                mode="STANDALONE",
            ),
        ]
        resp = await client.get("/browse/stages?timeout=0.1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["stage_id"] == "stage-peer01"

    async def test_browse_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/browse/stages?timeout=0.1")
        assert resp.status_code == 200
        assert resp.json() == []
