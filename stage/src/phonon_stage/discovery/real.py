"""Real discovery backend — mDNS-SD via zeroconf."""

from __future__ import annotations

import socket

import structlog
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from phonon_stage import __version__

logger = structlog.get_logger()

SERVICE_TYPE = "_phonon-stage._tcp.local."


class RealDiscoveryBackend:
    """Announce this Stage on the local network via mDNS-SD."""

    def __init__(self) -> None:
        self._azc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    async def register(self, stage_id: str, host: str, port: int) -> None:
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

    async def unregister(self) -> None:
        if self._azc and self._info:
            await self._azc.async_unregister_service(self._info)
            await self._azc.async_close()
            logger.info("discovery.unregistered")
