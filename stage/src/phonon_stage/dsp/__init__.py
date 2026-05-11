"""DSP (insert) plugin introspection.

This module is distinct from `phonon_stage.plugins` (which manages
upstream source daemons like shairport-sync). Here we describe
LADSPA plugins that the mixer splices into an Output's signal path
via PipeWire's module-filter-chain.

The introspector reads a plugin's control ports + ranges from the
LADSPA descriptor and surfaces them as a schema the UI auto-renders.
No control name is hardcoded — the user can fit any LADSPA-LSP
plugin into the same UI machinery.

Scope: stage-x99 (Optiplex, x86_64) only. Pis don't carry LSP
plugins in v1 (see CLAUDE.md memory project_plugins_scope).
"""

from phonon_stage.dsp.ladspa import (
    FakeLadspaIntrospector,
    LadspaIntrospector,
    PluginControl,
    PluginDescriptor,
    RealLadspaIntrospector,
    parse_analyseplugin_output,
)

__all__ = [
    "FakeLadspaIntrospector",
    "LadspaIntrospector",
    "PluginControl",
    "PluginDescriptor",
    "RealLadspaIntrospector",
    "parse_analyseplugin_output",
]
