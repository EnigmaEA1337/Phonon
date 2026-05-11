"""Tests for /mixer REST endpoints — full CRUD + validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import AsyncClient


class TestSnapshot:
    async def test_initial_snapshot_is_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/mixer")
        assert resp.status_code == 200
        body = resp.json()
        assert body["master"] == {
            "gain_db": 0.0,
            "mute": False,
            "mute_left": False,
            "mute_right": False,
        }
        assert body["outputs"] == []
        assert body["sources"] == []


class TestMaster:
    async def test_patch_master_gain_and_mute(self, client: AsyncClient) -> None:
        resp = await client.patch("/mixer/master", json={"gain_db": -6.0, "mute": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["gain_db"] == -6.0
        assert body["mute"] is True

    async def test_patch_master_rejects_out_of_range(self, client: AsyncClient) -> None:
        resp = await client.patch("/mixer/master", json={"gain_db": 99.0})
        assert resp.status_code == 422

    async def test_patch_master_rejects_unknown_field(self, client: AsyncClient) -> None:
        resp = await client.patch("/mixer/master", json={"foo": 1})
        assert resp.status_code == 422


class TestOutputs:
    async def test_create_and_list(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/mixer/outputs",
            json={
                "sink_node_name": "alsa_output.dg60_1",
                "label": "Pulse 3",
                "delay_ms": 115.0,
            },
        )
        assert resp.status_code == 201
        o = resp.json()
        assert o["label"] == "Pulse 3"
        assert o["delay_ms"] == 115.0
        assert o["receives_master"] is True

        snap = (await client.get("/mixer")).json()
        assert len(snap["outputs"]) == 1
        assert snap["outputs"][0]["id"] == o["id"]

    async def test_patch_output(self, client: AsyncClient) -> None:
        o = (
            await client.post(
                "/mixer/outputs",
                json={"sink_node_name": "alsa_output.dg60_1", "label": "A"},
            )
        ).json()
        resp = await client.patch(
            f"/mixer/outputs/{o['id']}",
            json={"delay_ms": 80.0, "mute": True, "label": "Pulse 3"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["delay_ms"] == 80.0
        assert body["mute"] is True
        assert body["label"] == "Pulse 3"

    async def test_delete_output(self, client: AsyncClient) -> None:
        o = (
            await client.post(
                "/mixer/outputs",
                json={"sink_node_name": "alsa_output.dg60_1", "label": "A"},
            )
        ).json()
        resp = await client.delete(f"/mixer/outputs/{o['id']}")
        assert resp.status_code == 204
        snap = (await client.get("/mixer")).json()
        assert snap["outputs"] == []

    async def test_patch_unknown_output_404s(self, client: AsyncClient) -> None:
        resp = await client.patch("/mixer/outputs/nope", json={"label": "X"})
        assert resp.status_code == 404

    async def test_invalid_delay_400s(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/mixer/outputs",
            json={"sink_node_name": "x", "label": "X", "delay_ms": 9999.0},
        )
        assert resp.status_code == 422


class TestSources:
    async def test_create_source_to_master(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/mixer/sources",
            json={
                "source_node_name": "airplay_in",
                "source_is_sink": True,
                "label": "AirPlay",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["to_master"] is True
        assert body["direct_outputs"] == []

    async def test_source_with_direct_output(self, client: AsyncClient) -> None:
        o = (
            await client.post(
                "/mixer/outputs",
                json={"sink_node_name": "alsa_output.dg60_1", "label": "A"},
            )
        ).json()
        resp = await client.post(
            "/mixer/sources",
            json={
                "source_node_name": "airplay_in",
                "source_is_sink": True,
                "label": "AP",
                "to_master": False,
                "direct_outputs": [o["id"]],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["direct_outputs"] == [o["id"]]

    async def test_source_with_bad_direct_output_409s(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/mixer/sources",
            json={
                "source_node_name": "airplay_in",
                "source_is_sink": True,
                "label": "AP",
                "direct_outputs": ["does-not-exist"],
            },
        )
        assert resp.status_code == 409

    async def test_patch_source_mute(self, client: AsyncClient) -> None:
        s = (
            await client.post(
                "/mixer/sources",
                json={
                    "source_node_name": "airplay_in",
                    "source_is_sink": True,
                    "label": "AP",
                },
            )
        ).json()
        resp = await client.patch(f"/mixer/sources/{s['id']}", json={"mute": True})
        assert resp.status_code == 200
        assert resp.json()["mute"] is True

    async def test_patch_unknown_source_404s(self, client: AsyncClient) -> None:
        resp = await client.patch("/mixer/sources/nope", json={"mute": True})
        assert resp.status_code == 404

    async def test_delete_source(self, client: AsyncClient) -> None:
        s = (
            await client.post(
                "/mixer/sources",
                json={
                    "source_node_name": "airplay_in",
                    "source_is_sink": True,
                    "label": "AP",
                },
            )
        ).json()
        resp = await client.delete(f"/mixer/sources/{s['id']}")
        assert resp.status_code == 204
        snap = (await client.get("/mixer")).json()
        assert snap["sources"] == []


class TestProductionScenario:
    async def test_2dg60_2sources_via_master(self, client: AsyncClient) -> None:
        """End-to-end: stage the user's actual layout via the REST API
        and verify the snapshot matches."""
        dg1 = (
            await client.post(
                "/mixer/outputs",
                json={
                    "sink_node_name": "alsa_output.dg60_1",
                    "label": "Pulse 3",
                    "delay_ms": 115.0,
                },
            )
        ).json()
        dg2 = (
            await client.post(
                "/mixer/outputs",
                json={
                    "sink_node_name": "alsa_output.dg60_2",
                    "label": "Xtreme 4",
                    "delay_ms": 0.0,
                },
            )
        ).json()
        await client.post(
            "/mixer/sources",
            json={
                "source_node_name": "airplay_in",
                "source_is_sink": True,
                "label": "AirPlay",
            },
        )
        await client.post(
            "/mixer/sources",
            json={
                "source_node_name": "bt_phone_in",
                "source_is_sink": True,
                "label": "BT Phone",
            },
        )
        snap = (await client.get("/mixer")).json()
        assert len(snap["outputs"]) == 2
        assert len(snap["sources"]) == 2
        # Each source goes via master, no direct routing.
        for s in snap["sources"]:
            assert s["to_master"] is True
            assert s["direct_outputs"] == []
        # The delay-per-output config is preserved.
        labels_to_delays = {o["label"]: o["delay_ms"] for o in snap["outputs"]}
        assert labels_to_delays == {"Pulse 3": 115.0, "Xtreme 4": 0.0}
        # Quiet unused-var.
        assert dg1["id"] != dg2["id"]
