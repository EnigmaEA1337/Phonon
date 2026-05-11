"""Real PipeWire backend — manages audio graph via pw-cli/pw-link/wpctl."""

from __future__ import annotations

import contextlib
from pathlib import Path

import structlog

from phonon_stage.pipewire import cli
from phonon_stage.pipewire.backend import PwLink, PwNode, PwPort

logger = structlog.get_logger()


# Default filter-chain conf drop-in dir under the phonon user's XDG
# config. `pipewire -c filter-chain.conf` (run by filter-chain.service)
# loads every .conf in this directory at startup.
_DEFAULT_FILTER_CHAIN_DIR = Path.home() / ".config" / "pipewire" / "filter-chain.conf.d"

# Systemd user unit that hosts the filter-chain pipewire instance.
# Restarting it makes PW pick up new conf drop-ins and tear down
# removed ones. Live control updates DON'T go through here.
_FILTER_CHAIN_UNIT = "filter-chain.service"


class RealPipeWireBackend:
    """Manage PipeWire audio graph via CLI tools (pw-dump, pw-link, wpctl)."""

    def __init__(self, filter_chain_dir: Path | None = None) -> None:
        # Resolved at construction so tests/install scripts can point
        # us at a tmp dir without monkey-patching. Created lazily on
        # first write.
        self._fc_dir = filter_chain_dir or _DEFAULT_FILTER_CHAIN_DIR

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
                    alsa_card=str(n.get("alsa_card") or ""),
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
        # Invalidate cache so the freshly-created (or pre-existing) link is found
        cli._pw_dump_cache = []
        cli._pw_dump_cache_time = 0.0
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
        try:
            await cli.wpctl_set_volume(node_id, volume_linear)
            logger.info("pipewire.volume_set", node_id=node_id, volume=volume_linear)
        except Exception:
            # wpctl may fail on null-sinks, try pactl as fallback
            try:
                vol_pct = int(volume_linear * 100)
                await cli.run_command("pactl", "set-sink-volume", str(node_id), f"{vol_pct}%")
                logger.info("pipewire.volume_set_pactl", node_id=node_id, volume=vol_pct)
            except Exception:
                logger.warning("pipewire.volume_set_failed", node_id=node_id, exc_info=True)

    async def set_node_channel_volumes(self, node_name: str, channels: list[float]) -> None:
        """Set per-channel volumes on a PW node by NAME. pactl's
        sink/source ID namespace doesn't match PW node IDs, so
        passing `node_id` returned 'No such entity' on every call —
        confirmed live on the 3070. PW exposes each node to PA with
        the same name, so the name is the unambiguous handle."""
        if not channels:
            return
        pcts = [f"{int(max(0.0, min(2.0, v)) * 100)}%" for v in channels]
        # Try sink first (covers airplay_in null-sink, phonon_master,
        # all alsa_output.* sinks). If that fails it's likely a real
        # source — retry as source.
        try:
            await cli.run_command("pactl", "set-sink-volume", node_name, *pcts)
            logger.info(
                "pipewire.channel_volume_set",
                node_name=node_name,
                channels=pcts,
                kind="sink",
            )
            return
        except Exception:
            pass
        try:
            await cli.run_command("pactl", "set-source-volume", node_name, *pcts)
            logger.info(
                "pipewire.channel_volume_set",
                node_name=node_name,
                channels=pcts,
                kind="source",
            )
        except Exception:
            logger.warning(
                "pipewire.channel_volume_failed",
                node_name=node_name,
                channels=pcts,
                exc_info=True,
            )

    async def set_node_mute(self, node_id: int, muted: bool) -> None:
        # WirePlumber starts every fresh playback sink in MUTED state
        # (security default — avoids blasting audio at full volume the
        # moment a USB sound card is plugged in). We unmute when a
        # mapping points at it, otherwise the user gets a perfectly
        # routed link that produces silence.
        flag = "1" if muted else "0"
        try:
            await cli.run_command("wpctl", "set-mute", str(node_id), flag)
            logger.info("pipewire.mute_set", node_id=node_id, muted=muted)
        except Exception:
            try:
                pactl_flag = "1" if muted else "0"
                await cli.run_command("pactl", "set-sink-mute", str(node_id), pactl_flag)
                logger.info("pipewire.mute_set_pactl", node_id=node_id, muted=muted)
            except Exception:
                logger.warning("pipewire.mute_set_failed", node_id=node_id, exc_info=True)

    async def set_node_latency_offset(self, node_id: int, offset_ns: int) -> None:
        try:
            await cli.pw_cli_set_latency_offset(node_id, offset_ns)
            logger.info("pipewire.latency_offset_set", node_id=node_id, offset_ns=offset_ns)
        except Exception:
            logger.warning("pipewire.latency_offset_failed", node_id=node_id, exc_info=True)

    async def load_loopback(self, source: str, sink: str, latency_msec: int) -> int | None:
        """Load pactl module-loopback that buffers `latency_msec` ms of audio
        between `source` and `sink`. Returns the module id (int) on success,
        None on failure.

        `source` and `sink` are PA-style names. For an Audio/Source node
        (e.g. `alsa_input.usb-...`), pass the node name directly. For a
        null-sink whose monitor port is the readable side (e.g. our
        `bt_<name>_in` BT capture bridge), pass `<name>.monitor`. The
        caller decides — MappingService inspects the source node's
        media_class to figure that out.
        """
        try:
            out = await cli.run_command(
                "pactl",
                "load-module",
                "module-loopback",
                f"source={source}",
                f"sink={sink}",
                f"latency_msec={int(latency_msec)}",
            )
        except Exception:
            logger.warning(
                "pipewire.loopback_load_failed",
                source=source,
                sink=sink,
                latency_msec=latency_msec,
                exc_info=True,
            )
            return None
        out = out.strip()
        if not out.isdigit():
            logger.warning(
                "pipewire.loopback_load_unexpected_output",
                source=source,
                sink=sink,
                output=out,
            )
            return None
        mid = int(out)
        logger.info(
            "pipewire.loopback_loaded",
            module_id=mid,
            source=source,
            sink=sink,
            latency_msec=latency_msec,
        )
        return mid

    async def unload_module(self, module_id: int) -> None:
        """Unload a pactl module by id. No-op on failure (module may already
        be gone from a prior cleanup pass)."""
        try:
            await cli.run_command("pactl", "unload-module", str(module_id))
            logger.info("pipewire.module_unloaded", module_id=module_id)
        except Exception:
            logger.info("pipewire.module_unload_failed", module_id=module_id, exc_info=False)

    async def list_loopback_modules(self) -> dict[int, str]:
        """Parse `pactl list short modules` and return only module-loopback
        entries as {id: argument_string}. Returns an empty dict on failure
        — orphan-cleanup at startup is best-effort, not load-bearing."""
        try:
            out = await cli.run_command("pactl", "list", "short", "modules")
        except Exception:
            logger.info("pipewire.list_modules_failed", exc_info=False)
            return {}
        result: dict[int, str] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[1] != "module-loopback":
                continue
            try:
                mid = int(parts[0])
            except ValueError:
                continue
            result[mid] = parts[2]
        return result

    async def list_null_sink_modules(self) -> dict[int, str]:
        """Parse `pactl list short modules` and return module-null-sink
        entries as {module_id: sink_name}. Caller uses the sink_name
        to spot duplicates. Empty dict on parse failure — best-effort,
        same shape as list_loopback_modules."""
        try:
            out = await cli.run_command("pactl", "list", "short", "modules")
        except Exception:
            logger.info("pipewire.list_modules_failed", exc_info=False)
            return {}
        result: dict[int, str] = {}
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[1] != "module-null-sink":
                continue
            try:
                mid = int(parts[0])
            except ValueError:
                continue
            # Extract sink_name=<value> from the args column. Strict
            # match — anything else is noise we don't classify.
            for tok in parts[2].split():
                if tok.startswith("sink_name="):
                    result[mid] = tok.split("=", 1)[1]
                    break
        return result

    async def load_null_sink(self, name: str, description: str) -> int | None:
        """Load a pactl module-null-sink. Returns the module id on success,
        None on failure.

        `name` is the sink_name (used as PW node name too). The
        sink's monitor_FL/FR ports expose the audio in `direction=output`,
        which is what surfaces it as a routable source in the patch bay.
        `description` is the human-readable label (sink_properties =
        device.description=...) shown in the UI.
        """
        try:
            out = await cli.run_command(
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={name}",
                f"sink_properties=device.description={description}",
            )
        except Exception:
            logger.warning(
                "pipewire.null_sink_load_failed",
                name=name,
                description=description,
                exc_info=True,
            )
            return None
        out = out.strip()
        if not out.isdigit():
            logger.warning(
                "pipewire.null_sink_load_unexpected_output",
                name=name,
                output=out,
            )
            return None
        mid = int(out)
        logger.info("pipewire.null_sink_loaded", module_id=mid, name=name)
        return mid

    # ── Filter-chain (DSP plugin insert) ──────────────────────────

    def _filter_chain_conf_path(self, chain_name: str) -> Path:
        """Resolve the conf file path for a chain. The filename
        becomes the chain's identity from a deployment standpoint —
        same name means the previous chain gets overwritten on the
        next write."""
        # Sanitize aggressively: filter-chain conf files end up in
        # the user's XDG config; we don't want path traversal even
        # from a misconfigured caller.
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in chain_name)
        return self._fc_dir / f"phonon-{safe}.conf"

    async def write_filter_chain_conf(self, chain_name: str, conf_body: str) -> None:
        """Write or overwrite the conf file for a filter-chain. Does
        NOT reload — the caller batches multiple writes then calls
        `reload_filter_chain` once at the end."""
        self._fc_dir.mkdir(parents=True, exist_ok=True)
        path = self._filter_chain_conf_path(chain_name)
        path.write_text(conf_body, encoding="utf-8")
        logger.info("pipewire.filter_chain_conf_written", chain=chain_name, path=str(path))

    async def delete_filter_chain_conf(self, chain_name: str) -> None:
        path = self._filter_chain_conf_path(chain_name)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            logger.info("pipewire.filter_chain_conf_deleted", chain=chain_name, path=str(path))

    async def list_filter_chain_confs(self) -> list[str]:
        """Return the chain names of every phonon-owned conf file
        currently on disk. The `phonon-` filename prefix scopes us
        to files we wrote — any operator-managed conf in the same
        dir is left alone."""
        if not self._fc_dir.exists():
            return []
        names: list[str] = []
        for path in self._fc_dir.glob("phonon-*.conf"):
            stem = path.stem  # "phonon-<chain_name>"
            if stem.startswith("phonon-"):
                names.append(stem[len("phonon-") :])
        return names

    async def reload_filter_chain(self) -> None:
        """Restart the filter-chain.service systemd user unit. This
        glitches every running chain for ~200-300 ms — not for live
        param tweaks. Used only when a chain is added or removed."""
        try:
            await cli.run_command("systemctl", "--user", "restart", _FILTER_CHAIN_UNIT)
            logger.info("pipewire.filter_chain_reloaded", unit=_FILTER_CHAIN_UNIT)
        except Exception:
            logger.warning("pipewire.filter_chain_reload_failed", exc_info=True)

    async def set_filter_node_control(
        self, node_name: str, control_name: str, value: float
    ) -> None:
        """Live-update one filter-chain plugin control. Drives the
        zero-click delay change use case: with LSP's Ramping=1 the
        plugin interpolates `Time (ms)` over a few quanta instead of
        snapping, so the value can be retuned mid-playback. The
        equivalent change on a module-loopback's latency_msec would
        require a full module unload/reload.

        Resolves the node id from `list_nodes()` first because pw-cli
        set-param accepts ids, not names. If the node isn't there
        (chain unloaded, service still restarting), logs and bails."""
        try:
            nodes = await self.list_nodes()
        except Exception:
            logger.warning(
                "pipewire.filter_control_lookup_failed",
                node_name=node_name,
                exc_info=True,
            )
            return
        target = next((n for n in nodes if n.name == node_name), None)
        if target is None:
            logger.info(
                "pipewire.filter_control_node_missing",
                node_name=node_name,
                control=control_name,
            )
            return
        # pw-cli set-param Props expects spa-json. Quoting the control
        # name preserves whitespace and special chars in LSP labels
        # (e.g. "Time (ms)").
        payload = '{ params = [ "' + control_name + '" ' + str(float(value)) + " ] }"
        try:
            await cli.run_command("pw-cli", "set-param", str(target.id), "Props", payload)
            logger.info(
                "pipewire.filter_control_set",
                node_name=node_name,
                node_id=target.id,
                control=control_name,
                value=value,
            )
        except Exception:
            logger.warning(
                "pipewire.filter_control_set_failed",
                node_name=node_name,
                control=control_name,
                exc_info=True,
            )
