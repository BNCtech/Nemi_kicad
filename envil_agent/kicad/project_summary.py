"""Deep project summary — walks the whole hierarchical schematic, not
just the root sheet.

The page_summary card needs to answer "what's on this design?" with
real content: circuit type, input/output voltages, key parts. A flat
read of the root .kicad_sch misses everything because KiCad
hierarchical designs put the actual components inside child sheets
(POWER, MCU, CAN_PHY, …); the root is just a wrapper of sheet stubs.

This module:
  1. Walks root → recursively reads every Sheetfile reference
  2. Aggregates component counts
  3. Pattern-matches regulators / MCUs / connectors against the rules
     in `layout_config.json:page_summary.detection`
  4. Extracts output voltages from regulator value strings
  5. Heuristically classifies the design (power-supply / MCU board /
     analog / discrete / mixed)

Pure deterministic — no LLM, no per-circuit hardcoding. All patterns
live in JSON; adding a new regulator family means a one-line edit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sexpdata

from .document import _head, _prop, read_summary, ComponentRow


# ---------------------------------------------------------------------------
# Hierarchical sheet walker
# ---------------------------------------------------------------------------

def _sheetfile_of(sheet_node: list) -> Optional[str]:
    """Read the Sheetfile property of a (sheet ...) node."""
    return _prop(sheet_node, "Sheetfile")


def _sheetname_of(sheet_node: list) -> Optional[str]:
    """Read the Sheetname property of a (sheet ...) node."""
    return _prop(sheet_node, "Sheetname")


def _list_child_sheets(sch_path: Path) -> List[Tuple[str, Path]]:
    """Parse a .kicad_sch and return [(sheetname, child_path), ...].

    Empty list when the file has no hierarchical children, or when the
    file can't be parsed. Sheetfiles are resolved relative to the
    parent's directory per KiCad convention.
    """
    out: List[Tuple[str, Path]] = []
    try:
        text = sch_path.read_text(encoding="utf-8")
        root = sexpdata.loads(text)
    except Exception:
        return out
    if not isinstance(root, list) or _head(root) != "kicad_sch":
        return out
    for node in root[1:]:
        if not (isinstance(node, list) and _head(node) == "sheet"):
            continue
        sf = _sheetfile_of(node)
        if not sf:
            continue
        sn = _sheetname_of(node) or sf
        out.append((sn, (sch_path.parent / sf).resolve()))
    return out


def _walk_hierarchy(root_path: Path) -> List[Tuple[str, Path]]:
    """Depth-first walk of the hierarchy. Returns [(sheetname, path), ...]
    including the root itself (named '<root>'). De-dupes by path so a
    diamond hierarchy doesn't double-count."""
    out: List[Tuple[str, Path]] = []
    visited: Set[Path] = set()

    def _visit(name: str, p: Path) -> None:
        rp = p.resolve()
        if rp in visited or not rp.exists():
            return
        visited.add(rp)
        out.append((name, rp))
        for child_name, child_path in _list_child_sheets(rp):
            _visit(child_name, child_path)

    _visit("<root>", root_path)
    return out


def collect_sheet_paths(entry_path: str | Path) -> List[Path]:
    """Every .kicad_sch file in the hierarchy that `entry_path` belongs
    to, with `entry_path` itself first.

    Works whether the caller hands us the project ROOT or a LEAF child:
      * descendants of `entry_path` are gathered via its (sheet ...) refs;
      * to also reach SIBLING sheets when a leaf was passed, every other
        .kicad_sch in the same folder is checked — but only the one(s)
        whose own hierarchy actually CONTAINS `entry_path` get expanded,
        so an unrelated project sharing the folder is never pulled in.

    De-duped, existing files only. For a flat / single-sheet design this
    returns just `[entry_path]`, so callers stay byte-for-byte identical
    to the old single-file behaviour."""
    entry = Path(entry_path).resolve()
    ordered: List[Path] = []
    seen: Set[Path] = set()

    def _add_tree(p: Path) -> None:
        for _, sp in _walk_hierarchy(p):
            rp = sp.resolve()
            if rp not in seen and rp.exists():
                seen.add(rp)
                ordered.append(rp)

    _add_tree(entry)
    # Leaf-entry case: find the real root among siblings so we can reach
    # the rest of the project. Only expand a sibling whose tree contains
    # `entry` (precise — no cross-project bleed).
    try:
        for sib in sorted(entry.parent.glob("*.kicad_sch")):
            srp = sib.resolve()
            if srp == entry or srp in seen:
                continue
            descendants = {p.resolve() for _, p in _walk_hierarchy(srp)}
            if entry in descendants:
                _add_tree(srp)
    except OSError:
        pass

    # Guarantee entry is first (it leads when it was the root; when it was
    # a leaf, a sibling-root walk may have ordered it later).
    if entry in seen:
        ordered = [entry] + [p for p in ordered if p != entry]
    elif entry.exists():
        ordered = [entry] + ordered
    return ordered


