"""Tests for AirplayV1Plugin — lifecycle + settings render/parse + null-sink."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.pipewire.fake import FakePipeWireBackend
from phonon_stage.plugins.airplay_v1 import (
    NULL_SINK_NAME,
    AirplayV1Plugin,
    AirplayV1Settings,
)
from phonon_stage.plugins.system import FakeSystemBackend

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fake_sys() -> FakeSystemBackend:
    return FakeSystemBackend()


@pytest.fixture()
def fake_pw_plugin() -> FakePipeWireBackend:
    """Empty PW backend — the plugin will populate it with airplay_in
    when enabled. Distinct from the conftest `fake_pw` so plugin tests
    don't fight with the standard SAMPLE_PW_NODES fixtures."""
    return FakePipeWireBackend()


@pytest.fixture()
def conf_path(tmp_path: Path) -> Path:
    return tmp_path / "shairport-sync.conf"


@pytest.fixture()
def plugin(
    fake_sys: FakeSystemBackend,
    fake_pw_plugin: FakePipeWireBackend,
    conf_path: Path,
) -> AirplayV1Plugin:
    return AirplayV1Plugin(system=fake_sys, pw_backend=fake_pw_plugin, conf_path=conf_path)


class TestAirplayV1Lifecycle:
    async def test_runtime_initial_state(self, plugin: AirplayV1Plugin) -> None:
        rt = await plugin.runtime()
        assert rt.enabled is False
        assert rt.running is False
        assert rt.last_error == ""

    async def test_enable_writes_default_conf_if_missing(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        """The shairport-sync daemon refuses to start without a conf —
        enabling the plugin must provision defaults so the user never
        sees a 'unit refused to start' error on first enable."""
        assert fake_sys.file_exists(conf_path) is False
        await plugin.enable()
        assert fake_sys.file_exists(conf_path) is True
        assert await fake_sys.systemctl_is_enabled(plugin.UNIT) is True

    async def test_enable_doesnt_overwrite_existing_conf(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        """User's settings shouldn't get clobbered if they disable/
        re-enable. Pre-existing conf must survive enable."""
        fake_sys.write_text_atomic(conf_path, "// my custom conf\n")
        await plugin.enable()
        assert fake_sys.read_text(conf_path) == "// my custom conf\n"

    async def test_disable_stops_then_disables(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend
    ) -> None:
        """Disable order matters: stop first so the running daemon is
        gone before the unit is taken off autostart, otherwise it
        lingers as an inert-but-running zombie until the next session
        boot."""
        await plugin.enable()
        await plugin.start()
        assert await fake_sys.systemctl_is_active(plugin.UNIT) is True
        await plugin.disable()
        assert await fake_sys.systemctl_is_active(plugin.UNIT) is False
        assert await fake_sys.systemctl_is_enabled(plugin.UNIT) is False

    async def test_start_writes_default_conf_if_missing(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        assert fake_sys.file_exists(conf_path) is False
        await plugin.start()
        assert fake_sys.file_exists(conf_path) is True
        assert await fake_sys.systemctl_is_active(plugin.UNIT) is True

    async def test_runtime_surfaces_last_error_when_enabled_but_inactive(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend
    ) -> None:
        """If a unit is enabled but won't stay up, the UI should see
        the daemon's last gasp instead of just a green/red toggle."""
        fake_sys.enabled[plugin.UNIT] = True
        fake_sys.active[plugin.UNIT] = False
        rt = await plugin.runtime()
        assert rt.enabled is True
        assert rt.running is False
        # fake_sys.systemctl_status_stderr returns a synthetic line
        assert plugin.UNIT in rt.last_error


class TestAirplayV1Settings:
    async def test_get_settings_when_no_conf_returns_defaults(
        self, plugin: AirplayV1Plugin
    ) -> None:
        s = await plugin.get_settings()
        assert isinstance(s, AirplayV1Settings)
        assert s.name == "Phonon"
        assert s.password == ""
        assert s.interpolation == "soxr"

    async def test_put_settings_writes_conf(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        await plugin.put_settings(
            AirplayV1Settings(name="Salon", password="abcd", interpolation="basic")
        )
        raw = fake_sys.read_text(conf_path)
        assert 'name = "Salon"' in raw
        assert 'password = "abcd"' in raw
        assert 'interpolation = "basic"' in raw

    async def test_put_settings_roundtrip(self, plugin: AirplayV1Plugin) -> None:
        """Render then parse must yield the same settings. Pins the
        custom curly-brace serializer/regex parser pair."""
        original = AirplayV1Settings(name="Salon Phonon", password="1234", interpolation="basic")
        await plugin.put_settings(original)
        roundtripped = await plugin.get_settings()
        assert roundtripped == original

    async def test_put_settings_roundtrip_full_surface(self, plugin: AirplayV1Plugin) -> None:
        """Every field of every type must survive a round-trip — pins
        the conf render/parse for the full settings surface so a future
        field addition doesn't silently drop on read."""
        original = AirplayV1Settings(
            name="Salon production",
            password="abcd",
            interpolation="basic",
            output_format="S24",
            playback_mode="mono",
            volume_control_profile="dasl_tapered",
            volume_max_db=-6.0,
            volume_range_db=72.0,
            ignore_volume_control=True,
            allow_session_interruption=False,
            session_timeout=300,
            audio_backend_buffer_desired_length_in_seconds=0.35,
            audio_backend_latency_offset_in_seconds=-0.020,
            drift_tolerance_in_seconds=0.005,
            resync_threshold_in_seconds=0.100,
            log_verbosity=2,
            statistics=True,
        )
        await plugin.put_settings(original)
        roundtripped = await plugin.get_settings()
        assert roundtripped == original

    async def test_put_settings_restarts_when_active(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend
    ) -> None:
        """A live settings change must restart the daemon so the new
        values take effect — otherwise the UI looks like it 'saved'
        but the running daemon ignores the change until next restart."""
        await plugin.start()
        # Drop the active flag and re-set to detect that put_settings
        # actually flipped restart on (restart in FakeSystemBackend
        # sets active back to True).
        fake_sys.active[plugin.UNIT] = False
        await plugin.put_settings(AirplayV1Settings(name="X"))
        # Not active going in → no restart triggered → still False.
        assert fake_sys.active[plugin.UNIT] is False

        fake_sys.active[plugin.UNIT] = True
        await plugin.put_settings(AirplayV1Settings(name="Y"))
        # Active going in → restart triggered → still True.
        assert fake_sys.active[plugin.UNIT] is True

    async def test_put_settings_rejects_wrong_model(self, plugin: AirplayV1Plugin) -> None:
        """Passing some other PluginSettings subclass (or a base
        PluginSettings) must fail loudly, not silently render a
        garbage conf."""
        from phonon_stage.plugins.backend import PluginSettings

        with pytest.raises(TypeError, match="expected AirplayV1Settings"):
            await plugin.put_settings(PluginSettings())

    async def test_password_empty_omits_line(
        self, plugin: AirplayV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        """Empty password = open AirPlay — the rendered conf must NOT
        contain `password = "";` because shairport-sync interprets an
        empty-string password as "0 chars required" which still gates
        clients. The renderer leaves the line out entirely."""
        await plugin.put_settings(AirplayV1Settings(name="Open", password=""))
        raw = fake_sys.read_text(conf_path)
        assert "password =" not in raw


class TestAirplayV1NullSink:
    """The plugin owns a dedicated null-sink so the AirPlay audio
    becomes a routable source in the patch bay (rather than being
    auto-routed by WirePlumber to the default sink). Pin the
    lifecycle so a refactor doesn't break the routing contract."""

    async def test_enable_creates_null_sink(
        self,
        plugin: AirplayV1Plugin,
        fake_pw_plugin: FakePipeWireBackend,
    ) -> None:
        assert NULL_SINK_NAME not in {n.name for n in fake_pw_plugin.nodes}
        await plugin.enable()
        names = {n.name for n in fake_pw_plugin.nodes}
        assert NULL_SINK_NAME in names

    async def test_start_creates_null_sink(
        self,
        plugin: AirplayV1Plugin,
        fake_pw_plugin: FakePipeWireBackend,
    ) -> None:
        assert NULL_SINK_NAME not in {n.name for n in fake_pw_plugin.nodes}
        await plugin.start()
        names = {n.name for n in fake_pw_plugin.nodes}
        assert NULL_SINK_NAME in names

    async def test_enable_is_idempotent(
        self,
        plugin: AirplayV1Plugin,
        fake_pw_plugin: FakePipeWireBackend,
    ) -> None:
        """Re-enabling must not stack duplicate null-sinks. Otherwise
        every phonon-stage restart that triggers an enable would leave
        orphan modules behind, exactly the bug we shipped on the
        loopback path."""
        await plugin.enable()
        await plugin.disable()  # tears the sink down
        await plugin.enable()  # bring it back

        count = sum(1 for n in fake_pw_plugin.nodes if n.name == NULL_SINK_NAME)
        assert count == 1

    async def test_disable_removes_null_sink(
        self,
        plugin: AirplayV1Plugin,
        fake_pw_plugin: FakePipeWireBackend,
    ) -> None:
        await plugin.enable()
        await plugin.disable()
        names = {n.name for n in fake_pw_plugin.nodes}
        assert NULL_SINK_NAME not in names

    async def test_null_sink_exposes_monitor_output_ports(
        self,
        plugin: AirplayV1Plugin,
        fake_pw_plugin: FakePipeWireBackend,
    ) -> None:
        """The monitor_FL/FR ports must be `direction=output` —
        that's what makes the null-sink behave as a routable source
        in the patch bay."""
        await plugin.enable()
        target = next(n for n in fake_pw_plugin.nodes if n.name == NULL_SINK_NAME)
        monitor_outs = [
            p for p in fake_pw_plugin.ports if p.node_id == target.id and p.direction == "output"
        ]
        names = {p.name for p in monitor_outs}
        assert names == {"monitor_FL", "monitor_FR"}

    async def test_conf_points_at_null_sink(
        self,
        plugin: AirplayV1Plugin,
        fake_sys: FakeSystemBackend,
        conf_path: object,
    ) -> None:
        """The shairport-sync conf must include `sink = "airplay_in"`
        — without it, audio falls back to the default sink and the
        whole null-sink approach is moot."""
        await plugin.put_settings(AirplayV1Settings())
        raw = fake_sys.read_text(conf_path)  # type: ignore[arg-type]
        assert f'sink = "{NULL_SINK_NAME}"' in raw

    async def test_pre_existing_null_sink_is_reused(
        self,
        plugin: AirplayV1Plugin,
        fake_pw_plugin: FakePipeWireBackend,
    ) -> None:
        """If a previous run left airplay_in around (pactl modules
        outlive phonon-stage as long as the user session is up),
        enable must NOT create a duplicate."""
        # Simulate the sink already being there.
        await fake_pw_plugin.load_null_sink(NULL_SINK_NAME, "AirPlay-In")
        assert sum(1 for n in fake_pw_plugin.nodes if n.name == NULL_SINK_NAME) == 1

        await plugin.enable()
        # Still exactly one — no duplicate from the enable path.
        assert sum(1 for n in fake_pw_plugin.nodes if n.name == NULL_SINK_NAME) == 1


class TestAirplayV1Validation:
    """Pydantic guarantees these — pin them so a refactor doesn't
    silently widen what the API will accept."""

    def test_name_required_non_empty(self) -> None:
        with pytest.raises(ValueError, match="at least 1 character"):
            AirplayV1Settings(name="")

    def test_name_length_capped(self) -> None:
        with pytest.raises(ValueError, match="at most 63 character"):
            AirplayV1Settings(name="x" * 64)

    def test_interpolation_constrained(self) -> None:
        with pytest.raises(ValueError):
            AirplayV1Settings(interpolation="lanczos")  # type: ignore[arg-type]
