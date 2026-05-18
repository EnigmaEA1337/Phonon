"""Live FFT spectrum sampler for the Graphic EQ overlay.

Reads PCM continuously from a PipeWire monitor port via `parec` (PA
shim — same path the BT bridges use, no extra deps on the audio
side), keeps a ring buffer, and on each call to `get_spectrum_db`
runs an FFT on the most-recent window and maps the frequency bins
to the operator-requested band centers (ISO 1/3-octave by default).

Design:
  * One LiveSpectrum singleton per phonon-stage daemon.
  * Lazy parec spawn per `node_name` — when the first request comes
    in for `phonon_master.monitor` we open the capture; idle nodes
    are torn down after IDLE_TTL_SECONDS without a request.
  * numpy is imported lazily inside the methods. Hosts without it
    (Pi 3, no ladspa-sdk by project policy) get a clean RuntimeError
    which the API layer translates to 503.
  * Sample window is fixed at FFT_SIZE = 4096 stereo frames =
    ~85 ms at 48 kHz. Resolution = sample_rate / FFT_SIZE ≈ 11.7 Hz —
    fine enough for sub-100 Hz band differentiation, coarse enough
    that the FFT cost stays trivial on stage-x99.
  * For each requested band center we peak-pick across all FFT bins
    in the ±1/6-octave window around it (matches the 1/3-octave EQ
    band boundaries). Output is dBFS, capped at SPECTRUM_FLOOR_DB
    so we don't return -inf.

This module is intentionally self-contained — no PipeWire backend
dependency. The capture path goes through pacat/parec which Just
Works against whatever is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_DTYPE = "<i2"  # s16le
SAMPLE_BYTES = 2 * CHANNELS  # 4 bytes per stereo frame

# 8192 frames @ 48 kHz = ~170 ms window. FFT bin width = 48000/8192 ≈
# 5.86 Hz — fine enough that low-frequency bands (16 / 20 / 25 Hz)
# have 1-2 bins each to peak-pick from instead of falling back to
# the single-nearest-bin path. Was 4096 (11.7 Hz) which gave a
# coarse low-end readout.
FFT_SIZE = 8192

# Ring buffer holds 2x the FFT window so the snapshot read is always
# safe (the producer may be mid-write).
RING_SIZE = FFT_SIZE * 2

# After this many seconds without a get_spectrum_db request the
# parec capture is torn down. Operator closes the EQ panel → CPU
# spend on FFT capture stops cleanly.
IDLE_TTL_SECONDS = 5.0

# Floor for dBFS output. -90 dB ≈ s16 LSB noise floor; anything
# below this is silence as far as the UI is concerned.
SPECTRUM_FLOOR_DB = -90.0


class SpectrumUnavailableError(RuntimeError):
    """Raised when numpy isn't installed on this host. The API layer
    turns this into a 503 distinct from "device-not-found" 404s."""


# Back-compat alias — early code paths and tests imported the old
# name. Keep both pointing at the same class.
SpectrumUnavailable = SpectrumUnavailableError


@dataclass
class _NodeCapture:
    """Per-node state. ring is initialised lazily on first read so
    the dataclass default_factory doesn't import numpy on hosts that
    won't ever scan a spectrum."""

    node_name: str
    proc: asyncio.subprocess.Process | None = None
    reader_task: asyncio.Task[None] | None = None
    last_request_ts: float = field(default_factory=time.monotonic)
    # The ring buffer lives in numpy land; we store it as bytes here
    # so this dataclass stays import-free. _to_array converts on
    # demand inside the FFT path.
    buffer: bytearray = field(default_factory=lambda: bytearray(RING_SIZE * SAMPLE_BYTES))
    write_pos: int = 0  # in frames; wraps modulo RING_SIZE
    samples_seen: int = 0  # total frames written; lets us detect "no data yet"


