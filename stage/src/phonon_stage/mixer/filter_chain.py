"""Filter-chain conf body generator for Output plugin chains.

PipeWire's module-filter-chain takes an SPA-JSON-ish conf describing
its capture/playback streams and the plugin graph. We generate one
conf per Output that has at least one PluginInsert. The chain captures
from `phonon_master` (sink-capture: reads what's been written to the
master null-sink) and plays into the output's actual sink. The
plugins sit in series between them.

Kept as a separate module so the body is unit-testable without a
PipeWire backend in the loop — and so the same generator drives the
Fake backend (which records the body string) and the Real one
(which writes it to disk).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from phonon_stage.mixer.models import Output, PluginInsert

# Chain identity helpers. The chain name doubles as the PipeWire
# node.name and the conf file's basename, so the rest of the system
# can find the chain by output id alone.
CHAIN_NAME_PREFIX = "phonon_fx_"
# Master chain identity. Single chain for the whole master bus —
# unique fixed name (no id suffix since there's only one master).
MASTER_CHAIN_NAME = "phonon_master_fx"
# The post-master null-sink. Created when the master chain is active.
# When the master has no enabled inserts, this sink + chain don't
# exist at all and outputs read directly from phonon_master.monitor.
MASTER_POST_SINK_NAME = "phonon_master_post"

# Bus identity helpers. Each Bus owns:
#   * `phonon_bus_<id>` — the null-sink every source send writes to
#   * `phonon_bus_<id>_post` — only when the bus has at least one
#     insert: the sink the bus's filter-chain plays into. The master
#     loopback then reads from `phonon_bus_<id>_post.monitor`.
#   * `phonon_bus_fx_<id>` — node.name of the bus filter-chain, when
#     active.
BUS_SINK_NAME_PREFIX = "phonon_bus_"
BUS_POST_SINK_SUFFIX = "_post"
BUS_CHAIN_NAME_PREFIX = "phonon_bus_fx_"


def bus_sink_name(bus_id: str) -> str:
    """PW node name of the bus's main null-sink. Stable across daemon
    restarts as long as the bus id is stable."""
    return f"{BUS_SINK_NAME_PREFIX}{bus_id}"


def bus_post_sink_name(bus_id: str) -> str:
    """PW node name of the bus's post-chain null-sink (only present
    when the bus has at least one insert)."""
    return f"{BUS_SINK_NAME_PREFIX}{bus_id}{BUS_POST_SINK_SUFFIX}"


def bus_chain_name_for(bus_id: str) -> str:
    """PW node.name + conf filename of the bus's filter-chain (only
    present when the bus has at least one insert)."""
    return f"{BUS_CHAIN_NAME_PREFIX}{bus_id}"


def chain_name_for(output: Output) -> str:
    """Stable PW node name for the chain. Tied to the output id, not
    to the underlying sink — keeps the name constant if the user
    repoints the output to a different sink (e.g. DG60 #1 → DG60 #2)."""
    return f"{CHAIN_NAME_PREFIX}{output.id}"


def render_filter_chain_conf(
    output: Output,
    inserts: Sequence[PluginInsert] | PluginInsert,
    master_sink_name: str,
) -> str:
    """Produce the conf body for one Output's filter-chain.

    `inserts` is either a sequence of PluginInsert (new multi-plugin
    chain shape) OR a single PluginInsert (back-compat with the v1
    callers that passed `output.insert`). Single → wrapped in a 1-tuple.

    The chain renders one filter-chain node per insert, in order, plus
    an explicit `links = [...]` block wiring fx_0:out → fx_1:in →
    fx_2:in → … → fx_N. PipeWire's filter-chain does NOT auto-link
    sibling nodes in the same `nodes = [...]` list (it routes the
    capture stream to fx_0 and reads playback from the last node,
    but everything in between has to be wired by hand or the audio
    dead-ends).

    Port names assume the LSP convention ("Input L", "Input R",
    "Output L", "Output R") — true for all of our Phonon Native
    plugins. Non-LSP LADSPA plugins with different port names would
    need a per-plugin layout override.

    Inserts whose `.enabled=False` are skipped entirely (their
    controls are kept in state but the conf omits them so the audio
    bypasses).
    """
    # Normalize the single-vs-list polymorphism. The None guard is
    # defensive — legacy state had `insert: PluginInsert | None` and
    # some old callers handed us None; new callers always pass a tuple.
    chain: list[PluginInsert]
    if inserts is None:
        chain = []
    elif _is_single_insert(inserts):
        # _is_single_insert narrows to PluginInsert at runtime; mypy
        # doesn't track the duck-type narrowing so we cast.
        single: PluginInsert = inserts  # type: ignore[assignment]
        chain = [single]
    else:
        seq: Sequence[PluginInsert] = inserts  # type: ignore[assignment]
        chain = [i for i in seq if i is not None]

    enabled = [i for i in chain if i.enabled]

    chain_id = chain_name_for(output)
    description = f"Phonon FX — {output.label or output.sink_node_name}"

    if not enabled:
        # All bypassed. We still need a valid filter-chain conf so the
        # node.name is present in PW (the mixer reconcile loop counts
        # on its existence to decide replace-vs-keep), but the graph
        # is a single passthrough node. SPA's `builtin copy` does the
        # job — it adds zero CPU and zero latency.
        return _passthrough_conf(chain_id, description, output, master_sink_name)

    nodes_body = "\n".join(_render_plugin_node(i, idx) for idx, i in enumerate(enabled))
    links_body = _render_chain_links(len(enabled))

    return f"""# Generated by phonon-stage. Do not edit by hand —
# changes are overwritten on the next mixer reconcile.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.name = {chain_id}
      node.description = "{description}"
      media.name = "{description}"
      filter.graph = {{
        nodes = [
{nodes_body}
        ]{links_body}
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.target = {master_sink_name}
        stream.capture.sink = true
      }}
      playback.props = {{
        node.target = "{output.sink_node_name}"
      }}
    }}
  }}
]
"""


def render_master_filter_chain_conf(
    inserts: Sequence[PluginInsert],
    master_sink_name: str,
    post_sink_name: str = MASTER_POST_SINK_NAME,
) -> str:
    """Produce the conf for the MASTER bus filter-chain.

    Topology when active:
        sources → master_sink (phonon_master, the summing point)
        master_sink.monitor → master_fx_chain.capture
        master_fx_chain → playback → post_sink (phonon_master_post)
        post_sink.monitor → output chains / loopbacks (per-output)

    All but the wiring is identical to render_filter_chain_conf —
    nodes block, links block, capture from a sink-monitor, playback
    to another null-sink. Same fx_<N> node naming, same LSP port
    convention. We're just feeding a different src/dst pair.
    """
    chain: list[PluginInsert] = [i for i in inserts if i is not None]
    enabled = [i for i in chain if i.enabled]
    description = "Phonon Master FX"

    if not enabled:
        # All bypassed — passthrough conf so the node.name stays in
        # PW for the reconcile diff to recognise.
        return _master_passthrough_conf(master_sink_name, post_sink_name, description)

    nodes_body = "\n".join(_render_plugin_node(i, idx) for idx, i in enumerate(enabled))
    links_body = _render_chain_links(len(enabled))

    return f"""# Generated by phonon-stage. Do not edit by hand —
# changes are overwritten on the next mixer reconcile.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.name = {MASTER_CHAIN_NAME}
      node.description = "{description}"
      media.name = "{description}"
      filter.graph = {{
        nodes = [
{nodes_body}
        ]{links_body}
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.target = {master_sink_name}
        stream.capture.sink = true
      }}
      playback.props = {{
        node.target = "{post_sink_name}"
      }}
    }}
  }}
]
"""


def render_bus_filter_chain_conf(
    bus_id: str,
    bus_label: str,
    inserts: Sequence[PluginInsert],
    bus_sink: str,
    bus_post_sink: str,
) -> str:
    """Produce the conf for a Bus's filter-chain.

    Topology when active:
        sources → bus_sink (phonon_bus_<id>, the bus summing point)
        bus_sink.monitor → bus_fx_chain.capture
        bus_fx_chain → playback → bus_post_sink (phonon_bus_<id>_post)
        bus_post_sink.monitor → master loopback

    Mirrors render_master_filter_chain_conf but with a bus-scoped
    chain_name so multiple buses can each carry their own FX without
    PW node.name collisions.
    """
    chain: list[PluginInsert] = [i for i in inserts if i is not None]
    enabled = [i for i in chain if i.enabled]
    chain_id = bus_chain_name_for(bus_id)
    description = f"Phonon Bus FX — {bus_label or bus_id}"

    if not enabled:
        return _bus_passthrough_conf(chain_id, bus_sink, bus_post_sink, description)

    nodes_body = "\n".join(_render_plugin_node(i, idx) for idx, i in enumerate(enabled))
    links_body = _render_chain_links(len(enabled))

    return f"""# Generated by phonon-stage. Do not edit by hand —
# changes are overwritten on the next mixer reconcile.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.name = {chain_id}
      node.description = "{description}"
      media.name = "{description}"
      filter.graph = {{
        nodes = [
{nodes_body}
        ]{links_body}
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.target = {bus_sink}
        stream.capture.sink = true
      }}
      playback.props = {{
        node.target = "{bus_post_sink}"
      }}
    }}
  }}
]
"""


def _bus_passthrough_conf(
    chain_id: str, bus_sink: str, bus_post_sink: str, description: str
) -> str:
    """Conf for a fully-bypassed bus chain. Same idea as
    _master_passthrough_conf — keeps the node.name visible to PW so
    the reconcile diff recognises an existing-but-bypassed bus chain."""
    return f"""# Generated by phonon-stage. All bus plugins bypassed.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.name = {chain_id}
      node.description = "{description} (bypassed)"
      media.name = "{description}"
      filter.graph = {{
        nodes = [
          {{
            type = builtin
            name = fx_bypass
            label = copy
          }}
        ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.target = {bus_sink}
        stream.capture.sink = true
      }}
      playback.props = {{
        node.target = "{bus_post_sink}"
      }}
    }}
  }}
]
"""


def _master_passthrough_conf(master_sink_name: str, post_sink_name: str, description: str) -> str:
    """Conf for a fully-bypassed master chain. Single builtin copy
    node so the master_fx node.name stays present in PW (the reconcile
    diff uses it) but no LADSPA plugin is loaded."""
    return f"""# Generated by phonon-stage. All master plugins bypassed.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.name = {MASTER_CHAIN_NAME}
      node.description = "{description} (bypassed)"
      media.name = "{description}"
      filter.graph = {{
        nodes = [
          {{
            type = builtin
            name = fx_bypass
            label = copy
          }}
        ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.target = {master_sink_name}
        stream.capture.sink = true
      }}
      playback.props = {{
        node.target = "{post_sink_name}"
      }}
    }}
  }}
]
"""


def master_chain_active(inserts: Sequence[PluginInsert]) -> bool:
    """Predicate: True when the master chain conf should be loaded
    (at least one insert exists, regardless of enabled state).
    Used by the service to decide whether to create the
    phonon_master_post null-sink + re-point outputs."""
    return any(True for _ in inserts)


def _render_chain_links(n_plugins: int) -> str:
    """Generate the `links = [...]` block wiring fx_<i>:Output L/R →
    fx_<i+1>:Input L/R for every consecutive plugin pair. Returns an
    empty string for a single-plugin chain (capture/playback streams
    auto-route to the only node when there's nothing to chain)."""
    if n_plugins <= 1:
        return ""
    lines: list[str] = []
    for i in range(n_plugins - 1):
        lines.append(f'          {{ output = "fx_{i}:Output L"  input = "fx_{i + 1}:Input L" }}')
        lines.append(f'          {{ output = "fx_{i}:Output R"  input = "fx_{i + 1}:Input R" }}')
    body = "\n".join(lines)
    return f"\n        links = [\n{body}\n        ]"


def _is_single_insert(obj: object) -> bool:
    """True for a bare PluginInsert (back-compat single-plugin call)."""
    # Avoid importing PluginInsert at top level (TYPE_CHECKING block);
    # duck-type by checking attributes the dataclass exposes.
    return (
        hasattr(obj, "backend")
        and hasattr(obj, "library")
        and hasattr(obj, "label")
        and hasattr(obj, "controls")
        and not hasattr(obj, "__iter__")
    )


def _render_plugin_node(insert: PluginInsert, idx: int) -> str:
    """One `nodes = [ … ]` entry. The unique node `name` (fx_0, fx_1…)
    matters when several plugins exist in the same chain — PW uses it
    to identify which node a `pw-cli set-param` targets."""
    controls_body = _render_controls(insert.controls)
    return f"""          {{
            type = {insert.backend}
            name = fx_{idx}
            plugin = "{insert.library}"
            label = "{insert.label}"
            control = {{
{controls_body}
            }}
          }}"""


def _passthrough_conf(
    chain_id: str, description: str, output: Output, master_sink_name: str
) -> str:
    """Conf for a fully-bypassed chain. Single builtin copy node so
    the PW node exists with the expected name but adds nothing to the
    signal."""
    return f"""# Generated by phonon-stage. All plugins bypassed.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    flags = [ nofail ]
    args = {{
      node.name = {chain_id}
      node.description = "{description} (bypassed)"
      media.name = "{description}"
      filter.graph = {{
        nodes = [
          {{
            type = builtin
            name = fx_bypass
            label = copy
          }}
        ]
      }}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{
        node.target = {master_sink_name}
        stream.capture.sink = true
      }}
      playback.props = {{
        node.target = "{output.sink_node_name}"
      }}
    }}
  }}
]
"""


def _render_controls(controls: dict[str, float]) -> str:
    """Render a {name → value} dict as SPA-JSON `key = value` lines.
    Names with spaces or special chars get quoted; bare-word safe
    names don't, to keep the conf readable for the LADSPA-savvy
    operator skimming the file."""
    lines: list[str] = []
    for name in sorted(controls.keys()):  # deterministic order — easier diffs
        value = controls[name]
        key = _quote_key_if_needed(name)
        lines.append(f"              {key} = {_render_value(value)}")
    return "\n".join(lines)


def _quote_key_if_needed(name: str) -> str:
    if name.isidentifier():
        return name
    # Escape embedded quotes defensively even though LSP labels don't
    # contain them today — cheap and future-proof.
    safe = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'


def _render_value(value: float) -> str:
    """Render a control value as bare numeric. PW accepts integers
    without a decimal point and treats toggles as 0/1, so we drop
    the trailing `.0` on whole numbers for a less noisy conf file."""
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return repr(f)
