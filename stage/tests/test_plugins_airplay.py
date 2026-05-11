"""Tests for AirplayV1Plugin — lifecycle + settings render/parse."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.plugins.airplay_v1 import AirplayV1Plugin, AirplayV1Settings
from phonon_stage.plugins.system import FakeSystemBackend

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fake_sys() -> FakeSystemBackend:
    return FakeSystemBackend()


@pytest.fixture()
def conf_path(tmp_path: Path) -> Path:
    return tmp_path / "shairport-sync.conf"


@pytest.fixture()
def plugin(fake_sys: FakeSystemBackend, conf_path: Path) -> AirplayV1Plugin:
    return AirplayV1Plugin(system=fake_sys, conf_path=conf_path)


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
        assert s.volume_mode == "software"

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
        original = AirplayV1Settings(
            name="Salon Phonon", password="1234", interpolation="basic", volume_mode="hardware"
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

    def test_volume_mode_constrained(self) -> None:
        with pytest.raises(ValueError):
            AirplayV1Settings(volume_mode="muted")  # type: ignore[arg-type]
