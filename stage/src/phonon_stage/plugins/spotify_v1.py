"""Spotify Connect plugin — wraps spotifyd.

spotifyd is a headless Spotify Connect daemon (Rust) built on top
of librespot. We use it instead of plain librespot because:

  * spotifyd ships prebuilt binaries for x86_64 / aarch64 / armv7
    in every GitHub release; librespot's release artifacts have
    been source-only since v0.4. No Rust toolchain needed at
    install time.
  * The TOML config file model fits our render/parse pattern more
    naturally than librespot's CLI-args-only approach.
  * Featureset for our use case (zeroconf-discovered Connect speaker
    writing to a PulseAudio sink) is identical. MPRIS / on_song_change
    hooks / keyring extras are unused — Phonon does control + routing.

Routing model
-------------

Same as airplay_v1: spotifyd's `device = "spotify_in"` pins its
output to a dedicated null-sink we own; its monitor port surfaces
as a routable source in the patch bay alongside `airplay_in` /
`bt_<name>_in`. No auto-routing to the default sink.

Daemon control
--------------

spotifyd reads its config from a TOML file (`spotifyd.conf`) at the
path passed via `--config-path`. The plugin renders settings into
that file and bounces the unit on changes via `systemctl --user
restart`. The systemd unit's `ConditionPathExists=/usr/bin/spotifyd`
keeps the unit inert when the binary is missing.

User experience
---------------

  1. Operator enables the plugin in the Phonon UI
  2. spotifyd appears in any Spotify mobile/desktop app's Connect
     picker (no login on the Stage — zeroconf delegation)
  3. Premium account required on the controlling app (Spotify
     business rule, not a plugin limitation)
  4. Audio routes through `spotify_in` → user maps it in the Mix
     Console like any other source
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field

from phonon_stage.plugins.backend import PluginRuntime, PluginSettings

if TYPE_CHECKING:
    from pathlib import Path

    from phonon_stage.pipewire.backend import PipeWireBackend
    from phonon_stage.plugins.system import SystemBackend


NULL_SINK_NAME = "spotify_in"
NULL_SINK_DESCRIPTION = "Spotify-In"


class SpotifyV1Settings(PluginSettings):
    """User-tunable surface for the Spotify Connect receiver.

    Field names mirror the Phonon-side semantics; the renderer maps
    them onto spotifyd's TOML keys (which use slightly different
    naming: `device_name` vs our `name`, `audio_format` vs `format`,
    `no_audio_cache` vs `disable_audio_cache`). The mapping is
    centralised in `_render_conf` / `_parse_conf` so the API surface
    stays stable if we ever swap daemons again."""

    model_config = ConfigDict(extra="forbid")

    # ── Identity ────────────────────────────────────────────────
    name: str = Field(
        default="Phonon",
        min_length=1,
        max_length=63,
        description="Name visible to Spotify clients in their Connect picker.",
    )
    device_type: Literal[
        "computer",
        "tablet",
        "smartphone",
        "speaker",
        "tv",
        "avr",
        "stb",
        "audiodongle",
    ] = Field(
        default="speaker",
        description="Device-type hint shown to Spotify clients (icon + label).",
    )

    # ── Audio quality ───────────────────────────────────────────
    bitrate: Literal[96, 160, 320] = Field(
        default=320,
        description="Spotify stream bitrate in kbps. 320 = high quality.",
    )
    format: Literal["F32", "F64", "S32", "S24", "S24_3", "S16"] = Field(
        default="F32",
        description="Sample format written into the PA sink.",
    )
    initial_volume: int = Field(
        default=50,
        ge=0,
        le=100,
        description=(
            "Volume spotifyd applies to its output (0-100). The Phonon "
            "mixer's source gain is independent and stacks on top."
        ),
    )

    # ── Behaviour ───────────────────────────────────────────────
    autoplay: bool = Field(
        default=True,
        description=(
            "When the current track ends and the user's queue is empty, "
            "ask Spotify to autoplay similar music."
        ),
    )
    disable_audio_cache: bool = Field(
        default=True,
        description=(
            "Disable on-disk caching of decoded audio. Default ON because "
            "Phonon Stages aren't meant to hold a music library, and the "
            "cache grows unbounded under heavy use."
        ),
    )

    # ── Discovery ───────────────────────────────────────────────
    zeroconf_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description=(
            "TCP port spotifyd's mDNS responder binds to. 0 = the kernel picks a free one."
        ),
    )


class SpotifyV1Plugin:
    """Concrete `SourcePlugin` implementation for spotifyd."""

    name: str = "spotify-v1"
    title: str = "Spotify Connect"
    description: str = (
        "Spotify Connect receiver via spotifyd. Appears in the Spotify "
        "app's device picker (Premium account required)."
    )
    family: str = "source"
    pw_node_pattern: str = rf"(?i){NULL_SINK_NAME}"

    settings_model: type[PluginSettings] = SpotifyV1Settings

    UNIT: str = "spotifyd.service"

    def __init__(
        self,
        system: SystemBackend,
        pw_backend: PipeWireBackend,
        conf_path: Path,
    ) -> None:
        self._system = system
        self._pw = pw_backend
        # TOML config path read by spotifyd.service via --config-path.
        self._conf_path = conf_path

    # ── Lifecycle ──────────────────────────────────────────────

    async def runtime(self) -> PluginRuntime:
        enabled = await self._system.systemctl_is_enabled(self.UNIT)
        running = await self._system.systemctl_is_active(self.UNIT)
        last_error = ""
        if enabled and not running:
            last_error = await self._system.systemctl_status_stderr(self.UNIT)
        return PluginRuntime(enabled=enabled, running=running, last_error=last_error)

    async def enable(self) -> None:
        if not self._system.file_exists(self._conf_path):
            await self.put_settings(SpotifyV1Settings())
        await self._ensure_null_sink()
        await self._system.systemctl_enable(self.UNIT)

    async def disable(self) -> None:
        await self._system.systemctl_stop(self.UNIT)
        await self._system.systemctl_disable(self.UNIT)
        await self._remove_null_sink()

    async def start(self) -> None:
        if not self._system.file_exists(self._conf_path):
            await self.put_settings(SpotifyV1Settings())
        await self._ensure_null_sink()
        await self._system.systemctl_start(self.UNIT)

    async def stop(self) -> None:
        await self._system.systemctl_stop(self.UNIT)

    async def restart(self) -> None:
        await self._ensure_null_sink()
        await self._system.systemctl_restart(self.UNIT)

    # ── Settings ───────────────────────────────────────────────

    async def get_settings(self) -> SpotifyV1Settings:
        if not self._system.file_exists(self._conf_path):
            return SpotifyV1Settings()
        try:
            raw = self._system.read_text(self._conf_path)
        except Exception:
            return SpotifyV1Settings()
        return self._parse_conf(raw)

    async def put_settings(self, settings: PluginSettings) -> None:
        if not isinstance(settings, SpotifyV1Settings):
            msg = (
                f"SpotifyV1Plugin.put_settings expected SpotifyV1Settings, "
                f"got {type(settings).__name__}"
            )
            raise TypeError(msg)
        rendered = self._render_conf(settings)
        self._system.write_text_atomic(self._conf_path, rendered, mode=0o644)
        if await self._system.systemctl_is_active(self.UNIT):
            await self._system.systemctl_restart(self.UNIT)

    # ── Null-sink management ───────────────────────────────────

    async def _ensure_null_sink(self) -> None:
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            nodes = []
        if any(n.name == NULL_SINK_NAME for n in nodes):
            return
        await self._pw.load_null_sink(NULL_SINK_NAME, NULL_SINK_DESCRIPTION)

    async def _remove_null_sink(self) -> None:
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            return
        if not any(n.name == NULL_SINK_NAME for n in nodes):
            return
        await self._unload_null_sink_by_name()

    async def _unload_null_sink_by_name(self) -> None:
        from phonon_stage.pipewire.fake import FakePipeWireBackend

        if isinstance(self._pw, FakePipeWireBackend):
            mid = next(
                (m for m, (n, _) in self._pw.null_sinks.items() if n == NULL_SINK_NAME),
                None,
            )
            if mid is not None:
                await self._pw.unload_module(mid)
            return
        await _real_unload_null_sink_by_name(self._pw)

    # ── TOML config rendering / parsing ────────────────────────

    @staticmethod
    def _render_conf(s: SpotifyV1Settings) -> str:
        """Render settings into spotifyd's TOML format.

        Mapping (Phonon → spotifyd):
          name                  → device_name
          format                → audio_format
          disable_audio_cache   → no_audio_cache
          (other keys match 1:1)
        """
        return (
            "# Generated by phonon-stage SpotifyV1Plugin. Do not edit by hand —\n"
            "# changes are overwritten on the next /plugins/spotify-v1/settings PUT.\n"
            "[global]\n"
            'backend = "pulseaudio"\n'
            f'device = "{NULL_SINK_NAME}"\n'
            f'device_name = "{_toml_escape(s.name)}"\n'
            f'device_type = "{s.device_type}"\n'
            f"bitrate = {s.bitrate}\n"
            f'audio_format = "{s.format}"\n'
            f'initial_volume = "{s.initial_volume}"\n'
            f"autoplay = {_toml_bool(s.autoplay)}\n"
            f"no_audio_cache = {_toml_bool(s.disable_audio_cache)}\n"
            f"zeroconf_port = {s.zeroconf_port}\n"
        )

    @staticmethod
    def _parse_conf(raw: str) -> SpotifyV1Settings:
        """Reverse-parse the TOML body into a SpotifyV1Settings. A
        bad / missing key falls back to the model default rather
        than rejecting the whole file — small forward-compat cushion
        if a future spotifyd version adds keys we don't model yet."""
        defaults = SpotifyV1Settings()
        out: dict[str, object] = {}
        name = _extract_string(raw, "device_name")
        if name is not None:
            out["name"] = name
        dtype = _extract_string(raw, "device_type")
        if dtype is not None:
            out["device_type"] = dtype
        br = _extract_int(raw, "bitrate")
        if br is not None:
            out["bitrate"] = br
        fmt = _extract_string(raw, "audio_format")
        if fmt is not None:
            out["format"] = fmt
        # spotifyd quotes initial_volume as a string in TOML
        iv = _extract_string(raw, "initial_volume")
        if iv is not None:
            with contextlib.suppress(ValueError):
                out["initial_volume"] = int(iv)
        ap = _extract_bool(raw, "autoplay")
        if ap is not None:
            out["autoplay"] = ap
        nc = _extract_bool(raw, "no_audio_cache")
        if nc is not None:
            out["disable_audio_cache"] = nc
        zp = _extract_int(raw, "zeroconf_port")
        if zp is not None:
            out["zeroconf_port"] = zp
        try:
            return SpotifyV1Settings(**out)
        except Exception:
            return defaults


