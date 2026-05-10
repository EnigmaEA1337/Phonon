"""Tests for the ALSA hardware mixer parser + endpoints."""

from __future__ import annotations

from typing import Any

import pytest

from phonon_stage.api import alsa_mixer

# ── Canned outputs from real `amixer` runs ──────────────────────────────────

# DG60 (Avantree, USB headset). Single mono PCM control with 0..15 range.
SCONTENTS_DG60 = """Simple mixer control 'PCM',0
  Capabilities: pvolume pvolume-joined pswitch pswitch-joined
  Playback channels: Mono
  Limits: Playback 0 - 15
  Mono: Playback 11 [73%] [-4.00dB] [on]
Simple mixer control 'Mic',0
  Capabilities: cvolume cvolume-joined
  Capture channels: Mono
  Limits: Capture 0 - 31
  Mono: Capture 31 [100%] [31.00dB]
"""

# Built-in audio (BCM2835 Headphones). Stereo Master with dB scale.
SCONTENTS_BUILTIN = """Simple mixer control 'Master',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback -10239 - 400
  Mono:
  Front Left: Playback -2048 [76%] [-20.48dB] [on]
  Front Right: Playback -2048 [76%] [-20.48dB] [on]
"""

# Edge case: control whose [%] hint is missing — exerciser for our pct
# fallback formula.
SCONTENTS_NO_PCT = """Simple mixer control 'PCM',0
  Capabilities: pvolume pvolume-joined pswitch pswitch-joined
  Playback channels: Mono
  Limits: Playback 0 - 15
  Mono: Playback 11
"""

# /proc/asound/cards fixture
ASOUND_CARDS = """ 0 [Headphones     ]: bcm2835_headphon - bcm2835 Headphones
                      bcm2835 Headphones
 1 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi
                      vc4-hdmi
 2 [DG60           ]: USB-Audio - Avantree DG60
                      Avantree DG60 at usb-3f980000.usb-1.2, full speed
"""


class TestScontentsParser:
    def test_dg60_pcm_control(self) -> None:
        controls = alsa_mixer._parse_scontents(SCONTENTS_DG60)
        # PCM (playback) + Mic (capture). Mic gets returned because cvolume
        # is set; the route filter in _list_card_controls drops zero-range
        # controls but both PCM and Mic have a real range here.
        assert len(controls) == 2
        pcm = next(c for c in controls if c.name == "PCM")
        assert pcm.has_playback is True
        assert pcm.has_capture is False
        assert pcm.has_pswitch is True
        assert pcm.volume_min == 0
        assert pcm.volume_max == 15
        assert pcm.volume_raw == 11
        assert pcm.volume_pct == 73
        assert pcm.volume_db == -4.00
        assert pcm.muted is False

    def test_dg60_mic_control(self) -> None:
        controls = alsa_mixer._parse_scontents(SCONTENTS_DG60)
        mic = next(c for c in controls if c.name == "Mic")
        assert mic.has_playback is False
        assert mic.has_capture is True
        assert mic.volume_pct == 100
        assert mic.volume_db == 31.00

    def test_builtin_master(self) -> None:
        controls = alsa_mixer._parse_scontents(SCONTENTS_BUILTIN)
        assert len(controls) == 1
        master = controls[0]
        assert master.name == "Master"
        assert master.volume_min == -10239
        assert master.volume_max == 400
        # Front Left line wins (we take the first val line per control)
        assert master.volume_raw == -2048
        assert master.volume_pct == 76
        assert master.volume_db == -20.48

    def test_pct_fallback_when_amixer_omits_hint(self) -> None:
        controls = alsa_mixer._parse_scontents(SCONTENTS_NO_PCT)
        assert len(controls) == 1
        c = controls[0]
        # 11 / 15 ≈ 73.3 → round to 73
        assert c.volume_pct == 73

    def test_empty_input_returns_empty(self) -> None:
        assert alsa_mixer._parse_scontents("") == []


class TestProcAsoundParser:
    @pytest.mark.asyncio
    async def test_three_cards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: str, timeout: float = 4.0) -> str:
            assert "/proc/asound/cards" in cmd
            return ASOUND_CARDS

        monkeypatch.setattr(alsa_mixer, "_run", fake_run)
        cards = await alsa_mixer._list_cards_raw()
        # 3 entries — Headphones, vc4hdmi, DG60
        assert [c[1] for c in cards] == ["Headphones", "vc4hdmi", "DG60"]
        assert cards[2][0] == 2  # DG60 is card 2
        assert "Avantree DG60" in cards[2][2]

    @pytest.mark.asyncio
    async def test_empty_proc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run(cmd: str, timeout: float = 4.0) -> str:
            return ""

        monkeypatch.setattr(alsa_mixer, "_run", fake_run)
        cards = await alsa_mixer._list_cards_raw()
        assert cards == []


