"""Tool: parse an ERC report, classify violations, propose / apply
concrete fixes.

Unlike `erc_check` which is read-only, this tool builds a list of
`apply_ops` operations that would resolve the reported issues —
optionally applying them in the same call.

Fix strategy per ERC violation type (all JSON-driven):
  pin_not_connected        -> not auto-fixable here (would need user
                              intent: is this a real unused pin or a
                              missing wire? Currently suppressed in
                              .kicad_pro anyway)
  power_pin_not_driven     -> add PWR_FLAG symbol on the rail
                              (suppressed by default — auto-fix only
                              meaningful when user re-enables the rule)
  isolated_pin_label       -> remove the orphan label (it duplicates
                              the global power port at the same coord)
  duplicate_reference      -> rename the duplicate via rename verb
  unconnected_wire_endpoint-> delete the dangling wire stub
  multiple_net_names       -> rename one of the labels to match the
                              dominant net name on the wire segment

Universal — works on any .kicad_sch. No per-circuit hardcoding."""
from __future__ import annotations

import importlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("erc_autofix", {}) or {}
    except Exception:
        return {}


# ----------------------------------------------------------------------
# Taxonomy classifier + Intent Confidence Layer (Phase 1)
#
# A declarative 4-tier router sits IN FRONT OF the legacy per-type fix
# dispatch. config/erc_taxonomy.json maps each KiCad ERC settings-key to
# one of: safe_auto | needs_reasoning | forbidden | ignore. Only
# safe_auto types whose executor is "legacy" reach the existing
# strategies — everything else becomes a structured diagnostic (root
# cause + candidate fixes) that is NEVER auto-applied.
#
# Non-breaking: when the taxonomy file is missing / unparseable / has
# enabled=false, _load_taxonomy() returns a falsy dict and the router is
# skipped entirely, so the legacy behaviour is byte-stable.
# ----------------------------------------------------------------------
_TAXONOMY_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "erc_taxonomy.json"
)


def _load_taxonomy() -> Dict[str, Any]:
    try:
        return json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# kicad-cli descr for a conflicting pin looks like:
#   "Symbol #FLG001 Pin 1 [Power output, Line]"
# Capture the reference designator so a power-output conflict between
# PWR_FLAG annotations can be told apart from one between real driver
# pins. \S+ greedily grabs "#FLG001" / "U2" etc.
_PIN_SYMBOL_REF_RE = re.compile(r"Symbol\s+(\S+)\s+Pin", re.IGNORECASE)


def _pwr_flag_conflict_refs(v: Dict[str, Any],
                             pf_cfg: Dict[str, Any]) -> List[str]:
    """For a `pin_to_pin` Power-output conflict, return the PWR_FLAG refs
    that are SAFE to delete — or [] if no PWR_FLAG is involved (caller
    then keeps the forbidden classification).

    A PWR_FLAG carries no current; it only asserts "this rail is driven".
    Two PWR_FLAGs on one net (or a PWR_FLAG sharing a net with a real
    driver) is therefore a redundant annotation, not a real short:
      * real driver also present  -> every PWR_FLAG is redundant -> delete all
      * only PWR_FLAGs present     -> keep the first, delete the rest
    The per-fix / batch regression guard re-runs ERC after the delete, so
    even the rare case where a flag was masking a genuinely-undriven rail
    is caught (the delete would resurface power_pin_not_driven and be
    rolled back). Purely additive: returns [] unless flags are found."""
    prefixes = [str(p).upper() for p in
                (pf_cfg.get("ref_prefixes") or ["#FLG"])]
    ordered: List[str] = []
    seen = set()
    for loc in v.get("locations", []) or []:
        m = _PIN_SYMBOL_REF_RE.search(str(loc.get("descr", "")))
        if not m:
            continue
        ref = m.group(1)
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    flags = [r for r in ordered
             if any(r.upper().startswith(p) for p in prefixes)]
    if not flags:
        return []
    non_flags = [r for r in ordered if r not in flags]
    return flags if non_flags else flags[1:]


def _classify_pin_to_pin(v: Dict[str, Any],
                          tax: Dict[str, Any]) -> Dict[str, Any]:
    """Second-stage classifier for `pin_to_pin`. KiCad collapses every
    pin-conflict-matrix violation into this one key; the real conflict is
    only in the description text. Route to a forbidden sub-class (or
    generic needs_reasoning) by lowercased substring match against
    message + every location descr. First matching rule wins."""
    sub = tax.get("pin_to_pin_subclassifier", {}) or {}
    text = " ".join(
        [str(v.get("message", ""))]
        + [str(l.get("descr", "")) for l in v.get("locations", [])]
    ).lower()
    # PRIORITY: redundant-PWR_FLAG is a SAFE auto-delete, so it must be
    # caught BEFORE the forbidden `power_output_conflict` rule below
    # (which would otherwise refuse to touch it). Self-limiting: only
    # fires when PWR_FLAG refs are actually present in the locations.
    pf_cfg = sub.get("pwr_flag_autofix", {}) or {}
    if pf_cfg.get("enabled", True):
        del_refs = _pwr_flag_conflict_refs(v, pf_cfg)
        if del_refs:
            return {
                "fix_class": "safe_auto",
                "kicad_key": "pin_to_pin",
                "subclass": "redundant_pwr_flag",
                "user_name": "Redundant PWR_FLAG on a driven rail",
                "delete_refs": del_refs,
                "candidate_fixes": pf_cfg.get("candidate_fixes", []),
            }
    for rule in sub.get("rules", []):
        any_phrases = rule.get("any_phrases") or []
        all_tokens = rule.get("all_tokens") or []
        none_tokens = rule.get("none_tokens") or []
        if any_phrases and not any(p in text for p in any_phrases):
            continue
        if all_tokens and not all(t in text for t in all_tokens):
            continue
        if none_tokens and any(t in text for t in none_tokens):
            continue
        return {
            "fix_class": rule.get("fix_class", "forbidden"),
            "kicad_key": "pin_to_pin",
            "subclass": rule.get("name", ""),
            "user_name": rule.get("name", "pin conflict"),
            "candidate_fixes": rule.get("candidate_fixes", []),
        }
    return {
        "fix_class": sub.get("default_fix_class", "needs_reasoning"),
        "kicad_key": "pin_to_pin",
        "subclass": sub.get("default_subclass", "pin_conflict_generic"),
        "user_name": "pin conflict",
        "candidate_fixes": sub.get("default_candidate_fixes", []),
    }


def _classify_violation(v: Dict[str, Any],
                         tax: Dict[str, Any]) -> Dict[str, Any]:
    """Map one violation to {fix_class, ...} using the taxonomy. Unknown
    types default to needs_reasoning (never auto-fixed)."""
    vtype = v.get("type", "")
    if vtype in set(tax.get("ignore_types", []) or []):
        return {"fix_class": "ignore", "kicad_key": vtype}
    if vtype == "pin_to_pin":
        return _classify_pin_to_pin(v, tax)
    entry = (tax.get("classes", {}) or {}).get(vtype)
    if entry is None:
        return {
            "fix_class": tax.get("default_fix_class", "needs_reasoning"),
            "kicad_key": vtype,
            "user_name": vtype,
            "candidate_fixes": [],
        }
    out = dict(entry)
    out["fix_class"] = entry.get("fix_class", "needs_reasoning")
    out["kicad_key"] = vtype
    return out


