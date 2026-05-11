"""Source plugin protocol and domain models.

A plugin wraps an upstream audio-source daemon (shairport-sync for
AirPlay, librespot for Spotify Connect, mpd, snapclient…) and exposes
a uniform control surface to Phonon: enable/disable, start/stop,
settings, and the PipeWire node it produces. The plugin itself never
moves audio — its job is to manage the daemon's lifecycle and surface
the PipeWire-side handle so the existing mapping engine can route it.

Plugins do NOT install packages at runtime: the curated set of
upstream daemons is pre-installed by `install.sh` at provisioning.
This keeps every plugin enable-able offline (Stages may be deployed
in venues without internet) and removes the need for a privileged
apt path from the daemon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class PluginRuntime:
    """What the plugin itself knows about its current state — derived
    from the systemd unit and the on-disk config. The PipeWire view
    (which nodes are exposed) is composed separately by the registry
    so the plugin stays decoupled from PW."""

    enabled: bool  # systemd unit enabled (autostart at user-session boot)
    running: bool  # systemd unit active right now
    last_error: str = ""  # most recent stderr/stdout snippet on failure, "" if healthy


@dataclass(frozen=True)
class PluginInfo:
    """Full API-facing snapshot of a plugin — static metadata + runtime
    state + the PipeWire nodes the daemon currently exposes."""

    name: str  # stable id, used in URLs: "airplay-v1", "spotify-connect"…
    title: str  # user-facing label: "AirPlay (Classic)"
    description: str  # one-line user-facing description
    family: str  # "source" | "sink" — forward-compat for outbound plugins
    enabled: bool
    running: bool
    pw_node_names: list[str] = field(default_factory=list)
    last_error: str = ""


class PluginSettings(BaseModel):
    """Base for per-plugin settings models. Subclasses define their fields
    with `model_config = ConfigDict(extra="forbid")` and `Field(...)`
    constraints. The plugin renders these into the daemon's native
    config file format when `put_settings` is called."""


class SourcePlugin(Protocol):
    """Protocol every source plugin implements.

    The lifecycle is intentionally narrow: enable/disable toggles the
    systemd unit's autostart, start/stop/restart drives the running
    state. `settings` reads/writes the daemon's config file and
    triggers a restart so changes take effect immediately.

    Static class attributes describe the plugin to the registry; the
    registry composes a full `PluginInfo` by joining these with the
    runtime state and the live PipeWire node list.
    """

    name: str
    title: str
    description: str
    family: str  # "source" for v1
    pw_node_pattern: str  # regex matched against PW node names — case-insensitive

    settings_model: type[PluginSettings]

    async def runtime(self) -> PluginRuntime: ...

    async def enable(self) -> None: ...

    async def disable(self) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def restart(self) -> None: ...

    async def get_settings(self) -> PluginSettings: ...

    async def put_settings(self, settings: PluginSettings) -> None: ...
