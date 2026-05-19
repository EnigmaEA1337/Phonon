"""Tests for the AES67 module — SAP packet parsing, mode computation,
stream restoration from on-disk PipeWire conf snippets."""

from __future__ import annotations

import socket
import struct
from typing import TYPE_CHECKING

import pytest

from phonon_stage.api import aes67

if TYPE_CHECKING:
    from pathlib import Path

# ── SAP packet parsing ────────────────────────────────────────────────────


def _build_sap_packet(sdp: str, src_ip: str = "10.0.0.1", deletion: bool = False) -> bytes:
    """Construct a minimal SAP/SDP packet (RFC 2974)."""
    flags = 0x20  # V=1, A=0, R=0, T=0/announce, E=0, C=0
    if deletion:
        flags |= 0x04  # T = 1 = deletion
    auth_len = 0
    msg_id_hash = 0xBEEF
    src_addr = socket.inet_aton(src_ip)
    header = struct.pack("!BBH", flags, auth_len, msg_id_hash) + src_addr
    return header + b"application/sdp\x00" + sdp.encode("utf-8")


SAMPLE_SDP = (
    "v=0\r\n"
    "o=- 12345 1 IN IP4 10.0.0.1\r\n"
    "s=test-stream-out\r\n"
    "c=IN IP4 239.69.10.10/32\r\n"
    "t=0 0\r\n"
    "m=audio 5004 RTP/AVP 96\r\n"
    "a=rtpmap:96 L16/48000/2\r\n"
    "a=ptime:1\r\n"
)


class TestSapParsing:
    def test_parse_valid_announce(self) -> None:
        pkt = _build_sap_packet(SAMPLE_SDP, src_ip="10.0.0.1")
        result = aes67._parse_sap_packet(pkt, "10.0.0.1")
        assert result is not None
        assert result["name"] == "test-stream-out"
        assert result["multicast_group"] == "239.69.10.10"
        assert result["port"] == 5004
        assert result["channels"] == 2
        assert result["sample_rate"] == 48000
        assert result["audio_format"] == "S16BE"  # L16 → S16BE
        assert result["is_deletion"] is False
        assert result["source_ip"] == "10.0.0.1"

    def test_parse_deletion_flag(self) -> None:
        pkt = _build_sap_packet(SAMPLE_SDP, deletion=True)
        result = aes67._parse_sap_packet(pkt, "10.0.0.1")
        assert result is not None
        assert result["is_deletion"] is True

    def test_reject_truncated_packet(self) -> None:
        assert aes67._parse_sap_packet(b"\x20\x00", "10.0.0.1") is None

    def test_reject_ipv6(self) -> None:
        # A=1 means IPv6 address — not supported
        flags = 0x30  # V=1, A=1
        pkt = (
            struct.pack("!BBH", flags, 0, 0)
            + b"\x00" * 16
            + b"application/sdp\x00"
            + SAMPLE_SDP.encode()
        )
        assert aes67._parse_sap_packet(pkt, "::1") is None

    def test_sdp_l24_format(self) -> None:
        sdp = SAMPLE_SDP.replace("L16/48000/2", "L24/48000/2")
        pkt = _build_sap_packet(sdp)
        result = aes67._parse_sap_packet(pkt, "10.0.0.1")
        assert result is not None
        # We don't translate L24 to anything specific — pass through
        assert result["audio_format"] == "L24"

    def test_sdp_missing_required_fields(self) -> None:
        # No connection line
        bad = "v=0\r\ns=test\r\nm=audio 5004 RTP/AVP 96\r\n"
        pkt = _build_sap_packet(bad)
        # Returns None because we can't extract a multicast group
        assert aes67._parse_sap_packet(pkt, "10.0.0.1") is None


# ── Mode computation ──────────────────────────────────────────────────────


class TestModeComputation:
    def test_standalone_when_empty(self) -> None:
        aes67._active_streams.clear()
        assert aes67.current_mode() == "STANDALONE"

    def test_mesh_when_any_stream_present(self) -> None:
        aes67._active_streams.clear()
        aes67._active_streams["abc123"] = {
            "kind": "send",
            "name": "test",
            "multicast_group": "239.69.10.10",
            "port": 5004,
            "channels": 2,
            "sample_rate": 48000,
            "audio_format": "S16BE",
            "conf_path": "/tmp/x.conf",
        }
        assert aes67.current_mode() == "MESH"
        aes67._active_streams.clear()


