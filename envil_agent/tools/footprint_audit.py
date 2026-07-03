"""Tool: footprint_audit — the per-BOARD footprint-assignment validator.

This is the "verify it's legal" half of the assign/validate split. The
engine + ``_resolve_default_footprint`` ASSIGN a footprint to every part;
this tool VERIFIES each assignment on a *rendered* ``.kicad_sch`` against
the physical ``.kicad_mod`` on disk. It never mutates the board — it reads,
checks, and reports, so a model-in-the-loop pipeline stays trustworthy.

It complements ``check_pins.py`` (which validates the config maps
statically, off-board) by validating the actual generated schematic, and
it reuses generation's own footprint-file resolution (``pcb_gen._fp_lib_dirs``
+ ``_find_kicad_mod``) so it never false-flags a system-library part as
"missing".

Rules checked (all gated in ``config/footprint_audit.json``):
  1 explicit_package   — footprint must be explicit, not guessed from the
                         symbol; fires on ``by_prefix`` / ``symbol_default``
                         provenance.
  2 pin_pad_match      — every symbol pin number ↔ footprint pad number.
  - footprint_resolves — the Lib:Footprint id resolves to a real .kicad_mod
                         (empty field also fails).
  5 tech_type          — pad tech (THT vs SMD) matches the assembly process.
  6 connector_review   — connectors / mechanical parts flagged for human
                         dimensional review.
  7 polarity_pin1      — polarized / keyed parts must have a pad '1'.
  8 courtyard/model_3d — footprint carries courtyard geometry; warn if no
                         3D model.

Verdict is reported in WORDS, never a percentage (feedback_no_percentage_scores).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    """Load config/footprint_audit.json once, cache it. Returns {} on any
    read / parse error so the tool degrades to "disabled" rather than
    crashing on a bad edit."""
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "footprint_audit.json"
        _CFG_CACHE = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CFG_CACHE = {}
    return _CFG_CACHE


def _rule(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    r = (cfg.get("rules", {}) or {}).get(name, {})
    return r if isinstance(r, dict) else {}


def _rule_on(cfg: Dict[str, Any], name: str) -> bool:
    return bool(_rule(cfg, name).get("enabled", True))


def _rule_sev(cfg: Dict[str, Any], name: str, default: str = "warning") -> str:
    return str(_rule(cfg, name).get("severity", default))


# --------------------------------------------------------------------------- #
# .kicad_sch component enumeration
# --------------------------------------------------------------------------- #

def _atom(x: Any) -> str:
    """Value of a sexpdata Symbol / str / number as a plain string."""
    if x is None:
        return ""
    v = getattr(x, "value", None)
    if callable(v):
        return str(v())
    return str(x)


def _resolve_sch_path(raw: str) -> Optional[Path]:
    """Accept a .kicad_sch, a sibling .kicad_pcb / .kicad_pro, or a project
    dir and return the .kicad_sch to audit (footprints live in the schematic
    Footprint field). Returns None when nothing usable is found."""
    p = Path(raw).expanduser()
    if p.is_dir():
        cands = sorted(p.glob("*.kicad_sch"))
        return cands[0] if cands else None
    if p.suffix.lower() == ".kicad_sch":
        return p if p.exists() else None
    if p.suffix.lower() in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        sib = p.with_suffix(".kicad_sch")
        if sib.exists():
            return sib
        cands = sorted(p.parent.glob("*.kicad_sch"))
        return cands[0] if cands else None
    return p if p.exists() else None


def _iter_components(sch_path: Path, skip_prefixes: List[str]
                     ) -> List[Dict[str, str]]:
    """Enumerate real placed symbols as {ref, lib_id, value, footprint}.
    First instance per reference wins (multi-unit parts share one footprint).
    Skips virtual refs (power ports, PWR_FLAG). TRAVERSES hierarchical child
    sheets (a hierarchical root holds only sheet pointers, not components), so
    a multi-sheet design is fully audited — not reported as "NO PARTS"."""
    out: List[Dict[str, str]] = []
    _collect_sheet(sch_path, skip_prefixes, out, set(), set())
    return out


def _collect_sheet(sch_path: Path, skip_prefixes: List[str],
                   out: List[Dict[str, str]], seen: set, visited: set) -> None:
    """Recursive worker: collect components from one .kicad_sch and every child
    sheet it references (property "Sheetfile"). `seen` dedupes refs across all
    sheets; `visited` guards against cycles / repeated shared sheets."""
    from ..kicad.document import _head, _prop
    import sexpdata

    sch_path = Path(sch_path)
    key = str(sch_path.resolve())
    if key in visited:
        return
    visited.add(key)
    try:
        root = sexpdata.loads(sch_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list):
            continue
        h = _head(node)
        if h == "symbol":
            ref = _prop(node, "Reference") or ""
            if not ref or any(ref.startswith(pre) for pre in skip_prefixes):
                continue
            if ref in seen:
                continue
            seen.add(ref)
            lib_id = ""
            for child in node[1:]:
                if isinstance(child, list) and _head(child) == "lib_id" and len(child) >= 2:
                    lib_id = str(child[1])
                    break
            out.append({
                "ref": ref,
                "lib_id": lib_id,
                "value": _prop(node, "Value") or "",
                "footprint": _prop(node, "Footprint") or "",
            })
        elif h == "sheet":
            sf = _prop(node, "Sheetfile")
            if sf:
                child = sch_path.parent / str(sf)
                if child.exists():
                    _collect_sheet(child, skip_prefixes, out, seen, visited)


def _ir_footprints(sch_path: Path) -> Dict[str, str]:
    """Read the {ref: footprint} the architect emitted, from the
    ``.envil-ir.json`` sidecar. A non-empty entry means the footprint was
    an explicit design input (best provenance). Empty / no sidecar → the
    engine resolved it, and the tracer decides the tier."""
    side = sch_path.with_suffix(".envil-ir.json")
    if not side.exists():
        return {}
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        comps = (data.get("ir") or {}).get("components") or []
        return {c.get("ref", ""): (c.get("footprint") or "").strip()
                for c in comps if c.get("ref")}
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
# .kicad_mod metadata
# --------------------------------------------------------------------------- #

def _mod_meta(node: list) -> Dict[str, Any]:
    """Pull the fields the audit needs out of a parsed (footprint ...) node:
    pad numbers→tech, declared attr, courtyard presence, 3D-model presence."""
    from ..layout.pcb_gen import _head, _child
    pads: Dict[str, str] = {}
    has_courtyard = False
    has_model = False
    attr_flags: List[str] = []
    crtyd_layers = {"F.CrtYd", "B.CrtYd"}

    for c in node[1:] if isinstance(node, list) else []:
        if not isinstance(c, list):
            continue
        head = _head(c)
        if head == "pad" and len(c) >= 3:
            num = _atom(c[1]).strip('"')
            ptype = _atom(c[2])           # thru_hole | smd | np_thru_hole | connect
            # First definition per number wins; a real pad type beats "connect".
            if num not in pads or pads[num] == "connect":
                pads[num] = ptype
        elif head == "model":
            has_model = True
        elif head == "attr":
            attr_flags = [_atom(x) for x in c[1:]]
        elif head in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
            lay = _child(c, "layer")
            if lay and len(lay) >= 2 and str(lay[1]).strip('"') in crtyd_layers:
                has_courtyard = True
    return {
        "pads": pads,
        "attr": attr_flags,
        "has_courtyard": has_courtyard,
        "has_model": has_model,
    }


def _pad_tech(pads: Dict[str, str], attr: List[str]) -> Tuple[bool, bool]:
    """(has_tht, has_smd) from pad tech tokens, with the footprint's own
    (attr ...) as a tiebreaker for footprints whose pads are ambiguous."""
    has_tht = any(t in ("thru_hole", "np_thru_hole") for t in pads.values())
    has_smd = any(t == "smd" for t in pads.values())
    if not has_tht and not has_smd:
        has_tht = "through_hole" in attr
        has_smd = "smd" in attr
    return has_tht, has_smd


# --------------------------------------------------------------------------- #
# footprint-file resolution (reuses generation's exact path)
# --------------------------------------------------------------------------- #

def _resolver():
    """Build (lib_dirs, lib_roots, find) matching pcb_gen so a system-library
    footprint resolves identically to how the board was generated. Returns
    (find_fn, lib_dirs, lib_roots) or (None, {}, []) if pcb_gen is broken."""
    try:
        from ..layout import pcb_gen
        from ..settings import envil_home, fp_lib_dir
        cfg = pcb_gen._load_cfg()
        kiprjmod = str(cfg.get("kiprjmod_base") or str(envil_home()))
        lib_root = Path(cfg.get("lib_root") or str(fp_lib_dir()))
        lib_roots = [lib_root]
        for r in pcb_gen._install_fp_roots():
            if r not in lib_roots:
                lib_roots.append(r)
        return pcb_gen, cfg, kiprjmod, lib_roots
    except Exception:
        return None, {}, "", []


# --------------------------------------------------------------------------- #
# dynamic classification — derived per-part from refdes + the symbol's own
# pins, so it works on ANY circuit without a per-circuit list.
# --------------------------------------------------------------------------- #

def _alpha_prefix(ref: str) -> str:
    """Leading alphabetic run of a reference designator (J1 -> J, MH2 -> MH)."""
    i = 0
    while i < len(ref) and ref[i].isalpha():
        i += 1
    return ref[:i].upper()


def _mech_category(ref: str, lib_id: str, cfg: Dict[str, Any]) -> str:
    """Rule 6, dynamic: classify a part as connector/mechanical from its
    REFDES prefix first (Appendix A triage), then a generic lib_id fallback.
    Returns the category ('connector'/'switch'/...) or '' if not mechanical."""
    ref_map = (cfg.get("mechanical_refdes_prefixes", {}) or {}).get("map", {}) or {}
    cat = ref_map.get(_alpha_prefix(ref))
    if cat:
        return cat
    lid = lib_id.lower()
    for sub in (cfg.get("review_classes", {}) or {}).get("lib_id_substrings", []):
        if str(sub).lower() in lid:
            return "mechanical"
    return ""


def _is_polarized(geom: Any, lib_id: str, cfg: Dict[str, Any]) -> bool:
    """Rule 7, dynamic: decide a part is polarity-sensitive / keyed from the
    SYMBOL's own pins — a polarity-marker pin name, a pin count that makes
    pin-1 orientation matter, or asymmetric electrical types. Falls back to a
    generic part-class hint only for polarized parts with unnamed pins."""
    sig = cfg.get("polarity_signals", {}) or {}
    tokens = {str(t).upper() for t in sig.get("pin_name_tokens", [])}
    min_pins = int(sig.get("min_pins", 3))
    pins = list(getattr(geom, "pins", []) or []) if geom is not None else []
    if pins:
        for p in pins:
            nm = str(getattr(p, "name", "") or "").upper().strip()
            if nm in tokens or nm.startswith("+") or nm.startswith("-"):
                return True
        numbered = [p for p in pins
                    if getattr(p, "number", "") not in ("", "?", "~")]
        if len(numbered) >= min_pins:
            return True
        if sig.get("etype_asymmetry", True):
            etypes = {str(getattr(p, "etype", "")) for p in pins
                      if getattr(p, "etype", "")}
            if len(etypes - {"passive", "unspecified"}) > 0 and len(etypes) > 1:
                return True
    lid = lib_id.lower()
    for h in sig.get("lib_class_hints", []):
        if str(h).lower() in lid:
            return True
    return False


# --------------------------------------------------------------------------- #
# tool
# --------------------------------------------------------------------------- #

@tool(
    name="footprint_audit",
    description=(
        "Validate every placed part's FOOTPRINT assignment on a rendered "
        "schematic, against the real .kicad_mod on disk. Read-only — reports, "
        "never edits. Checks: explicit-vs-guessed package (rule 1), symbol-"
        "pin ↔ footprint-pad match (rule 2), footprint resolves on disk, "
        "THT/SMD vs assembly process (rule 5), connectors/mechanical flagged "
        "for human review (rule 6), polarized parts have pad 1 (rule 7), "
        "courtyard + 3D-model presence (rule 8). Run it after a build / after "
        "assigning footprints, before generating or updating the PCB.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_sch"}   # or a sibling .kicad_pcb / project dir\n'
        "Returns a compact card + structured findings. Verdict is in words. "
        "All policy in config/footprint_audit.json. To FIX an empty/guessed "
        "footprint, apply the suggested id via apply_ops (set the Footprint "
        "field) then re-run — keep assign and verify separate."
    ),
    input_schema={"path": str},
)
async def footprint_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "footprint_audit disabled in config"}],
                "is_error": True}

    sch = _resolve_sch_path(str(args.get("path", "")).strip())
    if sch is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: no .kicad_sch found for "
                                     f"{args.get('path', '')!r}"}],
                "is_error": True}

    skip_prefixes = list((cfg.get("skip_ref_prefixes", {}) or {})
                         .get("prefixes", ["#PWR", "#FLG", "#"]))
    try:
        comps = _iter_components(sch, skip_prefixes)
    except Exception as exc:                                # noqa: BLE001
        return {"content": [{"type": "text",
                             "text": f"ERROR: could not parse {sch.name}: "
                                     f"{type(exc).__name__}: {exc}"}],
                "is_error": True}

    ir_fp = _ir_footprints(sch)
    pcb_gen, gen_cfg, kiprjmod, lib_roots = _resolver()
    lib_dirs: Dict[str, Path] = {}
    if pcb_gen is not None:
        try:
            lib_dirs = pcb_gen._fp_lib_dirs(gen_cfg, kiprjmod, sch_dir=str(sch.parent))
        except Exception:                                   # noqa: BLE001
            lib_dirs = {}

    from ..kicad.symbol_geom import load_symbol
    try:
        from ..intent.engine import _resolve_default_footprint_traced
    except Exception:                                       # noqa: BLE001
        _resolve_default_footprint_traced = None            # type: ignore

    trusted = set((cfg.get("provenance", {}) or {}).get(
        "trusted_sources", ["explicit_ir", "by_lib_id", "alias_lib_id"]))
    guess = set((cfg.get("provenance", {}) or {}).get(
        "guess_sources", ["by_prefix", "symbol_default"]))
    process = str((cfg.get("assembly_process", {}) or {}).get("default", "mixed"))
    cap = int(cfg.get("max_examples_per_rule", 12))

    findings: List[Dict[str, str]] = []      # {ref, rule, severity, msg, [suggest]}

    def add(ref: str, rule: str, msg: str, suggest: str = "") -> None:
        if not _rule_on(cfg, rule):
            return
        f = {"ref": ref, "rule": rule, "severity": _rule_sev(cfg, rule),
             "msg": msg}
        if suggest:
            f["suggest"] = suggest
        findings.append(f)

    part_rows: List[Dict[str, Any]] = []

    for c in comps:
        ref, lib_id, inst_fp = c["ref"], c["lib_id"], c["footprint"].strip()

        # --- symbol geometry (for pin/pad match + resolver fallback) ---
        geom = None
        try:
            geom = load_symbol(lib_id)
        except Exception:                                   # noqa: BLE001
            geom = None

        # --- provenance ---
        prov = ""
        if not inst_fp:
            prov = "empty"
        elif ir_fp.get(ref):
            prov = "explicit_ir" if ir_fp[ref] == inst_fp else "manual"
        elif _resolve_default_footprint_traced is not None:
            tfp, tsrc = _resolve_default_footprint_traced(lib_id, geom)
            prov = tsrc if (tfp and tfp == inst_fp) else "manual"
        else:
            prov = "manual"

        # A guessed footprint we still want to suggest a better explicit id
        # for: what the resolver produces IS the suggestion (it's what shipped).
        row: Dict[str, Any] = {"ref": ref, "lib_id": lib_id,
                               "footprint": inst_fp, "provenance": prov,
                               "resolved": False}

        # --- Rule 1: explicit package ---
        if prov in guess:
            add(ref, "explicit_package",
                f"footprint chosen by {prov} (a guess), not an explicit "
                f"package/MPN: {inst_fp or '(none)'}")

        # --- Rule 6: connector / mechanical human review (dynamic: refdes) ---
        # Category derived per-part from the reference-designator prefix, so
        # it works on any circuit. Fires even when the footprint is empty or
        # unresolved — a connector always needs a datasheet check.
        mech_cat = _mech_category(ref, lib_id, cfg)
        if mech_cat:
            add(ref, "connector_review",
                f"{lib_id} [{mech_cat}] — verify pitch, mounting & courtyard "
                f"against the datasheet")

        # --- footprint_resolves (also covers empty) ---
        if not inst_fp:
            add(ref, "footprint_resolves", "no footprint assigned")
            part_rows.append(row)
            continue
        mod_path = None
        if pcb_gen is not None:
            try:
                mod_path = pcb_gen._find_kicad_mod(inst_fp, lib_dirs, lib_roots)
            except Exception:                               # noqa: BLE001
                mod_path = None
        if mod_path is None:
            add(ref, "footprint_resolves",
                f"footprint '{inst_fp}' not found on disk")
            part_rows.append(row)
            continue
        row["resolved"] = True

        node = pcb_gen._parse_kicad_mod(mod_path)
        if node is None:
            add(ref, "footprint_resolves",
                f"footprint '{mod_path.name}' failed to parse")
            part_rows.append(row)
            continue
        meta = _mod_meta(node)
        pads = meta["pads"]
        row["pads"] = len(pads)

        # --- Rule 2: pin/pad number match ---
        if geom is None:
            add(ref, "pin_pad_match",
                f"symbol '{lib_id}' did not load — cannot check pin/pad match")
        else:
            sym_pins = {p.number for p in geom.pins
                        if p.number and p.number not in ("?", "~")}
            pad_nums = {n for n in pads if n and n != '""'}
            only_pins = sorted(sym_pins - pad_nums)
            only_pads = sorted(pad_nums - sym_pins)
            if only_pins or only_pads:
                bits = []
                if only_pins:
                    bits.append(f"symbol pins with no pad: {only_pins}")
                if only_pads:
                    bits.append(f"pads with no symbol pin: {only_pads}")
                add(ref, "pin_pad_match",
                    f"{len(sym_pins)} pins vs {len(pad_nums)} pads — "
                    + "; ".join(bits))

        # --- Rule 5: tech type vs assembly process ---
        has_tht, has_smd = _pad_tech(pads, meta["attr"])
        if process == "smt" and has_tht:
            add(ref, "tech_type",
                f"through-hole footprint '{inst_fp}' on an SMT-only board")
        elif process == "tht" and has_smd:
            add(ref, "tech_type",
                f"SMD footprint '{inst_fp}' on a THT-only board")

        # --- Rule 7: polarized parts need pad '1' (dynamic: from symbol pins) ---
        if _is_polarized(geom, lib_id, cfg) and "1" not in pads:
            add(ref, "polarity_pin1",
                f"polarized/keyed part but footprint '{inst_fp}' has no pad '1'")

        # --- Rule 8: courtyard + 3D model ---
        if not meta["has_courtyard"]:
            add(ref, "courtyard",
                f"footprint '{inst_fp}' has no courtyard geometry (F/B.CrtYd)")
        if not meta["has_model"]:
            add(ref, "model_3d",
                f"footprint '{inst_fp}' has no 3D model reference")

        part_rows.append(row)

    # --- aggregate ---
    n = len(comps)
    counts = {"error": 0, "warning": 0, "review": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w, r, i = counts["error"], counts["warning"], counts["review"], counts["info"]

    vt = cfg.get("verdict", {}) or {}
    if n == 0:
        # Never claim "VERIFIED" on an empty schematic — that's a false green
        # (feedback_honest_verify_loop / feedback_no_inspection_theater).
        verdict = vt.get("no_parts",
                         "NO PARTS — schematic has no components to audit")
        ok = True
    elif e == 0 and w == 0 and r == 0:
        verdict = vt.get("clean", "FOOTPRINTS VERIFIED — {n} parts").format(n=n)
        ok = True
    elif e == 0 and w == 0 and r > 0:
        verdict = vt.get("review_only",
                         "FOOTPRINTS OK — {n} parts, {r} need review"
                         ).format(n=n, r=r)
        ok = True
    else:
        verdict = vt.get("issues",
                         "FOOTPRINTS NOT CLEAN — {e} error(s), {w} warning(s), "
                         "{r} review across {n} parts"
                         ).format(e=e, w=w, r=r, n=n)
        ok = (e == 0)

    # --- card ---
    icon = {"error": "✗", "warning": "!", "review": "?", "info": "·"}
    lines: List[str] = [f"# Footprint audit — {sch.name}",
                        f"  {n} parts · {e} error · {w} warning · "
                        f"{r} review · {i} info"]
    guessed = [row["ref"] for row in part_rows if row["provenance"] in guess]
    empty = [row["ref"] for row in part_rows if row["provenance"] == "empty"]
    if empty:
        lines.append(f"  no footprint: {', '.join(empty)}")
    if guessed:
        lines.append(f"  guessed (rule 1): {', '.join(guessed)}")

    # findings grouped by severity, capped
    order = ["error", "warning", "review", "info"]
    for sev in order:
        group = [f for f in findings if f["severity"] == sev]
        if not group:
            continue
        lines.append("")
        lines.append(f"  {sev.upper()} ({len(group)})")
        for f in group[:cap]:
            lines.append(f"    {icon[sev]} {f['ref']}: {f['msg']}")
        if len(group) > cap:
            lines.append(f"    … +{len(group) - cap} more")

    lines.append("")
    lines.append(f"**{verdict}**")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": ok,
        "schematic": str(sch).replace("\\", "/"),
        "parts_audited": n,
        "counts": counts,
        "verdict": verdict,
        "findings": findings,
        "parts": part_rows,
    }
