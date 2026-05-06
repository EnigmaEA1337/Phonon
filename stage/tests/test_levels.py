"""Tests for the levels module — peak metering math.

The actual `_read_peak` function spawns parec and reads bytes from it.
We test the math part (peak → dB → 0..1 mapping) by feeding it a
canned byte buffer via subprocess mock.
"""

from __future__ import annotations

import struct
from typing import Any

import pytest

from phonon_stage.api import levels


@pytest.fixture()
def _mock_subprocess(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Replace asyncio.create_subprocess_exec with a stub that yields the
    bytes we control."""
    holder: dict[str, bytes] = {"audio": b""}

    class _FakeStdout:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._pos = 0

        async def read(self, n: int) -> bytes:
            chunk = self._data[self._pos : self._pos + n]
            self._pos += len(chunk)
            return chunk

    class _FakeProc:
        def __init__(self, data: bytes) -> None:
            self.stdout = _FakeStdout(data)
            self.returncode: int | None = 0

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            return 0

    async def _fake_create_exec(*_args: Any, **_kw: Any) -> _FakeProc:
        return _FakeProc(holder["audio"])

    import asyncio as _aio

    monkeypatch.setattr(_aio, "create_subprocess_exec", _fake_create_exec)
    return holder


def _make_pcm(samples: list[int]) -> bytes:
    """Pack a list of int16 samples little-endian (parec format)."""
    return struct.pack(f"<{len(samples)}h", *samples)


class TestReadPeak:
    @pytest.mark.asyncio
    async def test_silence_returns_zero(self, _mock_subprocess: dict[str, bytes]) -> None:
        # 100 silent samples
        _mock_subprocess["audio"] = _make_pcm([0] * 100)
        peak = await levels._read_peak("test-sink")
        assert peak == 0.0

    @pytest.mark.asyncio
    async def test_unity_full_scale_maps_to_one(self, _mock_subprocess: dict[str, bytes]) -> None:
        # int16 max amplitude → 0 dB → 1.0
        _mock_subprocess["audio"] = _make_pcm([32767, -32768] * 50)
        peak = await levels._read_peak("test-sink")
        assert peak == pytest.approx(1.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_minus_6_db_maps_around_0_9(self, _mock_subprocess: dict[str, bytes]) -> None:
        # -6 dB = 0.501 linear; on the 0..1 scale where -60 dB = 0
        # and 0 dB = 1, that's (-6 + 60)/60 = 0.9
        amp = int(32767 * 0.501)  # ≈ 16410
        _mock_subprocess["audio"] = _make_pcm([amp, -amp] * 50)
        peak = await levels._read_peak("test-sink")
        assert peak == pytest.approx(0.9, abs=0.02)

    @pytest.mark.asyncio
    async def test_truncated_data_returns_zero(self, _mock_subprocess: dict[str, bytes]) -> None:
        # Only 2 bytes — not even a full sample
        _mock_subprocess["audio"] = b"\x00\x00"
        peak = await levels._read_peak("test-sink")
        assert peak == 0.0

    @pytest.mark.asyncio
    async def test_subprocess_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(*_args: Any, **_kw: Any) -> Any:
            raise OSError("can't spawn parec")

        import asyncio as _aio

        monkeypatch.setattr(_aio, "create_subprocess_exec", _boom)
        peak = await levels._read_peak("test-sink")
        # Catch-all returns 0.0 — the metering loop must not crash on
        # transient subprocess errors during a PW restart.
        assert peak == 0.0
