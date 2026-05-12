"""Plugin registry — instantiates the known set of plugins and joins
their runtime state with the live PipeWire view to produce the
`PluginInfo` consumed by the API.

The registry is the only place that knows which plugins exist; adding
a new one means appending to `_build_plugins`. No dynamic loading
(no entry-points, no plugin dirs) — the curated set is part of the
deployed binary and reviewed like any other code.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from phonon_stage.plugins.airplay_v1 import AirplayV1Plugin
from phonon_stage.plugins.backend import PluginInfo
from phonon_stage.plugins.spotify_v1 import SpotifyV1Plugin

if TYPE_CHECKING:
    from pathlib import Path

    from phonon_stage.pipewire.backend import PipeWireBackend
    from phonon_stage.plugins.backend import SourcePlugin
    from phonon_stage.plugins.system import SystemBackend

logger = structlog.get_logger()


class PluginNotFoundError(KeyError):
    """Raised when the API looks up a plugin name we don't know about."""


def _build_plugins(
    system: SystemBackend,
    pw_backend: PipeWireBackend,
    plugin_data_root: Path,
) -> list[SourcePlugin]:
    """Hardcoded list of known plugins. Adding one means:
      1. write the concrete class
      2. import it here
      3. instantiate it in this list
    Conf paths land under <plugin_data_root>/<plugin-name>/ so each
    plugin gets its own scratch area without colliding."""
    airplay_conf = plugin_data_root / "airplay-v1" / "shairport-sync.conf"
    spotify_env = plugin_data_root / "spotify-v1" / "spotifyd.conf"
    return [
        AirplayV1Plugin(system=system, pw_backend=pw_backend, conf_path=airplay_conf),
        SpotifyV1Plugin(system=system, pw_backend=pw_backend, conf_path=spotify_env),
    ]


class PluginRegistry:
    """Holds the instantiated plugins and serves status/lookup."""

    def __init__(
        self,
        system: SystemBackend,
        pw_backend: PipeWireBackend,
        plugin_data_root: Path,
    ) -> None:
        self._system = system
        self._pw = pw_backend
        plugins = _build_plugins(system, pw_backend, plugin_data_root)
        # Index by name for O(1) lookup from the API.
        self._by_name: dict[str, SourcePlugin] = {p.name: p for p in plugins}

    def list_names(self) -> list[str]:
        return list(self._by_name.keys())

    def get(self, name: str) -> SourcePlugin:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise PluginNotFoundError(name) from exc

    async def info(self, name: str) -> PluginInfo:
        """Build a full `PluginInfo` by joining plugin metadata + runtime
        + the live PipeWire node list filtered by the plugin's pattern."""
        plugin = self.get(name)
        runtime = await plugin.runtime()
        pw_nodes = await self._matching_pw_nodes(plugin.pw_node_pattern)
        return PluginInfo(
            name=plugin.name,
            title=plugin.title,
            description=plugin.description,
            family=plugin.family,
            enabled=runtime.enabled,
            running=runtime.running,
            pw_node_names=pw_nodes,
            last_error=runtime.last_error,
        )

    async def info_all(self) -> list[PluginInfo]:
        """List view — one call into PW for the whole batch instead of
        per-plugin, since pw-dump is the expensive part."""
        try:
            nodes = await self._pw.list_nodes()
            node_names = [n.name for n in nodes]
        except Exception:
            logger.warning("plugins.list_nodes_failed", exc_info=True)
            node_names = []
        out: list[PluginInfo] = []
        for plugin in self._by_name.values():
            try:
                runtime = await plugin.runtime()
            except Exception:
                logger.warning("plugins.runtime_failed", plugin=plugin.name, exc_info=True)
                # Plugin reports as down with an error rather than 500-ing
                # the whole list endpoint.
                out.append(
                    PluginInfo(
                        name=plugin.name,
                        title=plugin.title,
                        description=plugin.description,
                        family=plugin.family,
                        enabled=False,
                        running=False,
                        pw_node_names=[],
                        last_error="runtime check failed (see daemon logs)",
                    )
                )
                continue
            matched = _match(plugin.pw_node_pattern, node_names)
            out.append(
                PluginInfo(
                    name=plugin.name,
                    title=plugin.title,
                    description=plugin.description,
                    family=plugin.family,
                    enabled=runtime.enabled,
                    running=runtime.running,
                    pw_node_names=matched,
                    last_error=runtime.last_error,
                )
            )
        return out

    async def _matching_pw_nodes(self, pattern: str) -> list[str]:
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            logger.warning("plugins.match_pw_failed", pattern=pattern, exc_info=True)
            return []
        return _match(pattern, [n.name for n in nodes])


def _match(pattern: str, names: list[str]) -> list[str]:
    try:
        rx = re.compile(pattern)
    except re.error:
        # Bad pattern in a plugin definition is a code bug, but don't
        # nuke the API response over it — just log and return empty.
        logger.warning("plugins.bad_pattern", pattern=pattern, exc_info=True)
        return []
    return [n for n in names if rx.search(n)]
