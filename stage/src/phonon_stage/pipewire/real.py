"""Real PipeWire backend — manages audio graph via pw-cli/pw-link/wpctl."""

from __future__ import annotations

import structlog

from phonon_stage.pipewire import cli
from phonon_stage.pipewire.backend import PwLink, PwNode, PwPort

logger = structlog.get_logger()


class RealPipeWireBackend:
    """Manage PipeWire audio graph via CLI tools (pw-dump, pw-link, wpctl)."""

    async def list_nodes(self) -> list[PwNode]:
        try:
            objects = await cli.pw_dump()
        except Exception as exc:
            import os

            logger.warning(
                "pipewire.pw_dump_failed",
                error=str(exc),
                xdg=os.environ.get("XDG_RUNTIME_DIR", "UNSET"),
                exc_info=True,
            )
            return []

        try:
            raw_nodes = cli.parse_pw_dump_nodes(objects)
            nodes = [
                PwNode(
                    id=int(n["id"]),
                    name=str(n["name"]),
                    media_class=str(n["media_class"]),
                    nick=str(n["nick"]),
                    state=str(n["state"]),
                    bt_codec=str(n.get("bt_codec") or ""),
                    bt_address=str(n.get("bt_address") or ""),
                    bt_profile=str(n.get("bt_profile") or ""),
                    latency_ms=float(n.get("latency_ms") or 0.0),
                    sample_rate=int(n.get("sample_rate") or 0),
                    channels=int(n.get("channels") or 0),
                )
                for n in raw_nodes
            ]
            logger.info("pipewire.nodes_listed", count=len(nodes))
            return nodes
        except Exception:
            logger.warning("pipewire.parse_nodes_failed", exc_info=True)
            return []

    async def list_ports(self, node_id: int | None = None) -> list[PwPort]:
        try:
            objects = await cli.pw_dump()
            raw_ports = cli.parse_pw_dump_ports(objects)
            ports = [
                PwPort(
                    id=int(p["id"]),
                    node_id=int(p["node_id"]),
                    name=str(p["name"]),
                    direction=str(p["direction"]),
                    alias=str(p["alias"]),
                )
                for p in raw_ports
            ]
            if node_id is not None:
                ports = [p for p in ports if p.node_id == node_id]
            logger.info("pipewire.ports_listed", count=len(ports), node_id=node_id)
            return ports
        except Exception:
            logger.warning("pipewire.list_ports_failed", exc_info=True)
            return []

    async def list_links(self) -> list[PwLink]:
        try:
            objects = await cli.pw_dump()
            raw_links = cli.parse_pw_dump_links(objects)
            links = [
                PwLink(
                    id=int(lk["id"]),
                    output_port_id=int(lk["output_port_id"]),
                    input_port_id=int(lk["input_port_id"]),
                    state=str(lk["state"]),
                )
                for lk in raw_links
            ]
            logger.info("pipewire.links_listed", count=len(links))
            return links
        except Exception:
            logger.warning("pipewire.list_links_failed", exc_info=True)
            return []

    async def create_link(self, output_port_id: int, input_port_id: int) -> PwLink:
        await cli.pw_link_create(output_port_id, input_port_id)
        logger.info(
            "pipewire.link_created",
            output_port_id=output_port_id,
            input_port_id=input_port_id,
        )
        # pw-link doesn't return the link ID, so we find it by querying
        links = await self.list_links()
        for link in links:
            if link.output_port_id == output_port_id and link.input_port_id == input_port_id:
                return link
        # Fallback: return a link with id=0 (will be resolved on next query)
        return PwLink(
            id=0, output_port_id=output_port_id, input_port_id=input_port_id, state="active"
        )

    async def destroy_link(self, link_id: int) -> None:
        await cli.pw_link_destroy(link_id)
        logger.info("pipewire.link_destroyed", link_id=link_id)

    async def set_node_volume(self, node_id: int, volume_linear: float) -> None:
        await cli.wpctl_set_volume(node_id, volume_linear)
        logger.info("pipewire.volume_set", node_id=node_id, volume=volume_linear)

    async def set_node_latency_offset(self, node_id: int, offset_ns: int) -> None:
        await cli.pw_cli_set_latency_offset(node_id, offset_ns)
        logger.info("pipewire.latency_offset_set", node_id=node_id, offset_ns=offset_ns)
