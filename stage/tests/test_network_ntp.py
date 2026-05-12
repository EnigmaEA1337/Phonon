"""Unit tests for the chronyc parsers in api/network.py.

Hardware-free: feeds sample chronyc output captured from real hosts
into the parsers and asserts the extracted fields. The parsers are
the only logic worth covering here — the async subprocess wrappers
go through the integration path on a real Stage.
"""

from __future__ import annotations

from phonon_stage.api.network import (
    _parse_chronyc_sources,
    _parse_chronyc_tracking,
    _parse_lastrx,
    _parse_us_or_ms,
)

# Sample captured from stage-x21 on 2026-05-12 — kept verbatim so
# whitespace quirks (multiple spaces, the [adj] bracket) get
# exercised by the parser.
TRACKING_OK = """\
Reference ID    : B97DBE7A (185.125.190.122)
Stratum         : 3
Ref time (UTC)  : Tue May 12 14:37:32 2026
System time     : 0.000148048 seconds slow of NTP time
Last offset     : -0.000165446 seconds
RMS offset      : 0.000099690 seconds
Frequency       : 3.443 ppm fast
Residual freq   : -0.005 ppm
Skew            : 0.150 ppm
Root delay      : 0.025 seconds
Root dispersion : 0.001 seconds
Update interval : 1024.6 seconds
Leap status     : Normal
"""

SOURCES_OK = """\
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^* 185.125.190.122               2  10   377   674   -544us[ -709us] +/-   14ms
^+ 185.125.190.123               2  10   377   672   -646us[ -646us] +/-   13ms
^- 91.189.91.112                 2  10   341   750  -1572us[-1736us] +/-   72ms
^- 91.189.91.113                 2  10   341    86  -4113us[-4113us] +/-   72ms
"""

# Edge: unreachable source — chrony marks state as '?' and shows zeros.
SOURCES_UNREACHABLE_TAIL = """\
MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
^? 10.0.0.1                      0   6     0     -     +0ns[   +0ns] +/-    0ns
"""


def test_tracking_extracts_signed_microseconds():
    p = _parse_chronyc_tracking(TRACKING_OK)
    assert p["reference_id"] == "B97DBE7A"
    assert p["reference_name"] == "185.125.190.122"
    assert p["stratum"] == 3
    # "0.000148048 seconds slow" → +148.048 us (slow = positive)
    assert abs(float(p["system_offset_us"]) - 148.048) < 0.01
    # "-0.000165446 seconds" → -165.446 us
    assert abs(float(p["last_offset_us"]) + 165.446) < 0.01
    assert abs(float(p["rms_offset_us"]) - 99.69) < 0.01
    # "3.443 ppm fast" → positive
    assert abs(float(p["frequency_ppm"]) - 3.443) < 0.001


def test_tracking_handles_fast_offset_sign():
    # "fast" → negative system_offset_us. Use a one-off sample.
    out = _parse_chronyc_tracking("System time     : 0.000200 seconds fast of NTP time\n")
    assert abs(float(out["system_offset_us"]) + 200.0) < 0.01


def test_tracking_handles_empty_reference_id():
    out = _parse_chronyc_tracking("Reference ID    : 00000000 ()\n")
    assert out["reference_id"] == "00000000"
    assert out["reference_name"] == ""


def test_sources_parses_four_servers():
    rows = _parse_chronyc_sources(SOURCES_OK)
    assert len(rows) == 4
    starred = [r for r in rows if r.state == "*"]
    assert len(starred) == 1
    assert starred[0].address == "185.125.190.122"
    assert starred[0].stratum == 2
    # reach 377 octal = 255 decimal (all 8 polls received)
    assert starred[0].reach == 0o377
    assert starred[0].last_rx_s == 674
    # -544us
    assert abs(starred[0].offset_us + 544.0) < 0.01
    # 14ms
    assert abs(starred[0].jitter_us - 14_000.0) < 0.01


def test_sources_marks_states_correctly():
    rows = _parse_chronyc_sources(SOURCES_OK)
    states = [r.state for r in rows]
    assert states == ["*", "+", "-", "-"]


def test_sources_handles_unreachable_zeros():
    rows = _parse_chronyc_sources(SOURCES_UNREACHABLE_TAIL)
    assert len(rows) == 1
    assert rows[0].state == "?"
    assert rows[0].reach == 0
    assert rows[0].offset_us == 0.0
    assert rows[0].jitter_us == 0.0


def test_sources_ignores_header_lines():
    # Header and separator lines must not parse as rows.
    text = "MS Name/IP address         Stratum Poll Reach LastRx\n" + "=" * 79 + "\n"
    assert _parse_chronyc_sources(text) == []


def test_lastrx_parses_units():
    assert _parse_lastrx("42") == 42
    assert _parse_lastrx("12m") == 720
    assert _parse_lastrx("3h") == 10_800
    assert _parse_lastrx("2d") == 172_800
    assert _parse_lastrx("-") == 0
    assert _parse_lastrx("") == 0


def test_us_or_ms_converts_units():
    assert _parse_us_or_ms("-544us") == -544.0
    assert _parse_us_or_ms("14ms") == 14_000.0
    assert _parse_us_or_ms("0.5s") == 500_000.0
    assert _parse_us_or_ms("100ns") == 0.1
    # Unknown unit → 0.0, not a crash
    assert _parse_us_or_ms("foo") == 0.0
    assert _parse_us_or_ms("") == 0.0