def _toml_bool(b: bool) -> str:
    return "true" if b else "false"


def _toml_escape(s: str) -> str:
    """Escape a string for a TOML double-quoted value."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


_STRING_RE_TMPL = r'^\s*{key}\s*=\s*"((?:[^"\\]|\\.)*)"\s*$'
_INT_RE_TMPL = r"^\s*{key}\s*=\s*(-?\d+)\s*$"
_BOOL_RE_TMPL = r"^\s*{key}\s*=\s*(true|false)\s*$"


def _extract_string(raw: str, key: str) -> str | None:
    m = re.search(_STRING_RE_TMPL.format(key=re.escape(key)), raw, re.MULTILINE)
    if not m:
        return None
    return m.group(1).replace('\\"', '"').replace("\\\\", "\\")


def _extract_int(raw: str, key: str) -> int | None:
    m = re.search(_INT_RE_TMPL.format(key=re.escape(key)), raw, re.MULTILINE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _extract_bool(raw: str, key: str) -> bool | None:
    m = re.search(_BOOL_RE_TMPL.format(key=re.escape(key)), raw, re.MULTILINE)
    if not m:
        return None
    return m.group(1) == "true"


async def _real_unload_null_sink_by_name(pw: PipeWireBackend) -> None:
    """Locate our null-sink module by sink_name in pactl's module list
    and unload it. Errors are logged, never raised."""
    import structlog

    log = structlog.get_logger()
    try:
        from phonon_stage.pipewire import cli

        out = await cli.run_command("pactl", "list", "short", "modules")
    except Exception:
        log.warning("plugins.spotify_v1.unload_null_sink_query_failed", exc_info=True)
        return
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or parts[1] != "module-null-sink":
            continue
        if f"sink_name={NULL_SINK_NAME}" not in parts[2]:
            continue
        try:
            mid = int(parts[0])
        except ValueError:
            continue
        try:
            await pw.unload_module(mid)
        except Exception:
            log.warning(
                "plugins.spotify_v1.unload_null_sink_failed",
                module_id=mid,
                exc_info=True,
            )
