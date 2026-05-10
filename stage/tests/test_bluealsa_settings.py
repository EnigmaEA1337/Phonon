"""Tests for the per-bridge settings store, codec helpers, override
resolution and HTTP endpoints in bluealsa_bridge."""

from __future__ import annotations

import json
from typing import Any

import pytest

from phonon_stage.api import bluealsa_bridge as bridge


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the store path to a temp file and reset the in-memory cache
    before each test, so tests don't write to /var/lib/phonon and don't
    bleed state between cases."""
    monkeypatch.setattr(bridge, "_BRIDGE_SETTINGS_PATH", tmp_path / "bridge_settings.json")
    bridge._settings_cache = {}


class TestStoreRoundtrip:
    def test_load_returns_empty_when_file_missing(self) -> None:
        bridge.init_settings_store()
        assert bridge.get_bridge_override("AA:BB:CC:00:11:22", "capture") == {}

    def test_set_then_get_persists(self) -> None:
        bridge.set_bridge_override(
            "AA:BB:CC:00:11:22", "capture", {"rate": 44100, "period_ms": 25}
        )
        # Round-trip via disk: rebuild the in-memory cache from the file.
        bridge._settings_cache = {}
        bridge.init_settings_store()
        ov = bridge.get_bridge_override("AA:BB:CC:00:11:22", "capture")
        assert ov == {"rate": 44100, "period_ms": 25}

    def test_clear_removes_entry(self) -> None:
        bridge.set_bridge_override("AA:BB:CC:00:11:22", "capture", {"rate": 44100})
        bridge.set_bridge_override("AA:BB:CC:00:11:22", "capture", {})
        # Persisted file should also drop the key
        bridge._settings_cache = {}
        bridge.init_settings_store()
        assert bridge.get_bridge_override("AA:BB:CC:00:11:22", "capture") == {}

    def test_mac_case_insensitive(self) -> None:
        bridge.set_bridge_override("aa:bb:cc:00:11:22", "capture", {"rate": 44100})
        assert bridge.get_bridge_override("AA:BB:CC:00:11:22", "capture") == {"rate": 44100}

    def test_corrupt_file_returns_empty(self, tmp_path: Any) -> None:
        bridge._BRIDGE_SETTINGS_PATH.write_text("not json {")
        bridge.init_settings_store()
        assert bridge.get_bridge_override("AA:BB:CC:00:11:22", "capture") == {}


class TestEffectiveParamsResolution:
    def test_no_override_uses_negotiated_rate(self) -> None:
        eff = bridge._resolve_effective_params({}, negotiated_rate=44100, default_buffer_ms=50)
        assert eff == {"rate": 44100, "period_ms": 50, "channels": 2, "format": "s16le"}

    def test_no_override_no_negotiated_falls_back_to_48k(self) -> None:
        eff = bridge._resolve_effective_params({}, negotiated_rate=0, default_buffer_ms=50)
        assert eff["rate"] == 48000

    def test_override_rate_wins(self) -> None:
        eff = bridge._resolve_effective_params({"rate": 48000}, 44100, 50)
        assert eff["rate"] == 48000

    def test_invalid_rate_falls_back(self) -> None:
        # 96000 isn't in _ALLOWED_RATES — resolver ignores it
        eff = bridge._resolve_effective_params({"rate": 96000}, 44100, 50)
        assert eff["rate"] == 44100

    def test_override_period_channels_format(self) -> None:
        eff = bridge._resolve_effective_params(
            {"period_ms": 25, "channels": 1, "format": "s24_3le"}, 48000, 50
        )
        assert eff == {"rate": 48000, "period_ms": 25, "channels": 1, "format": "s24_3le"}

    def test_invalid_period_falls_back_to_default(self) -> None:
        # 7 ms isn't allowed (too small for stable BT delivery)
        eff = bridge._resolve_effective_params({"period_ms": 7}, 48000, 50)
        assert eff["period_ms"] == 50


class TestCodecParser:
    @pytest.mark.asyncio
    async def test_codec_info_parses_selected_and_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two distinct canned outputs: list-pcms first, then info.
        outputs = iter(
            [
                # list-pcms — must contain a path matching our (mac, dtype)
                "/org/bluealsa/hci0/dev_AA_BB_CC_00_11_22/a2dpsink/source\n",
                # info
                """Device: /org/bluez/hci0/dev_AA_BB_CC_00_11_22
