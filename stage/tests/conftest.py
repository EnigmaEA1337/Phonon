"""Shared test fixtures — fake backends, test client, deterministic clock."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from phonon_stage.audio.backend import AudioDevice
from phonon_stage.audio.fake import FakeAudioBackend
from phonon_stage.bluetooth.backend import BluetoothController
from phonon_stage.bluetooth.fake import FakeBluetoothBackend
from phonon_stage.clock import FakeClock
from phonon_stage.config import StageConfig
from phonon_stage.discovery.fake import FakeDiscoveryBackend
from phonon_stage.main import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# ── Sample data ──────────────────────────────────────────────────────────

SAMPLE_AUDIO_DEVICES = [
    AudioDevice(
        card_index=0,
        name="bcm2835 ALSA",
        id="bcm2835ALSA",
        driver="bcm2835_alsa",
        playback=True,
        capture=False,
    ),
    AudioDevice(
        card_index=1,
        name="Avantree DG60",
        id="DG60",
        driver="USB-Audio",
        playback=True,
        capture=True,
    ),
]

SAMPLE_BT_CONTROLLERS = [
    BluetoothController(
        address="B8:27:EB:92:29:E3",
        name="hci0",
        alias="stage-x01",
        powered=False,
        discovering=False,
    ),
    BluetoothController(
        address="00:01:95:4B:40:46",
        name="hci1",
        alias="stage-x01",
        powered=False,
        discovering=False,
    ),
]

# Known machine-id for deterministic stage_id in tests
KNOWN_MACHINE_ID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
# Precomputed: hashlib.sha256(b"a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4").hexdigest()[:8]
EXPECTED_STAGE_ID_SUFFIX = "cb0296e6"


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock(fixed=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))


@pytest.fixture()
def fake_audio() -> FakeAudioBackend:
    return FakeAudioBackend(devices=SAMPLE_AUDIO_DEVICES)


@pytest.fixture()
def fake_bt() -> FakeBluetoothBackend:
    return FakeBluetoothBackend(controllers=SAMPLE_BT_CONTROLLERS)


@pytest.fixture()
def fake_discovery() -> FakeDiscoveryBackend:
    return FakeDiscoveryBackend()


@pytest.fixture()
def machine_id_file(tmp_path: Path) -> Path:
    mid = tmp_path / "machine-id"
    mid.write_text(KNOWN_MACHINE_ID + "\n")
    return mid


@pytest.fixture()
def stage_config(machine_id_file: Path) -> StageConfig:
    return StageConfig(
        bind_address="127.0.0.1",
        port=8401,
        machine_id_path=machine_id_file,
    )


@pytest.fixture()
async def client(
    stage_config: StageConfig,
    fake_audio: FakeAudioBackend,
    fake_bt: FakeBluetoothBackend,
    fake_discovery: FakeDiscoveryBackend,
    fake_clock: FakeClock,
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        config=stage_config,
        audio_backend=fake_audio,
        bt_backend=fake_bt,
        discovery_backend=fake_discovery,
        clock=fake_clock,
    )

    # Manually trigger the ASGI lifespan (httpx ASGITransport doesn't do it)
    @asynccontextmanager
    async def lifespan_wrapper() -> AsyncIterator[None]:
        async with app.router.lifespan_context(app):
            yield

    async with lifespan_wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
