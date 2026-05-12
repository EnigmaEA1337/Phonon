"""Tests for SpotifyV1Plugin — lifecycle + settings render/parse + null-sink."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.pipewire.fake import FakePipeWireBackend
from phonon_stage.plugins.spotify_v1 import (
    NULL_SINK_NAME,
    SpotifyV1Plugin,
    SpotifyV1Settings,
)
from phonon_stage.plugins.system import FakeSystemBackend

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def fake_sys() -> FakeSystemBackend:
    return FakeSystemBackend()


@pytest.fixture()
def fake_pw_plugin() -> FakePipeWireBackend:
    return FakePipeWireBackend()


@pytest.fixture()
def conf_path(tmp_path: Path) -> Path:
    return tmp_path / "librespot.env"


@pytest.fixture()
def plugin(
    fake_sys: FakeSystemBackend,
    fake_pw_plugin: FakePipeWireBackend,
    conf_path: Path,
) -> SpotifyV1Plugin:
    return SpotifyV1Plugin(system=fake_sys, pw_backend=fake_pw_plugin, conf_path=conf_path)


# ── Lifecycle ──────────────────────────────────────────────────────


class TestSpotifyV1Lifecycle:
    async def test_runtime_initial_state(self, plugin: SpotifyV1Plugin) -> None:
        rt = await plugin.runtime()
        assert rt.enabled is False
        assert rt.running is False
        assert rt.last_error == ""

    async def test_enable_writes_default_env_if_missing(
        self, plugin: SpotifyV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        """librespot.service refuses to start without LIBRESPOT_ARGS —
        provisioning defaults on enable means the user never hits a
        'unit failed' on first enable."""
        assert fake_sys.file_exists(conf_path) is False
        await plugin.enable()
        assert fake_sys.file_exists(conf_path) is True
        assert "LIBRESPOT_ARGS=" in fake_sys.read_text(conf_path)
        assert await fake_sys.systemctl_is_enabled(plugin.UNIT) is True

    async def test_enable_preserves_existing_env(
        self, plugin: SpotifyV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        fake_sys.write_text_atomic(conf_path, "LIBRESPOT_ARGS=--name Custom\n")
        await plugin.enable()
        assert "Custom" in fake_sys.read_text(conf_path)

    async def test_disable_stops_then_disables(
        self, plugin: SpotifyV1Plugin, fake_sys: FakeSystemBackend
    ) -> None:
        await plugin.enable()
        await plugin.start()
        assert await fake_sys.systemctl_is_active(plugin.UNIT) is True
        await plugin.disable()
        assert await fake_sys.systemctl_is_active(plugin.UNIT) is False
        assert await fake_sys.systemctl_is_enabled(plugin.UNIT) is False

    async def test_restart_keeps_null_sink(
        self, plugin: SpotifyV1Plugin, fake_pw_plugin: FakePipeWireBackend
    ) -> None:
        """restart() should bounce the daemon but leave the null-sink
        intact — mappings that target spotify_in must survive."""
        await plugin.enable()
        await plugin.start()
        assert any(n.name == NULL_SINK_NAME for n in await fake_pw_plugin.list_nodes())
        await plugin.restart()
        assert any(n.name == NULL_SINK_NAME for n in await fake_pw_plugin.list_nodes())


# ── Settings round-trip ────────────────────────────────────────────


class TestSpotifyV1Settings:
    async def test_default_settings_round_trip(
        self, plugin: SpotifyV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        await plugin.put_settings(SpotifyV1Settings())
        assert fake_sys.file_exists(conf_path)
        loaded = await plugin.get_settings()
        assert isinstance(loaded, SpotifyV1Settings)
        assert loaded == SpotifyV1Settings()

    async def test_custom_settings_round_trip(self, plugin: SpotifyV1Plugin) -> None:
        custom = SpotifyV1Settings(
            name="Living Room",
            device_type="avr",
            bitrate=160,
            format="S16",
            initial_volume=80,
            autoplay=False,
            disable_audio_cache=False,
            zeroconf_port=4321,
            quiet=False,
        )
        await plugin.put_settings(custom)
        loaded = await plugin.get_settings()
        assert loaded == custom

    async def test_rendered_conf_contains_required_args(
        self, plugin: SpotifyV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        """--device must point at our null-sink so librespot's output is
        contained in the Phonon routing matrix, never auto-routed to the
        default sink."""
        await plugin.put_settings(SpotifyV1Settings())
        body = fake_sys.read_text(conf_path)
        assert "--device" in body
        assert NULL_SINK_NAME in body
        assert "--backend pulseaudio" in body

    async def test_name_with_space_is_quoted(
        self, plugin: SpotifyV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        """systemd's $VAR expansion splits on whitespace, so a name
        with a space must be quoted in the env file or it'd land as
        two args."""
        await plugin.put_settings(SpotifyV1Settings(name="Living Room"))
        body = fake_sys.read_text(conf_path)
        # shlex.quote wraps it in single quotes
        assert "'Living Room'" in body
        # Round-trip should still recover the original name
        loaded = await plugin.get_settings()
        assert loaded.name == "Living Room"

    async def test_unknown_token_doesnt_break_parse(
        self, plugin: SpotifyV1Plugin, fake_sys: FakeSystemBackend, conf_path: Path
    ) -> None:
        """Future librespot versions may add flags we don't model — the
        parser should ignore them and recover what it does understand."""
        fake_sys.write_text_atomic(
            conf_path,
            "LIBRESPOT_ARGS=--name Phonon --some-future-flag value --bitrate 320\n",
        )
        loaded = await plugin.get_settings()
        assert loaded.name == "Phonon"
        assert loaded.bitrate == 320

    async def test_missing_env_file_returns_defaults(self, plugin: SpotifyV1Plugin) -> None:
        loaded = await plugin.get_settings()
        assert loaded == SpotifyV1Settings()


# ── Null-sink management ──────────────────────────────────────────


class TestSpotifyV1NullSink:
    async def test_enable_creates_null_sink(
        self, plugin: SpotifyV1Plugin, fake_pw_plugin: FakePipeWireBackend
    ) -> None:
        await plugin.enable()
        nodes = await fake_pw_plugin.list_nodes()
        assert any(n.name == NULL_SINK_NAME for n in nodes)

    async def test_disable_removes_null_sink(
        self, plugin: SpotifyV1Plugin, fake_pw_plugin: FakePipeWireBackend
    ) -> None:
        await plugin.enable()
        assert any(n.name == NULL_SINK_NAME for n in await fake_pw_plugin.list_nodes())
        await plugin.disable()
        assert not any(n.name == NULL_SINK_NAME for n in await fake_pw_plugin.list_nodes())

    async def test_enable_twice_doesnt_duplicate_null_sink(
        self, plugin: SpotifyV1Plugin, fake_pw_plugin: FakePipeWireBackend
    ) -> None:
        """pactl modules survive across phonon-stage restarts — enabling
        the plugin a second time must not stack a second null-sink with
        the same name."""
        await plugin.enable()
        await plugin.enable()
        nodes = await fake_pw_plugin.list_nodes()
        count = sum(1 for n in nodes if n.name == NULL_SINK_NAME)
        assert count == 1
