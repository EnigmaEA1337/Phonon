"""Integration tests for mappings API endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import AsyncClient


class TestMappingsApi:
    async def test_list_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/mappings")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_mapping(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/mappings",
            json={
                "source_node_id": 31,
                "source_port_ids": [42, 43],
                "sink_node_id": 30,
                "sink_port_ids": [40, 41],
                "gain_db": -3.0,
                "pan": 0.0,
                "mute": False,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["source_node_id"] == 31
        assert data["gain_db"] == -3.0
        assert len(data["link_ids"]) == 2

    async def test_create_and_list(self, client: AsyncClient) -> None:
        await client.post(
            "/mappings",
            json={
                "source_node_id": 31,
                "source_port_ids": [42],
                "sink_node_id": 30,
                "sink_port_ids": [40],
            },
        )
        resp = await client.get("/mappings")
        assert len(resp.json()) == 1

    async def test_delete_mapping(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/mappings",
            json={
                "source_node_id": 31,
                "source_port_ids": [42],
                "sink_node_id": 30,
                "sink_port_ids": [40],
            },
        )
        mapping_id = create_resp.json()["id"]
        del_resp = await client.delete(f"/mappings/{mapping_id}")
        assert del_resp.status_code == 204

        list_resp = await client.get("/mappings")
        assert list_resp.json() == []

    async def test_update_gain(self, client: AsyncClient) -> None:
        create_resp = await client.post(
            "/mappings",
            json={
                "source_node_id": 31,
                "source_port_ids": [42],
                "sink_node_id": 30,
                "sink_port_ids": [40],
            },
        )
        mapping_id = create_resp.json()["id"]
        patch_resp = await client.patch(f"/mappings/{mapping_id}", json={"gain_db": -12.0})
        assert patch_resp.status_code == 200
        assert patch_resp.json()["gain_db"] == -12.0

    async def test_create_invalid_gain(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/mappings",
            json={
                "source_node_id": 31,
                "source_port_ids": [42],
                "sink_node_id": 30,
                "sink_port_ids": [40],
                "gain_db": 20.0,
            },
        )
        assert resp.status_code == 422  # Pydantic validation

    async def test_pipewire_nodes(self, client: AsyncClient) -> None:
        resp = await client.get("/pipewire/nodes")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_pipewire_ports(self, client: AsyncClient) -> None:
        resp = await client.get("/pipewire/ports")
        assert resp.status_code == 200
        assert len(resp.json()) == 6

    async def test_pipewire_ports_filtered(self, client: AsyncClient) -> None:
        resp = await client.get("/pipewire/ports?node_id=30")
        assert resp.status_code == 200
        assert all(p["node_id"] == 30 for p in resp.json())