# ── Restore from existing conf snippets ───────────────────────────────────


SEND_CONF = """context.modules = [
  { name = libpipewire-module-rtp-sink
    args = {
      destination.ip = 239.69.10.10
      destination.port = 5004
      net.ttl = 1
      net.loop = true
      sess.name = "aes67-send-myname"
      sess.min-ptime = 1
      sess.max-ptime = 1
      audio.format = S16BE
      audio.rate = 48000
      audio.channels = 2
      stream.props = {
        node.name = "aes67-send-myname"
        node.description = "AES67 Send myname"
        media.class = Audio/Sink
      }
    }
  }
]
"""

RECV_CONF = """context.modules = [
  { name = libpipewire-module-rtp-source
    args = {
      source.ip = 239.69.10.20
      source.port = 5004
      sess.latency.msec = 20
      sess.name = "aes67-recv-otherone"
      audio.format = S24BE
      audio.rate = 48000
      audio.channels = 2
      stream.props = {
        node.name = "aes67-recv-otherone"
        node.description = "AES67 Recv otherone"
        media.class = Audio/Source
      }
    }
  }
]
"""


class TestRecvConfRendering:
    """The receiver buffer wiring from Settings → conf snippet."""

    def test_recv_uses_settings_default_buffer(self) -> None:
        from phonon_stage.api import settings as _settings_mod

        _settings_mod._settings = _settings_mod.Settings()  # default 50 ms
        req = aes67.CreateStreamRequest(name="test")
        out = aes67._render_recv_conf(req, "aes67-recv-test")
        assert "sess.latency.msec = 50" in out

    def test_recv_per_stream_override_wins(self) -> None:
        req = aes67.CreateStreamRequest(name="test", recv_buffer_ms=120)
        out = aes67._render_recv_conf(req, "aes67-recv-test")
        assert "sess.latency.msec = 120" in out

    def test_recv_setting_change_propagates(self) -> None:
        from phonon_stage.api import settings as _settings_mod

        _settings_mod._settings = _settings_mod.Settings.model_validate(
            {
                "sap": _settings_mod.SapSettings().model_dump(),
                "ptp": _settings_mod.PtpSettings().model_dump(),
                "aes67": _settings_mod.Aes67Defaults(recv_buffer_ms=200).model_dump(),
            }
        )
        req = aes67.CreateStreamRequest(name="test")
        out = aes67._render_recv_conf(req, "aes67-recv-test")
        assert "sess.latency.msec = 200" in out
        _settings_mod._settings = _settings_mod.Settings()  # restore for other tests


