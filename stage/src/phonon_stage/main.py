"""Phonon Stage Agent — FastAPI application factory with dependency injection."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from phonon_stage.api.bluetooth import router as bluetooth_router
from phonon_stage.api.browse import router as browse_router
from phonon_stage.api.capabilities import router as capabilities_router
from phonon_stage.api.health import router as health_router
from phonon_stage.api.mappings import router as mappings_router
from phonon_stage.api.pipewire import router as pipewire_router
from phonon_stage.audio.real import RealAudioBackend
from phonon_stage.bluetooth.real import RealBluetoothBackend
from phonon_stage.clock import Clock, SystemClock
from phonon_stage.config import StageConfig, load_config
from phonon_stage.discovery.real import RealDiscoveryBackend
from phonon_stage.logging import configure_logging
from phonon_stage.mappings.service import MappingService
from phonon_stage.mappings.store import MappingStore
from phonon_stage.pipewire.real import RealPipeWireBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from phonon_stage.audio.backend import AudioBackend
    from phonon_stage.bluetooth.backend import BluetoothBackend
    from phonon_stage.discovery.backend import DiscoveryBackend
    from phonon_stage.pipewire.backend import PipeWireBackend

logger = structlog.get_logger()

# Static files directory (alongside this module)
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: StageConfig | None = None,
    audio_backend: AudioBackend | None = None,
    bt_backend: BluetoothBackend | None = None,
    discovery_backend: DiscoveryBackend | None = None,
    pw_backend: PipeWireBackend | None = None,
    mapping_service: MappingService | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Create the FastAPI application with dependency injection.

    Args:
        config: Stage configuration. None = load from default path.
        audio_backend: Audio device enumerator. None = real ALSA backend.
        bt_backend: Bluetooth enumerator. None = real BlueZ backend.
        discovery_backend: mDNS-SD announcer. None = real zeroconf backend.
        pw_backend: PipeWire graph manager. None = real PipeWire backend.
        mapping_service: Audio mapping orchestrator. None = create with real backends.
        clock: Clock implementation. None = system clock.

    Returns:
        Configured FastAPI application.
    """
    cfg = config or load_config()
    clk = clock or SystemClock()
    audio = audio_backend or RealAudioBackend()
    bt = bt_backend or RealBluetoothBackend()
    discovery = discovery_backend or RealDiscoveryBackend()
    pw = pw_backend or RealPipeWireBackend()
    store = MappingStore(cfg.standalone_conf_path)
    svc = mapping_service or MappingService(pw_backend=pw, store=store, clock=clk)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = cfg
        app.state.clock = clk
        app.state.start_time = clk.monotonic()
        app.state.audio_backend = audio
        app.state.bt_backend = bt
        app.state.discovery_backend = discovery
        app.state.pw_backend = pw
        app.state.mapping_service = svc

        await discovery.register(cfg.stage_id, cfg.bind_address, cfg.port)

        # Restore persisted mappings
        try:
            await svc.restore_mappings()
        except Exception:
            logger.warning("stage.mapping_restore_failed", exc_info=True)

        logger.info(
            "stage.started",
            stage_id=cfg.stage_id,
            bind_address=cfg.bind_address,
            port=cfg.port,
            mode=cfg.mode,
        )
        yield
        await discovery.unregister()
        logger.info("stage.stopped", stage_id=cfg.stage_id)

    app = FastAPI(
        title="Phonon Stage Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(capabilities_router)
    app.include_router(pipewire_router)
    app.include_router(mappings_router)
    app.include_router(bluetooth_router)
    app.include_router(browse_router)

    # Mount standalone mini-UI static files
    if _STATIC_DIR.exists():
        app.mount("/standalone", StaticFiles(directory=str(_STATIC_DIR / "standalone"), html=True))

    return app


def cli_entry() -> None:
    """Console script entry point for phonon-stage."""
    parser = argparse.ArgumentParser(description="Phonon Stage Agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/phonon/stage.yaml"),
        help="Path to stage.yaml config file",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    configure_logging(cfg.log_level)

    logger.info("stage.starting", config_path=str(args.config), stage_id=cfg.stage_id)

    app = create_app(config=cfg)
    uvicorn.run(
        app,
        host=cfg.bind_address,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
        loop="asyncio",  # Don't use uvloop — it breaks subprocess on Pi ARM64
    )
