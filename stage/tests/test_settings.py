"""Tests for the runtime settings module — defaults, persistence, validation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from phonon_stage.api import settings as settings_mod
from phonon_stage.api.settings import Aes67Defaults, PtpSettings, SapSettings, Settings

if TYPE_CHECKING:
    from pathlib import Path


class TestSettingsModel:
    def test_defaults_are_sensible(self) -> None:
        s = Settings()
        # SAP — discovery enabled by default, sane interval
        assert s.sap.announce_enabled is True
        assert s.sap.listen_enabled is True
        assert 5 <= s.sap.announce_interval_s <= 30

        # PTP — disabled by default (needs linuxptp + opt-in)
        assert s.ptp.enabled is False
        assert s.ptp.mode == "auto"
        assert s.ptp.profile == "aes67"
        assert s.ptp.domain == 0

        # AES67 defaults — AES67 spec: 48 kHz, L16BE, 1 ms ptime
        assert s.aes67.sample_rate == 48000
        assert s.aes67.audio_format == "S16BE"
        assert s.aes67.ptime_ms == 1.0
        assert s.aes67.channels == 2

    def test_sap_interval_bounds(self) -> None:
        with pytest.raises(ValidationError):
            SapSettings(announce_interval_s=1)  # too low
        with pytest.raises(ValidationError):
            SapSettings(announce_interval_s=120)  # too high

    def test_ptp_domain_bounds(self) -> None:
        PtpSettings(domain=0)  # ok
        PtpSettings(domain=127)  # ok
        with pytest.raises(ValidationError):
            PtpSettings(domain=-1)
        with pytest.raises(ValidationError):
            PtpSettings(domain=128)

    def test_ptp_mode_enum(self) -> None:
        PtpSettings(mode="auto")
        PtpSettings(mode="grandmaster")
        PtpSettings(mode="slave")
        with pytest.raises(ValidationError):
            PtpSettings(mode="bogus")

    def test_aes67_format_enum(self) -> None:
        Aes67Defaults(audio_format="S16BE")
        Aes67Defaults(audio_format="S24BE")
        Aes67Defaults(audio_format="S32BE")
        with pytest.raises(ValidationError):
            Aes67Defaults(audio_format="MP3")  # not a raw format

    def test_aes67_ptime_bounds(self) -> None:
        Aes67Defaults(ptime_ms=0.125)  # AES67 minimum
        Aes67Defaults(ptime_ms=10.0)  # AES67 maximum
        with pytest.raises(ValidationError):
            Aes67Defaults(ptime_ms=0.05)
        with pytest.raises(ValidationError):
            Aes67Defaults(ptime_ms=20.0)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Settings(unknown_section={"foo": "bar"})  # type: ignore[call-arg]


class TestSettingsPersistence:
    def test_init_with_no_file_uses_defaults(self, tmp_path: Path) -> None:
        settings_mod._settings = Settings()  # reset
        settings_mod._path = None
        settings_mod.init(tmp_path)
        assert settings_mod.get().sap.announce_enabled is True
        assert settings_mod._path == tmp_path / "settings.json"

    def test_init_loads_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "sap": {
                        "announce_enabled": False,
                        "announce_interval_s": 30,
                        "listen_enabled": True,
                    },
                    "ptp": {
                        "enabled": True,
                        "mode": "grandmaster",
                        "interface": "eth0",
                        "profile": "aes67",
                        "domain": 5,
                    },
                    "aes67": {
                        "multicast_group": "239.69.99.99",
                        "port": 5555,
                        "loop": False,
                        "channels": 2,
                        "sample_rate": 48000,
                        "audio_format": "S24BE",
                        "ptime_ms": 4.0,
                    },
                }
            )
        )
        settings_mod.init(tmp_path)
        s = settings_mod.get()
        assert s.sap.announce_enabled is False
        assert s.sap.announce_interval_s == 30
        assert s.ptp.enabled is True
        assert s.ptp.mode == "grandmaster"
        assert s.ptp.interface == "eth0"
        assert s.ptp.domain == 5
        assert s.aes67.multicast_group == "239.69.99.99"
        assert s.aes67.audio_format == "S24BE"
        assert s.aes67.ptime_ms == 4.0

    def test_init_corrupted_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("{ this is not valid json")
        settings_mod._settings = Settings()  # reset
        settings_mod.init(tmp_path)
        # On error, defaults are kept; we don't blow up the daemon
        assert settings_mod.get().sap.announce_enabled is True
