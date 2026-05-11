"""LADSPA plugin introspection.

Wraps the LADSPA SDK's `analyseplugin` tool to extract a plugin's
control ports (name, range, default, hints) at runtime. The mixer
uses this schema both to populate sensible defaults the first time
a PluginInsert is enabled and to drive the auto-rendered UI panel
(no control name is hardcoded — any LSP LADSPA plugin works).

Real implementation shells out to analyseplugin. Fake implementation
returns canned descriptors — used in tests and on hosts without the
LADSPA SDK (Pi 3B Stages).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class PluginControl:
    """One control port on a LADSPA plugin.

    Audio ports are NOT exposed here — those are wired by the
    filter-chain conf (capture/playback streams). The UI only ever
    needs control ports.

    Fields:
      name:         the LADSPA port name, used verbatim as the key
                    in filter-chain's `control = { ... }` dict
      direction:    "input" (user-set) or "output" (read-only meter)
      toggled:      render as on/off (checkbox)
      integer:      render as integer slider — LADSPA still sends a
                    float to the plugin, but the value is rounded
      logarithmic:  apply log curve on the fader; pertinent for the
                    "Dry amount (G)" / "Wet amount (G)" / output gain
      minimum:      lower bound; None means LADSPA didn't declare one
      maximum:      upper bound; None means LADSPA didn't declare one
      default:      LADSPA-declared default value; None if absent
    """

    name: str
    direction: str  # "input" | "output"
    toggled: bool = False
    integer: bool = False
    logarithmic: bool = False
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "direction": self.direction,
            "toggled": self.toggled,
            "integer": self.integer,
            "logarithmic": self.logarithmic,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "default": self.default,
        }


@dataclass(frozen=True)
class PluginDescriptor:
    """A LADSPA plugin's introspected metadata.

    `library` is the LADSPA library name (what we pass to filter-chain's
    `plugin = "..."`), `label` is the unique plugin label inside it
    (LSP uses long URL-style labels like
    `http://lsp-plug.in/plugins/ladspa/comp_delay_stereo`).
    """

    library: str
    label: str
    name: str  # human-readable plugin name
    maker: str
    controls: tuple[PluginControl, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "library": self.library,
            "label": self.label,
            "name": self.name,
            "maker": self.maker,
            "controls": [c.to_dict() for c in self.controls],
        }


class LadspaIntrospector(Protocol):
    """Protocol for plugin introspection backends."""

    async def describe(self, library: str, label: str) -> PluginDescriptor: ...


class RealLadspaIntrospector:
    """Shells out to `analyseplugin <library> <label>` and parses the
    output. `library` is passed verbatim — LADSPA_PATH (typically
    `/usr/lib/ladspa` and `/usr/lib/<multiarch>/ladspa`) is what
    resolves it on disk.

    Important: LADSPA SDK's analyseplugin writes its descriptor to
    STDERR, not stdout — by design, since it's a diagnostic tool.
    We can't reuse `cli.run_command` (stdout-only); we run it
    directly and combine both streams before parsing.

    Multiarch quirk on Ubuntu Studio 26.04: lsp-plugins-ladspa.so
    lives under `/usr/lib/x86_64-linux-gnu/ladspa/` which isn't in
    analyseplugin's default search path. We seed LADSPA_PATH with
    both the canonical dir and the multiarch dir so the bare library
    name resolves regardless of distro layout."""

    _LADSPA_PATH = "/usr/lib/ladspa:/usr/lib/x86_64-linux-gnu/ladspa:/usr/local/lib/ladspa"

    async def describe(self, library: str, label: str) -> PluginDescriptor:
        import asyncio
        import os

        env = {
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "LADSPA_PATH": os.environ.get("LADSPA_PATH") or self._LADSPA_PATH,
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "analyseplugin",
                library,
                label,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=10.0
            )
        except FileNotFoundError as exc:
            logger.warning(
                "dsp.analyseplugin_missing",
                library=library,
                label=label,
            )
            msg = "analyseplugin binary not found — install ladspa-sdk"
            raise RuntimeError(msg) from exc
        except Exception:
            logger.warning(
                "dsp.analyseplugin_failed",
                library=library,
                label=label,
                exc_info=True,
            )
            raise
        # Combine streams — analyseplugin in some SDK builds writes
        # the descriptor to stderr, in others to stdout. We feed both.
        text = (stdout_bytes.decode("utf-8", errors="replace") + "\n"
                + stderr_bytes.decode("utf-8", errors="replace"))
        if proc.returncode != 0 and not text.strip():
            logger.warning(
                "dsp.analyseplugin_nonzero",
                library=library,
                label=label,
                returncode=proc.returncode,
            )
            msg = f"analyseplugin failed (rc={proc.returncode})"
            raise RuntimeError(msg)
        return parse_analyseplugin_output(text, library=library, label=label)


class FakeLadspaIntrospector:
    """Test/Pi fallback. Returns pre-registered descriptors. Raises
    KeyError if asked about an unknown plugin — same surface as Real
    when analyseplugin can't find the plugin."""

    def __init__(self, descriptors: dict[tuple[str, str], PluginDescriptor] | None = None) -> None:
        self._descriptors = dict(descriptors or {})

    def register(self, descriptor: PluginDescriptor) -> None:
        self._descriptors[(descriptor.library, descriptor.label)] = descriptor

    async def describe(self, library: str, label: str) -> PluginDescriptor:
        try:
            return self._descriptors[(library, label)]
        except KeyError as e:
            msg = f"unknown plugin {library!r} / {label!r}"
            raise KeyError(msg) from e


