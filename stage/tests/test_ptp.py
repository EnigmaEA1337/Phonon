"""Tests for the PTP scaffold module — journalctl scrape parsing,
status response shape, mode/role detection from ptp4l logs."""

from __future__ import annotations

import re

import pytest

from phonon_stage.api import ptp

# ── Journal log parsing ───────────────────────────────────────────────────


def _parse_journal(text: str) -> tuple[str, int | None]:
    """Mirror of the parsing logic in ptp._read_journal_state. The real one
    shells out to journalctl; the regex semantics are tested here."""
    if not text.strip():
        return "unknown", None
    role = "unknown"
    offset: int | None = None
    if "selected local clock" in text or "assuming the grand master role" in text:
        role = "grandmaster"
    elif re.search(r"port \d+.*\bto SLAVE\b", text):
        role = "slave"
    elif re.search(r"port \d+.*\bto MASTER\b", text):
        role = "grandmaster"
    elif re.search(r"port \d+.*\bto LISTENING\b", text) or re.search(
        r"port \d+.*: INITIALIZING to LISTENING", text
    ):
        role = "listening"
    last_offset = None
    for m in re.finditer(r"master offset\s+(-?\d+)", text):
        last_offset = int(m.group(1))
    if last_offset is not None:
        offset = last_offset
    return role, offset


GRANDMASTER_LOG = """
ptp4l[12345]: [60.123] port 1 (eth0): INITIALIZING to LISTENING on INIT_COMPLETE
ptp4l[12345]: [60.456] port 0 (/var/run/ptp4l): INITIALIZING to LISTENING on INIT_COMPLETE
ptp4l[12345]: [68.789] port 1 (eth0): LISTENING to MASTER on ANNOUNCE_RECEIPT_TIMEOUT_EXPIRES
ptp4l[12345]: [68.789] selected local clock 96ed55.fffe.bf3a00 as best master
ptp4l[12345]: [68.789] port 1 (eth0): assuming the grand master role
"""

SLAVE_LOG = """
ptp4l[23456]: [102.111] port 1 (eth0): INITIALIZING to LISTENING on INIT_COMPLETE
ptp4l[23456]: [108.222] port 1 (eth0): LISTENING to UNCALIBRATED on RS_SLAVE
ptp4l[23456]: [109.333] port 1 (eth0): UNCALIBRATED to SLAVE on MASTER_CLOCK_SELECTED
ptp4l[23456]: [110.444] master offset       -42 s2 freq  +1234 path delay      512
ptp4l[23456]: [111.555] master offset        17 s2 freq  +1235 path delay      511
"""

LISTENING_LOG = """
ptp4l[34567]: [5.123] port 1 (eth0): INITIALIZING to LISTENING on INIT_COMPLETE
"""


class TestJournalParsing:
    def test_grandmaster(self) -> None:
        role, offset = _parse_journal(GRANDMASTER_LOG)
        assert role == "grandmaster"
        assert offset is None  # no master offset when we ARE the master

    def test_slave_with_offset(self) -> None:
        role, offset = _parse_journal(SLAVE_LOG)
        assert role == "slave"
        # Last master-offset reading wins (most current sync sample)
        assert offset == 17

    def test_listening(self) -> None:
        role, offset = _parse_journal(LISTENING_LOG)
        assert role == "listening"
        assert offset is None

    def test_empty(self) -> None:
        role, offset = _parse_journal("")
        assert role == "unknown"
        assert offset is None


# ── Detection helpers ─────────────────────────────────────────────────────


class TestDetectionHelpers:
    def test_ptp4l_installed_returns_string(self) -> None:
        # Either the dev box has ptp4l (returns a path) or not (returns "")
        path = ptp._ptp4l_installed()
        assert isinstance(path, str)
        # If returned, it must point to an existing file
        if path:
            from pathlib import Path

            assert Path(path).is_file()


# ── Status response shape ────────────────────────────────────────────────


class TestStatusEndpoint:
    @pytest.mark.asyncio
    async def test_status_when_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force "not installed" path
        monkeypatch.setattr(ptp, "_ptp4l_installed", lambda: "")
        result = await ptp.ptp_status()
        assert result.available is False
        assert result.running is False
        assert result.role == "n/a"
        assert result.offset_ns is None
        assert "not installed" in result.note.lower()

    @pytest.mark.asyncio
    async def test_status_when_installed_but_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ptp, "_ptp4l_installed", lambda: "/usr/sbin/ptp4l")

        async def _not_running() -> bool:
            return False

        async def _no_unit() -> bool:
            return False

        monkeypatch.setattr(ptp, "_ptp4l_running", _not_running)
        monkeypatch.setattr(ptp, "_service_unit_exists", lambda _name: _no_unit())
        result = await ptp.ptp_status()
        assert result.available is True
        assert result.running is False
        assert result.role == "n/a"
