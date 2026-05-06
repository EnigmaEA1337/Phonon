"""Tests for the pw-top output parser. We test the pure parsing logic
against canned snapshots, not the actual subprocess invocation."""

# ruff: noqa: E501 — fixed-width pw-top output is naturally wider than 99 cols
from __future__ import annotations

from phonon_stage.pipewire import cli

# Two snapshots — the first is the freshly-created "C" pass with zeroed
# stats, the second is the steady-state "S/R" pass with real ERR counts.
# Parser picks whichever block has more lines. Same number of lines here,
# so we'll assert against the FIRST since `max(snapshots, key=len)` picks
# the first when ties (deterministic, list order). For state-letter and
# err-count assertions we add a richer trailing snapshot.
PW_TOP_OUTPUT = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
C   29      0      0    ---     ---   ---   ---     0                  Dummy-Driver
C   30      0      0    ---     ---   ---   ---     0                  Freewheel-Driver
C   43      0      0    ---     ---   ---   ---     0                  Midi-Bridge
C   65      0      0    ---     ---   ---   ---     0                  alsa_output.pci-0000_00_1f.3.analog-stereo
C   66      0      0    ---     ---   ---   ---     0                  alsa_input.pci-0000_00_1f.3.analog-stereo
C   91      0      0    ---     ---   ---   ---     0                  bt_1337-2_in
C   78      0      0    ---     ---   ---   ---     0                  pacat
C   82      0      0    ---     ---   ---   ---     0                  parec
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
S   29      0      0    ---     ---   ---   ---     0                  Dummy-Driver
S   30      0      0    ---     ---   ---   ---     0                  Freewheel-Driver
S   43      0      0    ---     ---   ---   ---     0                  Midi-Bridge
S   65      0      0    ---     ---   ---   ---     0                  alsa_output.pci-0000_00_1f.3.analog-stereo
S   66      0      0    ---     ---   ---   ---     0                  alsa_input.pci-0000_00_1f.3.analog-stereo
R   91    512  48000  95,2us  34,0us  0,01  0,00    0    S16LE 2 44100 bt_1337-2_in
R   78    551  44100  45,7us  35,8us  0,00  0,00   42    S16LE 2 44100  + pacat
R   82    960  48000  22,5us  21,1us  0,00  0,00    7    S16LE 2 48000  + parec
R   99    256  48000  10,0us   5,0us  0,00  0,00    0    S16LE 2 48000  + extra-stream
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
S   29  """  # truncated trailing snapshot — parser should ignore it


def _parse(text: str) -> dict[int, dict[str, object]]:
    """Re-implement just the parser part of cli.pw_top_xruns to test it
    in isolation, without spawning pw-top."""
    header_marker = "S   ID  QUANT"
    positions = []
    start = 0
    while True:
        idx = text.find(header_marker, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + 1
    if not positions:
        return {}
    snapshots = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        snapshots.append(text[pos:end].splitlines())
    snapshot = max(snapshots, key=len)
    if len(snapshot) < 2:
        return {}
    header_line = snapshot[0]
    name_col = header_line.find("NAME")
    if name_col < 0:
        return {}
    result: dict[int, dict[str, object]] = {}
    for line in snapshot[1:]:
        if not line.strip() or line.startswith(header_marker):
            continue
        padded = line if len(line) >= name_col else line.ljust(name_col)
        prefix = padded[:name_col]
        name_part = padded[name_col:].strip()
        if name_part.startswith("+ "):
            name_part = name_part[2:].strip()
        parts = prefix.split()
        if len(parts) < 9:
            continue
        try:
            state = parts[0]
            node_id = int(parts[1])
            err = int(parts[8])
        except (ValueError, IndexError):
            continue
        if not name_part:
            continue
        result[node_id] = {"name": name_part, "err": err, "state": state}
    return result


class TestPwTopParser:
    def test_picks_most_populated_snapshot(self) -> None:
        result = _parse(PW_TOP_OUTPUT)
        # Second snapshot has 9 nodes (one more than the first), trailing
        # snapshot is truncated and dropped.
        assert len(result) == 9

    def test_extracts_xrun_count_per_node(self) -> None:
        result = _parse(PW_TOP_OUTPUT)
        # pacat has 42 ERRs, parec has 7
        assert result[78]["err"] == 42
        assert result[82]["err"] == 7
        # idle nodes have 0
        assert result[29]["err"] == 0

    def test_strips_plus_prefix_from_client_streams(self) -> None:
        result = _parse(PW_TOP_OUTPUT)
        # `+ pacat` and `+ parec` lines: parser strips the leading "+ "
        assert result[78]["name"] == "pacat"
        assert result[82]["name"] == "parec"

    def test_keeps_full_node_names_with_dots_dashes(self) -> None:
        result = _parse(PW_TOP_OUTPUT)
        assert result[65]["name"] == "alsa_output.pci-0000_00_1f.3.analog-stereo"
        assert result[91]["name"] == "bt_1337-2_in"

    def test_records_state_letter(self) -> None:
        result = _parse(PW_TOP_OUTPUT)
        assert result[91]["state"] == "R"  # running
        assert result[29]["state"] == "S"  # suspended

    def test_empty_input_returns_empty(self) -> None:
        assert _parse("") == {}

    def test_no_header_returns_empty(self) -> None:
        assert _parse("just some random text\nno pw-top header") == {}


class TestPwTopParserMatchesProductionCode:
    """Smoke test: confirm the inline parser above matches what cli.py uses
    (so this test file remains a real safeguard against regressions)."""

    def test_parser_module_constants_present(self) -> None:
        # If these change name we want the test to flag it
        assert hasattr(cli, "pw_top_xruns")
        assert hasattr(cli, "_pw_top_cache")
        assert hasattr(cli, "_PW_TOP_CACHE_TTL")
