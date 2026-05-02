"""Integration tests for Bluetooth API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import AsyncClient

    from phonon_stage.bluetooth.fake import FakeBluetoothBackend


class TestBluetoothApi:
    async def test_power_on(self, client: AsyncClient, fake_bt: FakeBluetoothBackend) -> None:
        resp = await client.post(
            "/bluetooth/00:01:95:4B:40:46/power",
            json={"powered": True},
        )
        assert resp.status_code == 200
        assert "power:00:01:95:4B:40:46:True" in fake_bt.call_log

    async def test_scan(self, client: AsyncClient) -> None:
        resp = await client.get("/bluetooth/scan?controller_address=00:01:95:4B:40:46&timeout=0.1")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_pair(self, client: AsyncClient, fake_bt: FakeBluetoothBackend) -> None:
        resp = await client.post("/bluetooth/pair", json={"device_address": "AA:BB:CC:DD:EE:FF"})
        assert resp.status_code == 200
        assert "pair:AA:BB:CC:DD:EE:FF" in fake_bt.call_log

    async def test_connect(self, client: AsyncClient, fake_bt: FakeBluetoothBackend) -> None:
        resp = await client.post(
            "/bluetooth/connect", json={"device_address": "AA:BB:CC:DD:EE:FF"}
        )
        assert resp.status_code == 200
        assert "connect:AA:BB:CC:DD:EE:FF" in fake_bt.call_log

    async def test_disconnect(self, client: AsyncClient, fake_bt: FakeBluetoothBackend) -> None:
        resp = await client.request(
            "DELETE", "/bluetooth/disconnect", json={"device_address": "AA:BB:CC:DD:EE:FF"}
        )
        assert resp.status_code == 200
        assert "disconnect:AA:BB:CC:DD:EE:FF" in fake_bt.call_log
