"""Tools exposed to the agent via the Claude Agent SDK MCP wrapper.

Each module here defines one or more `@tool`-decorated coroutines. The
`agent` module imports them and bundles them into a single in-process
MCP server. Keep tool surfaces narrow — one verb, one return shape — so
the model can compose them without prompt gymnastics.

Page scoping
------------
Tools are split into three buckets so the chat panel can run in a
page-scoped mode (eeschema panel vs pcbnew panel):

  SCHEMATIC_TOOLS — only available when the chat is launched from
                    KiCad's Schematic Editor (eeschema). Mutate or
                    inspect `.kicad_sch` files.
  PCB_TOOLS       — only available when the chat is launched from
                    KiCad's PCB Editor (pcbnew). Mutate or inspect
                    `.kicad_pcb` files.
  COMMON_TOOLS    — available on both pages. Whole-project verbs
                    (ship_design, export_docs) plus pure-read helpers.

`tools_for_app(app)` returns the active list. `ALL_TOOLS` keeps the
full registry for any caller that needs everything (kicad-cli wrappers
during dev, smoke tests, etc.).
"""
from .apply_ops import apply_ops
from .assess_request import assess_request
from .audit_diff_pairs import audit_diff_pairs
from .audit_wires import audit_wires
from .plan_diff_pair_routes import plan_diff_pair_routes
from .auto_fiducials_pcb import auto_fiducials_pcb
from .auto_mounting_holes_pcb import auto_mounting_holes_pcb
from .auto_outline_pcb import auto_outline_pcb
from .auto_place_pcb import auto_place_pcb
from .auto_thermal_vias_pcb import auto_thermal_vias_pcb
from .auto_zones_pcb import auto_zones_pcb
from .pcb_verify import pcb_verify
from .pcb_quality import pcb_quality
from .pcb_improve import pcb_improve
from .route_pcb_simple import route_pcb_simple
from .silkscreen_cleanup_pcb import silkscreen_cleanup_pcb
from .set_track_widths_pcb import set_track_widths_pcb
from .auto_layout_pcb import auto_layout_pcb
from .build_circuit import build_circuit
from .generate_pcb import generate_pcb
from .create_project import create_project
from .combine_sheets import combine_sheets
from .convert_to_hierarchy import convert_to_hierarchy
from .create_symbol import create_symbol
from .drc_autofix import drc_autofix
from .drc_check import drc_check
from .erc_autofix import erc_autofix
from .erc_check import erc_check
from .export_bom import export_bom
from .export_docs import export_docs
from .export_pcb import export_pcb
from .lint_schematic import lint_schematic
from .read_schematic import read_schematic
from .schematic_quality import schematic_quality
from .render_pcb_3d import render_pcb_3d
from .set_design_rules import set_design_rules
from .ship_design import ship_design
from .trace_net import trace_net

# Schematic-side tools — every verb here reads or edits a .kicad_sch.
# Running these from pcbnew makes no sense (the user is not looking at
# the schematic) so the agent does not expose them there.
SCHEMATIC_TOOLS = [
    read_schematic,
    assess_request,
    create_project,
    create_symbol,
    build_circuit,
    apply_ops,
    erc_check,
    erc_autofix,
    trace_net,
    convert_to_hierarchy,
    audit_wires,
    lint_schematic,
    schematic_quality,
    combine_sheets,
    export_bom,
]

# PCB-side tools — every verb here reads or edits a .kicad_pcb. Running
# these from eeschema makes no sense.
PCB_TOOLS = [
    drc_check,
    drc_autofix,
    export_pcb,
    auto_place_pcb,
    auto_outline_pcb,
    auto_zones_pcb,
    auto_mounting_holes_pcb,
    auto_fiducials_pcb,
    auto_thermal_vias_pcb,
    render_pcb_3d,
    set_design_rules,
    route_pcb_simple,
    silkscreen_cleanup_pcb,
    set_track_widths_pcb,
    auto_layout_pcb,
    pcb_verify,
    pcb_quality,
    pcb_improve,
    audit_diff_pairs,
    plan_diff_pair_routes,
]

# Whole-project tools — touch BOTH .kicad_sch and .kicad_pcb (or are
# read-only and useful regardless of which panel the user opened from).
# Available on every page so "ship it" / "package docs" works either way.
COMMON_TOOLS = [
    generate_pcb,
    ship_design,
    export_docs,
]

ALL_TOOLS = SCHEMATIC_TOOLS + PCB_TOOLS + COMMON_TOOLS


def tools_for_app(app: str | None):
    """Return the tool list to expose for a given chat-panel context.

    Args:
      app: "schematic" (eeschema panel), "pcb" (pcbnew panel), or any
           other value / None for the unrestricted bundle.

    Returns the matching tool list. Unknown / missing `app` falls back
    to ALL_TOOLS so dev-mode CLI calls and existing callers that
    haven't passed an app yet keep working.
    """
    a = (app or "").strip().lower()
    if a in ("schematic", "sch", "eeschema"):
        return SCHEMATIC_TOOLS + COMMON_TOOLS
    if a in ("pcb", "pcbnew", "board"):
        return PCB_TOOLS + COMMON_TOOLS
    return ALL_TOOLS


__all__ = [
    "ALL_TOOLS", "SCHEMATIC_TOOLS", "PCB_TOOLS", "COMMON_TOOLS",
    "tools_for_app",
    "read_schematic", "assess_request", "create_project", "create_symbol",
    "build_circuit", "generate_pcb", "apply_ops",
    "erc_check", "erc_autofix", "drc_check", "drc_autofix",
    "export_pcb", "export_bom", "auto_place_pcb",
    "auto_outline_pcb", "auto_zones_pcb",
    "auto_mounting_holes_pcb", "auto_fiducials_pcb",
    "auto_thermal_vias_pcb",
    "render_pcb_3d",
    "set_design_rules",
    "route_pcb_simple", "silkscreen_cleanup_pcb", "set_track_widths_pcb",
    "auto_layout_pcb",
    "pcb_verify", "pcb_quality", "pcb_improve",
    "ship_design", "export_docs", "convert_to_hierarchy",
    "audit_wires", "audit_diff_pairs", "plan_diff_pair_routes",
    "combine_sheets", "trace_net", "lint_schematic", "schematic_quality",
]
