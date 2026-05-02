"""Integration tests for GET /health endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from .conftest import EXPECTED_STAGE_ID_SUFFIX

if TYPE_CHECKING:
    from httpx import AsyncClient


class TestHealthEndpoint:
    async def test_health_returns_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["stage_id"] == f"stage-{EXPECTED_STAGE_ID_SUFFIX}"

    async def test_health_uptime_increases(self, client: AsyncClient, fake_clock: object) -> None:
        from phonon_stage.clock import FakeClock

        assert isinstance(fake_clock, FakeClock)

        resp1 = await client.get("/health")
        uptime1 = resp1.json()["uptime_seconds"]

        fake_clock.advance(30.0)

        resp2 = await client.get("/health")
        uptime2 = resp2.json()["uptime_seconds"]

        assert uptime2 > uptime1
        assert uptime2 - uptime1 == pytest.approx(30.0, abs=0.5)

    async def test_health_response_has_exact_keys(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        data = resp.json()
        assert set(data.keys()) == {"status", "uptime_seconds", "stage_id"}
