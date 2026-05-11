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
from phonon_stage.mappings.service import MappingService
from phonon_stage.mappings.store import MappingStore
from phonon_stage.pipewire.backend import PwNode, PwPort
from phonon_stage.pipewire.fake import FakePipeWireBackend
from phonon_stage.plugins.system import FakeSystemBackend

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

SAMPLE_PW_NODES = [
    PwNode(
        id=30,
        name="alsa_output.bcm2835",
        media_class="Audio/Sink",
        nick="bcm2835 Headphones",
        state="idle",
    ),
    PwNode(
        id=31,
        name="alsa_input.usb-DG60",
        media_class="Audio/Source",
        nick="Avantree DG60",
        state="idle",
    ),
    PwNode(
        id=32,
        name="alsa_output.usb-DG60",
        media_class="Audio/Sink",
        nick="DG60 Output",
        state="idle",
    ),
]

SAMPLE_PW_PORTS = [
    PwPort(id=40, node_id=30, name="playback_FL", direction="input", alias="bcm2835:playback_FL"),
    PwPort(id=41, node_id=30, name="playback_FR", direction="input", alias="bcm2835:playback_FR"),
    PwPort(id=42, node_id=31, name="capture_FL", direction="output", alias="DG60:capture_FL"),
    PwPort(id=43, node_id=31, name="capture_FR", direction="output", alias="DG60:capture_FR"),
    PwPort(id=44, node_id=32, name="playback_FL", direction="input", alias="DG60:playback_FL"),
    PwPort(id=45, node_id=32, name="playback_FR", direction="input", alias="DG60:playback_FR"),
]

KNOWN_MACHINE_ID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
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
def fake_pw() -> FakePipeWireBackend:
    return FakePipeWireBackend(nodes=SAMPLE_PW_NODES, ports=SAMPLE_PW_PORTS)


@pytest.fixture(autouse=True)
def _reset_aes67_global_state(tmp_path: Path) -> None:
    """Tests share the aes67 module's _active_streams/_discovered_streams dicts
    by import. Reset them and point the conf dir at tmp so we don't bleed
    state from the developer's real ~/.config/pipewire."""
    from phonon_stage.api import aes67 as _a

    _a._active_streams.clear()
    _a._discovered_streams.clear()
    _a._CONF_DIR = tmp_path / "pipewire-test-conf.d"


@pytest.fixture()
def machine_id_file(tmp_path: Path) -> Path:
    mid = tmp_path / "machine-id"
    mid.write_text(KNOWN_MACHINE_ID + "\n")
    return mid


@pytest.fixture()
def stage_config(machine_id_file: Path, tmp_path: Path) -> StageConfig:
    return StageConfig(
        bind_address="127.0.0.1",
        port=8401,
        machine_id_path=machine_id_file,
        standalone_conf_path=tmp_path / "standalone.conf.json",
    )


@pytest.fixture()
def mapping_store(stage_config: StageConfig) -> MappingStore:
    return MappingStore(stage_config.standalone_conf_path)


@pytest.fixture()
def mapping_service(
    fake_pw: FakePipeWireBackend, mapping_store: MappingStore, fake_clock: FakeClock
) -> MappingService:
    return MappingService(pw_backend=fake_pw, store=mapping_store, clock=fake_clock)


@pytest.fixture()
def fake_system() -> FakeSystemBackend:
    return FakeSystemBackend()


@pytest.fixture()
async def client(
    stage_config: StageConfig,
    fake_audio: FakeAudioBackend,
    fake_bt: FakeBluetoothBackend,
    fake_discovery: FakeDiscoveryBackend,
    fake_pw: FakePipeWireBackend,
    mapping_service: MappingService,
    fake_clock: FakeClock,
    fake_system: FakeSystemBackend,
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        config=stage_config,
        audio_backend=fake_audio,
        bt_backend=fake_bt,
        discovery_backend=fake_discovery,
        pw_backend=fake_pw,
        mapping_service=mapping_service,
        clock=fake_clock,
        system_backend=fake_system,
    )

    @asynccontextmanager
    async def lifespan_wrapper() -> AsyncIterator[None]:
        async with app.router.lifespan_context(app):
            yield

    async with lifespan_wrapper():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