class TestRestoreExistingAes67:
    @pytest.mark.asyncio
    async def test_restores_send_and_recv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Mock out the actual pipewire spawn — we just want to verify
        # the parsing and state-registration logic, not run real PW.
        async def fake_spawn(stream_id: str, conf_path: Path) -> int:
            return 99999  # bogus PID

        monkeypatch.setattr(aes67, "_spawn_stream_process", fake_spawn)
        # Also skip the legacy-conf migration step — it would try to read
        # _LEGACY_CONF_DIR (the real path) which we don't want touching.
        monkeypatch.setattr(aes67, "_migrate_legacy_confs", lambda: None)

        aes67._CONF_DIR = tmp_path
        aes67._active_streams.clear()
        (tmp_path / f"{aes67._CONF_PREFIX}aaa11111.conf").write_text(SEND_CONF)
        (tmp_path / f"{aes67._CONF_PREFIX}bbb22222.conf").write_text(RECV_CONF)
        await aes67.restore_existing_aes67()
        assert "aaa11111" in aes67._active_streams
        assert "bbb22222" in aes67._active_streams
        send = aes67._active_streams["aaa11111"]
        assert send["kind"] == "send"
        assert send["name"] == "myname"
        assert send["multicast_group"] == "239.69.10.10"
        assert send["port"] == 5004
        assert send["audio_format"] == "S16BE"
        assert send["pid"] == 99999  # spawn was called and pid recorded
        recv = aes67._active_streams["bbb22222"]
        assert recv["kind"] == "recv"
        assert recv["name"] == "otherone"
        assert recv["multicast_group"] == "239.69.10.20"
        assert recv["audio_format"] == "S24BE"
        aes67._active_streams.clear()

    @pytest.mark.asyncio
    async def test_no_conf_dir_is_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(aes67, "_migrate_legacy_confs", lambda: None)
        aes67._CONF_DIR = tmp_path / "missing"
        aes67._active_streams.clear()
        await aes67.restore_existing_aes67()  # should not raise
        assert aes67._active_streams == {}

    @pytest.mark.asyncio
    async def test_skips_stream_if_spawn_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If pipewire isn't installed (or the spawn fails for any other
        # OSError reason) we drop the stream rather than register a
        # zombie entry with no audio behind it.
        async def fake_spawn_fails(stream_id: str, conf_path: Path) -> int:
            raise FileNotFoundError("pipewire not installed")

        monkeypatch.setattr(aes67, "_spawn_stream_process", fake_spawn_fails)
        monkeypatch.setattr(aes67, "_migrate_legacy_confs", lambda: None)
        aes67._CONF_DIR = tmp_path
        aes67._active_streams.clear()
        (tmp_path / f"{aes67._CONF_PREFIX}aaa11111.conf").write_text(SEND_CONF)
        await aes67.restore_existing_aes67()
        assert aes67._active_streams == {}


class TestLegacyConfMigration:
    def test_moves_legacy_confs_to_new_dir(self, tmp_path: Path) -> None:
        # Set up the legacy and new dirs in the tmp scratch space.
        legacy = tmp_path / "legacy"
        new = tmp_path / "new"
        legacy.mkdir()
        (legacy / f"{aes67._CONF_PREFIX}abc123.conf").write_text(SEND_CONF)
        (legacy / "unrelated.conf").write_text("ignored")
        aes67._LEGACY_CONF_DIR = legacy
        aes67._CONF_DIR = new
        aes67._migrate_legacy_confs()
        assert (new / f"{aes67._CONF_PREFIX}abc123.conf").exists()
        assert not (legacy / f"{aes67._CONF_PREFIX}abc123.conf").exists()
        # Non-phonon confs in the legacy dir must NOT be touched.
        assert (legacy / "unrelated.conf").exists()

    def test_no_legacy_dir_is_quiet(self, tmp_path: Path) -> None:
        aes67._LEGACY_CONF_DIR = tmp_path / "nope"
        aes67._CONF_DIR = tmp_path / "new"
        aes67._migrate_legacy_confs()  # should not raise


# ── Discovered stream key uniqueness ──────────────────────────────────────


class TestDiscoveredStreamKeys:
    def test_key_is_per_endpoint_not_per_announcer(self) -> None:
        """Two announces of the same stream from the same source overwrite,
        not duplicate. Key format: source_ip:multicast:port."""
        pkt1 = _build_sap_packet(SAMPLE_SDP, src_ip="10.0.0.1")
        listener = aes67._SapListenerProtocol()
        listener.datagram_received(pkt1, ("10.0.0.1", 9875))
        listener.datagram_received(pkt1, ("10.0.0.1", 9875))
        # Should be exactly one entry, not two
        ours = [k for k in aes67._discovered_streams if "239.69.10.10" in k]
        assert len(ours) == 1
        aes67._discovered_streams.clear()

    def test_deletion_removes_entry(self) -> None:
        # Announce
        pkt_add = _build_sap_packet(SAMPLE_SDP, src_ip="10.0.0.1")
        listener = aes67._SapListenerProtocol()
        listener.datagram_received(pkt_add, ("10.0.0.1", 9875))
        assert any("239.69.10.10" in k for k in aes67._discovered_streams)
        # Delete
        pkt_del = _build_sap_packet(SAMPLE_SDP, src_ip="10.0.0.1", deletion=True)
        listener.datagram_received(pkt_del, ("10.0.0.1", 9875))
        assert not any("239.69.10.10" in k for k in aes67._discovered_streams)
        aes67._discovered_streams.clear()
