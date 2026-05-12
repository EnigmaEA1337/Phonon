"""Spotify Connect plugin — wraps librespot.

librespot is the open-source Spotify Connect implementation (Rust).
When running it appears as a Connect-capable speaker in the Spotify
clients, and writes received audio to a PulseAudio sink.

Routing model
-------------

Same idea as airplay_v1: librespot's `--device <pa-sink>` argument
pins its output, and we own a dedicated null-sink named
`spotify_in` so the audio is contained in the Phonon routing matrix
instead of leaking to whatever PW happens to consider the default
sink. The null-sink's monitor port surfaces as a routable source
in the patch bay just like `airplay_in` / `bt_<name>_in`.

Daemon control
--------------

librespot is configured entirely via command-line args (no curly-
brace conf like shairport-sync). We render the settings into a
single `LIBRESPOT_ARGS=...` line in an EnvironmentFile that the
user systemd unit sources; the unit's ExecStart uses
`$LIBRESPOT_ARGS` (no braces) so systemd's shell-style word
splitting expands it into individual args. `systemctl --user
restart` picks up the new env on the next start.

Premium-only note
-----------------

Spotify Connect (the protocol librespot speaks) requires a Premium
account on the *controlling* device. The Free tier doesn't expose
the "send to a different speaker" affordance — the device shows up
in Connect picker only for Premium users. Not a plugin bug, just
a Spotify business rule.
"""

from __future__ import annotations

import contextlib
import re
import shlex
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field

from phonon_stage.plugins.backend import PluginRuntime, PluginSettings

if TYPE_CHECKING:
    from pathlib import Path

    from phonon_stage.pipewire.backend import PipeWireBackend
    from phonon_stage.plugins.system import SystemBackend


# Dedicated null-sink the plugin owns. librespot writes here via
# `--device spotify_in` and its monitor port becomes the routable
# source in the patch bay.
NULL_SINK_NAME = "spotify_in"
NULL_SINK_DESCRIPTION = "Spotify-In"


class SpotifyV1Settings(PluginSettings):
    """User-tunable surface for the Spotify Connect receiver.

    Maps directly onto librespot's CLI options. Defaults reflect the
    librespot upstream defaults except for `device_name` (we want it
    visible as "Phonon" in Connect, not the host's hostname) and
    `quiet` (we suppress the chatty info logs by default — the user
    can flip it for troubleshooting)."""

    model_config = ConfigDict(extra="forbid")

    # ── Identity ────────────────────────────────────────────────
    name: str = Field(
        default="Phonon",
        min_length=1,
        max_length=63,
        description="Name visible to Spotify clients in their Connect picker.",
    )
    # Zeroconf hint Spotify clients display next to the device name.
    # `speaker` matches the use case best; the others are listed as
    # informational so the operator can match the icon shown in
    # Spotify Mobile / Desktop if they care.
    device_type: Literal[
        "speaker",
        "computer",
        "tablet",
        "smartphone",
        "tv",
        "avr",
        "stb",
        "audiodongle",
        "gameconsole",
        "castaudio",
        "castvideo",
        "automobile",
        "smartwatch",
        "chromebook",
        "carthing",
        "homething",
    ] = Field(
        default="speaker",
        description="Device-type hint shown to Spotify clients (icon + label).",
    )

    # ── Audio quality ───────────────────────────────────────────
    bitrate: Literal[96, 160, 320] = Field(
        default=320,
        description="Spotify stream bitrate in kbps. 320 = high quality.",
    )
    # librespot's PulseAudio backend supports several sample formats;
    # F32 is the widest dynamic range and matches what PW's filter
    # graph expects natively without conversion.
    format: Literal["F32", "F64", "S32", "S24", "S24_3", "S16"] = Field(
        default="F32",
        description="Sample format written into the PA sink.",
    )
    initial_volume: int = Field(
        default=50,
        ge=0,
        le=100,
        description=(
            "Volume librespot applies to its output (0-100). The Phonon "
            "mixer's source gain is independent and stacks on top."
        ),
    )

    # ── Behaviour ───────────────────────────────────────────────
    autoplay: bool = Field(
        default=True,
        description=(
            "When the current track ends and the user's queue is empty, "
            "ask Spotify to autoplay similar music. Off = the stream "
            "goes silent until the user picks something new."
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
            "TCP port librespot's mDNS responder binds to. 0 = the "
            "kernel picks a free one. Set explicitly if you have a "
            "firewall rule that needs a stable port."
        ),
    )

    # ── Diagnostics ─────────────────────────────────────────────
    quiet: bool = Field(
        default=True,
        description=(
            "Suppress librespot's info-level chatter (every track change "
            "logs a line otherwise). Turn off when troubleshooting."
        ),
    )


