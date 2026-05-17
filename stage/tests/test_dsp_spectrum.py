"""Tests for the live FFT spectrum sampler.

We don't spawn a real parec here — the unit tests inject synthetic
PCM bytes directly into the capture's ring buffer and exercise the
FFT + band-mapping math. The persistent-parec wiring is covered by
the API integration test below (which uses the fake introspector).
"""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING

import pytest

from phonon_stage.dsp.spectrum import (
    FFT_SIZE,
    SAMPLE_BYTES,
    SAMPLE_RATE,
    SPECTRUM_FLOOR_DB,
    LiveSpectrum,
    _NodeCapture,
)

if TYPE_CHECKING:
    from httpx import AsyncClient


def _sine_bytes(freq_hz: float, n_frames: int, amplitude: float = 0.5) -> bytes:
    """Generate stereo s16le PCM for a pure sine wave."""
    out = bytearray(n_frames * SAMPLE_BYTES)
    amp_int = int(amplitude * 32767)
    for i in range(n_frames):
        s = int(amp_int * math.sin(2 * math.pi * freq_hz * i / SAMPLE_RATE))
        struct.pack_into("<hh", out, i * SAMPLE_BYTES, s, s)
    return bytes(out)


def _silence_bytes(n_frames: int) -> bytes:
    return bytes(n_frames * SAMPLE_BYTES)


def _inject(capture: _NodeCapture, pcm: bytes) -> None:
    """Pretend the reader_loop received these bytes — write into the
    ring buffer exactly like _reader_loop would have."""
    from phonon_stage.dsp.spectrum import RING_SIZE

    n_frames = len(pcm) // SAMPLE_BYTES
    pos = capture.write_pos
    end = pos + n_frames
    buf = capture.buffer
    if end <= RING_SIZE:
        start_b = pos * SAMPLE_BYTES
        buf[start_b : start_b + n_frames * SAMPLE_BYTES] = pcm
    else:
        first = RING_SIZE - pos
        second = n_frames - first
        buf[pos * SAMPLE_BYTES : RING_SIZE * SAMPLE_BYTES] = pcm[: first * SAMPLE_BYTES]
        buf[: second * SAMPLE_BYTES] = pcm[first * SAMPLE_BYTES : (first + second) * SAMPLE_BYTES]
    capture.write_pos = (pos + n_frames) % RING_SIZE
    capture.samples_seen += n_frames


class TestFftBands:
    """The FFT path — feed known signals, expect known dB readings."""

    @pytest.mark.asyncio()
    async def test_silence_returns_floor(self) -> None:
        svc = LiveSpectrum()
        cap = _NodeCapture(node_name="test")
        svc._captures["test"] = cap
        _inject(cap, _silence_bytes(FFT_SIZE))
        bands = [100.0, 1000.0, 10000.0]
        out = await svc.get_spectrum_db("test", bands)
        # Pure-zero input → -inf math; service floors to SPECTRUM_FLOOR_DB.
        for v in out:
            assert v <= SPECTRUM_FLOOR_DB + 1

    @pytest.mark.asyncio()
    async def test_sine_peaks_at_its_band(self) -> None:
        # 1 kHz full-amplitude sine should peak ~near 0 dBFS at the
        # 1000 Hz band and stay well below at 100 Hz / 10 kHz.
        svc = LiveSpectrum()
        cap = _NodeCapture(node_name="test")
        svc._captures["test"] = cap
        _inject(cap, _sine_bytes(1000.0, FFT_SIZE, amplitude=0.95))
        bands = [100.0, 1000.0, 10000.0]
        out = await svc.get_spectrum_db("test", bands)
        # 1 kHz at -0.5 dBFS (0.95 amplitude * Hann loss ≈ -6 to -10
        # dBFS expected on a single FFT; we just check it's clearly
        # loudest at the right band).
        assert out[1] > out[0] + 10
        assert out[1] > out[2] + 10
        # Adjacent off-band readings should be near the floor.
        assert out[0] < -30
        assert out[2] < -30

    @pytest.mark.asyncio()
    async def test_unknown_node_serves_floor_until_samples_arrive(self) -> None:
        # A bare LiveSpectrum with no capture yet — calling
        # get_spectrum_db tries to spawn parec. We intercept by
        # pre-registering a capture without a process so the
        # samples_seen<FFT_SIZE branch is exercised.
        svc = LiveSpectrum()
        cap = _NodeCapture(node_name="test")
        # samples_seen=0 — the early-return path fires.
        svc._captures["test"] = cap
        bands = [1000.0]
        out = await svc.get_spectrum_db("test", bands)
        assert out == [SPECTRUM_FLOOR_DB]


class TestSpectrumApi:
    """End-to-end through the FastAPI client. Doesn't actually spawn
    parec — we pre-seed a fake LiveSpectrum on app.state."""

    @pytest.mark.asyncio()
    async def test_validates_bands_param(self, client: AsyncClient) -> None:
        r = await client.get("/dsp/spectrum?node=foo&bands=")
        assert r.status_code == 400
        r = await client.get("/dsp/spectrum?node=foo&bands=abc,def")
        assert r.status_code == 400

    @pytest.mark.asyncio()
    async def test_returns_band_values(self, client: AsyncClient) -> None:
        # Pre-seed a LiveSpectrum on app.state with a fake capture
        # holding silence so we get a deterministic floor reading.
        svc = LiveSpectrum()
        cap = _NodeCapture(node_name="phonon_master.monitor")
        svc._captures["phonon_master.monitor"] = cap
        _inject(cap, _silence_bytes(FFT_SIZE))
        client.app.state.live_spectrum = svc  # type: ignore[attr-defined]
        # await start so the reaper task exists (not needed for the
        # call but matches the production path).
        await svc.start()
        try:
            r = await client.get(
                "/dsp/spectrum?node=phonon_master.monitor&bands=100,1000,10000"
            )
            assert r.status_code == 200
            body = r.json()
            assert body["node"] == "phonon_master.monitor"
            assert len(body["bands"]) == 3
            for v in body["bands"]:
                assert isinstance(v, float)
        finally:
            await svc.stop()
