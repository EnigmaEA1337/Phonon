"""AirPlay v1 plugin — wraps shairport-sync 3.x.

shairport-sync exposes the Stage as an AirPlay 1 receiver (classic
RAOP protocol). It listens on TCP 5000 + UDP, announces itself via
mDNS as a `_raop._tcp` service, and pushes received audio into a
PulseAudio backend (here: pipewire-pulse, our compat layer).

Why AirPlay 1 first:
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

from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, Field

from phonon_stage.plugins.backend import (
    PluginRuntime,
    PluginSettings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from phonon_stage.plugins.system import SystemBackend


class AirplayV1Settings(PluginSettings):
    """User-tunable fields for the AirPlay 1 receiver."""

    model_config = ConfigDict(extra="forbid")

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
    volume_mode: Literal["software", "hardware"] = Field(
        default="software",
        description=(
            "software = shairport applies volume in software before "
            "PA output (recommended behind pipewire-pulse). "
            "hardware = pass-through to the ALSA mixer of the output "
            "device (only useful with raw alsa backend)."
        ),
    )
    interpolation: Literal["basic", "soxr"] = Field(
        default="soxr",
        description=(
            "Resampling algorithm. soxr = SoX Resampler, higher quality "
            "at the cost of ~2% extra CPU. basic = linear interpolation, "
            "lighter but audibly degraded on high frequencies."
        ),
    )


class AirplayV1Plugin:
    """Concrete `SourcePlugin` implementation for shairport-sync 3.x."""

    name: str = "airplay-v1"
    title: str = "AirPlay (Classic)"
    description: str = (
        "AirPlay 1 receiver via shairport-sync — works with every "
        "Apple device since 2007. AirPlay 2 (newer iPhones) is "
        "a separate plugin."
    )
    family: str = "source"
    # shairport-sync with the `pa` backend exposes a stream into
    # pipewire-pulse named after `application_name` (default
    # "Shairport Sync"). PW normalises the node name so we match
    # case-insensitively on the substring.
    pw_node_pattern: str = r"(?i)shairport"

    settings_model: type[PluginSettings] = AirplayV1Settings

    UNIT: str = "shairport-sync.service"

    def __init__(self, system: SystemBackend, conf_path: Path) -> None:
        self._system = system
        # shairport-sync conf path — owned by the phonon user so the
        # daemon (running as the same user) can read it without
        # privileged escalation. install.sh creates the parent dir.
        self._conf_path = conf_path

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
        # Make sure a config file exists before the daemon ever runs —
        # shairport-sync refuses to start without one. We write defaults
        # if the user hasn't pushed settings yet.
        if not self._system.file_exists(self._conf_path):
            await self.put_settings(AirplayV1Settings())
        await self._system.systemctl_enable(self.UNIT)

    async def disable(self) -> None:
        # Stop first so the unit is in a clean state when disabled —
        # otherwise it could linger until the next session reboot.
        await self._system.systemctl_stop(self.UNIT)
        await self._system.systemctl_disable(self.UNIT)

    async def start(self) -> None:
        if not self._system.file_exists(self._conf_path):
            await self.put_settings(AirplayV1Settings())
        await self._system.systemctl_start(self.UNIT)

    async def stop(self) -> None:
        await self._system.systemctl_stop(self.UNIT)

    async def restart(self) -> None:
        await self._system.systemctl_restart(self.UNIT)

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
        # Restart only if the unit is currently active — otherwise
        # the next start/enable will pick up the new config naturally.
        if await self._system.systemctl_is_active(self.UNIT):
            await self._system.systemctl_restart(self.UNIT)

    # ── Config file rendering / parsing ─────────────────────────────

    @staticmethod
    def _render_conf(s: AirplayV1Settings) -> str:
        """Render an AirplayV1Settings into shairport-sync's curly-brace
        config syntax. Only the fields we expose are written; users
        wanting full customization edit the file directly (and we
        re-render on next put, which overwrites them — documented
        limitation, fine for v1)."""
        password_line = f'password = "{s.password}";' if s.password else "// password unset"
        return (
            "// Auto-generated by phonon-stage AirPlay v1 plugin.\n"
            "// Edits made by hand will be overwritten on the next\n"
            "// settings update from the UI / API.\n"
            "\n"
            "general =\n"
            "{\n"
            f'  name = "{s.name}";\n'
            f"  {password_line}\n"
            f'  interpolation = "{s.interpolation}";\n'
            f'  volume_control_profile = "{s.volume_mode}";\n'
            "};\n"
            "\n"
            "sessioncontrol =\n"
            "{\n"
            '  allow_session_interruption = "yes";\n'
            "};\n"
            "\n"
            "// Route audio into pipewire-pulse so it appears as a\n"
            "// stream node in the Phonon mapping matrix.\n"
            'output_backend = "pa";\n'
            "\n"
            "pa =\n"
            "{\n"
            '  application_name = "Shairport Sync";\n'
            "};\n"
        )

    @staticmethod
    def _parse_conf(raw: str) -> AirplayV1Settings:
        """Best-effort reverse parse. We don't link a libconfig parser
        for one read path — a few regex-style extractions are enough
        for the fields we ourselves render, and we fall back to
        defaults on any anomaly."""
        defaults = AirplayV1Settings()
        # Strip line comments to avoid matching commented-out values.
        lines = [
            line.split("//", 1)[0]
            for line in raw.splitlines()
            if not line.lstrip().startswith("//")
        ]
        text = "\n".join(lines)
        name = _extract_quoted(text, "name") or defaults.name
        password = _extract_quoted(text, "password") or ""
        interp = _extract_quoted(text, "interpolation") or defaults.interpolation
        if interp not in {"basic", "soxr"}:
            interp = defaults.interpolation
        vmode = _extract_quoted(text, "volume_control_profile") or defaults.volume_mode
        if vmode not in {"software", "hardware"}:
            vmode = defaults.volume_mode
        return AirplayV1Settings(
            name=name,
            password=password,
            interpolation=interp,
            volume_mode=vmode,
        )


def _extract_quoted(text: str, key: str) -> str | None:
    """Find `key = "value"` in shairport-sync conf-ish text. Returns
    None if the key isn't found at all (caller substitutes a default).
    Empty quotes return an empty string — caller decides whether that
    means 'unset' (e.g. password)."""
    import re

    pattern = rf'{re.escape(key)}\s*=\s*"([^"]*)"\s*;'
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(1)
