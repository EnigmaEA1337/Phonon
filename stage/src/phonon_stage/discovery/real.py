"""Real discovery backend — mDNS-SD via zeroconf (register + browse)."""

from __future__ import annotations

import asyncio
import socket

import structlog
from zeroconf import IPVersion, ServiceInfo, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from phonon_stage import __version__
from phonon_stage.discovery.backend import DiscoveredStage

logger = structlog.get_logger()

SERVICE_TYPE = "_phonon-stage._tcp.local."


class RealDiscoveryBackend:
    """Announce this Stage and discover others on the local network via mDNS-SD."""

    def __init__(self) -> None:
        self._azc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._own_stage_id: str = ""

    async def register(self, stage_id: str, host: str, port: int) -> None:
        self._own_stage_id = stage_id
        self._azc = AsyncZeroconf(interfaces=[host], ip_version=IPVersion.V4Only)
        self._info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=f"{stage_id}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(host)],
            port=port,
            properties={
                "stage_id": stage_id,
                "version": __version__,
                "mode": "STANDALONE",
            },
        )
        await self._azc.async_register_service(self._info)
        logger.info(
            "discovery.registered",
            stage_id=stage_id,
            host=host,
            port=port,
            service_type=SERVICE_TYPE,
        )

    async def update_mode(self, mode: str) -> None:
        """Update the announced mode (e.g. STANDALONE → MESH) without re-creating the service."""
        if not self._azc or not self._info:
            return
        props = dict(self._info.properties or {})
        new_mode = mode.encode() if isinstance(mode, str) else mode
        if props.get(b"mode") == new_mode:
            return  # no change
        props[b"mode"] = new_mode
        self._info = ServiceInfo(
            type_=self._info.type,
            name=self._info.name,
            addresses=list(self._info.addresses),
            port=self._info.port or 0,
            properties=props,
            server=self._info.server,
        )
        await self._azc.async_update_service(self._info)
        logger.info("discovery.mode_updated", mode=mode)

    async def unregister(self) -> None:
        if self._azc and self._info:
            await self._azc.async_unregister_service(self._info)
            await self._azc.async_close()
            logger.info("discovery.unregistered")

    async def browse(self, timeout: float = 3.0) -> list[DiscoveredStage]:
        """Browse for other Phonon Stages on the network."""
        if not self._azc:
            return []

        found: list[DiscoveredStage] = []
        seen_names: set[str] = set()
        loop = asyncio.get_running_loop()

        async def _resolve(service_type: str, name: str) -> None:
            info = AsyncServiceInfo(service_type, name)
            ok = await info.async_request(self._azc.zeroconf, 2000)  # type: ignore[union-attr]
            if not ok:
                return
            props = {
                k.decode() if isinstance(k, bytes) else k: v.decode()
                if isinstance(v, bytes)
                else v
                for k, v in (info.properties or {}).items()
            }
            sid = str(props.get("stage_id", ""))
            if sid == self._own_stage_id:
                return
            addresses = info.parsed_addresses()
            host = addresses[0] if addresses else ""
            found.append(
                DiscoveredStage(
                    stage_id=sid,
                    host=host,
                    port=info.port or 0,
                    version=str(props.get("version", "")),
                    mode=str(props.get("mode", "")),
                )
            )

        def on_state_change(
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            if state_change != ServiceStateChange.Added or name in seen_names:
                return
            seen_names.add(name)
            asyncio.run_coroutine_threadsafe(_resolve(service_type, name), loop)

        browser = AsyncServiceBrowser(self._azc.zeroconf, SERVICE_TYPE, handlers=[on_state_change])
        await asyncio.sleep(timeout)
        await browser.async_cancel()

        logger.info("discovery.browse_complete", found=len(found))
        return found
