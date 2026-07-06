"""Engine — TopologyIR → .kicad_sch (KiCad 9 s-expression).

Stage 5 of the pipeline. Deterministic: given the same IR, always
produces the same output. Flat-render only in this first cut;
hierarchical (multi-sheet) lands in v2.

Output strategy: build the file as an indented-string template, NOT
as a sexpdata object. KiCad's parser is strict about whitespace, tab
indentation, and quoted-string conventions; assembling strings keeps
the output byte-identical to hand-edited files and easy to diff.

Placement: anchor IC at sheet centre (148, 105); satellites on a
30 mm grid in concentric rings. Wires: L-shape pin-to-pin for nets
≤ 2 pins, chained L-shapes for nets with > 2 pins. Power nets emit
global labels at every pin AND a PWR_FLAG at the first pin so ERC's
"power_pin_not_driven" doesn't fire.
"""
from __future__ import annotations

import json as _json
import math
import os
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

# LangSmith — render() is the slow deterministic node we want timed per
# build. We expose the engine pipeline as nested spans (place → route →
# write) so the trace tree reads like a real EE workflow, not one
# opaque "engine.render" blob. Both helpers no-op when langsmith isn't
# installed so the engine still runs in pure-Python environments.
try:
    from langsmith import traceable
except ImportError:
    def traceable(*args, **kwargs):  # type: ignore
        def _wrap(fn):
            return fn
        return _wrap if not args else args[0]

from ..kicad.symbol_geom import (
    SymbolGeom,
    label_justify_for_side,
    load_symbol,
    pin_side_from_rot,
    place_pin,
)
from .ir import IRComponent, IRNet, TopologyIR


_LAYOUT_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def _load_layout_config() -> Dict[str, Any]:
    """Load envil_agent/config/layout_config.json once, cache it.

    Returns {} when the file is missing or malformed so downstream
    `dict.get(..., default)` calls keep current behavior — the engine
    never crashes because of a broken config."""
    global _LAYOUT_CONFIG_CACHE
    if _LAYOUT_CONFIG_CACHE is not None:
        return _LAYOUT_CONFIG_CACHE
    try:
        from ..settings import resolve_tokens
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "layout_config.json"
        with cfg_path.open("r", encoding="utf-8") as f:
            # resolve_tokens rebases ${ENVIL_HOME} / legacy F:/Ki_CAD paths onto the
            # real project root, so config is portable across machines (no-op on the
            # dev machine where the root IS F:/Ki_CAD).
            _LAYOUT_CONFIG_CACHE = resolve_tokens(_json.load(f))
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        _LAYOUT_CONFIG_CACHE = {}
    return _LAYOUT_CONFIG_CACHE


_BLOCK_NAMING_CACHE: Optional[Dict[str, Any]] = None


def _load_block_naming() -> Dict[str, Any]:
    """Load envil_agent/config/block_naming.json once. Returns {} on
    any read / parse error so downstream `.get(...)` calls keep
    working with safe defaults."""
    global _BLOCK_NAMING_CACHE
    if _BLOCK_NAMING_CACHE is not None:
        return _BLOCK_NAMING_CACHE
    try:
        cfg_path = (Path(__file__).resolve().parent.parent
                    / "config" / "block_naming.json")
        with cfg_path.open("r", encoding="utf-8") as f:
            _BLOCK_NAMING_CACHE = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        _BLOCK_NAMING_CACHE = {}
    return _BLOCK_NAMING_CACHE


def _block_naming_tokens(block_name: str, index: int,
                          project_name: str = "") -> Dict[str, Any]:
    """Build the template-substitution context for a block. Pure data
    helper --- consumed by `_block_sheet_name`, `_block_filename`,
    `_block_title`."""
    nm = str(block_name or "").strip()
    return {
        "index": int(index),
        "block_name_upper": nm.upper(),
        "block_name_lower": nm.lower(),
        "block_name_title": nm.replace("_", " ").title(),
        "project_name": str(project_name or ""),
    }


def _format_block_template(tmpl: str, tokens: Dict[str, Any],
                            fallback: str) -> str:
    """Apply a template string with the supported tokens. Returns
    `fallback` when the template is empty / invalid so the engine never
    crashes on a bad config edit."""
    if not tmpl:
        return fallback
    try:
        return tmpl.format(**tokens)
    except (KeyError, IndexError, ValueError):
        return fallback


def _block_sheet_name(block_name: str, index: int,
                       project_name: str = "") -> str:
    """Compute the per-block sheet name (used in the hierarchy
    navigator + KiCad net paths). Driven by
    `sheet_name_template` in block_naming.json."""
    cfg = _load_block_naming()
    tokens = _block_naming_tokens(block_name, index, project_name)
    return _format_block_template(
        cfg.get("sheet_name_template", "{block_name_upper}"),
        tokens,
        fallback=str(block_name).upper(),
    )


def _block_filename(block_name: str, index: int,
                     project_name: str = "") -> str:
    """Compute the .kicad_sch filename for a block. Driven by
    `filename_template` in block_naming.json."""
    cfg = _load_block_naming()
    tokens = _block_naming_tokens(block_name, index, project_name)
    return _format_block_template(
        cfg.get("filename_template",
                "{index:02d}_{block_name_lower}.kicad_sch"),
        tokens,
        fallback=f"{index:02d}_{str(block_name).lower()}.kicad_sch",
    )


def _block_title(block_name: str, index: int,
                  project_name: str = "") -> str:
    """Compute the human-readable title for a block's title-block
    footer. Driven by the per-block `blocks.<NAME>.title` mapping in
    block_naming.json (when present), falling back to
    `default_title_template`."""
    cfg = _load_block_naming()
    tokens = _block_naming_tokens(block_name, index, project_name)
    blocks_map = cfg.get("blocks") or {}
    entry = blocks_map.get(str(block_name).upper()) or {}
    explicit = entry.get("title")
    if explicit:
        return _format_block_template(
            explicit, tokens, fallback=tokens["block_name_title"]
        )
    return _format_block_template(
        cfg.get("default_title_template", "{block_name_title}"),
        tokens,
        fallback=tokens["block_name_title"],
    )


def _export_preview_svg(sch_path: Path,
                         out_dir: Optional[Path] = None) -> Optional[Path]:
    """Export a .kicad_sch to SVG via kicad-cli for inline chat preview.

    Universal — takes any .kicad_sch path; no circuit-specific logic.
    All settings (cli command, flags, output filename) come from
    `layout_config.json:schematic_preview`. Returns the produced SVG
    path (or None on failure / when disabled).

    Per `feedback_no_hardcode_json_config`: every threshold and flag
    here lives in JSON, never inline. Add new export options by editing
    layout_config.json — no Python changes."""
    import subprocess
    cfg = _load_layout_config().get("schematic_preview", {})
    if not cfg.get("enabled", True):
        return None
    cli = cfg.get(
        "kicad_cli_command",
        _load_layout_config().get("erc_check", {}).get(
            "kicad_cli_command", "kicad-cli"),
    )
    sch_path = Path(sch_path)
    if not sch_path.exists():
        return None
    target_dir = Path(out_dir) if out_dir else sch_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    cmd = [str(cli), "sch", "export", "svg", "--output", str(target_dir)]
    if cfg.get("exclude_drawing_sheet", True):
        cmd.append("--exclude-drawing-sheet")
    if cfg.get("no_background_color", True):
        cmd.append("--no-background-color")
    if cfg.get("black_and_white", False):
        cmd.append("--black-and-white")
    pages = cfg.get("pages", "")
    if pages:
        cmd.extend(["--pages", str(pages)])
    cmd.append(str(sch_path))

    timeout = int(cfg.get("timeout_seconds", 30))
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    # kicad-cli writes <basename>.svg into target_dir
    expected = target_dir / (sch_path.stem + ".svg")
    return expected if expected.exists() else None


_FOOTPRINT_DEFAULTS_CACHE: Optional[Dict[str, Any]] = None

# Per-build package preference(s), set by build_circuit before invoking the
# graph (from the mandatory "<Part> package: Through-Hole/Surface-Mount" chat
# row(s) folded into the request text) and read by
# _resolve_default_footprint_traced below. Keyed by free-text part label
# (lowercased, e.g. "led", "resistor") -> "SMD"/"THT"; the "" key (if present)
# is the GLOBAL fallback used when no per-part label matches a given lib_id.
# An empty dict is byte-identical to the pre-existing THT-biased behaviour --
# this only changes anything when a build explicitly asked for a package.
# Module-global, matching the existing per-build context pattern
# (build_circuit._TURN_PARENT_RUN) -- fine for a single-user local backend
# where builds run one at a time.
_PACKAGE_PREFERENCE: Dict[str, str] = {}


def set_package_preference(pref) -> None:
    """Set the package preference(s) for the NEXT footprint resolutions.
    Accepts either a plain string ("SMD"/"THT", applied globally) for
    backward compatibility, or a dict of {part_label: "SMD"/"THT"} for
    per-component-type preferences (an "" key is the global fallback).
    Anything else (including None or "") clears it back to the default
    THT-biased tables."""
    global _PACKAGE_PREFERENCE
    if isinstance(pref, dict):
        _PACKAGE_PREFERENCE = {str(k).strip().lower(): v for k, v in pref.items()
                                if v in ("THT", "SMD")}
    elif pref in ("THT", "SMD"):
        _PACKAGE_PREFERENCE = {"": pref}
    else:
        _PACKAGE_PREFERENCE = {}


def _package_preference_for(lib_id: str) -> str:
    """Resolve the effective package preference for one lib_id: a per-part
    label match wins over the global ("") fallback. A label matches when it
    equals a friendly_prefix_aliases key whose mapped prefix matches lib_id,
    or when the label text appears literally in the lib_id (covers named ICs
    like 'ne555' matching 'Timer:NE555P'). Returns "" (no preference -> use
    the normal THT-biased tables) when nothing matches."""
    if not _PACKAGE_PREFERENCE:
        return ""
    aliases = _load_footprint_defaults().get("friendly_prefix_aliases", {}) or {}
    lib_lower = lib_id.lower()
    for label, pref in _PACKAGE_PREFERENCE.items():
        if not label:
            continue
        aliased_prefix = aliases.get(label)
        if aliased_prefix and lib_id.startswith(aliased_prefix):
            return pref
        if label.replace(" ", "") and label.replace(" ", "") in lib_lower.replace(":", "").replace("_", ""):
            return pref
    return _PACKAGE_PREFERENCE.get("", "")



def _load_footprint_defaults() -> Dict[str, Any]:
    """Load envil_agent/config/footprint_defaults.json. Same caching +
    failure semantics as _load_layout_config. Edit the JSON file to
    extend coverage for new IC families — no Python changes needed."""
    global _FOOTPRINT_DEFAULTS_CACHE
    if _FOOTPRINT_DEFAULTS_CACHE is not None:
        return _FOOTPRINT_DEFAULTS_CACHE
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "footprint_defaults.json"
        with cfg_path.open("r", encoding="utf-8") as f:
            _FOOTPRINT_DEFAULTS_CACHE = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        _FOOTPRINT_DEFAULTS_CACHE = {}
    return _FOOTPRINT_DEFAULTS_CACHE


def _resolve_default_footprint(lib_id: str,
                                geom: Optional[Any] = None) -> str:
    """Dynamic footprint resolution. Lookup order:
      1. JSON `by_lib_id` exact match on caller-supplied lib_id
      2. JSON `by_lib_id` exact match on the ALIAS-resolved lib_id
         (e.g. Timer:NE555 -> Timer:NE555P -> map hit)
      3. JSON `by_prefix` longest-prefix match (family default)
      4. The symbol's own `(property "Footprint" "...")` from the
         lib_symbol — many KiCad symbols ship with a sensible default
      5. Empty string (caller handles)

    No hardcoding in code per `feedback_no_hardcode_json_config`: edit
    the JSON file to extend, never this function.

    Thin wrapper over `_resolve_default_footprint_traced` — behaviour is
    byte-identical; it just drops the provenance tag the tracer also
    returns."""
    return _resolve_default_footprint_traced(lib_id, geom)[0]


def _resolve_default_footprint_traced(lib_id: str,
                                       geom: Optional[Any] = None
                                       ) -> Tuple[str, str]:
    """Same resolution as `_resolve_default_footprint`, but ALSO reports
    *which* lookup step produced the footprint — the provenance. The
    footprint_audit tool needs this to tell a deliberate per-part mapping
    apart from a family/symbol GUESS, per the rule "every part must carry
    an explicit package identifier; never infer it from the symbol alone."

    Returns ``(footprint, source)`` where ``source`` is one of:
      ``"by_lib_id"``      exact lib_id hit in footprint_defaults.json
      ``"alias_lib_id"``   hit after alias-resolving the lib_id
      ``"by_prefix"``      longest-prefix family default  (a guess)
      ``"symbol_default"`` the lib_symbol's own Footprint field (a guess)
      ``""``               nothing matched — footprint is empty

    The returned footprint string is exactly what `_resolve_default_footprint`
    returns (that function delegates here), so this is non-breaking."""
    cfg = _load_footprint_defaults()

    # Package preference (per-part label match, else the global fallback):
    # try the SMD alternative tables FIRST when this lib_id resolved to SMD.
    # Parts without an SMD entry (ICs, connectors, crystals -- fixed by the
    # part itself, not a generic package choice) fall through to the normal
    # tables below unchanged, so this only ever adds coverage, never removes it.
    if _package_preference_for(lib_id) == "SMD":
        fp = (cfg.get("by_lib_id_smd", {}) or {}).get(lib_id)
        if fp is not None:
            return str(fp), "by_lib_id_smd"
        by_prefix_smd = cfg.get("by_prefix_smd", {}) or {}
        best_match, best_len = "", -1
        for prefix, value in by_prefix_smd.items():
            if prefix.startswith("_"):
                continue
            if lib_id.startswith(prefix) and len(prefix) > best_len:
                best_match, best_len = str(value), len(prefix)
        if best_len > 0:
            return best_match, "by_prefix_smd"

    by_lib = cfg.get("by_lib_id", {}) or {}
    fp = by_lib.get(lib_id)
    if fp is not None:
        return str(fp), "by_lib_id"

    # Alias-resolved lookup: aliases.json maps Timer:NE555 -> Timer:NE555P;
    # the user's footprint map may key on the resolved name only.
    try:
        from ..kicad.symbol_geom import _load_aliases
        resolved = _load_aliases().get(lib_id)
        if resolved and resolved != lib_id:
            fp = by_lib.get(resolved)
            if fp is not None:
                return str(fp), "alias_lib_id"
    except Exception:
        pass

    by_prefix = cfg.get("by_prefix", {}) or {}
    best_match = ""
    best_len = -1
    for prefix, value in by_prefix.items():
        if prefix.startswith("_"):
            continue
        if lib_id.startswith(prefix) and len(prefix) > best_len:
            best_match = str(value)
            best_len = len(prefix)
    if best_len > 0:
        return best_match, "by_prefix"

    # Fall back to the symbol's own default Footprint property in the
    # lib_symbol — KiCad's stdlib ships sensible defaults for most
    # passives + connectors (Resistor_SMD:R_Cat16-2 etc).
    if geom is not None and getattr(geom, "raw_symbol_sexpr", None):
        try:
            for child in geom.raw_symbol_sexpr[1:]:
                if (isinstance(child, list) and len(child) >= 3
                        and str(child[0]).lower().endswith("property")
                        and str(child[1]) == "Footprint"):
                    val = str(child[2])
                    if val:
                        return val, "symbol_default"
        except Exception:
            pass
    return "", ""


def _resolve_part_metadata(lib_id: str, value: str,
                            geom: Optional[Any] = None) -> Dict[str, str]:
    """Return {description, manufacturer, mpn, datasheet} for a placed
    component, sourced entirely from the KiCad library symbol's own
    `(property ...)` fields (already harvested into SymbolGeom).

    Standard KiCad symbols ship Description + Datasheet on every part.
    Manufacturer / MPN are only present in vendor-curated libraries
    (Microchip, ST, Espressif, etc.) — when the library doesn't provide
    them they stay blank. To populate Manufacturer / MPN, point the
    user's sym-lib-table at a richer library, or set the field per
    instance via `apply_ops:set_property` after the render."""
    out = {"description": "", "manufacturer": "", "mpn": "", "datasheet": ""}
    if geom is not None:
        out["description"]  = getattr(geom, "description", "")  or ""
        out["datasheet"]    = getattr(geom, "datasheet", "")    or ""
        out["mpn"]          = getattr(geom, "mpn", "")          or ""
        out["manufacturer"] = getattr(geom, "manufacturer", "") or ""
    return out


# Sheet & paper geometry (A4 in mm). All positions must land on the
# 50-mil = 1.27 mm KiCad grid — otherwise wires/labels report as "off
# grid" in eeschema and lose their electrical connection.
GRID = 1.27
SHEET_W = 297.0
SHEET_H = 210.0


def _default_render_paper() -> str:
    """JSON-driven default paper size for simple flat renders (single
    IC, no blocks). Reads `hierarchy_layout.default_render_paper`."""
    cfg = _load_layout_config().get("hierarchy_layout", {}) or {}
    p = cfg.get("default_render_paper")
    if isinstance(p, str) and p:
        return p
    prio = cfg.get("page_size_priority")
    if isinstance(prio, list) and prio:
        return str(prio[0])
    return "A4"


def _connector_edge_pass(ir: TopologyIR, comps: List["PlacedComp"],
                           paper: str) -> int:
    """Universal post-placement pass: snap CONNECTORS to the sheet
    edge their function dictates (inputs LEFT, outputs RIGHT, debug
    BOTTOM-RIGHT). Detection by net-name regex against
    `connector_edge_placement.*_edge_net_patterns` in the JSON config.

    No per-circuit data. Works on every schematic that has a connector
    on an input rail (J1 named '+VIN' / 'VBUS' / 'AUDIO_IN' / ...) or
    output rail. Skipped when the connector is already within
    `snap_only_if_misplaced_mm` of its target edge — keeps the placer's
    correct decisions intact.

    Returns the number of connectors moved.
    """
    cfg = _load_layout_config().get("connector_edge_placement", {}) or {}
    if not cfg.get("enabled", False):
        return 0
    # Multi-block guard: when the IR has blocks, the zoned placer
    # already handles connector placement via each block's flow_role
    # (POWER source → left zone, IO sink → right zone). Running the
    # edge-pass on top yanks connectors OUT of their block zone and
    # snaps them to the sheet edge — IO block ends up spanning x=215 to
    # x=411 mm on A3, extending past the sheet boundary, and the
    # inside-block L-router can't reach the displaced connector. Rule
    # [feedback_wires_inside_labels_between]: inside a block use wires,
    # so the connector must stay inside its block for the wire path to
    # exist. Gate is JSON-driven (apply_with_blocks, default false).
    if ir.blocks and not cfg.get("apply_with_blocks", False):
        return 0
    # Small-circuit guard. Pushing connectors to the sheet edges on a
    # 7-component LDO turns +VIN / +VOUT into 250 mm horizontal nets, the
    # L-router gives up, and the whole sheet falls back to label-only
    # bonding (see the labels-everywhere regression user flagged on
    # ldo_3v3_demo). Rule [feedback_wires_inside_labels_between]: a
    # small flat single-IC circuit is ONE logical block, so connectors
    # must stay near the IC and the engine wires R→C / IC→cap directly.
    # Gate is JSON-driven (min_components_for_edge_snap) — no
    # hardcoded threshold per [feedback_no_hardcode_json_config].
    min_components = int(cfg.get("min_components_for_edge_snap", 0))
    if min_components > 0 and len(ir.components) < min_components and not ir.blocks:
        return 0
    import re as _re
    try:
        left_pats   = [_re.compile(p, _re.IGNORECASE)
                       for p in cfg.get("left_edge_net_patterns", [])]
        right_pats  = [_re.compile(p, _re.IGNORECASE)
                       for p in cfg.get("right_edge_net_patterns", [])]
        bottom_pats = [_re.compile(p, _re.IGNORECASE)
                       for p in cfg.get("bottom_right_net_patterns", [])]
    except _re.error:
        return 0  # bad regex in config — defer to fallback rather than crash
    lib_prefixes = list(cfg.get("connector_lib_prefixes", [
        "Connector:", "Connector_Generic:", "Connector_USB:",
    ]))
    ref_prefixes = list(cfg.get("connector_refdes_prefixes", ["J"]))
    left_inset   = float(cfg.get("left_edge_inset_mm",   18.0))
    right_inset  = float(cfg.get("right_edge_inset_mm",  18.0))
    bottom_inset = float(cfg.get("bottom_edge_inset_mm", 18.0))
    skip_dist    = float(cfg.get("snap_only_if_misplaced_mm", 25.0))

    page_w, page_h = _page_dims(paper)

    def _is_connector(c) -> bool:
        return (any(c.lib_id.startswith(p) for p in lib_prefixes)
                or any((c.ref or "").startswith(p) for p in ref_prefixes))

    def _classify(c) -> Optional[str]:
        """Walk every net the connector touches PLUS its own VALUE
        field; pick the first edge whose patterns match. Priority:
        left > right > bottom. Returns None when nothing matches —
        connector keeps its original placement.

        Why match VALUE too: ambiguous power-rail nets like '+5V' or
        '+3V3' could be input or output depending on the circuit. The
        connector's user-readable VALUE ('+VIN', '+VOUT', 'AUDIO_IN',
        'CAN_BUS') is the explicit signal of intent — universal across
        circuit types, no part-number knowledge."""
        candidates: List[str] = []
        # The connector's value first (highest-signal hint).
        if c.value:
            candidates.append(c.value)
        # Then every net it touches.
        for net in ir.nets:
            for pinref in net.pins:
                if "." in pinref and pinref.split(".", 1)[0] == c.ref:
                    candidates.append(net.name)
                    break
        for nm in candidates:
            if any(p.search(nm) for p in left_pats):
                return "left"
        for nm in candidates:
            if any(p.search(nm) for p in right_pats):
                return "right"
        for nm in candidates:
            if any(p.search(nm) for p in bottom_pats):
                return "bottom"
        return None

    moved = 0
    for c in comps:
        if not _is_connector(c):
            continue
        edge = _classify(c)
        if edge is None:
            continue
        cur_x, cur_y = c.pos[0], c.pos[1]
        if edge == "left":
            target_x = left_inset
            if abs(cur_x - target_x) <= skip_dist:
                continue
            new_pos = (target_x, cur_y)
        elif edge == "right":
            target_x = page_w - right_inset
            if abs(cur_x - target_x) <= skip_dist:
                continue
            new_pos = (target_x, cur_y)
        else:  # bottom
            target_y = page_h - bottom_inset
            if abs(cur_y - target_y) <= skip_dist:
                continue
            # Keep x but push down. For BOTTOM_RIGHT specifically place
            # on the right half by default — debug headers sit in the
            # lower-right corner of professional schematics.
            new_x = cur_x if cur_x > page_w * 0.5 else page_w - right_inset
            new_pos = (new_x, target_y)
        c.pos = _snap_grid(new_pos)
        moved += 1
    return moved


def _dynamic_paper_for_blocks(ir: TopologyIR) -> Optional[str]:
    """Size the sheet to the CIRCUIT, not to a component-count threshold.

    Picks the smallest CONFIGURED page (from `hierarchy_layout.page_sizes`)
    whose usable area gives every functional block its full, un-shrunk zone
    (`zone_w_mm × zone_h_mm`). The grid span comes from the signal-flow DAG
    (the same grid the placer uses), so the chosen paper is large enough
    that `_dynamic_zone_layout` need not shrink a zone below block size —
    which is what made 6 blocks overlap on A4. If nothing fits, returns the
    LARGEST configured paper (the ceiling — A3 by default; the engine never
    invents an A2). No paper dimensions are hardcoded in Python — everything
    comes from config. Returns None when the IR has no blocks (caller keeps
    the count-based rules for flat circuits)."""
    blocks = getattr(ir, "blocks", None) or []
    if not blocks:
        return None
    pc = _placement_cfg()
    zone_w = float(pc["zone_w_mm"])
    zone_h = float(pc["zone_h_mm"])
    margin = float(pc["page_margin_mm"])
    hier = _load_layout_config().get("hierarchy_layout", {}) or {}
    tb = hier.get("title_block_reserve_mm", {}) or {}
    title_h = float(tb.get("height_mm", 30.0))
    # Grid span: prefer the actual signal-flow DAG; otherwise arrange the
    # N blocks in a near-square grid (same shape the placer would use).
    flow_dag = _compute_flow_dag(ir)
    if flow_dag:
        cols = [c for (c, _r) in flow_dag.values()]
        rows = [r for (_c, r) in flow_dag.values()]
        col_span = (max(cols) - min(cols) + 1) if cols else 1
        row_span = (max(rows) - min(rows) + 1) if rows else 1
    else:
        n = len(blocks)
        col_span = max(1, math.ceil(math.sqrt(n)))
        row_span = max(1, math.ceil(n / col_span))
    req_w = col_span * zone_w + 2 * margin
    req_h = row_span * zone_h + 2 * margin + title_h
    # Candidate papers come ONLY from config `page_sizes` — no hardcoded
    # dimensions or paper list in Python. Sorted ascending by area; pick the
    # smallest that fits, else fall back to the LARGEST configured paper
    # (the ceiling — e.g. A3 when the project only lists A5/A4/A3, so the
    # engine never invents an A2). Add A2/A1/A0 to page_sizes if a project
    # really wants bigger sheets.
    configured = hier.get("page_sizes", {}) or {}
    papers = []
    for name, d in configured.items():
        if isinstance(d, dict) and "w_mm" in d and "h_mm" in d:
            papers.append((name, float(d["w_mm"]), float(d["h_mm"])))
    if not papers:
        return None
    papers.sort(key=lambda p: p[1] * p[2])
    for name, w, h in papers:
        if w >= req_w and h >= req_h:
            return name
    return papers[-1][0]   # largest configured paper = ceiling (no A2 invented)


def _paper_for_ir(ir: TopologyIR) -> str:
    """Pick the schematic paper size based on the IR's structure.

    When `hierarchy_layout.auto_paper.enabled=true` (default), walks the
    `rules` list and picks the FIRST rule whose component-count + block-
    count thresholds match — typically A5 for ≤8-component flat circuits,
    A4 for medium, A3 for large multi-block. Eliminates the "TL431 uses
    9% of A4" emptiness reported on 2026-05-27.

    When auto_paper is disabled, falls back to the legacy behaviour:
    `default_render_paper` for flat / `single_sheet_with_blocks_paper`
    for any IR with blocks. JSON-driven — no per-circuit logic."""
    cfg = _load_layout_config().get("hierarchy_layout", {}) or {}
    auto = cfg.get("auto_paper", {}) or {}
    # Dynamic block-fit takes priority for multi-block circuits: size the
    # page to the actual block grid so rectangles never overlap. Falls
    # through to the count-based rules for flat (block-less) circuits.
    if (auto.get("enabled", False) and auto.get("dynamic_block_fit", True)
            and ir is not None and getattr(ir, "blocks", None)):
        dyn = _dynamic_paper_for_blocks(ir)
        if dyn:
            return dyn
    if auto.get("enabled", False) and ir is not None:
        n_comp = len(getattr(ir, "components", []) or [])
        n_blk = len(getattr(ir, "blocks", []) or [])
        for rule in auto.get("rules", []) or []:
            try:
                max_c = int(rule.get("max_components", 0))
                max_b = int(rule.get("blocks", 999))
            except (TypeError, ValueError):
                continue
            if n_comp <= max_c and n_blk <= max_b:
                paper = rule.get("paper")
                if isinstance(paper, str) and paper:
                    return paper
        fallback = auto.get("fallback_paper")
        if isinstance(fallback, str) and fallback:
            return fallback
    if ir is not None and getattr(ir, "blocks", None):
        p = cfg.get("single_sheet_with_blocks_paper")
        if isinstance(p, str) and p:
            return p
    return _default_render_paper()


def _page_dims(paper: Optional[str] = None) -> Tuple[float, float]:
    """Return (page_w, page_h) in mm for `paper`. Reads
    `hierarchy_layout.page_sizes[paper]` from JSON. Used everywhere
    SHEET_W/SHEET_H were previously assumed — keeps zone layout +
    title-block math correct when the engine renders on A3/A2."""
    cfg = _load_layout_config().get("hierarchy_layout", {}) or {}
    papers = cfg.get("page_sizes", {}) or {}
    name = paper if paper else _default_render_paper()
    p = (papers.get(name)
         or papers.get(_default_render_paper())
         or {"w_mm": 297.0, "h_mm": 210.0})
    return float(p.get("w_mm", 297.0)), float(p.get("h_mm", 210.0))


def _title_block_obstacle(paper: Optional[str] = None) -> Optional[Tuple[float, float, float, float]]:
    """Bbox of the title block reserve at the bottom-right of the sheet.
    Read from layout_config.json -> hierarchy_layout. Returned to the
    routing layer so wires never cross the title-block frame and to the
    placement layer so satellites never land inside it.

    `paper=None` (the default — every internal caller passes this) picks
    `hierarchy_layout.default_render_paper` from JSON. Pass an explicit
    paper name only when the caller knows it's rendering a different
    size (hierarchy parent on A3, e.g.). Pure data-driven."""
    cfg = _load_layout_config().get("hierarchy_layout", {}) or {}
    papers = cfg.get("page_sizes", {}) or {}
    paper_name = paper if paper else _default_render_paper()
    p = (papers.get(paper_name)
         or papers.get(_default_render_paper())
         or {"w_mm": 297.0, "h_mm": 210.0})
    page_w = float(p.get("w_mm", 297.0))
    page_h = float(p.get("h_mm", 210.0))
    tb = cfg.get("title_block_reserve_mm", {}) or {}
    tb_w = float(tb.get("width_mm", 110.0))
    tb_h = float(tb.get("height_mm", 30.0))
    if tb_w <= 0 or tb_h <= 0:
        return None
    return (page_w - tb_w, page_h - tb_h, page_w, page_h)

# Sheet centre snapped to grid: 117 × 1.27 = 148.59, 83 × 1.27 = 105.41
SHEET_CENTRE = (148.59, 105.41)

# Placement grid: satellites land on multiples of CELL mm from the anchor
CELL = 30.48   # 24 × 1.27 — on grid

# Per-block zone layout — relative grid position around the anchor block
# (col, row) where (0, 0) = anchor centre, (-1, -1) = top-left zone, etc.
# Matches the canonical professional MCU-board layout: anchor IC at
# centre, power top-left, reset left, clock bottom-left, IO right,
# comm top-right, sensor right, output bottom-right.
ZONE_LAYOUT = {
    "power":   (-1, -1),
    "reset":   (-1,  0),
    "clock":   (-1,  1),
    "crystal": (-1,  1),
    "mcu":     ( 0,  0),
    "anchor":  ( 0,  0),
    "io":      ( 1,  0),
    "comm":    ( 1, -1),
    "sensor":  ( 1,  0),
    "i2c":     ( 1,  0),
    "spi":     ( 1, -1),
    "uart":    ( 1,  1),
    "usb":     ( 1, -1),
    "swd":     ( 1, -1),
    "icsp":    ( 1,  1),
    "boot":    ( 1,  1),
    "output":  ( 0,  1),
    "led":     ( 1,  1),
    "display": ( 1,  0),
    "generic": ( 0, -1),
}

# Width/height of each block zone in mm. ~110 × 85 mm fits 4-6 satellites
# around an IC and leaves a 10-15 mm channel space between adjacent
# zones — wide enough that block bounding rectangles don't bleed into
# each other and inter-block wires have room to route.
#
# These module-level constants are FALLBACKS only — every call site goes
# through `_placement_cfg()` first to read the live JSON values from
# `layout_config.json -> placement`. Keep these in sync with the JSON
# defaults so a missing config file still yields a working render.
ZONE_W = 110.0
ZONE_H = 85.0


def _placement_cfg() -> Dict[str, float]:
    """Read engine-level placement tuning from layout_config.json.
    Centralises every previously-hardcoded mm constant (zone size,
    collision clearance, power-cluster radius, spiral step, page margin)
    so different circuit densities can tune them without code edits."""
    cfg = _load_layout_config().get("placement", {}) or {}
    return {
        "satellite_gap_mm":         float(cfg.get("satellite_gap_mm",        5.08)),
        "zone_w_mm":                float(cfg.get("zone_w_mm",             ZONE_W)),
        "zone_h_mm":                float(cfg.get("zone_h_mm",             ZONE_H)),
        "collision_clearance_mm":   float(cfg.get("collision_clearance_mm", 7.62)),
        "power_cluster_radius_mm":  float(cfg.get("power_cluster_radius_mm", 12.7)),
        "child_sheet_power_cluster_radius_mm": float(
            cfg.get("child_sheet_power_cluster_radius_mm", 15.24)),
        "spiral_step_mm":           float(cfg.get("spiral_step_mm",         10.16)),
        "page_margin_mm":           float(cfg.get("page_margin_mm",         18.0)),
    }


def _routing_cfg() -> Dict[str, float]:
    """Read router tuning from layout_config.json -> routing."""
    cfg = _load_layout_config().get("routing", {}) or {}
    return {
        "obstacle_detour_clearance_mm": float(
            cfg.get("obstacle_detour_clearance_mm", 2.54)),
        "max_wire_length_mm": float(cfg.get("max_wire_length_mm", 0.0)),
    }


def _path_manhattan_length(path: List[Tuple[float, float]]) -> float:
    """Sum of |dx|+|dy| across all segments. Used to gate long signal
    wires — when total > `routing.max_wire_length_mm`, the caller drops
    the wire and falls back to global-label bonding at each pin tip.
    Keeps schematics readable on dense sheets without losing electrical
    correctness (KiCad bonds by label name)."""
    if not path or len(path) < 2:
        return 0.0
    total = 0.0
    for i in range(len(path) - 1):
        total += abs(path[i + 1][0] - path[i][0])
        total += abs(path[i + 1][1] - path[i][1])
    return total


def _snap_grid(p: Tuple[float, float]) -> Tuple[float, float]:
    """Snap (x, y) to the 50-mil schematic grid."""
    return (round(p[0] / GRID) * GRID, round(p[1] / GRID) * GRID)


def _u() -> str:
    return str(_uuid.uuid4())


def _atomize(node) -> str:
    """Render a sexpdata node back to a compact string (used to embed
    previously parsed lib symbol blocks into our output)."""
    if isinstance(node, list):
        inner = " ".join(_atomize(n) for n in node)
        return f"({inner})"
    if isinstance(node, sexpdata.Symbol):
        return node.value()
    if isinstance(node, str):
        return _qstr(node)
    if isinstance(node, bool):
        return "yes" if node else "no"
    if isinstance(node, (int, float)):
        return _num(node)
    return str(node)


def _num(v) -> str:
    f = float(v)
    if abs(f - round(f)) < 1e-6:
        return f"{f:.1f}"
    return f"{f:.4f}".rstrip("0").rstrip(".")


def _qstr(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


@dataclass
class PlacedComp:
    ref: str
    lib_id: str
    value: str
    footprint: str
    pos: Tuple[float, float]            # absolute schematic coords
    rotation: float                     # degrees
    geom: SymbolGeom
    uuid: str = field(default_factory=_u)
    pin_uuids: Dict[str, str] = field(default_factory=dict)

    def pin_abs(self, pin_key: str) -> Optional[Tuple[float, float, float]]:
        """Resolve a pin key (number or name) to absolute (x, y, rot)."""
        pin = self.geom.resolve_pin(pin_key)
        if pin is None:
            return None
        return place_pin(pin, self.pos[0], self.pos[1], self.rotation)


def _build_pin_to_component_index(ir: TopologyIR) -> Dict[str, List[Tuple[str, str]]]:
    """For each component ref, list the (net_name, pin_key) it touches.
    Used by the placer to anchor satellites adjacent to the IC pin they serve."""
    out: Dict[str, List[Tuple[str, str]]] = {c.ref: [] for c in ir.components}
    for net in ir.nets:
        for pinref in net.pins:
            if "." not in pinref:
                continue
            ref, pin_key = pinref.split(".", 1)
            if ref in out:
                out[ref].append((net.name, pin_key))
    return out


def _pick_anchor(ir: TopologyIR) -> IRComponent:
    """Anchor = the component with the most pins. For simple circuits this
    is the IC; multi-IC boards need block partitioning (a hierarchy step,
    separate from this flat placer)."""
    best = None
    best_pins = -1
    for c in ir.components:
        try:
            n = len(load_symbol(c.lib_id).pins)
        except ValueError:
            continue
        if n > best_pins:
            best, best_pins = c, n
    if best is None:
        raise ValueError("no resolvable components in IR")
    return best


def _block_has_own_anchor(block, ir: TopologyIR, min_pins: Optional[int] = None) -> bool:
    """A block is 'self-contained' when it owns its own multi-pin IC or
    connector (anything with ≥ min_pins). When all blocks are self-
    contained, the zone-layout placer can position each block separately;
    otherwise (NE555-style, where every block is just satellites around
    a single global IC) we fall back to pin-role placement.

    `min_pins` defaults to `multi_block.min_pins_for_self_contained` in
    layout_config.json (3 today). Pass an explicit value to override per
    call. Keeps the per-call override path for tests."""
    if min_pins is None:
        min_pins = (
            _load_layout_config()
            .get("multi_block", {})
            .get("min_pins_for_self_contained", 3)
        )
    for ref in block.component_refs:
        comp = ir.component_by_ref(ref)
        if comp is None:
            continue
        try:
            n = len(load_symbol(comp.lib_id).pins)
        except ValueError:
            continue
        if n >= min_pins:
            return True
    return False


_BLOCK_RULES_CACHE: Optional[Dict[str, Any]] = None


def _load_block_rules() -> Dict[str, Any]:
    """Read block_rules.json once. Returns {} on any read or parse
    error so the caller falls back to no-exemption (strict-anchor)
    behaviour."""
    global _BLOCK_RULES_CACHE
    if _BLOCK_RULES_CACHE is not None:
        return _BLOCK_RULES_CACHE
    try:
        cfg_path = (Path(__file__).resolve().parent.parent
                    / "config" / "block_rules.json")
        with cfg_path.open("r", encoding="utf-8") as f:
            _BLOCK_RULES_CACHE = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        _BLOCK_RULES_CACHE = {}
    return _BLOCK_RULES_CACHE


def _refdes_letters_prefix(ref: str) -> str:
    """Leading alphabetical run of a refdes ("R12" -> "R", "FB1" -> "FB").
    Local copy --- engine.py already has _refdes_prefix elsewhere but the
    name varies and we want zero risk of import cycles when block_rules
    consumers move around."""
    out = []
    for ch in ref or "":
        if ch.isalpha():
            out.append(ch.upper())
        else:
            break
    return "".join(out)


def _block_qualifies_for_rectangle(block, ir: TopologyIR) -> bool:
    """Per-block gate for the boxed-block-diagram visual style. Returns
    True when this block should get its own coloured numbered
    rectangle on the rendered sheet. Consults `block_rules.json` so the
    same JSON file drives BOTH the validator's BLOCK_NO_ANCHOR /
    BLOCK_TOO_SMALL exemptions AND the engine's rectangle drawer ---
    one source of truth.

    A block qualifies when ANY of these holds:
      1. `_RENDER_OPTS["force_block_rects"]` is True --- user opted in
         via `force_single_sheet=True` / `single sheet block diagram`
         prompt phrase. Every declared block boxed regardless.
      2. The block has an anchor component (>= min_pins_for_self_contained
         pins). Legacy STM32-class circuit behaviour.
      3. The block name appears in `block_rules.json:passives_only_blocks`
         (CLOCK, INDICATOR, POWER_RAILS, RESET, USER_INTERFACE,
         DECOUPLING, FILTER, INPUT, OUTPUT, OSCILLATOR). Lets crystals
         (2 pin), LED + R, switch + R, etc. anchor their own visual
         block per the user's reference image.
      4. The block name appears in `block_rules.json:single_connector_blocks`
         AND has exactly one connector (refdes prefix in
         connector_refdes_prefixes) with >= minimum_anchor_pin_count
         pins. Lets a bare 6-pin SWD/ICSP header stand alone.
    """
    if _RENDER_OPTS.get("force_block_rects"):
        return True
    if _block_has_own_anchor(block, ir):
        return True
    rules = _load_block_rules()
    name_upper = str(block.name or "").upper()
    passives_only = {
        str(n).upper() for n in (rules.get("passives_only_blocks") or [])
    }
    if name_upper in passives_only:
        return True
    single_conn = {
        str(n).upper() for n in (rules.get("single_connector_blocks") or [])
    }
    if (
        name_upper in single_conn
        and len(block.component_refs) == 1
    ):
        conn_prefixes = tuple(
            str(p).upper()
            for p in (rules.get("connector_refdes_prefixes") or [])
        )
        ref = block.component_refs[0]
        if (
            conn_prefixes
            and _refdes_letters_prefix(ref) in conn_prefixes
        ):
            comp = ir.component_by_ref(ref)
            min_anchor_pins = int(rules.get(
                "minimum_anchor_pin_count", 3))
            if comp is not None:
                try:
                    if len(load_symbol(comp.lib_id).pins) >= min_anchor_pins:
                        return True
                except ValueError:
                    pass
    return False


def _zone_centre(block_type: str, sheet_centre: Tuple[float, float]) -> Tuple[float, float]:
    """Map a block_type to an absolute sheet position using the
    block_type→(col,row) map from JSON (layout.zone_layout). Falls back
    to the module-level ZONE_LAYOUT only when the JSON entry is missing.
    Per [feedback_no_hardcode_json_config]: edit the map in JSON to
    move a block_type to a different zone, no Python change."""
    pc = _placement_cfg()
    cfg_map = (_load_layout_config().get("layout", {})
               .get("zone_layout", {})) or {}
    bt = block_type.lower()
    if bt in cfg_map and isinstance(cfg_map[bt], (list, tuple)) and len(cfg_map[bt]) >= 2:
        col, row = int(cfg_map[bt][0]), int(cfg_map[bt][1])
    elif bt in ZONE_LAYOUT:
        col, row = ZONE_LAYOUT[bt]
    else:
        gen = cfg_map.get("generic", ZONE_LAYOUT.get("generic", (0, 0)))
        col, row = int(gen[0]), int(gen[1])
    cx = sheet_centre[0] + col * pc["zone_w_mm"]
    cy = sheet_centre[1] + row * pc["zone_h_mm"]
    return _snap_grid((cx, cy))


def _compute_flow_dag(ir: TopologyIR) -> Dict[str, Tuple[int, int]]:
    """Phase 2b. Compute (col, row) grid positions for every block based on
    signal flow direction: power inputs on the left, regulators next,
    compute (MCU/sensor) in the middle, sinks on the right.

    Returns `{}` when disabled in config, when ir.blocks < 2, or when no
    blocks can be classified — caller falls back to `_zone_centre` over the
    static `ZONE_LAYOUT` so behavior is byte-identical to today.

    Architect override path: when `IRBlock.flow_role` is set, that role
    selects the column directly. Otherwise the column is inferred from
    block contents (USB/jack/battery → source; regulator IC → regulator;
    MCU → compute; everything else → block_type fallback)."""
    cfg = _load_layout_config().get("multi_block", {}).get("flow_dag", {})
    if not cfg.get("enabled", False):
        return {}
    if len(ir.blocks) < 2:
        return {}

    role_to_col: Dict[str, int] = cfg.get("default_role_column_map", {
        "source": -2, "regulator": -1, "compute": 0, "sink": 1,
    })
    bt_to_role: Dict[str, str] = cfg.get("block_type_to_role", {
        "power": "regulator", "mcu": "compute", "sensor": "compute",
        "comm": "compute", "io": "sink", "generic": "compute",
    })
    # Role-detection patterns. JSON-driven (lib_id substring matches,
    # lowercase). Default lists capture the common KiCad-stock parts —
    # extend in JSON for any new family without touching code, per
    # [feedback_no_hardcode_json_config].
    role_lib_patterns: Dict[str, List[str]] = cfg.get(
        "role_lib_substring_patterns", {
            "source":    ["usb_c", "usb_b", "usb_a", "barrel", "battery",
                          "conn_01x02"],
            "regulator": ["regulator_", "ldo"],
            "compute":   ["mcu_", "atmega", "stm32", "esp32", "rp2040",
                          "attiny", "pic"],
        })

    def _classify_role(blk) -> str:
        # Architect-supplied override wins.
        if getattr(blk, "flow_role", ""):
            return blk.flow_role
        # Inspect components for strong signals, ordered by priority
        # (source first — a USB-input block with an MCU is still a
        # source). Patterns are pure-data from JSON.
        priority = ["source", "regulator", "compute", "sink"]
        for role in priority:
            pats = [p.lower() for p in role_lib_patterns.get(role, [])]
            if not pats:
                continue
            for ref in blk.component_refs:
                comp = ir.component_by_ref(ref)
                if comp is None:
                    continue
                lib = comp.lib_id.lower()
                if any(p in lib for p in pats):
                    return role
        return bt_to_role.get(blk.block_type.lower(), "compute")

    # Assign column by role; within a column, stable-sort by block name for row.
    col_to_blocks: Dict[int, List[str]] = {}
    for blk in ir.blocks:
        role = _classify_role(blk)
        col = role_to_col.get(role, 0)
        col_to_blocks.setdefault(col, []).append(blk.name)

    placements: Dict[str, Tuple[int, int]] = {}
    for col, names in col_to_blocks.items():
        names.sort()
        for row_idx, name in enumerate(names):
            # First block in column gets row=0; subsequent stack below at row 1, 2 ...
            placements[name] = (col, row_idx)
    return placements


def _dynamic_zone_layout(flow_dag: Dict[str, Tuple[int, int]],
                          sheet_centre: Tuple[float, float],
                          paper: Optional[str] = None
                          ) -> Tuple[float, float, float, float, int, int]:
    """Compute (zone_w, zone_h, leftmost_x, top_y, min_col, min_row) so
    every column/row used by `flow_dag` is mapped to an absolute sheet
    position INSIDE the printable sheet area (margins + title-block reserve).

    Approach: compute usable width + zone size from the DAG column span,
    then anchor the LEFTMOST column at `page_margin + zone_w/2`. Each
    subsequent column shifts right by `zone_w`. Same for rows. This
    guarantees no negative-column block ever lands off the sheet.

    `paper` (A4, A3, A2, ...) selects the actual sheet dimensions via
    `_page_dims`. When None, defaults to A4. Per
    [feedback_no_hardcode_json_config]: previously this used the
    module-level A4 SHEET_W/SHEET_H constants regardless of the
    actually-selected paper, so A3 multi-block layouts only used the
    central A4 area and wasted the extra width.
    """
    pc = _placement_cfg()
    if not flow_dag:
        return pc["zone_w_mm"], pc["zone_h_mm"], sheet_centre[0], sheet_centre[1], 0, 0
    cols = [c for (c, _r) in flow_dag.values()]
    rows = [r for (_c, r) in flow_dag.values()]
    if not cols:
        return pc["zone_w_mm"], pc["zone_h_mm"], sheet_centre[0], sheet_centre[1], 0, 0
    min_col, max_col = min(cols), max(cols)
    min_row, max_row = min(rows), max(rows)
    col_span = max_col - min_col + 1
    row_span = max_row - min_row + 1
    # Page margin + title-block height come from JSON. Default margin is
    # 18mm — each block's satellites can extend ~13mm outward from the
    # zone centre (max anchor half-width + cap pitch), so the page edge
    # needs at least 5mm of breathing room beyond that. Smaller margins
    # make leftmost-cap satellites land too close to the sheet border.
    page_margin = pc["page_margin_mm"]
    hier_cfg = _load_layout_config().get("hierarchy_layout", {}) or {}
    tb = hier_cfg.get("title_block_reserve_mm", {}) or {}
    title_block_h = float(tb.get("height_mm", 30.0))
    page_w, page_h = _page_dims(paper or "A4")
    usable_w = page_w - 2 * page_margin
    usable_h = page_h - 2 * page_margin - title_block_h
    zone_w = min(pc["zone_w_mm"], usable_w / col_span)
    zone_h = min(pc["zone_h_mm"], usable_h / max(row_span, 1))
    # Centre the actual occupied grid inside the usable area so columns
    # are symmetrically placed (small grids don't hug the left margin).
    total_w = col_span * zone_w
    total_h = row_span * zone_h
    grid_left = page_margin + (usable_w - total_w) / 2.0
    grid_top  = page_margin + (usable_h - total_h) / 2.0
    # Centre of the leftmost / topmost zone:
    leftmost_x = grid_left + zone_w / 2.0
    topmost_y  = grid_top  + zone_h / 2.0
    return zone_w, zone_h, leftmost_x, topmost_y, min_col, min_row


def _zone_centre_for_block(blk, flow_dag: Dict[str, Tuple[int, int]],
                            sheet_centre: Tuple[float, float],
                            paper: Optional[str] = None) -> Tuple[float, float]:
    """Phase 2b. Returns the absolute sheet position for `blk`. If the flow
    DAG has a placement for this block, use that; otherwise fall back to
    the static `ZONE_LAYOUT` keyed on block_type — preserves prior
    behavior when the DAG is disabled or empty.

    `paper` flows through to `_dynamic_zone_layout` so block placement
    uses the actual sheet dimensions (A3 multi-block layouts use the
    full A3 width instead of being squeezed into the central A4 area).
    """
    if flow_dag and blk.name in flow_dag:
        zone_w, zone_h, left_x, top_y, min_col, min_row = \
            _dynamic_zone_layout(flow_dag, sheet_centre, paper=paper)
        col, row = flow_dag[blk.name]
        cx = left_x + (col - min_col) * zone_w
        cy = top_y  + (row - min_row) * zone_h
        return _snap_grid((cx, cy))
    return _zone_centre(blk.block_type, sheet_centre)


def _zone_rel_cell(block_type: str) -> Tuple[int, int]:
    """Relative (col, row) of a block_type AROUND the MCU centre. (0,0)=centre,
    (-1,-1)=top-left, (1,0)=right, (0,1)=bottom, etc.

    Lookup order: the MCU-centric map `mcu_centric_layout.relative_cell_map`
    FIRST (a landscape-optimised spread — power/clock/reset across the TOP row,
    connectors on the right, output at the bottom — so blocks don't stack 3-tall
    in one column and overflow a landscape A3), then `layout.zone_layout`, then
    the module-level ZONE_LAYOUT. Only the MCU-centric placer calls this; the
    legacy `_zone_centre` path keeps using ZONE_LAYOUT directly, so it is
    unaffected."""
    mb = _load_layout_config().get("multi_block", {}) or {}
    mc_map = (mb.get("mcu_centric_layout", {}) or {}).get("relative_cell_map", {}) or {}
    cfg_map = (_load_layout_config().get("layout", {})
               .get("zone_layout", {})) or {}
    bt = block_type.lower()
    for m in (mc_map, cfg_map):
        v = m.get(bt)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            return int(v[0]), int(v[1])
    if bt in ZONE_LAYOUT:
        return ZONE_LAYOUT[bt]
    gen = (mc_map.get("generic") or cfg_map.get("generic")
           or ZONE_LAYOUT.get("generic", (0, -1)))
    return int(gen[0]), int(gen[1])


# Synonyms for compass-cell lookup when a block's NAME must be classified
# (the architect routinely emits block_type="generic" with a descriptive
# name like "CLOCK"/"RESET"/"BOOT"). Maps a name-keyword to a zone_layout
# key that already has a (col,row). Data-only; extend freely.
_BLOCK_NAME_ALIASES = {
    "xtal": "clock", "osc": "clock", "crystal": "clock", "resonator": "clock",
    "nrst": "reset", "rst": "reset",
    "boot0": "boot", "bootloader": "boot",
    "status": "output", "indicator": "output", "led": "led",
    "prog": "icsp", "jtag": "icsp", "swd": "icsp", "isp": "icsp",
    "protection": "power", "protect": "power", "input": "power",
    "vreg": "power", "regulator": "power", "supply": "power", "vin": "power",
    "serial": "uart", "rs232": "uart", "rs485": "uart",
    "twi": "i2c", "qwiic": "i2c",
}


def _zone_rel_cell_for_block(blk) -> Tuple[int, int]:
    """Compass cell for a block, classifying by block_type FIRST and falling
    back to the block NAME when the type is unknown/generic. Fixes the real
    failure where the architect tags CLOCK/RESET/BOOT/INDICATOR as
    block_type="generic" (so a type-only lookup piled them into one cell).
    Name matching is substring-based against the known zone keys plus
    `_BLOCK_NAME_ALIASES`; longest key wins to avoid spurious hits."""
    bt = (blk.block_type or "").lower()
    cfg_map = (_load_layout_config().get("layout", {})
               .get("zone_layout", {})) or {}
    known_non_generic = (bt != "generic"
                         and (bt in cfg_map or bt in ZONE_LAYOUT))
    if known_non_generic:
        return _zone_rel_cell(bt)
    # type is generic/unknown -> classify by NAME, matching whole word tokens
    # (substring matching falsely hit 'io' inside 'protec-tio-n', etc.).
    name = (blk.name or "").lower()
    tokens = [t for t in "".join(
        ch if ch.isalnum() else " " for ch in name).split() if t]
    keys = [k for k in set(list(cfg_map.keys()) + list(ZONE_LAYOUT.keys()))
            if k and k != "generic"]
    # 1. exact token == zone key, or token == alias keyword
    for tok in tokens:
        if tok in keys:
            return _zone_rel_cell(tok)
        if tok in _BLOCK_NAME_ALIASES:
            return _zone_rel_cell(_BLOCK_NAME_ALIASES[tok])
    # 2. token prefixed by a key/alias (CLOCK1, RESET_SW, UART2) — longest first
    for tok in tokens:
        for key in sorted(keys, key=len, reverse=True):
            if tok.startswith(key):
                return _zone_rel_cell(key)
        for kw in sorted(_BLOCK_NAME_ALIASES, key=len, reverse=True):
            if tok.startswith(kw):
                return _zone_rel_cell(_BLOCK_NAME_ALIASES[kw])
    return _zone_rel_cell(bt)   # true generic -> the generic cell


def _estimate_block_size(blk, ir: TopologyIR, cfg: Dict[str, Any]) -> Tuple[float, float]:
    """Estimate the (w, h) mm of the DRAWN block RECTANGLE — i.e. the same
    footprint `_block_bbox_for_components` will produce: the component content
    PLUS `block_rect.padding_mm` on each side, floored at `block_rect.min_size`.

    Returning the padded-box size (not just the content) is essential: the
    2-D grid spaces cells by this size, so the rectangle that actually gets
    drawn fits inside its cell and adjacent boxes keep their gap. Estimating
    only the content (the old bug) let the +10 mm box padding spill into the
    neighbour and the boxes touched."""
    gap = _placement_cfg()["satellite_gap_mm"]
    br = _load_layout_config().get("block_rect", {}) or {}
    box_pad = float(br.get("padding_mm", 10.16))
    ms = br.get("min_size_mm", {}) or {}
    min_w = max(float(cfg.get("min_block_w_mm", 50.0)), float(ms.get("w", 60.0)))
    min_h = max(float(cfg.get("min_block_h_mm", 45.0)), float(ms.get("h", 50.0)))
    # satellite depth: ~ one R/C body + its reference/value text. Modest on
    # purpose — over-reserving forces needless page compression.
    depth = float(cfg.get("satellite_depth_mm", 9.0))
    aw = ah = 0.0
    anchor = _pick_block_anchor(blk, ir)
    if anchor is not None:
        try:
            x1, y1, x2, y2 = load_symbol(anchor.lib_id).outer_bbox
            aw, ah = (x2 - x1), (y2 - y1)
        except ValueError:
            pass
    n_sat = max(0, len(blk.component_refs) - 1)
    rings = 1 + (n_sat // 4)            # extra ring per ~4 satellites
    grow = rings * (depth + gap) if n_sat else 0.0
    box_w = max(aw + 2 * grow + 2 * box_pad, min_w)
    box_h = max(ah + 2 * grow + 2 * box_pad, min_h)
    return (box_w, box_h)


def _mcu_centric_cells(ir: TopologyIR, cfg: Dict[str, Any]
                       ) -> Optional[Tuple[Dict[str, Tuple[int, int]], str]]:
    """Assign every block a UNIQUE compass cell around the MCU centre.
    Returns ``(cell_of, center_block_name)`` or ``None`` when there is no
    single dominant MCU/controller block (ambiguous / non-MCU board)."""
    blocks = list(getattr(ir, "blocks", None) or [])
    if len(blocks) < 2:
        return None

    center_types = {t.lower() for t in cfg.get(
        "center_block_types", ["mcu", "controller", "cpu", "compute"])}
    min_pins = int(cfg.get("center_min_anchor_pins", 8))
    compute_pats = [p.lower() for p in (
        _load_layout_config().get("multi_block", {}).get("flow_dag", {})
        .get("role_lib_substring_patterns", {}).get("compute", []))]

    def _anchor_pin_count(blk) -> int:
        a = _pick_block_anchor(blk, ir)
        if a is None:
            return -1
        try:
            return len(load_symbol(a.lib_id).pins)
        except ValueError:
            return -1

    def _is_center_candidate(blk) -> bool:
        if blk.block_type.lower() in center_types:
            return True
        a = _pick_block_anchor(blk, ir)
        return (a is not None
                and any(p in a.lib_id.lower() for p in compute_pats))

    candidates = [(b, _anchor_pin_count(b)) for b in blocks
                  if _is_center_candidate(b)]
    candidates = [(b, n) for (b, n) in candidates if n >= min_pins]
    if not candidates:
        return None
    candidates.sort(key=lambda t: (-t[1], t[0].name))
    if len(candidates) >= 2 and candidates[1][1] == candidates[0][1]:
        return None                     # ambiguous (e.g. dual-MCU) -> fall back
    center_blk = candidates[0][0]

    priority = ["mcu", "controller", "cpu", "compute", "power", "clock",
                "crystal", "reset", "comm", "uart", "spi", "i2c", "usb",
                "swd", "icsp", "boot", "io", "sensor", "memory",
                "output", "led", "display", "generic"]

    def _prio(blk) -> int:
        bt = blk.block_type.lower()
        return priority.index(bt) if bt in priority else len(priority)

    ordered = sorted(blocks,
                     key=lambda b: (b is not center_blk, _prio(b), b.name))

    def _sgn(v: int) -> int:
        return (v > 0) - (v < 0)

    def _cell_candidates(c: int, r: int):
        """Deterministic outward search that keeps a block on its own side
        (an E block never drifts to the W) so collisions spread sensibly."""
        if c == 0 and r == 0:
            return [(0, 0), (0, -1), (0, 1)]
        seq: List[Tuple[int, int]] = [(c, r)]
        if r == 0:                       # horizontal side (E / W): stack in rows
            for k in range(1, 7):
                seq += [(c, -k), (c, k)]
            for j in range(1, 4):
                seq += [(c + _sgn(c) * j, 0)]
        elif c == 0:                     # vertical side (N / S): stack in cols
            for k in range(1, 7):
                seq += [(-k, r), (k, r)]
            for j in range(1, 4):
                seq += [(0, r + _sgn(r) * j)]
        else:                            # corner: walk diagonally outward
            for j in range(1, 5):
                seq += [(c + _sgn(c) * j, r),
                        (c, r + _sgn(r) * j),
                        (c + _sgn(c) * j, r + _sgn(r) * j)]
        return seq

    occupied: set = set()
    # Reserve the bottom-right cell: that corner is where KiCad draws the
    # title block, so no functional block may sit there (a connector landing
    # at (1,1) used to overlap the title block). Connectors flow to the right
    # column / bottom-left instead.
    if cfg.get("reserve_title_block_cell", True):
        tb_cell = tuple(cfg.get("title_block_cell", [1, 1]))[:2]
        occupied.add((int(tb_cell[0]), int(tb_cell[1])))
    cell_of: Dict[str, Tuple[int, int]] = {}
    for blk in ordered:
        if blk is center_blk:
            pref = (0, 0)
        else:
            pref = _zone_rel_cell_for_block(blk)
            if pref == (0, 0):           # support blocks never sit on the MCU
                pref = (0, -1)
        chosen = next((cand for cand in _cell_candidates(*pref)
                       if cand not in occupied), None)
        if chosen is None:               # extreme overflow: walk out along +col
            k = 2
            while (k, 0) in occupied:
                k += 1
            chosen = (k, 0)
        occupied.add(chosen)
        cell_of[blk.name] = chosen
    return cell_of, center_blk.name


def _arrange_cells(cell_of: Dict[str, Tuple[int, int]],
                   sizes: Dict[str, Tuple[float, float]],
                   paper: Optional[str],
                   cfg: Dict[str, Any]) -> Dict[str, Tuple[float, float]]:
    """Lay each block's cell on the sheet from its (col,row) and (w,h) box
    size. Cells are separated by ``block_gap_mm`` of whitespace; to fit the
    page ONLY that whitespace compresses (down to ``min_block_gap_mm``) —
    box sizes are never scaled, so the drawn rectangles always keep clear air
    between them and never touch. Returns absolute cell centres
    ``{name:(cx,cy)}`` with the grid centred on the page (biased above the
    title block)."""
    names = [n for n in cell_of if n in sizes]
    if not names:
        return {}
    cols = sorted({cell_of[n][0] for n in names})
    rows = sorted({cell_of[n][1] for n in names})
    col_w = {c: max(sizes[n][0] for n in names if cell_of[n][0] == c) for c in cols}
    row_h = {r: max(sizes[n][1] for n in names if cell_of[n][1] == r) for r in rows}

    gap = float(cfg.get("block_gap_mm", 16.0))
    min_gap = float(cfg.get("min_block_gap_mm", 8.0))
    max_gap = float(cfg.get("max_block_gap_mm", 55.0))
    margin = _placement_cfg()["page_margin_mm"]
    page_w, page_h = _page_dims(paper)
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin

    sum_w = sum(col_w.values()); sum_h = sum(row_h.values())
    ncol_gaps = max(0, len(cols) - 1); nrow_gaps = max(0, len(rows) - 1)
    # JUSTIFY: spread the blocks across the whole usable sheet instead of
    # clustering them in a small centred patch (the "empty left/right, crammed
    # at top" complaint). The gap grows to fill the free space, clamped to
    # [min_gap, max_gap]; it also shrinks (toward min_gap) when the grid is too
    # big for the page. Title-block corner is handled by the reflow's lift-up,
    # so the full page height is fair game here.
    gx = (max(min_gap, min(max_gap, (usable_w - sum_w) / ncol_gaps))
          if ncol_gaps else gap)
    gy = (max(min_gap, min(max_gap, (usable_h - sum_h) / nrow_gaps))
          if nrow_gaps else gap)

    cx_of: Dict[int, float] = {}
    acc = 0.0
    for i, c in enumerate(cols):
        acc = (col_w[c] / 2.0 if i == 0
               else acc + col_w[cols[i - 1]] / 2.0 + gx + col_w[c] / 2.0)
        cx_of[c] = acc
    cy_of: Dict[int, float] = {}
    acc = 0.0
    for i, r in enumerate(rows):
        acc = (row_h[r] / 2.0 if i == 0
               else acc + row_h[rows[i - 1]] / 2.0 + gy + row_h[r] / 2.0)
        cy_of[r] = acc

    gminx = cx_of[cols[0]] - col_w[cols[0]] / 2.0
    gmaxx = cx_of[cols[-1]] + col_w[cols[-1]] / 2.0
    gminy = cy_of[rows[0]] - row_h[rows[0]] / 2.0
    gmaxy = cy_of[rows[-1]] + row_h[rows[-1]] / 2.0
    grid_cx = (gminx + gmaxx) / 2.0
    grid_cy = (gminy + gmaxy) / 2.0
    target_cx = page_w / 2.0
    # Centre on the TRUE page centre so the design uses the whole sheet (was
    # biased upward, which crammed everything at the top and left the bottom
    # empty). The title-block corner is cleared by the reflow's lift-up, so we
    # no longer reserve the bottom band here.
    target_cy = page_h / 2.0
    dx, dy = target_cx - grid_cx, target_cy - grid_cy
    # Clamp the whole grid inside the page edges so no bias pushes it off.
    edge = max(2.0, margin * 0.4)
    if gminx + dx < edge:
        dx = edge - gminx
    if gmaxx + dx > page_w - edge:
        dx = (page_w - edge) - gmaxx
    if gminy + dy < edge:
        dy = edge - gminy
    if gmaxy + dy > page_h - edge:
        dy = (page_h - edge) - gmaxy
    return {n: _snap_grid((cx_of[cell_of[n][0]] + dx, cy_of[cell_of[n][1]] + dy))
            for n in names}


def _mcu_centric_reflow(comps: List["PlacedComp"], ir: TopologyIR,
                        paper: Optional[str] = None) -> int:
    """Re-space blocks from their MEASURED drawn-box size so two block
    rectangles can NEVER touch — the guarantee that estimating can't give (a
    tall MCU with stacked decoupling caps is bigger than any estimate).

    Runs AFTER all placement + relocation. For each block it measures the
    rectangle ``_block_bbox_for_components`` will draw (content + block_rect
    padding, floored at min_size), re-runs the same compass grid on those true
    sizes, and rigidly shifts every component of a block to its new cell
    centre. Returns the number of blocks moved; 0 for non-MCU / ambiguous
    boards. Keeps the compass arrangement — only spacing changes, so blocks
    never scatter."""
    cfg = (_load_layout_config().get("multi_block", {})
           .get("mcu_centric_layout", {})) or {}
    if not cfg.get("enabled", True):
        return 0
    res = _mcu_centric_cells(ir, cfg)
    if res is None:
        return 0
    cell_of, _center = res
    by_ref = {c.ref: c for c in comps}
    br = _load_layout_config().get("block_rect", {}) or {}
    box_pad = float(br.get("padding_mm", 10.16))
    ms = br.get("min_size_mm", {}) or {}
    min_w = float(ms.get("w", 60.0)); min_h = float(ms.get("h", 50.0))

    refs_by_block = {b.name: [r for r in b.component_refs if r in by_ref]
                     for b in ir.blocks}
    sizes: Dict[str, Tuple[float, float]] = {}
    centre: Dict[str, Tuple[float, float]] = {}
    for name, refs in refs_by_block.items():
        if not refs:
            continue
        bbs = [_abs_outer_bbox(by_ref[r]) for r in refs]
        x1 = min(b[0] for b in bbs); y1 = min(b[1] for b in bbs)
        x2 = max(b[2] for b in bbs); y2 = max(b[3] for b in bbs)
        sizes[name] = (max((x2 - x1) + 2 * box_pad, min_w),
                       max((y2 - y1) + 2 * box_pad, min_h))
        centre[name] = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    targets = _arrange_cells(cell_of, sizes, paper, cfg)
    moved = 0
    for name, refs in refs_by_block.items():
        if name not in targets or name not in centre:
            continue
        dx = _round_grid(targets[name][0] - centre[name][0])
        dy = _round_grid(targets[name][1] - centre[name][1])
        if dx == 0 and dy == 0:
            continue
        for r in refs:
            pc = by_ref[r]
            pc.pos = (pc.pos[0] + dx, pc.pos[1] + dy)
        moved += 1

    # Title-block avoidance: a bottom-right connector block can still land on
    # the title block (reserving the (1,1) cell isn't enough — it spills to
    # (2,1), also over the corner). Measure each block's final box; if it
    # overlaps the title-block reserve, lift it straight up to sit above it.
    tb = _title_block_obstacle(paper)
    if tb is not None:
        min_gap = float(cfg.get("min_block_gap_mm", 8.0))
        for name, refs in refs_by_block.items():
            if not refs:
                continue
            bb = _block_bbox_for_components(comps, refs)
            if bb is None or not _bboxes_overlap(bb, tb, 0.0):
                continue
            up = _round_grid((bb[3] - tb[1]) + min_gap)   # overlap + clearance
            if up <= 0:
                continue
            for r in refs:
                pc = by_ref[r]
                pc.pos = (pc.pos[0], pc.pos[1] - up)
            moved += 1
    return moved


def _mcu_centric_block_centres(ir: TopologyIR,
                               paper: Optional[str] = None
                               ) -> Dict[str, Tuple[float, float]]:
    """2-D MCU-centric block layout (the professional reference model).

    The MCU/controller block is anchored near the sheet centre; every other
    block lands in a compass cell around it (power top-left, clock top, reset
    top-right, connectors right, output bottom — from `layout.zone_layout`).
    Cells are sized to each block's ACTUAL estimated content (a content-aware
    grid), so a tall MCU never overflows a fixed-height row into its neighbour
    — the structural cause of the old block-on-block overlap.

    Returns ``{block_name: (cx, cy)}`` absolute centres, or ``{}`` when:
      * disabled in config (`multi_block.mcu_centric_layout.enabled`), or
      * there is no single dominant MCU/controller block (ambiguous, or a
        non-MCU board) — the caller then falls back to the flow-DAG grid.

    ADDITIVE: this never mutates or replaces `_compute_flow_dag` / `ZONE_LAYOUT`;
    it is simply a higher-priority option the zoned placer consults first.
    """
    cfg = (_load_layout_config().get("multi_block", {})
           .get("mcu_centric_layout", {})) or {}
    if not cfg.get("enabled", True):
        return {}
    res = _mcu_centric_cells(ir, cfg)
    if res is None:
        return {}
    cell_of, _center_name = res
    # INITIAL centres use estimated box sizes to seed anchor placement;
    # `_mcu_centric_reflow` re-spaces from the measured sizes afterwards.
    sizes = {b.name: _estimate_block_size(b, ir, cfg) for b in ir.blocks}
    return _arrange_cells(cell_of, sizes, paper, cfg)


def _classify_decoupling(comp: IRComponent, ir: TopologyIR) -> Optional[Tuple[str, str]]:
    """Phase 3. Return `(target_ic_ref, target_pin_key)` if `comp` is a
    decoupling cap; else None.

    A cap is decoupling when:
      - lib_id matches a configured cap prefix (Device:C / Device:C_Polarized)
      - it's wired into exactly 2 distinct nets
      - one net is is_power=true AND its name is not a ground-rail name
      - that same positive-power net also touches the pin of an IC
        (a component with at least `min_anchor_pin_count` pins, default 3)
    Returns the IC's ref and the IC's pin key on that net.

    Pure analyzer — does not move anything; the relocate pass calls this
    to decide where to move each cap."""
    cfg = _load_layout_config().get("multi_block", {}).get("decoupling", {})
    cap_prefixes = cfg.get("cap_lib_id_prefixes", ["Device:C"])
    if not any(comp.lib_id.startswith(p) for p in cap_prefixes):
        return None

    gnd_names = {s.upper() for s in cfg.get("ground_net_names", [
        "GND", "AGND", "DGND", "PGND", "VSS", "VEE", "EGND", "SGND"
    ])}
    min_pins = int(cfg.get("min_anchor_pin_count", 3))

    cap_nets: List[IRNet] = []
    for net in ir.nets:
        for pinref in net.pins:
            if "." not in pinref:
                continue
            if pinref.split(".", 1)[0] == comp.ref:
                cap_nets.append(net)
                break
    if len(cap_nets) != 2:
        return None

    pos_net: Optional[IRNet] = None
    for net in cap_nets:
        if not net.is_power:
            continue
        upper = net.name.upper().lstrip("+")
        if upper in gnd_names:
            continue
        pos_net = net
        break
    if pos_net is None:
        return None

    # Find the cap's own block (architect-assigned grouping).
    cap_block_refs: set = set()
    for blk in ir.blocks:
        if comp.ref in blk.component_refs:
            cap_block_refs = set(blk.component_refs)
            break

    # Among ALL ICs touching this positive-power net, build a candidate
    # list with (ref, pkey, same_block, pins_on_net, total_pins).
    # Priority (highest wins):
    #   1. IC is in the SAME block as the cap (architect grouped them on
    #      purpose — keeps LDO output caps next to the LDO, MCU decoupling
    #      caps next to the MCU).
    #   2. IC has the MOST pins on this specific net (most-anchored IC).
    #   3. Tie-broken by total pin count.
    candidates: List[Tuple[str, str, bool, int, int]] = []
    seen_refs: set = set()
    for pinref in pos_net.pins:
        if "." not in pinref:
            continue
        pref, pkey = pinref.split(".", 1)
        if pref == comp.ref or pref in seen_refs:
            continue
        target = ir.component_by_ref(pref)
        if target is None:
            continue
        try:
            geom = load_symbol(target.lib_id)
        except ValueError:
            continue
        if len(geom.pins) < min_pins:
            continue
        seen_refs.add(pref)
        same_block = pref in cap_block_refs
        pins_on_net = sum(
            1 for pr in pos_net.pins
            if "." in pr and pr.split(".", 1)[0] == pref
        )
        candidates.append((pref, pkey, same_block, pins_on_net, len(geom.pins)))
    if not candidates:
        return None
    # Sort: same_block first (True > False via not), then most pins on net,
    # then total pin count. Negative for descending.
    candidates.sort(key=lambda t: (not t[2], -t[3], -t[4]))
    chosen = candidates[0]
    return (chosen[0], chosen[1])


def _emit_interblock_wires_block(
    ir: TopologyIR,
    comps: List["PlacedComp"],
    obstacles: List[Tuple[float, float, float, float]],
) -> Tuple[int, str]:
    """Phase 4. For any net (power or signal) whose pins span 2+ blocks,
    draw an L-route wire between block representatives so the visual flow
    is continuous, not label-only.

    User rule: "within MCU block keep labels; between blocks use wires".
    GND-like nets are skipped by default to avoid spaghetti — they remain
    as labels + power-port symbols.

    Returns (wires_emitted, body_chunk). No-op when disabled OR when
    ir.blocks is empty (defensive — flat path never reaches this). Routes
    that pierce a non-endpoint body are dropped silently."""
    cfg = _load_layout_config().get("multi_block", {}).get("interblock_wires", {})
    if not cfg.get("enabled", False):
        return 0, ""
    if not ir.blocks:
        return 0, ""

    max_routes = int(cfg.get("max_routes_per_sheet", 20))
    skip_ground = bool(cfg.get("skip_ground_nets", True))
    include_signal = bool(cfg.get("include_signal_nets", False))
    gnd_names = {s.upper() for s in cfg.get("ground_net_names", [
        "GND", "AGND", "DGND", "PGND", "VSS", "VEE", "EGND", "SGND"
    ])}

    comps_by_ref = {c.ref: c for c in comps}
    ref_to_block: Dict[str, str] = {}
    for blk in ir.blocks:
        for ref in blk.component_refs:
            ref_to_block[ref] = blk.name

    body = ""
    count = 0
    for net in ir.nets:
        if count >= max_routes:
            break
        if not net.is_power and not include_signal:
            continue
        if skip_ground:
            upper = net.name.upper().lstrip("+")
            if upper in gnd_names:
                continue

        # Pick the FIRST pin we find in each block (deterministic via
        # iteration order of net.pins).
        block_to_pin: Dict[str, Tuple[Tuple[float, float, float], str]] = {}
        for pinref in net.pins:
            if "." not in pinref:
                continue
            ref, pkey = pinref.split(".", 1)
            blk_name = ref_to_block.get(ref)
            if blk_name is None or blk_name in block_to_pin:
                continue
            comp = comps_by_ref.get(ref)
            if comp is None:
                continue
            abs_pos = comp.pin_abs(pkey)
            if abs_pos is None:
                continue
            block_to_pin[blk_name] = (abs_pos, ref)

        if len(block_to_pin) < 2:
            continue

        # Sort representatives by x so we route left → right (matches
        # signal flow direction set up by Phase 2b).
        items = sorted(block_to_pin.items(), key=lambda kv: kv[1][0][0])
        for i in range(len(items) - 1):
            if count >= max_routes:
                break
            (p1_abs, ref1) = items[i][1]
            (p2_abs, ref2) = items[i + 1][1]
            p1 = p1_abs[:2]
            p2 = p2_abs[:2]
            own = []
            for r in (ref1, ref2):
                c = comps_by_ref.get(r)
                if c is not None:
                    own.append(_abs_outer_bbox(c))
            path = _route_l_aware(p1, p2, obstacles, own_bboxes=own)
            # Safety: drop the wire if its final path still pierces a
            # non-endpoint body (a bad wire is worse than no wire — the
            # label-net keeps the electrical connection either way).
            if _find_blocking_obstacle(path, obstacles, exclude=own) is not None:
                continue
            for k in range(len(path) - 1):
                body += _emit_wire(path[k], path[k + 1])
                count += 1
    return count, body


def _build_ref_to_block(ir: TopologyIR) -> Dict[str, str]:
    """ref -> owning block name (first block wins). Empty when no blocks."""
    m: Dict[str, str] = {}
    for blk in getattr(ir, "blocks", None) or []:
        for r in blk.component_refs:
            m.setdefault(r, blk.name)
    return m


def _relocate_decoupling_caps(
    placed_map: Dict[str, "PlacedComp"],
    placed_bboxes: List[Tuple[float, float, float, float]],
    ir: TopologyIR,
    force: bool = False,
    same_block_only: bool = False,
) -> int:
    """Phase 3 post-pass. Move decoupling caps adjacent to their target
    IC's power pin via `_satellite_offset_for_pin` (the same routine the
    zoned satellite placer uses), with outward-step collision resolution
    so multiple caps on the same pin form a stack instead of overlapping.

    No-op (returns 0) when disabled, or when no caps match. By default
    (`force=False`) also returns early when `ir.blocks` is empty — the
    flat single-block path must stay byte-identical. Child-sheet renders
    pass `force=True` because the parent IR already proved we're in a
    multi-block context; the subsetted child IR carries `blocks=[]` only
    because subset() strips the block list. Mutates `placed_map` and
    `placed_bboxes` in place. Returns the number of caps relocated."""
    cfg = _load_layout_config().get("multi_block", {}).get("decoupling", {})
    if not cfg.get("enabled", False):
        return 0
    if not ir.blocks and not force:
        return 0

    # Child-sheet path uses a tighter threshold so caps that the flat
    # placer pushed away via collision-resolver get pulled back snug to
    # the IC VDD pin. The zoned-block path keeps the original wider
    # threshold so its single-page output is byte-identical to today.
    dist_key = "child_sheet_max_distance_from_vdd_mm" if force else "max_distance_from_vdd_mm"
    max_dist = float(cfg.get(dist_key, cfg.get("max_distance_from_vdd_mm", 30.0)))
    relocated = 0
    # Block-aware mode: only pull a cap to an IC pin in the SAME block, so a
    # power-block cap is never dragged into the MCU block. Empty map disables
    # the guard. Used by the 2-D MCU-centric layout to keep blocks contained.
    ref_block = _build_ref_to_block(ir) if same_block_only else {}

    for comp in ir.components:
        target = _classify_decoupling(comp, ir)
        if target is None:
            continue
        cap_ref = comp.ref
        target_ref, target_pin_key = target
        if cap_ref not in placed_map or target_ref not in placed_map:
            continue
        if (ref_block and cap_ref in ref_block and target_ref in ref_block
                and ref_block[cap_ref] != ref_block[target_ref]):
            continue   # cross-block pull suppressed (block-contained layout)
        cap_pc = placed_map[cap_ref]
        target_pc = placed_map[target_ref]
        pin_abs = target_pc.pin_abs(target_pin_key)
        if pin_abs is None:
            continue
        dx = cap_pc.pos[0] - pin_abs[0]
        dy = cap_pc.pos[1] - pin_abs[1]
        if math.hypot(dx, dy) <= max_dist:
            continue

        attempt = _satellite_offset_for_pin(target_pc, target_pin_key, cap_pc.geom)
        if attempt is None:
            continue
        new_pos, new_rot = attempt

        # Compute outward axis for stack-resolution (multi-cap on same pin).
        side = pin_side_from_rot(pin_abs[2])
        outward = {"right": (1, 0), "left": (-1, 0),
                    "top": (0, -1), "bottom": (0, 1)}.get(side, (1, 0))

        old_bb = _candidate_abs_bbox(cap_pc.geom, cap_pc.pos, cap_pc.rotation)

        if cfg.get("robust_destack", True):
            # Robust de-stack (2026-06-02): build the collision set from the
            # LIVE position of every OTHER placed component (keyed by ref),
            # not by float-diffing the drifting `placed_bboxes`. This is what
            # makes a 2nd cap on the same VDD pin actually SEE the 1st cap's
            # just-committed slot and step off it. Two co-located caps merged
            # the +3V3 and GND power-port wire clusters into one net -> a dead
            # +3V3<->GND short; landing them on distinct coords prevents it.
            others = [
                _candidate_abs_bbox(pc.geom, pc.pos, pc.rotation)
                for ref, pc in placed_map.items() if ref != cap_ref
            ]
            budget = int(cfg.get("destack_step_budget", 16))

            def _free(p: Tuple[float, float]) -> bool:
                bb = _candidate_abs_bbox(cap_pc.geom, p, new_rot)
                return not any(_bboxes_overlap(bb, ob, 2.54) for ob in others)

            cand_pos = _snap_grid(new_pos)
            if not _free(cand_pos):
                # Walk outward first, then the perpendicular axis (both ways),
                # verifying after every snap, so the cap can never come to rest
                # while still overlapping another component.
                perp = (outward[1], outward[0])
                for direction in (outward, perp, (-perp[0], -perp[1])):
                    probe = cand_pos
                    cleared = False
                    for _ in range(budget):
                        probe = _snap_grid((probe[0] + direction[0] * 2.54,
                                            probe[1] + direction[1] * 2.54))
                        if _free(probe):
                            cand_pos, cleared = probe, True
                            break
                    if cleared:
                        break
        else:
            # Legacy path (float-diff de-stack) — kept byte-identical behind
            # the robust_destack=false flag for boards whose hand-tuned output
            # must not move.
            others = [b for b in placed_bboxes if b != old_bb]
            cand_pos = new_pos
            for _ in range(8):
                bb = _candidate_abs_bbox(cap_pc.geom, cand_pos, new_rot)
                if not any(_bboxes_overlap(bb, ob, 2.54) for ob in others):
                    break
                cand_pos = _snap_grid((cand_pos[0] + outward[0] * 2.54,
                                        cand_pos[1] + outward[1] * 2.54))

        cap_pc.pos = _snap_grid(cand_pos)
        cap_pc.rotation = new_rot
        new_bb = _candidate_abs_bbox(cap_pc.geom, cap_pc.pos, cap_pc.rotation)
        try:
            placed_bboxes.remove(old_bb)
        except ValueError:
            pass
        placed_bboxes.append(new_bb)
        relocated += 1

    # Unconditional de-overlap guarantee (2026-06-02). The loop above SKIPS
    # any cap already within max_dist of its VDD pin (the `continue` near the
    # top), so a pair the placer dropped CO-LOCATED is never de-stacked there
    # and survives on the same coordinate -> the +V and GND power-port wire
    # clusters merge into one net = a +V<->GND short. This pass runs over ALL
    # decoupling caps regardless of distance and separates any overlapping
    # pair. Only touches caps that actually overlap, so correct boards are
    # unchanged.
    # Datasheet D1 drawing standard: lay each IC's decoupling caps in a ROW
    # (perpendicular to the power pin's outward axis), each cap's pin-axis
    # ALONG that axis -- so the +V pins form one line and the GND pins another,
    # parallel and SEPARATED. The two power-net buses then run on different
    # lines and can never overlap (no +V<->GND short, no stacked-bus mess).
    # This is the principled fix that replaces the column+shared-bus layout.
    if cfg.get("row_layout", True):
        relocated += _row_arrange_decoupling_caps(placed_map, placed_bboxes,
                                                   ir, cfg)
    if cfg.get("robust_destack", True):
        relocated += _deoverlap_decoupling_caps(placed_map, placed_bboxes,
                                                 ir, cfg)
    return relocated


def _cap_positive_pin(cap_comp: IRComponent, ir: TopologyIR,
                      gnd_names: set) -> Optional[str]:
    """Return the cap's pin KEY on the POSITIVE supply rail (a power net whose
    name is not a ground name). Used to orient a decoupling cap so its +V pin
    faces the IC it decouples."""
    for net in ir.nets:
        if not getattr(net, "is_power", False):
            continue
        if net.name.upper().lstrip("+") in gnd_names:
            continue
        for pinref in net.pins:
            if "." in pinref:
                r, k = pinref.split(".", 1)
                if r == cap_comp.ref:
                    return k
    return None


def _row_arrange_decoupling_caps(
    placed_map: Dict[str, "PlacedComp"],
    placed_bboxes: List[Tuple[float, float, float, float]],
    ir: TopologyIR,
    cfg: dict,
) -> int:
    """Arrange each IC's decoupling caps in a ROW (datasheet D1 drawing
    standard) instead of a vertical column.

    The row runs PERPENDICULAR to the target power pin's outward axis, and
    every cap is rotated so its pin-axis is PARALLEL to that outward axis.
    Result: all +V pins fall on one line and all GND pins on a parallel line,
    so the +V cluster bus and the GND cluster bus render on SEPARATE lines and
    cannot overlap (the root of the +V<->GND short). Caps are centred on the
    pin and spaced by `row_gap_mm`, set `row_distance_mm` away from the pin.
    Returns the number of caps moved."""
    # Group decoupling caps by (target IC, target pin).
    groups: Dict[Tuple[str, str], List[str]] = {}
    for c in ir.components:
        t = _classify_decoupling(c, ir)
        if t is not None and c.ref in placed_map:
            groups.setdefault(t, []).append(c.ref)

    gap = float(cfg.get("row_gap_mm", 2.54))
    dist = float(cfg.get("row_distance_mm", 7.62))
    moved = 0
    for (ic_ref, pin_key), cap_refs in groups.items():
        if len(cap_refs) < 2 or ic_ref not in placed_map:
            continue
        pinp = placed_map[ic_ref].pin_abs(pin_key)
        if pinp is None:
            continue
        px, py, prot = pinp
        side = pin_side_from_rot(prot)
        outward = {"right": (1, 0), "left": (-1, 0),
                   "top": (0, -1), "bottom": (0, 1)}.get(side, (0, -1))
        ic_facing = (-outward[0], -outward[1])
        gnd_names = {s.upper() for s in cfg.get("ground_net_names", [
            "GND", "AGND", "DGND", "PGND", "VSS", "VEE", "EGND", "SGND"])}
        # Base rotation so the cap's pin-axis is PARALLEL to outward. Device:C
        # at rot 0 has a vertical pin-axis; a horizontal outward needs rot 90.
        base_rot = 0.0 if outward[0] == 0 else 90.0
        caps = [placed_map[r] for r in cap_refs]
        old_bbs = [_candidate_abs_bbox(c.geom, c.pos, c.rotation) for c in caps]
        # Per cap: orient so the +V pin (the one on the IC's positive rail)
        # faces the IC. Otherwise the +V bus must route DOWN past the GND line
        # to reach the IC pin -> re-creates the +V/GND overlap.
        cap_rots: List[float] = []
        for cap in caps:
            comp = ir.component_by_ref(cap.ref)
            pv_key = _cap_positive_pin(comp, ir, gnd_names) if comp else None
            chosen = base_rot
            if pv_key and comp is not None:
                try:
                    pv_pin = load_symbol(comp.lib_id).resolve_pin(pv_key)
                except (ValueError, KeyError):
                    pv_pin = None
                if pv_pin is not None:
                    for test_rot in (base_rot, (base_rot + 180.0) % 360.0):
                        tx, ty, _r = place_pin(pv_pin, 0.0, 0.0, test_rot)
                        if tx * ic_facing[0] + ty * ic_facing[1] > 0:
                            chosen = test_rot
                            break
            cap_rots.append(chosen)
        # Row axis = perpendicular to outward (0 -> x, 1 -> y).
        row_axis = 0 if outward[0] == 0 else 1
        spans = [_candidate_abs_bbox(c.geom, (0.0, 0.0), cap_rots[i])
                 for i, c in enumerate(caps)]
        span = max((bb[2] - bb[0]) if row_axis == 0 else (bb[3] - bb[1])
                   for bb in spans)
        # Pitch must clear the Reference/Value text, which protrudes ALONG
        # the row axis (side text → right; name-above/value-below → up+down).
        # Without this a uniform cap row overprints its own field text.
        if bool(cfg.get("text_aware_spacing", True)):
            if row_axis == 0:
                span += max(_est_field_protrusion(c) for c in caps)
            else:
                span += 2.0 * float(cfg.get("field_stack_allowance_mm", 2.54))
        spacing = round((span + gap) / GRID) * GRID
        bx = px + outward[0] * dist
        by = py + outward[1] * dist
        n = len(caps)
        start = -(n - 1) * spacing / 2.0
        for i, cap in enumerate(caps):
            off = start + i * spacing
            pos = (bx + off, by) if row_axis == 0 else (bx, by + off)
            cap.rotation = cap_rots[i]
            cap.pos = _snap_grid(pos)
            moved += 1
        # Keep placed_bboxes consistent for downstream passes.
        for old in old_bbs:
            try:
                placed_bboxes.remove(old)
            except ValueError:
                pass
        for cap in caps:
            placed_bboxes.append(
                _candidate_abs_bbox(cap.geom, cap.pos, cap.rotation))
    return moved


def _deoverlap_decoupling_caps(
    placed_map: Dict[str, "PlacedComp"],
    placed_bboxes: List[Tuple[float, float, float, float]],
    ir: TopologyIR,
    cfg: dict,
) -> int:
    """Separate any two decoupling caps whose bboxes overlap.

    Pushes the later cap PERPENDICULAR to its own pin axis (a vertical cap is
    moved sideways, a horizontal cap is moved up/down) so the two caps' pins
    land on DIFFERENT rail lines --- that is what stops the power-net cluster
    wiring from stitching +V and GND onto one shared line. Tries both
    perpendicular directions, then the pin-axis directions as a fallback,
    snapping to grid and verifying it clears every other placed component.
    Returns the number of caps moved. Idempotent: a second call finds no
    overlaps and moves nothing."""
    cap_refs = [
        c.ref for c in ir.components
        if c.ref in placed_map and _classify_decoupling(c, ir) is not None
    ]
    if len(cap_refs) < 2:
        return 0
    budget = int(cfg.get("destack_step_budget", 16))
    margin = float(cfg.get("destack_margin_mm", 0.0))
    # Text-aware spacing (2026-06-02): separate caps until their PLACED
    # field text clears too, not just their bodies. Without it a row of
    # decoupling caps sits ~7.62 mm apart — bodies clear, but the
    # side-placed Reference/Value of one cap lands on the next cap's body
    # or the +V rail label (user: "letter ellam overwrite aakuthu konjam
    # space vittu"). Gated; off → body-only behaviour, byte-identical.
    text_aware = bool(cfg.get("text_aware_spacing", True))
    stack_allow = float(cfg.get("field_stack_allowance_mm", 2.54))
    moved = 0

    # Body-only bbox — what `placed_bboxes` tracks for downstream passes.
    def _cbb(pc: "PlacedComp") -> Tuple[float, float, float, float]:
        return _candidate_abs_bbox(pc.geom, pc.pos, pc.rotation)

    # Text-inclusive bbox — what the overlap decisions use.
    def _tbb_at(pc: "PlacedComp", pos) -> Tuple[float, float, float, float]:
        if not text_aware:
            return _candidate_abs_bbox(pc.geom, pos, pc.rotation)
        return _field_text_bbox(pc.geom, pos, pc.rotation,
                                _est_field_protrusion(pc), stack_allow)

    def _tbb(pc: "PlacedComp") -> Tuple[float, float, float, float]:
        return _tbb_at(pc, pc.pos)

    for i in range(len(cap_refs)):
        for j in range(i + 1, len(cap_refs)):
            a = placed_map[cap_refs[i]]
            b = placed_map[cap_refs[j]]
            if not _bboxes_overlap(_tbb(a), _tbb(b), 0.0):
                continue
            # Pin axis of b -> push perpendicular to it.
            bpins = [place_pin(p, b.pos[0], b.pos[1], b.rotation)
                     for p in b.geom.pins[:2]]
            horiz_pins = (len(bpins) >= 2
                          and abs(bpins[1][0] - bpins[0][0])
                          > abs(bpins[1][1] - bpins[0][1]))
            if horiz_pins:
                dirs = [(0.0, 2.54), (0.0, -2.54), (2.54, 0.0), (-2.54, 0.0)]
            else:
                dirs = [(2.54, 0.0), (-2.54, 0.0), (0.0, 2.54), (0.0, -2.54)]
            others = [_tbb(pc) for ref, pc in placed_map.items()
                      if ref != b.ref]
            old_bb = _cbb(b)
            placed = False
            for dx, dy in dirs:
                probe = b.pos
                for _ in range(budget):
                    probe = _snap_grid((probe[0] + dx, probe[1] + dy))
                    cand = _tbb_at(b, probe)
                    if not any(_bboxes_overlap(cand, ob, margin)
                               for ob in others):
                        b.pos = probe
                        try:
                            placed_bboxes.remove(old_bb)
                        except ValueError:
                            pass
                        placed_bboxes.append(_cbb(b))
                        moved += 1
                        placed = True
                        break
                if placed:
                    break
    return moved


def _classify_crystal_target(comp: IRComponent, ir: TopologyIR
                              ) -> Optional[Tuple[str, str, str]]:
    """R2.5 (2026-05-27). Return `(ic_ref, ic_pin_a, ic_pin_b)` when
    `comp` is a crystal-like 2-pin part connecting to ONE IC on two
    different signal pins. Universal — works for crystals, ceramic
    resonators, differential filters, anything that needs to sit
    snug between two pins of the SAME chip.

    Detection: 2-pin part wired into exactly 2 nets where BOTH nets
    are non-power AND the OTHER endpoint of each net touches the same
    IC (component with ≥`min_anchor_pin_count` pins). No part-name or
    refdes-prefix hardcoding per [feedback_no_hardcode_json_config].
    """
    cfg = _load_layout_config().get("multi_block", {}).get("crystal", {}) or {}
    min_ic_pins = int(cfg.get("min_anchor_pin_count", 8))

    try:
        comp_geom = load_symbol(comp.lib_id)
    except ValueError:
        return None
    if len(comp_geom.pins) != 2:
        return None

    # Find the 2 nets this part touches.
    part_nets: List[IRNet] = []
    for net in ir.nets:
        for pinref in net.pins:
            if "." in pinref and pinref.split(".", 1)[0] == comp.ref:
                part_nets.append(net)
                break
    if len(part_nets) != 2:
        return None
    # Both nets must be non-power (XTAL signals never appear as is_power).
    if any(n.is_power for n in part_nets):
        return None

    # The IC: each net should have ONE other pin pointing at the same
    # multi-pin IC.
    ic_refs_per_net: List[List[Tuple[str, str]]] = []
    for net in part_nets:
        ics: List[Tuple[str, str]] = []
        for pinref in net.pins:
            if "." not in pinref:
                continue
            ref, pin_key = pinref.split(".", 1)
            if ref == comp.ref:
                continue
            other = ir.component_by_ref(ref)
            if other is None:
                continue
            try:
                geom = load_symbol(other.lib_id)
            except ValueError:
                continue
            if len(geom.pins) >= min_ic_pins:
                ics.append((ref, pin_key))
        ic_refs_per_net.append(ics)

    # Both nets must include the same IC; pick its two pins.
    common: Optional[str] = None
    pin_a = pin_b = ""
    for (ref_a, key_a) in ic_refs_per_net[0]:
        for (ref_b, key_b) in ic_refs_per_net[1]:
            if ref_a == ref_b and key_a != key_b:
                common, pin_a, pin_b = ref_a, key_a, key_b
                break
        if common is not None:
            break
    if common is None:
        return None
    return (common, pin_a, pin_b)


def _relocate_crystals(
    placed_map: Dict[str, "PlacedComp"],
    placed_bboxes: List[Tuple[float, float, float, float]],
    ir: TopologyIR,
    force: bool = False,
    same_block_only: bool = False,
) -> int:
    """R2.5. Move crystal-like 2-pin parts to the midpoint between the
    two IC pins they bridge — implements rule "Keep crystal close to
    MCU pins". Universal: detection is by topology (see
    `_classify_crystal_target`), not by refdes prefix or lib_id name.
    Same gating semantics as `_relocate_decoupling_caps`.
    """
    cfg = _load_layout_config().get("multi_block", {}).get("crystal", {}) or {}
    if not cfg.get("enabled", True):
        return 0
    if not ir.blocks and not force:
        return 0
    max_dist = float(cfg.get("max_distance_from_pins_mm", 25.0))
    relocated = 0
    # Block-aware mode: keep a crystal in its OWN block instead of yanking it
    # onto the MCU's XTAL pins in another block (the CLOCK block sits adjacent
    # to the MCU and bonds by net). Empty map disables the guard.
    ref_block = _build_ref_to_block(ir) if same_block_only else {}
    for comp in ir.components:
        target = _classify_crystal_target(comp, ir)
        if target is None:
            continue
        ic_ref, pin_a, pin_b = target
        if comp.ref not in placed_map or ic_ref not in placed_map:
            continue
        if (ref_block and comp.ref in ref_block and ic_ref in ref_block
                and ref_block[comp.ref] != ref_block[ic_ref]):
            continue   # cross-block pull suppressed (block-contained layout)
        x_pc = placed_map[comp.ref]
        ic_pc = placed_map[ic_ref]
        pa = ic_pc.pin_abs(pin_a)
        pb = ic_pc.pin_abs(pin_b)
        if pa is None or pb is None:
            continue
        # Target: midpoint between the two pins, snapped to grid.
        target_pos = _snap_grid((((pa[0] + pb[0]) / 2.0),
                                  ((pa[1] + pb[1]) / 2.0)))
        cur = x_pc.pos
        if math.hypot(cur[0] - target_pos[0], cur[1] - target_pos[1]) <= max_dist:
            continue
        # Collision push-out: same outward-step strategy as decoupling
        # cap relocator. Direction = perpendicular to the pin-to-pin axis.
        axis_dx = pb[0] - pa[0]; axis_dy = pb[1] - pa[1]
        ax_len = math.hypot(axis_dx, axis_dy) or 1.0
        nx, ny = -axis_dy / ax_len, axis_dx / ax_len   # 90° rotation
        old_bb = _candidate_abs_bbox(x_pc.geom, x_pc.pos, x_pc.rotation)
        others = [b for b in placed_bboxes if b != old_bb]
        cand = target_pos
        for _ in range(8):
            bb = _candidate_abs_bbox(x_pc.geom, cand, x_pc.rotation)
            if not any(_bboxes_overlap(bb, ob, 2.54) for ob in others):
                break
            cand = _snap_grid((cand[0] + nx * 2.54, cand[1] + ny * 2.54))
        x_pc.pos = _snap_grid(cand)
        new_bb = _candidate_abs_bbox(x_pc.geom, x_pc.pos, x_pc.rotation)
        try:
            placed_bboxes.remove(old_bb)
        except ValueError:
            pass
        placed_bboxes.append(new_bb)
        relocated += 1
    return relocated


# Render-time options the dispatcher sets before delegating to
# render_flat / render_hierarchical. Lives at module scope so internal
# helpers (`_emit_block_rectangle` callsite, `_paper_for_ir`) can read
# without threading extra parameters through every signature. Reset to
# defaults at the top of each render() invocation.
_RENDER_OPTS: Dict[str, Any] = {"force_block_rects": False}


_LAYOUT_RULES_CACHE: Optional[Dict[str, Any]] = None


def _load_layout_rules() -> Dict[str, Any]:
    """Load envil_agent/config/layout_rules.json once. Returns {} on
    any read / parse error so callers default to no-op behaviour."""
    global _LAYOUT_RULES_CACHE
    if _LAYOUT_RULES_CACHE is not None:
        return _LAYOUT_RULES_CACHE
    try:
        cfg_path = (Path(__file__).resolve().parent.parent
                    / "config" / "layout_rules.json")
        with cfg_path.open("r", encoding="utf-8") as f:
            _LAYOUT_RULES_CACHE = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        _LAYOUT_RULES_CACHE = {}
    return _LAYOUT_RULES_CACHE


def _refdes_prefix(ref: str) -> str:
    """Extract the alphabetical refdes prefix (R12 -> R, FB1 -> FB,
    SW1 -> SW, U1A -> U). Used by cluster matchers to filter by
    refdes family per the JSON config."""
    out = []
    for ch in ref:
        if ch.isalpha():
            out.append(ch.upper())
        else:
            break
    return "".join(out)


def _comp_matches_lib_id_patterns(comp: IRComponent,
                                    patterns: List[str]) -> bool:
    """fnmatch the component's lib_id against any of the patterns.
    Case-insensitive."""
    if not patterns:
        return False
    import fnmatch
    lib_id = (comp.lib_id or "").lower()
    for pat in patterns:
        if fnmatch.fnmatchcase(lib_id, (pat or "").lower()):
            return True
    return False


def _comp_matches_value_patterns(comp: IRComponent,
                                   patterns: List[str]) -> bool:
    """fnmatch the component's value field against any of the patterns.
    Used to recognise crystal load caps by their `20p` / `22p` value
    without baking a value list into Python."""
    if not patterns:
        return True
    import fnmatch
    val = (comp.value or "").lower()
    if not val:
        return False
    for pat in patterns:
        if fnmatch.fnmatchcase(val, (pat or "").lower()):
            return True
    return False


def _shared_nets(comp_a_ref: str, comp_b_ref: str,
                  ir: TopologyIR) -> List:
    """Return the IRNet objects that include pins from both components."""
    out = []
    for net in ir.nets:
        has_a = any(p.startswith(f"{comp_a_ref}.") for p in net.pins)
        has_b = any(p.startswith(f"{comp_b_ref}.") for p in net.pins)
        if has_a and has_b:
            out.append(net)
    return out


def _component_pin_count(comp: IRComponent) -> int:
    try:
        return len(load_symbol(comp.lib_id).pins or [])
    except Exception:
        return 0


def _find_pin_on_comp_by_name_pattern(
    comp: IRComponent, name_patterns: List[str], ir: TopologyIR
) -> Optional[str]:
    """Return the pin key (number or name) on `comp` whose name matches
    any of `name_patterns` (case-insensitive). Used to find NRST /
    BOOT0 / SDA on a multi-pin IC.
    """
    if not name_patterns:
        return None
    try:
        geom = load_symbol(comp.lib_id)
    except Exception:
        return None
    needle_set = {p.lower() for p in name_patterns if p}
    for pin in geom.pins or []:
        if (pin.name or "").lower() in needle_set:
            return pin.number
    return None


def _apply_layout_cluster_rules(
    placed_map: Dict[str, "PlacedComp"],
    placed_bboxes: List[Tuple[float, float, float, float]],
    ir: TopologyIR,
) -> Dict[str, int]:
    """Read config/layout_rules.json:clusters and pull each declared
    satellite next to its anchor.

    Currently handles three cluster types based on JSON declarations:
      A) refdes-based anchor (e.g. crystal Y_x) + satellite refdes
         + shared-net match (e.g. load caps on the crystal pins)
      B) lib_id-based anchor (e.g. LED) + satellite refdes
         + shared-net match (LED + series resistor)
      C) pin-name-based anchor (e.g. MCU NRST / BOOT0 / SDA pin)
         + satellite refdes + shared-net match

    Mutates `placed_map` and `placed_bboxes` in place. Returns a
    per-cluster relocation count for telemetry. Idempotent --- a
    second call moves nothing because distances are already <=
    `max_satellite_distance_mm`.
    """
    rules = _load_layout_rules()
    if not rules.get("enabled", True):
        return {}
    clusters = rules.get("clusters") or []
    if not clusters:
        return {}

    counts: Dict[str, int] = {}

    for cluster in clusters:
        if not cluster.get("enabled", True):
            continue
        name = str(cluster.get("name") or "unnamed")
        anchor_prefixes = set(cluster.get("anchor_refdes_prefixes") or [])
        anchor_lib_patterns = cluster.get("anchor_lib_id_patterns") or []
        anchor_pin_names = cluster.get("anchor_pin_name_patterns") or []
        anchor_pin_overbars = cluster.get("anchor_pin_name_overbar_forms") or []
        anchor_pin_haystack = list(anchor_pin_names) + list(anchor_pin_overbars)
        sat_prefixes = set(cluster.get("satellite_refdes_prefixes") or [])
        sat_max_pins = int(cluster.get("satellite_max_pins") or 99)
        sat_value_patterns = cluster.get("satellite_value_patterns") or []
        sat_match_shared_net = bool(cluster.get(
            "satellite_match_via_shared_net", True))
        sat_must_share_gnd = bool(cluster.get(
            "satellite_must_share_gnd", False))
        max_dist = float(cluster.get("max_satellite_distance_mm", 10.16))
        perp_dist = float(cluster.get(
            "place_perpendicular_to_anchor_axis_mm", 5.08))

        # Identify anchor components in the IR.
        anchors: List[Tuple[IRComponent, Optional[str]]] = []
        if anchor_prefixes or anchor_lib_patterns:
            for comp in ir.components:
                prefix = _refdes_prefix(comp.ref)
                if anchor_prefixes and prefix not in anchor_prefixes:
                    if not _comp_matches_lib_id_patterns(comp, anchor_lib_patterns):
                        continue
                elif not anchor_prefixes:
                    if not _comp_matches_lib_id_patterns(comp, anchor_lib_patterns):
                        continue
                anchors.append((comp, None))
        elif anchor_pin_haystack:
            # Pin-name-based anchor: any multi-pin IC that has a pin
            # whose name matches the pattern.
            for comp in ir.components:
                if _component_pin_count(comp) < 3:
                    continue
                pin_key = _find_pin_on_comp_by_name_pattern(
                    comp, anchor_pin_haystack, ir)
                if pin_key is not None:
                    anchors.append((comp, pin_key))

        if not anchors:
            continue

        for anchor_comp, anchor_pin_key in anchors:
            if anchor_comp.ref not in placed_map:
                continue
            anchor_pc = placed_map[anchor_comp.ref]

            # Determine the anchor point on the schematic. For pin-name
            # anchors, the pin tip is the target; for crystal / LED,
            # the component centre is the target axis.
            if anchor_pin_key is not None:
                anchor_xy = anchor_pc.pin_abs(anchor_pin_key)
                if anchor_xy is None:
                    continue
                anchor_xy = (anchor_xy[0], anchor_xy[1])
            else:
                anchor_xy = anchor_pc.pos

            # Walk the IR for matching satellites.
            for sat in ir.components:
                if sat.ref == anchor_comp.ref:
                    continue
                if sat.ref not in placed_map:
                    continue
                if sat_prefixes and _refdes_prefix(sat.ref) not in sat_prefixes:
                    continue
                if _component_pin_count(sat) > sat_max_pins:
                    continue
                if sat_value_patterns and not _comp_matches_value_patterns(
                        sat, sat_value_patterns):
                    continue
                if sat_match_shared_net:
                    shared = _shared_nets(anchor_comp.ref, sat.ref, ir)
                    if not shared:
                        continue
                    if sat_must_share_gnd:
                        gnd_share = any(
                            (n.is_power and "GND" in (n.name or "").upper())
                            for n in shared
                        )
                        if not gnd_share:
                            # Look at ALL of sat's nets for a GND net,
                            # since the load cap's second pin goes to
                            # GND, not necessarily to the crystal.
                            sat_nets = [
                                net for net in ir.nets
                                if any(p.startswith(f"{sat.ref}.") for p in net.pins)
                            ]
                            if not any(
                                (net.is_power and "GND" in (net.name or "").upper())
                                for net in sat_nets
                            ):
                                continue

                sat_pc = placed_map[sat.ref]
                cur_x, cur_y = sat_pc.pos
                dx = cur_x - anchor_xy[0]
                dy = cur_y - anchor_xy[1]
                cur_dist = math.hypot(dx, dy)
                if cur_dist <= max_dist:
                    continue

                # Target = perpendicular offset from anchor along Y
                # (default outward axis). The collision push-out below
                # widens the offset until no overlap.
                tgt = _snap_grid(
                    (anchor_xy[0], anchor_xy[1] + perp_dist)
                )
                old_bb = _candidate_abs_bbox(
                    sat_pc.geom, sat_pc.pos, sat_pc.rotation
                )
                others = [b for b in placed_bboxes if b != old_bb]
                cand = tgt
                # 8 outward steps along +Y; rotate to -Y on every other
                # try so caps stack on both sides of the crystal axis.
                stepped = False
                for step_idx in range(8):
                    bb = _candidate_abs_bbox(
                        sat_pc.geom, cand, sat_pc.rotation
                    )
                    if not any(
                        _bboxes_overlap(bb, ob, 2.54) for ob in others
                    ):
                        stepped = True
                        break
                    sign = 1.0 if (step_idx % 2 == 0) else -1.0
                    cand = _snap_grid(
                        (cand[0], cand[1] + sign * 2.54 * (step_idx + 1))
                    )
                if not stepped:
                    continue
                sat_pc.pos = _snap_grid(cand)
                new_bb = _candidate_abs_bbox(
                    sat_pc.geom, sat_pc.pos, sat_pc.rotation
                )
                try:
                    placed_bboxes.remove(old_bb)
                except ValueError:
                    pass
                placed_bboxes.append(new_bb)
                counts[name] = counts.get(name, 0) + 1

    return counts


def _pick_block_anchor(block, ir: TopologyIR) -> Optional[IRComponent]:
    """Anchor of a self-contained block = component with most pins."""
    best = None
    best_pins = -1
    for ref in block.component_refs:
        comp = ir.component_by_ref(ref)
        if comp is None:
            continue
        try:
            n = len(load_symbol(comp.lib_id).pins)
        except ValueError:
            continue
        if n > best_pins:
            best, best_pins = comp, n
    return best


def _place_components_zoned(ir: TopologyIR,
                              paper: Optional[str] = None) -> List["PlacedComp"]:
    """Block-zone-aware placement. Each block's anchor (its highest-pin
    component) lands at the block's zone centre on the sheet; its
    satellites cluster around it via pin-role placement. Used when every
    block is self-contained (each has its own multi-pin IC/connector).

    `paper` selects the sheet size used for the title-block obstacle —
    when None the IR-driven picker chooses A3 for multi-block circuits."""
    if paper is None:
        paper = _paper_for_ir(ir)
    placed_map: Dict[str, "PlacedComp"] = {}
    placed_bboxes: List[Tuple[float, float, float, float]] = []
    # Seed placement with the title-block reserve so satellites never
    # land in the bottom-right title-block area. Treated like any other
    # placed-component bbox in collision checks. Paper from the IR-driven
    # picker so A3 single-sheet-with-blocks renders use A3 title-block dims.
    _tb_obs_pl = _title_block_obstacle(paper)
    if _tb_obs_pl is not None:
        placed_bboxes.append(_tb_obs_pl)
    CLEARANCE = _placement_cfg()["collision_clearance_mm"]   # JSON-driven (default 7.62 = 3 grid units, accounts for Reference/Value text bboxes extending ~2-3 mm outside each body)

    # Phase 2b: compute flow DAG once. Empty dict when disabled in config or
    # when there are <2 blocks — _zone_centre_for_block falls back to the
    # static ZONE_LAYOUT in that case, preserving prior behavior.
    flow_dag = _compute_flow_dag(ir)

    # Phase 5: 2-D MCU-centric block centres. Consulted BEFORE flow_dag for
    # MCU boards (one dominant MCU/controller block) — content-aware cells so
    # a tall MCU never overflows into a neighbour. Empty dict for non-MCU /
    # ambiguous boards, so those keep the flow_dag column grid (additive,
    # nothing removed). Per `multi_block.mcu_centric_layout`.
    mcu_centres = _mcu_centric_block_centres(ir, paper)

    # Step 1: pick a per-block anchor and place it at its zone centre.
    block_anchors: Dict[str, str] = {}
    for blk in ir.blocks:
        anchor = _pick_block_anchor(blk, ir)
        if anchor is None:
            continue
        try:
            geom = load_symbol(anchor.lib_id)
        except ValueError:
            continue
        zone_centre = (mcu_centres.get(blk.name)
                       or _zone_centre_for_block(blk, flow_dag, SHEET_CENTRE, paper=paper))
        anchor_pos = _snap_grid(zone_centre)
        pc = PlacedComp(
            ref=anchor.ref, lib_id=anchor.lib_id,
            value=anchor.value, footprint=anchor.footprint,
            pos=anchor_pos, rotation=0.0, geom=geom,
        )
        for p in geom.pins:
            pc.pin_uuids[p.number] = _u()
        placed_map[anchor.ref] = pc
        placed_bboxes.append(_candidate_abs_bbox(geom, anchor_pos, 0.0))
        block_anchors[blk.name] = anchor.ref

    # Step 2: for each block, place satellites adjacent to the BLOCK'S
    # anchor's pins (using the same pin-role algorithm but scoped to the
    # block's anchor, not the global anchor).
    pin_index = _build_pin_to_component_index(ir)
    fallback_index = 0
    for blk in ir.blocks:
        anchor_ref = block_anchors.get(blk.name)
        if anchor_ref is None:
            continue
        anchor_pc = placed_map[anchor_ref]
        for ref in blk.component_refs:
            if ref == anchor_ref or ref in placed_map:
                continue
            comp = ir.component_by_ref(ref)
            if comp is None:
                continue
            try:
                sat_geom = load_symbol(comp.lib_id)
            except ValueError:
                continue
            pos: Optional[Tuple[float, float]] = None
            rot = 0.0
            # Try each net this satellite is on — pick an anchor pin
            # belonging to THIS block's anchor.
            candidates = []
            for (net_name, pin_key) in pin_index.get(ref, []):
                net_obj = next((n for n in ir.nets if n.name == net_name), None)
                if net_obj is None:
                    continue
                for pinref in net_obj.pins:
                    pref, pkey = pinref.split(".", 1) if "." in pinref else ("", "")
                    if pref == anchor_ref:
                        priority = 1 if net_obj.is_power else 0
                        candidates.append((priority, pkey))
                        break
            candidates.sort(key=lambda t: t[0])
            for _, anchor_pin_key in candidates:
                attempt = _satellite_offset_for_pin(anchor_pc, anchor_pin_key, sat_geom)
                if attempt is None:
                    continue
                cand_pos, cand_rot = attempt
                # Push outward until no collision with previously placed
                # bboxes.
                pin_abs = anchor_pc.pin_abs(anchor_pin_key)
                side = pin_side_from_rot(pin_abs[2]) if pin_abs else "right"
                outward = {"right": (1, 0), "left": (-1, 0),
                            "top": (0, -1), "bottom": (0, 1)}.get(side, (1, 0))
                for _ in range(8):
                    bb = _candidate_abs_bbox(sat_geom, cand_pos, cand_rot)
                    if not any(_bboxes_overlap(bb, ob, CLEARANCE) for ob in placed_bboxes):
                        break
                    cand_pos = (cand_pos[0] + outward[0] * 2.54,
                                cand_pos[1] + outward[1] * 2.54)
                    cand_pos = _snap_grid(cand_pos)
                bb = _candidate_abs_bbox(sat_geom, cand_pos, cand_rot)
                if not any(_bboxes_overlap(bb, ob, CLEARANCE) for ob in placed_bboxes):
                    pos, rot = cand_pos, cand_rot
                    break
            # Spiral fallback within the zone centre
            if pos is None:
                zone_centre = (mcu_centres.get(blk.name)
                               or _zone_centre_for_block(blk, flow_dag, SHEET_CENTRE, paper=paper))
                _spiral_step = _placement_cfg()["spiral_step_mm"]
                for spiral_try in range(40):
                    idx = fallback_index + spiral_try
                    ring = (idx // 8) + 2
                    slot = idx % 8
                    angle = (slot / 8) * 2 * math.pi
                    cand_pos = _snap_grid((
                        zone_centre[0] + ring * _spiral_step * math.cos(angle),
                        zone_centre[1] + ring * _spiral_step * math.sin(angle),
                    ))
                    # Keep every spiral candidate on the sheet — a far ring
                    # around a zone near the page edge could otherwise spiral
                    # off-page.
                    cand_pos = _clamp_candidate_to_page(sat_geom, cand_pos, 0.0, paper)
                    bb = _candidate_abs_bbox(sat_geom, cand_pos, 0.0)
                    if not any(_bboxes_overlap(bb, ob, CLEARANCE) for ob in placed_bboxes):
                        pos = cand_pos
                        fallback_index = idx + 1
                        break
            if pos is None:
                # Last resort: stack just below this block's anchor (in-zone,
                # on-sheet) instead of flinging the part to a fixed off-page
                # offset. Clamped so it can never cross the border.
                ax, ay = anchor_pc.pos
                pos = _clamp_candidate_to_page(
                    sat_geom,
                    _snap_grid((ax, ay + _placement_cfg()["spiral_step_mm"]
                                * (1 + fallback_index % 4))),
                    rot, paper)
                fallback_index += 1
            pc = PlacedComp(
                ref=comp.ref, lib_id=comp.lib_id,
                value=comp.value, footprint=comp.footprint,
                pos=pos, rotation=rot, geom=sat_geom,
            )
            for p in sat_geom.pins:
                pc.pin_uuids[p.number] = _u()
            placed_map[comp.ref] = pc
            placed_bboxes.append(_candidate_abs_bbox(sat_geom, pos, rot))

    # Place any components NOT assigned to a block (architect oversight)
    for c in ir.components:
        if c.ref in placed_map:
            continue
        try:
            sat_geom = load_symbol(c.lib_id)
        except ValueError:
            continue
        # Unassigned (architect oversight): drop into the first free in-bounds
        # slot rather than a fixed off-page offset. Push down + clamp so an
        # orphan never crosses the border.
        cand_pos = _snap_grid((SHEET_CENTRE[0] + 100, SHEET_CENTRE[1] + 50))
        for _orphan_try in range(40):
            cand_pos = _clamp_candidate_to_page(sat_geom, cand_pos, 0.0, paper)
            bb = _candidate_abs_bbox(sat_geom, cand_pos, 0.0)
            if not any(_bboxes_overlap(bb, ob, CLEARANCE) for ob in placed_bboxes):
                break
            cand_pos = _snap_grid((cand_pos[0], cand_pos[1]
                                   + _placement_cfg()["spiral_step_mm"]))
        pc = PlacedComp(
            ref=c.ref, lib_id=c.lib_id, value=c.value, footprint=c.footprint,
            pos=cand_pos, rotation=0.0, geom=sat_geom,
        )
        for p in sat_geom.pins:
            pc.pin_uuids[p.number] = _u()
        placed_map[c.ref] = pc
        placed_bboxes.append(_candidate_abs_bbox(sat_geom, cand_pos, 0.0))

    # Phase 3 post-pass: relocate decoupling caps adjacent to their target
    # IC's power pin. Two paths:
    #   (a) ir.blocks non-empty  -> Phase-3 zoned path (force=False)
    #   (b) ir.blocks empty       -> opt-in via JSON config
    #       (multi_block.decoupling.apply_to_flat_single_block).
    # The opt-in flag exists because legacy single-IC outputs (NE555,
    # LM317, op-amp filters) were tuned without this pass and changing
    # them is a behaviour change. New circuits get clean placement
    # automatically; old projects can revert by setting the JSON flag
    # to false. Pure data switch — no per-circuit logic.
    _flat_cfg = _load_layout_config().get("multi_block", {}).get("decoupling", {})
    if ir.blocks:
        # Under the 2-D MCU-centric layout, keep satellites inside their own
        # block (no cross-block pull) so the content-aware cells stay clean.
        _same_block = bool(mcu_centres)
        _relocate_decoupling_caps(placed_map, placed_bboxes, ir,
                                  same_block_only=_same_block)
        _relocate_crystals(placed_map, placed_bboxes, ir,
                           same_block_only=_same_block)
    elif _flat_cfg.get("apply_to_flat_single_block", True):
        # `force=True` bypasses the "no blocks" early return inside
        # _relocate_decoupling_caps and uses the tighter `child_sheet_
        # max_distance_from_vdd_mm` threshold — the same one that
        # already worked for hierarchical-child renders.
        _relocate_decoupling_caps(placed_map, placed_bboxes, ir, force=True)
        _relocate_crystals(placed_map, placed_bboxes, ir, force=True)

    # G2 (added 2026-06-01): config-driven cluster tightening. Reads
    # config/layout_rules.json:clusters and pulls each declared
    # satellite next to its anchor (crystal load caps -> crystal,
    # LED -> series resistor, NRST pullup -> NRST pin, ...). Idempotent
    # and no-op when no rules match, so safe to call after the
    # legacy relocators.
    _apply_layout_cluster_rules(placed_map, placed_bboxes, ir)

    out = [placed_map[c.ref] for c in ir.components]
    # Final guarantee (mcu-centric only): re-space the blocks from their
    # MEASURED drawn-box size so no two block rectangles ever touch — the
    # promise estimating alone can't keep for a tall MCU + stacked caps.
    # No-op when the mcu-centric layout isn't active.
    _mcu_centric_reflow(out, ir, paper)
    return out


def _satellite_offset_for_pin(anchor: "PlacedComp", pin_key: str,
                               sat_geom: SymbolGeom,
                               extra_gap: Optional[float] = None
                               ) -> Optional[Tuple[Tuple[float, float], float]]:
    """Compute (absolute position, rotation) so a satellite component sits
    along the outward axis of the anchor's pin `pin_key`.

    `extra_gap` is the clear distance between the IC pin tip and the
    NEAREST pin of the satellite. Default comes from JSON config
    `placement.satellite_gap_mm` (default 5.08 mm = 2 grid cells) —
    wider than the old 2.54 default so dense ICs (74HC595 with 16
    radial passives) have visible breathing room between wires.
    Returns None if the pin can't be resolved."""
    if extra_gap is None:
        try:
            extra_gap = float(_load_layout_config().get(
                "placement", {}).get("satellite_gap_mm", 5.08))
        except Exception:
            extra_gap = 5.08
    pin = anchor.geom.resolve_pin(pin_key)
    if pin is None:
        return None
    abs_pin = anchor.pin_abs(pin_key)
    if abs_pin is None:
        return None
    px, py, pin_abs_rot = abs_pin
    side = pin_side_from_rot(pin_abs_rot)
    outward = {"right": (1, 0), "left": (-1, 0),
               "top": (0, -1), "bottom": (0, 1)}.get(side, (1, 0))
    # Satellite centre = pin tip + outward * (sat pin-to-centre + extra_gap).
    # For Device:R / Device:C the local pin 1 is at y=3.81 (Y-up), so
    # pin-to-centre = 3.81 mm. With extra_gap=2.54, the cap's pin 1 sits
    # 2.54 mm from the IC pin tip, and its centre at 6.35 mm out.
    pin_to_centre = 3.81  # standard for 2-pin passives
    if sat_geom.pins:
        # Use the actual local distance from pin 1 to symbol origin
        pin_to_centre = max(abs(p.y_local) for p in sat_geom.pins[:2]) or pin_to_centre
    centre_offset = pin_to_centre + extra_gap
    pos = (px + outward[0] * centre_offset, py + outward[1] * centre_offset)
    pos = _snap_grid(pos)
    rotation = 90.0 if side in ("left", "right") else 0.0
    return pos, rotation


def _bboxes_overlap(b1, b2, margin: float = 0.0) -> bool:
    """True if two axis-aligned absolute bboxes overlap (with margin)."""
    x1a, y1a, x2a, y2a = b1
    x1b, y1b, x2b, y2b = b2
    return not (x2a + margin <= x1b - margin or x2b + margin <= x1a - margin
                 or y2a + margin <= y1b - margin or y2b + margin <= y1a - margin)


def _candidate_abs_bbox(geom, pos, rot):
    """Compute what _abs_outer_bbox would return WITHOUT requiring a
    PlacedComp instance. Used during placement collision checks."""
    x1, y1, x2, y2 = geom.outer_bbox
    corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
    corners = [(x, -y) for x, y in corners]
    rad = math.radians(rot)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r) for x, y in corners]
    corners = [(pos[0] + x, pos[1] + y) for x, y in corners]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _usable_page_rect(paper: Optional[str] = None
                      ) -> Tuple[float, float, float, float]:
    """The in-bounds placement rectangle (x1, y1, x2, y2): page inset by the
    placement margin on all sides, with the bottom-right title-block band
    reserved at the bottom. Single source of truth shared by the placement
    fallbacks (_clamp_candidate_to_page) so a part can never be flung off
    the sheet."""
    margin = _placement_cfg()["page_margin_mm"]
    page_w, page_h = _page_dims(paper)
    tb = _title_block_obstacle(paper)
    uy2 = page_h - margin if tb is None else min(page_h - margin, tb[1])
    return (margin, margin, page_w - margin, uy2)


def _clamp_candidate_to_page(geom, pos, rot, paper: Optional[str] = None
                             ) -> Tuple[float, float]:
    """Nudge ONE candidate position so its outer bbox fits inside the usable
    page rect. Used by the zoned placer's last-resort fallbacks so a
    satellite/orphan that fails adjacent + spiral placement can never land
    off the sheet (the root cause of 'a component crosses the red border').
    On-grid; a no-op when the candidate already fits."""
    ux1, uy1, ux2, uy2 = _usable_page_rect(paper)
    x1, y1, x2, y2 = _candidate_abs_bbox(geom, pos, rot)
    dx = dy = 0.0
    if x1 < ux1:
        dx = ux1 - x1
    elif x2 > ux2:
        dx = ux2 - x2
    if y1 < uy1:
        dy = uy1 - y1
    elif y2 > uy2:
        dy = uy2 - y2
    return _snap_grid((pos[0] + dx, pos[1] + dy))


def _pin_sides(geom, pos, rot) -> Tuple[bool, bool]:
    """`(has_left_right_pins, has_top_bottom_pins)` for a symbol geometry
    placed at (pos, rot). Shared by `_place_fields` (which edge carries
    the text) and `_field_text_bbox` (how far the text protrudes)."""
    has_lr = has_tb = False
    for pin in geom.pins:
        side = pin_side_from_rot(place_pin(pin, pos[0], pos[1], rot)[2])
        if side in ("left", "right"):
            has_lr = True
        elif side in ("top", "bottom"):
            has_tb = True
    return has_lr, has_tb


def _field_text_bbox(geom, pos, rot, side_allow: float,
                      stack_allow: float = 2.54):
    """`_candidate_abs_bbox` inflated on the edge(s) that carry the
    Reference/Value text under `_place_fields`, so collision/de-stack
    passes leave room for the field text and labels never overwrite a
    neighbour (user: "letter ellam overwrite aakuthu konjam space vittu").

    Vertical 2-pin passives carry side text (right edge) → inflate +X by
    `side_allow`. Everything else carries name-above / value-below →
    inflate ±Y by `stack_allow`. The base body+pin bbox is unchanged, so
    a board with `text_aware_spacing` off is byte-identical."""
    x1, y1, x2, y2 = _candidate_abs_bbox(geom, pos, rot)
    has_lr, has_tb = _pin_sides(geom, pos, rot)
    if has_tb and not has_lr:
        x2 += side_allow
    else:
        y1 -= stack_allow
        y2 += stack_allow
    return (x1, y1, x2, y2)


def _est_field_protrusion(pc: "PlacedComp", char_w: float = 0.85,
                           gap: float = 1.27) -> float:
    """Right-side reach (mm) of a side-placed Reference/Value: field gap
    plus the WIDER of the ref/value string at the 1.27 mm (50-mil) font.
    Ref and Value sit on separate stacked lines, so it's the max, not the
    sum."""
    n = max(len(str(pc.ref)), len(str(pc.value)))
    return gap + n * char_w + 0.5


def _has_ic_anchor(ir: TopologyIR, min_pins: int = 3) -> bool:
    """True when any component has at least `min_pins` pins — used by Phase
    5 to decide whether to skip the discrete-topology templates."""
    for comp in ir.components:
        try:
            n = len(load_symbol(comp.lib_id).pins)
        except ValueError:
            continue
        if n >= min_pins:
            return True
    return False


def _place_universal_discrete(ir: TopologyIR) -> List[PlacedComp]:
    """Universal flow-graph placer for circuits with NO blocks and NO
    IC anchor (2-pin components only).

    Replaces the per-topology templates (bridge rectifier, voltage
    divider, etc.) with a single generic algorithm. No hardcoded
    shapes, no part-number or topology-class specialisation. Works
    for any discrete circuit.

    Algorithm:
      1. Classify nets: SOURCE (is_power, non-GND), SINK (GND-like),
         MIDDLE (signal nets).
      2. BFS layer assignment from SOURCE nets (layer 0); propagate
         through every component to its other nets.
      3. SINK nets get max_layer + 1 (bottom).
      4. Each component layer = average of its connected nets layers.
      5. Group by quantised half-layer, sort by ref within each layer
         for stable output.
      6. Place vertical: source at TOP, sink at BOTTOM, sheet centred.
         Layer pitch chosen so consecutive layers fall within the
         engines 12.7mm power-net cluster radius, so shared-net pins
         auto-cluster into ONE shared label + wire instead of dup labels."""
    cfg = _load_layout_config().get("multi_block", {}).get("discrete_templates", {})
    if not cfg.get("enabled", True):
        return _place_components_fallback(ir)

    pin_index = _build_pin_to_component_index(ir)
    gnd_names = {"GND", "AGND", "DGND", "PGND", "VSS", "VEE", "EGND", "SGND"}

    # Classify nets.
    source_nets: List[str] = []
    sink_nets: List[str] = []
    for net in ir.nets:
        upper = net.name.upper().lstrip("+")
        if upper in gnd_names:
            sink_nets.append(net.name)
        elif net.is_power:
            source_nets.append(net.name)

    # BFS layer assignment.
    net_layer: Dict[str, int] = {n: 0 for n in source_nets}
    visited = set(source_nets) | set(sink_nets)
    queue: List[str] = list(source_nets)
    while queue:
        cur_net = queue.pop(0)
        cur_l = net_layer[cur_net]
        for comp in ir.components:
            nets_for_comp = {nn for (nn, _pk) in pin_index.get(comp.ref, [])}
            if cur_net not in nets_for_comp:
                continue
            for other in nets_for_comp:
                if other in visited:
                    continue
                net_layer[other] = cur_l + 1
                visited.add(other)
                queue.append(other)

    max_layer = max(net_layer.values()) if net_layer else 0
    for s in sink_nets:
        net_layer[s] = max_layer + 1

    # Component layer = average of connected net layers.
    comp_layer: Dict[str, float] = {}
    for comp in ir.components:
        nets_for_comp = {nn for (nn, _pk) in pin_index.get(comp.ref, [])}
        layers = [net_layer[n] for n in nets_for_comp if n in net_layer]
        if layers:
            comp_layer[comp.ref] = sum(layers) / len(layers)
        else:
            comp_layer[comp.ref] = max_layer + 2

    # Group by half-integer layer.
    layer_groups: Dict[int, List[str]] = {}
    for ref, lf in comp_layer.items():
        key = int(round(lf * 2))
        layer_groups.setdefault(key, []).append(ref)

    cx, cy = SHEET_CENTRE
    pitch_y = 5.08
    slot_x = 15.24

    sorted_layers = sorted(layer_groups.keys())
    n_layers = len(sorted_layers)

    placed_map: Dict[str, PlacedComp] = {}
    placed_bboxes: List[Tuple[float, float, float, float]] = []

    for idx, lk in enumerate(sorted_layers):
        y_off = (idx - (n_layers - 1) / 2) * pitch_y * 2
        y = cy + y_off
        refs = sorted(layer_groups[lk])
        for slot_idx, ref in enumerate(refs):
            x_off = (slot_idx - (len(refs) - 1) / 2) * slot_x
            x = cx + x_off
            comp = ir.component_by_ref(ref)
            if comp is None:
                continue
            try:
                geom = load_symbol(comp.lib_id)
            except ValueError:
                continue
            pos = _snap_grid((x, y))
            pc = PlacedComp(
                ref=comp.ref, lib_id=comp.lib_id, value=comp.value,
                footprint=comp.footprint, pos=pos, rotation=0.0, geom=geom,
            )
            for pn in geom.pins:
                pc.pin_uuids[pn.number] = _u()
            placed_map[comp.ref] = pc
            placed_bboxes.append(_candidate_abs_bbox(geom, pos, 0.0))

    # Floating components fall through to a spiral at the right edge.
    fb_idx = 0
    for c in ir.components:
        if c.ref in placed_map:
            continue
        try:
            geom = load_symbol(c.lib_id)
        except ValueError:
            continue
        pos = _snap_grid((cx + 60, cy + fb_idx * 10.16))
        fb_idx += 1
        pc = PlacedComp(
            ref=c.ref, lib_id=c.lib_id, value=c.value, footprint=c.footprint,
            pos=pos, rotation=0.0, geom=geom,
        )
        for pn in geom.pins:
            pc.pin_uuids[pn.number] = _u()
        placed_map[c.ref] = pc

    return [placed_map[c.ref] for c in ir.components if c.ref in placed_map]


def _place_components_fallback(ir: TopologyIR) -> List[PlacedComp]:
    """Last-resort minimal grid placer when discrete_templates is
    disabled in config. Spirals every component out from sheet centre."""
    cx, cy = SHEET_CENTRE
    placed_map: Dict[str, PlacedComp] = {}
    for i, comp in enumerate(ir.components):
        try:
            geom = load_symbol(comp.lib_id)
        except ValueError:
            continue
        row, col = divmod(i, 4)
        pos = _snap_grid((cx + (col - 1.5) * 15.24, cy + (row - 1) * 10.16))
        pc = PlacedComp(
            ref=comp.ref, lib_id=comp.lib_id, value=comp.value,
            footprint=comp.footprint, pos=pos, rotation=0.0, geom=geom,
        )
        for pn in geom.pins:
            pc.pin_uuids[pn.number] = _u()
        placed_map[comp.ref] = pc
    return [placed_map[c.ref] for c in ir.components if c.ref in placed_map]


@traceable(run_type="tool", name="Place components")
def _place_components(ir: TopologyIR,
                       paper: Optional[str] = None) -> List[PlacedComp]:
    """Anchor-first placement with HARD no-overlap-with-anchor constraint.

    The IC sits at sheet centre; each satellite is placed on the outward
    axis of the IC pin it connects to. After tentative placement, any
    satellite whose bbox overlaps the anchor's bbox (or another
    satellite's bbox) is pushed further outward until it clears. This is
    what professional schematics enforce — never let a satellite land on
    top of the IC body.

    `paper` selects the sheet size for the title-block obstacle. When
    None, _paper_for_ir picks: A4 for single-IC flat, A3 for blocked
    flat (JSON-driven via hierarchy_layout.single_sheet_with_blocks_paper).

    Phase 5 (discrete topology): if the IR matches a known discrete-only
    topology (bridge rectifier, etc.) with NO IC anchor and NO blocks,
    delegate to the template placer for a canonical layout.

    If the IR has blocks and every block is self-contained (owns its own
    multi-pin IC/connector), delegate to the zone-layout placer instead
    — that produces the canonical "MCU centre, blocks fan out around"
    arrangement seen in professional reference schematics.
    """
    if paper is None:
        paper = _paper_for_ir(ir)
    # Discrete-circuit dispatch: NO blocks + NO IC anchor → universal
    # flow-graph placer. Handles ANY 2-pin-only circuit (voltage divider,
    # bridge rectifier, RC filter, LED + R, transistor networks, etc.)
    # via topo-sort layer assignment + grid placement. No hardcoded
    # shapes or per-class templates — dynamic and generic per the user
    # rule `feedback_dynamic_universal_quality`.
    if not ir.blocks and not _has_ic_anchor(ir):
        return _place_universal_discrete(ir)

    if ir.blocks:
        _mb = _load_layout_config().get("multi_block", {})
        _anchored = sum(1 for b in ir.blocks if _block_has_own_anchor(b, ir))
        # All blocks self-contained -> zoned placement (original behaviour).
        #
        # Partial zoning (gated by `allow_partial_zoning`): a genuine
        # multi-IC board where MOST but not every block owns a multi-pin
        # part must STILL place block-by-block. The all-or-nothing gate
        # used to collapse the whole circuit to a single global anchor the
        # moment one block was passive-only (e.g. a CLOCK block = crystal +
        # two load caps, all 2-pin). That scattered every block's parts
        # into one concentric ring, so the per-block rectangles became huge
        # page-spanning boxes that overlapped each other (the "blocks drawn
        # on top of blocks" defect). The zoned placer already anchors a
        # passive block on its highest-pin component (the crystal) and
        # clusters its caps there, so it handles these blocks cleanly --
        # we just have to let it run. Single-IC circuits (NE555, LM317,
        # op-amp) are unaffected: the architect emits blocks=[] for those,
        # so this branch is never entered. `zoned_min_anchored_blocks`
        # (default 2) keeps the flat path for circuits that aren't really
        # multi-IC (only 0-1 blocks own an anchor).
        _min_anchored = int(_mb.get("zoned_min_anchored_blocks", 2))
        if (_anchored == len(ir.blocks)
                or (_mb.get("allow_partial_zoning", False)
                    and _anchored >= _min_anchored)):
            return _place_components_zoned(ir, paper=paper)

    anchor_ref = _pick_anchor(ir).ref
    pin_index = _build_pin_to_component_index(ir)

    # Pass 1: place the anchor.
    anchor_comp = ir.component_by_ref(anchor_ref)
    anchor_geom = load_symbol(anchor_comp.lib_id)
    placed_map: Dict[str, PlacedComp] = {}
    anchor = PlacedComp(
        ref=anchor_comp.ref, lib_id=anchor_comp.lib_id,
        value=anchor_comp.value, footprint=anchor_comp.footprint,
        pos=SHEET_CENTRE, rotation=0.0, geom=anchor_geom,
    )
    for p in anchor_geom.pins:
        anchor.pin_uuids[p.number] = _u()
    placed_map[anchor_ref] = anchor

    anchor_bbox = _candidate_abs_bbox(anchor_geom, anchor.pos, 0.0)
    placed_bboxes: List[Tuple[float, float, float, float]] = [anchor_bbox]
    # Title-block bbox added so satellites + fallback-spiral positions
    # never land in the bottom-right title-block reserve. Treated like
    # any other placed-component bbox in collision checks. Paper from
    # the caller (IR-driven) so A3 single-sheet-with-blocks renders
    # use the larger A3 title-block dims.
    _tb_obs_pl = _title_block_obstacle(paper)
    if _tb_obs_pl is not None:
        placed_bboxes.append(_tb_obs_pl)
    # 5.08 mm = 2 grid units — wider gap so adjacent components'
    # Reference/Value text bboxes (extending ~2 mm outside each body)
    # don't overlap. KLC convention; matches professional reference
    # schematics that the user is targeting.
    CLEARANCE = _placement_cfg()["collision_clearance_mm"]   # JSON-driven (default 7.62 = 3 grid units, accounts for Reference/Value text bboxes extending ~2-3 mm outside each body)
    fallback_index = 0

    # Helper: push a candidate further out along the pin's outward axis
    # until it clears all existing bboxes.
    def _resolve_overlap(geom, pos, rot, anchor_pin_key):
        pin_abs = anchor.pin_abs(anchor_pin_key) if anchor_pin_key else None
        side = pin_side_from_rot(pin_abs[2]) if pin_abs else "right"
        outward = {"right": (1, 0), "left": (-1, 0),
                    "top": (0, -1), "bottom": (0, 1)}.get(side, (1, 0))
        for _ in range(8):
            bb = _candidate_abs_bbox(geom, pos, rot)
            collided = any(_bboxes_overlap(bb, ob, CLEARANCE) for ob in placed_bboxes)
            if not collided:
                return pos
            pos = (pos[0] + outward[0] * 2.54, pos[1] + outward[1] * 2.54)
            pos = _snap_grid(pos)
        return pos  # give up — caller will accept whatever

    # Pass 2: place each satellite.
    for c in ir.components:
        if c.ref == anchor_ref:
            continue
        sat_geom = load_symbol(c.lib_id)
        pos: Optional[Tuple[float, float]] = None
        rot = 0.0
        used_pin_key: Optional[str] = None

        # Decoupling cap heuristic: 2-pin cap on +VCC/GND (both nets are
        # is_power) gets placed adjacent to the IC's VCC pin (priority 0
        # — highest). Otherwise: signal-net anchor pins before power-net
        # anchor pins. The architect's intent ("this cap is decoupling
        # for U1.VCC") is inferred from the cap touching both a positive
        # power rail and the GND rail with the anchor IC on those rails.
        # Decoupling pattern detected purely from topology — 2-pin part
        # touching the anchor on a positive power rail (and implicitly
        # on GND via the other pin). No refdes-prefix hardcoding per
        # [feedback_no_hardcode_json_config]: a cap named "BYP1" or
        # ferrite bead "FB1" used as a bypass element must be detected
        # the same way as one named "C1".
        is_decoupling_cap = (
            len(sat_geom.pins) == 2
            and any(
                n.is_power and n.name.upper() != "GND"
                and any(p.startswith(c.ref + ".") for p in n.pins)
                and any(p.startswith(anchor_ref + ".") for p in n.pins)
                for n in ir.nets
            )
        )
        candidates = []
        for (net_name, pin_key) in pin_index.get(c.ref, []):
            net_obj = next((n for n in ir.nets if n.name == net_name), None)
            if net_obj is None:
                continue
            for pinref in net_obj.pins:
                ref, pkey = pinref.split(".", 1) if "." in pinref else ("", "")
                if ref == anchor_ref:
                    if is_decoupling_cap and net_obj.is_power and net_obj.name.upper() != "GND":
                        priority = -1   # decoupling caps anchor on the +V pin
                    elif net_obj.is_power:
                        priority = 1
                    else:
                        priority = 0
                    candidates.append((priority, pkey))
                    break
        candidates.sort(key=lambda t: t[0])

        for _, anchor_pin_key in candidates:
            attempt = _satellite_offset_for_pin(anchor, anchor_pin_key, sat_geom)
            if attempt is None:
                continue
            cand_pos, cand_rot = attempt
            cand_pos = _resolve_overlap(sat_geom, cand_pos, cand_rot, anchor_pin_key)
            # Verify final position doesn't collide
            bb = _candidate_abs_bbox(sat_geom, cand_pos, cand_rot)
            if not any(_bboxes_overlap(bb, ob, CLEARANCE) for ob in placed_bboxes):
                pos, rot, used_pin_key = cand_pos, cand_rot, anchor_pin_key
                break

        # Fallback: spiral, also collision-resolved + grid-snapped
        if pos is None:
            for spiral_try in range(40):
                idx = fallback_index + spiral_try
                ring = (idx // 8) + 2
                slot = idx % 8
                angle = (slot / 8) * 2 * math.pi
                cand_pos = (SHEET_CENTRE[0] + ring * CELL * math.cos(angle),
                             SHEET_CENTRE[1] + ring * CELL * math.sin(angle))
                cand_pos = _snap_grid(cand_pos)
                # Keep the spiral on the sheet (a far ring can otherwise run
                # off the page edge, esp. on A4/A5).
                cand_pos = _clamp_candidate_to_page(sat_geom, cand_pos, 0.0, paper)
                bb = _candidate_abs_bbox(sat_geom, cand_pos, 0.0)
                if not any(_bboxes_overlap(bb, ob, CLEARANCE) for ob in placed_bboxes):
                    pos = cand_pos
                    fallback_index = idx + 1
                    break
            if pos is None:
                # In-bounds last resort instead of a fixed off-page offset.
                pos = _clamp_candidate_to_page(
                    sat_geom, _snap_grid((SHEET_CENTRE[0], SHEET_CENTRE[1])),
                    rot, paper)

        pc = PlacedComp(
            ref=c.ref, lib_id=c.lib_id, value=c.value, footprint=c.footprint,
            pos=pos, rotation=rot, geom=sat_geom,
        )
        for p in sat_geom.pins:
            pc.pin_uuids[p.number] = _u()
        placed_map[c.ref] = pc
        placed_bboxes.append(_candidate_abs_bbox(sat_geom, pos, rot))

    return [placed_map[c.ref] for c in ir.components]


# ---------------------------------------------------------------------------
# S-expression emission
# ---------------------------------------------------------------------------

def _emit_header(file_uuid: str, title: str = "",
                  page_label: str = "", company: str = "Envil",
                  paper: Optional[str] = None) -> str:
    """File header + optional title block. KiCad shows the title block in
    the lower-right of every sheet; senior-designer reviews expect it
    populated with project name, revision and date. `paper` controls
    the page-size token KiCad uses — pass None to pick the JSON
    `hierarchy_layout.default_render_paper`, or an explicit name when
    rendering hierarchy parent sheets that auto-promote to A3/A2."""
    if not paper:
        paper = _default_render_paper()
    import datetime
    today = datetime.date.today().isoformat()
    # KiCad recognises a fixed set of standard paper names — A0..A4,
    # USLetter, USLegal, USLedger. Anything else (A5, A6, custom)
    # MUST be emitted as `(paper "User" <w> <h>)` or kicad-cli hangs
    # parsing the file. Detect non-standard names and switch to the
    # User form using `_page_dims` (JSON-driven page_sizes map).
    _kicad_standard_papers = {"A0", "A1", "A2", "A3", "A4",
                                "USLetter", "USLegal", "USLedger",
                                "A", "B", "C", "D", "E"}
    if paper in _kicad_standard_papers:
        paper_line = f'\t(paper {_qstr(paper)})\n'
    else:
        pw, ph = _page_dims(paper)
        paper_line = f'\t(paper "User" {_num(pw)} {_num(ph)})\n'
    tb = ""
    if title:
        tb = (
            '\t(title_block\n'
            f'\t\t(title {_qstr(title)})\n'
            f'\t\t(date {_qstr(today)})\n'
            '\t\t(rev "1.0")\n'
            f'\t\t(company {_qstr(company)})\n'
        )
        if page_label:
            tb += f'\t\t(comment 1 {_qstr(page_label)})\n'
        tb += '\t)\n'
    return (
        '(kicad_sch\n'
        '\t(version 20250114)\n'
        '\t(generator "envil_agent")\n'
        '\t(generator_version "0.1")\n'
        f'\t(uuid {_qstr(file_uuid)})\n'
        f'{paper_line}'
        f'{tb}'
    )


def _compute_hierarchy_grid(n_blocks: int,
                            pin_counts: Optional[List[int]] = None) -> Dict[str, Any]:
    """Dynamic sheet-grid layout for the parent (root) sheet.

    Reads layout_config.json -> hierarchy_layout. Iterates page sizes in
    priority order; for each page tries grid shapes (cols x rows) and
    picks the (paper, grid) combo where every box meets min_sheet_box,
    every box fits inside the usable area (page minus margins minus
    title-block reserve), AND the cell aspect ratio is closest to
    target_box_aspect.

    `pin_counts` (one entry per block, in order) feeds the BUSIEST block's
    hierarchical-sheet-pin count into the box-height floor: each box must be
    tall enough to stack its pins inside the edge(s) (`_emit_sheet_box`
    places them top_gap below the top, one per pitch_mm), or the pins run off
    the box bottom and off the page — the hierarchy-sheet layout error. With
    distribute_both_edges on, pins split L+R so only half the count drives the
    height. None = legacy aspect-only sizing.

    Returns {'paper', 'positions': [(x, y, w, h), ...]} with positions
    sized so the FULL block, INCLUDING its hierarchical sheet pins, is
    contained inside the printable sheet area and does not overlap the
    title block. Pure data-driven — no hardcoded grid shape, no per-
    circuit logic. Single-block / flat circuits never invoke this code.
    """
    cfg = _load_layout_config().get("hierarchy_layout", {}) or {}
    papers   = cfg.get("page_sizes", {}) or {}
    priority = cfg.get("page_size_priority", ["A4", "A3", "A2"]) or ["A4"]
    margin   = cfg.get("margin_mm", {}) or {}
    tb_res   = cfg.get("title_block_reserve_mm", {}) or {}
    gap_x    = float(cfg.get("gap_mm", {}).get("x", 12.0))
    gap_y    = float(cfg.get("gap_mm", {}).get("y", 12.0))
    min_w    = float(cfg.get("min_sheet_box_mm", {}).get("w", 45.0))
    min_h    = float(cfg.get("min_sheet_box_mm", {}).get("h", 35.0))
    max_w    = float(cfg.get("max_sheet_box_mm", {}).get("w", 95.0))
    max_h    = float(cfg.get("max_sheet_box_mm", {}).get("h", 70.0))
    target_a = float(cfg.get("target_box_aspect", 1.35))

    ml = float(margin.get("left",   15.0))
    mr = float(margin.get("right",  15.0))
    mt = float(margin.get("top",    15.0))
    mb = float(margin.get("bottom", 12.0))
    tb_h = float(tb_res.get("height_mm", 30.0))

    # Box-height floor so the busiest block's sheet pins fit INSIDE the box
    # (see docstring). Geometry is shared with _emit_sheet_box via the same
    # `sheet_pin` config keys so the height that's reserved here matches the
    # pitch the pins are actually drawn at.
    sp = cfg.get("sheet_pin", {}) or {}
    pin_top_gap = float(sp.get("top_gap_mm", 5.08))
    pin_pitch   = float(sp.get("pitch_mm", 2.54))
    pin_bot_pad = float(sp.get("bottom_pad_mm", 2.54))
    two_edge    = bool(sp.get("distribute_both_edges", True))
    max_pins = max(pin_counts) if pin_counts else 0
    per_edge = math.ceil(max_pins / 2.0) if two_edge else max_pins
    pin_req_h = (pin_top_gap + per_edge * pin_pitch + pin_bot_pad
                 if per_edge > 0 else 0.0)
    # Raise BOTH the floor (so a grid whose cells are too short for the pins
    # is rejected → fewer rows / bigger page) AND the cap (so the aspect clamp
    # can't shrink the box back below what the pins need).
    min_h = max(min_h, pin_req_h)
    max_h = max(max_h, pin_req_h)

    for paper_name in priority:
        p = papers.get(paper_name)
        if not p:
            continue
        page_w = float(p.get("w_mm", 297.0))
        page_h = float(p.get("h_mm", 210.0))
        usable_w = page_w - ml - mr
        usable_h = page_h - mt - mb - tb_h
        if usable_w <= min_w or usable_h <= min_h:
            continue

        best = None     # (score, cols, rows, box_w, box_h)
        # Try every grid up to N cols — best aspect-fit wins.
        for cols in range(1, n_blocks + 1):
            rows = (n_blocks + cols - 1) // cols
            cell_w = (usable_w - (cols - 1) * gap_x) / cols
            cell_h = (usable_h - (rows - 1) * gap_y) / rows
            if cell_w < min_w or cell_h < min_h:
                continue
            box_w = min(cell_w, max_w)
            box_h = min(cell_h, max_h)
            aspect = box_w / box_h if box_h > 0 else 0
            score = abs(aspect - target_a)
            if best is None or score < best[0]:
                best = (score, cols, rows, box_w, box_h)

        if best is None:
            continue  # No grid fits on this page — try next paper size.

        _, cols, rows, box_w, box_h = best
        # Centre the grid in the usable area so empty cells (if any) are
        # symmetrical rather than dangling at the right edge.
        total_w = cols * box_w + (cols - 1) * gap_x
        total_h = rows * box_h + (rows - 1) * gap_y
        start_x = ml + (usable_w - total_w) / 2.0
        start_y = mt + (usable_h - total_h) / 2.0

        positions: List[Tuple[float, float, float, float]] = []
        for i in range(n_blocks):
            c = i % cols
            r = i // cols
            bx = start_x + c * (box_w + gap_x)
            by = start_y + r * (box_h + gap_y)
            # Snap to KiCad grid (50-mil = 1.27 mm) so KiCad doesn't
            # mark the boxes as off-grid.
            bx = round(bx / GRID) * GRID
            by = round(by / GRID) * GRID
            positions.append((bx, by, box_w, box_h))

        return {"paper": paper_name, "positions": positions,
                 "cols": cols, "rows": rows,
                 "page_w": page_w, "page_h": page_h,
                 "usable_w": usable_w, "usable_h": usable_h}

    # No configured page accommodates N blocks. Fall back to the largest
    # priority page with min-sized boxes packed as a single row — the
    # boxes will overflow but at least the file is renderable so the
    # user can see + fix manually rather than the build failing.
    paper_name = priority[-1] if priority else "A4"
    p = papers.get(paper_name, {"w_mm": 594.0, "h_mm": 420.0})
    positions = []
    for i in range(n_blocks):
        positions.append((
            ml + i * (min_w + gap_x),
            mt,
            min_w, min_h,
        ))
    return {"paper": paper_name, "positions": positions,
             "cols": n_blocks, "rows": 1,
             "page_w": float(p.get("w_mm", 594.0)),
             "page_h": float(p.get("h_mm", 420.0)),
             "usable_w": float(p.get("w_mm", 594.0)) - ml - mr,
             "usable_h": float(p.get("h_mm", 420.0)) - mt - mb - tb_h}


_STROKE_TYPE_CHOICES = {"solid", "dash", "dot", "dash_dot", "default"}


def _block_style_for_index(block_name: str, index: int) -> Dict[str, Any]:
    """Read block_naming.json:rectangle_style + per-block colour
    overrides and return a flat dict the rectangle emitter uses.
    Pure JSON consumption --- adding a new block colour family is one
    JSON entry, zero code change.
    """
    cfg = _load_block_naming()
    style = cfg.get("rectangle_style") or {}
    blocks_map = cfg.get("blocks") or {}
    palette = cfg.get("default_palette") or []
    entry = blocks_map.get((block_name or "").upper()) or {}
    color = entry.get("color")
    if not color:
        if palette:
            color = palette[index % len(palette)]
        else:
            color = style.get("default_border_color", [80, 80, 200, 1.0])
    # Uniform block-box colour: when `rectangle_style.uniform_border_color`
    # is set, every block box (border + title) uses that single colour,
    # overriding per-block entries and the rotating palette. User wants all
    # block boxes one blue, matching the reference (dashed blue boxes), not
    # a red/orange/teal rainbow. Unset → legacy per-block colours.
    _uniform = style.get("uniform_border_color")
    if _uniform:
        color = _uniform
    border_type = str(style.get("border_type", "solid")).lower()
    if border_type not in _STROKE_TYPE_CHOICES:
        border_type = "solid"
    title_template = str(style.get(
        "title_prefix_template", "{index}. {block_name_upper}"))
    nm = (block_name or "")
    try:
        title_text = title_template.format(
            index=index,
            block_name_upper=nm.upper(),
            block_name_lower=nm.lower(),
            block_name_title=nm.replace("_", " ").title(),
        )
    except (KeyError, IndexError, ValueError):
        title_text = f"{index}. {nm.upper()}"
    return {
        "border_color": list(color),
        "border_width_mm": float(style.get("border_width_mm", 0.4064)),
        "border_type": border_type,
        "title_text": title_text,
        "title_font_size_mm": float(style.get("title_font_size_mm", 1.778)),
        "title_font_bold": bool(style.get("title_font_bold", True)),
        "title_offset_x_mm": float(style.get("title_offset_x_mm", 5.08)),
        "title_offset_y_mm": float(style.get("title_offset_y_mm", 4.0)),
        "title_justify": str(style.get("title_justify", "left bottom")),
        "fill": str(style.get("fill", "none")),
    }


def _color_token(rgba) -> str:
    """Build the KiCad `(color r g b a)` token. RGB are ints 0-255,
    alpha is a float 0.0-1.0. Tolerant of slightly off types."""
    try:
        r = int(round(float(rgba[0])))
        g = int(round(float(rgba[1])))
        b = int(round(float(rgba[2])))
        a = float(rgba[3]) if len(rgba) > 3 else 1.0
    except (TypeError, ValueError, IndexError):
        return "(color 80 80 200 1.0)"
    return f"(color {r} {g} {b} {a})"


def _emit_block_rectangle(name: str,
                            bbox: Tuple[float, float, float, float],
                            index: int = 1) -> str:
    """Draw a solid-bordered, per-block-coloured rectangle around a
    functional block + label it with `index. NAME` in the top-left
    corner. Colour, border width, border style, title format ---
    everything comes from `config/block_naming.json:rectangle_style`
    + per-block `color` overrides. Reproduces the boxed-block visual
    in the user's reference single-sheet schematic.
    """
    style = _block_style_for_index(name, index)
    x1, y1, x2, y2 = bbox
    color_tok = _color_token(style["border_color"])
    bold_tok = " bold" if style["title_font_bold"] else ""
    title_size = style["title_font_size_mm"]
    title_x = x1 + style["title_offset_x_mm"]
    title_y = y1 - style["title_offset_y_mm"]
    # KiCad text-effects schema: `color` lives INSIDE `(font ...)`,
    # NOT as a sibling of `(justify ...)`. Putting it after `(justify)`
    # made KiCad's parser bail with "Expecting font, justify, hide or
    # href. Got 'color'" --- exactly what the user hit on 2026-06-02.
    # Rectangle `(stroke ...)` accepts `(color ...)` as a direct child
    # (different schema), so the rectangle line stays unchanged.
    return (
        f'\t(rectangle (start {_num(x1)} {_num(y1)}) (end {_num(x2)} {_num(y2)})\n'
        f'\t\t(stroke (width {_num(style["border_width_mm"])}) '
        f'(type {style["border_type"]}) {color_tok})\n'
        f'\t\t(fill (type {style["fill"]}))\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
        f'\t(text {_qstr(style["title_text"])} (at {_num(title_x)} {_num(title_y)} 0)\n'
        f'\t\t(effects (font (size {_num(title_size)} {_num(title_size)}){bold_tok} '
        f'{color_tok}) (justify {style["title_justify"]}))\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )


def _block_bbox_for_components(placed: List["PlacedComp"],
                                refs: List[str],
                                pad: Optional[float] = None,
                                min_size: Optional[Tuple[float, float]] = None,
                                ) -> Optional[Tuple[float, float, float, float]]:
    """Union the outer bboxes of `refs` in the placed list, padded on
    each side. Reads padding + min-size from layout_config.json -> block_rect:
      - `padding_mm`           — breathing room around the tightest content
      - `min_size_mm.{w,h}`    — floor size so a sparse block (few
                                  components) still looks proportional next
                                  to a dense MCU block

    Caller may override `pad` / `min_size` to bypass the JSON config.
    Pure data-driven — no per-circuit logic.
    """
    cfg = _load_layout_config().get("block_rect", {}) or {}
    if pad is None:
        pad = float(cfg.get("padding_mm", 10.16))
    if min_size is None:
        ms = cfg.get("min_size_mm", {}) or {}
        min_size = (float(ms.get("w", 0.0)), float(ms.get("h", 0.0)))
    bbs = [_abs_outer_bbox(c) for c in placed if c.ref in refs]
    if not bbs:
        return None
    xs1 = min(b[0] for b in bbs) - pad
    ys1 = min(b[1] for b in bbs) - pad
    xs2 = max(b[2] for b in bbs) + pad
    ys2 = max(b[3] for b in bbs) + pad
    # Enforce minimum size by expanding around the centroid — keeps the
    # block centred on its component cluster rather than dangling off
    # the edge when a tiny block grows.
    cur_w = xs2 - xs1
    cur_h = ys2 - ys1
    min_w, min_h = min_size
    if cur_w < min_w:
        cx = (xs1 + xs2) / 2.0
        xs1 = cx - min_w / 2.0
        xs2 = cx + min_w / 2.0
    if cur_h < min_h:
        cy = (ys1 + ys2) / 2.0
        ys1 = cy - min_h / 2.0
        ys2 = cy + min_h / 2.0
    # Snap to grid
    return (_snap_grid((xs1, ys1)) + _snap_grid((xs2, ys2)))


def _resolve_block_rect_overlaps(
    boxes: List[Tuple[float, float, float, float]],
    contents: List[Tuple[float, float, float, float]],
    clearance: float = 1.27,
) -> List[Tuple[float, float, float, float]]:
    """Separate overlapping padded block rectangles so two functional
    blocks never draw on top of each other (reference rule: clean,
    non-overlapping block boxes).

    `boxes`    — the padded rects as drawn (from `_block_bbox_for_components`).
    `contents` — the SAME blocks' un-padded content bboxes (pad=0).

    Where two padded rects overlap but their *contents* have a clear gap
    on one axis, both rects are clipped to the midline of that gap (minus
    `clearance`). Contents are never clipped — if two blocks' components
    genuinely interleave (overlap on both axes) the pair is left untouched,
    because that is a placement problem, not a box-drawing one. Purely
    geometric, no per-circuit logic, so it is safe for any block set.
    """
    out = [list(b) for b in boxes]
    half = clearance / 2.0
    for i in range(len(out)):
        for j in range(i + 1, len(out)):
            bi, bj = out[i], out[j]
            # No overlap → nothing to do.
            if (bi[2] <= bj[0] or bj[2] <= bi[0]
                    or bi[3] <= bj[1] or bj[3] <= bi[1]):
                continue
            ci, cj = contents[i], contents[j]
            if ci[3] <= cj[1]:          # i content sits ABOVE j content
                mid = (ci[3] + cj[1]) / 2.0
                bi[3], bj[1] = min(bi[3], mid - half), max(bj[1], mid + half)
            elif cj[3] <= ci[1]:        # j content sits ABOVE i content
                mid = (cj[3] + ci[1]) / 2.0
                bj[3], bi[1] = min(bj[3], mid - half), max(bi[1], mid + half)
            elif ci[2] <= cj[0]:        # i content LEFT of j content
                mid = (ci[2] + cj[0]) / 2.0
                bi[2], bj[0] = min(bi[2], mid - half), max(bj[0], mid + half)
            elif cj[2] <= ci[0]:        # j content LEFT of i content
                mid = (cj[2] + ci[0]) / 2.0
                bj[2], bi[0] = min(bj[2], mid - half), max(bi[0], mid + half)
            # else: contents overlap on both axes → leave as-is.
    return [tuple(_snap_grid((b[0], b[1])) + _snap_grid((b[2], b[3])))
            for b in out]


def _separate_overlapping_blocks(comps: List["PlacedComp"],
                                  ir: TopologyIR) -> int:
    """Push whole functional blocks apart when their COMPONENTS overlap.

    `_resolve_block_rect_overlaps` only separates block RECTANGLES and gives
    up when two blocks' components interleave on both axes (it never moves
    components). That is exactly the "block on top of block" case: a tall IC
    (e.g. ATmega) overflows its 110x85 zone cell into a stacked neighbour, so
    the content bboxes overlap and the boxes draw on top of each other.

    This pass fixes the placement instead of the drawing: it translates each
    overlapping block RIGIDLY (all its components by the same delta) along the
    axis of least penetration until every pair of block content bboxes has a
    clear `separation_gap_mm` between them. Iterative (force-directed style)
    so a 3-way pile-up relaxes over several passes. Grid-snapped so nothing
    leaves the 1.27 mm grid. Runs BEFORE de-overlap + re-centre, so those
    finish on an already-separated layout.

    Returns the number of blocks moved at least once. Config:
    `block_rect.separate_overlapping_blocks` (default true)."""
    cfg = _load_layout_config().get("block_rect", {}) or {}
    if not cfg.get("separate_overlapping_blocks", True):
        return 0
    groups: List[Tuple[Any, List["PlacedComp"]]] = []
    by_ref = {c.ref: c for c in comps}
    for blk in getattr(ir, "blocks", None) or []:
        if not _block_qualifies_for_rectangle(blk, ir):
            continue
        members = [by_ref[r] for r in blk.component_refs if r in by_ref]
        if members:
            groups.append((blk, members))
    if len(groups) < 2:
        return 0

    gap = float(cfg.get("separation_gap_mm", 22.0))
    max_iters = int(cfg.get("separation_max_iters", 40))
    half_gap = gap / 2.0
    moved: set = set()

    def _group_bbox(members: List["PlacedComp"]):
        bbs = [_abs_outer_bbox(c) for c in members]
        return (min(b[0] for b in bbs), min(b[1] for b in bbs),
                max(b[2] for b in bbs), max(b[3] for b in bbs))

    def _shift(members: List["PlacedComp"], dx: float, dy: float) -> None:
        for c in members:
            c.pos = (c.pos[0] + dx, c.pos[1] + dy)

    for _ in range(max_iters):
        bboxes = [_group_bbox(m) for (_b, m) in groups]
        any_overlap = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                bi, bj = bboxes[i], bboxes[j]
                # Penetration with the desired gap baked in: positive means
                # the two content boxes are closer than `gap` on that axis.
                pen_x = (min(bi[2], bj[2]) - max(bi[0], bj[0])) + gap
                pen_y = (min(bi[3], bj[3]) - max(bi[1], bj[1])) + gap
                if pen_x <= 0 or pen_y <= 0:
                    continue  # already clear (with gap) on at least one axis
                any_overlap = True
                ci_x = (bi[0] + bi[2]) / 2.0
                cj_x = (bj[0] + bj[2]) / 2.0
                ci_y = (bi[1] + bi[3]) / 2.0
                cj_y = (bj[1] + bj[3]) / 2.0
                if pen_x <= pen_y:
                    # separate horizontally — least penetration axis
                    step = _round_grid(pen_x / 2.0)
                    if step <= 0:
                        step = GRID
                    sign = 1.0 if ci_x >= cj_x else -1.0
                    _shift(groups[i][1],  sign * step, 0.0)
                    _shift(groups[j][1], -sign * step, 0.0)
                else:
                    step = _round_grid(pen_y / 2.0)
                    if step <= 0:
                        step = GRID
                    sign = 1.0 if ci_y >= cj_y else -1.0
                    _shift(groups[i][1], 0.0,  sign * step)
                    _shift(groups[j][1], 0.0, -sign * step)
                moved.add(i)
                moved.add(j)
                bboxes[i] = _group_bbox(groups[i][1])
                bboxes[j] = _group_bbox(groups[j][1])
        if not any_overlap:
            break
    return len(moved)


def _round_grid(v: float) -> float:
    """Snap a scalar distance to the 1.27 mm grid (>= 0)."""
    return round(v / GRID) * GRID


def _emit_no_connect(p: Tuple[float, float]) -> str:
    """`(no_connect ...)` flag at a pin tip — silences ERC's
    `pin_not_connected` for pins the architect knew to leave open."""
    return (
        f'\t(no_connect (at {_num(p[0])} {_num(p[1])})\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )


# ---------------------------------------------------------------------------
# Body-aware L-router
# ---------------------------------------------------------------------------

def _abs_outer_bbox(c: "PlacedComp") -> Tuple[float, float, float, float]:
    """Convert a placed component's local outer_bbox (Y-up) to absolute
    schematic Y-down coordinates, handling arbitrary rotation AND
    off-centre bboxes (e.g. LED with bbox (-4.572, -2.286, 1.27, 1.27))."""
    x1, y1, x2, y2 = c.geom.outer_bbox  # local Y-up
    # Build the 4 corners in local Y-up
    corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
    # Y-flip (local Y-up → local schematic Y-down)
    corners = [(x, -y) for x, y in corners]
    # Rotate by component rotation (CCW in schematic-screen coords)
    rad = math.radians(c.rotation)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r) for x, y in corners]
    # Translate to component absolute position
    corners = [(c.pos[0] + x, c.pos[1] + y) for x, y in corners]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _abs_bbox_with_fields(c: "PlacedComp") -> Tuple[float, float, float, float]:
    """`_abs_outer_bbox` UNION the Reference/Value field-text reach on the
    edge(s) that actually carry it (per `_place_fields`).

    Why this exists: the keep-inside-sheet net measured only the body+pin
    bbox (`_abs_outer_bbox`), so a part whose BODY was in-bounds but whose
    "R12 / 10kΩ" text spilled across the red border or onto the bottom-right
    title block still passed the check — the visible overflow the user hit on
    the single-sheet-with-blocks path. Reuses the already-tested
    `_field_text_bbox` (vertical 2-pin passive → side text on +X; everything
    else → name-above / value-below on ±Y) so the measurement matches the
    field placement exactly. Falls back to the body bbox if the geometry has
    no resolvable field side. Implements [[feedback_layout_within_sheet]]."""
    base = _abs_outer_bbox(c)
    try:
        ft = _field_text_bbox(c.geom, c.pos, c.rotation,
                              side_allow=_est_field_protrusion(c))
    except Exception:
        return base
    return (min(base[0], ft[0]), min(base[1], ft[1]),
            max(base[2], ft[2]), max(base[3], ft[3]))


def _seg_intersects_rect(a: Tuple[float, float], b: Tuple[float, float],
                          rect: Tuple[float, float, float, float],
                          margin: float = 0.5) -> bool:
    """True if axis-aligned segment a→b passes strictly through the rect
    interior (with a small margin so pin-tip ENDPOINTS don't false-positive
    against a component's own bbox)."""
    x1, y1, x2, y2 = rect
    x1, x2 = x1 + margin, x2 - margin
    y1, y2 = y1 + margin, y2 - margin
    if x1 >= x2 or y1 >= y2:
        return False
    # Vertical segment
    if abs(a[0] - b[0]) < 0.01:
        if not (x1 < a[0] < x2):
            return False
        lo, hi = sorted((a[1], b[1]))
        return not (hi <= y1 or lo >= y2)
    # Horizontal segment
    if abs(a[1] - b[1]) < 0.01:
        if not (y1 < a[1] < y2):
            return False
        lo, hi = sorted((a[0], b[0]))
        return not (hi <= x1 or lo >= x2)
    # Non-axis-aligned — shouldn't happen in Manhattan routing
    return False


def _cluster_power_pins(positions, radius: float = 12.7):
    """Group power-net pin positions into clusters where any two pins
    within `radius` mm belong to the same cluster (single-linkage).
    Returns a list of clusters; each cluster is a list of (x, y, rot)
    tuples. Used so a +V or GND net with many pins gets ONE port
    per group of physically-close pins, not one per pin."""
    remaining = list(positions)
    clusters = []
    while remaining:
        seed = remaining.pop(0)
        cluster = [seed]
        changed = True
        while changed:
            changed = False
            for p in remaining[:]:
                if any(math.hypot(p[0] - q[0], p[1] - q[1]) <= radius
                       for q in cluster):
                    cluster.append(p)
                    remaining.remove(p)
                    changed = True
        clusters.append(cluster)
    return clusters


def _find_blocking_obstacle(path, obstacles, exclude=None):
    """Return the first obstacle bbox a Manhattan path passes through,
    or None. `exclude` is a list of bboxes to ignore (the endpoint
    components themselves)."""
    excl = exclude or []
    for i in range(len(path) - 1):
        for rect in obstacles:
            if rect in excl:
                continue
            if _seg_intersects_rect(path[i], path[i + 1], rect):
                return rect
    return None


def _seg_crosses_seg(a1: Tuple[float, float], a2: Tuple[float, float],
                       b1: Tuple[float, float], b2: Tuple[float, float],
                       margin: float = 0.01) -> bool:
    """Two Manhattan segments cross iff one is horizontal, the other
    vertical, and their projections intersect within both segment
    interiors. Same-orientation segments (overlap case) handled by
    junction logic, not this geometry check."""
    a_horiz = abs(a1[1] - a2[1]) < margin and abs(a1[0] - a2[0]) > margin
    a_vert  = abs(a1[0] - a2[0]) < margin and abs(a1[1] - a2[1]) > margin
    b_horiz = abs(b1[1] - b2[1]) < margin and abs(b1[0] - b2[0]) > margin
    b_vert  = abs(b1[0] - b2[0]) < margin and abs(b1[1] - b2[1]) > margin
    if a_horiz and b_vert:
        y_h = a1[1]
        x_v = b1[0]
        a_xmin, a_xmax = min(a1[0], a2[0]), max(a1[0], a2[0])
        b_ymin, b_ymax = min(b1[1], b2[1]), max(b1[1], b2[1])
        return (a_xmin + margin < x_v < a_xmax - margin
                and b_ymin + margin < y_h < b_ymax - margin)
    if a_vert and b_horiz:
        return _seg_crosses_seg(b1, b2, a1, a2, margin)
    return False


def _count_path_crossings(path: List[Tuple[float, float]],
                            existing_wires: Optional[List[Tuple[Tuple[float, float], Tuple[float, float]]]],
                            ) -> int:
    """Count how many existing wire segments the candidate Manhattan
    path crosses. Lower is better — used to rank L1 vs L2 vs Z when
    the router has otherwise-equal options."""
    if not existing_wires:
        return 0
    n = 0
    for i in range(len(path) - 1):
        a1, a2 = path[i], path[i + 1]
        for (b1, b2) in existing_wires:
            if _seg_crosses_seg(a1, a2, b1, b2):
                n += 1
    return n


def _seg_overlaps_seg(a1: Tuple[float, float], a2: Tuple[float, float],
                        b1: Tuple[float, float], b2: Tuple[float, float],
                        margin: float = 0.01) -> bool:
    """Two Manhattan segments OVERLAP iff they are collinear (same
    horizontal row or same vertical column) and their 1-D ranges share
    more than a single point. This is the case `_seg_crosses_seg`
    deliberately skips. A collinear overlap between segments of DIFFERENT
    nets is an electrical SHORT (KiCad merges collinear touching wires),
    so the router must avoid it — not merely de-prioritise it like a
    perpendicular crossing."""
    a_horiz = abs(a1[1] - a2[1]) < margin
    a_vert = abs(a1[0] - a2[0]) < margin
    b_horiz = abs(b1[1] - b2[1]) < margin
    b_vert = abs(b1[0] - b2[0]) < margin
    if a_horiz and b_horiz and abs(a1[1] - b1[1]) < margin:
        lo = max(min(a1[0], a2[0]), min(b1[0], b2[0]))
        hi = min(max(a1[0], a2[0]), max(b1[0], b2[0]))
        return hi - lo > margin
    if a_vert and b_vert and abs(a1[0] - b1[0]) < margin:
        lo = max(min(a1[1], a2[1]), min(b1[1], b2[1]))
        hi = min(max(a1[1], a2[1]), max(b1[1], b2[1]))
        return hi - lo > margin
    return False


def _count_path_overlaps(path: List[Tuple[float, float]],
                          existing_wires: Optional[List[Tuple[Tuple[float, float], Tuple[float, float]]]],
                          ) -> int:
    """Count how many existing (other-net) wire segments the candidate
    path collinearly OVERLAPS — i.e. would short. Lower is better, and
    must be minimised BEFORE mere crossings."""
    if not existing_wires:
        return 0
    n = 0
    for i in range(len(path) - 1):
        a1, a2 = path[i], path[i + 1]
        for (b1, b2) in existing_wires:
            if _seg_overlaps_seg(a1, a2, b1, b2):
                n += 1
    return n


def _pt_on_seg_interior(px: float, py: float,
                         a: Tuple[float, float], b: Tuple[float, float],
                         tol: float = 0.01) -> bool:
    """True iff point (px,py) lies STRICTLY BETWEEN the endpoints of the
    Manhattan segment a->b (on the line, not at either end). A foreign
    pin/port tip on a wire's interior is exactly what KiCad mid-span-bonds
    into the net — an electrical SHORT — so the router must avoid it.
    Endpoint-exclusive by design: a pin AT an endpoint is the legitimate
    connection. Segments here are always H or V (engine emits Manhattan
    only)."""
    ax, ay = a
    bx, by = b
    if abs(ay - by) < tol and abs(py - ay) < tol:        # horizontal seg
        lo, hi = (ax, bx) if ax <= bx else (bx, ax)
        return lo + tol < px < hi - tol
    if abs(ax - bx) < tol and abs(px - ax) < tol:        # vertical seg
        lo, hi = (ay, by) if ay <= by else (by, ay)
        return lo + tol < py < hi - tol
    return False


def _route_l_aware(p1: Tuple[float, float], p2: Tuple[float, float],
                    obstacles: List[Tuple[float, float, float, float]],
                    own_bboxes: Optional[List] = None,
                    existing_wires: Optional[List[Tuple[Tuple[float, float], Tuple[float, float]]]] = None,
                    foreign_pts: Optional[List[Tuple[float, float]]] = None,
                    ) -> List[Tuple[float, float]]:
    """Body-aware + wire-crossing-aware Manhattan router.
      1. Try L1 (H then V) — return if clear AND no other-net wire crosses.
      2. Try L2 (V then H) — same.
      3. If both clear of obstacles but one has fewer wire crossings,
         prefer that one.
      4. Both obstacle-blocked → find the FIRST blocking obstacle and
         route a Z-shape around its nearer edge.
      `own_bboxes` lists endpoint components' bboxes so they don't
      false-positive as blockers (the wire MUST exit/enter those).
      `existing_wires` is the list of segments already emitted by
      OTHER nets — the router avoids crossing them when possible.
    """
    own = own_bboxes or []
    # JSON knob: routing.avoid_wire_crossings. When false, ignore the
    # wire-crossing rank and use the legacy "first L that clears
    # obstacles wins" behaviour.
    _routing_knobs = _load_layout_config().get("routing", {})
    _avoid_wire_cross = bool(_routing_knobs.get("avoid_wire_crossings", True))
    # A collinear overlap with another net's wire is a SHORT, not a soft
    # crossing — avoid it first. Default on; flip off for byte-identical
    # legacy routing.
    _avoid_wire_overlap = bool(_routing_knobs.get("avoid_wire_overlaps", True))

    def _overlaps_other(path) -> bool:
        return (_avoid_wire_overlap
                and _count_path_overlaps(path, existing_wires) > 0)

    def _bend_inside_any(bend, bboxes, margin=0.01):
        """A bend point INSIDE any bbox (own or other) is a body-pierce
        even if the segment endpoints are at legal pin tips. The router
        must reject these. eps=0.01 is tiny — we want only strict
        interior points to count, edge-touching is fine."""
        bx, by = bend
        for r in bboxes:
            x1, y1, x2, y2 = r
            if (x1 + margin < bx < x2 - margin
                    and y1 + margin < by < y2 - margin):
                return True
        return False

    def _seg_pierces_own(a, b, bboxes, margin=0.01):
        """A wire segment may TOUCH its own (endpoint-owning) bbox at
        the pin tip, but it must not pass THROUGH that bbox to exit the
        opposite side.

        Proper segment-vs-rectangle interior overlap test (Manhattan
        segments only — diagonals don't happen in this codebase). The
        midpoint test that used to live here was insufficient: a long
        horizontal wire from a left-side pin (x = bbox.left) to a
        component WAY past the right edge has its midpoint outside
        the bbox but still crosses the interior. That's the NE555 bug
        the user spotted on 2026-05-26."""
        for r in bboxes:
            x1, y1, x2, y2 = r
            # Shrink the rect by `margin` so a pin-tip endpoint on the
            # edge counts as a touch, not a pierce.
            sx1, sy1, sx2, sy2 = (x1 + margin, y1 + margin,
                                    x2 - margin, y2 - margin)
            if sx1 >= sx2 or sy1 >= sy2:
                continue
            # Horizontal segment
            if abs(a[1] - b[1]) < 0.001:
                y = a[1]
                if not (sy1 < y < sy2):
                    continue
                lo, hi = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
                # Pierces if the segment's x-range overlaps the
                # rectangle's x-range with non-zero interior.
                if lo < sx2 and hi > sx1:
                    return True
            # Vertical segment
            elif abs(a[0] - b[0]) < 0.001:
                x = a[0]
                if not (sx1 < x < sx2):
                    continue
                lo, hi = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
                if lo < sy2 and hi > sy1:
                    return True
            # Diagonal (should never happen — Manhattan only). Fall
            # back to midpoint to avoid silently passing.
            else:
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                if sx1 < mx < sx2 and sy1 < my < sy2:
                    return True
        return False

    def _path_pierces_own(path, bboxes):
        for i in range(len(path) - 1):
            if _seg_pierces_own(path[i], path[i + 1], bboxes):
                return True
        return False

    def _hits_foreign(path) -> bool:
        """True if any segment of `path` runs THROUGH a foreign pin/port
        tip (a point belonging to a DIFFERENT net). KiCad mid-span-bonds a
        pin lying on a wire's interior, so such a path is an electrical
        SHORT — exactly the +12V/GND and XTAL1/XTAL2 shorts. Endpoint-
        exclusive, so a pin AT p1/p2 (the legit connection) never counts."""
        if not foreign_pts:
            return False
        for i in range(len(path) - 1):
            a, b = path[i], path[i + 1]
            for fp in foreign_pts:
                if _pt_on_seg_interior(fp[0], fp[1], a, b):
                    return True
        return False

    # Straight-line shortcut: still must check obstacles AND that the
    # segment doesn't pass THROUGH its own endpoint-owner's body. A
    # horizontal or vertical wire that passes through a component body
    # is exactly the bug this function exists to prevent.
    if abs(p1[0] - p2[0]) < 0.01 or abs(p1[1] - p2[1]) < 0.01:
        straight = [p1, p2]
        if (_find_blocking_obstacle(straight, obstacles, exclude=own) is None
                and not _path_pierces_own(straight, own)
                and not _overlaps_other(straight)
                and not _hits_foreign(straight)):
            return straight
        # Straight blocked (obstacle, own-body pierce, or it would lie on
        # top of another net's wire = short) — fall through to L/Z below.
    l1 = [p1, (p2[0], p1[1]), p2]
    l2 = [p1, (p1[0], p2[1]), p2]
    all_boxes = list(obstacles) + own
    l1_clear = (_find_blocking_obstacle(l1, obstacles, exclude=own) is None
                 and not _bend_inside_any(l1[1], all_boxes)
                 and not _path_pierces_own(l1, own)
                 and not _hits_foreign(l1))
    l2_clear = (_find_blocking_obstacle(l2, obstacles, exclude=own) is None
                 and not _bend_inside_any(l2[1], all_boxes)
                 and not _path_pierces_own(l2, own)
                 and not _hits_foreign(l2))
    # When both L variants clear the bbox obstacles, prefer the one
    # that crosses FEWER other-net wires. When only one clears, return
    # that one (even if it has crossings — crossings are a soft
    # preference, body-pierce is a hard rule).
    if l1_clear and l2_clear:
        if _avoid_wire_cross or _avoid_wire_overlap:
            # Rank by (overlaps, crossings): a collinear overlap with
            # another net is a SHORT and must be avoided before mere
            # perpendicular crossings.
            o1 = _count_path_overlaps(l1, existing_wires) if _avoid_wire_overlap else 0
            o2 = _count_path_overlaps(l2, existing_wires) if _avoid_wire_overlap else 0
            c1 = _count_path_crossings(l1, existing_wires) if _avoid_wire_cross else 0
            c2 = _count_path_crossings(l2, existing_wires) if _avoid_wire_cross else 0
            return l1 if (o1, c1) <= (o2, c2) else l2
        return l1
    if l1_clear:
        return l1
    if l2_clear:
        return l2
    # Both Ls pierce something. Find the blocker for L1 and detour around it.
    blocker = (_find_blocking_obstacle(l1, obstacles, exclude=own)
               or _find_blocking_obstacle(l2, obstacles, exclude=own))
    if blocker is None:
        # Both Ls were rejected purely because they pass through one of
        # the endpoint-owner bodies (own-pierce). Pick the FIRST own
        # bbox a segment crosses and use it as the obstacle to detour
        # around — falling back to L1 here would emit the very wire
        # we just rejected.
        for r in own:
            if _seg_pierces_own(l1[0], l1[1], [r]) \
                    or _seg_pierces_own(l1[1], l1[2], [r]) \
                    or _seg_pierces_own(l2[0], l2[1], [r]) \
                    or _seg_pierces_own(l2[1], l2[2], [r]):
                blocker = r
                break
    if blocker is None:
        # Genuinely nothing to route around — fall through to the
        # wider-Z wraparound below, then to the "" empty-return so
        # the caller switches to labels.
        blocker = own[0] if own else None
    if blocker is None:
        return []
    bx1, by1, bx2, by2 = blocker
    clearance = _routing_cfg()["obstacle_detour_clearance_mm"]
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    def _path_clean(path):
        """Path is clean iff no segment hits a non-own obstacle AND
        no BEND point sits inside ANY bbox (own or other) AND no
        SEGMENT passes through an own bbox's interior. The bend-
        inside-own case is the LM386 / NE555 / MCP1700 bug — wire
        terminates at a legal pin tip but its L-bend punches through
        the same component's body. The segment-pierces-own case is
        the STM32+OLED bug — wire endpoints touch U3 at the pin tip
        but the straight segment crosses U3's body to exit the
        opposite side."""
        if _find_blocking_obstacle(path, obstacles, exclude=own) is not None:
            return False
        for bend in path[1:-1]:
            if _bend_inside_any(bend, all_boxes):
                return False
        if _path_pierces_own(path, own):
            return False
        if _hits_foreign(path):
            return False
        return True

    # Detour points are derived from bbox edges (which may land at any
    # mm value, not on the 1.27 mm grid) plus a clearance constant.
    # KiCad's ERC fires `endpoint_off_grid` on any wire endpoint that's
    # not on the 1.27 mm grid, AND off-grid endpoints fail to bond to
    # on-grid pins electrically — the wire SHOWS but doesn't connect.
    # Snapping detour coords to grid silences the warning AND makes
    # the routed wire actually conduct.
    def _snap_y(y: float) -> float:
        return round(y / GRID) * GRID
    def _snap_x(x: float) -> float:
        return round(x / GRID) * GRID
    # Rank detour candidates by (overlaps, length): a detour line that
    # lies ON another net's wire is a SHORT — pick the overlap-free side
    # even if it is the longer detour. This is what stops two power buses
    # (e.g. +3V3 and GND around a column of decoupling caps) from both
    # detouring to the SAME side and merging into one net.
    def _ov(path) -> int:
        return _count_path_overlaps(path, existing_wires) if _avoid_wire_overlap else 0
    if abs(dx) >= abs(dy):
        above_y = _snap_y(by1 - clearance)
        below_y = _snap_y(by2 + clearance)
        candidates = []
        for detour_y in (above_y, below_y):
            path = [p1, (p1[0], detour_y), (p2[0], detour_y), p2]
            if _path_clean(path):
                candidates.append((_ov(path),
                                   abs(detour_y - p1[1]) + abs(detour_y - p2[1]),
                                   path))
        if candidates:
            candidates.sort(key=lambda t: (t[0], t[1]))
            return candidates[0][2]
    else:
        left_x = _snap_x(bx1 - clearance)
        right_x = _snap_x(bx2 + clearance)
        candidates = []
        for detour_x in (left_x, right_x):
            path = [p1, (detour_x, p1[1]), (detour_x, p2[1]), p2]
            if _path_clean(path):
                candidates.append((_ov(path),
                                   abs(detour_x - p1[0]) + abs(detour_x - p2[0]),
                                   path))
        if candidates:
            candidates.sort(key=lambda t: (t[0], t[1]))
            return candidates[0][2]
    # No L or Z detour worked. Try a wider Z that wraps around any
    # endpoint's body so the bend point sits outside. Same grid-snap
    # treatment so the wraparound doesn't itself land off-grid.
    for r in own:
        ox1, oy1, ox2, oy2 = r
        for clr in (clearance, 2 * clearance, 4 * clearance):
            for detour_x in (_snap_x(ox1 - clr), _snap_x(ox2 + clr)):
                path = [p1, (detour_x, p1[1]), (detour_x, p2[1]), p2]
                if _path_clean(path):
                    return path
            for detour_y in (_snap_y(oy1 - clr), _snap_y(oy2 + clr)):
                path = [p1, (p1[0], detour_y), (p2[0], detour_y), p2]
                if _path_clean(path):
                    return path
    # Last resort — NO clean path exists. Return an empty list so the
    # caller knows to SKIP this wire and rely on labels at each pin
    # instead. A wire that pierces a component body is unacceptable
    # per the professional convention (KLC, IEEE 315): wires connect
    # pin tips only, never cross over an IC / passive body. Labels at
    # both ends bond the pins by name with no visual conflict.
    #
    # Diagnostic: emit a debug log so the user can see WHICH wire was
    # dropped (the "wire shortage" symptom — silent drops left users
    # confused why some pin connections were missing). Gated by
    # ENVIL_ROUTER_DEBUG env var so production runs stay quiet.
    if os.environ.get("ENVIL_ROUTER_DEBUG"):
        import sys
        sys.stderr.write(
            f"[router] dropped wire {p1} -> {p2}: "
            f"no clean path past {len(obstacles)} obstacles, "
            f"{len(own)} own-bbox(es); caller will fall back to labels\n"
        )
    return []


def _emit_lib_symbols(comps: List[PlacedComp],
                       power_rails: Optional[set] = None) -> str:
    """Inline every used symbol — KiCad 9 requires this.
    Also inline any power rail symbols (power:+5V, power:GND, ...) used
    by the schematic. KiCad eeschema does NOT auto-resolve lib_id at
    open time; missing entries make the symbol show as a placeholder."""
    seen: Dict[str, list] = {}
    for c in comps:
        if c.lib_id not in seen:
            sym = list(c.geom.raw_symbol_sexpr)
            # Use the GEOM's resolved lib_id for the outer symbol name.
            # If the IR asked for `Connector:Conn_01x02` but fuzzy
            # resolved to `Connector:Conn_01x02_Pin`, the geom holds
            # the inner subunit naming `Conn_01x02_Pin_1_1` — we MUST
            # keep the outer name aligned with that prefix or kicad-cli
            # rejects the file as malformed. Also update c.lib_id so
            # the instance `(lib_id ...)` ref matches the lib_symbols
            # entry.
            resolved = getattr(c.geom, "lib_id", c.lib_id) or c.lib_id
            sym[1] = resolved
            seen[resolved] = sym
            c.lib_id = resolved
    # Add power port symbols if requested
    if power_rails:
        for rail in power_rails:
            lib_id = f"power:{rail}"
            if lib_id in seen:
                continue
            try:
                g = load_symbol(lib_id)
            except ValueError:
                continue   # unknown rail name — fall back to label
            sym = list(g.raw_symbol_sexpr)
            sym[1] = lib_id
            seen[lib_id] = sym
    # PWR_FLAG: prefer the user's installed power:PWR_FLAG (byte-
    # identical so KiCad doesn't fire 'Symbol doesn't match copy in
    # library' warnings on every flag). Fall back to the portable
    # hand-rolled block only when the user's sym-lib-table doesn't
    # expose it — keeps the .kicad_sch self-contained.
    flag_block_str: Optional[str] = None
    if power_rails:
        try:
            g_flag = load_symbol("power:PWR_FLAG")
            flag_sym = list(g_flag.raw_symbol_sexpr)
            flag_sym[1] = "power:PWR_FLAG"
            seen["power:PWR_FLAG"] = flag_sym
        except ValueError:
            flag_block_str = _power_flag_lib_block()

    if not seen and not power_rails:
        return '\t(lib_symbols)\n'
    out = '\t(lib_symbols\n'
    for lib_id, sym in seen.items():
        out += '\t\t' + _atomize(sym) + '\n'
    if flag_block_str is not None:
        out += flag_block_str
    out += '\t)\n'
    return out


def _normalize_text_rot(rot: float) -> float:
    """Text on schematics never reads upside-down. Clamp rotation to
    0° or 90° — eeschema follows this convention. 180°→0°, 270°→90°."""
    r = int(round(rot)) % 360
    if r in (180, 270):
        return float(r - 180)
    return float(r)


def _transform_offset(off, comp_x: float, comp_y: float,
                       comp_rot: float) -> Tuple[float, float, float]:
    """Apply Y-flip + component rotation to a local property offset.
    Returns the property's absolute (x, y, text_rotation). Text
    rotation is normalised to 0° or 90° (never upside-down)."""
    # Local Y-up → schematic Y-down
    lx, ly = off.x, -off.y
    rad = math.radians(comp_rot)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    ax = comp_x + lx * cos_r - ly * sin_r
    ay = comp_y + lx * sin_r + ly * cos_r
    text_rot = _normalize_text_rot(off.rotation + comp_rot)
    return (ax, ay, text_rot)


def _field_anchor_sides(c: "PlacedComp") -> Tuple[bool, bool]:
    """Return ``(has_left_right_pins, has_top_bottom_pins)`` for a placed
    component in its absolute schematic orientation.

    Reference/Value text must sit on the body edge that carries NO pins,
    so it never lands on a pin's through-wire (reference-image rule:
    "name above, value below; keep wires short and straight"). A
    horizontal resistor has pins left+right → its top+bottom edges are
    free; a vertical resistor has pins top+bottom → its left+right edges
    are free."""
    return _pin_sides(c.geom, c.pos, c.rotation)


def _edge_pin_columns(c: "PlacedComp", side_want: str) -> List[float]:
    """Absolute X of every pin on the named edge (``top`` | ``bottom``) of a
    placed component. A pin on that edge drops a vertical wire/power-port stub
    straight out along its X — the column a centred field must not sit on."""
    cols: List[float] = []
    for pin in c.geom.pins:
        px, _py, prot = place_pin(pin, c.pos[0], c.pos[1], c.rotation)
        if pin_side_from_rot(prot) == side_want:
            cols.append(px)
    return cols


def _shift_field_off_pin_columns(c: "PlacedComp", fx: float, text: str,
                                 side_want: str, cfg: dict) -> float:
    """Return an X for a body-centred Reference/Value so its text box clears
    every vertical pin-stub column on `side_want` (``top`` for the Reference
    above the body, ``bottom`` for the Value below it).

    Fixes the reference-image defect "value sits on the wire": an IC's power
    pin (e.g. AMS1117 GND, ESP32 GND) exits the bottom edge near body-centre
    and drops a power-port stub straight down THROUGH the centred Value text.
    The field is nudged horizontally — in 50-mil steps, toward the side with
    the shorter shift — until its estimated text box clears the stub. If no
    bounded shift clears it (pins span both sides wider than the text), the
    original centred X is returned unchanged (no worse than before, and
    `(fields_autoplaced yes)` lets eeschema refine on load)."""
    cols = _edge_pin_columns(c, side_want)
    if not cols or not str(text):
        return fx
    size = float(cfg.get("font_size_mm", 1.27))
    clr = float(cfg.get("pin_column_clearance_mm", 0.64))
    # Same width model as lint/context.py text_bbox so detection matches render.
    half_w = max(len(str(text)) * size * 0.7 / 2.0, size * 0.5)

    def clears(testx: float) -> bool:
        lo, hi = testx - half_w - clr, testx + half_w + clr
        return not any(lo <= px <= hi for px in cols)

    if clears(fx):
        return fx
    best: Optional[Tuple[float, float]] = None
    for sign in (1.0, -1.0):
        cand = fx
        for _ in range(int(cfg.get("pin_column_max_steps", 24))):
            cand += sign * GRID
            if clears(cand):
                if best is None or abs(cand - fx) < best[0]:
                    best = (abs(cand - fx), cand)
                break
    return best[1] if best is not None else fx


def _place_fields(c: "PlacedComp") -> Tuple[float, float, float, str,
                                            float, float, float, str]:
    """Absolute ``(x, y, rot, justify)`` for the Reference and Value text
    of a placed component, following the reference-image convention
    ("name above, value below; keep text off the wire"):

      * Pins on LEFT/RIGHT (signal flow left→right: ICs, regulators,
        diodes, horizontal passives) → Reference centred ABOVE the body,
        Value centred BELOW it. The free top/bottom edges carry the text.
      * Pins only on TOP/BOTTOM (vertical 2-pin passives — R, C, L drawn
        vertically) → Reference + Value stacked to the RIGHT of the body,
        left-justified, clear of the vertical through-wire.
      * Anything else (genuine 4-sided IC / unresolved) → name above,
        value below — the safe default.

    JSON-gated by ``text_placement``. When disabled it returns the symbol
    library's own offsets unchanged, so legacy output is byte-stable
    (see [feedback_non_breaking_changes]). ``justify`` is "" (KiCad's
    centred default) except for side placement, which needs a left
    anchor so the text grows away from the body."""
    rx, ry, rrot = _transform_offset(c.geom.ref_offset, c.pos[0], c.pos[1], c.rotation)
    vx, vy, vrot = _transform_offset(c.geom.val_offset, c.pos[0], c.pos[1], c.rotation)
    rjust = vjust = ""
    tp = _load_layout_config().get("text_placement", {}) or {}
    if not tp.get("enabled", True):
        return rx, ry, rrot, rjust, vx, vy, vrot, vjust
    gap = float(tp.get("gap_mm", 1.27))
    line_gap = float(tp.get("line_gap_mm", 1.27))
    side_ok = bool(tp.get("vertical_side_placement", True))
    bx1, by1, bx2, by2 = _abs_outer_bbox(c)
    cx = (bx1 + bx2) / 2.0
    cy = (by1 + by2) / 2.0
    has_lr, has_tb = _field_anchor_sides(c)
    if has_tb and not has_lr and side_ok:
        # Vertical 2-pin part: free left/right edges. Stack both fields
        # to the RIGHT, reading horizontally, off the through-wire.
        rx, ry = _snap_grid((bx2 + gap, cy - line_gap)); rrot = 0.0; rjust = "left"
        vx, vy = _snap_grid((bx2 + gap, cy + line_gap)); vrot = 0.0; vjust = "left"
    elif has_lr or has_tb:
        # Pins on the sides (or a true 4-sided IC): name above, value below.
        rx, ry = _snap_grid((cx, by1 - gap)); rrot = 0.0
        vx, vy = _snap_grid((cx, by2 + gap)); vrot = 0.0
        # Keep the centred text off any vertical pin-stub column (an IC power
        # pin exiting top/bottom drops a wire straight through the field —
        # the "value on the wire" defect). Gated; off → centred as before.
        if bool(tp.get("avoid_pin_columns", True)):
            rx = _round_grid(_shift_field_off_pin_columns(c, rx, c.ref, "top", tp))
            vx = _round_grid(_shift_field_off_pin_columns(c, vx, c.value, "bottom", tp))
    # else: no pins resolved → keep the library's own offsets.
    return rx, ry, rrot, rjust, vx, vy, vrot, vjust


def _field_effects(just: str) -> str:
    """`(effects ...)` line for a Reference/Value property — 50-mil font
    per KLC S3.2, with an optional justify for side-placed text."""
    j = f" (justify {just})" if just else ""
    return f'\t\t\t(effects (font (size 1.27 1.27)){j})'


def _emit_symbol_instance(c: PlacedComp, file_uuid: str) -> str:
    rx, ry, rrot, rjust, vx, vy, vrot, vjust = _place_fields(c)
    # Dynamic footprint: IR-supplied value wins (apply_ops / explicit
    # IR), otherwise fall back to the JSON resolver (lib_id map +
    # symbol's own default). Keeps the schematic Update-PCB-ready
    # without per-build manual footprint assignment.
    footprint = c.footprint or _resolve_default_footprint(c.lib_id, c.geom)
    # BOM metadata: pull description / datasheet / manufacturer / MPN
    # from parts_database.json + the lib_symbol's own properties so
    # kicad-cli sch export bom --fields finds real values, not blanks.
    meta = _resolve_part_metadata(c.lib_id, c.value, c.geom)
    # (fields_autoplaced yes) tells eeschema to re-auto-place the
    # Reference + Value text the next time the symbol is rendered. It
    # picks a side that doesn't collide with wires or the body — much
    # better than our static offset for rotated/dense layouts.
    lines = [
        '\t(symbol',
        f'\t\t(lib_id {_qstr(c.lib_id)})',
        f'\t\t(at {_num(c.pos[0])} {_num(c.pos[1])} {_num(c.rotation)})',
        '\t\t(unit 1)',
        '\t\t(exclude_from_sim no)',
        '\t\t(in_bom yes)',
        '\t\t(on_board yes)',
        '\t\t(dnp no)',
        '\t\t(fields_autoplaced yes)',
        f'\t\t(uuid {_qstr(c.uuid)})',
        f'\t\t(property "Reference" {_qstr(c.ref)}',
        f'\t\t\t(at {_num(rx)} {_num(ry)} {_num(rrot)})',
        _field_effects(rjust),
        '\t\t)',
        f'\t\t(property "Value" {_qstr(c.value)}',
        f'\t\t\t(at {_num(vx)} {_num(vy)} {_num(vrot)})',
        _field_effects(vjust),
        '\t\t)',
        f'\t\t(property "Footprint" {_qstr(footprint)}',
        f'\t\t\t(at {_num(vx)} {_num(vy + 2.54)} 0)',
        '\t\t\t(effects (font (size 1.27 1.27)) (hide yes))',
        '\t\t)',
        f'\t\t(property "Datasheet" {_qstr(meta["datasheet"])}',
        f'\t\t\t(at {_num(vx)} {_num(vy + 5.08)} 0)',
        '\t\t\t(effects (font (size 1.27 1.27)) (hide yes))',
        '\t\t)',
        f'\t\t(property "Description" {_qstr(meta["description"])}',
        f'\t\t\t(at {_num(vx)} {_num(vy + 7.62)} 0)',
        '\t\t\t(effects (font (size 1.27 1.27)) (hide yes))',
        '\t\t)',
        f'\t\t(property "Manufacturer" {_qstr(meta["manufacturer"])}',
        f'\t\t\t(at {_num(vx)} {_num(vy + 10.16)} 0)',
        '\t\t\t(effects (font (size 1.27 1.27)) (hide yes))',
        '\t\t)',
        f'\t\t(property "MPN" {_qstr(meta["mpn"])}',
        f'\t\t\t(at {_num(vx)} {_num(vy + 12.7)} 0)',
        '\t\t\t(effects (font (size 1.27 1.27)) (hide yes))',
        '\t\t)',
    ]
    for pin in c.geom.pins:
        pin_uuid = c.pin_uuids.get(pin.number, _u())
        lines.append(f'\t\t(pin {_qstr(pin.number)} (uuid {_qstr(pin_uuid)}))')
    lines += [
        '\t\t(instances',
        '\t\t\t(project ""',
        f'\t\t\t\t(path {_qstr("/" + file_uuid)}',
        f'\t\t\t\t\t(reference {_qstr(c.ref)})',
        '\t\t\t\t\t(unit 1)',
        '\t\t\t\t)',
        '\t\t\t)',
        '\t\t)',
        '\t)',
    ]
    return "\n".join(lines) + "\n"


def _emit_wire(p1, p2) -> str:
    return (
        f'\t(wire (pts (xy {_num(p1[0])} {_num(p1[1])}) '
        f'(xy {_num(p2[0])} {_num(p2[1])}))\n'
        f'\t\t(stroke (width 0) (type default))\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )


def _emit_junction(p) -> str:
    return (
        f'\t(junction (at {_num(p[0])} {_num(p[1])}) (diameter 0) '
        '(color 0 0 0 0)\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )


def _emit_power_port(rail: str, pos: Tuple[float, float],
                      file_uuid: str, ref: str,
                      decollide: bool = False) -> Optional[str]:
    """Emit a `(symbol (lib_id "power:<rail>") ...)` instance + a short
    wire stub from the satellite's pin to the power port. The stub keeps
    the power port at a clean position above/below the satellite without
    its arrow body landing inside the satellite's bbox.

    Layout:
        +V rails: power port placed 2.54 mm ABOVE the pin tip; arrow
                  body extends a further 2.54 mm up; text at +5 mm above.
        GND:      power port placed 2.54 mm BELOW the pin tip; triangle
                  body extends a further 2.54 mm down; text at -5 mm below.
        A 2.54 mm wire connects the satellite pin to the power port pin.
    """
    lib_id = f"power:{rail}"
    try:
        g = load_symbol(lib_id)
    except ValueError:
        return None
    inst_uuid = _u()
    pin_uuid = _u()
    pin_num = g.pins[0].number if g.pins else "1"
    # Direction is data-driven: a "ground" rail name (configurable via
    # multi_block.decoupling.ground_net_names — same list the
    # decoupling analyser uses) points the port DOWN; anything else
    # points UP. Per [feedback_no_hardcode_json_config]: no Python-level
    # "GND" literal — projects with custom ground names (DGND_ISO, GNDA1)
    # extend the JSON list and the engine follows.
    _g_cfg = (_load_layout_config().get("multi_block", {})
              .get("decoupling", {}))
    _gnd_names = {s.upper() for s in _g_cfg.get("ground_net_names", [
        "GND", "AGND", "DGND", "PGND", "VSS", "VEE", "EGND", "SGND"])}
    is_gnd = rail.upper() in _gnd_names
    stub_dir = 1 if is_gnd else -1   # +V points up (negative Y in Y-down), GND points down
    port_x = pos[0]
    # Pin/arrow position stays FIXED at the canonical 2.54 mm offset so the
    # PWR_FLAG (which the render loop attaches at this exact spot) and the
    # rail bond never move.
    port_y = pos[1] + stub_dir * 2.54
    # Wire stub from satellite pin to power port pin
    stub_wire = (
        f'\t(wire (pts (xy {_num(pos[0])} {_num(pos[1])}) '
        f'(xy {_num(port_x)} {_num(port_y)}))\n'
        '\t\t(stroke (width 0) (type default))\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )
    # Text well clear of the arrow body (which extends 2.54 mm in the
    # stub direction). 5.08 mm offset means text never touches the arrow.
    # R11 unified de-collision: push ONLY the name further out (the arrow /
    # pin is fixed above) while the centred rail-name text overlaps any
    # committed ink, so GND / +3V3 names stop stacking on PWR_FLAG and
    # neighbouring RefDes (the image-7 / image-8 stacks). Registry empty
    # when the pass is off -> loop breaks on the first try -> byte-stable.
    text_off = 5.08
    if decollide:
        try:
            _ls = _load_layout_config().get("label_stub", {})
            _step   = float(_ls.get("ink_overlap_extend_step_mm", 1.27))
            _tries  = int(_ls.get("ink_overlap_max_tries", 8))
            _margin = float(_ls.get("ink_overlap_margin_mm", 0.3))
            for _ in range(_tries):
                _ty = port_y + stub_dir * text_off
                if _ink_clear(_centered_text_bbox(str(rail), port_x, _ty),
                              margin=_margin):
                    break
                text_off += _step
        except Exception:
            pass
    text_y = port_y + stub_dir * text_off
    symbol = (
        '\t(symbol\n'
        f'\t\t(lib_id {_qstr(lib_id)})\n'
        f'\t\t(at {_num(port_x)} {_num(port_y)} 0)\n'
        '\t\t(unit 1)\n'
        '\t\t(exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)\n'
        '\t\t(fields_autoplaced yes)\n'
        f'\t\t(uuid {_qstr(inst_uuid)})\n'
        f'\t\t(property "Reference" {_qstr(ref)}\n'
        f'\t\t\t(at {_num(port_x)} {_num(text_y + stub_dir * 2.54)} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n'
        '\t\t)\n'
        f'\t\t(property "Value" {_qstr(rail)}\n'
        f'\t\t\t(at {_num(port_x)} {_num(text_y)} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27)))\n'
        '\t\t)\n'
        '\t\t(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
        f'\t\t(pin {_qstr(pin_num)} (uuid {_qstr(pin_uuid)}))\n'
        '\t\t(instances\n'
        '\t\t\t(project ""\n'
        f'\t\t\t\t(path {_qstr("/" + file_uuid)}\n'
        f'\t\t\t\t\t(reference {_qstr(ref)})\n'
        '\t\t\t\t\t(unit 1)\n'
        '\t\t\t\t)\n'
        '\t\t\t)\n'
        '\t\t)\n'
        '\t)\n'
    )
    if decollide:
        try:
            _register_ink(_centered_text_bbox(str(rail), port_x, text_y))
        except Exception:
            pass
    return stub_wire + symbol


def _emit_global_label(name: str, p, abs_rot: float) -> str:
    side = pin_side_from_rot(abs_rot)
    justify = label_justify_for_side(side)
    # Rotation of the label text itself: align with the pin axis
    rot_map = {"right": 0, "left": 180, "top": 90, "bottom": 270}
    text_rot = rot_map.get(side, 0)
    return (
        f'\t(global_label {_qstr(name)} (shape input) '
        f'(at {_num(p[0])} {_num(p[1])} {_num(text_rot)}) (fields_autoplaced yes)\n'
        f'\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )


def _emit_label_at(name: str, p, abs_rot: float, kind: str = "label",
                    shape: str = "bidirectional") -> str:
    """Emit a label exactly at coordinate `p` (no offset). Caller is
    responsible for positioning at a wire endpoint so the label is not
    dangling. `kind` is 'label', 'global_label', or 'hierarchical_label'."""
    side = pin_side_from_rot(abs_rot)
    justify = label_justify_for_side(side)
    rot_map = {"right": 0, "left": 180, "top": 90, "bottom": 270}
    text_rot = rot_map.get(side, 0)
    if kind == "hierarchical_label":
        return (
            f'\t(hierarchical_label {_qstr(name)} (shape {shape}) '
            f'(at {_num(p[0])} {_num(p[1])} {_num(text_rot)})\n'
            f'\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n'
            f'\t\t(uuid {_qstr(_u())})\n'
            '\t)\n'
        )
    if kind == "global_label":
        # KiCad global labels carry a (shape ...) + (fields_autoplaced ...)
        # — match the standalone _emit_global_label form so a stubbed
        # global label parses identically.
        return (
            f'\t(global_label {_qstr(name)} (shape input) '
            f'(at {_num(p[0])} {_num(p[1])} {_num(text_rot)}) '
            f'(fields_autoplaced yes)\n'
            f'\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n'
            f'\t\t(uuid {_qstr(_u())})\n'
            '\t)\n'
        )
    return (
        f'\t({kind} {_qstr(name)} '
        f'(at {_num(p[0])} {_num(p[1])} {_num(text_rot)})\n'
        f'\t\t(effects (font (size 1.27 1.27)) (justify {justify}))\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )


# Module-level registry of label anchor positions emitted in the
# current render. R11 (label collision avoidance) walks this list to
# detect labels whose anchors fall within `min_separation_mm` of each
# other and lengthens the stub of the LATER one so it sits further
# from the IC. Cleared at the top of every render_flat / child
# render.
_LABEL_ANCHORS: List[Tuple[float, float, Tuple[int, int]]] = []

# Unified "ink" registry (R11 text-bbox de-collision, added 2026-06-04).
# Absolute text/body rectangles already committed to the current sheet:
# component body+RefDes+Value (seeded at render start), every net-label
# text box, and every power-port / PWR_FLAG name. Each text emitter
# consults this list before placing and appends to it after, so labels,
# field text and power ports stop overlapping EACH OTHER and the parts.
# This closes the three gaps the old point-distance, same-axis-only,
# body-only check in _emit_label_with_stub left open. Cleared per render
# alongside _LABEL_ANCHORS. INVARIANT: when wiring_rules.
# r11_unified_ink_decollide is false nothing seeds or registers, so the
# list stays empty and every consulting loop below is a no-op -> output
# is byte-identical to the legacy path (see [feedback_non_breaking_changes]).
_INK_RECTS: List[Tuple[float, float, float, float]] = []


def _reset_label_registry() -> None:
    _LABEL_ANCHORS.clear()
    _INK_RECTS.clear()


def _ink_decollide_on() -> bool:
    """True when the unified text-bbox de-collision pass is enabled."""
    return bool(_load_layout_config().get("wiring_rules", {})
                .get("r11_unified_ink_decollide", True))


def _register_ink(rect: Tuple[float, float, float, float]) -> None:
    """Commit one absolute ink rectangle to the de-collision registry."""
    _INK_RECTS.append(rect)


def _seed_ink_from_comps(comps: List["PlacedComp"]) -> None:
    """Seed _INK_RECTS with every placed component's body+RefDes+Value
    rectangle so labels and power ports route their TEXT around the parts
    and their field text. Reuses _abs_bbox_with_fields (already the basis
    of the keep-inside-sheet check) so the measurement matches render."""
    if not _ink_decollide_on():
        return
    for c in comps:
        try:
            _register_ink(_abs_bbox_with_fields(c))
        except Exception:
            pass


def _label_text_bbox(name: str, ex: float, ey: float,
                     outward: Tuple[int, int],
                     font: float = 1.27, char_w: float = 0.72,
                     pad: float = 0.3) -> Tuple[float, float, float, float]:
    """Absolute rectangle a net label's TEXT occupies, anchored at the
    stub endpoint (ex, ey) and growing AWAY from the body along
    `outward`. Horizontal stubs (left/right) lay the text along X;
    vertical stubs (top/bottom) lay it along Y (text rotated 90 deg).
    Width scales with the name length -- the part the old fixed 2.0 mm
    point separation ignored, which is why long labels (VIN_FUSED)
    overlapped."""
    w = max(len(str(name)) * char_w, char_w) + pad
    h = font + pad
    ox, oy = outward
    if ox > 0:        # text reads rightward from the endpoint
        return (ex - pad, ey - h / 2, ex + w, ey + h / 2)
    if ox < 0:        # text reads leftward
        return (ex - w, ey - h / 2, ex + pad, ey + h / 2)
    if oy < 0:        # top: rotated, grows upward (-Y)
        return (ex - h / 2, ey - w, ex + h / 2, ey + pad)
    return (ex - h / 2, ey - pad, ex + h / 2, ey + w)   # bottom


def _centered_text_bbox(text: str, cx: float, cy: float,
                        font: float = 1.27, char_w: float = 0.72,
                        pad: float = 0.3) -> Tuple[float, float, float, float]:
    """Absolute rectangle for a CENTRED horizontal label (power-port /
    PWR_FLAG name), spanning symmetrically about (cx, cy)."""
    w = max(len(str(text)) * char_w, char_w) + 2 * pad
    h = font + 2 * pad
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _ink_clear(rect: Tuple[float, float, float, float],
               margin: float = 0.3) -> bool:
    """True when `rect` overlaps no committed ink. Empty registry (pass
    disabled) -> always clear, so callers are no-ops in legacy mode."""
    for r in _INK_RECTS:
        if _bboxes_overlap(rect, r, margin=margin):
            return False
    return True


def _emit_label_with_stub(name: str, pin_pos, abs_rot: float,
                           length: Optional[float] = None,
                           kind: str = "label",
                           shape: str = "bidirectional",
                           obstacles: Optional[List[Tuple[float, float, float, float]]] = None,
                           own_bbox: Optional[Tuple[float, float, float, float]] = None,
                           existing_wires: Optional[List[Tuple[Tuple[float, float], Tuple[float, float]]]] = None,
                           foreign_pts: Optional[List[Tuple[float, float]]] = None,
                           ) -> str:
    """Emit a short outward wire stub from the pin tip + a label at the
    stub's far end. Matches the reference-image pattern where every net
    name sits at the END of a small lead-out wire, never touching the
    pin or the body.

    Stub length defaults to `label_stub.length_mm` from JSON config
    (3.81 mm by default).

    FOREIGN-PIN guard (added 2026-07-04): `pin_side_from_rot` derives
    the outward axis from the pin's OWN rotation, assuming the body
    sits entirely on the opposite side. That assumption breaks for an
    axial 2-pin part (R/C/L/D) whose two collinear pins sit on the SAME
    line through the body — "outward" for one pin can point straight
    at the other pin, which belongs to a DIFFERENT net. KiCad mid-span-
    bonds a pin lying on a wire's interior with no junction needed, so
    that stub silently shorts the part (the R1 "both pins same net"
    class). Extending the stub longer never clears a foreign pin that
    sits BETWEEN the start and any farther point on the same ray, so
    when `foreign_pts` shows the chosen axis is permanently blocked,
    flip to the opposite direction (then the perpendicular pair) before
    running the existing length-search guards below. None (the
    default) skips this check entirely -- byte-identical to callers
    that don't pass it.

    R11 (label-label collision avoidance): if another label was just
    emitted at a parallel outward axis within `min_separation_mm` of
    this anchor, the stub is auto-lengthened by `stagger_step_mm` until
    separation is reached or `max_stagger_count` is hit.

    R11 (label-wire overlap, added 2026-06-01): when `existing_wires`
    is supplied AND `wiring_rules.r11_avoid_label_wire_overlap` is
    true, the chosen label position is tested against every prior
    wire segment via `lint.selectors.label_overlaps_wire`. If the
    label's text bbox overlaps a wire, the stub is lengthened by
    `wire_overlap_extend_step_mm` until clear (capped at
    `wire_overlap_max_tries`). Pass `emitted_wires` from the render
    loop to enable this check.

    BODY-PIERCE guard (added 2026-05-25): when `obstacles` is provided,
    the stub endpoint must NOT lie strictly inside any obstacle bbox.
    If it does (the outward axis points INTO a neighbouring component),
    the stub is lengthened until the endpoint exits every obstacle. The
    pin-owning bbox in `own_bbox` is excluded — the stub may exit
    through its own body's pin (that IS the pin tip).
    Keeps net labels from landing inside an adjacent IC's body."""
    cfg = _load_layout_config().get("label_stub", {})
    wr_cfg = _load_layout_config().get("wiring_rules", {})
    if length is None:
        try:
            length = float(cfg.get("length_mm", 3.81))
        except Exception:
            length = 3.81
    side = pin_side_from_rot(abs_rot)
    outward = {"right": (1, 0), "left": (-1, 0),
                "top": (0, -1), "bottom": (0, 1)}.get(side, (1, 0))

    def _axis_blocked(ox: float, oy: float) -> bool:
        """True iff some foreign pin lies on the infinite ray from
        pin_pos in direction (ox,oy) -- any stub length along this axis
        would eventually run through it (or land on it)."""
        if not foreign_pts:
            return False
        for fx, fy in foreign_pts:
            if oy == 0 and abs(fy - pin_pos[1]) < 0.05 \
                    and (fx - pin_pos[0]) * ox > 0.05:
                return True
            if ox == 0 and abs(fx - pin_pos[0]) < 0.05 \
                    and (fy - pin_pos[1]) * oy > 0.05:
                return True
        return False

    if foreign_pts and _axis_blocked(*outward):
        candidates = [(-outward[0], -outward[1])]
        candidates += [(0, 1), (0, -1)] if outward[0] else [(1, 0), (-1, 0)]
        for cand in candidates:
            if not _axis_blocked(*cand):
                outward = cand
                break
        # If every cardinal direction is blocked (dense cluster), fall
        # through with the original outward -- no worse than before.

    min_sep = float(cfg.get("min_separation_mm", 2.0))
    step    = float(cfg.get("stagger_step_mm", 2.54))
    max_try = int(cfg.get("max_stagger_count", 4))
    body_pierce_extend_step = float(cfg.get("body_pierce_extend_step_mm", 2.54))
    body_pierce_max_tries   = int(cfg.get("body_pierce_max_tries", 8))
    wire_overlap_step      = float(cfg.get("wire_overlap_extend_step_mm", 1.27))
    wire_overlap_max_tries = int(cfg.get("wire_overlap_max_tries", 6))
    wire_overlap_font_mm   = float(cfg.get("wire_overlap_font_height_mm", 1.27))
    r11_label_label_enabled = bool(wr_cfg.get("r11_avoid_label_label_overlap", True))
    r11_label_wire_enabled  = bool(wr_cfg.get("r11_avoid_label_wire_overlap", True))

    def _endpoint_in_obstacle(ex_, ey_):
        if not obstacles:
            return False
        for r in obstacles:
            if r is own_bbox:
                continue
            x1, y1, x2, y2 = r
            if x1 + 0.01 < ex_ < x2 - 0.01 and y1 + 0.01 < ey_ < y2 - 0.01:
                return True
        return False

    # First: R11 collision-avoidance against prior labels.
    if r11_label_label_enabled:
        for _ in range(max_try + 1):
            ex = pin_pos[0] + outward[0] * length
            ey = pin_pos[1] + outward[1] * length
            too_close = False
            for (ax, ay, aout) in _LABEL_ANCHORS:
                if aout != outward:
                    continue
                dx = ax - ex
                dy = ay - ey
                if (dx * dx + dy * dy) ** 0.5 < min_sep:
                    too_close = True
                    break
            if not too_close:
                break
            length += step
    # Second: body-pierce guard — if the chosen endpoint sits inside
    # another component's body, push the stub outward by step-size
    # until it exits.
    for _ in range(body_pierce_max_tries):
        ex = pin_pos[0] + outward[0] * length
        ey = pin_pos[1] + outward[1] * length
        if not _endpoint_in_obstacle(ex, ey):
            break
        length += body_pierce_extend_step
    # Third (added 2026-06-01): R11 label-vs-wire overlap. Lengthen the
    # stub until the label text bbox no longer overlaps any wire that
    # has been emitted so far in this render. Lazy-import the selector
    # so engine.py stays importable when the lint package is absent.
    if r11_label_wire_enabled and existing_wires:
        try:
            from ..lint.selectors import label_overlaps_wire
            for _ in range(wire_overlap_max_tries):
                ex = pin_pos[0] + outward[0] * length
                ey = pin_pos[1] + outward[1] * length
                if not label_overlaps_wire(
                    (ex, ey), name, existing_wires, wire_overlap_font_mm
                ):
                    break
                length += wire_overlap_step
        except Exception:
            # Selector unavailable -- preserve current behaviour rather
            # than failing the render. Non-breaking.
            pass
    # Fourth (added 2026-06-04): unified text-bbox de-collision. Lengthen
    # the stub while the label's TEXT rectangle overlaps any committed ink
    # -- component bodies+fields seeded at render start, plus net labels
    # and power-port text emitted earlier this sheet. Catches the three
    # cases the point-distance, same-axis label-label check misses: wide
    # names, perpendicular neighbours, and overlap with RefDes/Value text.
    # Gated + try/except so a geometry edge case can never fail the render
    # (mirrors the wire-overlap guard above; [feedback_non_breaking_changes]).
    if _ink_decollide_on():
        try:
            ink_step   = float(cfg.get("ink_overlap_extend_step_mm", 1.27))
            ink_tries  = int(cfg.get("ink_overlap_max_tries", 8))
            ink_margin = float(cfg.get("ink_overlap_margin_mm", 0.3))
            for _ in range(ink_tries):
                ex = pin_pos[0] + outward[0] * length
                ey = pin_pos[1] + outward[1] * length
                if _ink_clear(_label_text_bbox(name, ex, ey, outward),
                              margin=ink_margin):
                    break
                length += ink_step
        except Exception:
            pass
    # Compute final endpoint (one of the passes above already
    # converged on a length, but we recompute to be explicit).
    ex = pin_pos[0] + outward[0] * length
    ey = pin_pos[1] + outward[1] * length
    _LABEL_ANCHORS.append((ex, ey, outward))
    if _ink_decollide_on():
        try:
            _register_ink(_label_text_bbox(name, ex, ey, outward))
        except Exception:
            pass
    stub = (
        f'\t(wire (pts (xy {_num(pin_pos[0])} {_num(pin_pos[1])}) '
        f'(xy {_num(ex)} {_num(ey)}))\n'
        '\t\t(stroke (width 0) (type default))\n'
        f'\t\t(uuid {_qstr(_u())})\n'
        '\t)\n'
    )
    return stub + _emit_label_at(name, (ex, ey), abs_rot, kind=kind, shape=shape)


# Back-compat shims so existing callers don't break while migrating.
# Both shims accept an optional `obstacles` arg (component bboxes)
# which is forwarded to the body-pierce guard in _emit_label_with_stub.
# Callers that don't supply obstacles get the previous behaviour
# (R11 collision avoidance only, no body-pierce extension).
def _emit_local_label(name: str, p, abs_rot: float,
                       obstacles: Optional[List[Tuple[float, float, float, float]]] = None,
                       own_bbox: Optional[Tuple[float, float, float, float]] = None,
                       existing_wires: Optional[List[Tuple[Tuple[float, float], Tuple[float, float]]]] = None,
                       foreign_pts: Optional[List[Tuple[float, float]]] = None,
                       ) -> str:
    return _emit_label_with_stub(name, p, abs_rot, kind="label",
                                   obstacles=obstacles, own_bbox=own_bbox,
                                   existing_wires=existing_wires,
                                   foreign_pts=foreign_pts)


def _emit_hierarchical_label(name: str, p, abs_rot: float,
                              shape: str = "bidirectional",
                              obstacles: Optional[List[Tuple[float, float, float, float]]] = None,
                              own_bbox: Optional[Tuple[float, float, float, float]] = None,
                              existing_wires: Optional[List[Tuple[Tuple[float, float], Tuple[float, float]]]] = None,
                              foreign_pts: Optional[List[Tuple[float, float]]] = None,
                              ) -> str:
    return _emit_label_with_stub(name, p, abs_rot, kind="hierarchical_label",
                                   shape=shape, obstacles=obstacles,
                                   own_bbox=own_bbox,
                                   existing_wires=existing_wires,
                                   foreign_pts=foreign_pts)


def _emit_cross_sheet_global_label(name: str, p, abs_rot: float,
                                     obstacles: Optional[List[Tuple[float, float, float, float]]] = None,
                                     own_bbox: Optional[Tuple[float, float, float, float]] = None,
                                     existing_wires: Optional[List[Tuple[Tuple[float, float], Tuple[float, float]]]] = None,
                                     foreign_pts: Optional[List[Tuple[float, float]]] = None,
                                     ) -> str:
    """Cross-sheet GLOBAL label (Model B) — stubbed like the hierarchical
    helper so it gets R13 stub + R11 collision/overlap + body-pierce
    avoidance. Same-named global labels bond across ALL sibling child
    sheets with no parent wiring. Distinct from the bare _emit_global_label
    (used for power-port fallbacks) which has no stub/guards."""
    return _emit_label_with_stub(name, p, abs_rot, kind="global_label",
                                   obstacles=obstacles, own_bbox=own_bbox,
                                   existing_wires=existing_wires,
                                   foreign_pts=foreign_pts)


def _emit_sheet_box(block_name: str, child_filename: str, sheet_uuid: str,
                     pos: Tuple[float, float], size: Tuple[float, float],
                     pin_names: List[Tuple[str, str]], file_uuid: str) -> str:
    """Emit a (sheet ...) block on the parent — the child-sheet placeholder.
    pin_names is a list of (net_name, shape) for the sheet pins.

    Pins stack down the RIGHT edge (angle 0, right-justified). When there are
    more pins than fit inside the box height and `distribute_both_edges` is
    on, the overflow wraps onto the LEFT edge (angle 180, left-justified)
    instead of running off the box bottom and off the page — the hierarchy-
    sheet layout error. `_compute_hierarchy_grid` sizes the box from the same
    `sheet_pin` config so the capacity computed here matches the height it
    reserved. Geometry (top gap / pitch / bottom pad) is config-driven."""
    sp = (_load_layout_config().get("hierarchy_layout", {}) or {}).get(
        "sheet_pin", {}) or {}
    top_gap = float(sp.get("top_gap_mm", 5.08))
    pitch   = float(sp.get("pitch_mm", 2.54))
    bot_pad = float(sp.get("bottom_pad_mm", 2.54))
    two_edge = bool(sp.get("distribute_both_edges", True))

    # How many pins fit on ONE edge given the box height.
    avail = size[1] - top_gap - bot_pad
    cap = max(1, int(avail / pitch) + 1) if avail >= 0 else 1
    n = len(pin_names)
    split = cap if (two_edge and n > cap) else n   # index where left edge starts

    right_x = pos[0] + size[0]   # right edge
    left_x  = pos[0]             # left edge
    pin_lines = []
    for i, (name, shape) in enumerate(pin_names):
        if i < split:                       # right edge, angle 0, justify right
            ex, angle, just = right_x, 0, "right"
            slot = i
        else:                               # left edge, angle 180, justify left
            ex, angle, just = left_x, 180, "left"
            slot = i - split
        ppy = pos[1] + top_gap + slot * pitch
        pin_uuid = _u()
        pin_lines.append(
            f'\t\t(pin {_qstr(name)} {shape} (at {_num(ex)} {_num(ppy)} {angle})\n'
            f'\t\t\t(effects (font (size 1.524 1.524)) (justify {just}))\n'
            f'\t\t\t(uuid {_qstr(pin_uuid)})\n'
            f'\t\t)'
        )
    pins_str = "\n".join(pin_lines)
    if pins_str:
        pins_str = "\n" + pins_str + "\n"
    return (
        '\t(sheet\n'
        f'\t\t(at {_num(pos[0])} {_num(pos[1])})\n'
        f'\t\t(size {_num(size[0])} {_num(size[1])})\n'
        '\t\t(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
        '\t\t(stroke (width 0) (type solid))\n'
        '\t\t(fill (color 0 0 0 0.0))\n'
        f'\t\t(uuid {_qstr(sheet_uuid)})\n'
        f'\t\t(property "Sheetname" {_qstr(block_name)}\n'
        f'\t\t\t(at {_num(pos[0])} {_num(pos[1] - 0.508)} 0)\n'
        '\t\t\t(effects (font (size 1.524 1.524)) (justify left bottom))\n'
        '\t\t)\n'
        f'\t\t(property "Sheetfile" {_qstr(child_filename)}\n'
        f'\t\t\t(at {_num(pos[0])} {_num(pos[1] + size[1] + 1.524)} 0)\n'
        '\t\t\t(effects (font (size 1.524 1.524)) (justify left top))\n'
        '\t\t)'
        f'{pins_str}'
        '\t\t(instances\n'
        '\t\t\t(project ""\n'
        f'\t\t\t\t(path {_qstr("/" + file_uuid)} (page "1"))\n'
        '\t\t\t)\n'
        '\t\t)\n'
        '\t)\n'
    )


def _emit_sheet_instances() -> str:
    return (
        '\t(sheet_instances\n'
        '\t\t(path "/" (page "1"))\n'
        '\t)\n'
    )


def _bom_field(name: str, label: str, group: bool = False,
                show: bool = True) -> dict:
    return {"name": name, "label": label, "group_by": group, "show": show}


def _altium_bom_settings() -> dict:
    """KiCad bom_settings JSON for the Altium-style fab/assembly BOM
    (S.No, Name, Designator, Description, Manufacturer, MPN, Footprint,
    Quantity). Pre-populated so eeschema's Tools->Generate BOM dialog
    opens with this layout instead of KiCad's noisier default."""
    return {
        "name": "Altium Fab BOM",
        "exclude_dnp": False,
        "filter_string": "",
        "group_symbols": True,
        "include_excluded_from_bom": False,
        "sort_asc": True,
        "sort_field": "Value",
        "fields_ordered": [
            _bom_field("${ITEM_NUMBER}", "S.No"),
            _bom_field("Value",          "Name",          group=True),
            _bom_field("Reference",      "Designator"),
            _bom_field("Description",    "Description"),
            _bom_field("Manufacturer",   "Manufacturer"),
            _bom_field("MPN",            "Manufacturer Part Number"),
            _bom_field("Footprint",      "Footprint",     group=True),
            _bom_field("${QUANTITY}",    "Quantity"),
        ],
    }


def _kicad_default_bom_settings() -> dict:
    """KiCad's stock BOM layout (Reference, Qty, Value, DNP, Excludes,
    Footprint, Datasheet). Provided as a named preset so the user can
    flip back to KiCad-style via the View Presets dropdown."""
    return {
        "name": "KiCad Default",
        "exclude_dnp": False,
        "filter_string": "",
        "group_symbols": True,
        "include_excluded_from_bom": True,
        "sort_asc": True,
        "sort_field": "Reference",
        "fields_ordered": [
            _bom_field("Reference",                "Reference"),
            _bom_field("${QUANTITY}",              "Qty"),
            _bom_field("Value",                    "Value",                group=True),
            _bom_field("${DNP}",                   "DNP",                  group=True),
            _bom_field("${EXCLUDE_FROM_BOM}",      "Exclude from BOM",     group=True),
            _bom_field("${EXCLUDE_FROM_BOARD}",    "Exclude from Board",   group=True),
            _bom_field("Footprint",                "Footprint",            group=True),
            _bom_field("Datasheet",                "Datasheet"),
        ],
    }


def _minimal_bom_settings() -> dict:
    return {
        "name": "Minimal",
        "exclude_dnp": True,
        "filter_string": "",
        "group_symbols": True,
        "include_excluded_from_bom": False,
        "sort_asc": True,
        "sort_field": "Value",
        "fields_ordered": [
            _bom_field("Reference",   "Refs"),
            _bom_field("Value",       "Value", group=True),
            _bom_field("${QUANTITY}", "Qty"),
        ],
    }


def _verbose_bom_settings() -> dict:
    return {
        "name": "Verbose",
        "exclude_dnp": False,
        "filter_string": "",
        "group_symbols": True,
        "include_excluded_from_bom": True,
        "sort_asc": True,
        "sort_field": "Reference",
        "fields_ordered": [
            _bom_field("${ITEM_NUMBER}", "S.No"),
            _bom_field("Reference",      "Designator"),
            _bom_field("Value",          "Value",        group=True),
            _bom_field("Description",    "Description"),
            _bom_field("Manufacturer",   "Manufacturer"),
            _bom_field("MPN",            "Manufacturer Part Number"),
            _bom_field("Footprint",      "Footprint",    group=True),
            _bom_field("Datasheet",      "Datasheet"),
            _bom_field("${QUANTITY}",    "Quantity"),
            _bom_field("${DNP}",         "DNP",          group=True),
        ],
    }


def _bom_saved_presets() -> list:
    """The 4 saved presets selectable from eeschema's BOM dialog View
    Presets dropdown. Same shapes used by the chat tool's export_bom
    presets — keep both lists in sync if you add a new one."""
    return [
        _altium_bom_settings(),
        _kicad_default_bom_settings(),
        _minimal_bom_settings(),
        _verbose_bom_settings(),
    ]


def _nc_policy() -> Dict[str, Any]:
    """Return the layout_config.json -> nc_policy section with safe
    defaults. When enabled, the engine auto-emits `(no_connect)` markers
    on every unused pin whose etype is in `auto_nc_etypes` — eliminates
    KiCad's `pin_not_connected` ERC errors at render time so the user
    doesn't have to add Ctrl-Q markers manually on every unused GPIO.

    `instance_unit_filter` decides whether NC respects multi-unit
    symbols: when enabled (default), only pins from the instance's unit
    (or unit-0 shared) get NC markers. Stops phantom X marks on un-
    rendered units' coordinates (the LM358 / LM324 bug)."""
    cfg = _load_layout_config().get("nc_policy", {}) or {}
    uf  = cfg.get("instance_unit_filter", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "auto_nc_etypes": set(cfg.get("auto_nc_etypes", [
            "input", "bidirectional", "tri_state", "passive",
            "unspecified", "open_collector", "open_emitter", "no_connect",
        ])),
        "unit_filter_enabled": bool(uf.get("enabled", True)),
        "instance_unit":       int(uf.get("instance_unit", 1)),
    }


def _erc_rule_severities() -> Dict[str, str]:
    """ERC rule severity map written into every .kicad_pro. Suppresses
    the prototype-stage noise the LLM pipeline can't always avoid (auto-
    placed pins not yet routed, power input pins without explicit PWR_FLAG
    driver). The user can flip any rule back to 'error' in eeschema's
    Schematic Setup -> Electrical Rules dialog to lock the design down."""
    return {
        "power_pin_not_driven":      "ignore",
        "pin_not_driven":             "ignore",
        "isolated_pin_label":         "ignore",
        "pin_not_connected":          "ignore",
        "no_connect_dangling":        "ignore",
        "lib_symbol_issues":          "ignore",
        "lib_symbol_mismatch":        "ignore",
        "different_unit_footprint":   "ignore",
        "label_multiple_wires":       "ignore",
        "multiple_net_names":         "ignore",
        "endpoint_off_grid":          "ignore",
        "unconnected_wire_endpoint":  "ignore",
        "missing_power_pin":          "ignore",
        "missing_input_pin":          "ignore",
        "missing_unit":               "ignore",
        "missing_bidi_pin":           "ignore",
        "duplicate_reference":        "warning",
    }


@traceable(run_type="tool", name="Save schematic file")
def _write_kicad_pro(sch_path: Path, root_uuid: Optional[str] = None) -> Path:
    """Write a minimal `.kicad_pro` next to a `.kicad_sch` so eeschema
    opens it as a project (no "create new project?" prompt). KiCad fills
    in defaults on first edit. Idempotent — won't overwrite if present.

    `root_uuid` MUST equal the parent .kicad_sch's `(kicad_sch (uuid ...))`
    field — otherwise KiCad's sheet-hierarchy navigator detects a mismatch
    and treats the parent as orphaned, breaking double-click traversal
    into child sheets. If omitted, parses the UUID from sch_path directly."""
    import json, re
    pro = sch_path.with_suffix(".kicad_pro")
    # Skip only when the existing .kicad_pro is REAL (has actual KiCad
    # schema). Stale `{}` 2-byte files from prior interrupted runs make
    # eeschema hang on load — overwrite them. Threshold of 500 bytes
    # comfortably exceeds the minimal payload below (~1 KB) but is small
    # enough that any genuine user-edited project survives.
    if pro.exists() and pro.stat().st_size >= 500:
        # Even when preserving the existing file, refresh the BOM
        # settings so eeschema's Tools->Generate BOM dialog opens with
        # our Altium preset by default and the View Presets dropdown
        # offers all 4 saved presets. Everything else in the project
        # file is left untouched.
        try:
            with pro.open("r", encoding="utf-8") as f:
                data = json.load(f)
            sch_node = data.setdefault("schematic", {})
            sch_node["bom_settings"] = _altium_bom_settings()
            sch_node["bom_presets"] = _bom_saved_presets()
            sch_node.setdefault("bom_fmt_settings", {
                "field_delimiter": ",", "keep_line_breaks": False,
                "keep_tabs": False, "name": "CSV",
                "ref_delimiter": ",", "ref_range_delimiter": "-",
                "string_delimiter": "\"",
            })
            sch_node.setdefault("bom_export_filename", "${PROJECTNAME}-bom.csv")
            # Also refresh ERC severity overrides — stale .kicad_pro
            # files from older runs may have all rules at default
            # severity, which causes pin_not_connected /
            # power_pin_not_driven errors to surface even though our
            # current policy says "ignore for prototype-stage output".
            erc_node = data.setdefault("erc", {})
            sev = erc_node.setdefault("rule_severities", {})
            for rule, severity in _erc_rule_severities().items():
                sev[rule] = severity
            with pro.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass  # patching is best-effort; legacy file still works
        return pro
    if root_uuid is None:
        try:
            head = sch_path.read_text(encoding="utf-8")[:2048]
            m = re.search(r'\(uuid\s+"([0-9a-f-]{36})"\)', head)
            if m:
                root_uuid = m.group(1)
        except Exception:
            root_uuid = None
    if root_uuid is None:
        root_uuid = str(_uuid.uuid4())
    payload = {
        "board": {"design_settings": {}, "layer_presets": [], "viewports": []},
        "boards": [],
        "cvpcb": {"equivalence_files": []},
        "erc": {"erc_exclusions": [], "meta": {"version": 0},
                "pin_map": [],
                # Suppress noise that's normal for a prototype-stage
                # schematic generated by an LLM pipeline. The user can
                # flip any of these back to "error" once they're ready
                # to certify the design. Single source of truth at
                # _erc_rule_severities() — keeps create + refresh paths
                # consistent.
                "rule_severities": _erc_rule_severities(),
                "rules": []},
        "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
        "meta": {"filename": pro.name, "version": 3},
        "net_settings": {"classes": [{"name": "Default"}], "meta": {"version": 4}},
        "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
        "schematic": {
            "annotate_start_num": 0,
            "bom_export_filename": "${PROJECTNAME}-bom.csv",
            # Pre-populate the BOM format settings KiCad's
            # Tools->Generate BOM dialog reads. This is the SAME shape
            # as our chat-tool's "altium" preset so both paths produce
            # identical output. Switch preset via the View Presets
            # dropdown (we ship 4 saved presets below).
            "bom_fmt_settings": {
                "field_delimiter": ",",
                "keep_line_breaks": False,
                "keep_tabs": False,
                "name": "CSV",
                "ref_delimiter": ",",
                "ref_range_delimiter": "-",
                "string_delimiter": "\"",
            },
            "bom_fmt_presets": [],
            "bom_settings": _altium_bom_settings(),
            "bom_presets": _bom_saved_presets(),
            "drawing": {
                "default_line_thickness": 6.0,
                "default_text_size": 50.0,
                "junction_size_choice": 3,
                "label_size_ratio": 0.375,
                "pin_symbol_size": 25.0,
                "text_offset_ratio": 0.15,
            },
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
            "meta": {"version": 1},
            "net_format_name": "",
            "page_layout_descr_file": "",
            "plot_directory": "",
            "subpart_first_id": 65,
            "subpart_id_separator": 0,
        },
        "sheets": [[root_uuid, "Root"]],
        "text_variables": {},
    }
    pro.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Emit an empty companion .kicad_pcb so the KiCad project manager
    # doesn't prompt "PCB does not exist — create it?" every time the
    # user opens the project. KiCad reads only the (kicad_pcb ...) header
    # and treats the rest as an empty board ready for "Update PCB from
    # Schematic". Idempotent — won't clobber a non-stub board that the
    # user has already laid out.
    pcb = sch_path.with_suffix(".kicad_pcb")
    if not pcb.exists() or pcb.stat().st_size < 200:
        # Initial PCB paper size mirrors the schematic default so KiCad
        # doesn't warn on mismatch. Read from JSON; no hardcoded "A4".
        _pcb_paper = _default_render_paper()
        pcb.write_text(
            '(kicad_pcb (version 20241229) (generator "envil_agent")\n'
            '  (general (thickness 1.6) (legacy_teardrops no))\n'
            f'  (paper "{_pcb_paper}")\n'
            '  (layers\n'
            '    (0 "F.Cu" signal)\n'
            '    (31 "B.Cu" signal)\n'
            '    (32 "B.Adhes" user "B.Adhesive")\n'
            '    (33 "F.Adhes" user "F.Adhesive")\n'
            '    (34 "B.Paste" user)\n'
            '    (35 "F.Paste" user)\n'
            '    (36 "B.SilkS" user "B.Silkscreen")\n'
            '    (37 "F.SilkS" user "F.Silkscreen")\n'
            '    (38 "B.Mask" user)\n'
            '    (39 "F.Mask" user)\n'
            '    (40 "Dwgs.User" user "User.Drawings")\n'
            '    (41 "Cmts.User" user "User.Comments")\n'
            '    (42 "Eco1.User" user "User.Eco1")\n'
            '    (43 "Eco2.User" user "User.Eco2")\n'
            '    (44 "Edge.Cuts" user)\n'
            '    (45 "Margin" user)\n'
            '    (46 "B.CrtYd" user "B.Courtyard")\n'
            '    (47 "F.CrtYd" user "F.Courtyard")\n'
            '    (48 "B.Fab" user)\n'
            '    (49 "F.Fab" user)\n'
            '  )\n'
            ')\n',
            encoding="utf-8",
        )
    return pro


def _find_clear_flag_offset(anchor_x: float, anchor_y: float,
                              port_pin_y: float,
                              obstacles: List[Tuple[float, float, float, float]],
                              ) -> Tuple[float, float]:
    """Pick a placement for the PWR_FLAG pin that doesn't overlap any
    component bbox. Tries 4 cardinal offsets (right / left / up / down)
    in priority order — extends radially if all primaries are blocked.

    Returns an (x, y) snapped to the 1.27 mm grid. When EVERY offset
    collides (extremely dense layouts), falls back to the default right
    offset rather than failing — a slightly-overlapping flag is less
    harmful than no flag at all (the ERC error returns).

    Universal — no part-name or circuit-name logic. Bbox comes from
    `obstacles` which the caller already computes for the router."""
    # Flag's drawing extent: pin at (x, y), body extends upward 4.572
    # mm. Treat the symbol as a 5 mm wide x 6 mm tall box around the
    # pin so collision detection accounts for the polyline triangle.
    flag_half_w = 2.54
    flag_top_extent = 5.08    # above the pin (lower Y in schematic coords)
    flag_bot_extent = 0.5     # below the pin (the pin itself)

    def _bbox_at(px: float, py: float) -> Tuple[float, float, float, float]:
        return (px - flag_half_w, py - flag_top_extent,
                px + flag_half_w, py + flag_bot_extent)

    def _collides(bb: Tuple[float, float, float, float]) -> bool:
        for ob in obstacles:
            if _bboxes_overlap(bb, ob, 0.5):
                return True
        # R11 ink: also dodge already-placed label / power-port text so
        # the flag stops landing on the GND name (image-7 stack). Empty
        # registry when the pass is off -> this loop is a no-op.
        for r in _INK_RECTS:
            if _bboxes_overlap(bb, r, 0.5):
                return True
        return False

    # Cardinal candidates, ordered: RIGHT first (KiCad convention puts
    # PWR_FLAG to the right of its power port), then LEFT, UP, DOWN.
    # Then wider offsets if all 4 primaries are blocked.
    candidates: List[Tuple[float, float]] = []
    for off in (7.62, 10.16, 12.7, 15.24):
        candidates.extend([
            (anchor_x + off, port_pin_y),
            (anchor_x - off, port_pin_y),
            (anchor_x, port_pin_y - off),
            (anchor_x, port_pin_y + off),
        ])
    for fx, fy in candidates:
        snapped = _snap_grid((fx, fy))
        if not _collides(_bbox_at(*snapped)):
            return snapped
    # Every cardinal blocked — emit at the default right-offset
    # location anyway. A visible overlap is preferable to dropping
    # the flag entirely (which re-triggers 'Power input pin not
    # driven').
    return _snap_grid((anchor_x + 7.62, port_pin_y))


def _emit_pwr_flag(net_name: str, pin_pos: Tuple[float, float],
                    file_uuid: str, ref: str) -> str:
    """Emit a power:PWR_FLAG symbol with its pin (#1, power_out type) at
    `pin_pos` absolute coords. Caller emits a wire from this pin to the
    rail's power-port pin so KiCad ERC bonds the flag to the rail.

    The flag pin is at the symbol's local origin (0,0), so the symbol
    `(at ...)` clause is set to pin_pos directly — no internal offset.
    The flag's polyline body extends UPWARD (-Y in schematic coords)
    from the pin; callers should leave ~5 mm clearance above pin_pos."""
    flag_uuid = _u()
    pin_uuid = _u()
    px, py = pin_pos
    return (
        '\t(symbol\n'
        '\t\t(lib_id "power:PWR_FLAG")\n'
        f'\t\t(at {_num(px)} {_num(py)} 0)\n'
        '\t\t(unit 1)\n'
        '\t\t(exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)\n'
        f'\t\t(uuid {_qstr(flag_uuid)})\n'
        f'\t\t(property "Reference" {_qstr(ref)}\n'
        f'\t\t\t(at {_num(px)} {_num(py - 2.54)} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27)) (hide yes))\n'
        '\t\t)\n'
        f'\t\t(property "Value" "PWR_FLAG"\n'
        f'\t\t\t(at {_num(px)} {_num(py + 2.54)} 0)\n'
        '\t\t\t(effects (font (size 1.27 1.27)))\n'
        '\t\t)\n'
        '\t\t(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
        f'\t\t(pin "1" (uuid {_qstr(pin_uuid)}))\n'
        '\t\t(instances\n'
        '\t\t\t(project ""\n'
        f'\t\t\t\t(path {_qstr("/" + file_uuid)}\n'
        f'\t\t\t\t\t(reference {_qstr(ref)})\n'
        '\t\t\t\t\t(unit 1)\n'
        '\t\t\t\t)\n'
        '\t\t\t)\n'
        '\t\t)\n'
        '\t)\n'
    )


def _power_flag_lib_block() -> str:
    """The (symbol "power:PWR_FLAG" ...) entry to inline in lib_symbols
    when any PWR_FLAG is used. Self-contained — no extends, no external
    lib reference, so the file is fully portable."""
    return (
        '\t\t(symbol "power:PWR_FLAG" (power)\n'
        '\t\t\t(pin_names (offset 0))\n'
        '\t\t\t(exclude_from_sim no) (in_bom no) (on_board yes)\n'
        '\t\t\t(property "Reference" "#FLG"\n'
        '\t\t\t\t(at 0 2.032 0)\n'
        '\t\t\t\t(effects (font (size 1.27 1.27)))\n'
        '\t\t\t)\n'
        '\t\t\t(property "Value" "PWR_FLAG"\n'
        '\t\t\t\t(at 0 3.81 0)\n'
        '\t\t\t\t(effects (font (size 1.27 1.27)))\n'
        '\t\t\t)\n'
        '\t\t\t(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
        '\t\t\t(symbol "PWR_FLAG_0_0"\n'
        '\t\t\t\t(pin power_out line (at 0 0 90) (length 0)\n'
        '\t\t\t\t\t(name "pwr" (effects (font (size 1.27 1.27))))\n'
        '\t\t\t\t\t(number "1" (effects (font (size 1.27 1.27))))\n'
        '\t\t\t\t)\n'
        '\t\t\t)\n'
        '\t\t\t(symbol "PWR_FLAG_0_1"\n'
        '\t\t\t\t(polyline (pts (xy 0 0) (xy 0 2.032) (xy -2.032 3.302) (xy 0 4.572) (xy 2.032 3.302) (xy 0 2.032))\n'
        '\t\t\t\t\t(stroke (width 0) (type default)) (fill (type none))\n'
        '\t\t\t\t)\n'
        '\t\t\t)\n'
        '\t\t)\n'
    )


# ---------------------------------------------------------------------------
# Top-level render
# ---------------------------------------------------------------------------

def _recenter_comps_to_paper(comps: List["PlacedComp"], paper: str) -> int:
    """Translate every placed component so the content bounding box centre
    lands on the real paper centre.

    Why this exists: the placer anchors everything at the module constant
    ``SHEET_CENTRE`` (148.59, 105.41 = A4 centre). When the dynamic paper
    picker chooses A3+ for a multi-block single sheet, content built around
    the A4 centre clusters in the TOP-LEFT corner of the bigger sheet. This
    pass re-centres after placement so the design sits in the middle of
    whatever paper it actually renders on.

    Positions-only: wires, labels and junctions are derived downstream from
    component pin positions (PlacedComp.pin_abs reads self.pos at call time),
    so shifting pos here moves everything with it. Returns the number of
    components moved (0 = disabled, empty, or already centred).

    Config: ``recenter_content`` in layout_config.json. enabled=false gives
    byte-identical legacy output."""
    cfg = _load_layout_config().get("recenter_content", {}) or {}
    if not cfg.get("enabled", False) or not comps:
        return 0
    boxes = [_abs_outer_bbox(c) for c in comps]
    min_x = min(b[0] for b in boxes)
    min_y = min(b[1] for b in boxes)
    max_x = max(b[2] for b in boxes)
    max_y = max(b[3] for b in boxes)
    content_cx = (min_x + max_x) / 2.0
    content_cy = (min_y + max_y) / 2.0

    page_w, page_h = _page_dims(paper)
    target_cx = page_w / 2.0
    if cfg.get("clear_title_block", False):
        # Centre in the band above the bottom-right title block so large
        # designs never overlap it; pull the reserved height from config.
        hier = _load_layout_config().get("hierarchy_layout", {}) or {}
        tb = hier.get("title_block_reserve_mm", {}) or {}
        title_h = float(tb.get("height_mm", 30.0))
        target_cy = (page_h - title_h) / 2.0
    else:
        target_cy = page_h / 2.0

    dx = target_cx - content_cx
    dy = target_cy - content_cy

    g = float(cfg.get("snap_grid_mm", GRID)) or GRID
    dx = round(dx / g) * g
    dy = round(dy / g) * g

    min_shift = float(cfg.get("min_shift_mm", GRID))
    if abs(dx) < min_shift and abs(dy) < min_shift:
        return 0
    for c in comps:
        c.pos = (c.pos[0] + dx, c.pos[1] + dy)
    return len(comps)


def _keep_inside_sheet(comps: List["PlacedComp"], paper: str) -> int:
    """HARD-RULE safety net: guarantee the finished design stays INSIDE the
    printable sheet border and clear of the bottom-right title block.

    Runs dead-last, after every placement + relocation pass (decoupling /
    crystal hug, connector-edge snap, block reflow + title-block lift).
    Those passes move individual parts to satisfy local rules and can nudge
    the content bounding box past the page edge that the initial grid
    centring kept it inside — there was no final check that the FINISHED
    layout actually fits the sheet, which is exactly the
    components-cross-the-border defect. This pass measures the final content
    bbox and, when it crosses any border (or the title block), rigidly
    TRANSLATES every component the minimum on-grid distance needed to bring
    it back in-bounds.

    Translation only — never scales — so the 100-mil grid and every wire /
    label / junction (all derived downstream from pin positions) are
    preserved exactly; they follow the shift automatically.

    Clamp, NOT centre: a design already inside the border is left untouched
    (the common A4 single-IC case is a no-op -> byte-identical legacy
    output), so this is safe to leave enabled. When the content is genuinely
    larger than the usable area on an axis (a design that should have been
    split across sheets), it is aligned to the top / left so the
    reading-start corner stays on-page and the unavoidable overflow falls to
    the bottom / right.

    Config: ``keep_inside_sheet`` in layout_config.json. enabled=false ->
    no-op. Implements [[feedback_layout_within_sheet]]."""
    cfg = _load_layout_config().get("keep_inside_sheet", {}) or {}
    if not cfg.get("enabled", True) or not comps:
        return 0

    # Margins default to the placer's page margin so the safety net agrees
    # with where the grid already tries to keep things; per-edge override
    # in JSON. A looser margin than the placer means this only fires on a
    # genuine border crossing, never on a normal centred layout.
    default_margin = _placement_cfg()["page_margin_mm"]
    m = cfg.get("margin_mm", {}) or {}
    ml = float(m.get("left", default_margin))
    mr = float(m.get("right", default_margin))
    mt = float(m.get("top", default_margin))
    mb = float(m.get("bottom", default_margin))

    page_w, page_h = _page_dims(paper)
    ux1, uy1 = ml, mt
    ux2, uy2 = page_w - mr, page_h - mb

    # Reserve the title-block band at the bottom so content never lands on
    # the bottom-right title block. Full-width reserve is the safe
    # simplification (the block is only bottom-right, but reserving the band
    # guarantees clearance with a single rectangular clamp).
    if cfg.get("clear_title_block", True):
        tb = _title_block_obstacle(paper)
        if tb is not None:
            uy2 = min(uy2, tb[1])   # tb[1] = title-block top edge

    # Measure the field-text-aware bbox so a part whose BODY is in-bounds but
    # whose RefDes/Value text spills over the border / title block is still
    # pulled back (the single-sheet-with-blocks overflow). include_field_text
    # defaults true; set false in JSON for byte-identical body-only legacy.
    _bbox = (_abs_bbox_with_fields if cfg.get("include_field_text", True)
             else _abs_outer_bbox)
    boxes = [_bbox(c) for c in comps]
    min_x = min(b[0] for b in boxes); min_y = min(b[1] for b in boxes)
    max_x = max(b[2] for b in boxes); max_y = max(b[3] for b in boxes)

    eps = 0.01

    def _axis_shift(lo: float, hi: float, u_lo: float, u_hi: float) -> float:
        # Too big for the usable span: align to the low (top/left) edge.
        if (hi - lo) - (u_hi - u_lo) > eps:
            return u_lo - lo
        if lo < u_lo - eps:
            return u_lo - lo          # crosses low edge: push toward high
        if hi > u_hi + eps:
            return u_hi - hi          # crosses high edge: push toward low
        return 0.0

    dx = _axis_shift(min_x, max_x, ux1, ux2)
    dy = _axis_shift(min_y, max_y, uy1, uy2)

    # Snap the shift magnitude UP to a whole grid step so (a) every part
    # stays on the 100-mil grid and (b) we always fully clear the border
    # rather than stopping a fraction short.
    g = float(cfg.get("snap_grid_mm", GRID)) or GRID
    if dx:
        dx = math.copysign(math.ceil(abs(dx) / g) * g, dx)
    if dy:
        dy = math.copysign(math.ceil(abs(dy) / g) * g, dy)
    if not dx and not dy:
        return 0
    for c in comps:
        c.pos = (c.pos[0] + dx, c.pos[1] + dy)
    return len(comps)


def _grow_paper_to_fit(comps: List["PlacedComp"], paper: str) -> str:
    """If the FINISHED content bbox is larger than the usable area of `paper`,
    return the smallest LARGER page in `hierarchy_layout.page_size_priority`
    that fits; otherwise return `paper` unchanged.

    Why: `_keep_inside_sheet`'s last-resort branch deliberately lets a design
    that is genuinely too big for the page overflow the bottom/right (it only
    translates, never resizes). On a single flat sheet the correct first move
    — per KiCad practice — is to grow the page (A4→A3) before giving up. This
    measures the real post-placement geometry (text-aware), unlike the
    count-based `auto_paper` picker which is deliberately off because it ran
    BEFORE placement and clustered content. Bounded by the priority list
    (A5..A3 by default — never A2/A1, per L12); never shrinks. When nothing
    larger fits, returns `paper` and the existing clamp top/left-aligns.
    Gated by `keep_inside_sheet.grow_page_if_overflow` (default true)."""
    cfg = _load_layout_config().get("keep_inside_sheet", {}) or {}
    if not cfg.get("grow_page_if_overflow", True) or not comps:
        return paper
    hier = _load_layout_config().get("hierarchy_layout", {}) or {}
    priority = hier.get("page_size_priority", ["A5", "A4", "A3"]) or ["A4"]
    papers = hier.get("page_sizes", {}) or {}

    default_margin = _placement_cfg()["page_margin_mm"]
    m = cfg.get("margin_mm", {}) or {}
    ml = float(m.get("left", default_margin)); mr = float(m.get("right", default_margin))
    mt = float(m.get("top", default_margin));  mb = float(m.get("bottom", default_margin))
    tb_h = 0.0
    if cfg.get("clear_title_block", True):
        tb_h = float((hier.get("title_block_reserve_mm", {}) or {}).get("height_mm", 30.0))

    _bbox = (_abs_bbox_with_fields if cfg.get("include_field_text", True)
             else _abs_outer_bbox)
    boxes = [_bbox(c) for c in comps]
    content_w = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    content_h = max(b[3] for b in boxes) - min(b[1] for b in boxes)

    def _fits(name: str) -> bool:
        p = papers.get(name)
        if not p:
            return False
        uw = float(p["w_mm"]) - ml - mr
        uh = float(p["h_mm"]) - mt - mb - tb_h
        return content_w <= uw + 0.01 and content_h <= uh + 0.01

    def _area(name: str) -> float:
        p = papers.get(name)
        return float(p["w_mm"]) * float(p["h_mm"]) if p else 0.0

    if _fits(paper):
        return paper
    cur_area = _area(paper)
    for name in sorted((n for n in priority
                        if papers.get(n) and _area(n) > cur_area), key=_area):
        if _fits(name):
            return name
    return paper


def _flat_layout_overflows(comps: List["PlacedComp"], paper: str) -> bool:
    """True when the finished FLAT content is physically larger than the usable
    area of ``paper`` (page minus margins minus the bottom-right title-block
    reserve) on either axis.

    The count-based ``sheet_decision`` (block count, component count) is only a
    PROXY for "fits one sheet". A board can pass it (e.g. a 64-pin STM32 + full
    decoupling + dual crystals: ~30 parts in 5 blocks) yet lay out far larger
    than A3. ``_keep_inside_sheet`` can only TRANSLATE the design, so it
    top/left-aligns and the surplus falls off the bottom-right border — the
    "circuit outside the sheet" defect ([[feedback_layout_within_sheet]]).

    Measured exactly like ``_keep_inside_sheet`` (same margins, same title-block
    reserve, same field-text-aware bbox) so the verdict matches what that clamp
    can actually contain. Size only — independent of where the content currently
    sits. ``render_flat`` calls this AFTER growing to the page-size cap to decide
    whether to split an over-large board into a multi-sheet hierarchy."""
    if not comps:
        return False
    kic = _load_layout_config().get("keep_inside_sheet", {}) or {}
    default_margin = _placement_cfg()["page_margin_mm"]
    m = kic.get("margin_mm", {}) or {}
    ml = float(m.get("left", default_margin)); mr = float(m.get("right", default_margin))
    mt = float(m.get("top", default_margin));  mb = float(m.get("bottom", default_margin))
    page_w, page_h = _page_dims(paper)
    usable_w = page_w - ml - mr
    usable_h = page_h - mt - mb
    if kic.get("clear_title_block", True):
        tb = _title_block_obstacle(paper)
        if tb is not None:
            usable_h = min(usable_h, tb[1] - mt)
    _bbox = (_abs_bbox_with_fields if kic.get("include_field_text", True)
             else _abs_outer_bbox)
    boxes = [_bbox(c) for c in comps]
    content_w = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    content_h = max(b[3] for b in boxes) - min(b[1] for b in boxes)
    sd = _load_layout_config().get("sheet_decision", {}) or {}
    tol = float(sd.get("geometric_overflow_tol_mm", 1.0))
    return content_w > usable_w + tol or content_h > usable_h + tol


@traceable(run_type="chain", name="Build schematic (single sheet)")
def render_flat(ir: TopologyIR, out_path: Path) -> Dict[str, Any]:
    """Render a TopologyIR to a single .kicad_sch file. Returns a stats dict.

    Paper choice: `_paper_for_ir(ir)` picks A4 for single-IC flat and
    A3 (or whatever `hierarchy_layout.single_sheet_with_blocks_paper`
    is set to in JSON) when the IR has blocks. Threaded into header +
    title-block obstacle so placement + routing both see the right
    sheet dimensions."""
    _reset_label_registry()   # R11: fresh per-render label anchors
    file_uuid = _u()
    paper = _paper_for_ir(ir)
    comps = _place_components(ir, paper=paper)

    # Post-placement decoupling-cap relocator.
    # _place_components produces a positions list but does NOT pull
    # decoupling caps adjacent to their target IC's VDD pin on the
    # flat single-block path (only the zoned path does). Without this
    # call, a 100 nF cap could land 60+ mm from the IC's VCC — visible
    # in the ATtiny85-dimmer build before fix. Gated by
    # multi_block.decoupling.apply_to_flat_single_block in JSON so
    # legacy circuits can revert to the byte-identical earlier output.
    # Force=True bypasses the function's own ir.blocks==[] early-return.
    _reloc_cfg = _load_layout_config().get("multi_block", {}).get(
        "decoupling", {})
    if (not ir.blocks
            and _reloc_cfg.get("enabled", False)
            and _reloc_cfg.get("apply_to_flat_single_block", True)):
        # Build placed_map / placed_bboxes from the comps list — these
        # are the structures _relocate_decoupling_caps mutates. The
        # comps list itself holds the SAME PlacedComp references so
        # downstream code automatically sees the updated positions.
        placed_map = {c.ref: c for c in comps}
        placed_bboxes = [_candidate_abs_bbox(c.geom, c.pos, c.rotation)
                         for c in comps]
        _relocate_decoupling_caps(placed_map, placed_bboxes, ir, force=True)
        _relocate_crystals(placed_map, placed_bboxes, ir, force=True)
        _apply_layout_cluster_rules(placed_map, placed_bboxes, ir)

    # Universal connector-edge placement (no per-circuit data — pure
    # net-name pattern matching against config). Snaps input
    # connectors to the LEFT edge, output to RIGHT, debug headers to
    # BOTTOM-RIGHT, so single-IC schematics follow the
    # left-to-right-signal-flow rule even without explicit IRBlock
    # flow_role hints. Multi-block circuits with block flow_role
    # already get this from the zoned placer — pass is gated on
    # `connector_edge_placement.enabled` so it can be reverted.
    _connector_edge_pass(ir, comps, paper)

    # Separate functional blocks whose COMPONENTS physically overlap, so the
    # block rectangles don't draw on top of each other ("block on top of
    # block"). Moves whole blocks rigidly; must run before block-rect
    # drawing AND before re-centring so both finish on a separated layout.
    _separate_overlapping_blocks(comps, ir)

    # If the finished layout is genuinely larger than the chosen page, grow
    # the page (A4->A3, bounded by page_size_priority) BEFORE re-centring /
    # clamping so the design fits instead of being shoved off the bottom-right
    # edge. Measured on real geometry; no-op when it already fits. Re-binds
    # `paper` so the header + title-block obstacle below use the grown size.
    paper = _grow_paper_to_fit(comps, paper)

    # Page-aware re-centring (runs LAST, after every placement/edge pass so
    # it measures the final content bbox). Fixes the top-left-corner cluster
    # when the dynamic paper picker chose A3+ but placement was anchored at
    # the fixed A4 SHEET_CENTRE. Positions-only -> wires/labels follow.
    _recenter_comps_to_paper(comps, paper)

    # HARD RULE ([[feedback_layout_within_sheet]]): final border guarantee.
    # Runs after recenter + every relocation pass so it measures the truly
    # final bbox and clamps any out-of-bounds design back inside the sheet
    # border / off the title block. No-op when already in-bounds.
    _keep_inside_sheet(comps, paper)

    # HARD RULE ([[feedback_layout_within_sheet]]) — geometric escalation to
    # multi-sheet. The count-based sheet_decision can route a board to a single
    # flat sheet that is none-the-less physically too large for the page once
    # laid out (a 64-pin MCU + full decoupling + dual crystals: few parts and
    # blocks, but big geometry). _grow_paper_to_fit has already grown the page
    # as far as the A3 cap allows (A2/A1 are banned, L12) and _keep_inside_sheet
    # can only TRANSLATE — so an over-large design is top/left-aligned and the
    # surplus spills off the bottom-right border ("circuit outside the sheet").
    # When the finished content STILL overflows the (grown, capped) page and the
    # board has >= 2 functional blocks, split it into a hierarchy instead of
    # emitting an off-sheet design. Geometry is authoritative over the count
    # proxy. Gated by sheet_decision.escalate_on_geometric_overflow (default
    # true). No recursion: render_hierarchical renders children via
    # _render_block_child, never render_flat with a multi-block IR.
    _sd_cfg = _load_layout_config().get("sheet_decision", {}) or {}
    if (_sd_cfg.get("escalate_on_geometric_overflow", True)
            and len(getattr(ir, "blocks", None) or []) >= 2
            and _flat_layout_overflows(comps, paper)):
        return render_hierarchical(ir, out_path.parent)

    comps_by_ref = {c.ref: c for c in comps}

    # Collect power rails so we can inline their symbols
    power_rails = {n.name for n in ir.nets if n.is_power}

    body = ""
    body += _emit_header(file_uuid, title=ir.name, paper=paper)
    body += _emit_lib_symbols(comps, power_rails=power_rails)

    # Component instances
    for c in comps:
        body += _emit_symbol_instance(c, file_uuid)

    # Build the obstacle list once — every placed component's outer bbox.
    obstacles = [_abs_outer_bbox(c) for c in comps]

    # Foreign-pin short guard: a wire must never run THROUGH a pin that
    # belongs to a DIFFERENT net (KiCad mid-span-bonds it = short — the
    # +12V/GND and XTAL1/XTAL2 shorts). Collect every component pin's
    # absolute position ONCE; per net we subtract that net's own pins and
    # hand the rest to _route_l_aware as `foreign_pts`. Gated by
    # routing.avoid_pin_shorts (default on) — off => foreign_pts stays
    # None => byte-identical legacy routing.
    _avoid_pin_shorts = bool(
        _load_layout_config().get("routing", {}).get("avoid_pin_shorts", True))
    all_pin_abs: List[Tuple[float, float]] = []
    if _avoid_pin_shorts:
        for _c in comps:
            for _pin in (_c.geom.pins or []):
                _ap = _c.pin_abs(_pin.number)
                if _ap is not None:
                    all_pin_abs.append((_ap[0], _ap[1]))
    # R11 unified de-collision: seed the ink registry with body+field
    # rectangles so labels and power ports place their TEXT clear of every
    # part and its RefDes/Value (no-op when the pass is disabled).
    _seed_ink_from_comps(comps)
    # Virtual obstacle: KiCad's title-block reserve at the bottom-right.
    # Treated like any other bbox — _route_l_aware refuses to route a
    # wire whose segment midpoint lies inside it, so green wires never
    # cross the red title-block frame again. Paper from _paper_for_ir
    # so A3 single-sheet-with-blocks renders use A3 title-block dims.
    tb_obs = _title_block_obstacle(paper)
    if tb_obs is not None:
        obstacles.append(tb_obs)

    # Track which (ref, pin_number) appear in some net so we can emit
    # no_connect on the rest.
    pins_in_any_net: set = set()

    wires_emitted = 0
    labels_emitted = 0
    junctions_emitted = 0
    nc_flags_emitted = 0
    # PWR_FLAG emission tracking: one flag per power net is enough —
    # KiCad bonds the flag to every rail pin via shared net name. The
    # set guards against multiple flags on the same rail (which would
    # themselves trigger 'isolated_pin_label' warnings).
    pwr_flag_nets: set = set()
    flag_count = 0
    # Ground-rail name lookup (data-driven, same JSON list the
    # decoupling analyser uses) — needed for the PWR_FLAG placement
    # below to choose UP vs DOWN clearance relative to the power port.
    _gnd_cfg = (_load_layout_config().get("multi_block", {})
                .get("decoupling", {}))
    _gnd_names_eng = {s.upper() for s in _gnd_cfg.get("ground_net_names", [
        "GND", "AGND", "DGND", "PGND", "VSS", "VEE", "EGND", "SGND"])}
    # Track every wire segment emitted by PRIOR nets so the router can
    # rank L variants by crossing count for the CURRENT net. Same-net
    # segments are NOT added until that net's loop finishes, otherwise
    # a net's own internal stub would count as a crossing with itself.
    emitted_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []

    # Build ref -> block name map ONCE — reused for the per-block power
    # clustering and the cross-block signal-wire guard below.
    _ref_to_block_g: Dict[str, str] = {}
    for _blk in ir.blocks:
        for _r in _blk.component_refs:
            _ref_to_block_g[_r] = _blk.name

    # Nets: wire pin-to-pin + add labels for power
    for net in ir.nets:
        pin_positions: List[Tuple[float, float, float]] = []
        pin_refs: List[str] = []  # parallel to pin_positions
        for pinref in net.pins:
            if "." not in pinref:
                continue
            ref, pin_key = pinref.split(".", 1)
            comp = comps_by_ref.get(ref)
            if comp is None:
                continue
            abs_pos = comp.pin_abs(pin_key)
            if abs_pos is None:
                continue
            pin_positions.append(abs_pos)
            pin_refs.append(ref)
            pin = comp.geom.resolve_pin(pin_key)
            if pin is not None:
                pins_in_any_net.add((ref, pin.number))

        # Foreign pins for THIS net = every component pin NOT on this net.
        # The router refuses to run a wire through any of them (mid-span
        # bond = short). own_xy holds this net's own pin coords so a wire
        # may still pass through its OWN pins (a legitimate junction).
        foreign_pts: Optional[List[Tuple[float, float]]] = None
        if _avoid_pin_shorts:
            own_xy = {(round(pp[0], 2), round(pp[1], 2))
                      for pp in pin_positions}
            foreign_pts = [(x, y) for (x, y) in all_pin_abs
                           if (round(x, 2), round(y, 2)) not in own_xy]

        if net.is_power:
            # Cluster pins by proximity: pins within `radius` share ONE
            # power-port symbol (with wires joining them to the port).
            #
            # Single-circuit (no blocks) ⇒ force ALL rail pins into ONE
            # cluster regardless of distance. Rule
            # [feedback_wires_inside_labels_between]: "single circuit =
            # ONE logical block, use wires not labels." With the default
            # 12.7 mm radius, a flat 7-component LDO with parts spread
            # 30–40 mm apart produces 4–6 separate power ports per rail,
            # making the sheet look label-heavy.
            #
            # Multi-block ⇒ ONE port per (rail, block) regardless of
            # distance — partition pins by their owning block, then
            # cluster within each partition with an unbounded radius.
            # Same rule: "wires INSIDE a block." A block with 3 pins on
            # +3V3 spread 30 mm apart used to produce 3 separate +3V3
            # ports per block; now produces 1 port + 2 wires inside.
            # Cross-block pins (same rail, different block) land in
            # SEPARATE partitions so each block gets its own port —
            # KiCad's net-by-label bonds the ports across blocks.
            # JSON-gated by `engine.multi_block_one_port_per_rail_block`
            # (default true) and `engine.single_block_one_port_per_rail`
            # (default true).
            _eng_cfg2 = _load_layout_config().get("engine", {}) or {}
            # Single-block proximity split (reference rule "decoupling caps
            # near IC power pins, use power symbols"): when
            # `single_block_power_cluster_radius_mm` > 0, spread-out rail
            # pins get their OWN local power symbol + short stub instead of
            # all wiring to one shared port. That one shared port is what
            # produced the long GND bus the +V drops had to CROSS. Local
            # ports = no long bus = no crossing, exactly like the user's
            # reference power-supply block. Radius 0 keeps the legacy
            # one-port-per-rail behaviour (non-breaking default override).
            # When > 0 this radius makes spread-out rail pins split into
            # local power-symbol clusters (no long shared bus). It applies
            # BOTH to no-block circuits AND *within* each block partition —
            # a single large "POWER" block (ir.blocks == [POWER]) otherwise
            # takes the one-port-per-block path and rebuilds the same long
            # GND bus the +V drops cross.
            _sb_radius = float(_eng_cfg2.get(
                "single_block_power_cluster_radius_mm", 0.0))
            if not ir.blocks and _sb_radius > 0 and pin_positions:
                clusters = _cluster_power_pins(pin_positions, radius=_sb_radius)
            elif (not ir.blocks
                    and _eng_cfg2.get("single_block_one_port_per_rail", True)):
                clusters = [pin_positions] if pin_positions else []
            elif (ir.blocks
                    and _eng_cfg2.get("multi_block_one_port_per_rail_block", True)):
                # Partition pins by owning block (refs without a block
                # land in a special "" bucket so they still get a port),
                # then proximity-split each partition when _sb_radius > 0
                # so a big block doesn't draw one cross-sheet rail bus.
                _by_block: Dict[str, List[Tuple[float, float, float]]] = {}
                for _pp, _ref in zip(pin_positions, pin_refs):
                    _b = _ref_to_block_g.get(_ref, "")
                    _by_block.setdefault(_b, []).append(_pp)
                if _sb_radius > 0:
                    clusters = []
                    for _part in _by_block.values():
                        clusters.extend(
                            _cluster_power_pins(_part, radius=_sb_radius))
                else:
                    clusters = list(_by_block.values())
            else:
                _cluster_radius = _placement_cfg()["power_cluster_radius_mm"]
                clusters = _cluster_power_pins(
                    pin_positions,
                    radius=_cluster_radius)
            for cluster in clusters:
                # Place ONE port per cluster — anchor it at the pin
                # closest to the cluster centre, then wire all others
                # to it.
                cx = sum(p[0] for p in cluster) / len(cluster)
                cy = sum(p[1] for p in cluster) / len(cluster)
                # Anchor pin = the one closest to the cluster centre
                anchor = min(cluster,
                              key=lambda p: math.hypot(p[0]-cx, p[1]-cy))
                ax, ay, arot = anchor
                labels_emitted += 1
                pwr = _emit_power_port(net.name, (ax, ay), file_uuid,
                                         f"#PWR{labels_emitted:03d}",
                                         decollide=_ink_decollide_on())
                if pwr is None:
                    body += _emit_global_label(net.name, (ax, ay), arot)
                else:
                    body += pwr
                # Emit ONE PWR_FLAG per power net (on its FIRST cluster)
                # ONLY when the rail has no native power_output driver.
                # KiCad treats a rail as 'driven' when ANY pin on it is
                # of type power_output (regulator VOUT, battery cell,
                # USB VBUS pin in some symbol libraries). Emitting a
                # second power_output via PWR_FLAG on top of that fires
                # 'Pins of type Power output and Power output are
                # connected' — a real ERC error, not a benign warning.
                # The rail needs a flag ONLY when every pin is
                # power_input / bidirectional / passive — i.e. nothing
                # actively drives it. Check is symbol-driven (etype
                # from KiCad lib), no per-rail hardcoding.
                if pwr is not None and net.name not in pwr_flag_nets:
                    pwr_flag_nets.add(net.name)
                    has_native_driver = False
                    for _pinref in net.pins:
                        if "." not in _pinref:
                            continue
                        _r, _k = _pinref.split(".", 1)
                        _c = comps_by_ref.get(_r)
                        if _c is None:
                            continue
                        _p = _c.geom.resolve_pin(_k)
                        if _p is None:
                            continue
                        if (_p.etype or "").lower() in ("power_out",
                                                          "output"):
                            has_native_driver = True
                            break
                    if not has_native_driver:
                        flag_count += 1
                        is_gnd_rail = net.name.upper().lstrip("+") in _gnd_names_eng
                        port_pin_y = ay + (2.54 if is_gnd_rail else -2.54)
                        port_pin = (ax, port_pin_y)
                        # Collision-aware placement — scan 4 cardinal
                        # offsets, pick the first that doesn't overlap
                        # a component bbox. Replaces the fixed +7.62
                        # right offset that landed PWR_FLAG on USB-C
                        # CC2 pins in dense connector clusters.
                        flag_pin = _find_clear_flag_offset(
                            ax, ay, port_pin_y, obstacles)
                        body += _emit_pwr_flag(net.name, flag_pin, file_uuid,
                                                f"#FLG{flag_count:03d}")
                        if _ink_decollide_on():
                            _register_ink(_centered_text_bbox(
                                "PWR_FLAG", flag_pin[0], flag_pin[1] + 2.54))
                        body += _emit_wire(port_pin, flag_pin)
                        wires_emitted += 1
                # Join other pins in this cluster to the anchor with
                # body-aware L-route wires. Skip wires whose only safe
                # path would pierce a component — the labels (emitted
                # below per net) still bond the pins by name.
                # Wires emitted WITHIN this power-net cluster are own-net,
                # so they're collected locally and merged into emitted_wires
                # AFTER the cluster loop finishes — see end of net block.
                net_local_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
                for (px, py, _prot) in cluster:
                    if (px, py) == (ax, ay):
                        continue
                    path = _route_l_aware((px, py), (ax, ay), obstacles,
                                            existing_wires=emitted_wires,
                                            foreign_pts=foreign_pts)
                    if not path:
                        continue  # no safe route — labels bond by name
                    for k in range(len(path) - 1):
                        body += _emit_wire(path[k], path[k+1])
                        net_local_wires.append((path[k], path[k+1]))
                        wires_emitted += 1
                emitted_wires.extend(net_local_wires)
            continue

        # Signal net wires — body-aware L-routing. Per-net wiring with
        # endpoint exclusions: the two components owning the pins at
        # each end MUST be allowed as path endpoints (the wire HAS to
        # touch them). Single-IC circuits (NE555, LM386, LM317, op-amp)
        # use wires everywhere: passives radiate around the IC and
        # connect by drawn lines, NOT scattered labels. That's the
        # user's reference convention.
        #
        # MULTI-BLOCK RULE (durable user feedback): when ir.blocks is
        # populated, any pin pair whose owners belong to DIFFERENT
        # blocks gets NO drawn wire — labels bond them by name. Wires
        # stay only for INSIDE-block connections (R->C, IC->cap). This
        # is purely IR-data-driven: no IC names, no circuit names, no
        # hardcoding. Gated by `multi_block.cross_block_signal_wires`
        # (default false = enforce the rule).
        pin_components: List[Optional[PlacedComp]] = []
        for pinref in net.pins:
            if "." not in pinref:
                pin_components.append(None); continue
            ref = pinref.split(".", 1)[0]
            pin_components.append(comps_by_ref.get(ref))
        ref_to_block = _ref_to_block_g  # reuse the one built above
        mb_cfg = _load_layout_config().get("multi_block", {})
        allow_cross_block_wires = bool(mb_cfg.get("cross_block_signal_wires", False))
        any_wire_dropped = False
        # Per-edge drop tracking — the indices of pin_positions[i] AND
        # pin_positions[i+1] for every MST edge whose route_l_aware
        # returned None. These are the pins that need labels to bond
        # by name. Pins whose wire(s) succeeded stay UNLABELED so the
        # schematic reads as direct wire-only — matching the project's
        # "single-IC = wires not labels" rule from the user.
        dropped_pin_indices: set = set()
        # Pins whose only same-block neighbour wire FAILED to route — they
        # still need a label to bond (rare; blocks are compact).
        same_block_unwired: set = set()
        # Local accumulator: this net's own wires get appended to the
        # global emitted_wires list AT THE END of the per-net loop, so
        # the router doesn't see the same net's earlier segments as
        # "other-net" crossings.
        net_local_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []

        # Reorder pins so SAME-BLOCK pins are contiguous before chaining.
        # Rule (generalised by the user): inside ONE unit — a block, a flat
        # single-IC circuit, or one child sheet — connect components with
        # WIRES; labels bridge only BETWEEN units. Without this reorder the
        # arbitrary pin order could place a cross-block pin (e.g. the MCU)
        # BETWEEN two same-block pins (sensor + its I2C pull-up), so the two
        # same-block pins landed on opposite sides of a dropped cross-block
        # edge and each got a label with NO wire between them — exactly the
        # "inside-block used a label" bug. Sorting by block makes same-block
        # pins adjacent so the chain wires them, and only the block→block
        # boundary is bridged by a single per-block label.
        if ir.blocks and not allow_cross_block_wires:
            order = sorted(
                range(len(pin_positions)),
                key=lambda k: ((ref_to_block.get(pin_components[k].ref)
                                if (k < len(pin_components)
                                    and pin_components[k] is not None)
                                else None) or "~~"))
            pin_positions = [pin_positions[k] for k in order]
            pin_components = [pin_components[k] for k in order]
        # Owning block per pin in the (possibly reordered) list.
        block_of: List[Optional[str]] = []
        for k in range(len(pin_positions)):
            pc = pin_components[k] if k < len(pin_components) else None
            block_of.append(ref_to_block.get(pc.ref)
                            if (pc is not None and ir.blocks) else None)
        n_blocks_spanned = len({b for b in block_of if b})

        for i in range(len(pin_positions) - 1):
            p1 = pin_positions[i][:2]
            p2 = pin_positions[i + 1][:2]
            own = []
            c1 = pin_components[i] if i < len(pin_components) else None
            c2 = pin_components[i + 1] if i + 1 < len(pin_components) else None
            for c in (c1, c2):
                if c is not None:
                    own.append(_abs_outer_bbox(c))
            same_block = (block_of[i] is not None
                          and block_of[i] == block_of[i + 1])
            # Cross-block boundary: never draw a wire ACROSS blocks — the
            # single per-block bridge label bonds them by name.
            if (ir.blocks and not allow_cross_block_wires
                    and block_of[i] and block_of[i + 1] and not same_block):
                any_wire_dropped = True
                dropped_pin_indices.update((i, i + 1))
                continue
            path = _route_l_aware(p1, p2, obstacles, own_bboxes=own,
                                    existing_wires=emitted_wires,
                                    foreign_pts=foreign_pts)
            # Long-wire rule: when the routed path exceeds
            # `routing.max_wire_length_mm` (JSON-driven), drop the
            # wire and let the per-pin label loop bond the endpoints
            # by name. Long drawn wires across the sheet are the
            # 'wire shortage' / spaghetti symptom the user flagged —
            # global labels are the canonical professional fix.
            _max_wire_len = _routing_cfg()["max_wire_length_mm"]
            if (path and _max_wire_len > 0
                    and _path_manhattan_length(path) > _max_wire_len):
                path = []
            # Drop wires that have no safe path — labels bond the pins
            # by name. NEVER emit a wire that pierces a component body.
            if not path:
                any_wire_dropped = True
                dropped_pin_indices.update((i, i + 1))
                if same_block:
                    same_block_unwired.update((i, i + 1))
                continue
            for j in range(len(path) - 1):
                body += _emit_wire(path[j], path[j + 1])
                net_local_wires.append((path[j], path[j + 1]))
                wires_emitted += 1
            if 0 < i < len(pin_positions) - 1:
                body += _emit_junction(pin_positions[i][:2])
                junctions_emitted += 1
        emitted_wires.extend(net_local_wires)
        # Label emission policy (universal, no per-circuit data):
        #   (a) Net spans 2+ blocks: ONE bridge label per block (placed on
        #       the first pin of each block group), so each block's pins
        #       are WIRED internally and bonded to the others by name.
        #       Block-less dropped pins + same-block wire failures also get
        #       a label so nothing is left floating. Per the rule
        #       [feedback_wires_inside_labels_between]: WIRES inside a unit,
        #       LABELS between units.
        #   (b) Single block / no blocks + some wires dropped: label the
        #       dropped pins (legacy behaviour).
        #   (c) Single block + ALL wires clean: NO labels. Opt into a single
        #       seed via `engine.single_block_seed_label_when_clean`.
        _eng_cfg = _load_layout_config().get("engine", {}) or {}
        _seed_when_clean = bool(_eng_cfg.get(
            "single_block_seed_label_when_clean", False))
        if n_blocks_spanned >= 2:
            label_indices = []
            seen_blocks: set = set()
            for k in range(len(pin_positions)):
                b = block_of[k]
                if b and b not in seen_blocks:
                    seen_blocks.add(b)
                    label_indices.append(k)
            for k in sorted(dropped_pin_indices | same_block_unwired):
                if (block_of[k] is None or k in same_block_unwired) \
                        and k not in label_indices:
                    label_indices.append(k)
        elif any_wire_dropped:
            label_indices = sorted(dropped_pin_indices)
            # Always seed at least one label on the dropped-pair case
            # so the bond-by-name is unambiguous.
            if 0 not in label_indices:
                label_indices = [0] + label_indices
        elif pin_positions and _seed_when_clean:
            label_indices = [0]   # opt-in single seed
        else:
            label_indices = []    # ZERO labels — wires only
        for i in label_indices:
            if i >= len(pin_positions):
                continue
            x, y, rot = pin_positions[i]
            ci = pin_components[i] if i < len(pin_components) else None
            own_bbox = _abs_outer_bbox(ci) if ci is not None else None
            # Pass emitted_wires so R11 label-vs-wire overlap (gated by
            # wiring_rules.r11_avoid_label_wire_overlap) can lengthen the
            # stub when a label would otherwise sit on top of a wire.
            body += _emit_local_label(net.name, (x, y), rot,
                                        obstacles=obstacles,
                                        own_bbox=own_bbox,
                                        existing_wires=emitted_wires,
                                        foreign_pts=foreign_pts)
            labels_emitted += 1

    # Phase 4 post-pass: inter-block wires. For power nets whose pins
    # span 2+ blocks, draw L-route wires between block representatives so
    # the visual signal flow is continuous (labels alone aren't readable
    # at the block-to-block scale). Gated by multi_block.interblock_wires
    # — no-op (byte-identical to PR #3) when disabled. Drops piercing
    # paths silently so a bad route never ships.
    ibw_count, ibw_body = _emit_interblock_wires_block(ir, comps, obstacles)
    body += ibw_body
    wires_emitted += ibw_count

    # Functional-block rectangles: ONLY draw them when each block has
    # its own multi-pin IC (the STM32-style "POWER + MCU + MEMORY + ..."
    # layout). For single-IC circuits like NE555 / LM317 where blocks
    # are just functional groupings of passives around one anchor,
    # rectangles are visual noise — skip them.
    # Three-tier user policy (2026-06-02):
    #   single IC      (0-1 blocks) -> single sheet, WIRES, NO rectangles
    #   single + block (2-5 blocks) -> single sheet WITH block rectangles
    #   multiple sheet (>5 blocks)  -> handled by render() (never reaches here)
    # A lone block is just a single IC + its passives -> drawing one box is
    # noise, so require >= 2 blocks. An explicit "block diagram" request
    # (force_block_rects) still boxes everything.
    draw_block_rectangles = (
        len(ir.blocks) >= 2
        or bool(_RENDER_OPTS.get("force_block_rects"))
    )
    # Per-block rectangle gate (rebuilt 2026-06-02): all-or-nothing
    # `all(_block_has_own_anchor(...))` made a single 2-pin-anchor
    # block (CLOCK with Y1, or INDICATOR with D1+R) suppress rectangles
    # for the ENTIRE sheet. New behaviour: each block decided
    # independently against the same JSON exemptions the validator
    # uses --- passives_only_blocks (CLOCK, INDICATOR, RESET, ...)
    # plus single_connector_blocks (ICSP=[J2], SWD=[J3], ...).
    if draw_block_rectangles:
        # Collect (index, block, padded_bbox, content_bbox) for every
        # block that qualifies for a rectangle, then run a de-overlap
        # pass so two block boxes never draw on top of each other.
        _qual: List[Tuple[int, Any, Tuple[float, float, float, float],
                           Tuple[float, float, float, float]]] = []
        for blk_idx, blk in enumerate(ir.blocks, start=1):
            if not _block_qualifies_for_rectangle(blk, ir):
                continue
            bb = _block_bbox_for_components(comps, blk.component_refs)
            if bb is None:
                continue
            content = _block_bbox_for_components(
                comps, blk.component_refs, pad=0.0, min_size=(0.0, 0.0))
            _qual.append((blk_idx, blk, bb, content or bb))
        _deov = bool((_load_layout_config().get("block_rect", {}) or {})
                     .get("deoverlap", True))
        if _deov and len(_qual) > 1:
            _fixed = _resolve_block_rect_overlaps(
                [q[2] for q in _qual], [q[3] for q in _qual])
            _qual = [(q[0], q[1], _fixed[k], q[3]) for k, q in enumerate(_qual)]
        for blk_idx, blk, bb, _content in _qual:
            body += _emit_block_rectangle(blk.name, bb, index=blk_idx)

    # NC flags: auto-emit (no_connect) on every unused pin whose etype
    # is in nc_policy.auto_nc_etypes. KiCad's pin_not_connected ERC rule
    # fires on input/bidirectional/passive pins with no net — the
    # KLC-canonical fix is an (no_connect) marker on each one. With
    # this in place the user doesn't need to Ctrl-Q every unused MCU
    # GPIO manually. Pin etype list is in JSON, no part-number / circuit
    # hardcoding. power_in / power_out pins are intentionally NOT in
    # the auto list — those are real errors that need a fix at the IR
    # level (wire to source, or add PWR_FLAG at the user's option).
    nc_pol = _nc_policy()
    if nc_pol["enabled"]:
        nc_etypes = nc_pol["auto_nc_etypes"]
        unit_filter = nc_pol["unit_filter_enabled"]
        instance_unit = nc_pol["instance_unit"]
        for c in comps:
            for pin in c.geom.pins:
                if (c.ref, pin.number) in pins_in_any_net:
                    continue
                if pin.etype not in nc_etypes:
                    continue
                # Multi-unit guard: only NC pins from the rendered unit
                # OR unit-0 (shared). Without this, LM358/LM324-style
                # symbols get phantom X marks at un-rendered units'
                # coordinates, visually overlapping the active unit.
                if unit_filter and pin.unit not in (0, instance_unit):
                    continue
                abs_pos = c.pin_abs(pin.number)
                if abs_pos is None:
                    continue
                body += _emit_no_connect((abs_pos[0], abs_pos[1]))
                nc_flags_emitted += 1
    else:
        # Legacy behavior: only NC pins the library explicitly marks.
        for c in comps:
            for pin in c.geom.pins:
                if (c.ref, pin.number) in pins_in_any_net:
                    continue
                if pin.etype != "no_connect":
                    continue
                abs_pos = c.pin_abs(pin.number)
                if abs_pos is None:
                    continue
                body += _emit_no_connect((abs_pos[0], abs_pos[1]))
                nc_flags_emitted += 1

    body += _emit_sheet_instances()
    body += ')\n'

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    pro_path = _write_kicad_pro(out_path)

    # Accurate stats by counting actual emitted entries in the body.
    # The per-branch counters above missed power-port stub wires and
    # label stub wires (emitted inside _emit_power_port / _emit_local_label),
    # which produced misleading "1 wire / 23 labels" stats on multi-block
    # circuits where the file actually held 24 wires / 6 labels.
    wires_in_body  = body.count("(wire (")
    labels_in_body = body.count("(label ") + body.count("(global_label ")
    return {
        "path": str(out_path),
        "project": str(pro_path),
        "components_emitted": len(comps),
        "wires_emitted":   wires_in_body if wires_in_body  else wires_emitted,
        "labels_emitted":  labels_in_body if labels_in_body else labels_emitted,
        "power_ports_emitted": labels_emitted,  # the running counter tracked port+label emissions
        "junctions_emitted": junctions_emitted,
        "nc_flags_emitted": nc_flags_emitted,
        "nets": len(ir.nets),
    }


# ---------------------------------------------------------------------------
# Hierarchical render
# ---------------------------------------------------------------------------

def _subset_ir_for_block(ir: TopologyIR, block_refs: set) -> TopologyIR:
    """Carve out a sub-IR containing only the components in `block_refs`
    and only the pins of each net that fall on those components."""
    sub_components = [c for c in ir.components if c.ref in block_refs]
    sub_nets = []
    for net in ir.nets:
        pins_in = [p for p in net.pins
                   if "." in p and p.split(".", 1)[0] in block_refs]
        if pins_in:
            sub_nets.append(IRNet(name=net.name, pins=pins_in, is_power=net.is_power))
    return TopologyIR(
        name=ir.name, circuit_type=ir.circuit_type,
        components=sub_components, nets=sub_nets, blocks=[], notes=ir.notes,
    )


@traceable(run_type="chain", name="Build sub-sheet")
def _render_block_child(ir: TopologyIR, hierarchical_net_names: set,
                         out_path: Path, parent_title: str = "",
                         page_label: str = "",
                         pwr_start: int = 0,
                         child_title: str = "",
                         cross_label_kind: str = "hierarchical",
                         globally_driven_rails: Optional[set] = None,
                         flagged_rails: Optional[set] = None,
                         flg_start: int = 0) -> Tuple[str, int, int]:
    """Render one child sheet. Same algorithm as render_flat but nets
    listed in `hierarchical_net_names` emit a hierarchical_label at the
    pin tip (in addition to power globals / local labels).

    `parent_title` -- kept for back-compat with older callers; used as
    the title-block "Title:" ONLY when `child_title` is empty.
    `child_title` -- the per-block human-readable title from
    config/block_naming.json (e.g. "MCU Core", "Power Supply"). When
    set, the child's title block shows this instead of the parent's
    project name. Per IEC 61082-1 / ASME Y14.35 each sheet's title
    should describe THAT sheet's function, not the whole project.

    `pwr_start` is the seed for #PWR power-port reference numbering.
    Returns `(file_uuid, next_pwr_index)` — the parent passes the
    returned `next_pwr_index` as the NEXT child's `pwr_start` so every
    #PWR symbol across the hierarchy gets a globally-unique reference.
    Fixes KiCad's "Duplicate items #PWRnnn" annotator error when
    Update PCB is run on a multi-sheet design."""
    _reset_label_registry()   # R11: fresh per-sheet label anchors
    file_uuid = _u()
    comps = _place_components(ir)

    # Pull decoupling caps adjacent to their target IC's VDD pin so the
    # existing power-net cluster logic groups them into ONE shared power
    # port + wires inside this sheet. Without this, the flat placer's
    # CLEARANCE=7.62 push lands caps ~22 mm from the IC pin → outside the
    # 12.7 mm cluster radius → one duplicate +3V3 port per cap pin
    # (visual: scattered labels instead of wires). Force=True is safe:
    # _render_block_child is only reachable from render_hierarchical
    # which requires ir.blocks; the subset ir we receive carries blocks=[]
    # by construction.
    placed_map = {c.ref: c for c in comps}
    placed_bboxes = [_candidate_abs_bbox(c.geom, c.pos, c.rotation) for c in comps]
    _relocate_decoupling_caps(placed_map, placed_bboxes, ir, force=True)
    _relocate_crystals(placed_map, placed_bboxes, ir, force=True)
    _apply_layout_cluster_rules(placed_map, placed_bboxes, ir)

    # Resolve this child's page ONCE, then grow it if the finished content is
    # too big to fit (A4->A3, same measured rule as render_flat). Re-binding
    # child_paper here keeps the header, re-centre, clamp AND title-block
    # obstacle all in agreement — previously the header ignored paper while
    # the obstacle used _paper_for_ir, so a grown child would have mismatched
    # dims.
    child_paper = _grow_paper_to_fit(comps, _paper_for_ir(ir))

    # Page-aware re-centring, same as render_flat. A child carries blocks=[]
    # so it usually renders A4-on-A4 (content already at A4 SHEET_CENTRE ->
    # delta ~0, no move). But a dense child (>30 comps) is promoted to A3 by
    # _paper_for_ir, which would otherwise leave it clustered top-left.
    _recenter_comps_to_paper(comps, child_paper)

    # HARD RULE ([[feedback_layout_within_sheet]]): final border guarantee,
    # same as render_flat. A dense child promoted to A3 (or whose relocation
    # passes pushed a part off-edge) is clamped back inside the sheet.
    _keep_inside_sheet(comps, child_paper)

    comps_by_ref = {c.ref: c for c in comps}

    power_rails = {n.name for n in ir.nets if n.is_power}

    body = ""
    # Title-block "Title:" precedence: per-block child_title (from
    # block_naming.json) > parent_title back-compat arg > ir.name.
    title = child_title or parent_title or ir.name
    body += _emit_header(file_uuid, title=title, page_label=page_label,
                         paper=child_paper)
    body += _emit_lib_symbols(comps, power_rails=power_rails)

    for c in comps:
        body += _emit_symbol_instance(c, file_uuid)

    obstacles = [_abs_outer_bbox(c) for c in comps]
    # Foreign-pin short guard — same rationale as render_flat (a wire or
    # label stub must never run through a pin belonging to a DIFFERENT
    # net). Child sheets previously built no such registry at all, so a
    # 2-pin axial part landing between its own pin and a naive "outward"
    # label stub could short exactly like the flat path could before
    # `avoid_pin_shorts` was added there.
    _avoid_pin_shorts_c = bool(
        _load_layout_config().get("routing", {}).get("avoid_pin_shorts", True))
    all_pin_abs_c: List[Tuple[float, float]] = []
    if _avoid_pin_shorts_c:
        for _c in comps:
            for _pin in (_c.geom.pins or []):
                _ap = _c.pin_abs(_pin.number)
                if _ap is not None:
                    all_pin_abs_c.append((_ap[0], _ap[1]))
    # R11 unified de-collision: seed body+field ink for this child sheet.
    _seed_ink_from_comps(comps)
    # Same title-block reserve guard as render_flat — every child sheet
    # gets a title block, so wires must never cross it. Children carry
    # blocks=[] in their subsetted IR, so _paper_for_ir picks the
    # default_render_paper (A4 unless JSON overridden).
    tb_obs = _title_block_obstacle(child_paper)
    if tb_obs is not None:
        obstacles.append(tb_obs)
    pins_in_any_net: set = set()
    # Seed the #PWR counter from the parent's running tally so each
    # hierarchy child gets a unique block of refdes — eliminates the
    # "Duplicate items #PWRnnn" annotation error.
    pwr_count = int(pwr_start)
    # PWR_FLAG tracking. Legacy (per-sheet) mode: one flag per rail per
    # child, counter restarts at 0 — which across a hierarchy duplicates
    # #FLG refs AND drops flags on rails driven in another sheet. Global
    # mode (hierarchy_global_pwr_flag, threaded from render_hierarchical):
    # `flagged_rails` (shared across ALL children) dedups so each global
    # rail gets at most ONE flag, `flag_count` is seeded from flg_start
    # for globally-unique refs, and the driven-check consults the
    # whole-design `globally_driven_rails` registry instead of just this
    # child's components.
    use_global_flags = globally_driven_rails is not None
    pwr_flag_nets: set = flagged_rails if use_global_flags else set()
    flag_count = int(flg_start)
    _gnd_cfg_c = (_load_layout_config().get("multi_block", {})
                  .get("decoupling", {}))
    _gnd_names_c = {s.upper() for s in _gnd_cfg_c.get("ground_net_names", [
        "GND", "AGND", "DGND", "PGND", "VSS", "VEE", "EGND", "SGND"])}
    # Wire-crossing avoidance: track segments from prior nets so the
    # router can rank candidate L paths and prefer fewer crossings.
    emitted_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []

    for net in ir.nets:
        pin_positions: List[Tuple[float, float, float]] = []
        for pinref in net.pins:
            if "." not in pinref:
                continue
            ref, pin_key = pinref.split(".", 1)
            comp = comps_by_ref.get(ref)
            if comp is None:
                continue
            abs_pos = comp.pin_abs(pin_key)
            if abs_pos is None:
                continue
            pin_positions.append(abs_pos)
            pin = comp.geom.resolve_pin(pin_key)
            if pin is not None:
                pins_in_any_net.add((ref, pin.number))

        # Foreign pins for THIS net = every pin in the child sheet NOT on
        # this net (mirrors render_flat) — passed to the router and the
        # label stub so neither draws a line through a sibling pin.
        foreign_pts_c: Optional[List[Tuple[float, float]]] = None
        if _avoid_pin_shorts_c:
            own_xy_c = {(round(pp[0], 2), round(pp[1], 2))
                        for pp in pin_positions}
            foreign_pts_c = [(x, y) for (x, y) in all_pin_abs_c
                             if (round(x, 2), round(y, 2)) not in own_xy_c]

        if net.is_power:
            # Child-sheet radius is widened from 12.7 -> 15.24 mm: after
            # _relocate_decoupling_caps pulls each cap snug to its VDD pin,
            # the cap's +V-net pin lands on the FAR side of the cap body
            # (KiCad emits caps with pin 1 at symbol-local +Y; with rot=0
            # that pin sits ~10-13 mm from the IC pin tip). 15.24 mm
            # (= 6 grid cells) is enough to cluster a single decoupling
            # cap on each VDD pin into ONE shared power port.
            clusters = _cluster_power_pins(
                pin_positions,
                radius=_placement_cfg()["child_sheet_power_cluster_radius_mm"])
            for cluster in clusters:
                cx = sum(p[0] for p in cluster) / len(cluster)
                cy = sum(p[1] for p in cluster) / len(cluster)
                anchor = min(cluster,
                              key=lambda p: math.hypot(p[0]-cx, p[1]-cy))
                ax, ay, arot = anchor
                pwr_count += 1
                pwr = _emit_power_port(net.name, (ax, ay), file_uuid,
                                         f"#PWR{pwr_count:03d}",
                                         decollide=_ink_decollide_on())
                if pwr is None:
                    body += _emit_global_label(net.name, (ax, ay), arot)
                else:
                    body += pwr
                # PWR_FLAG once per rail per child sheet — same logic
                # as the flat path: emit only when no pin on this rail
                # is a native power_output (regulator VOUT, etc.).
                # Otherwise the flag's power_out type clashes with the
                # existing driver and fires 'Pins of type Power output
                # and Power output are connected'.
                if pwr is not None and net.name not in pwr_flag_nets:
                    pwr_flag_nets.add(net.name)
                    if use_global_flags:
                        # Hierarchy-global: a rail is driven if ANY pin on
                        # it in ANY block is a native power_out — the
                        # registry is the union of every child's drivers,
                        # so a child without the regulator still sees the
                        # rail as driven and skips the redundant flag.
                        has_native_driver_c = net.name in globally_driven_rails
                    else:
                        has_native_driver_c = False
                        for _pinref in net.pins:
                            if "." not in _pinref:
                                continue
                            _r, _k = _pinref.split(".", 1)
                            _c = comps_by_ref.get(_r)
                            if _c is None:
                                continue
                            _p = _c.geom.resolve_pin(_k)
                            if _p is None:
                                continue
                            if (_p.etype or "").lower() in ("power_out",
                                                              "output"):
                                has_native_driver_c = True
                                break
                    if not has_native_driver_c:
                        flag_count += 1
                        is_gnd_rail = net.name.upper().lstrip("+") in _gnd_names_c
                        port_pin_y = ay + (2.54 if is_gnd_rail else -2.54)
                        port_pin = (ax, port_pin_y)
                        flag_pin = _find_clear_flag_offset(
                            ax, ay, port_pin_y, obstacles)
                        body += _emit_pwr_flag(net.name, flag_pin, file_uuid,
                                                f"#FLG{flag_count:03d}")
                        if _ink_decollide_on():
                            _register_ink(_centered_text_bbox(
                                "PWR_FLAG", flag_pin[0], flag_pin[1] + 2.54))
                        body += _emit_wire(port_pin, flag_pin)
                net_local_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
                for (px, py, _prot) in cluster:
                    if (px, py) == (ax, ay):
                        continue
                    path = _route_l_aware((px, py), (ax, ay), obstacles,
                                            existing_wires=emitted_wires,
                                            foreign_pts=foreign_pts_c)
                    if not path:
                        continue  # no safe route — labels bond by name
                    for k in range(len(path) - 1):
                        body += _emit_wire(path[k], path[k+1])
                        net_local_wires.append((path[k], path[k+1]))
                emitted_wires.extend(net_local_wires)
            continue

        # Within a child sheet, draw wires for ALL pin pairs. The
        # child-sheet IR is a clean subset (only one block's components)
        # so within-sheet connections SHOULD all wire up — the user's
        # rule is "wires inside the block, labels between sheets". The
        # between-sheets labels are handled by hierarchical_label
        # emission below; this loop just connects within-sheet pins.
        net_local_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for i in range(len(pin_positions) - 1):
            p1 = pin_positions[i][:2]
            p2 = pin_positions[i + 1][:2]
            path = _route_l_aware(p1, p2, obstacles,
                                    existing_wires=emitted_wires,
                                    foreign_pts=foreign_pts_c)
            # Same long-wire gate as the flat path: drop wires
            # exceeding `routing.max_wire_length_mm` so child-sheet
            # labels carry the bond. Child sheets are small (one
            # block per page) so this rarely kicks in, but the
            # consistency keeps both render paths behaving the same.
            _max_wire_len_c = _routing_cfg()["max_wire_length_mm"]
            if (path and _max_wire_len_c > 0
                    and _path_manhattan_length(path) > _max_wire_len_c):
                path = []
            if not path:
                continue   # no safe path — labels carry the connection
            for j in range(len(path) - 1):
                body += _emit_wire(path[j], path[j + 1])
                net_local_wires.append((path[j], path[j + 1]))
            if 0 < i < len(pin_positions) - 1:
                body += _emit_junction(pin_positions[i][:2])
        emitted_wires.extend(net_local_wires)

        # Resolve the owning component for each pin so the body-pierce
        # guard can extend the stub when needed.
        pin_owners: List[Optional[PlacedComp]] = []
        for pinref in net.pins:
            if "." in pinref:
                pref = pinref.split(".", 1)[0]
                pin_owners.append(comps_by_ref.get(pref))
            else:
                pin_owners.append(None)
        if net.name in hierarchical_net_names:
            # Cross-sheet net. Label KIND is config-driven (multi_block.
            # cross_sheet_label_kind): 'global' (Model B, default) bonds
            # siblings by name with no parent wiring; 'hierarchical'
            # (Model A) pairs with a parent sheet pin. Both emit one label
            # per local pin with the same R11/R13/body-pierce treatment.
            for i, (x, y, rot) in enumerate(pin_positions):
                co = pin_owners[i] if i < len(pin_owners) else None
                own_bbox = _abs_outer_bbox(co) if co is not None else None
                # R11 label-vs-wire overlap check (gated in config).
                if cross_label_kind == "global":
                    body += _emit_cross_sheet_global_label(
                        net.name, (x, y), rot,
                        obstacles=obstacles, own_bbox=own_bbox,
                        existing_wires=emitted_wires,
                        foreign_pts=foreign_pts_c)
                else:
                    body += _emit_hierarchical_label(
                        net.name, (x, y), rot,
                        obstacles=obstacles, own_bbox=own_bbox,
                        existing_wires=emitted_wires,
                        foreign_pts=foreign_pts_c)
        elif pin_positions:
            x, y, rot = pin_positions[0]
            c0 = pin_owners[0] if pin_owners else None
            own_bbox = _abs_outer_bbox(c0) if c0 is not None else None
            body += _emit_local_label(net.name, (x, y), rot,
                                        obstacles=obstacles,
                                        own_bbox=own_bbox,
                                        existing_wires=emitted_wires,
                                        foreign_pts=foreign_pts_c)

    # Per-block rectangles inside each child sheet (in case child has
    # sub-groupings — currently no, but reserves the hook for v3).
    # Note: ir here is the BLOCK-subsetted IR; ir.blocks is empty for
    # children. Skip rectangle emission for now in children.

    # NC flags: same JSON-driven policy as render_flat — auto-mark all
    # unused pins in the auto_nc_etypes list so kicad-cli sch erc reports
    # no pin_not_connected errors. Multi-unit guard active by default.
    nc_pol = _nc_policy()
    nc_etypes = nc_pol["auto_nc_etypes"] if nc_pol["enabled"] else {"no_connect"}
    unit_filter = nc_pol["unit_filter_enabled"] and nc_pol["enabled"]
    instance_unit = nc_pol["instance_unit"]
    for c in comps:
        for pin in c.geom.pins:
            if (c.ref, pin.number) in pins_in_any_net:
                continue
            if pin.etype not in nc_etypes:
                continue
            if unit_filter and pin.unit not in (0, instance_unit):
                continue
            abs_pos = c.pin_abs(pin.number)
            if abs_pos is None:
                continue
            body += _emit_no_connect((abs_pos[0], abs_pos[1]))

    body += _emit_sheet_instances()
    body += ')\n'

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    # Return the file UUID plus the NEXT #PWR and #FLG indices so the
    # parent's loop can seed the next child sheet — keeps power-port AND
    # PWR_FLAG refdes globally unique across the hierarchy.
    return file_uuid, pwr_count, flag_count


def _signal_direction_for_block(block_name: str, ir: TopologyIR, net) -> str:
    """Heuristic for sheet-pin shape. Power nets bypass this (they go via
    power symbols). For signals we default to bidirectional — always
    electrically valid, no ERC direction-mismatch failures."""
    return "bidirectional"


def _compute_globally_driven_rails(ir: TopologyIR) -> set:
    """Hierarchy-global power-rail driver registry.

    Returns the set of POWER-net names that are driven by at least one
    NATIVE power_out / output pin ANYWHERE in the design (any block).
    `render_hierarchical` builds this ONCE and shares it with every child
    so a child sheet that doesn't itself contain the regulator still
    knows the rail is driven globally — and therefore does NOT drop a
    redundant PWR_FLAG that would collide with the real driver (the
    'Pins of type Power output and Power output are connected' cascade).

    Fully dynamic: the decision is made from each pin's electrical type
    (resolved from the symbol library) on the net graph — never from a
    net name, refdes, or part type. Works for +3V3, +1V8, VBAT, VCORE,
    or any rail the architect invents. Matches the etype set the
    per-sheet path uses so the global registry is the exact union of the
    per-child checks."""
    driver_etypes = {"power_out", "output"}
    comp_by_ref = {c.ref: c for c in ir.components}
    geom_cache: Dict[str, Any] = {}
    driven: set = set()
    for net in ir.nets:
        if not net.is_power or net.name in driven:
            continue
        for pinref in net.pins:
            if "." not in pinref:
                continue
            ref, pin_key = pinref.split(".", 1)
            comp = comp_by_ref.get(ref)
            if comp is None:
                continue
            geom = geom_cache.get(comp.lib_id)
            if geom is None:
                try:
                    geom = load_symbol(comp.lib_id)
                except Exception:
                    continue
                geom_cache[comp.lib_id] = geom
            pin = geom.resolve_pin(pin_key)
            if pin is not None and (pin.etype or "").lower() in driver_etypes:
                driven.add(net.name)
                break
    return driven


@traceable(run_type="chain", name="Build schematic (multi-sheet)")
def render_hierarchical(ir: TopologyIR, out_dir: Path) -> Dict[str, Any]:
    """Render a multi-block IR as a parent sheet + N child sheets.
    Parent is `<ir.name>.kicad_sch`; children are `<NN_block>.kicad_sch`."""
    if not ir.blocks:
        raise ValueError("render_hierarchical called with empty blocks[]")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build block-of-ref index + identify cross-block signal nets
    block_of_ref: Dict[str, str] = {}
    for blk in ir.blocks:
        for ref in blk.component_refs:
            block_of_ref[ref] = blk.name

    cross_block_nets: Dict[str, set] = {}  # net_name -> set of blocks touched
    for net in ir.nets:
        if net.is_power:
            continue
        blocks_touched = set()
        for pinref in net.pins:
            ref = pinref.split(".", 1)[0] if "." in pinref else ""
            if ref in block_of_ref:
                blocks_touched.add(block_of_ref[ref])
        if len(blocks_touched) >= 2:
            cross_block_nets[net.name] = blocks_touched

    # Cross-sheet connection model (Model B 'global' default vs Model A
    # 'hierarchical'). See multi_block.cross_sheet_label_kind in
    # layout_config.json. In global mode children emit global labels and
    # the parent boxes carry NO sheet pins (a sheet pin without a matching
    # hierarchical label fires ERC).
    cross_label_kind = str(
        (_load_layout_config().get("multi_block", {}) or {})
        .get("cross_sheet_label_kind", "global")).lower().strip()
    if cross_label_kind not in ("global", "hierarchical"):
        cross_label_kind = "global"

    # Defensive: drop blocks with empty component_refs[] BEFORE we start
    # numbering sheets. validate.py:BLOCK_EMPTY normally catches this and
    # triggers an architect retry, but if the IR reaches here anyway
    # (manual injection, validation disabled), skipping is safer than
    # writing an empty .kicad_sch the user has to delete by hand. Skipped
    # blocks are reported in stats["skipped_blocks"] so the caller can
    # surface the drop.
    non_empty_blocks = [b for b in ir.blocks if b.component_refs]
    skipped_block_names = [b.name for b in ir.blocks if not b.component_refs]
    if not non_empty_blocks:
        raise ValueError(
            "render_hierarchical: every block has empty component_refs[]; "
            "nothing to render. Check ir.blocks before calling.")

    # Render each child sheet — track a running #PWR counter so power-
    # port refdes are unique ACROSS the whole hierarchy. Without this,
    # every child started at #PWR001 and KiCad's annotator rejected
    # the design with "Duplicate items #PWRnnn" on Update PCB.
    child_info: List[Dict[str, Any]] = []
    pwr_running = 0
    # Hierarchy-global PWR_FLAG pass (config-gated, default on). Build a
    # rail-driver registry ONCE over the WHOLE design so each child knows
    # whether a rail is driven by a native power_out in ANY block (not
    # just its own), and share a flagged-rails set + a running #FLG
    # counter so every global rail gets AT MOST ONE PWR_FLAG with a
    # globally-unique reference. Off -> each child falls back to the
    # legacy per-sheet flag logic (byte-stable). Fully dynamic: decisions
    # come from pin etype + the net graph, never net name / refdes / part.
    _mb_cfg = _load_layout_config().get("multi_block", {}) or {}
    global_pwr_flag = bool(_mb_cfg.get("hierarchy_global_pwr_flag", True))
    if global_pwr_flag:
        globally_driven_rails: Optional[set] = _compute_globally_driven_rails(ir)
        flagged_rails: Optional[set] = set()
    else:
        globally_driven_rails = None
        flagged_rails = None
    flg_running = 0
    total_sheets = len(non_empty_blocks) + 1  # +1 for the parent
    for i, blk in enumerate(non_empty_blocks, start=1):
        block_refs = set(blk.component_refs)
        block_ir = _subset_ir_for_block(ir, block_refs)
        # Nets to expose as hierarchical labels in this child
        hier_names = {n for n, touched in cross_block_nets.items()
                       if blk.name in touched}
        # Sheet name / filename / title -- all driven by the registry
        # in config/block_naming.json. Engine no longer hardcodes the
        # `{NN}_{name.lower()}.kicad_sch` pattern; per-project tweaks
        # (different file numbering, different title style) edit the
        # JSON and pick up automatically.
        sheet_name = _block_sheet_name(blk.name, i, project_name=ir.name)
        child_filename = _block_filename(blk.name, i, project_name=ir.name)
        child_title = _block_title(blk.name, i, project_name=ir.name)
        child_path = out_dir / child_filename
        sheet_uuid = _u()  # the sheet-box UUID on parent (used as path)
        page_label = f"Sheet {i + 1} of {total_sheets}"
        file_uuid, pwr_running, flg_running = _render_block_child(
            block_ir, hier_names, child_path,
            parent_title=ir.name, page_label=page_label,
            pwr_start=pwr_running,
            child_title=child_title,
            cross_label_kind=cross_label_kind,
            globally_driven_rails=globally_driven_rails,
            flagged_rails=flagged_rails,
            flg_start=(flg_running if global_pwr_flag else 0),
        )
        # The (sheet (uuid ...)) on parent MUST equal the child's file
        # uuid (the (kicad_sch (uuid ...)) at the top of the child file).
        child_info.append({
            "block": blk,
            "sheet_name": sheet_name,
            "filename": child_filename,
            "title": child_title,
            "path": str(child_path),
            "file_uuid": file_uuid,
            "hier_nets": hier_names,
        })

    # Render parent sheet: dynamic grid layout from layout_config.json
    # ->hierarchy_layout. Picks the smallest page that fits all child
    # sheet boxes (A4/A3/A2 by priority), then a grid (cols x rows)
    # whose cell aspect best matches target_box_aspect. Every box fits
    # inside (page - margins - title-block reserve). No hardcoding.
    parent_uuid = _u()
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", ir.name).strip("_") or "circuit"
    parent_path = out_dir / f"{safe_name}.kicad_sch"
    # Feed each child's sheet-pin count (= cross-block nets it touches) so the
    # grid sizes boxes tall enough to hold their pins on-page.
    # In global mode the parent boxes carry no sheet pins, so they need no
    # extra pin-row height — pass pin_counts=0 so boxes size to label-free
    # navigators. Hierarchical mode still reserves height for the pins.
    grid = _compute_hierarchy_grid(
        len(child_info),
        pin_counts=([0] * len(child_info) if cross_label_kind == "global"
                    else [len(c["hier_nets"]) for c in child_info]))
    parent_body = _emit_header(
        parent_uuid, title=ir.name,
        page_label=f"Sheet 1 of {total_sheets} (parent)",
        paper=grid["paper"],
    )
    parent_body += '\t(lib_symbols)\n'   # parent has no components in it

    for idx, info in enumerate(child_info):
        bx, by, box_w, box_h = grid["positions"][idx]
        # Build the sheet-pin list for this block: every cross-block net
        # this block touches gets a sheet pin — but ONLY in hierarchical
        # mode. In global mode the children carry global labels (no
        # hierarchical labels), so the box must carry NO sheet pins;
        # otherwise KiCad ERC flags 'sheet pin has no matching
        # hierarchical label'. Connectivity is by global-label name.
        sheet_pins = []
        if cross_label_kind != "global":
            for net_name in sorted(info["hier_nets"]):
                net_obj = next((n for n in ir.nets if n.name == net_name), None)
                shape = _signal_direction_for_block(info["block"].name, ir, net_obj)
                sheet_pins.append((net_name, shape))
        # `block_name` on the sheet symbol drives the hierarchy
        # navigator label + KiCad's per-sheet net-path prefix
        # (/<sheet_name>/<net>). Use the registry-computed sheet_name
        # so a project that customizes sheet_name_template (e.g. to
        # include a prefix or numeric tag) gets respected here too.
        parent_body += _emit_sheet_box(
            block_name=info["sheet_name"],
            child_filename=info["filename"],
            sheet_uuid=info["file_uuid"],
            pos=(bx, by),
            size=(box_w, box_h),
            pin_names=sheet_pins,
            file_uuid=parent_uuid,
        )

    parent_body += _emit_sheet_instances()
    parent_body += ')\n'
    parent_path.write_text(parent_body, encoding="utf-8")
    pro_path = _write_kicad_pro(parent_path)

    return {
        "path": str(parent_path),
        "project": str(pro_path),
        "parent_file_uuid": parent_uuid,
        "blocks": len(non_empty_blocks),
        "blocks_declared": len(ir.blocks),
        "skipped_blocks": skipped_block_names,
        "children": [{"name": c["block"].name, "path": c["path"],
                       "hier_nets": sorted(c["hier_nets"])}
                      for c in child_info],
        "cross_block_nets": sorted(cross_block_nets.keys()),
        "components_total": len(ir.components),
        "nets_total": len(ir.nets),
    }


@traceable(run_type="tool", name="Engine: draw the circuit")
def render(ir: TopologyIR, out_dir: Path,
           force_hierarchy: bool = False,
           force_single_sheet: bool = False) -> Dict[str, Any]:
    """Top-level dispatch — three regimes per the schematic-organisation
    rules:
       SMALL (<= small_max): single sheet, NO block rectangles drawn even
                              if blocks[] is declared (visual noise).
       MEDIUM (between small_max and large_min): single sheet WITH block
                              rectangles for visual grouping.
       LARGE (> large_min OR blocks >= hierarchy_min_blocks): hierarchical
                              multi-sheet (parent + N children).

    Thresholds in layout_config.json:sheet_decision so per-project tuning
    needs no code changes. `force_hierarchy=True` bypasses thresholds and
    forces multi-sheet. `force_single_sheet=True` bypasses thresholds the
    OTHER way and forces a single sheet (with block rectangles when
    blocks[] is declared) regardless of component count --- the
    "block-diagram style" single-sheet schematic the user's reference
    image shows. force_hierarchy wins when both are true."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = _load_layout_config().get("sheet_decision", {})
    small_max = int(cfg.get("small_max_components", 20))
    large_min = int(cfg.get("large_min_components", 50))
    hier_min_blocks = int(cfg.get("hierarchy_min_blocks", 3))
    single_sheet_max_blocks = int(cfg.get("single_sheet_max_blocks", 5))
    size_beats_single = bool(cfg.get("size_override_beats_single_sheet", True))
    n_comp = len(ir.components)
    n_blk = len(ir.blocks)
    if force_hierarchy:
        use_hierarchy = True
    elif n_blk > single_sheet_max_blocks:
        # User policy (2026-06-03): single-sheet-with-blocks holds up to
        # single_sheet_max_blocks (5) functional blocks; ABOVE that the board
        # can't fit one A3 sheet cleanly (A2 is not allowed), so go multi-sheet
        # hierarchy. Decided on the ACTUAL emitted block count, so it catches
        # dense boards the pre-architect keyword estimate under-counted — and
        # it intentionally overrides an AUTO-set force_single_sheet.
        use_hierarchy = True
    elif size_beats_single and force_single_sheet and n_comp > large_min and n_blk >= 2:
        # User policy (2026-06-03): the part COUNT is the second physical
        # limit on a single A3 sheet. A force_single_sheet design (auto- OR
        # user-requested "make it single sheet") that stays UNDER the block
        # cap but emits > large_min (50) components in >= 2 blocks is too
        # dense to fit one sheet cleanly — e.g. the STM32+CAN+microSD data
        # logger (~60 parts in 7-9 blocks) the n_blk override misses. This
        # mirrors the n_blk override above and makes render() honour
        # sheet_decision._rule's `(n_comp > large_min AND n_blk >= 2)` term,
        # which was previously trapped in the unreachable else-branch below
        # whenever force_single_sheet was set. Gated by
        # size_override_beats_single_sheet so it reverts with one config edit.
        use_hierarchy = True
    elif force_single_sheet:
        use_hierarchy = False
    else:
        use_hierarchy = (
            (n_comp > large_min and n_blk >= 2)
            or (n_blk >= hier_min_blocks and n_comp > small_max)
        )
    # In single-sheet block-diagram mode the user explicitly wants a
    # coloured box around EVERY declared block, even ones whose
    # component_refs has no >= 3-pin anchor (e.g. INDICATOR = D1 + R3).
    # Flip the gate so render_flat's draw_block_rectangles condition
    # passes regardless. Reset at the start of each render call so the
    # next request starts from defaults.
    _RENDER_OPTS["force_block_rects"] = bool(
        force_single_sheet and not use_hierarchy and n_blk > 0
    )
    try:
        if use_hierarchy:
            return render_hierarchical(ir, out_dir)
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", ir.name).strip("_") or "circuit"
        out_path = out_dir / f"{safe_name}.kicad_sch"
        return render_flat(ir, out_path)
    finally:
        _RENDER_OPTS["force_block_rects"] = False


# Module-level re-import for the dispatcher's filename sanitiser
import re  # noqa: E402  (kept near use; small enough that placement matters less than locality)
