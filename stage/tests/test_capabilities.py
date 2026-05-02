"""Integration tests for GET /capabilities endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from phonon_stage.audio.fake import FakeAudioBackend
from phonon_stage.bluetooth.fake import FakeBluetoothBackend
from phonon_stage.clock import FakeClock
from phonon_stage.discovery.fake import FakeDiscoveryBackend
from phonon_stage.main import create_app

from .conftest import EXPECTED_STAGE_ID_SUFFIX

if TYPE_CHECKING:
    from phonon_stage.config import StageConfig


class TestCapabilitiesEndpoint:
    async def test_lists_audio_devices(self, client: AsyncClient) -> None:
        resp = await client.get("/capabilities")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["audio_devices"]) == 2
        ids = [d["id"] for d in data["audio_devices"]]
        assert "DG60" in ids
        assert "bcm2835ALSA" in ids

    async def test_lists_bt_controllers(self, client: AsyncClient) -> None:
        resp = await client.get("/capabilities")
        data = resp.json()
        assert len(data["bluetooth_controllers"]) == 2
        addresses = [c["address"] for c in data["bluetooth_controllers"]]
        assert "B8:27:EB:92:29:E3" in addresses

    async def test_includes_stage_id_and_mode(self, client: AsyncClient) -> None:
        resp = await client.get("/capabilities")
        data = resp.json()
        assert data["stage_id"] == f"stage-{EXPECTED_STAGE_ID_SUFFIX}"
        assert data["mode"] == "STANDALONE"

    async def test_empty_when_no_hardware(self, stage_config: StageConfig) -> None:
        """With empty backends, capabilities returns empty lists (not errors)."""
        app = create_app(
            config=stage_config,
            audio_backend=FakeAudioBackend(devices=[]),
            bt_backend=FakeBluetoothBackend(controllers=[]),
            discovery_backend=FakeDiscoveryBackend(),
            clock=FakeClock(),
        )
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                resp = await ac.get("/capabilities")
                assert resp.status_code == 200
                data = resp.json()
                assert data["audio_devices"] == []
                assert data["bluetooth_controllers"] == []