def _confidence_pin_not_connected(
        v: Dict[str, Any], sch_path: Optional[str],
        conf: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Intent confidence for a `pin_not_connected` violation.

    HIGH  -> the pin has no wire/label anywhere near it: it is genuinely
             unused, so a No-Connect flag is the right (safe) fix.
    LOW   -> a wire endpoint or a label sits within near_mm of the pin
             but is NOT actually on it: that smells like a missing
             wire-snap, so capping the pin with NC would hide a real
             connectivity bug. Demote to needs_reasoning.
    Returns the WORST (lowest) score across the violation's coords."""
    pcfg = conf.get("pin_not_connected", {}) or {}
    unused = float(pcfg.get("unused_score", 0.9))
    near = float(pcfg.get("near_obstruction_score", 0.2))
    fail = float(pcfg.get("trace_fail_score", 0.3))
    near_mm = float(conf.get("near_mm", 2.54))
    on_tol = 0.05   # mm — a wire/label this close counts as ON the pin

    locs = v.get("locations", []) or []
    if not sch_path or not locs:
        return fail, ["no coord or no schematic — cannot assess intent"]
    try:
        import sexpdata
        # NB: `from . import trace_net` resolves to the @tool OBJECT
        # (re-exported in tools/__init__.py), which has no ._scan.
        # Import the real MODULE — the same pattern this file already
        # uses for apply_ops / erc_check.
        TN = importlib.import_module("envil_agent.tools.trace_net")
        root = sexpdata.loads(Path(sch_path).read_text(encoding="utf-8"))
        scan = TN._scan(root)
    except Exception:
        return fail, ["schematic scan failed — cannot assess intent"]

    wires = scan.get("wires", [])
    labels = scan.get("labels", [])
    score = unused
    factors: List[str] = []
    for loc in locs:
        try:
            x = float(loc["x"])
            y = float(loc["y"])
        except (KeyError, TypeError, ValueError):
            continue
        near_wire = False
        for (ax, ay, bx, by) in wires:
            for (ex, ey) in ((ax, ay), (bx, by)):
                d = math.hypot(ex - x, ey - y)
                if on_tol < d <= near_mm:
                    near_wire = True
                    break
            if near_wire:
                break
        near_label = any(
            math.hypot(lx - x, ly - y) <= near_mm
            for (_n, lx, ly, _k) in labels
        )
        if near_wire or near_label:
            score = min(score, near)
            what = "wire" if near_wire else ""
            what += "/label" if near_label else ""
            factors.append(
                f"@({x:.2f},{y:.2f}) {what.strip('/')} within "
                f"{near_mm}mm but not on pin — probable missing snap")
        else:
            factors.append(
                f"@({x:.2f},{y:.2f}) no nearby wire/label — pin unused")
    return score, factors


def _confidence_score(v: Dict[str, Any], cls: Dict[str, Any],
                       sch_path: Optional[str],
                       tax: Dict[str, Any]) -> Tuple[float, List[str]]:
    """Dispatch to the per-type confidence scorer. Types without a scorer
    return full confidence (the taxonomy already vetted them as safe)."""
    conf = tax.get("confidence", {}) or {}
    vtype = v.get("type", "")
    if vtype == "pin_not_connected":
        return _confidence_pin_not_connected(v, sch_path, conf)
    if vtype == "endpoint_off_grid":
        ecfg = conf.get("endpoint_off_grid", {}) or {}
        return float(ecfg.get("default_score", 0.85)), ["off-grid snap"]
    return 1.0, ["no confidence rule — trusting safe_auto classification"]


def _make_diagnostic(v: Dict[str, Any], cls: Dict[str, Any],
                      fix_class: str, confidence: Optional[float] = None,
                      extra_reason: str = "") -> Dict[str, Any]:
    """Build a diagnostic-only proposal (ops=[]) carrying the fix_class,
    sub-class, candidate fixes and confidence so the report and the
    LLM-escalation summary can surface root cause + options without
    touching the schematic."""
    vtype = v.get("type", "")
    locs = v.get("locations", []) or []
    loc0 = locs[0] if locs else {}
    name = cls.get("user_name", vtype)
    sub = cls.get("subclass", "")
    label = {
        "forbidden": "DANGEROUS — AI must NOT auto-fix",
        "needs_reasoning": "needs reasoning — escalate to LLM/human",
        "safe_auto": "safe to auto-fix",
    }.get(fix_class, fix_class)
    root_cause = f"{name} ({fix_class}{('/' + sub) if sub else ''})"
    if loc0.get("descr"):
        root_cause += f" — {loc0['descr']}"
    reason = label + (f" — {extra_reason}" if extra_reason else "")
    return {
        "violation_type": vtype,
        "severity": v.get("severity", ""),
        "root_cause": root_cause,
        "reason": reason,
        "validation": "diagnostic only — no schematic change applied",
        "side_effects": "none",
        "confidence": confidence if confidence is not None else 0.0,
        "diagnostic_only": True,
        "fix_class": fix_class,
        "subclass": sub,
        "candidate_fixes": cls.get("candidate_fixes", []) or [],
        "ops": [],
    }


# kicad-cli ERC report line patterns. The location regex accepts BOTH
# units kicad-cli emits — `mm` (default kicad-cli) and `mils` (when the
# user's project is set to mils). Bug 2026-05-27: a hardcoded `mm` made
# the parser silently extract 0 locations from a mils-unit report, so
# erc_autofix proposed nothing and the agent fell back to "no fix
# applies" even though the report had 10 valid violations.
_VIOLATION_TYPE_RE = re.compile(r"^\[([a-z_]+)\]:\s*(.*)$")
_LOCATION_RE      = re.compile(
    r"@\(\s*([-0-9.]+)\s*(mm|mils)\s*,\s*([-0-9.]+)\s*(mm|mils)\s*\)\s*:\s*(.*)$")
# kicad-cli groups violations under `***** Sheet /path/` headers (the root
# sheet is `***** Sheet /`). Capturing the path lets the apply loop route a
# fix to the child .kicad_sch that actually owns the violating pin.
_SHEET_HEADER_RE  = re.compile(r"^\*{3,}\s*Sheet\s+(\S+)\s*$")
_MILS_TO_MM = 0.0254


def _parse_erc_report(report_text: str) -> List[Dict[str, Any]]:
    """Split the kicad-cli ERC plain-text report into a structured list:
       [{type, severity, message, sheet, locations: [(x, y, descr), ...]}, ...]

    `sheet` is the hierarchical sheet path the violation lives on (e.g.
    "/" for the root, "/POWER/" for a child). kicad-cli groups every
    violation under a `***** Sheet /path/` header and the `@(x,y)` coords
    are LOCAL to that sheet. Capturing the header is what lets the apply
    loop route each fix to the owning child .kicad_sch (hierarchy_aware);
    flat designs only ever emit `***** Sheet /`, so the key is harmless
    there and the legacy single-file path stays byte-stable."""
    out: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    current_sheet = "/"
    for raw in report_text.splitlines():
        line = raw.rstrip()
        sheet_m = _SHEET_HEADER_RE.match(line.strip())
        if sheet_m:
            current_sheet = sheet_m.group(1).strip()
            continue
        m = _VIOLATION_TYPE_RE.match(line.strip())
        if m:
            if current is not None:
                out.append(current)
            current = {"type": m.group(1), "severity": "",
                        "message": m.group(2).strip(),
                        "sheet": current_sheet,
                        "locations": []}
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith(";"):
            sev = stripped.lstrip(";").strip().lower()
            if "error" in sev:
                current["severity"] = "error"
            elif "warning" in sev:
                current["severity"] = "warning"
            continue
        loc = _LOCATION_RE.match(stripped)
        if loc:
            try:
                x = float(loc.group(1))
                xu = loc.group(2)
                y = float(loc.group(3))
                yu = loc.group(4)
                descr = loc.group(5).strip()
                # Normalise to mm so downstream code (apply_ops verbs,
                # bbox math, _find_clear_flag_pos) sees a single unit.
                if xu == "mils":
                    x *= _MILS_TO_MM
                if yu == "mils":
                    y *= _MILS_TO_MM
                current["locations"].append({
                    "x": x, "y": y, "descr": descr,
                })
            except (ValueError, TypeError):
                pass
    if current is not None:
        out.append(current)
    return out


# ----------------------------------------------------------------------
# Hierarchy routing — map an ERC `***** Sheet /NAME/` path to the child
# .kicad_sch that owns it, so a fix lands in the right file at the report's
# (sheet-LOCAL) coordinates. A flat / single-sheet design has no child
# sheets, so `_resolve_sheet_files` returns {} and every violation resolves
# to the parent path — the legacy single-file behaviour, byte-stable.
# ----------------------------------------------------------------------
def _resolve_sheet_files(parent_path: Path) -> Dict[str, Path]:
    """{sheetname_lower: child_Path} for every sheet in the hierarchy
    rooted at `parent_path`. Empty for a flat/single-sheet design."""
    out: Dict[str, Path] = {}
    try:
        PS = importlib.import_module("envil_agent.kicad.project_summary")
        pairs = PS._walk_hierarchy(Path(parent_path).resolve())
    except Exception:
        return out
    for name, p in pairs:
        nm = str(name or "").strip()
        if nm and nm != "<root>":
            out[nm.lower()] = p
    return out


def _sheet_to_file(sheet_path: str, name_map: Dict[str, Path],
                   parent_path: Path) -> Path:
    """Resolve an ERC sheet path ("/", "/POWER/", "/A/B/") to the owning
    .kicad_sch. Root ("/") and any unknown sheet fall back to the parent
    — the same target the legacy flat path always used, so a routing miss
    can never make things worse than before."""
    s = (sheet_path or "").strip().strip("/")
    if not s:
        return parent_path
    leaf = s.split("/")[-1].strip().lower()
    return name_map.get(leaf, parent_path)


# ----------------------------------------------------------------------
# Phase A — signature-aware regression guard.
#
# The legacy apply loop compared only the ERROR COUNT before/after each
# fix. That misses two real cascades the user hit:
#   * SWAP   — a fix clears violation X but introduces violation Y at a
#              different coord (count unchanged, but a NEW problem).
#   * REINTRO— a fix re-creates a violation an earlier fix had cleared.
# A "violation signature" = `type@x,y` (coord rounded to 0.01 mm). The
# guard rolls back whenever a signature appears that was NOT in the
# state immediately before the fix (per_fix) / not in the baseline
# report (batch). HARD invariant: the schematic can only ever come out
# the same or better — never with a new ERROR.
#
# ERRORS-ONLY by design: signatures and counts are taken from
# error-severity violations, NOT warnings. A fix that legitimately
# clears errors but happens to add a benign warning (e.g. a PWR_FLAG)
# must NOT trigger a rollback. The user's requirement is specifically
# "errors must not increase".
#
# COUNT SOURCE: the error count is parsed straight from the kicad-cli
# report TEXT (via _parse_erc_report, the same parser used for the
# baseline) — NOT from erc_check's JSON `error_count`, which only
# inspects the `[...]` bracket (the violation TYPE, e.g.
# `[power_pin_not_driven]`) and so reports 0 for every modern report.
# ----------------------------------------------------------------------
def _violation_signatures(v: Dict[str, Any]) -> List[str]:
    """One stable signature per location of a violation. Locationless
    violations collapse to `type@-`."""
    vt = v.get("type", "")
    locs = v.get("locations", []) or []
    if not locs:
        return [f"{vt}@-"]
    out: List[str] = []
    for loc in locs:
        try:
            x = round(float(loc["x"]), 2)
            y = round(float(loc["y"]), 2)
            out.append(f"{vt}@{x},{y}")
        except (KeyError, TypeError, ValueError):
            out.append(f"{vt}@-")
    return out


def _error_signature_set(violations: List[Dict[str, Any]]) -> set:
    """Set of signatures for ERROR-severity violations only. Warnings
    never enter the set, so a new warning can never trip the guard."""
    sigs: set = set()
    for v in violations:
        if v.get("severity") == "error":
            sigs.update(_violation_signatures(v))
    return sigs


def _atomic_restore(path: Path, data: bytes) -> bool:
    """Restore `data` onto `path` atomically: write a sibling temp file
    then os.replace() it over the target. A failure or kill mid-write
    can therefore NEVER truncate/corrupt the original .kicad_sch — the
    replace is the only step that touches it, and it is atomic on NTFS.

    Returns True on success; False if the file is locked/unwritable
    (e.g. open in eeschema or a stale .kicad_sch.lck) so the caller can
    surface a plain-English message instead of crashing."""
    tmp = path.with_suffix(path.suffix + ".envil-restore-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(str(tmp), str(path))
        return True
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


async def _rerun_erc(path: Path) -> Optional[Dict[str, Any]]:
    """Re-run kicad-cli ERC and return {errors, sigs, report_file},
    where `errors` and `sigs` are parsed from the report TEXT (so the
    count is real, not erc_check's broken bracket-based count).

    Returns None when erc_check errored, produced no report file, or the
    file could not be read — callers treat None as "unverifiable" and,
    per the conservative-keep policy, do NOT roll back on a flaky run (a
    parse glitch must not throw away good fixes)."""
    ERC = importlib.import_module("envil_agent.tools.erc_check")
    erc_r = await ERC.erc_check.handler({"path": str(path)})
    if erc_r.get("is_error"):
        return None
    try:
        summary = json.loads(erc_r["content"][0]["text"])
    except Exception:
        return None
    rf = summary.get("report_file") or ""
    if not rf or not Path(rf).exists():
        return None
    try:
        text = Path(rf).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    vios = _parse_erc_report(text)
    errors = sum(1 for v in vios if v.get("severity") == "error")
    return {"errors": errors,
            "sigs": _error_signature_set(vios),
            "report_file": rf}


_SYMBOL_AT_RE = re.compile(
    r'\(symbol\s*\(lib_id\s+"([^"]+)"\)\s*\(at\s+([-0-9.]+)\s+([-0-9.]+)',
    re.DOTALL,
)
_REFDES_RE = re.compile(r'\(property\s+"Reference"\s+"([A-Z#]+)(\d+)"')
_REFDES_SPLIT_RE = re.compile(r'^([A-Z#]+)(\d+)$')
# A symbol instance whose lib_id ends in PWR_FLAG, paired with its
# Reference. Non-greedy DOTALL between lib_id and the first Reference
# property — in the engine's stable s-expr the Reference is the first
# property after lib_id, so this captures the flag's own ref. Used to
# CONFIRM (by library identity, not just the #FLG ref-name convention)
# that a symbol slated for deletion really is a PWR_FLAG.
_PWRFLAG_REF_RE = re.compile(
    r'\(lib_id\s+"[^"]*PWR_FLAG"\).*?\(property\s+"Reference"\s+"([^"]+)"',
    re.DOTALL,
)


def _scan_schematic_geometry(sch_text: str) -> Dict[str, Any]:
    """Quick read-only scan of the schematic file to extract:
       - placed_at: [(lib_id, x, y), ...] for every symbol instance
       - refdes_by_prefix: {"R": {1, 2, 3}, "C": {1, 5}, ...}
       - pwr_flag_refs: {"#FLG001", ...} refs whose lib_id is PWR_FLAG
    Used by the fix strategies to pick SAFE locations / SAFE refdes
    suffixes / confirm a delete target's identity without a full sexpdata
    parse. Cheap regex pass — works because the engine writes a stable
    s-expr format. Caller passes the raw text once; lookups share it."""
    placed = []
    for m in _SYMBOL_AT_RE.finditer(sch_text):
        try:
            placed.append((m.group(1), float(m.group(2)), float(m.group(3))))
        except (TypeError, ValueError):
            continue
    refdes_by_prefix: Dict[str, set] = {}
    for m in _REFDES_RE.finditer(sch_text):
        prefix = m.group(1)
        try:
            num = int(m.group(2))
        except ValueError:
            continue
        refdes_by_prefix.setdefault(prefix, set()).add(num)
    pwr_flag_refs = {m.group(1) for m in _PWRFLAG_REF_RE.finditer(sch_text)}
    return {"placed_at": placed, "refdes_by_prefix": refdes_by_prefix,
             "pwr_flag_refs": pwr_flag_refs}


def _find_clear_flag_pos(error_x: float, error_y: float,
                           placed_at: List[Tuple[str, float, float]],
                           clearance_mm: float = 5.08) -> Tuple[float, float]:
    """Pick a coord near (error_x, error_y) that doesn't collide with
    any existing symbol's (at ...) position. Spiral outward in 5.08 mm
    steps so the new PWR_FLAG ends up adjacent to the failing pin
    without landing ON another component.

    Cardinal offsets in priority order: UP first (PWR_FLAGs canonically
    sit above the rail), then RIGHT / LEFT / DOWN. Doubles the radius
    when all 4 are blocked. Universal — no part-name logic."""
    GRID = 1.27
    def _snap(p):
        return (round(p[0] / GRID) * GRID, round(p[1] / GRID) * GRID)

    def _free(px: float, py: float) -> bool:
        for _lib, sx, sy in placed_at:
            if abs(sx - px) <= clearance_mm and abs(sy - py) <= clearance_mm:
                return False
        return True

    for radius in (7.62, 10.16, 12.7, 15.24, 20.32, 25.4):
        candidates = [
            (error_x, error_y - radius),  # UP first
            (error_x + radius, error_y),
            (error_x - radius, error_y),
            (error_x, error_y + radius),
        ]
        for px, py in candidates:
            snapped = _snap((px, py))
            if _free(*snapped):
                return snapped
    # All radii blocked — return the priority-1 UP offset anyway. A
    # slightly-overlapping flag is preferable to the error returning.
    return _snap((error_x, error_y - 7.62))


def _next_refdes(prefix: str, used: set) -> str:
    """Return the next available refdes for `prefix` given the set of
    already-used numbers. R1, R2, R3 used → returns 'R4'. Used by the
    duplicate_reference fix strategy. Pure data-driven, no part-name
    hardcoding."""
    n = 1
    while n in used:
        n += 1
    used.add(n)
    return f"{prefix}{n}"


def _diagnose_power_net(sch_path: str, err_x: float, err_y: float
                        ) -> Optional[Dict[str, Any]]:
    """Circuit-aware diagnosis for a `power_pin_not_driven` violation.

    Traces the actual net at the ERC coord and the whole schematic, then
    classifies the root cause so the caller can pick the RIGHT fix
    instead of reflexively dropping a PWR_FLAG (the senior-engineer
    workflow: trace the net first, find why it's undriven).

    Returns one of:
      {"kind": "already_driven", net, drivers}
          -> the rail DOES have a power_out on it. A PWR_FLAG would be
             redundant / wrong; the ERC error is stale or a continuity
             issue elsewhere. Caller should NOT add a flag.
      {"kind": "missing_wire", net, source: {tag,x,y}}
          -> exactly one orphan power_out pin (a dangling regulator
             output / unwired source) exists. The real fix is a WIRE
             from that pin to the rail — not a flag.
      {"kind": "no_driver", net}
          -> the rail genuinely has no power_out source anywhere. Fall
             back to the PWR_FLAG strategy.
    Returns None if tracing fails (caller falls back to PWR_FLAG)."""
    try:
        # `from . import trace_net` resolves to the @tool OBJECT (no
        # .trace), silently disabling trace-before-flag. Import the real
        # MODULE — matches the apply_ops / erc_check pattern below.
        TN = importlib.import_module("envil_agent.tools.trace_net")
    except Exception:
        return None
    p = Path(sch_path)
    try:
        whole = TN.trace(p)
        fres = TN.trace(p, x=err_x, y=err_y)
    except Exception:
        return None
    if not whole.get("ok") or not fres.get("ok"):
        return None

    matched = fres.get("matched") or []
    fnet = matched[0] if matched else None
    fname = (fnet or {}).get("name", "")

    if fnet and fnet.get("has_driver"):
        return {"kind": "already_driven", "net": fname,
                "drivers": fnet.get("drivers", [])}

    # Hunt for ORPHAN power_out pins — a regulator output that is dangling
    # (its net has a single connection) or sitting on an unnamed net with
    # no sinks. Those are outputs that clearly still need wiring; a
    # properly-distributed output (already on a named, populated rail) is
    # NOT a candidate, so we never re-route a healthy net.
    candidates: List[Dict[str, Any]] = []
    for net in whole.get("all_nets", []):
        if fname and net.get("name") == fname:
            continue
        orphan = (net.get("pin_count", 0) <= 1) or (
            not net.get("name") and not net.get("power_inputs"))
        if not orphan:
            continue
        for dp in net.get("driver_pins", []):
            if dp.get("etype") == "power_out":
                candidates.append(dp)

    if len(candidates) == 1:
        return {"kind": "missing_wire", "net": fname,
                "source": candidates[0]}
    return {"kind": "no_driver", "net": fname,
            "candidate_count": len(candidates)}


# ----------------------------------------------------------------------
# Generic verb executor (the "last mile").
#
# The taxonomy classifies many ERC types as safe_auto + scores their
# intent confidence, but historically STOPPED there and emitted a
# diagnostic tagged "apply wired in a later phase" because the apply_ops
# verb that would execute the fix did not exist yet. Those verbs now
# exist (add_no_connect, snap_endpoint) — plus delete_wire was always
# there — so a taxonomy entry can set `"executor": "verb"` and this
# bridge turns its `requires_verb` + the ERC report's coordinates into
# concrete apply_ops. One op per ERC location.
#
# SAFETY: this only ever runs for a violation the taxonomy already vetted
# as safe_auto AND that cleared the confidence threshold; every op it
# emits is still re-validated by the per-fix / batch regression guard,
# which rolls back on any new error. Unknown verbs return [] so the
# caller falls back to the historical diagnostic — never a blind apply.
# ----------------------------------------------------------------------
def _ops_from_locations(cls: Dict[str, Any], v: Dict[str, Any],
                         cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build concrete apply_ops for a safe_auto violation whose taxonomy
    executor == 'verb'. Returns [] for an unknown / un-buildable verb so
    the caller emits a diagnostic instead of applying blind."""
    verb = cls.get("requires_verb", "")
    locs = v.get("locations", []) or []
    ops: List[Dict[str, Any]] = []
    for loc in locs:
        try:
            x = float(loc["x"]); y = float(loc["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if verb == "add_no_connect":
            ops.append({"verb": "add_no_connect", "x": x, "y": y})
        elif verb == "snap_endpoint":
            ops.append({"verb": "snap_endpoint", "x": x, "y": y})
        elif verb == "delete_wire":
            tol = float((cfg.get("strategies", {})
                         .get("unconnected_wire_endpoint", {}) or {})
                        .get("delete_wire_tol_mm", 0.1))
            # either_end: KiCad reports only the dangling end's coord, so
            # match a wire by EITHER endpoint, not both (a both-endpoint
            # match would never find a real, non-degenerate dangling wire).
            ops.append({"verb": "delete_wire",
                         "x1": x, "y1": y, "x2": x, "y2": y,
                         "tol": tol, "match": "either_end"})
    return ops


def _safe_auto_proposal(v: Dict[str, Any], cls: Dict[str, Any],
                         cfg: Dict[str, Any],
                         confidence: Optional[float] = None,
                         factors: Optional[List[str]] = None
                         ) -> Dict[str, Any]:
    """For a safe_auto violation whose executor != 'legacy': emit a REAL
    ops proposal when executor=='verb' and the verb is buildable (and the
    `verb_executors` gate is on), else fall back to the historical
    diagnostic ('apply wired in a later phase'). Byte-stable when the gate
    is off or the executor is still 'diagnostic'."""
    factors = factors or []
    if (bool(cfg.get("verb_executors", True))
            and cls.get("executor") == "verb"):
        vops = _ops_from_locations(cls, v, cfg)
        if vops:
            verb = cls.get("requires_verb", "")
            cands = cls.get("candidate_fixes", []) or []
            reason = cands[0] if cands else f"apply {verb}"
            conf_str = (f" (confidence {confidence*100:.0f}%)"
                        if confidence is not None else "")
            return {
                "violation_type": v.get("type", ""),
                "severity": v.get("severity", ""),
                "root_cause": (f"{cls.get('user_name', v.get('type', ''))} "
                                f"— safe_auto via '{verb}'{conf_str}"
                                + (f"; {'; '.join(factors)}" if factors else "")),
                "reason": reason,
                "validation": ("ERC is re-run after the edit; the regression "
                                "guard rolls it back if any new error appears"),
                "side_effects": "none",
                "confidence": confidence if confidence is not None else 0.8,
                "diagnostic_only": False,
                "fix_class": "safe_auto",
                "subclass": cls.get("subclass", ""),
                "candidate_fixes": cands,
                "ops": vops,
            }
    extra = (f"HIGH CONFIDENCE {confidence*100:.0f}% — "
             if confidence is not None else "")
    extra += (f"strategy '{cls.get('strategy', '')}' "
              f"(apply wired in a later phase)")
    if factors:
        extra += f"; {'; '.join(factors)}"
    return _make_diagnostic(v, cls, "safe_auto", confidence=confidence,
                             extra_reason=extra)


def _propose_fixes(violations: List[Dict[str, Any]],
                    cfg: Dict[str, Any],
                    geom: Optional[Dict[str, Any]] = None,
                    sch_path: Optional[str] = None
                    ) -> List[Dict[str, Any]]:
    """Map each violation to a list of apply_ops dicts. Each result
    entry: {violation_type, ops, reason, root_cause, validation,
            confidence, ...}.

    `geom` is the output of `_scan_schematic_geometry(sch_text)`. When
    None, strategies fall back to defaults (less safe — e.g. PWR_FLAG
    placed AT error coord). Caller should always pass it.

    Strategies are intentionally MINIMAL: one fix per violation, no
    chained edits, no global modifications. The caller's apply loop
    validates each fix in isolation and rolls back on regression."""
    proposals: List[Dict[str, Any]] = []
    fix_map = cfg.get("strategies", {}) or {}
    placed_at: List[Tuple[str, float, float]] = []
    refdes_used: Dict[str, set] = {}
    if geom is not None:
        placed_at = geom.get("placed_at", [])
        # Copy so we can mutate (each fix that adds a refdes claims it)
        refdes_used = {k: set(v) for k, v in geom.get("refdes_by_prefix",
                                                       {}).items()}

    # Taxonomy router (Phase 1). Falsy taxonomy -> router off -> legacy
    # behaviour byte-stable. Loaded once for the whole batch.
    tax = _load_taxonomy()
    tax_on = bool(tax) and tax.get("enabled", True)

    for v in violations:
        vtype = v.get("type", "")

        # --- 4-tier classification + Intent Confidence Layer ----------
        # Routes BEFORE the legacy dispatch. forbidden / needs_reasoning
        # / ignore short-circuit here; safe_auto with executor!="legacy"
        # becomes a diagnostic (apply wired in a later phase); only
        # executor=="legacy" falls through to the strategies below.
        if tax_on:
            cls = _classify_violation(v, tax)
            fclass = cls.get("fix_class", "needs_reasoning")
            if fclass == "ignore":
                continue
            # Redundant PWR_FLAG -> REAL safe_auto delete (executor-free).
            # A power-output conflict whose drivers are PWR_FLAG
            # annotations is a duplicate, not a short: delete the extra
            # flag(s). Emitted as concrete delete_component ops and
            # guarded by the per-fix/batch regression check.
            if cls.get("subclass") == "redundant_pwr_flag" \
                    and cls.get("delete_refs"):
                del_refs = cls["delete_refs"]
                # IDENTITY CHECK: only delete refs the schematic confirms
                # are actually PWR_FLAG symbols (by lib_id, not just the
                # #FLG ref-name). If the scan is available and confirms
                # NONE, this is not a flag duplicate after all — refuse
                # and escalate (a real power-output short stays forbidden).
                confirmed = (geom or {}).get("pwr_flag_refs")
                if confirmed is not None:
                    verified = [r for r in del_refs if r in confirmed]
                else:
                    verified = del_refs   # no scan -> trust ref-prefix
                if not verified:
                    proposals.append(_make_diagnostic(
                        v, {"user_name": "Power-output conflict",
                            "subclass": "power_output_conflict",
                            "candidate_fixes": [
                                "Two power outputs share a net but neither is "
                                "a PWR_FLAG — treat as a real driver conflict; "
                                "human review required."]},
                        "forbidden",
                        extra_reason=("conflicting symbols are not confirmed "
                                       "PWR_FLAGs in the schematic")))
                    continue
                ops = [{"verb": "delete_component", "ref": r}
                       for r in verified]
                proposals.append({
                    "violation_type": vtype,
                    "severity": v.get("severity", ""),
                    "root_cause": (
                        f"two or more power-output pins share one net, but "
                        f"they are PWR_FLAG annotations ({', '.join(verified)} "
                        f"redundant) — a duplicate flag, not a real short"),
                    "reason": (f"delete the redundant PWR_FLAG(s) "
                                f"{', '.join(verified)}; one driver is enough"),
                    "validation": ("each ref confirmed as a power:PWR_FLAG "
                                    "symbol; PWR_FLAG carries no current; ERC "
                                    "is re-run after the delete and rolled "
                                    "back if any new error appears"),
                    "side_effects": "none",
                    "confidence": 0.9,
                    "diagnostic_only": False,
                    "fix_class": "safe_auto",
                    "subclass": "redundant_pwr_flag",
                    "candidate_fixes": cls.get("candidate_fixes", []),
                    "ops": ops,
                })
                continue
            if fclass in ("forbidden", "needs_reasoning"):
                proposals.append(_make_diagnostic(v, cls, fclass))
                continue
            # safe_auto
            if cls.get("confidence_required"):
                score, factors = _confidence_score(v, cls, sch_path, tax)
                thr = float(cls.get("confidence_threshold", 0.7))
                if score < thr:
                    proposals.append(_make_diagnostic(
                        v, cls, "needs_reasoning", confidence=score,
                        extra_reason=(
                            f"LOW CONFIDENCE {score*100:.0f}% < "
                            f"{thr*100:.0f}% — {'; '.join(factors)}")))
                    continue
                if cls.get("executor") != "legacy":
                    proposals.append(_safe_auto_proposal(
                        v, cls, cfg, confidence=score, factors=factors))
                    continue
            elif cls.get("executor") != "legacy":
                proposals.append(_safe_auto_proposal(v, cls, cfg))
                continue
            # executor == "legacy" -> fall through to the dispatch below.

        strategy = fix_map.get(vtype, {}) if isinstance(fix_map, dict) else {}
        if not strategy.get("enabled", True):
            continue
        ops: List[Dict[str, Any]] = []
        reason = ""
        root_cause = ""
        validation = ""
        side_effects = "none"
        confidence = 0.5
        diagnostic_only = False   # set True when a fix is intentionally
                                  # NOT proposed but the diagnosis matters

        if vtype == "unconnected_wire_endpoint":
            # SURGICAL deletion: KiCad reports the dangling endpoint
            # coord. Use a TIGHT tolerance (0.1 mm) so we only remove
            # a wire whose endpoint sits EXACTLY at the reported coord
            # — never a nearby valid wire. The old default (1.27 mm)
            # was wide enough to delete a perfectly-good wire that
            # happened to terminate one grid-cell away from the stub.
            for loc in v.get("locations", []):
                tol = float(strategy.get("delete_wire_tol_mm", 0.1))
                ops.append({"verb": "delete_wire",
                              "x1": loc["x"], "y1": loc["y"],
                              "x2": loc["x"], "y2": loc["y"],
                              "tol": tol})
            root_cause = (f"{len(v['locations'])} wire stub(s) terminate "
                           f"at a point with no pin / junction / other "
                           f"wire — leftover from an edit")
            reason = "delete the orphan stub only (tight 0.1mm tolerance)"
            validation = ("only the wire whose endpoint is at the exact "
                           "ERC-reported coord is removed; surrounding "
                           "wires untouched")
            confidence = 0.8

        elif vtype == "duplicate_reference":
            # SAFE rename: pick the NEXT available number for the
            # refdes prefix instead of appending "_dup". KiCad's
            # annotator requires LETTERS+DIGITS — "R1_dup" violated
            # the format and corrupted the file. Now: R1 (dup) →
            # next free R-number, e.g. R7.
            for loc in v.get("locations", []):
                m = re.search(r"Symbol\s+(\S+)\s+", loc.get("descr", ""))
                if not m:
                    continue
                ref = m.group(1)
                m2 = _REFDES_SPLIT_RE.match(ref)
                if not m2:
                    continue
                prefix = m2.group(1)
                used = refdes_used.setdefault(prefix, set())
                new_ref = _next_refdes(prefix, used)
                ops.append({"verb": "set_property",
                              "ref": ref, "key": "Reference",
                              "value": new_ref})
            root_cause = ("two symbols share the same reference "
                           "designator — KiCad needs every ref unique")
            reason = ("rename duplicate(s) to the next free number "
                       "in the same letter family")
            validation = ("new refdes follows letter+digit format and "
                           "doesn't collide with any existing ref")
            confidence = 0.7

        elif vtype == "isolated_pin_label":
            continue   # manual review — engine already labels rails correctly

        elif vtype == "power_pin_not_driven":
            # CIRCUIT-AWARE repair (2026-06-01). Before reaching for a
            # PWR_FLAG we TRACE the net (trace_net) to find the real root
            # cause — exactly the senior-engineer workflow:
            #   already_driven -> the rail HAS a power_out; a flag is wrong
            #                     -> propose nothing, surface the diagnosis
            #   missing_wire   -> a dangling regulator output exists
            #                     -> draw the WIRE (the real fix)
            #   no_driver      -> genuinely no source -> PWR_FLAG fallback
            #
            # `trace_before_flag` (default true) gates this. Set false to
            # restore the legacy PWR_FLAG-first behaviour for every error.
            trace_first = bool(strategy.get("trace_before_flag", True))
            existing_flag_count = sum(
                1 for p in proposals
                for o in p.get("ops", [])
                if o.get("lib_id") == "power:PWR_FLAG"
            )
            flag_idx = existing_flag_count

            for loc in v.get("locations", []):
                err_x = float(loc["x"])
                err_y = float(loc["y"])
                diag = (_diagnose_power_net(sch_path, err_x, err_y)
                        if (trace_first and sch_path) else None)

                if diag and diag["kind"] == "already_driven":
                    # The rail is actually driven — do NOT add a flag.
                    drv = ", ".join(diag.get("drivers", [])) or "a power_out pin"
                    root_cause = (f"net {diag['net'] or '(at error coord)'} "
                                   f"ALREADY has driver {drv}; the ERC error "
                                   f"is stale or a wire/label-continuity "
                                   f"issue elsewhere — a PWR_FLAG would mask "
                                   f"the real problem")
                    reason = ("no fix applied — trace shows the rail is "
                               "driven; investigate wire/label continuity")
                    validation = "trace_net confirms a power_out is on this net"
                    confidence = 0.5
                    diagnostic_only = True
                    continue

                if diag and diag["kind"] == "missing_wire":
                    src = diag["source"]
                    # The real fix: wire the orphan regulator output to the
                    # rail. apply_ops L-routes around component bodies.
                    ops.append({"verb": "add_wire",
                                  "x1": float(src["x"]), "y1": float(src["y"]),
                                  "x2": err_x, "y2": err_y})
                    root_cause = (f"net {diag['net'] or '(rail)'} has the "
                                   f"power_out pin {src['tag']} nearby but it "
                                   f"is unwired — MISSING WIRE, not a missing "
                                   f"driver")
                    reason = (f"draw the missing wire from {src['tag']} "
                               f"(power_out) to the rail")
                    validation = ("source is the only orphan power_out in the "
                                   "schematic; per-fix ERC re-run + rollback "
                                   "guards against a wrong join")
                    confidence = 0.7
                    continue

                # no_driver (or tracing unavailable) -> PWR_FLAG fallback.
                # SAFE placement + WIRED bond (legacy two-step):
                #   1. add PWR_FLAG at a coord clear of symbols
                #   2. wire flag pin -> floating pin coord (else KiCad
                #      fires 'pin not connected' on the flag itself)
                flag_idx += 1
                fx, fy = _find_clear_flag_pos(err_x, err_y, placed_at)
                placed_at.append(("power:PWR_FLAG", fx, fy))
                ops.append({"verb": "add_component",
                              "ref": f"#FLG_AUTO{flag_idx:03d}",
                              "lib_id": "power:PWR_FLAG",
                              "value": "PWR_FLAG",
                              "x": fx, "y": fy})
                ops.append({"verb": "add_wire",
                              "x1": fx, "y1": fy,
                              "x2": err_x, "y2": err_y})
                if not root_cause:
                    root_cause = ("rail has no `power_output` driver anywhere "
                                   "— KiCad ERC needs at least one power_out "
                                   "pin per rail")
                    reason = ("place PWR_FLAG adjacent to the floating pin AND "
                               "draw a wire connecting flag pin to the rail")
                    validation = ("flag coord scanned to avoid existing "
                                   "symbols; wire bonds flag to rail so the "
                                   "flag itself is connected")
                    confidence = 0.6

        elif vtype == "multiple_net_names":
            continue   # need user intent — skip

        elif vtype == "missing_power_pin":
            continue   # multi-unit placement — out of scope

        if ops or diagnostic_only:
            proposals.append({
                "violation_type": vtype,
                "severity": v.get("severity", ""),
                "root_cause": root_cause,
                "reason": reason,
                "validation": validation,
                "side_effects": side_effects,
                "confidence": confidence,
                "diagnostic_only": diagnostic_only,
                "ops": ops,
            })
    return proposals


def _sort_violations_by_priority(violations: List[Dict[str, Any]],
                                  cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Order violations by JSON-driven priority. Fatal-connectivity
    issues fix first, then power, then junction/layout. Same violation
    type stays in source-report order (stable sort).

    `priority_order` is a list of violation types from most-critical to
    least. Violations not in the list go LAST (priority = +inf)."""
    order = cfg.get("priority_order", [
        "pin_not_connected",
        "unconnected_wire_endpoint",
        "duplicate_reference",
        "power_pin_not_driven",
        "missing_power_pin",
        "isolated_pin_label",
        "multiple_net_names",
    ])
    prio = {t: i for i, t in enumerate(order)}
    return sorted(violations,
                   key=lambda v: prio.get(v.get("type", ""), 9999))


@tool(
    name="erc_autofix",
    description=(
        "INTELLIGENT ERC repair: classifies each violation and proposes "
        "concrete apply_ops fixes. Optional `apply: true` executes them. "
        "Triggers: 'fix the ERC errors', 'auto-fix design issues', "
        "'clean up the schematic', 'erc fix pannu' (Tanglish).\n"
        "\n"
        "Report source — pass ONE of:\n"
        '  {"path": "...sch"}                  # run kicad-cli on this sch\n'
        '  {"path": "...sch", "report_path": "C:/Users/.../ERC.rpt"}\n'
        "                                        # use an existing report file\n"
        '  {"path": "...sch", "report_text": "<paste of ERC errors>"}\n'
        "                                        # use pasted/OCRd text\n"
        "When `report_path` or `report_text` is provided, kicad-cli is "
        "NOT invoked — the tool parses the supplied report directly. Use "
        "this when the user uploads an ERC.rpt file, pastes ERC errors "
        "as text, or shares a screenshot (call this with text OCRd from "
        "the image).\n"
        "\n"
        "Optional:\n"
        '  {"apply": true}                       # execute fixes (default false)\n'
        '  {"only_severity": "error"}            # skip warnings\n'
        "Strategy table in layout_config.json:erc_autofix.strategies."
    ),
    input_schema={"path": str},
)
async def erc_autofix(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(args.get("path", "")).strip()).expanduser()
    if not path.exists():
        return {"content": [{"type": "text",
                              "text": f"ERROR: not found: {path}"}],
                 "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                              "text": "erc_autofix disabled in layout_config.json"}],
                 "is_error": True}

    apply = bool(args.get("apply", False))
    only_severity = str(args.get("only_severity", "")).lower()
    report_path_arg = str(args.get("report_path", "")).strip()
    report_text_arg = str(args.get("report_text", "")).strip()

    # Step 1: obtain the ERC report. Three input paths:
    #   a) user-supplied `report_path` (uploaded .rpt / .erc.txt)
    #   b) user-supplied `report_text` (paste / OCRd from screenshot)
    #   c) fallback — run kicad-cli sch erc on `path`
    # HARD RULE: never guess. If (c) fails AND (a)+(b) absent, surface
    # the upstream error so the chat AI shows the real cause to the
    # user instead of fabricating violations from circuit topology.
    report_file = ""
    report_text = ""
    if report_text_arg:
        report_text = report_text_arg
    elif report_path_arg:
        rp = Path(report_path_arg).expanduser()
        if not rp.exists():
            return {"content": [{"type": "text",
                                  "text": (f"ERROR: report_path not found: "
                                            f"{rp}")}],
                     "is_error": True}
        try:
            report_text = rp.read_text(encoding="utf-8", errors="replace")
            report_file = str(rp)
        except OSError as exc:
            return {"content": [{"type": "text",
                                  "text": f"ERROR: cannot read report: {exc}"}],
                     "is_error": True}
    else:
        ERC = importlib.import_module("envil_agent.tools.erc_check")
        erc_res = await ERC.erc_check.handler({"path": str(path)})
        if erc_res.get("is_error"):
            upstream = ""
            try:
                upstream = erc_res["content"][0]["text"]
            except Exception:
                pass
            return {
                "content": [{"type": "text",
                              "text": (f"ERROR: erc_check failed - cannot "
                                        f"propose fixes without an actual "
                                        f"report.\n  upstream: {upstream}\n"
                                        f"Hint: upload the ERC.rpt file or "
                                        f"paste the ERC error text and "
                                        f"re-run.")}],
                "is_error": True,
            }
        try:
            erc_summary = json.loads(erc_res["content"][0]["text"])
        except Exception:
            return {"content": [{"type": "text",
                                  "text": ("ERROR: failed to parse erc_check "
                                            "output - kicad-cli may have "
                                            "produced an unexpected report "
                                            "format.")}],
                     "is_error": True}

        report_file = erc_summary.get("report_file") or ""
        if not report_file or not Path(report_file).exists():
            return {"content": [{"type": "text",
                                  "text": ("ERROR: ERC report not on disk - "
                                            "kicad-cli ran but produced no "
                                            "file. Cannot propose fixes "
                                            "blind.")}],
                     "is_error": True}
        try:
            report_text = Path(report_file).read_text(encoding="utf-8")
        except OSError as exc:
            return {"content": [{"type": "text",
                                  "text": f"ERROR: cannot read kicad-cli report: {exc}"}],
                     "is_error": True}

    # Step 2: parse the report into structured violations. report_text
    # was already loaded from whichever source step 1 used (uploaded
    # file, pasted text, or kicad-cli output).
    violations = _parse_erc_report(report_text)
    if only_severity:
        violations = [v for v in violations
                       if v.get("severity") == only_severity]

    # Step 2b: priority sort. Fatal-connectivity issues fix first so
    # later strategies see a sane net topology. JSON-driven order.
    violations = _sort_violations_by_priority(violations, cfg)

    # Step 2c: HIERARCHY ROUTING. For a multi-sheet project the ERC report
    # groups violations under `***** Sheet /NAME/` headers and the coords
    # are LOCAL to that child sheet. Map each violation to the child
    # .kicad_sch that owns it so the fix is proposed against THAT file's
    # geometry and applied THERE (at the already-correct local coords).
    # Without this, every flag landed on the parent canvas at child-local
    # coords — connected to nothing — which fixed nothing and ADDED new
    # "#FLG Pin not connected" errors. Gated by `hierarchy_aware` (default
    # true); a flat design has no child sheets so the map is empty and the
    # whole block collapses to the single-file path below, byte-stable.
    hier_aware = bool(cfg.get("hierarchy_aware", True))
    sheet_file_map: Dict[str, Path] = (
        _resolve_sheet_files(path) if hier_aware else {})
    for v in violations:
        v["_file"] = str(path)
    routed = False
    if sheet_file_map:
        for v in violations:
            cf = _sheet_to_file(v.get("sheet", "/"), sheet_file_map, path)
            v["_file"] = str(cf)
            if cf.resolve() != path.resolve():
                routed = True

    # Step 2d: read each target sheet ONCE for geometry context (avoid
    # placing PWR_FLAGs on top of components, pick the next free refdes).
    # Strategies need the geometry of the file the fix lands in, so scan
    # per target file, not just the parent.
    geom_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def _geom_for(fp: str) -> Optional[Dict[str, Any]]:
        if fp not in geom_cache:
            try:
                geom_cache[fp] = _scan_schematic_geometry(
                    Path(fp).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                geom_cache[fp] = None
        return geom_cache[fp]

    # Step 3: propose fixes. Group by owning file (order-preserving, so the
    # priority sort above still holds within each sheet) and propose each
    # group against its own geometry. Flat design => one group on `path` =>
    # identical to the legacy single `_propose_fixes` call.
    if routed:
        # dict preserves insertion order (py3.7+), so the priority sort holds.
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for v in violations:
            groups.setdefault(v["_file"], []).append(v)
        proposals = []
        for fpath, vios in groups.items():
            ps = _propose_fixes(vios, cfg, geom=_geom_for(fpath),
                                 sch_path=fpath)
            for p in ps:
                p["_file"] = fpath
            proposals.extend(ps)
    else:
        proposals = _propose_fixes(violations, cfg, geom=_geom_for(str(path)),
                                    sch_path=str(path))
        for p in proposals:
            p["_file"] = str(path)
    total_ops = sum(len(p["ops"]) for p in proposals)

    if not proposals:
        # Always show the verbatim violations so the chat AI can quote
        # them — never just say "no fix applies" without the list.
        lines = [
            f"ERC found {len(violations)} violation(s) "
            f"but no auto-fix strategy currently applies.",
            "",
            "Violations (verbatim):",
        ]
        for j, v in enumerate(violations, 1):
            sev = v.get("severity") or "?"
            locs = v.get("locations", []) or []
            loc_str = ""
            if locs:
                first = locs[0]
                loc_str = (f" @({first.get('x')},{first.get('y')}): "
                            f"{first.get('descr', '')}")
                if len(locs) > 1:
                    loc_str += f" (+{len(locs) - 1} more loc)"
            lines.append(f"  V{j}. [{sev}] {v.get('type', '?')}{loc_str}")
        lines.append("")
        lines.append("Manual review needed for these violation types.")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": True, "violations": violations, "proposals": [],
        }

    # Step 4: apply with a signature-aware regression guard.
    #
    # HARD invariant (user requirement): applying fixes must NEVER leave
    # the schematic with MORE errors — or with a NEW violation — than it
    # started with. Three modes (config `regression_guard`):
    #
    #   "per_fix"  — each proposal applied IN ISOLATION, ERC re-run, and
    #                rolled back via undo_last_edit if THIS fix raised the
    #                error count OR introduced a signature not present
    #                before it. Surgical: good fixes from earlier steps
    #                stay. Cost: one kicad-cli ERC per fix (slow).
    #   "batch"    — apply ALL proposals, run ERC ONCE, and if the final
    #                report has more errors than baseline OR any NEW
    #                signature, restore the original file wholesale. Cost:
    #                a single ERC run; still guarantees no-regression, but
    #                an all-or-nothing rollback. DEFAULT — best balance.
    #   "off"      — apply everything, no verification (fastest, unsafe).
    #
    # Back-compat: when `regression_guard` is absent the mode is derived
    # from the legacy `validate_each_fix` flag (true -> per_fix, false ->
    # off) so existing configs stay byte-stable. `signature_regression_
    # guard` (default true) adds the NEW-signature check on top of the
    # legacy error-count check; set false for pure count-based legacy.
    incremental_results: List[Dict[str, Any]] = []
    applied_results: Dict[str, Any] = {}
    if apply:
        OPS = importlib.import_module("envil_agent.tools.apply_ops")
        guard_mode = str(cfg.get("regression_guard", "")).lower().strip()
        if guard_mode not in ("off", "batch", "per_fix"):
            guard_mode = ("per_fix"
                          if bool(cfg.get("validate_each_fix", True))
                          else "off")
        sig_guard = bool(cfg.get("signature_regression_guard", True))

        # Baseline: error count + ERROR-only signature set BEFORE any
        # fix. Parsed from the full original report_text (NOT the
        # possibly only_severity-filtered `violations`) so both sides of
        # every comparison use the identical source + parser as the
        # re-run — no asymmetry, no false rollback.
        baseline_vios = _parse_erc_report(report_text)
        baseline_errors = sum(1 for v in baseline_vios
                               if v.get("severity") == "error")
        baseline_sigs = (_error_signature_set(baseline_vios)
                         if sig_guard else set())
        # Whole-file snapshot for the batch-mode wholesale restore. With
        # hierarchy routing a fix can land in ANY child .kicad_sch, so we
        # snapshot every file a proposal targets (plus the parent). Flat
        # design => just {parent: bytes} => identical to the old single-file
        # snapshot.
        snapshot_files = {p.get("_file", str(path))
                          for p in proposals if p.get("ops")}
        snapshot_files.add(str(path))
        original_bytes_map: Dict[str, bytes] = {}
        for fp in snapshot_files:
            try:
                original_bytes_map[fp] = Path(fp).read_bytes()
            except OSError:
                pass

        running_errors = baseline_errors
        running_sigs = set(baseline_sigs)   # per_fix incremental state
        total_applied = 0
        total_failed = 0
        total_rolled_back = 0
        batch_rolled_back = False
        batch_restore_failed = False

        for idx, p in enumerate(proposals, 1):
            vtype = p["violation_type"]
            # Diagnostic-only proposals (forbidden / needs_reasoning /
            # not-yet-wired safe_auto) carry no ops — skip apply, record.
            if not p.get("ops"):
                incremental_results.append({
                    "step": idx, "violation_type": vtype,
                    "root_cause": p.get("root_cause", ""),
                    "ops_applied": 0, "ops_failed": 0, "rolled_back": False,
                    "pre_errors": running_errors, "post_errors": running_errors,
                })
                continue
            # Apply ONE proposal's ops to the file that OWNS this violation
            # (the child sheet under hierarchy routing; the parent for a
            # flat design). apply_ops auto-snapshots on entry — so a single
            # undo_last_edit on the same file rolls back ONLY this proposal.
            p_file = p.get("_file", str(path))
            r = await OPS.apply_ops.handler({"path": p_file,
                                               "ops": p["ops"]})
            try:
                op_result = json.loads(r["content"][0]["text"])
            except Exception:
                op_result = {"parse_error": True,
                             "ops_applied": 0, "ops_failed": len(p["ops"])}

            applied_n = int(op_result.get("ops_applied", 0))
            failed_n = int(op_result.get("ops_failed", 0))
            total_failed += failed_n

            rolled_back = False
            post_errors = running_errors
            if guard_mode == "per_fix" and applied_n > 0:
                # Re-run ERC; roll back if THIS fix raised the error count
                # or introduced a signature absent from the pre-fix state.
                erc = await _rerun_erc(path)
                if erc is None:
                    # Unverifiable (flaky/parse error) — conservative
                    # keep, matching the historical behaviour.
                    total_applied += applied_n
                else:
                    new_errors = (erc["errors"] if erc["errors"] >= 0
                                  else running_errors)
                    new_sigs = erc["sigs"]
                    introduced = (new_sigs - running_sigs) if sig_guard else set()
                    if new_errors > running_errors or introduced:
                        # Undo on the SAME file the fix landed in (child
                        # sheet under routing) — apply_ops keeps a per-file
                        # snapshot, so this reverts exactly this proposal.
                        await OPS.apply_ops.handler({
                            "path": p_file,
                            "ops": [{"verb": "undo_last_edit"}]})
                        rolled_back = True
                        total_rolled_back += 1
                        post_errors = running_errors
                    else:
                        running_errors = new_errors
                        running_sigs = new_sigs if sig_guard else running_sigs
                        post_errors = new_errors
                        total_applied += applied_n
            else:
                # batch / off — defer verification to the post-loop check.
                total_applied += applied_n

            incremental_results.append({
                "step": idx,
                "violation_type": vtype,
                "root_cause": p.get("root_cause", ""),
                "ops_applied": applied_n,
                "ops_failed":  failed_n,
                "rolled_back": rolled_back,
                "pre_errors":  running_errors,
                "post_errors": post_errors,
            })

        # Batch-mode verification: ONE ERC run over the whole project
        # (kicad-cli on the parent walks every child sheet). Any regression
        # (more ERRORS than baseline, or a brand-new error-signature)
        # triggers a wholesale restore of EVERY file a fix touched — the
        # all-or-nothing guarantee that the project is never worse. Each
        # restore is ATOMIC (_atomic_restore) so a locked file can never be
        # left truncated; if any file can't be rewritten (open in eeschema
        # / stale .lck) we flag it for a plain-English message.
        if guard_mode == "batch":
            erc = await _rerun_erc(path)
            if erc is not None:
                introduced = (erc["sigs"] - baseline_sigs) if sig_guard else set()
                final_errors = (erc["errors"] if erc["errors"] >= 0
                                else baseline_errors)
                if (final_errors > baseline_errors or introduced) \
                        and original_bytes_map:
                    all_ok = True
                    for fp, data in original_bytes_map.items():
                        if not _atomic_restore(Path(fp), data):
                            all_ok = False
                    if all_ok:
                        batch_rolled_back = True
                        total_applied = 0
                        running_errors = baseline_errors
                    else:
                        # At least one file locked — partial state. Surface
                        # the failure rather than claim a clean rollback.
                        batch_restore_failed = True
                        running_errors = final_errors
                else:
                    running_errors = final_errors
            # erc is None -> unverifiable -> keep fixes (conservative).

        applied_results = {
            "guard_mode": guard_mode,
            "signature_guard": sig_guard,
            "ops_applied": total_applied,
            "ops_failed":  total_failed,
            "rolled_back": total_rolled_back,
            "batch_rolled_back": batch_rolled_back,
            "batch_restore_failed": batch_restore_failed,
            "baseline_errors": baseline_errors,
            "final_errors":   running_errors,
            "per_fix": incremental_results,
        }

    # Step 5: report. Quote the actual violations the report contained
    # FIRST (so the chat AI is forced to surface real errors instead of
    # paraphrasing), then list proposed fixes.
    fix_count = sum(1 for p in proposals if p.get("ops"))
    diag_count = len(proposals) - fix_count
    lines = [
        f"ERC auto-fix on {path.name}",
        f"  parsed {len(violations)} violation(s) from {Path(report_file).name}",
        f"  proposed {fix_count} fix(es) + {diag_count} diagnostic(s), "
        f"{total_ops} ops total",
    ]
    if routed:
        n_files = len({v["_file"] for v in violations})
        lines.append(f"  HIERARCHY: routed fixes across {n_files} sheet "
                     f"file(s) by their `***** Sheet` headers")
    lines.append("")
    lines.append("Violations (verbatim from kicad-cli report):")
    for j, v in enumerate(violations, 1):
        sev = v.get("severity") or "?"
        locs = v.get("locations", []) or []
        loc_str = ""
        if locs:
            first = locs[0]
            loc_str = f" @({first.get('x')},{first.get('y')}): {first.get('descr', '')}"
            if len(locs) > 1:
                loc_str += f" (+{len(locs) - 1} more loc)"
        sheet_tag = ""
        if routed:
            sheet_tag = f" {{{v.get('sheet', '/')}}}-> {Path(v['_file']).name}"
        lines.append(f"  V{j}. [{sev}] {v.get('type', '?')}{loc_str}{sheet_tag}")
    lines.append("")
    lines.append("Proposed fixes (incremental, validated per step):")
    for i, p in enumerate(proposals, 1):
        sev = p["severity"] or "?"
        fc = p.get("fix_class")
        tag = f" <{fc}>" if fc else ""
        lines.append(f"  {i}. [{sev}] {p['violation_type']}{tag}")
        if p.get("root_cause"):
            lines.append(f"     ROOT CAUSE: {p['root_cause']}")
        head = "NOTE" if p.get("diagnostic_only") else "MINIMAL FIX"
        lines.append(f"     {head}: {p['reason']}")
        for op in p["ops"][:3]:
            verb = op.get("verb", "")
            extra = ", ".join(f"{k}={v}" for k, v in op.items()
                                if k != "verb")
            lines.append(f"        -> {verb}({extra})")
        if len(p["ops"]) > 3:
            lines.append(f"        -> ... ({len(p['ops']) - 3} more ops)")
        for cf in (p.get("candidate_fixes") or [])[:3]:
            lines.append(f"     CANDIDATE: {cf}")
        if p.get("validation"):
            lines.append(f"     VALIDATION: {p['validation']}")
        if p.get("side_effects"):
            lines.append(f"     SIDE EFFECTS: {p['side_effects']}")
    lines.append("")
    if apply:
        gm = applied_results.get("guard_mode", "?")
        lines.append(f"APPLIED (regression_guard={gm}, "
                       f"signature_guard={applied_results.get('signature_guard')}):")
        lines.append(f"  baseline_errors={applied_results.get('baseline_errors', '?')}  "
                       f"final_errors={applied_results.get('final_errors', '?')}")
        lines.append(f"  ops_applied={applied_results.get('ops_applied', '?')}  "
                       f"ops_failed={applied_results.get('ops_failed', '?')}  "
                       f"rolled_back={applied_results.get('rolled_back', '?')}")
        batch_rb = bool(applied_results.get("batch_rolled_back"))
        if batch_rb:
            lines.append("  ** BATCH ROLLED BACK ** — the fixes together "
                           "introduced a new error, so the original "
                           "schematic was restored UNCHANGED (no regression).")
        if applied_results.get("batch_restore_failed"):
            lines.append("  ** RESTORE FAILED ** — a regression was detected "
                           "but the schematic could not be rewritten (it is "
                           "likely open in KiCad or has a stale .kicad_sch.lck "
                           "lock). Close it / remove the lock and re-run.")
        per_fix = applied_results.get("per_fix") or []
        for step in per_fix:
            if batch_rb and step.get("ops_applied", 0) > 0 \
                    and not step.get("rolled_back"):
                status = "reverted (batch)"
            elif step.get("rolled_back"):
                status = "ROLLED BACK"
            elif step.get("ops_applied", 0) > 0:
                status = "kept"
            else:
                status = "skip"
            lines.append(
                f"    step{step.get('step')} {step.get('violation_type')}: "
                f"applied={step.get('ops_applied', 0)}  "
                f"post_errors={step.get('post_errors', '?')}  [{status}]"
            )
    else:
        lines.append("(preview only — pass apply=true to execute "
                       "with per-fix validation + rollback)")

    report_text = "\n".join(lines)
    # Auto-refresh KiCad after a fix lands — the SAME mechanism
    # build_circuit and apply_ops use. The server (server.py) reads the
    # FIRST text block as JSON (tool_result = json.loads(content[0].text))
    # and, when it carries a ".kicad_sch" `path`, broadcasts an
    # open_file + revert IPC to eeschema so the open canvas RELOADS with
    # no manual File>Revert. erc_autofix previously returned the bare
    # human report here, so json.loads failed -> tool_result was None ->
    # the refresh block was skipped (apply_ops works because it already
    # json.dumps its summary, with `path`, into the text block).
    # Now we emit JSON carrying the `path` (gated on a fix ACTUALLY
    # landing, so a preview / no-op run never triggers a needless revert)
    # plus the full report under "report" for the chat AI to surface.
    # Gated by `result_as_json` (default true); false => legacy prose text
    # (byte-stable, but no auto-refresh).
    if cfg.get("result_as_json", True):
        landed = bool(apply and applied_results.get("ops_applied", 0) > 0)
        text_block = json.dumps({
            "path": str(path) if landed else None,
            "ops_applied": (applied_results.get("ops_applied", 0)
                            if apply else 0),
            "report": report_text,
        })
    else:
        text_block = report_text

    return {
        "content": [{"type": "text", "text": text_block}],
        "ok": True,
        "violations": violations,
        "proposals": proposals,
        "applied": applied_results if apply else None,
    }
