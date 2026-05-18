"""Phonon Stage Agent — FastAPI application factory with dependency injection."""

from __future__ import annotations

import argparse
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from phonon_stage.api.aes67 import router as aes67_router
from phonon_stage.api.alsa_mixer import router as alsa_mixer_router
from phonon_stage.api.bluealsa_bridge import router as bluealsa_router
from phonon_stage.api.bluetooth import router as bluetooth_router
from phonon_stage.api.browse import router as browse_router
from phonon_stage.api.capabilities import router as capabilities_router
from phonon_stage.api.dsp import router as dsp_router
from phonon_stage.api.health import router as health_router
from phonon_stage.api.levels import router as levels_router
from phonon_stage.api.mappings import router as mappings_router
from phonon_stage.api.mixer import router as mixer_router
from phonon_stage.api.network import router as network_router
from phonon_stage.api.pipewire import router as pipewire_router
from phonon_stage.api.plugins import router as plugins_router
from phonon_stage.api.ptp import router as ptp_router
from phonon_stage.api.settings import router as settings_router
from phonon_stage.api.system import router as system_router
from phonon_stage.api.update import router as update_router
from phonon_stage.api.ws import router as ws_router
from phonon_stage.audio.real import RealAudioBackend
from phonon_stage.bluetooth.real import RealBluetoothBackend
from phonon_stage.clock import Clock, SystemClock
from phonon_stage.config import StageConfig, load_config
from phonon_stage.discovery.real import RealDiscoveryBackend
from phonon_stage.dsp.ladspa import LadspaIntrospector, RealLadspaIntrospector
from phonon_stage.logging import configure_logging
from phonon_stage.mappings.service import MappingService
from phonon_stage.mappings.store import MappingStore
from phonon_stage.mixer.service import MixerService
from phonon_stage.mixer.sessions import SessionStore
from phonon_stage.mixer.store import MixerStore
from phonon_stage.pipewire.real import RealPipeWireBackend
from phonon_stage.plugins.registry import PluginRegistry
from phonon_stage.plugins.system import RealSystemBackend, SystemBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from phonon_stage.audio.backend import AudioBackend
    from phonon_stage.bluetooth.backend import BluetoothBackend
    from phonon_stage.discovery.backend import DiscoveryBackend
    from phonon_stage.pipewire.backend import PipeWireBackend

logger = structlog.get_logger()

# Static files directory (alongside this module)
_STATIC_DIR = Path(__file__).parent / "static"


def _plugins_supported_on_host() -> bool:
    """Stub of the platform gate: only x86_64 Stages run LSP plugins
    in v1 (see CLAUDE.md memory project_plugins_scope). We check the
    architecture rather than try to invoke analyseplugin — that's a
    deployment problem (install.sh), not a runtime probe."""
    import platform

    return platform.machine() in {"x86_64", "amd64"}


