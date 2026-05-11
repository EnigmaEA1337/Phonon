"""Tests for /plugins REST endpoints — list, lifecycle, settings, errors."""

from __future__ import annotations

from typing import TYPE_CHECKING

from phonon_stage.plugins.airplay_v1 import AirplayV1Plugin

if TYPE_CHECKING:
    from httpx import AsyncClient

    from phonon_stage.plugins.system import FakeSystemBackend


AIRPLAY_NAME = "airplay-v1"
UNIT = AirplayV1Plugin.UNIT


class TestPluginListing:
    async def test_list_includes_airplay(self, client: AsyncClient) -> None:
        resp = await client.get("/plugins")
        assert resp.status_code == 200
        names = {p["name"] for p in resp.json()}
        assert AIRPLAY_NAME in names

    async def test_list_shape(self, client: AsyncClient) -> None:
        resp = await client.get("/plugins")
        airplay = next(p for p in resp.json() if p["name"] == AIRPLAY_NAME)
        assert airplay["title"]
        assert airplay["description"]
        assert airplay["family"] == "source"
        assert airplay["enabled"] is False
        assert airplay["running"] is False
        assert airplay["pw_node_names"] == []
        assert airplay["last_error"] == ""

    async def test_get_unknown_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/plugins/does-not-exist")
        assert resp.status_code == 404
        assert "unknown plugin" in resp.json()["detail"]


class TestPluginLifecycle:
    async def test_enable_flips_state(
        self, client: AsyncClient, fake_system: FakeSystemBackend
    ) -> None:
        resp = await client.post(f"/plugins/{AIRPLAY_NAME}/enable")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert fake_system.enabled.get(UNIT) is True

    async def test_disable_after_enable(
        self, client: AsyncClient, fake_system: FakeSystemBackend
    ) -> None:
        await client.post(f"/plugins/{AIRPLAY_NAME}/enable")
        await client.post(f"/plugins/{AIRPLAY_NAME}/start")
        resp = await client.post(f"/plugins/{AIRPLAY_NAME}/disable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert resp.json()["running"] is False
        assert fake_system.enabled.get(UNIT) is False

    async def test_start_stop(self, client: AsyncClient, fake_system: FakeSystemBackend) -> None:
        resp = await client.post(f"/plugins/{AIRPLAY_NAME}/start")
        assert resp.status_code == 200
        assert resp.json()["running"] is True
        resp = await client.post(f"/plugins/{AIRPLAY_NAME}/stop")
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    async def test_restart(self, client: AsyncClient, fake_system: FakeSystemBackend) -> None:
        await client.post(f"/plugins/{AIRPLAY_NAME}/start")
        resp = await client.post(f"/plugins/{AIRPLAY_NAME}/restart")
        assert resp.status_code == 200
        assert resp.json()["running"] is True

    async def test_lifecycle_on_unknown_404s(self, client: AsyncClient) -> None:
        for verb in ("enable", "disable", "start", "stop", "restart"):
            resp = await client.post(f"/plugins/nope/{verb}")
            assert resp.status_code == 404, f"{verb} did not 404"

    async def test_systemctl_failure_surfaces_as_500(
        self, client: AsyncClient, fake_system: FakeSystemBackend
    ) -> None:
        """Real systemctl can fail (D-Bus error, unit missing) — the API
        translates that to a 500 with the underlying message instead of
        silently returning the previous state."""
        fake_system.fail_on.add(("enable", UNIT))
        resp = await client.post(f"/plugins/{AIRPLAY_NAME}/enable")
        assert resp.status_code == 500
        assert "forced failure" in resp.json()["detail"]


class TestPluginSettings:
    async def test_get_settings_returns_defaults(self, client: AsyncClient) -> None:
        resp = await client.get(f"/plugins/{AIRPLAY_NAME}/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Phonon"
        assert body["interpolation"] == "soxr"
        assert body["volume_mode"] == "software"

    async def test_put_settings_persists_and_responds(self, client: AsyncClient) -> None:
        resp = await client.put(
            f"/plugins/{AIRPLAY_NAME}/settings",
            json={"name": "Salon", "interpolation": "basic"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Salon"
        # Verify by re-reading
        resp2 = await client.get(f"/plugins/{AIRPLAY_NAME}/settings")
        assert resp2.json()["name"] == "Salon"
        assert resp2.json()["interpolation"] == "basic"

    async def test_put_settings_rejects_unknown_field(self, client: AsyncClient) -> None:
        """ConfigDict(extra='forbid') on the settings model means unknown
        keys are caught at validation time → 422, not silently dropped."""
        resp = await client.put(
            f"/plugins/{AIRPLAY_NAME}/settings",
            json={"name": "X", "magic": "value"},
        )
        assert resp.status_code == 422

    async def test_put_settings_rejects_bad_enum(self, client: AsyncClient) -> None:
        resp = await client.put(
            f"/plugins/{AIRPLAY_NAME}/settings",
            json={"interpolation": "lanczos"},
        )
        assert resp.status_code == 422

    async def test_put_settings_unknown_plugin_404s(self, client: AsyncClient) -> None:
        resp = await client.put("/plugins/nope/settings", json={"name": "x"})
        assert resp.status_code == 404


class TestPluginPipeWireJoin:
    async def test_pw_node_match_surfaces_in_response(self, client: AsyncClient) -> None:
        """When shairport-sync is running and pushing into pipewire-pulse,
        a node matching the plugin's pw_node_pattern shows up. The API
        joins the runtime PW state with the plugin metadata so the UI
        can render 'connected, source visible' without a separate call."""
        from phonon_stage.pipewire.backend import PwNode

        # The fixtures' fake_pw has 3 nodes by default; inject one that
        # matches the airplay pattern.
        from tests.conftest import SAMPLE_PW_NODES

        # We can't easily re-construct fixtures inside a test, but the
        # client uses the same fake_pw object as fake_pw fixture — fetch
        # it from app.state.
        app = client._transport.app  # type: ignore[attr-defined]
        fake_pw = app.state.pw_backend
        fake_pw.nodes = [
            *SAMPLE_PW_NODES,
            PwNode(
                id=99,
                name="Shairport Sync",
                media_class="Stream/Output/Audio",
                nick="Shairport Sync",
                state="running",
            ),
        ]

        resp = await client.get(f"/plugins/{AIRPLAY_NAME}")
        assert resp.status_code == 200
        assert "Shairport Sync" in resp.json()["pw_node_names"]