Selected codec: SBC
Available codecs: SBC[*] AAC
Channels: 2
Sampling: 44100 Hz
""",
            ]
        )

        async def fake_run(cmd: str, timeout: float = 5.0) -> str:
            return next(outputs)

        monkeypatch.setattr(bridge, "_run", fake_run)
        info = await bridge.get_codec_info("AA:BB:CC:00:11:22", "capture")
        assert info["selected"] == "SBC"
        assert info["available"] == ["SBC", "AAC"]

    @pytest.mark.asyncio
    async def test_codec_info_missing_path_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_run(cmd: str, timeout: float = 5.0) -> str:
            return ""  # list-pcms empty → no path resolvable

        monkeypatch.setattr(bridge, "_run", fake_run)
        info = await bridge.get_codec_info("AA:BB:CC:00:11:22", "capture")
        assert info == {"selected": "", "available": []}

    @pytest.mark.asyncio
    async def test_pcm_path_disambiguates_capture_vs_playback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both paths exist — ensure we pick the right one per dtype.
        list_pcms_out = (
            "/org/bluealsa/hci0/dev_AA_BB_CC_00_11_22/a2dpsink/source\n"
            "/org/bluealsa/hci0/dev_AA_BB_CC_00_11_22/a2dpsource/sink\n"
        )

        async def fake_run(cmd: str, timeout: float = 5.0) -> str:
            return list_pcms_out

        monkeypatch.setattr(bridge, "_run", fake_run)
        capture_path = await bridge._bluealsa_pcm_path("AA:BB:CC:00:11:22", "capture")
        playback_path = await bridge._bluealsa_pcm_path("AA:BB:CC:00:11:22", "playback")
        assert capture_path.endswith("a2dpsink/source")
        assert playback_path.endswith("a2dpsource/sink")


class TestEndpointValidation:
    def test_validate_rejects_bad_rate(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            bridge._validate_override_or_400({"rate": 96000})
        assert exc.value.status_code == 400

    def test_validate_rejects_bad_period(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            bridge._validate_override_or_400({"period_ms": 7})
        assert exc.value.status_code == 400

    def test_validate_rejects_bad_format(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            bridge._validate_override_or_400({"format": "float64le"})
        assert exc.value.status_code == 400

    def test_validate_accepts_partial_override(self) -> None:
        # Only setting rate — others must be allowed missing
        bridge._validate_override_or_400({"rate": 44100})

    def test_normalize_dtype_rejects_garbage(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            bridge._normalize_dtype("nope")
        assert bridge._normalize_dtype("capture") == "capture"
        assert bridge._normalize_dtype("playback") == "playback"


class TestStoreFilePermissions:
    def test_save_writes_atomically_and_chmods_0600(self, tmp_path: Any) -> None:
        bridge.set_bridge_override("AA:BB:CC:00:11:22", "capture", {"rate": 44100})
        # File should exist with the data
        path = bridge._BRIDGE_SETTINGS_PATH
        assert path.exists()
        data = json.loads(path.read_text())
        assert "AA:BB:CC:00:11:22_capture" in data
        # Mode 0600 (best-effort — chmod is suppressed on OSError so we
        # check the actual mode here. May not match on filesystems that
        # don't carry POSIX modes; in CI we run on tmpfs/ext4 so it does.)
        assert path.stat().st_mode & 0o777 == 0o600
