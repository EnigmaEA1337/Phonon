"""Tests for discovery backend — fake registration lifecycle."""

from __future__ import annotations

from phonon_stage.discovery.fake import FakeDiscoveryBackend


class TestFakeDiscoveryBackend:
    async def test_register_sets_state(self) -> None:
        backend = FakeDiscoveryBackend()
        assert not backend.registered

        await backend.register("stage-abc12345", "10.100.0.50", 8401)

        assert backend.registered
        assert backend.last_stage_id == "stage-abc12345"
        assert backend.last_host == "10.100.0.50"
        assert backend.last_port == 8401
        assert "register" in backend.call_log

    async def test_unregister_clears_state(self) -> None:
        backend = FakeDiscoveryBackend()
        await backend.register("stage-abc12345", "10.100.0.50", 8401)
        await backend.unregister()

        assert not backend.registered
        assert backend.call_log == ["register", "unregister"]


class TestDiscoveryLifecycle:
    async def test_mdns_registered_on_app_startup(
        self, client: object, fake_discovery: FakeDiscoveryBackend
    ) -> None:
        """After app starts (client fixture triggers lifespan), mDNS is registered."""
        assert fake_discovery.registered
        assert fake_discovery.last_port == 8401
