"""Tests for the bluealsa-aplay output parser. We mock the subprocess
call (`_run`) and test the pure parsing logic against canned outputs
from real bluez-alsa-utils releases."""

from __future__ import annotations

from typing import Any

import pytest

from phonon_stage.api import bluealsa_bridge as bridge

# ── Canned outputs from real `bluealsa-aplay --list-pcms` runs ───────────


# bluez-alsa 4.1+ format (Ubuntu 24.04, bookworm-backports)
# DEV= is in the FIRST comma-segment, with the bluealsa: prefix
BAA_4_1_TWO_PHONES = """bluealsa:DEV=64:6D:2F:1B:C5:41,PROFILE=a2dp,SRV=org.bluealsa
    1337, phone, capture
    A2DP (SBC): S16_LE 2 channels 44100 Hz
bluealsa:DEV=B8:7B:C5:DF:89:55,PROFILE=a2dp,SRV=org.bluealsa
    1337-2, trusted phone, capture
    A2DP (SBC): S16_LE 2 channels 44100 Hz
"""

# bluez-alsa 4.0 format (Pi RPiOS Bookworm stock) — SRV= first, DEV= second
BAA_4_0_ONE_PHONE = """bluealsa:SRV=org.bluealsa,DEV=64:6D:2F:1B:C5:41,PROFILE=a2dp
    1337, phone, capture
    A2DP (SBC): S16_LE 2 channels 44100 Hz
"""

# Mixed playback + capture (transmitter to JBL + receiver from phone)
BAA_MIXED = """bluealsa:DEV=AA:BB:CC:11:22:33,PROFILE=a2dp,SRV=org.bluealsa
    JBL Xtreme, , playback
    A2DP (SBC): S16_LE 2 channels 48000 Hz
bluealsa:DEV=64:6D:2F:1B:C5:41,PROFILE=a2dp,SRV=org.bluealsa
    1337, phone, capture
    A2DP (SBC): S16_LE 2 channels 44100 Hz
"""

# aptX codec — different latency estimate
BAA_APTX = """bluealsa:DEV=DD:EE:FF:00:11:22,PROFILE=a2dp,SRV=org.bluealsa
    Sennheiser TW, , playback
    A2DP (aptX): S16_LE 2 channels 44100 Hz
"""

# Empty / disconnected case
BAA_EMPTY = ""


@pytest.fixture()
def _mock_run(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stub bluealsa_bridge._run so the parser sees canned output instead
    of shelling out to bluealsa-aplay."""

    holder: dict[str, str] = {"raw": ""}

    async def _fake_run(cmd: str, timeout: float = 5.0) -> str:
        return holder["raw"]

    monkeypatch.setattr(bridge, "_run", _fake_run)
    return holder


class TestBluealsaParser:
    @pytest.mark.asyncio
    async def test_4_1_format_two_phones(self, _mock_run: dict[str, str]) -> None:
        _mock_run["raw"] = BAA_4_1_TWO_PHONES
        pcms = await bridge._list_bluealsa_pcms()
        assert len(pcms) == 2
        macs = {p["mac"] for p in pcms}
        assert macs == {"64:6D:2F:1B:C5:41", "B8:7B:C5:DF:89:55"}
        for p in pcms:
            assert p["type"] == "capture"
            assert p["codec"] == "SBC"
            assert p["channels"] == "2"
            assert p["rate"] == "44100"

    @pytest.mark.asyncio
    async def test_4_0_format_still_works(self, _mock_run: dict[str, str]) -> None:
        """The DEV= matcher is permissive — works for both 4.0 (SRV first)
        and 4.1+ (bluealsa:DEV first) layouts."""
        _mock_run["raw"] = BAA_4_0_ONE_PHONE
        pcms = await bridge._list_bluealsa_pcms()
        assert len(pcms) == 1
        assert pcms[0]["mac"] == "64:6D:2F:1B:C5:41"
        assert pcms[0]["name"] == "1337"

    @pytest.mark.asyncio
    async def test_mixed_playback_and_capture(self, _mock_run: dict[str, str]) -> None:
        _mock_run["raw"] = BAA_MIXED
        pcms = await bridge._list_bluealsa_pcms()
        assert len(pcms) == 2
        types = {p["type"] for p in pcms}
        assert types == {"playback", "capture"}
        # JBL is 48 kHz playback
        jbl = next(p for p in pcms if p["type"] == "playback")
        assert jbl["rate"] == "48000"
        assert jbl["mac"] == "AA:BB:CC:11:22:33"

    @pytest.mark.asyncio
    async def test_codec_latency_estimate_aptx(self, _mock_run: dict[str, str]) -> None:
        _mock_run["raw"] = BAA_APTX
        pcms = await bridge._list_bluealsa_pcms()
        assert pcms[0]["codec"] == "aptX"
        assert pcms[0]["codec_latency_ms"] == "70"
        assert pcms[0]["total_latency_ms"] == "120"  # 70 + 50 bridge

    @pytest.mark.asyncio
    async def test_codec_latency_estimate_sbc(self, _mock_run: dict[str, str]) -> None:
        _mock_run["raw"] = BAA_4_1_TWO_PHONES
        pcms = await bridge._list_bluealsa_pcms()
        for p in pcms:
            assert p["codec_latency_ms"] == "150"  # SBC default
            assert p["bridge_buffer_ms"] == "50"
            assert p["total_latency_ms"] == "200"

    @pytest.mark.asyncio
    async def test_empty_input(self, _mock_run: dict[str, str]) -> None:
        _mock_run["raw"] = BAA_EMPTY
        pcms = await bridge._list_bluealsa_pcms()
        assert pcms == []

    @pytest.mark.asyncio
    async def test_dedup_by_mac_plus_type(self, _mock_run: dict[str, str]) -> None:
        """Same device announced twice (e.g. unstable PCM) should produce
        only one entry per (mac, type)."""
        dup = BAA_4_1_TWO_PHONES + BAA_4_1_TWO_PHONES
        _mock_run["raw"] = dup
        pcms = await bridge._list_bluealsa_pcms()
        assert len(pcms) == 2  # not 4

    @pytest.mark.asyncio
    async def test_unknown_codec_falls_back_to_sbc_latency(
        self, _mock_run: dict[str, str]
    ) -> None:
        weird = BAA_APTX.replace("aptX", "WeirdCodec")
        _mock_run["raw"] = weird
        pcms = await bridge._list_bluealsa_pcms()
        assert pcms[0]["codec"] == "WeirdCodec"
        # Unknown codec → 150 ms default (matches SBC/A2DP worst case)
        assert pcms[0]["codec_latency_ms"] == "150"