class TestListCardControls:
    @pytest.mark.asyncio
    async def test_drops_zero_range_controls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A control whose Limits line has min == max should be filtered
        # out (it's not actually a volume).
        canned = """Simple mixer control 'Routing',0
  Capabilities: pswitch
  Playback channels: Mono
  Limits: Playback 0 - 0
  Mono: Playback 0 [on]
Simple mixer control 'PCM',0
  Capabilities: pvolume pvolume-joined pswitch pswitch-joined
  Playback channels: Mono
  Limits: Playback 0 - 15
  Mono: Playback 11 [73%] [-4.00dB] [on]
"""

        async def fake_run(cmd: str, timeout: float = 4.0) -> str:
            return canned

        monkeypatch.setattr(alsa_mixer, "_run", fake_run)
        controls = await alsa_mixer._list_card_controls(0)
        # Routing dropped, only PCM remains
        assert [c.name for c in controls] == ["PCM"]


class TestSetControlValidation:
    @pytest.mark.asyncio
    async def test_rejects_empty_body(self) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await alsa_mixer.set_control(
                card="DG60",
                control="PCM",
                body=alsa_mixer.SetControlRequest(),
            )
        assert exc.value.status_code == 400
        assert "must provide" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_rejects_unknown_card(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi import HTTPException

        async def fake_run(cmd: str, timeout: float = 4.0) -> str:
            return ASOUND_CARDS  # DG60 exists, NotACard does not

        monkeypatch.setattr(alsa_mixer, "_run", fake_run)
        with pytest.raises(HTTPException) as exc:
            await alsa_mixer.set_control(
                card="NotACard",
                control="PCM",
                body=alsa_mixer.SetControlRequest(volume_pct=80),
            )
        assert exc.value.status_code == 404


class TestSetControlRequestSchema:
    def test_volume_in_range(self) -> None:
        # 0-100 inclusive
        alsa_mixer.SetControlRequest(volume_pct=0)
        alsa_mixer.SetControlRequest(volume_pct=100)

    def test_volume_out_of_range(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            alsa_mixer.SetControlRequest(volume_pct=-1)
        with pytest.raises(ValidationError):
            alsa_mixer.SetControlRequest(volume_pct=101)

    def test_muted_only(self) -> None:
        body = alsa_mixer.SetControlRequest(muted=True)
        assert body.muted is True
        assert body.volume_pct is None

    def test_extra_field_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            alsa_mixer.SetControlRequest.model_validate({"volume_pct": 50, "extra": "x"})


class TestPwNodeAlsaCardField:
    """Ensure the new alsa_card field flows from pw-dump → PwNode → API."""

    def test_parser_extracts_alsa_card_name(self) -> None:
        from phonon_stage.pipewire import cli

        objects: list[dict[str, Any]] = [
            {
                "id": 75,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "node.name": "alsa_output.usb-Avantree_DG60",
                        "media.class": "Audio/Sink",
                        "node.nick": "Avantree DG60",
                        "alsa.card_name": "Avantree DG60",
                        "alsa.card": "DG60",
                    },
                    "params": {},
                },
            }
        ]
        nodes = cli.parse_pw_dump_nodes(objects)
        assert len(nodes) == 1
        # alsa.card_name takes priority (full descriptive name)
        assert nodes[0]["alsa_card"] == "Avantree DG60"

    def test_parser_falls_back_to_alsa_card_short_name(self) -> None:
        from phonon_stage.pipewire import cli

        objects: list[dict[str, Any]] = [
            {
                "id": 75,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "node.name": "alsa_output.usb-x",
                        "media.class": "Audio/Sink",
                        "alsa.card": "DG60",
                    },
                    "params": {},
                },
            }
        ]
        nodes = cli.parse_pw_dump_nodes(objects)
        assert nodes[0]["alsa_card"] == "DG60"

    def test_parser_returns_empty_for_non_alsa_node(self) -> None:
        from phonon_stage.pipewire import cli

        objects: list[dict[str, Any]] = [
            {
                "id": 100,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "state": "running",
                    "props": {
                        "node.name": "bt_phone_in",
                        "media.class": "Audio/Sink",
                    },
                    "params": {},
                },
            }
        ]
        nodes = cli.parse_pw_dump_nodes(objects)
        assert nodes[0]["alsa_card"] == ""
