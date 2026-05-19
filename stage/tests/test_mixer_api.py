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
        # Master gains an `insert` + `inserts` field once we added the
        # master FX chain — same shape as the Output response.
        assert body["master"] == {
            "gain_db": 0.0,
            "mute": False,
            "mute_left": False,
            "mute_right": False,
            "insert": None,
            "inserts": [],
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


class TestTestTone:
    """The TEST AUDIO button — generated signal injected into the master
    so the operator can audibly verify the Master → outputs chain."""

    async def test_rejects_unknown_kind(self, client: AsyncClient) -> None:
        resp = await client.post("/mixer/admin/test-tone/start?kind=banana")
        assert resp.status_code == 400

    async def test_status_idle(self, client: AsyncClient) -> None:
        resp = await client.get("/mixer/admin/test-tone/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is False
        assert body["kind"] is None

    async def test_stop_is_idempotent_when_idle(self, client: AsyncClient) -> None:
        # Stop while nothing is running should still answer cleanly.
        resp = await client.post("/mixer/admin/test-tone/stop")
        assert resp.status_code == 200
        assert resp.json() == {"status": "stopped"}

    def test_chunk_shape_matches_constant(self) -> None:
        # Chunk math: 1024 frames * stereo * 2 bytes = 4096 bytes.
        from phonon_stage.api.mixer import _CHUNK_BYTES, _generate_chunk

        for kind in ("click", "tone", "pink"):
            buf = _generate_chunk(kind, pos=0)
            assert len(buf) == _CHUNK_BYTES

    def test_chunk_unknown_kind_raises(self) -> None:
        import pytest

        from phonon_stage.api.mixer import _generate_chunk

        with pytest.raises(ValueError, match="unknown kind"):
            _generate_chunk("banana", pos=0)

    def test_tone_phase_continuity_across_chunks(self) -> None:
        # Two consecutive chunks should look like one continuous 440 Hz
        # sine — i.e. the second chunk's first sample is what the first
        # chunk's "next-after-last" sample would have been. Regression
        # guard against the 1 Hz pop that the old looped buffer caused.
        import struct as _struct

        from phonon_stage.api.mixer import _CHUNK_FRAMES, _generate_chunk

        c0 = _generate_chunk("tone", pos=0)
        c1 = _generate_chunk("tone", pos=_CHUNK_FRAMES)
        # Last frame of c0 vs first frame of c1 — they should be close
        # (one sample period of a 440 Hz sine is ~0.6% of full scale).
        last_c0 = _struct.unpack_from("<hh", c0, (_CHUNK_FRAMES - 1) * 4)[0]
        first_c1 = _struct.unpack_from("<hh", c1, 0)[0]
        assert abs(last_c0 - first_c1) < 1500  # at -10 dBFS, ~5% of 10350


class TestBuses:
    """Bus CRUD over REST + source bus_sends upsert/delete. The
    underlying service is exercised in test_mixer_service.TestBuses;
    here we focus on the HTTP shape (status codes, response keys,
    error mapping)."""

    async def test_create_list_and_snapshot_includes_buses(self, client: AsyncClient) -> None:
        resp = await client.post("/mixer/buses", json={"label": "Drums", "gain_db": -3.0})
        assert resp.status_code == 201
        body = resp.json()
        assert body["label"] == "Drums"
        assert body["gain_db"] == -3.0
        assert body["sink_node_name"].startswith("phonon_bus_")
        assert body["inserts"] == []
        # Listed in the bus list and in the global snapshot.
        listed = (await client.get("/mixer/buses")).json()
        assert len(listed) == 1 and listed[0]["id"] == body["id"]
        snap = (await client.get("/mixer")).json()
        assert any(b["id"] == body["id"] for b in snap["buses"])

    async def test_patch_bus_gain_and_mute(self, client: AsyncClient) -> None:
        bus_id = (await client.post("/mixer/buses", json={"label": "B1"})).json()["id"]
        resp = await client.patch(
            f"/mixer/buses/{bus_id}",
            json={"gain_db": -6.0, "mute": True, "solo": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["gain_db"] == -6.0
        assert body["mute"] is True

    async def test_delete_bus(self, client: AsyncClient) -> None:
        bus_id = (await client.post("/mixer/buses", json={"label": "B1"})).json()["id"]
        resp = await client.delete(f"/mixer/buses/{bus_id}")
        assert resp.status_code == 204
        listed = (await client.get("/mixer/buses")).json()
        assert listed == []

    async def test_patch_unknown_bus_404s(self, client: AsyncClient) -> None:
        resp = await client.patch("/mixer/buses/nope", json={"gain_db": 0.0})
        assert resp.status_code == 404

    async def test_invalid_gain_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/mixer/buses", json={"label": "B", "gain_db": 99.0})
        assert resp.status_code == 422

    async def test_put_source_bus_send_creates_then_updates(self, client: AsyncClient) -> None:
        bus_id = (await client.post("/mixer/buses", json={"label": "Drums"})).json()["id"]
        s = await client.post(
            "/mixer/sources",
            json={
                "source_node_name": "airplay_in",
                "source_is_sink": True,
                "label": "AP",
                "to_master": False,
            },
        )
        src_id = s.json()["id"]
        # First PUT creates the send.
        resp = await client.put(
            f"/mixer/sources/{src_id}/bus-sends/{bus_id}",
            json={"gain_db": -3.0, "enabled": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        sends = body["bus_sends"]
        assert len(sends) == 1
        assert sends[0]["bus_id"] == bus_id
        assert sends[0]["gain_db"] == -3.0
        assert sends[0]["enabled"] is True
        # Second PUT updates only the supplied field.
        resp = await client.put(
            f"/mixer/sources/{src_id}/bus-sends/{bus_id}", json={"enabled": False}
        )
        sends = resp.json()["bus_sends"]
        assert len(sends) == 1
        assert sends[0]["gain_db"] == -3.0  # preserved
        assert sends[0]["enabled"] is False

    async def test_put_source_bus_send_unknown_bus_404s(self, client: AsyncClient) -> None:
        s = await client.post(
            "/mixer/sources",
            json={
                "source_node_name": "airplay_in",
                "source_is_sink": True,
                "label": "AP",
            },
        )
        src_id = s.json()["id"]
        resp = await client.put(
            f"/mixer/sources/{src_id}/bus-sends/missing-bus",
            json={"gain_db": 0.0, "enabled": True},
        )
        assert resp.status_code == 404

    async def test_delete_source_bus_send_idempotent(self, client: AsyncClient) -> None:
        bus_id = (await client.post("/mixer/buses", json={"label": "Drums"})).json()["id"]
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
        # No send yet — delete still returns the source, 200.
        resp = await client.delete(f"/mixer/sources/{s['id']}/bus-sends/{bus_id}")
        assert resp.status_code == 200
        # Create then delete.
        await client.put(
            f"/mixer/sources/{s['id']}/bus-sends/{bus_id}",
            json={"gain_db": 0.0, "enabled": True},
        )
        resp = await client.delete(f"/mixer/sources/{s['id']}/bus-sends/{bus_id}")
        assert resp.status_code == 200
        assert resp.json()["bus_sends"] == []

    async def test_remove_bus_prunes_source_sends(self, client: AsyncClient) -> None:
        bus_id = (await client.post("/mixer/buses", json={"label": "Drums"})).json()["id"]
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
        await client.put(
            f"/mixer/sources/{s['id']}/bus-sends/{bus_id}",
            json={"gain_db": 0.0, "enabled": True},
        )
        await client.delete(f"/mixer/buses/{bus_id}")
        snap = (await client.get("/mixer")).json()
        # Bus gone + every source's bus_sends pruned.
        assert snap["buses"] == []
        for src in snap["sources"]:
            assert src["bus_sends"] == []
