"""AirPlay plugin — wraps shairport-sync 3.x / 4.x (AP1 or AP2 mode).

shairport-sync exposes the Stage as an AirPlay receiver. The `airplay_version`
setting picks between:

  * AP1 (RAOP): legacy protocol, works with every Apple device since
    2007, 16-bit 44.1 kHz lossy ALAC. No external daemon needed.
  * AP2: lossless ALAC up to 24/48, multi-room sync via PTP. Requires
    the nqptp companion daemon running on the same host (binds UDP
    319 + 320 — collides with ptp4l on the same iface).

The legacy module name (`airplay_v1.py`, class `AirplayV1Plugin`, unit
`shairport-sync.service`) stays for API/state compatibility; the v1
vs v2 toggle lives inside the settings model.

Routing model
-------------

shairport-sync's `pa` backend writes into pipewire-pulse. Left to its
own devices, that stream lands on the system default sink — bypassing
the Phonon routing matrix entirely, which is not what we want: the
engineer doing the routing must decide where AirPlay audio goes.

So the plugin owns a dedicated null-sink named `airplay_in` (loaded
via `pactl load-module module-null-sink` at enable time). shairport-
sync's conf points to it via `pa.sink = "airplay_in"`. The null-sink's
monitor_FL/FR ports come out as `direction=output` in pw-dump → they
surface as a normal routable source in the patch bay, just like the
BT bridge's `bt_<name>_in` null-sink. The user maps them to whatever
they want; no auto-routing, no defaults.

The null-sink is created by the plugin on `enable`/`start` (idempotent —
skipped if a node with that name already exists in PW) and torn down
on `disable`. Across phonon-stage restarts the null-sink survives if
it's still in PW (pactl modules persist as long as the user's pipewire
session is up), so we check by name rather than by stashed module id.

Why AirPlay 1 first
-------------------

  * No NQPTP dependency — AirPlay 2 needs a separate PTP daemon that
    would conflict with our existing ptp4l on the AES67 multicast
    domain; arbitrating both cleanly is its own work item.
  * Wider hardware coverage (every Apple device since 2007 supports
    AirPlay 1; AirPlay 2 only since iOS 11.4 / macOS 10.13.6).
  * Smaller integration surface — restart-on-config is sufficient,
    no PTP-aware coordination needed.

A separate `airplay_v2.py` plugin will be added later for AirPlay 2.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field

from phonon_stage.plugins.backend import (
    PluginRuntime,
    PluginSettings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from phonon_stage.pipewire.backend import PipeWireBackend
    from phonon_stage.plugins.system import SystemBackend


# Dedicated null-sink the plugin owns. shairport-sync writes here and
# its monitor port becomes a routable source in the patch bay.
NULL_SINK_NAME = "airplay_in"
NULL_SINK_DESCRIPTION = "AirPlay-In"


class AirplayV1Settings(PluginSettings):
    """Full user-tunable surface for the AirPlay 1 receiver.

    Every field maps to a shairport-sync.conf option. Defaults reflect
    what shairport-sync itself ships as conservative values — changing
    them is an engineering decision (latency tuning, volume curves,
    diagnostics verbosity)."""

    model_config = ConfigDict(extra="forbid")

    # ── Protocol version ────────────────────────────────────────
    airplay_version: Literal[1, 2] = Field(
        default=1,
        description=(
            "AirPlay protocol version. 1 = classic RAOP (44.1k/16 lossy ALAC, "
            "every Apple device since 2007). 2 = AirPlay 2 (up to 24/48 "
            "lossless, multi-room sync via PTP — needs nqptp companion "
            "daemon, conflicts with ptp4l on UDP 319/320). Switching v1→v2 "
            "starts nqptp; v2→v1 stops it."
        ),
    )

    # ── Identity ────────────────────────────────────────────────
    name: str = Field(
        default="Phonon",
        min_length=1,
        max_length=63,
        description="Name visible to AirPlay clients in their device picker.",
    )
    password: str = Field(
        default="",
        max_length=64,
        description="Optional password clients must supply. Empty = open.",
    )

    # ── Audio quality ───────────────────────────────────────────
    interpolation: Literal["basic", "soxr"] = Field(
        default="soxr",
        description=(
            "Resampling algorithm. soxr = SoX Resampler, higher quality "
            "at ~2% extra CPU. basic = linear interpolation, lighter."
        ),
    )
    output_format: Literal["S16", "S24", "S32", "auto"] = Field(
        default="auto",
        description=(
            "Output sample format. AirPlay 1 sources are always 16-bit "
            "at 44.1 kHz; auto = let shairport pick what the backend "
            "prefers, usually the same as the input."
        ),
    )
    playback_mode: Literal["stereo", "mono", "reverse_stereo", "both_left", "both_right"] = Field(
        default="stereo",
        description=(
            "Channel routing. mono = sum L+R, reverse_stereo = swap "
            "L↔R, both_left / both_right = duplicate one channel onto "
            "both outputs."
        ),
    )

    # ── Volume ──────────────────────────────────────────────────
    volume_control_profile: Literal["standard", "dasl_tapered", "flat"] = Field(
        default="standard",
        description=(
            "Volume curve. standard = shairport default (gentle taper "
            "in the low end). dasl_tapered = perceptually linear, "
            "louder mid-range. flat = pass the iOS slider through 1:1."
        ),
    )
    volume_max_db: float = Field(
        default=0.0,
        ge=-30.0,
        le=0.0,
        description=(
            "Maximum output level in dB. 0 = no attenuation; -6 caps "
            "the peak loudness, useful to protect downstream gear."
        ),
    )
    volume_range_db: float = Field(
        default=0.0,
        ge=0.0,
        le=120.0,
        description=(
            "Total volume range (dB). 0 = use the device default "
            "(typically 60 dB on shairport's software volume)."
        ),
    )
    ignore_volume_control: bool = Field(
        default=False,
        description=(
            "If true, shairport ignores iOS volume changes — useful "
            "when downstream gear handles volume itself."
        ),
    )

    # ── Session ─────────────────────────────────────────────────
    allow_session_interruption: bool = Field(
        default=True,
        description=(
            "If true, a new AirPlay client can kick off a current "
            "session. If false, sessions are sticky until the source "
            "disconnects."
        ),
    )
    session_timeout: int = Field(
        default=120,
        ge=0,
        le=3600,
        description=(
            "Seconds of silence before shairport ends the session. "
            "0 = never (the session stays open until the client "
            "explicitly disconnects)."
        ),
    )

    # ── Latency / sync ──────────────────────────────────────────
    audio_backend_buffer_desired_length_in_seconds: float = Field(
        default=0.20,
        ge=0.05,
        le=2.0,
        description=(
            "Buffer between shairport and the PA sink. Higher = more "
            "tolerant of system jitter, more added latency."
        ),
    )
    audio_backend_latency_offset_in_seconds: float = Field(
        default=0.0,
        ge=-0.5,
        le=0.5,
        description=(
            "Manual offset added to the calculated latency. Use for "
            "fine alignment with other AirPlay or non-AirPlay sources."
        ),
    )
    drift_tolerance_in_seconds: float = Field(
        default=0.002,
        ge=0.0,
        le=0.1,
        description=(
            "Max drift between source and backend clocks before "
            "shairport applies micro-corrections. Lower = tighter sync."
        ),
    )
    resync_threshold_in_seconds: float = Field(
        default=0.050,
        ge=0.0,
        le=1.0,
        description=(
            "Drift above this threshold triggers a full resync (audible "
            "glitch). Should be well above drift_tolerance."
        ),
    )

    # ── Diagnostics ─────────────────────────────────────────────
    log_verbosity: Literal[0, 1, 2, 3] = Field(
        default=0,
        description=(
            "0 = silent, 1 = warnings only, 2 = info, 3 = debug "
            "(very chatty, useful only when troubleshooting)."
        ),
    )
    statistics: bool = Field(
        default=False,
        description="Log periodic playback statistics (jitter, drift, latency).",
    )


class AirplayV1Plugin:
    """Concrete `SourcePlugin` implementation for shairport-sync 3.x / 4.x."""

    name: str = "airplay-v1"
    title: str = "AirPlay (Classic)"
    description: str = (
        "AirPlay 1 receiver via shairport-sync — works with every "
        "Apple device since 2007. AirPlay 2 (newer iPhones) is "
        "a separate plugin."
    )
    family: str = "source"
    # The routable surface this plugin exposes is the null-sink's
    # monitor port, not the shairport-sync stream itself. Match on the
    # null-sink name so /plugins reports the right node.
    pw_node_pattern: str = rf"(?i){NULL_SINK_NAME}"

    settings_model: type[PluginSettings] = AirplayV1Settings

    UNIT: str = "shairport-sync.service"
    # AP2 requires nqptp (Not Quite PTP) running on the host. The
    # plugin starts/stops it alongside shairport-sync whenever the
    # current settings have airplay_version=2. AP1 mode tears it down.
    NQPTP_UNIT: str = "nqptp.service"

    def __init__(
        self,
        system: SystemBackend,
        pw_backend: PipeWireBackend,
        conf_path: Path,
    ) -> None:
        self._system = system
        self._pw = pw_backend
        # shairport-sync conf path — owned by the phonon user so the
        # daemon (running as the same user) can read it without
        # privileged escalation. install.sh creates the parent dir.
        self._conf_path = conf_path

    # ── Lifecycle ──────────────────────────────────────────────

    async def runtime(self) -> PluginRuntime:
        enabled = await self._system.systemctl_is_enabled(self.UNIT)
        running = await self._system.systemctl_is_active(self.UNIT)
        last_error = ""
        if enabled and not running:
            # Likely failed to come up — surface a snippet of the
            # status output so the UI can show what shairport-sync
            # said before giving up.
            last_error = await self._system.systemctl_status_stderr(self.UNIT)
        return PluginRuntime(enabled=enabled, running=running, last_error=last_error)

    async def enable(self) -> None:
        # Order matters: conf first (so shairport doesn't crash on
        # start with no file), then null-sink (so the conf's pa.sink
        # target exists when the daemon starts), then enable the unit.
        if not self._system.file_exists(self._conf_path):
            await self.put_settings(AirplayV1Settings())
        await self._ensure_null_sink()
        await self._sync_nqptp_to_settings()
        await self._system.systemctl_enable(self.UNIT)

    async def disable(self) -> None:
        # Stop first so the running daemon is gone before the unit is
        # taken off autostart, then unload the null-sink (no point
        # keeping it around if no daemon is writing to it).
        await self._system.systemctl_stop(self.UNIT)
        await self._system.systemctl_disable(self.UNIT)
        # nqptp on the host is shared infrastructure — but our plugin
        # is the only thing that uses it today, so tearing it down on
        # disable is correct.
        await self._stop_nqptp_quiet()
        await self._remove_null_sink()

    async def start(self) -> None:
        if not self._system.file_exists(self._conf_path):
            await self.put_settings(AirplayV1Settings())
        await self._ensure_null_sink()
        await self._sync_nqptp_to_settings()
        await self._system.systemctl_start(self.UNIT)

    async def stop(self) -> None:
        await self._system.systemctl_stop(self.UNIT)
        await self._stop_nqptp_quiet()

    async def restart(self) -> None:
        # Don't tear down the null-sink here — the goal of restart is
        # to bounce shairport-sync with the current conf, while keeping
        # the routing target stable so any mappings to airplay_in stay
        # intact.
        await self._ensure_null_sink()
        await self._sync_nqptp_to_settings()
        await self._system.systemctl_restart(self.UNIT)

    # ── nqptp lifecycle (AP2 companion) ────────────────────────

    async def _sync_nqptp_to_settings(self) -> None:
        """Bring nqptp's running state into agreement with the current
        airplay_version setting. Called from every start path.

        v2 → start nqptp (no-op if already running)
        v1 → stop nqptp  (so its UDP 319/320 bind doesn't squat the
                          ports when ptp4l later wants them)

        Errors are swallowed-and-logged: a host without nqptp installed
        is a valid AP1-only deployment, and `systemctl start` returning
        non-zero shouldn't crash the plugin's start path."""
        settings = await self.get_settings()
        if settings.airplay_version == 2:
            try:
                await self._system.systemctl_start(self.NQPTP_UNIT)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "plugins.airplay.nqptp_start_failed",
                    exc_info=True,
                )
        else:
            await self._stop_nqptp_quiet()

    async def _stop_nqptp_quiet(self) -> None:
        try:
            await self._system.systemctl_stop(self.NQPTP_UNIT)
        except Exception:
            # Already stopped / never installed / not granted via sudoers
            # — all benign here, the AP1 path doesn't depend on nqptp.
            pass

    # ── Settings ───────────────────────────────────────────────

    async def get_settings(self) -> AirplayV1Settings:
        """Read the on-disk shairport-sync.conf and reverse-parse it
        back into the settings model. If the file is missing or has
        been hand-edited beyond what we know how to read, fall back
        to defaults rather than crashing the API."""
        if not self._system.file_exists(self._conf_path):
            return AirplayV1Settings()
        try:
            raw = self._system.read_text(self._conf_path)
        except Exception:
            return AirplayV1Settings()
        return self._parse_conf(raw)

    async def put_settings(self, settings: PluginSettings) -> None:
        if not isinstance(settings, AirplayV1Settings):
            msg = (
                f"AirplayV1Plugin.put_settings expected AirplayV1Settings, "
                f"got {type(settings).__name__}"
            )
            raise TypeError(msg)
        rendered = self._render_conf(settings)
        self._system.write_text_atomic(self._conf_path, rendered, mode=0o644)
        # v1↔v2 toggle changes nqptp's required state — bring it in
        # line BEFORE restarting shairport-sync so the daemon comes up
        # against a healthy companion (or no companion in AP1 mode).
        if settings.airplay_version == 2:
            try:
                await self._system.systemctl_start(self.NQPTP_UNIT)
            except Exception:
                import structlog
                structlog.get_logger().warning(
                    "plugins.airplay.nqptp_start_failed",
                    exc_info=True,
                )
        else:
            await self._stop_nqptp_quiet()
        # Restart only if the unit is currently active — otherwise
        # the next start/enable will pick up the new config naturally.
        if await self._system.systemctl_is_active(self.UNIT):
            await self._system.systemctl_restart(self.UNIT)

    # ── Null-sink management ───────────────────────────────────

    async def _ensure_null_sink(self) -> None:
        """Idempotent: load module-null-sink only if no node with our
        name is currently in the PW graph. pactl modules survive across
        phonon-stage restarts (they live in the user's pipewire
        session), so re-loading on every enable would just stack
        duplicates."""
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            nodes = []
        if any(n.name == NULL_SINK_NAME for n in nodes):
            return
        await self._pw.load_null_sink(NULL_SINK_NAME, NULL_SINK_DESCRIPTION)

    async def _remove_null_sink(self) -> None:
        """Find the null-sink by name in the current PW graph, locate
        its owner module, unload it. Pactl unload-module needs the
        module id (not a name), and we don't persist the id across
        restarts — so we always scan, which is also self-healing in
        case the user reloaded modules out of band."""
        try:
            nodes = await self._pw.list_nodes()
        except Exception:
            return
        target = next((n for n in nodes if n.name == NULL_SINK_NAME), None)
        if target is None:
            return
        # We don't have the module id in the PwNode dataclass. Probe
        # pactl directly through the same backend; if our fake fakes
        # `null_sinks`, the integration is clean.
        await self._unload_null_sink_by_name()

    async def _unload_null_sink_by_name(self) -> None:
        """Implementation detail: walk the backend's known modules to
        find the one whose sink_name matches NULL_SINK_NAME, unload it.

        Real backend: uses `pactl list short modules` parsing.
        Fake backend: iterates its `null_sinks` dict.

        Either way the backend takes care of mapping name → id; the
        plugin just asks for it to be gone.
        """
        # The PipeWireBackend Protocol intentionally doesn't expose a
        # generic "find module by name" — that's a backend-internal
        # concern. We piggyback on `unload_module` and a small lookup
        # done here by re-using `load_null_sink` semantics: when the
        # backend is asked to load a duplicate it returns the same id.
        # That's only worth doing in the real path. In tests the fake
        # exposes `null_sinks` for direct inspection.
        from phonon_stage.pipewire.fake import FakePipeWireBackend

        if isinstance(self._pw, FakePipeWireBackend):
            # Test path: find by name, ask the backend to unload.
            mid = next(
                (m for m, (n, _) in self._pw.null_sinks.items() if n == NULL_SINK_NAME),
                None,
            )
            if mid is not None:
                await self._pw.unload_module(mid)
            return
        # Real path: query pactl for our module id.
        await _real_unload_null_sink_by_name(self._pw)

    # ── Config file rendering / parsing ────────────────────────

    @staticmethod
    def _render_conf(s: AirplayV1Settings) -> str:
        """Render an AirplayV1Settings into shairport-sync's curly-brace
        config syntax. The pa.sink = airplay_in line is mandatory —
        it pins the daemon's output to the null-sink we created.

        Several gotchas from shairport-sync 4.x we work around here:
          * `volume_range_db = 0` is rejected even though the doc says
            "0 = use device default" — we emit the line only when > 0.
          * `volume_max_db = 0` would attenuate by 0 dB which is a no-
            op but shairport accepts it; we emit it only when negative
            to keep the conf tidy.
          * `statistics` and `log_verbosity` moved out of `general`
            into a new `diagnostics` section in 4.x; emitting them in
            `general` only triggers warnings, but writing them once in
            the right place avoids the chatter.
          * `playback_mode` and `output_format` are valid in `general`.
        """
        password_line = f'password = "{s.password}";' if s.password else "// password unset"
        # airplay-version is only honoured by shairport-sync builds
        # compiled with --with-airplay-2. The apt binary (AP1-only)
        # silently ignores it, so emitting unconditionally is safe —
        # but we omit it for v1 to keep the conf tidy and avoid
        # confusion when an operator reads the file looking for
        # "what protocol is this stage advertising?".
        version_line = (
            f"  airplay-version = {s.airplay_version};\n" if s.airplay_version == 2 else ""
        )
        # Optional lines — only emit when the value would be accepted /
        # meaningful. shairport-sync 4.x rejects `volume_range_db = 0`
        # despite the doc saying it means "device default".
        vol_range_line = (
            f"  volume_range_db = {s.volume_range_db:.2f};\n" if s.volume_range_db > 0 else ""
        )
        vol_max_line = f"  volume_max_db = {s.volume_max_db:.2f};\n" if s.volume_max_db < 0 else ""
        return (
            "// Auto-generated by phonon-stage AirPlay v1 plugin.\n"
            "// Edits made by hand will be overwritten on the next\n"
            "// settings update from the UI / API.\n"
            "\n"
            "general =\n"
            "{\n"
            f'  name = "{s.name}";\n'
            f"  {password_line}\n"
            f"{version_line}"
            f'  interpolation = "{s.interpolation}";\n'
            f'  output_format = "{s.output_format}";\n'
            f'  playback_mode = "{s.playback_mode}";\n'
            f'  volume_control_profile = "{s.volume_control_profile}";\n'
            f"{vol_max_line}"
            f"{vol_range_line}"
            f'  ignore_volume_control = "{_yn(s.ignore_volume_control)}";\n'
            f"  audio_backend_buffer_desired_length_in_seconds = "
            f"{s.audio_backend_buffer_desired_length_in_seconds:.3f};\n"
            f"  audio_backend_latency_offset_in_seconds = "
            f"{s.audio_backend_latency_offset_in_seconds:.3f};\n"
            f"  drift_tolerance_in_seconds = {s.drift_tolerance_in_seconds:.4f};\n"
            f"  resync_threshold_in_seconds = {s.resync_threshold_in_seconds:.3f};\n"
            "};\n"
            "\n"
            "sessioncontrol =\n"
            "{\n"
            f'  allow_session_interruption = "{_yn(s.allow_session_interruption)}";\n'
            f"  session_timeout = {s.session_timeout};\n"
            "};\n"
            "\n"
            "diagnostics =\n"
            "{\n"
            f"  log_verbosity = {s.log_verbosity};\n"
            f'  statistics = "{_yn(s.statistics)}";\n'
            "};\n"
            "\n"
            "// Route audio into the dedicated null-sink so its monitor\n"
            "// port becomes a routable source in the patch bay.\n"
            'output_backend = "pa";\n'
            "\n"
            "pa =\n"
            "{\n"
            '  application_name = "Shairport Sync";\n'
            f'  sink = "{NULL_SINK_NAME}";\n'
            "};\n"
        )

    @staticmethod
    def _parse_conf(raw: str) -> AirplayV1Settings:
        """Best-effort reverse parse. We don't link a libconfig parser
        for one read path — regex extraction is enough for the fields
        we render. Unknown / malformed fields fall back to defaults
        rather than 500-ing the API."""
        defaults = AirplayV1Settings()
        # Strip line comments to avoid matching commented-out values.
        lines = [
            line.split("//", 1)[0]
            for line in raw.splitlines()
            if not line.lstrip().startswith("//")
        ]
        text = "\n".join(lines)

        def s_q(key: str, allowed: set[str] | None = None, fallback: str = "") -> str:
            v = _extract_quoted(text, key)
            if v is None:
                return fallback
            if allowed and v not in allowed:
                return fallback
            return v

        def s_n(key: str, fallback: float) -> float:
            v = _extract_number(text, key)
            return v if v is not None else fallback

        def s_b(key: str, fallback: bool) -> bool:
            v = _extract_quoted(text, key)
            if v is None:
                return fallback
            return v.lower() in {"yes", "true", "1"}

        # airplay-version reads as an integer; default to v1 if the
        # line is missing (legacy confs predating the toggle).
        ap_v_raw = _extract_number(text, "airplay-version")
        airplay_version: Literal[1, 2] = 2 if ap_v_raw == 2 else 1

        return AirplayV1Settings(
            airplay_version=airplay_version,
            name=s_q("name", fallback=defaults.name),
            password=s_q("password", fallback=""),
            interpolation=s_q(
                "interpolation", allowed={"basic", "soxr"}, fallback=defaults.interpolation
            ),
            output_format=s_q(
                "output_format",
                allowed={"S16", "S24", "S32", "auto"},
                fallback=defaults.output_format,
            ),
            playback_mode=s_q(
                "playback_mode",
                allowed={"stereo", "mono", "reverse_stereo", "both_left", "both_right"},
                fallback=defaults.playback_mode,
            ),
            volume_control_profile=s_q(
                "volume_control_profile",
                allowed={"standard", "dasl_tapered", "flat"},
                fallback=defaults.volume_control_profile,
            ),
            volume_max_db=s_n("volume_max_db", defaults.volume_max_db),
            volume_range_db=s_n("volume_range_db", defaults.volume_range_db),
            ignore_volume_control=s_b("ignore_volume_control", defaults.ignore_volume_control),
            allow_session_interruption=s_b(
                "allow_session_interruption", defaults.allow_session_interruption
            ),
            session_timeout=int(s_n("session_timeout", defaults.session_timeout)),
            audio_backend_buffer_desired_length_in_seconds=s_n(
                "audio_backend_buffer_desired_length_in_seconds",
                defaults.audio_backend_buffer_desired_length_in_seconds,
            ),
            audio_backend_latency_offset_in_seconds=s_n(
                "audio_backend_latency_offset_in_seconds",
                defaults.audio_backend_latency_offset_in_seconds,
            ),
            drift_tolerance_in_seconds=s_n(
                "drift_tolerance_in_seconds", defaults.drift_tolerance_in_seconds
            ),
            resync_threshold_in_seconds=s_n(
                "resync_threshold_in_seconds", defaults.resync_threshold_in_seconds
            ),
            log_verbosity=int(s_n("log_verbosity", defaults.log_verbosity)),
            statistics=s_b("statistics", defaults.statistics),
        )


def _yn(b: bool) -> str:
    """shairport-sync conf uses "yes"/"no" strings, not booleans."""
    return "yes" if b else "no"


async def _real_unload_null_sink_by_name(pw: PipeWireBackend) -> None:
    """Locate `airplay_in` in pactl's module list and unload it.

    We import pactl access lazily so the typing layer doesn't pull
    cli into the test environment (the test path never hits this).
    Errors are logged but never raised — `disable()` shouldn't fail
    just because the null-sink was already torn down by the user.
    """
    import structlog

    log = structlog.get_logger()
    try:
        from phonon_stage.pipewire import cli

        out = await cli.run_command("pactl", "list", "short", "modules")
    except Exception:
        log.warning("plugins.airplay_v1.unload_null_sink_query_failed", exc_info=True)
        return
    # Lines look like:
    #   536870917<TAB>module-null-sink<TAB>sink_name=airplay_in sink_properties=...
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
                "plugins.airplay_v1.unload_null_sink_failed",
                module_id=mid,
                exc_info=True,
            )


def _extract_quoted(text: str, key: str) -> str | None:
    """Find `key = "value"` in shairport-sync conf-ish text. Returns
    None if the key isn't found. Empty quotes return an empty
    string — caller decides whether that means 'unset'."""
    pattern = rf'{re.escape(key)}\s*=\s*"([^"]*)"\s*;'
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(1)


def _extract_number(text: str, key: str) -> float | None:
    """Find `key = N;` (integer or float) in shairport-sync conf-ish
    text. Returns None if not found."""
    pattern = rf"{re.escape(key)}\s*=\s*(-?\d+(?:\.\d+)?)\s*;"
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