@dataclass
class DeepSummary:
    """Hierarchy-aware schematic summary: every sheet's components folded
    into one list, each tagged with its owning sheet file. Mirrors the
    flat SchematicSummary shape the snapshot block expects, plus a
    per-sheet breakdown."""
    path: str
    sheet_count: int
    component_count: int
    wire_count: int
    label_count: int
    global_label_count: int
    hierarchical_label_count: int
    components: List[ComponentRow] = field(default_factory=list)
    sheet_rows: List[Tuple[str, str, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "sheet_count": self.sheet_count,
            "totals": {
                "components": self.component_count,
                "wires": self.wire_count,
                "labels": self.label_count,
                "global_labels": self.global_label_count,
                "hierarchical_labels": self.hierarchical_label_count,
            },
            "sheets": [
                {"sheet": sn, "file": sf, "components": n}
                for (sn, sf, n) in self.sheet_rows
            ],
            "components": [
                {
                    "ref": c.reference,
                    "value": c.value,
                    "lib_id": c.lib_id,
                    "sheet": c.sheet,
                    "pos": [c.x, c.y, c.rot],
                }
                for c in self.components
            ],
        }


def read_summary_deep(entry_path: str | Path) -> DeepSummary:
    """Read a schematic and ALL its hierarchy children, returning one
    aggregated `DeepSummary`. Each component carries the basename of the
    sheet file it lives on. For a flat design this is a thin wrapper over
    a single `read_summary`, so the output equals the old behaviour with
    `sheet` left blank."""
    entry = Path(entry_path)
    paths = collect_sheet_paths(entry)
    comps: List[ComponentRow] = []
    wires = labels = glabels = hlabels = 0
    rows: List[Tuple[str, str, int]] = []
    for p in paths:
        try:
            s = read_summary(p)
        except Exception:
            continue
        wires += s.wire_count
        labels += s.label_count
        glabels += s.global_label_count
        hlabels += s.hierarchical_label_count
        for c in s.components:
            c.sheet = p.name
            comps.append(c)
        rows.append((p.stem, p.name, s.component_count))
    return DeepSummary(
        path=str(entry),
        sheet_count=len(paths),
        component_count=len(comps),
        wire_count=wires,
        label_count=labels,
        global_label_count=glabels,
        hierarchical_label_count=hlabels,
        components=comps,
        sheet_rows=rows,
    )


# ---------------------------------------------------------------------------
# Pattern-based component classification
# ---------------------------------------------------------------------------

@dataclass
class ProjectSummary:
    """End-to-end snapshot of a hierarchical KiCad schematic."""
    root_path: str
    sheet_count: int                              # total sheets including root
    total_components: int                          # excludes power-port stubs
    sheet_rows: List[Tuple[str, int]]              # (sheetname, component_count)
    regulators: List[Tuple[str, str, str]]         # (ref, value, output_voltage)
    mcus: List[Tuple[str, str]]                    # (ref, value/lib_id)
    connectors: List[Tuple[str, str]]              # (ref, value)
    crystals: List[Tuple[str, str]]                # (ref, value)
    power_nets: List[str]                          # detected from power-port symbols
    input_voltage: str                             # heuristic
    output_voltages: List[str]                     # from regulators
    circuit_type: str                              # heuristic label

    def to_dict(self) -> dict:
        return {
            "root_path": self.root_path,
            "sheet_count": self.sheet_count,
            "total_components": self.total_components,
            "sheet_rows": self.sheet_rows,
            "regulators": self.regulators,
            "mcus": self.mcus,
            "connectors": self.connectors,
            "crystals": self.crystals,
            "power_nets": self.power_nets,
            "input_voltage": self.input_voltage,
            "output_voltages": self.output_voltages,
            "circuit_type": self.circuit_type,
        }


def _compile_patterns(raw: List[str]) -> List[re.Pattern]:
    """Compile a list of regex strings to case-insensitive patterns.
    Bad patterns are silently dropped — we'd rather miss a match than
    fail the whole page-summary turn."""
    out: List[re.Pattern] = []
    for p in raw or []:
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            continue
    return out


def _matches_any(text: str, pats: List[re.Pattern]) -> bool:
    return any(p.search(text) for p in pats)


def _regulator_output(value: str, voltage_map: Dict[str, str],
                       suffix_pat: Optional[re.Pattern]) -> str:
    """Resolve a regulator's output voltage.

    1) Exact lookup in `voltage_map` (e.g. "LM7805" -> "+5V").
    2) Substring lookup — handle "AMS1117-3.3" when map has "AMS1117-3.3".
    3) Suffix regex — pull the volt-fraction from the value string
       ("AMS1117-3.3" -> 3.3 -> "+3V3"). Last-resort heuristic; only
       trusted when no map entry matched.
    """
    v = value.strip()
    if v in voltage_map:
        return voltage_map[v]
    # Substring — useful when value has trailing whitespace / package marks
    for key, out in voltage_map.items():
        if key and key in v:
            return out
    if suffix_pat:
        m = suffix_pat.search(v)
        if m:
            try:
                volts = float(m.group(1))
                if volts == int(volts):
                    return f"+{int(volts)}V"
                # 3.3 -> +3V3 KLC style
                whole, frac = str(volts).split(".")
                return f"+{whole}V{frac}"
            except (ValueError, IndexError):
                pass
    return ""


def _detect_circuit_type(regulators: List, mcus: List, connectors: List,
                          total_components: int,
                          rules: List[Dict[str, Any]]) -> str:
    """Walk the rule list top-to-bottom; first matching rule wins.
    Each rule looks like {if: {<condition>: value}, type: '<label>'}.
    Universal — circuit types come from JSON, not code."""
    n_reg = len(regulators)
    n_mcu = len(mcus)
    n_con = len(connectors)
    for rule in rules or []:
        cond = rule.get("if") or {}
        ok = True
        if "min_regulators" in cond and n_reg < cond["min_regulators"]:
            ok = False
        if "max_regulators" in cond and n_reg > cond["max_regulators"]:
            ok = False
        if "min_mcus" in cond and n_mcu < cond["min_mcus"]:
            ok = False
        if "max_mcus" in cond and n_mcu > cond["max_mcus"]:
            ok = False
        if "min_connectors" in cond and n_con < cond["min_connectors"]:
            ok = False
        if "min_components" in cond and total_components < cond["min_components"]:
            ok = False
        if "max_components" in cond and total_components > cond["max_components"]:
            ok = False
        if ok:
            return str(rule.get("type", ""))
    return "Mixed / generic circuit"


# ---------------------------------------------------------------------------
# Top-level: read_project_summary
# ---------------------------------------------------------------------------

def read_project_summary(root_path: str | Path,
                           detection_cfg: Optional[Dict[str, Any]] = None
                           ) -> ProjectSummary:
    """Build a deep `ProjectSummary` for the hierarchical schematic
    rooted at `root_path`.

    `detection_cfg` is the `page_summary.detection` block from
    layout_config.json. When None, defaults are minimal — caller is
    responsible for piping the config through.
    """
    cfg = detection_cfg or {}
    reg_patterns = _compile_patterns(cfg.get("regulator_value_patterns", []))
    mcu_patterns = _compile_patterns(cfg.get("mcu_value_patterns", []))
    connector_prefixes = list(cfg.get("connector_lib_id_prefixes", []))
    crystal_lib_prefixes = list(cfg.get("crystal_lib_id_prefixes", []))
    voltage_map = dict(cfg.get("voltage_extraction", {}) or {})
    suffix_pat_raw = cfg.get("regulator_voltage_suffix_regex", "")
    suffix_pat = re.compile(suffix_pat_raw) if suffix_pat_raw else None
    type_rules = cfg.get("circuit_type_rules", []) or []
    power_net_patterns = list(cfg.get("power_net_patterns", []))
    input_priority = list(cfg.get("input_voltage_priority", []))

    root = Path(root_path).resolve()
    sheets = _walk_hierarchy(root)
    sheet_rows: List[Tuple[str, int]] = []
    all_components: List[ComponentRow] = []
    all_labels: List[str] = []                    # net names from labels

    for (sheetname, path) in sheets:
        try:
            s = read_summary(path)
        except Exception:
            sheet_rows.append((sheetname, 0))
            continue
        sheet_rows.append((sheetname, s.component_count))
        all_components.extend(s.components)
        # Pull label names too so we can detect power nets
        try:
            text = path.read_text(encoding="utf-8")
            root_sx = sexpdata.loads(text)
            if isinstance(root_sx, list) and _head(root_sx) == "kicad_sch":
                for node in root_sx[1:]:
                    if not isinstance(node, list):
                        continue
                    h = _head(node)
                    if h in ("label", "global_label", "hierarchical_label"):
                        if len(node) >= 2 and isinstance(node[1], str):
                            all_labels.append(node[1])
                    elif h == "symbol":
                        # power-port symbols carry their net name as
                        # the Value property (e.g. "+5V", "GND")
                        ref = _prop(node, "Reference") or ""
                        if ref.startswith("#PWR"):
                            v = _prop(node, "Value") or ""
                            if v:
                                all_labels.append(v)
        except Exception:
            pass

    # Classify components
    regulators: List[Tuple[str, str, str]] = []
    mcus: List[Tuple[str, str]] = []
    connectors: List[Tuple[str, str]] = []
    crystals: List[Tuple[str, str]] = []

    for c in all_components:
        val = (c.value or "").strip()
        lib = (c.lib_id or "").strip()
        # MCU — match value patterns first (STM32G0..., ATmega328P, etc.)
        if _matches_any(val, mcu_patterns) or _matches_any(lib, mcu_patterns):
            mcus.append((c.reference, val or lib))
            continue
        # Regulator
        if _matches_any(val, reg_patterns):
            vout = _regulator_output(val, voltage_map, suffix_pat)
            regulators.append((c.reference, val, vout))
            continue
        # Connector — by lib_id prefix
        if any(lib.startswith(p) for p in connector_prefixes):
            connectors.append((c.reference, val or lib))
            continue
        # Crystal / oscillator
        if any(lib.startswith(p) for p in crystal_lib_prefixes):
            crystals.append((c.reference, val or lib))

    # Power nets (from labels + power-port values)
    power_pat = [re.compile(p, re.IGNORECASE) for p in power_net_patterns]
    power_nets: List[str] = []
    seen_nets: Set[str] = set()
    for name in all_labels:
        if not name or name in seen_nets:
            continue
        if any(p.search(name) for p in power_pat) or name.upper() in (
                "GND", "AGND", "DGND", "PGND", "VSS", "VCC", "VDD",
                "VBUS", "VBAT", "VIN", "VOUT", "VSYS"):
            seen_nets.add(name)
            power_nets.append(name)

    # Output voltages = unique non-empty regulator outputs
    out_voltages: List[str] = []
    for (_, _, vout) in regulators:
        if vout and vout not in out_voltages:
            out_voltages.append(vout)

    # Input voltage = highest-priority power net that's NOT one of the
    # regulator outputs (so on a 5V→3V3 board we pick 5V/VBUS as the
    # input). Falls back to the first non-GND power net.
    input_voltage = ""
    out_voltage_set = set(out_voltages)
    candidates = [n for n in power_nets
                   if n not in out_voltage_set
                   and n.upper() not in ("GND", "AGND", "DGND",
                                          "PGND", "SGND", "VSS")]
    for pri in input_priority:
        pri_pat = re.compile(pri, re.IGNORECASE)
        for n in candidates:
            if pri_pat.search(n):
                input_voltage = n
                break
        if input_voltage:
            break
    if not input_voltage and candidates:
        input_voltage = candidates[0]

    circuit_type = _detect_circuit_type(
        regulators, mcus, connectors,
        len(all_components), type_rules)

    return ProjectSummary(
        root_path=str(root),
        sheet_count=len(sheets),
        total_components=len(all_components),
        sheet_rows=sheet_rows,
        regulators=regulators,
        mcus=mcus,
        connectors=connectors,
        crystals=crystals,
        power_nets=power_nets,
        input_voltage=input_voltage,
        output_voltages=out_voltages,
        circuit_type=circuit_type,
    )
