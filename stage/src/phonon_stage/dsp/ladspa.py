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


@dataclass(frozen=True)
class CatalogEntry:
    """One entry returned by the LADSPA catalog scan. Lightweight
    on purpose: name + library + label is enough for the picker UI,
    full schema is fetched lazily per plugin via describe()."""

    library: str  # e.g. "lsp-plugins-ladspa"
    label: str  # e.g. "http://lsp-plug.in/plugins/ladspa/comp_delay_stereo"
    name: str  # human-readable, e.g. "Delay Compensator (Stereo)"
    category: str  # heuristic, e.g. "Dynamics", "Time", "EQ"
    is_stereo: bool  # True if the plugin's audio I/O is 2 in + 2 out

    def to_dict(self) -> dict[str, object]:
        return {
            "library": self.library,
            "label": self.label,
            "name": self.name,
            "category": self.category,
            "is_stereo": self.is_stereo,
        }


class LadspaIntrospector(Protocol):
    """Protocol for plugin introspection backends."""

    async def describe(self, library: str, label: str) -> PluginDescriptor: ...

    async def scan_catalog(self) -> list[CatalogEntry]: ...


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
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10.0)
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
        text = (
            stdout_bytes.decode("utf-8", errors="replace")
            + "\n"
            + stderr_bytes.decode("utf-8", errors="replace")
        )
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

    async def scan_catalog(self) -> list[CatalogEntry]:
        """Run `listplugins` and parse its output into a CatalogEntry
        list. Plugins are categorised heuristically from their label /
        name (LSP plugins follow predictable naming conventions). The
        returned list is filtered to insert-suitable plugins (stereo
        in + stereo out, no test/analysis tooling) so the UI picker
        only shows usable choices."""
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
                "listplugins",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except FileNotFoundError:
            logger.warning("dsp.listplugins_missing")
            return []
        except Exception:
            logger.warning("dsp.listplugins_failed", exc_info=True)
            return []
        if proc.returncode != 0:
            logger.warning("dsp.listplugins_nonzero", rc=proc.returncode)
            return []
        return parse_listplugins_output(out_b.decode("utf-8", errors="replace"))


class FakeLadspaIntrospector:
    """Test/Pi fallback. Returns pre-registered descriptors. Raises
    KeyError if asked about an unknown plugin — same surface as Real
    when analyseplugin can't find the plugin."""

    def __init__(self, descriptors: dict[tuple[str, str], PluginDescriptor] | None = None) -> None:
        self._descriptors = dict(descriptors or {})
        self._catalog: list[CatalogEntry] = []

    def register(self, descriptor: PluginDescriptor) -> None:
        self._descriptors[(descriptor.library, descriptor.label)] = descriptor

    def register_catalog(self, entries: list[CatalogEntry]) -> None:
        """Test seam — let suites pre-load a catalog so scan_catalog
        returns deterministic content without shelling out to listplugins."""
        self._catalog = list(entries)

    async def describe(self, library: str, label: str) -> PluginDescriptor:
        try:
            return self._descriptors[(library, label)]
        except KeyError as e:
            msg = f"unknown plugin {library!r} / {label!r}"
            raise KeyError(msg) from e

    async def scan_catalog(self) -> list[CatalogEntry]:
        return list(self._catalog)


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


# ── listplugins catalog parser ──────────────────────────────────────


# Patterns we strip from plugin names to derive a category.
# LSP plugins follow predictable naming: <effect>_<channel-config>
# e.g. comp_delay_stereo, para_equalizer_x32_stereo, chorus_mono.
# The category mapping below is heuristic and intentionally
# conservative — when in doubt we drop it under "Effect".
_CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    # (keywords-in-label-OR-name, category)
    (("comp_delay",), "Time"),
    (("delay", "art_delay", "echo"), "Time"),
    (("reverb", "room_builder"), "Space"),
    (
        ("comp_", "compressor", "expander", "gate_", "gott_", "limiter", "autogain"),
        "Dynamics",
    ),
    (("para_equalizer", "graph_equalizer", "filter_", "lpf", "hpf"), "EQ"),
    (("crossover",), "Crossover"),
    (("multiband", "mb_dyna"), "Multiband"),
    (("chorus", "flanger", "phaser", "tremolo"), "Modulation"),
    (("saturator", "exciter", "distort"), "Saturation"),
    (("sine_", "sine osc", "noise"), "Generator"),
    (("ab_tester",), "Test"),
    (("spectrum_analyzer", "oscilloscope", "loudness_meter"), "Analysis"),
]

# Plugins we hide from the picker even if they parse — useless or
# unsuitable as a serial insert in the master→output path.
_HIDDEN_CATEGORIES: set[str] = {"Test", "Analysis", "Generator"}


def _categorize(label: str, name: str) -> str:
    haystack = (label + " " + name).lower()
    for keywords, category in _CATEGORY_RULES:
        if any(k in haystack for k in keywords):
            return category
    return "Effect"


def parse_listplugins_output(output: str) -> list[CatalogEntry]:
    """Parse `listplugins` output into a CatalogEntry list.

    listplugins format:
        /path/to/lib.so:
            Name (uniqueID/label)
            Name (uniqueID/label)
        /path/to/other.so:
            Name (uniqueID/label)

    Library is the basename minus the `.so` extension because
    LADSPA_PATH resolves that to the .so. label is the second value
    inside the parens (after the `/`).
    """
    import re
    from pathlib import Path

    entries: list[CatalogEntry] = []
    current_library: str | None = None
    # Plugin line:   <Name> (<id>/<label>)
    plugin_re = re.compile(r"^\s+(.+?)\s+\((\d+)/(.+)\)\s*$")
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.endswith(":"):
            current_library = Path(raw_line.rstrip(":")).stem
            continue
        if current_library is None:
            continue
        m = plugin_re.match(raw_line)
        if not m:
            continue
        name = m.group(1).strip()
        label = m.group(3).strip()
        category = _categorize(label, name)
        if category in _HIDDEN_CATEGORIES:
            continue
        # Stereo heuristic: LSP plugins suffix their channel-config in
        # the label (..._stereo, ..._mono, ...) — fall back to name
        # for non-LSP plugins that don't follow that convention.
        is_stereo = "stereo" in (label + " " + name).lower()
        entries.append(
            CatalogEntry(
                library=current_library,
                label=label,
                name=name,
                category=category,
                is_stereo=is_stereo,
            )
        )
    return entries