# ── analyseplugin output parser ─────────────────────────────────────

# Matches a single Ports: line. Per LADSPA SDK format:
#     "<name>" <input|output>, <audio|control>[, toggled][, integer]
#       [, logarithmic][, <min> to <max>][, default <num>]
#
# The name may contain commas (rare but legal in LSP) so we anchor on
# the quotes. After the closing quote we get a comma-separated list of
# attribute tokens we scan for known hint keywords.
_PORT_LINE_RE = re.compile(r'^\s*"(?P<name>[^"]+)"\s+(?P<attrs>.+?)\s*$')

# A range hint: "0 to 1000" or "-60 to 60" or "0 to 1, logarithmic"
_RANGE_RE = re.compile(r"(?P<min>-?\d+(?:\.\d+)?)\s+to\s+(?P<max>-?\d+(?:\.\d+)?)")
_DEFAULT_RE = re.compile(r"default\s+(?P<val>-?\d+(?:\.\d+)?)")


def parse_analyseplugin_output(output: str, *, library: str, label: str) -> PluginDescriptor:
    """Parse analyseplugin stdout into a PluginDescriptor.

    Best-effort: unknown lines are skipped. The parser tolerates the
    formatting drift between LADSPA SDK versions (some emit one port
    per line, some wrap, some pad with tabs vs spaces).
    """
    name = ""
    maker = ""
    controls: list[PluginControl] = []
    in_ports = False

    for raw_line in output.splitlines():
        # Header fields — match before we enter the Ports: section so
        # a port whose name happens to be "Plugin Name" doesn't fool us.
        if not in_ports:
            if raw_line.startswith("Plugin Name:"):
                name = _strip_quoted(raw_line.split(":", 1)[1])
                continue
            if raw_line.startswith("Maker:"):
                maker = _strip_quoted(raw_line.split(":", 1)[1])
                continue
            if raw_line.startswith("Ports:"):
                in_ports = True
                # The first port may already be on the Ports: line —
                # the SDK formats it as `Ports:\t"name" ...`.
                rest = raw_line.split(":", 1)[1].strip()
                if rest:
                    parsed = _parse_port_line(rest)
                    if parsed is not None:
                        controls.append(parsed)
                continue

        if in_ports:
            stripped = raw_line.strip()
            if not stripped:
                # Blank line ends the Ports: block in some SDK builds.
                in_ports = False
                continue
            parsed = _parse_port_line(stripped)
            if parsed is None:
                # Likely a continuation or unrelated trailer ("Run-time
                # licensing..." etc.) — break out, don't keep gobbling.
                in_ports = False
                continue
            controls.append(parsed)

    return PluginDescriptor(
        library=library,
        label=label,
        name=name,
        maker=maker,
        controls=tuple(controls),
    )


def _strip_quoted(s: str) -> str:
    s = s.strip()
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        return s[1:-1]
    return s


def _parse_port_line(line: str) -> PluginControl | None:
    """Parse one Ports: line. Returns None when the line doesn't match
    a port pattern (signals the parser to stop scanning the Ports
    block). Returns a PluginControl with direction="" for audio ports
    — caller filters those out before exposing the schema."""
    m = _PORT_LINE_RE.match(line)
    if not m:
        return None
    port_name = m.group("name")
    attrs = m.group("attrs")
    # Strip an optional trailing comment in parentheses some SDKs add
    # for output ports, e.g. " (HardRTCapable)".
    parts = [p.strip() for p in attrs.split(",")]

    direction = ""
    kind = ""
    if parts:
        first = parts[0].split()
        if first:
            direction = first[0].lower()
        if len(first) > 1:
            kind = first[1].lower()
        if len(parts) > 1 and not kind:
            kind = parts[1].split()[0].lower()

    # Audio ports — not surfaced in the schema. Returning the control
    # with direction="" lets the caller spot them and skip.
    if kind == "audio":
        return PluginControl(name=port_name, direction="")

    toggled = any(p == "toggled" for p in parts)
    integer = any(p == "integer" for p in parts)
    logarithmic = any(p == "logarithmic" for p in parts)
    minimum: float | None = None
    maximum: float | None = None
    default: float | None = None
    joined = ", ".join(parts)
    rm = _RANGE_RE.search(joined)
    if rm:
        minimum = float(rm.group("min"))
        maximum = float(rm.group("max"))
    dm = _DEFAULT_RE.search(joined)
    if dm:
        default = float(dm.group("val"))

    return PluginControl(
        name=port_name,
        direction="input" if direction == "input" else "output",
        toggled=toggled,
        integer=integer,
        logarithmic=logarithmic,
        minimum=minimum,
        maximum=maximum,
        default=default,
    )