def create_app(
    config: StageConfig | None = None,
    audio_backend: AudioBackend | None = None,
    bt_backend: BluetoothBackend | None = None,
    discovery_backend: DiscoveryBackend | None = None,
    pw_backend: PipeWireBackend | None = None,
    mapping_service: MappingService | None = None,
    clock: Clock | None = None,
    system_backend: SystemBackend | None = None,
    ladspa_introspector: LadspaIntrospector | None = None,
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
        system_backend: systemd + filesystem driver used by source plugins.
            None = real systemctl --user + atomic file writes.

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
    sysbe = system_backend or RealSystemBackend()
    # Each plugin's settings / runtime scratch lives under
    # <standalone-conf-dir>/plugins/<plugin-name>/ — same data root as
    # the rest of the Stage's persisted state, so backup tooling sees
    # one tree.
    plugin_data_root = cfg.standalone_conf_path.parent / "plugins"
    plugin_registry = PluginRegistry(
        system=sysbe, pw_backend=pw, plugin_data_root=plugin_data_root
    )
    # Mix console state lives next to mappings/plugins state — same
    # data dir, single backup tree.
    mixer_store = MixerStore(cfg.standalone_conf_path.parent / "mixer.conf.json")
    # Sessions store: named snapshots of mixer state (full or fx-only).
    # One JSON per session under <data-dir>/sessions/.
    session_store = SessionStore(cfg.standalone_conf_path.parent / "sessions")
    # LADSPA plugin introspector — wired only on hosts that can run
    # filter-chain LSP plugins (x86_64 + analyseplugin installed).
    # Pis stay with introspector=None, which makes set_output_insert
    # skip default-seeding and the /dsp endpoints return 503.
    intr: LadspaIntrospector | None = ladspa_introspector
    if intr is None and _plugins_supported_on_host():
        intr = RealLadspaIntrospector()
    mixer_service = MixerService(
        pw_backend=pw, store=mixer_store, introspector=intr,
        session_store=session_store,
    )

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
        app.state.system_backend = sysbe
        app.state.plugin_registry = plugin_registry
        app.state.mixer_service = mixer_service
        app.state.ladspa_introspector = intr

        await discovery.register(cfg.stage_id, cfg.bind_address, cfg.port)

        # Load persisted runtime settings (SAP/PTP/AES67 defaults)
        try:
            from phonon_stage.api import settings as settings_mod

            settings_mod.init(cfg.standalone_conf_path.parent)
        except Exception:
            logger.warning("stage.settings_init_failed", exc_info=True)

        # Load persisted network state (managed iface overrides + VLANs)
        try:
            from phonon_stage.api import network as network_mod

            network_mod.init(cfg.standalone_conf_path.parent)
        except Exception:
            logger.warning("stage.network_init_failed", exc_info=True)

        # Restore persisted mappings
        try:
            await svc.restore_mappings()
        except Exception:
            logger.warning("stage.mapping_restore_failed", exc_info=True)

        # Bring up the mix console: ensure phonon_master null-sink
        # exists, then apply the persisted state to PW. Idempotent.
        try:
            await mixer_service.init()
        except Exception:
            logger.warning("stage.mixer_init_failed", exc_info=True)

        # Source-plugin null-sinks (airplay_in, spotify_in) get wiped
        # whenever the user's pipewire session restarts — reboot or a
        # filter-chain.service reload cascade. The daemons stay
        # running but write into a sink that doesn't exist, so audio
        # silently dies until something recreates the module. Heal
        # now so the mixer reconcile that follows can wire them.
        try:
            heal_status = await plugin_registry.heal_null_sinks()
            logger.info("stage.plugin_null_sinks_healed", status=heal_status)
        except Exception:
            logger.warning("stage.plugin_null_sink_heal_failed", exc_info=True)

        # Re-run mixer reconcile now that the source null-sinks are
        # present so the source→master links get created.
        try:
            await mixer_service._reconcile()
        except Exception:
            logger.warning("stage.mixer_post_heal_reconcile_failed", exc_info=True)

        # Wire the mixer into the WS levels loop so it can meter the
        # master bus + every output on every tick. Cheap to set even
        # if init() above failed — the loop's `for o in svc.outputs`
        # is a no-op on an empty mixer.
        try:
            from phonon_stage.api.ws import set_mixer_service as _ws_set_mixer

            _ws_set_mixer(mixer_service)
        except Exception:
            logger.warning("stage.mixer_ws_wire_failed", exc_info=True)

        # Clean up stale bluealsa bridges from previous run + load per-bridge
        # user overrides (rate / period / channels / format / codec) so the
        # next auto-sync re-creates bridges with the configured params.
        try:
            from phonon_stage.api.bluealsa_bridge import (
                cleanup_stale_bridges,
                init_settings_store,
            )

            init_settings_store()
            await cleanup_stale_bridges()
        except Exception:
            logger.warning("stage.bluealsa_cleanup_failed", exc_info=True)

        # Wire the discovery backend into aes67 BEFORE restoring streams
        # so restore_existing_aes67 can push the MESH mDNS update if any
        # streams are reloaded. Otherwise mode stays STANDALONE forever
        # after a daemon restart, even with active AES67 streams.
        try:
            from phonon_stage.api.aes67 import set_discovery_backend, set_mapping_service

            set_discovery_backend(discovery)
            set_mapping_service(svc)
        except Exception:
            logger.warning("stage.aes67_wiring_failed", exc_info=True)

        # Restore AES67 streams from on-disk conf snippets — they're already
        # loaded by PipeWire, we just need to know about them in-memory.
        try:
            from phonon_stage.api.aes67 import restore_existing_aes67

            await restore_existing_aes67()
        except Exception:
            logger.warning("stage.aes67_restore_failed", exc_info=True)

        # Start SAP listener + announcer for AES67 stream discovery
        try:
            from phonon_stage.api.aes67 import (
                start_sap_announcer,
                start_sap_listener,
            )

            await start_sap_listener()
            await start_sap_announcer()
        except Exception:
            logger.warning("stage.sap_init_failed", exc_info=True)

        logger.info(
            "stage.started",
            stage_id=cfg.stage_id,
            bind_address=cfg.bind_address,
            port=cfg.port,
            mode=cfg.mode,
        )
        yield
        await discovery.unregister()
        # Tear down the spectrum sampler if any EQ panel ever asked
        # for a reading — kills the persistent parec captures cleanly.
        spectrum = getattr(app.state, "live_spectrum", None)
        if spectrum is not None:
            with contextlib.suppress(Exception):
                await spectrum.stop()
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
    app.include_router(system_router)
    app.include_router(bluealsa_router)
    app.include_router(alsa_mixer_router)
    app.include_router(levels_router)
    app.include_router(ws_router)
    app.include_router(aes67_router)
    app.include_router(ptp_router)
    app.include_router(settings_router)
    app.include_router(update_router)
    app.include_router(plugins_router)
    app.include_router(mixer_router)
    app.include_router(dsp_router)
    app.include_router(network_router)

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