class LiveSpectrum:
    """Async-safe live FFT sampler. One instance per daemon (stored
    on app.state)."""

    def __init__(self) -> None:
        self._captures: dict[str, _NodeCapture] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Kick off the idle reaper. Safe to call multiple times."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        """Tear down every capture + cancel the reaper."""
        if self._reaper_task is not None and not self._reaper_task.done():
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
        async with self._lock:
            for name in list(self._captures.keys()):
                await self._stop_capture_locked(name)

    async def get_spectrum_db(
        self, node_name: str, band_centers_hz: list[float]
    ) -> list[float]:
        """Return one dBFS value per requested band center. Spawns
        the parec capture for the node on first call.

        Raises SpectrumUnavailable if numpy isn't installed.
        """
        try:
            import numpy as np
        except ImportError as exc:
            raise SpectrumUnavailableError(
                "numpy not installed — install it on this host for the spectrum overlay"
            ) from exc

        async with self._lock:
            capture = self._captures.get(node_name)
            if capture is None:
                capture = _NodeCapture(node_name=node_name)
                self._captures[node_name] = capture
                await self._start_capture_locked(capture)
            capture.last_request_ts = time.monotonic()

        # Take a snapshot of the most-recent FFT_SIZE frames. We
        # don't need the lock for the read — bytearray slicing is
        # atomic enough for our pacing (writer is at ~150 KB/s,
        # reader at 20 Hz max).
        if capture.samples_seen < FFT_SIZE:
            # No usable data yet — return the floor so the UI shows
            # silence instead of garbage.
            return [SPECTRUM_FLOOR_DB] * len(band_centers_hz)
        snapshot = self._snapshot_window(capture)
        return self._fft_bands(snapshot, band_centers_hz)

    # ── Internals ──────────────────────────────────────────────────

    async def _start_capture_locked(self, capture: _NodeCapture) -> None:
        """Spawn parec for this node + the reader task that fills
        the ring buffer. Caller holds self._lock."""
        env = {
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
        }
        try:
            capture.proc = await asyncio.create_subprocess_exec(
                "parec",
                f"--device={capture.node_name}",
                "--format=s16le",
                f"--rate={SAMPLE_RATE}",
                f"--channels={CHANNELS}",
                "--latency-msec=40",
                "--raw",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except FileNotFoundError:
            logger.warning("spectrum.parec_missing", node=capture.node_name)
            raise
        capture.reader_task = asyncio.create_task(self._reader_loop(capture))
        logger.info("spectrum.capture_started", node=capture.node_name)

    async def _stop_capture_locked(self, node_name: str) -> None:
        capture = self._captures.pop(node_name, None)
        if capture is None:
            return
        if capture.reader_task is not None and not capture.reader_task.done():
            capture.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await capture.reader_task
        if capture.proc is not None and capture.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                capture.proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(capture.proc.wait(), timeout=2.0)
            if capture.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    capture.proc.kill()
                with contextlib.suppress(Exception):
                    await capture.proc.wait()
        logger.info("spectrum.capture_stopped", node=node_name)

    async def _reader_loop(self, capture: _NodeCapture) -> None:
        """Drain parec into the ring buffer until cancelled."""
        proc = capture.proc
        if proc is None or proc.stdout is None:
            return
        chunk_frames = 512  # ~10 ms of audio per read
        chunk_bytes = chunk_frames * SAMPLE_BYTES
        buf = capture.buffer
        try:
            while True:
                data = await proc.stdout.read(chunk_bytes)
                if not data:
                    return
                n_frames = len(data) // SAMPLE_BYTES
                if n_frames == 0:
                    continue
                # Write into the ring. Wrap if necessary.
                pos = capture.write_pos
                end = pos + n_frames
                if end <= RING_SIZE:
                    start_b = pos * SAMPLE_BYTES
                    buf[start_b : start_b + n_frames * SAMPLE_BYTES] = data[
                        : n_frames * SAMPLE_BYTES
                    ]
                else:
                    first = RING_SIZE - pos
                    second = n_frames - first
                    buf[pos * SAMPLE_BYTES : RING_SIZE * SAMPLE_BYTES] = data[
                        : first * SAMPLE_BYTES
                    ]
                    buf[: second * SAMPLE_BYTES] = data[
                        first * SAMPLE_BYTES : (first + second) * SAMPLE_BYTES
                    ]
                capture.write_pos = (pos + n_frames) % RING_SIZE
                capture.samples_seen += n_frames
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("spectrum.reader_loop_failed", node=capture.node_name, exc_info=True)

    def _snapshot_window(self, capture: _NodeCapture) -> bytes:
        """Return the last FFT_SIZE frames as a contiguous bytes
        object. Reads the ring in one or two slices depending on
        wrap position."""
        buf = capture.buffer
        write_pos = capture.write_pos
        start = (write_pos - FFT_SIZE) % RING_SIZE
        end_frame = start + FFT_SIZE
        if end_frame <= RING_SIZE:
            return bytes(buf[start * SAMPLE_BYTES : end_frame * SAMPLE_BYTES])
        first = RING_SIZE - start
        return (
            bytes(buf[start * SAMPLE_BYTES : RING_SIZE * SAMPLE_BYTES])
            + bytes(buf[: (FFT_SIZE - first) * SAMPLE_BYTES])
        )

    def _fft_bands(
        self,
        snapshot: bytes,
        band_centers_hz: list[float],
    ) -> list[float]:
        """Run FFT on the snapshot + return dBFS per requested band.
        Bands are peak-picked across the ±1/6-octave window so the
        readout matches what a 1/3-octave RTA would show."""
        import numpy as np  # local import keeps top-of-module clean

        samples = np.frombuffer(snapshot, dtype=np.int16).astype(np.float32)
        # Stereo → mono mix for the analyser. Average L+R, normalise
        # to ±1.0 so the dBFS reading is intuitive (0 dBFS = sin at
        # full scale).
        if CHANNELS == 2:
            samples = samples.reshape(-1, 2).mean(axis=1)
        samples /= 32768.0
        # Hann window — standard choice for visualisation FFTs;
        # cheap and the lobe leakage is acceptable for a meter.
        window = np.hanning(len(samples))
        windowed = samples * window
        spec = np.fft.rfft(windowed)
        mag = np.abs(spec) / (len(samples) / 2)
        freqs = np.fft.rfftfreq(len(samples), 1.0 / SAMPLE_RATE)

        results: list[float] = []
        log2_sixth = 1.0 / 6.0
        for center in band_centers_hz:
            if center <= 0:
                results.append(SPECTRUM_FLOOR_DB)
                continue
            lo = center * (2 ** -log2_sixth)
            hi = center * (2 ** log2_sixth)
            mask = (freqs >= lo) & (freqs <= hi)
            if not mask.any():
                # Sub-bin band (very low end) — fall back to the
                # nearest bin.
                idx = int(np.argmin(np.abs(freqs - center)))
                m = mag[idx]
            else:
                m = mag[mask].max()
            if m <= 0:
                results.append(SPECTRUM_FLOOR_DB)
                continue
            db = 20.0 * float(np.log10(m))
            results.append(max(SPECTRUM_FLOOR_DB, db))
        return results

    async def _reaper_loop(self) -> None:
        """Tear down captures whose last request was > IDLE_TTL ago.
        Runs forever until the daemon shuts down."""
        try:
            while True:
                await asyncio.sleep(IDLE_TTL_SECONDS)
                now = time.monotonic()
                async with self._lock:
                    stale = [
                        name
                        for name, cap in self._captures.items()
                        if (now - cap.last_request_ts) > IDLE_TTL_SECONDS
                    ]
                    for name in stale:
                        await self._stop_capture_locked(name)
        except asyncio.CancelledError:
            return