class SpotifyV1Plugin:
    """Concrete `SourcePlugin` implementation for librespot."""

    name: str = "spotify-v1"
    title: str = "Spotify Connect"
    description: str = (
        "Spotify Connect receiver via librespot. Appears in the "
        "Spotify app's device picker (Premium account required)."
    )
    family: str = "source"
    # The routable surface is the null-sink's monitor — match on its
    # name so /plugins reports the right PW node.
    pw_node_pattern: str = rf"(?i){NULL_SINK_NAME}"

    settings_model: type[PluginSettings] = SpotifyV1Settings

    UNIT: str = "librespot.service"

    def __init__(
        self,
        system: SystemBackend,
        pw_backend: PipeWireBackend,
        conf_path: Path,
    ) -> None:
        self._system = system
        self._pw = pw_backend
        # EnvironmentFile path read by librespot.service. Owned by
        # the phonon user — install.sh creates the parent dir.
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
        # Order matters: env file first (so librespot starts with the
        # configured args), null-sink (so --device spotify_in resolves
        # on first start), then enable the unit. Mirrors airplay_v1.
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
        # Keep the null-sink around — the goal is a daemon bounce
        # with the current env, mappings to spotify_in stay intact.
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
        """Idempotent: load only if no node with our name exists in PW."""
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
        target = next((n for n in nodes if n.name == NULL_SINK_NAME), None)
        if target is None:
            return
        await self._unload_null_sink_by_name()

    async def _unload_null_sink_by_name(self) -> None:
        """Locate our null-sink module by sink_name and unload it.
        Same Real/Fake split as airplay_v1 — the Fake exposes its
        `null_sinks` dict directly, the Real backend queries pactl."""
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

    # ── EnvironmentFile rendering / parsing ────────────────────

    @staticmethod
    def _render_conf(s: SpotifyV1Settings) -> str:
        """Render settings as an EnvironmentFile that librespot.service
        sources. The whole CLI is squeezed into a single
        `LIBRESPOT_ARGS=...` line; systemd's `$VAR` (no braces) splits
        on whitespace in ExecStart so each arg lands as expected.

        Args are quoted with `shlex.quote` so a `name` containing
        spaces or shell metacharacters stays in one piece. Boolean
        flags appear or don't (no `--flag false` form — librespot
        flags are presence-only)."""
        args: list[str] = [
            "--name",
            s.name,
            "--device-type",
            s.device_type,
            "--backend",
            "pulseaudio",
            "--device",
            NULL_SINK_NAME,
            "--bitrate",
            str(s.bitrate),
            "--format",
            s.format,
            "--initial-volume",
            str(s.initial_volume),
        ]
        if s.zeroconf_port > 0:
            args += ["--zeroconf-port", str(s.zeroconf_port)]
        # Boolean flags — order doesn't matter to librespot but we
        # keep it deterministic for clean diffs in /etc/passwd-style
        # ops review.
        if s.autoplay:
            args += ["--autoplay", "on"]
        else:
            args += ["--autoplay", "off"]
        if s.disable_audio_cache:
            args.append("--disable-audio-cache")
        if s.quiet:
            args.append("--quiet")

        joined = " ".join(shlex.quote(a) for a in args)
        return (
            "# Generated by phonon-stage SpotifyV1Plugin. Do not edit by hand —\n"
            "# changes are overwritten on the next /plugins/spotify-v1/settings PUT.\n"
            f"LIBRESPOT_ARGS={joined}\n"
        )

    @staticmethod
    def _parse_conf(raw: str) -> SpotifyV1Settings:
        """Reverse-parse the EnvironmentFile into a SpotifyV1Settings.
        Defaults cover every field the user hasn't set or the file
        doesn't carry — if parsing fails on any specific arg we just
        leave that field at default rather than refusing to load."""
        defaults = SpotifyV1Settings()
        m = re.search(r"^LIBRESPOT_ARGS=(.*)$", raw, re.MULTILINE)
        if not m:
            return defaults
        try:
            tokens = shlex.split(m.group(1))
        except ValueError:
            return defaults
        # Walk tokens as a flag/value stream. Each `--key value` pair
        # consumes two tokens; presence-only boolean flags one.
        # Build a dict-of-fields and feed it into pydantic at the end
        # so a single bad token doesn't poison everything.
        out: dict[str, object] = {}
        # Map of `--cli-flag` → settings field name for the value-pair
        # flags. Order doesn't matter — handled below by a lookup.
        str_flags = {"--name": "name", "--device-type": "device_type", "--format": "format"}
        int_flags = {
            "--bitrate": "bitrate",
            "--initial-volume": "initial_volume",
            "--zeroconf-port": "zeroconf_port",
        }
        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            if tok in str_flags and i + 1 < n:
                out[str_flags[tok]] = tokens[i + 1]
                i += 2
                continue
            if tok in int_flags and i + 1 < n:
                with contextlib.suppress(ValueError):
                    out[int_flags[tok]] = int(tokens[i + 1])
                i += 2
                continue
            if tok == "--autoplay" and i + 1 < n:
                out["autoplay"] = tokens[i + 1].lower() == "on"
                i += 2
                continue
            if tok == "--disable-audio-cache":
                out["disable_audio_cache"] = True
                i += 1
                continue
            if tok == "--quiet":
                out["quiet"] = True
                i += 1
                continue
            # Unknown token — librespot upstream may have added flags
            # we don't model. Skip without erroring.
            i += 1
        # Presence-only boolean flags: when the flag is absent from the
        # args we must record `False` explicitly, otherwise pydantic
        # would fall back to the model's default (True for both) and
        # the parser would round-trip the wrong value.
        out.setdefault("disable_audio_cache", False)
        out.setdefault("quiet", False)
        try:
            return SpotifyV1Settings(**out)
        except Exception:
            return defaults


async def _real_unload_null_sink_by_name(pw: PipeWireBackend) -> None:
    """Same shape as the airplay_v1 helper — locate our null-sink
    module by sink_name in pactl's module list and unload it. Errors
    are logged, never raised: disable() shouldn't fail just because
    pactl didn't surface our module any more."""
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
