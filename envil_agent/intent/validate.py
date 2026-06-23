"""IR validation — pre-render structural check.

Stage 3 of the prompt-to-schematic pipeline. Catches architect mistakes
(missing decoupling, undefined pins, ref collisions, undriven power
rails) BEFORE the engine starts emitting s-expressions, so the failure
message is small and actionable instead of "schematic looks weird".

Each issue is a small dict with `code`, `severity`, `where`, `text` —
serializable so it can be sent back to the architect as a structured
revision request.

Block-level checks consult three JSON configs (no hardcoded thresholds
or block-name lists):
  - config/block_rules.json        -- anchor / size / gating
  - config/block_naming.json       -- registry of accepted block names
  - config/sheet_planner_rules.json:block_justifications
                                   -- per-block keyword + lib_id justification
"""
from __future__ import annotations

import fnmatch
import json
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

import difflib
from pathlib import Path

from ..kicad.symbol_geom import _sym_roots, load_symbol
from .ir import TopologyIR


_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=1)
def _load_block_rules() -> dict:
    try:
        return json.loads((_CONFIG_DIR / "block_rules.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _load_block_naming() -> dict:
    try:
        return json.loads((_CONFIG_DIR / "block_naming.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _load_block_justifications() -> dict:
    try:
        cfg = json.loads((_CONFIG_DIR / "sheet_planner_rules.json").read_text(encoding="utf-8"))
        return cfg.get("block_justifications") or {}
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _load_design_checklist() -> dict:
    try:
        return json.loads((_CONFIG_DIR / "design_checklist.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _comp_matches_any_lib_pattern(comp, patterns) -> bool:
    if not patterns:
        return False
    lib = (comp.lib_id or "")
    lib_l = lib.lower()
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatchcase(lib, pat) or fnmatch.fnmatchcase(lib_l, pat.lower()):
            return True
    return False


def _ir_nets_for_pin(ir: TopologyIR, ref: str, pin_token: str):
    """Return nets that include the given pin reference EITHER by pin
    name OR by pin number. The architect can emit `J1.CC1` (pin name)
    or `J1.A5` (pin number) interchangeably; the symbol's pin table
    bridges the two forms so the checklist works regardless of which
    style was used."""
    comp = ir.component_by_ref(ref)
    candidates = {f"{ref}.{pin_token}"}
    if comp is not None:
        try:
            geom = load_symbol(comp.lib_id)
        except Exception:
            geom = None
        if geom is not None:
            token_l = (pin_token or "").lower()
            for pin in geom.pins or []:
                pname = (pin.name or "")
                pnum = (pin.number or "")
                if pname.lower() == token_l or pnum.lower() == token_l:
                    candidates.add(f"{ref}.{pname}")
                    candidates.add(f"{ref}.{pnum}")
    for net in ir.nets:
        if any(p in candidates for p in net.pins):
            yield net


def _pin_value_to_uf(value: str) -> float:
    """Parse capacitor values like '100n', '4u7', '22u', '10uF', '0.1u'
    into microfarads. Returns 0.0 when unparseable so the caller can
    treat as 'unknown'."""
    if not value:
        return 0.0
    v = value.strip().lower().replace("uf", "u").replace("ufarad", "u")
    v = v.replace(" ", "")
    try:
        # IEC RKM with letter in the middle: 4u7 -> 4.7
        for unit, mul in (("u", 1.0), ("n", 1e-3), ("p", 1e-6), ("m", 1e3), ("f", 1e-9)):
            if unit in v:
                a, _, b = v.partition(unit)
                if not a and not b:
                    return 0.0
                num = float((a or "0") + "." + (b or "0"))
                return num * mul
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _refdes_prefix_letters(ref: str) -> str:
    out = []
    for ch in ref:
        if ch.isalpha():
            out.append(ch.upper())
        else:
            break
    return "".join(out)


def _issue(code: str, severity: str, where: str, text: str) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "where": where, "text": text}


def _feedback_available_pins_on() -> bool:
    """Gate: layout_config.json -> build_graph.feedback_available_pins
    (default true). When ON, a PIN_NOT_ON_SYMBOL issue also carries the
    FULL real pin list (not the truncated prose) as structured data, and
    the repair prompts render it back so the architect stops re-emitting a
    pin the symbol does not have. When OFF, the issue dict is byte-identical
    to the legacy behaviour (no extra keys, helper renders nothing)."""
    try:
        from .engine import _load_layout_config
        return bool((_load_layout_config().get("build_graph") or {}).get(
            "feedback_available_pins", True))
    except Exception:
        return True


def _cross_block_guard_cfg() -> Dict[str, Any]:
    """Phase 0.2 config block (layout_config.json:cross_block_guard). Empty dict
    on any failure -> every consumer falls back to its OFF default, so validate
    output stays byte-identical."""
    try:
        from .engine import _load_layout_config
        return _load_layout_config().get("cross_block_guard", {}) or {}
    except Exception:
        return {}


def _check_cross_block_collisions(ir) -> List[Dict[str, Any]]:
    """Cross-block net-name collision guard, validate side (Phase 0.2).

    Two sources, one issue code (CROSS_BLOCK_NET_COLLISION), warning-only so
    error_count never moves:
      (1) Layer-B renames — incidental same-name collisions the splice guard
          already auto-renamed to prevent a silent short; surfaced so the user
          sees what was fixed (read from the `_cross_block_collisions` stamp,
          mirroring the `_incremental_skipped` pattern above).
      (2) Layer-A heuristic — OPT-IN (validate_collision_check default false):
          a non-power net whose pins span >= 2 blocks AND whose name is a bare
          BUS-INDEX token (D5/A3/IO7/...). A merged IR has no per-block origin
          (NET_DUPLICATE already forbids two same-named nets), so detection is
          name-shape based. The pattern list is deliberately narrow — functional
          names (ALERT/RST/EN/...) are OFTEN legitimate single shared signals on
          a real board, so they are NOT flagged; descriptive names and protocol
          buses never match. Off by default because even bus-index names can be
          legitimate; turn on as a diagnostic only.
    Returns [] for a flat (block-less) IR and when both sources are inactive, so
    validate is byte-identical for non-hierarchical circuits."""
    out: List[Dict[str, Any]] = []
    cfg = _cross_block_guard_cfg()
    sev = str(cfg.get("collision_severity", "warning"))

    # (1) Layer-B auto-renames (attribute present only when the splice guard,
    # gated OFF by default, actually renamed something).
    for rec in (getattr(ir, "_cross_block_collisions", None) or []):
        if not isinstance(rec, dict):
            continue
        _from, _to, _blk = rec.get("from", "?"), rec.get("to", "?"), rec.get("block", "?")
        out.append(_issue(
            "CROSS_BLOCK_NET_COLLISION", sev, str(_blk),
            f"incidental net-name collision auto-renamed {_from!r} -> {_to!r} in "
            f"block {_blk!r} to stop two different signals shorting via same-named "
            f"global labels. Give the signal a unique descriptive name."))

    # (2) Layer-A heuristic lint on the merged IR (gated; no-op for flat IRs).
    if cfg.get("validate_collision_check", False):
        blocks = getattr(ir, "blocks", None) or []
        if len(blocks) >= 2:
            import re
            pats = []
            for p in (cfg.get("weak_signal_name_patterns") or []):
                try:
                    pats.append(re.compile(p))
                except re.error:
                    continue
            if pats:
                ref_to_block: Dict[str, str] = {}
                for b in blocks:
                    for r in (getattr(b, "component_refs", None) or []):
                        ref_to_block[r] = b.name
                for n in ir.nets:
                    if bool(getattr(n, "is_power", False)):
                        continue
                    name = getattr(n, "name", "") or ""
                    if not any(p.search(name) for p in pats):
                        continue
                    blks = set()
                    for pinref in (getattr(n, "pins", None) or []):
                        if "." not in str(pinref):
                            continue
                        b = ref_to_block.get(str(pinref).split(".", 1)[0])
                        if b:
                            blks.add(b)
                    if len(blks) >= 2:
                        _sb = sorted(blks)
                        out.append(_issue(
                            "CROSS_BLOCK_NET_COLLISION", sev, name,
                            f"net {name!r} spans {len(blks)} blocks ({', '.join(_sb)}) "
                            f"with a generic name. If these are two DIFFERENT signals "
                            f"they are silently shorted via same-named global labels "
                            f"— give each a unique descriptive name (e.g. {_sb[0]}_{name})."))
    return out


def _load_intake_rules() -> dict:
    """intake_rules.json (cached by triage). {} on any failure -> conformance
    no-ops, byte-stable."""
    try:
        from .triage import _load_rules
        return _load_rules() or {}
    except Exception:
        return {}


def _check_requirement_conformance(ir, prompt: str) -> List[Dict[str, Any]]:
    """Phase 3 drift/conformance: every part-number the USER named in `prompt`
    must appear in the generated circuit -- catch the architect silently
    dropping or substituting a requested part. GENERAL + part-agnostic +
    config-driven (intake_rules.json:requirement_conformance). Warning-only.
    No-op on an empty prompt, so the golden harness (validate_ir without a
    prompt) stays byte-stable."""
    if not prompt:
        return []
    cfg = _load_intake_rules().get("requirement_conformance") or {}
    if not cfg.get("enabled", False):
        return []
    pat = cfg.get("token_pattern") or r"[A-Za-z]{2,}[0-9]{2,}[A-Za-z0-9-]*"
    try:
        toks = re.findall(pat, prompt)
    except re.error:
        return []
    exclude = {str(t).upper() for t in (cfg.get("token_exclude") or [])}
    minlen = int(cfg.get("min_token_len", 4))
    sev = str(cfg.get("severity", "warning"))
    haystack = " ".join(f"{c.lib_id} {c.value}" for c in ir.components).lower()
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for t in toks:
        tu = t.upper()
        if tu in exclude or len(tu) < minlen or tu in seen:
            continue
        seen.add(tu)
        if t.lower() not in haystack:
            out.append(_issue(
                "REQUIREMENT_PART_MISSING", sev, t,
                f"the prompt named part {t!r} but no component in the generated "
                "circuit matches it -- the architect may have dropped or "
                f"substituted it. Add {t}, or confirm the substitution was intended."))
    return out


_PIN_SECTION_TITLE = ("AVAILABLE PINS for the parts you mis-pinned "
                      "(use ONLY these real <ref>.<pin> names from the library):")


def render_available_pins_section(errors: List[Dict[str, Any]]) -> str:
    """Render an English block listing the REAL pins of every part that
    raised PIN_NOT_ON_SYMBOL, from the structured `available_pins` that
    validate_ir attached. Single source of this prompt string for EVERY
    repair path (per-block and full-architect). Returns '' when the gate is
    off or no error carries pin data, so callers stay byte-identical when
    the feature is gated off. Pins come live from the loaded symbol — no
    curated list, works for any part."""
    if not _feedback_available_pins_on():
        return ""
    by_ref: Dict[tuple, Dict[str, Any]] = {}
    for e in errors or []:
        if e.get("code") != "PIN_NOT_ON_SYMBOL" or not e.get("available_pins"):
            continue
        ref = str(e.get("where", "")).split(".", 1)[0]
        lib = e.get("lib_id", "")
        slot = by_ref.setdefault((ref, lib), {"pins": e["available_pins"], "hints": []})
        for h in (e.get("did_you_mean") or []):
            if h not in slot["hints"]:
                slot["hints"].append(h)
    if not by_ref:
        return ""
    lines = [_PIN_SECTION_TITLE]
    for (ref, lib), slot in by_ref.items():
        lines.append(f"  {ref} ({lib}):" if lib else f"  {ref}:")
        if slot["hints"]:
            lines.append(f"    closest matches: {', '.join(slot['hints'])}")
        lines.append(f"    all pins: {', '.join(slot['pins'])}")
    return "\n".join(lines)


def _pin_name_tokens(pin_name: str) -> set:
    """Tokenise a KiCad pin name for pattern matching. KiCad commonly names a
    reset / strap pin as a COMPOUND of its functions with overbar syntax
    (`~{RESET}/PC6`, `~{MCLR}/VPP`), so an exact-equality match misses it on
    most MCUs (only ST's bare `NRST` matches). Split on `/`, strip the overbar
    braces, return the lowercase function tokens."""
    s = (pin_name or "").lower().replace("~{", " ").replace("{", " ").replace("}", " ")
    for sep in ("/", "\\", ",", " "):
        s = s.replace(sep, "\x00")
    return {t for t in s.split("\x00") if t}


def _pin_name_matches(pin_name: str, patterns) -> bool:
    """True when any configured pin-name pattern matches a TOKEN of the
    (possibly compound, overbar-wrapped) pin name -- so `~{RESET}/PC6` matches
    the `RESET` pattern on an AVR exactly as `NRST` matches on an STM32. Keeps
    the rule part-agnostic across MCU families with one matcher."""
    toks = _pin_name_tokens(pin_name)
    for pat in patterns or []:
        p = (pat or "").lower().replace("~{", "").replace("{", "").replace("}", "")
        if p and p in toks:
            return True
    return False


def _suggest_lib_id(lib_id: str) -> str:
    """When a lib_id misses, scan the same library folder for close-named
    parts and return a 'Did you mean: ...' hint. Helps the architect's
    retry pick the right name on the second attempt."""
    if ":" not in lib_id:
        return ""
    libnick, part = lib_id.split(":", 1)
    candidates = []
    for root in _sym_roots():
        symdir = root / f"{libnick}.kicad_symdir"
        if symdir.exists() and symdir.is_dir():
            for f in symdir.glob("*.kicad_sym"):
                candidates.append(f.stem)
    if not candidates:
        return ""
    close = difflib.get_close_matches(part, candidates, n=5, cutoff=0.5)
    if not close:
        # No fuzzy match — list a few alphabetically close ones
        close = sorted(candidates)[:5]
    return " Did you mean: " + ", ".join(f"{libnick}:{c}" for c in close)


def _block_anchor_pin_count(comp_ref: str, ir: TopologyIR) -> int:
    """Return the geometric pin count of a component ref (or 0 when the
    lib_id can't be loaded). Used by the anchor-IC check."""
    comp = ir.component_by_ref(comp_ref)
    if comp is None:
        return 0
    try:
        geom = load_symbol(comp.lib_id)
    except Exception:
        return 0
    return len(geom.pins or [])


def _block_has_anchor(block, ir: TopologyIR, rules: dict) -> bool:
    """True when at least one component in block.component_refs has a
    refdes prefix in anchor_refdes_prefixes AND a pin count >=
    minimum_anchor_pin_count. Pure config consultation -- both the
    refdes list and the threshold live in JSON."""
    anchor_prefixes = tuple(rules.get("anchor_refdes_prefixes") or ())
    min_pins = int(rules.get("minimum_anchor_pin_count", 3))
    for ref in block.component_refs:
        # Extract the alphabetical refdes prefix (R12 -> R, U1A -> U).
        prefix = "".join(c for c in ref if c.isalpha()).upper()
        # Walk the longest possible prefix first so "FB1" matches "FB"
        # before "F". We try the full alpha prefix, then the leading
        # 2 letters, then the leading 1 letter.
        candidate_prefixes = []
        if prefix:
            candidate_prefixes.append(prefix)
            if len(prefix) > 1:
                candidate_prefixes.append(prefix[:2])
            candidate_prefixes.append(prefix[:1])
        if not any(cp in anchor_prefixes for cp in candidate_prefixes):
            continue
        if _block_anchor_pin_count(ref, ir) >= min_pins:
            return True
    return False


def _block_is_justified(block_name: str, prompt: str, ir: TopologyIR,
                         justifications: dict) -> bool:
    """True when block_name is either (a) unlisted in the
    justifications map (= structural staple like POWER / MCU /
    PROTECTION), or (b) satisfies at least one keyword match against
    the user prompt OR at least one lib_id pattern match against the
    IR's components[]. Pure config -- keywords and patterns are all
    in sheet_planner_rules.json:block_justifications."""
    rule = justifications.get(block_name)
    if rule is None:
        return True
    lower_prompt = (prompt or "").lower()
    for kw in rule.get("requires_keywords") or []:
        if kw and kw.lower() in lower_prompt:
            return True
    patterns = rule.get("requires_lib_id_patterns") or []
    if patterns:
        for comp in ir.components:
            for pat in patterns:
                if fnmatch.fnmatchcase(comp.lib_id, pat) \
                        or fnmatch.fnmatchcase(comp.lib_id.lower(), pat.lower()):
                    return True
    return False


def validate_ir(ir: TopologyIR, prompt: str = "") -> List[Dict[str, Any]]:
    """Return a list of issues. Empty list = IR is OK to render.

    `prompt` is the user's original natural-language request, threaded
    through so the block-justification check can verify that hallucinated
    blocks (e.g. RF when the user never asked for wireless) get caught
    before render. Optional with safe default to keep older callers
    working byte-identically."""
    issues: List[Dict[str, Any]] = []

    # 0. incremental draw/splice failures (set by build_incrementally when
    # gated on). An unwired block must NOT ship as a green success — emit one
    # error per failed block, keyed by block name so it localises to that block
    # and the per-block repair loop redraws it. No attribute => no issue, so
    # validate stays byte-identical for non-incremental / gate-off builds.
    for _bn in (getattr(ir, "_incremental_skipped", None) or []):
        issues.append(_issue(
            "INCREMENTAL_BLOCK_SKIPPED", "error", str(_bn),
            f"block {_bn!r} failed to draw during incremental build and was left "
            f"unwired (its components have only boundary stubs). Redraw this block."))

    # 1. ref uniqueness
    refs = [c.ref for c in ir.components]
    seen = set()
    for r in refs:
        if r in seen:
            issues.append(_issue("REF_DUPLICATE", "error", r,
                                  f"reference {r!r} is used by more than one component"))
        seen.add(r)

    # 2. every net pin reference resolves to (a) an existing component and
    #    (b) an actual pin on its symbol
    for net in ir.nets:
        for pinref in net.pins:
            if "." not in pinref:
                issues.append(_issue("PIN_MALFORMED", "error", pinref,
                                      f"pin reference {pinref!r} missing '.' (expected <ref>.<pin>)"))
                continue
            ref, pin_key = pinref.split(".", 1)
            comp = ir.component_by_ref(ref)
            if comp is None:
                issues.append(_issue("PIN_UNKNOWN_REF", "error", pinref,
                                      f"net {net.name!r} references {ref!r}, which is not in components[]"))
                continue
            try:
                geom = load_symbol(comp.lib_id)
            except ValueError as exc:
                suggestion = _suggest_lib_id(comp.lib_id)
                issues.append(_issue("LIB_NOT_FOUND", "error", comp.lib_id,
                                      f"component {ref!r}: symbol not found.{suggestion}"))
                continue
            if geom.resolve_pin(pin_key) is None:
                avail = ", ".join(f"{p.number}({p.name})" for p in geom.pins[:8])
                iss = _issue("PIN_NOT_ON_SYMBOL", "error", pinref,
                             f"pin {pin_key!r} not found on {comp.lib_id}. "
                             f"Available: {avail}{' ...' if len(geom.pins) > 8 else ''}")
                # Additive (gated): carry the FULL real pin set + a symbol-
                # derived 'did you mean' so the repair prompt can show the
                # architect the actual pin it should have used (the prose
                # 'text' above stays byte-identical for the legacy path).
                if _feedback_available_pins_on():
                    iss["available_pins"] = [f"{p.number}({p.name})" for p in geom.pins]
                    iss["lib_id"] = comp.lib_id
                    _b, _s, _cands = geom.resolve_pin_with_score(pin_key)
                    if _cands:
                        iss["did_you_mean"] = [f"{p.number}({p.name})" for _sc, p in _cands]
                issues.append(iss)

    # 3. duplicate net names
    net_names = [n.name for n in ir.nets]
    seen_nets = set()
    for n in net_names:
        if n in seen_nets:
            issues.append(_issue("NET_DUPLICATE", "error", n,
                                  f"net name {n!r} declared twice"))
        seen_nets.add(n)

    # 3a. PIN_IN_MULTIPLE_NETS — same pin appears in 2+ nets.
    # Caused the "PWMOSI" label collision we saw on ATtiny85.PB0 in
    # the dimmer build: PWM and MOSI both listed U1.PB0. KiCad puts
    # one label per net at one coordinate, so they rendered on top
    # of each other and the rendered net topology is electrically
    # wrong (two distinct nets claiming the same physical pin).
    # When a physical pin truly serves two roles (ICSP MOSI = PWM
    # at runtime on AVR; SWDIO = JTMS on STM32; etc.) the architect
    # must pick ONE net name for that pin and route every consumer
    # of either role onto it.  Pure-logic check, no per-part data,
    # works for any circuit.
    pin_to_nets: Dict[str, List[str]] = {}
    for net in ir.nets:
        for pinref in net.pins:
            pin_to_nets.setdefault(pinref, []).append(net.name)
    for pinref, nets_using in pin_to_nets.items():
        if len(nets_using) > 1:
            issues.append(_issue(
                "PIN_IN_MULTIPLE_NETS", "error", pinref,
                f"pin {pinref!r} is on multiple nets {nets_using!r}. "
                f"A pin belongs to exactly one electrical net. When a "
                f"physical pin serves two functions (e.g. PB0 acts as "
                f"MOSI during ICSP programming AND as PWM at runtime), "
                f"use ONE net name and route both consumers to it."))

    # 3b. non-standard power rail names (e.g. "+3V5", "+5V3")
    # Power rails should be canonical: list lives in
    # layout_config.json -> validate.canonical_power_rails so projects
    # using custom rails (e.g. +3V3_ANALOG, +VBAT_PROT) can extend it
    # without forking the validator. Python list here is the fallback
    # when the JSON file is unreadable.
    try:
        from .engine import _load_layout_config
        _v_cfg = _load_layout_config().get("validate", {}) or {}
        canonical_rails = set(_v_cfg.get("canonical_power_rails", []))
    except Exception:
        canonical_rails = set()
    if not canonical_rails:
        canonical_rails = {
            "+3V3", "+3.3V", "+5V", "+12V", "+9V", "+15V", "+24V",
            "+1V8", "+1.8V", "+2V5", "+2.5V",
            "-5V", "-12V", "-15V",
            "VBUS", "VBAT", "VCC", "VDD", "VEE", "VSS",
            "GND", "AGND", "DGND", "PGND",
        }
    for net in ir.nets:
        if not net.is_power:
            continue
        if net.name.upper() not in {r.upper() for r in canonical_rails}:
            issues.append(_issue("POWER_RAIL_NONSTANDARD", "warning", net.name,
                                  f"power rail {net.name!r} is not a canonical "
                                  f"rail name. Use one of: +3V3, +5V, +12V, "
                                  f"+9V, VBUS, VBAT, GND, etc."))

    # 3c. NET_FLOATING --- every non-power net needs >=2 pins.
    # A net with one pin is electrically meaningless (nothing to connect
    # to). Power nets are exempt because the engine synthesises a
    # PWR_FLAG when there's no driver, so a one-pin power net can still
    # render correctly. Reuses lint.selectors.detect_floating_nets so
    # the IR-stage and post-render lint stages share the same rule.
    try:
        from ..lint.selectors import detect_floating_nets
        non_power_nets = [n for n in ir.nets if not n.is_power]
        for lint_issue in detect_floating_nets(non_power_nets, min_pins=2):
            issues.append(_issue(
                "NET_FLOATING", "error",
                lint_issue["where"].get("net", ""),
                lint_issue["message"],
            ))
    except Exception:
        # Lint engine optional --- a missing import should never block
        # the legacy validation path.
        pass

    # 3d. BOARD_UNDER_WIRED --- catastrophic under-connection guard.
    # A board can fail EVERY net-relative check above and still be almost
    # entirely UNWIRED: when the architect returns a big parts list with
    # `nets: null` (observed on ~100-part boards — see the BMS build trace,
    # 98 components / 0 architect nets), there are no nets for the PIN /
    # NET_FLOATING checks to flag, so a non-circuit nearly PASSES with only a
    # handful of part-presence errors. This guard measures the ACTUAL wiring
    # coverage — the fraction of component pins that appear in any net — and
    # rejects loudly when it collapses on a board large enough for the ratio
    # to be meaningful. Thresholds are config-driven
    # (layout_config.json -> validate.connectivity_guard); the inline defaults
    # are deliberately conservative so a normal sparse board (unused GPIO, NC
    # pins, big connectors) never trips it — only a board that was never wired.
    try:
        from .engine import _load_layout_config
        _cg = (_load_layout_config().get("validate", {}) or {}).get(
            "connectivity_guard", {}) or {}
    except Exception:
        _cg = {}
    if _cg.get("enabled", True):
        min_components = int(_cg.get("min_components", 8))
        min_pin_coverage = float(_cg.get("min_pin_coverage", 0.25))
        if len(ir.components) >= min_components:
            covered_tokens = {p for net in ir.nets for p in net.pins}
            total_pins = 0
            connected_pins = 0
            for comp in ir.components:
                try:
                    geom = load_symbol(comp.lib_id)
                except Exception:
                    continue  # unloadable symbol already reported by check 2
                for pin in geom.pins or []:
                    total_pins += 1
                    if (f"{comp.ref}.{pin.number}" in covered_tokens
                            or f"{comp.ref}.{pin.name}" in covered_tokens):
                        connected_pins += 1
            coverage = (connected_pins / total_pins) if total_pins else 1.0
            if total_pins and coverage < min_pin_coverage:
                issues.append(_issue(
                    "BOARD_UNDER_WIRED", "error", ir.name or "board",
                    f"board has {len(ir.components)} components and "
                    f"{len(ir.nets)} net(s), but only {connected_pins} of "
                    f"{total_pins} pins ({coverage * 100:.0f}%) are wired to any "
                    f"net - the architect returned a parts list without wiring "
                    f"it. Decompose the board into functional blocks (power, "
                    f"MCU, comms, sensing, protection, ...) and emit the nets "
                    f"that connect each block's pins, including the inter-block "
                    f"signals. Every IC signal pin in use must appear in a net."))

    # 4. every IC's power/ground pins must appear in some net
    for comp in ir.components:
        try:
            geom = load_symbol(comp.lib_id)
        except ValueError:
            continue  # already reported above
        for pin in geom.pins:
            tag = pin.name.upper()
            if pin.etype not in ("power_in", "power_out"):
                continue
            pinref = f"{comp.ref}.{pin.number}"
            covered = any(pinref in net.pins or f"{comp.ref}.{pin.name}" in net.pins
                          for net in ir.nets)
            if not covered:
                issues.append(_issue("POWER_PIN_FLOATING", "error", pinref,
                                      f"power pin {pin.name}({pin.number}) of {comp.ref} "
                                      f"({comp.lib_id}) is not in any net"))

    # 4b. PIN_ROLE_MISMATCH --- datasheet pin-role guard.
    # The engine resolves a net's pin refs by EXACT name/number, so when the
    # architect hallucinates a part's pinout (no pin catalog for that lib_id)
    # the wrong pins render faithfully and ERC stays SILENT (a GPIO is still
    # "connected"). Two unambiguous violations are rejected here so the
    # architect-retry loop regenerates (now with the pin catalog in hand):
    #   (i)  a ground-named power pin on a +V supply rail, or a positive-
    #        supply pin on GND  --> VCC/GND swap.
    #   (ii) a net NAMED for a dedicated function (XTAL1/XTAL2/RESET) landing
    #        on a pin OTHER than the part's dedicated pin for it.
    # Conservative: only clearly-named power pins and explicitly function-
    # named nets are checked, so V-/VEE/VREF and arbitrary net names never
    # false-trip. Power-port / PWR_FLAG symbols are skipped. Config + token
    # lists live in layout_config.json -> validate.pin_role_check.
    try:
        from .engine import _load_layout_config
        _prc = (_load_layout_config().get("validate", {}) or {}).get(
            "pin_role_check", {}) or {}
    except Exception:
        _prc = {}
    if _prc.get("enabled", True):
        gnd_toks = tuple(t.upper() for t in (_prc.get("ground_tokens") or
            ["GND", "VSS", "AGND", "AVSS", "DGND", "VSSA", "EARTH"]))
        sup_toks = tuple(t.upper() for t in (_prc.get("supply_tokens") or
            ["VCC", "VDD", "AVCC", "VDDA", "VDDIO", "VBAT", "VBUS", "VIN", "V+"]))
        fn_pins = _prc.get("function_pins") or {
            "XTAL1": ["XTAL1", "OSC_IN", "OSCIN"],
            "XTAL2": ["XTAL2", "OSC_OUT", "OSCOUT"],
            "RESET": ["RESET", "NRST"],
        }

        def _name_is_gnd(nm: str) -> bool:
            u = (nm or "").upper().lstrip("+").strip()
            return (u in gnd_toks or u.startswith("GND")
                    or u.startswith("VSS") or u.startswith("AGND"))

        for comp in ir.components:
            if str(comp.lib_id or "").lower().startswith("power:"):
                continue   # power ports / PWR_FLAG carry no real role
            try:
                geom = load_symbol(comp.lib_id)
            except Exception:
                continue
            pins = list(geom.pins or [])

            # (i) VCC <-> GND swap on clearly-named power pins.
            for pin in pins:
                if pin.etype not in ("power_in", "power_out"):
                    continue
                nm = (pin.name or "").upper()
                pin_is_gnd = _name_is_gnd(nm)
                pin_is_pos = (nm in sup_toks or nm.startswith("VCC")
                              or nm.startswith("VDD") or nm.startswith("AVCC")
                              or nm == "V+")
                if not (pin_is_gnd or pin_is_pos):
                    continue   # ambiguous (VI/VO/V-/VREF...) -- never flag
                t_num = f"{comp.ref}.{pin.number}"
                t_name = f"{comp.ref}.{pin.name}"
                for net in ir.nets:
                    if t_num not in net.pins and t_name not in net.pins:
                        continue
                    net_is_gnd = _name_is_gnd(net.name)
                    net_is_supply = (
                        (net.is_power or net.name.strip().startswith("+"))
                        and not net_is_gnd)
                    if pin_is_gnd and net_is_supply:
                        issues.append(_issue(
                            "PIN_ROLE_MISMATCH", "error", t_num,
                            f"ground pin {pin.name}({pin.number}) of {comp.ref} "
                            f"is wired to supply rail {net.name!r} - VCC/GND "
                            f"swapped. Wire ground pins to GND and supply pins "
                            f"to the +V rail."))
                    elif pin_is_pos and net_is_gnd:
                        issues.append(_issue(
                            "PIN_ROLE_MISMATCH", "error", t_num,
                            f"supply pin {pin.name}({pin.number}) of {comp.ref} "
                            f"is wired to ground net {net.name!r} - VCC/GND "
                            f"swapped. Wire supply pins to the +V rail and "
                            f"ground pins to GND."))

            # (ii) a function-named net landing on the WRONG pin while the
            # part has a dedicated pin for that function (left unused).
            for fam, toks in fn_pins.items():
                toks_u = [t.upper() for t in toks]
                dedicated = [p for p in pins
                             if any(t in (p.name or "").upper() for t in toks_u)]
                if not dedicated:
                    continue   # part has no such pin -> nothing to bypass
                fam_u = fam.upper()
                for net in ir.nets:
                    nu = net.name.upper().lstrip("~").strip()
                    if nu != fam_u and nu not in toks_u:
                        continue
                    comp_in = [p for p in pins
                               if f"{comp.ref}.{p.number}" in net.pins
                               or f"{comp.ref}.{p.name}" in net.pins]
                    if comp_in and not any(p in dedicated for p in comp_in):
                        wrong = comp_in[0]
                        ded_str = ", ".join(
                            f"{p.name}({p.number})" for p in dedicated)
                        issues.append(_issue(
                            "PIN_ROLE_MISMATCH", "error",
                            f"{comp.ref}.{wrong.number}",
                            f"net {net.name!r} is wired to {comp.ref} pin "
                            f"{wrong.name}({wrong.number}), but {comp.ref} has a "
                            f"dedicated {fam} pin ({ded_str}). Wire {net.name!r} "
                            f"to the dedicated pin."))

    # 5. block coverage --- when blocks[] is populated, every block must
    # have at least one component AND every component must appear in
    # exactly one block. Catches the "MCU/PROG sheet is empty" pattern
    # where the architect emits a block declaration but forgets to
    # populate component_refs[] (the engine then renders an empty child
    # sheet because _subset_ir_for_block returns no components).
    # Fires as `error` severity so the architect-retry loop kicks in.
    if ir.blocks:
        ref_to_blocks: Dict[str, List[str]] = {}
        for blk in ir.blocks:
            if not blk.component_refs:
                issues.append(_issue(
                    "BLOCK_EMPTY", "error", blk.name,
                    f"block {blk.name!r} has empty component_refs[]. "
                    "Either delete the block from blocks[] or move "
                    "components into its component_refs[]. The engine "
                    "would otherwise render an empty child sheet."))
                continue
            for ref in blk.component_refs:
                ref_to_blocks.setdefault(ref, []).append(blk.name)
        all_comp_refs = {c.ref for c in ir.components}
        uncovered = sorted(all_comp_refs - set(ref_to_blocks.keys()))
        for ref in uncovered:
            issues.append(_issue(
                "COMPONENT_NOT_IN_ANY_BLOCK", "error", ref,
                f"component {ref!r} is not assigned to any block. "
                "When blocks[] is non-empty, every component in "
                "components[] must appear in exactly one block's "
                "component_refs[]."))
        for ref, blocks_using in ref_to_blocks.items():
            if len(blocks_using) > 1:
                issues.append(_issue(
                    "COMPONENT_IN_MULTIPLE_BLOCKS", "error", ref,
                    f"component {ref!r} appears in multiple blocks "
                    f"{blocks_using!r}. A component belongs to exactly "
                    "one block. Move it to the most-appropriate block "
                    "and remove from the others."))
        # Also catch refs listed in blocks but missing from components[].
        unknown_refs = sorted(set(ref_to_blocks.keys()) - all_comp_refs)
        for ref in unknown_refs:
            issues.append(_issue(
                "BLOCK_REFS_UNKNOWN_COMPONENT", "error", ref,
                f"block(s) {ref_to_blocks[ref]!r} list ref {ref!r} "
                "which is not in components[]. Add the component "
                "to components[] or remove from component_refs[]."))

        # 6. structural block rules. Every threshold / list comes from
        # JSON --- no Python literals.
        rules_cfg = _load_block_rules()
        naming_cfg = _load_block_naming()
        justifications = _load_block_justifications()
        size_min = int(rules_cfg.get("minimum_components_per_block", 2))
        size_allow_one = bool(rules_cfg.get(
            "single_component_blocks_allowed", False))
        enforce_anchor = bool(rules_cfg.get("enforce_anchor_check", True))
        enforce_size = bool(rules_cfg.get("enforce_size_check", True))
        enforce_just = bool(rules_cfg.get("enforce_justification_check", True))
        enforce_unique = bool(rules_cfg.get("enforce_name_uniqueness", True))
        enforce_registry = bool(rules_cfg.get("enforce_name_in_registry", True))
        registry_blocks = set((naming_cfg.get("blocks") or {}).keys())

        # 6a. uniqueness -- duplicate block names break KiCad's per-sheet
        # net-path resolution (/MCU/RESET vs /MCU/RESET would collide).
        if enforce_unique:
            seen_names: Dict[str, int] = {}
            for blk in ir.blocks:
                seen_names[blk.name] = seen_names.get(blk.name, 0) + 1
            for name, count in seen_names.items():
                if count > 1:
                    issues.append(_issue(
                        "BLOCK_NAME_DUPLICATE", "error", name,
                        f"block name {name!r} appears {count}x in "
                        "blocks[]. Sheet names must be unique --- "
                        "KiCad uses them as the per-sheet net-path "
                        "prefix. Rename one of the duplicates."))

        for blk in ir.blocks:
            # 6b. name in the registry --- prevents the architect from
            # inventing block names not covered by block_naming.json
            # (which would lose their human-readable title).
            if enforce_registry and registry_blocks \
                    and blk.name not in registry_blocks:
                issues.append(_issue(
                    "BLOCK_NAME_NOT_IN_REGISTRY", "error", blk.name,
                    f"block name {blk.name!r} is not in "
                    "config/block_naming.json:blocks. Either rename to "
                    "one of the registered names, or extend the "
                    "registry. Common registered names include POWER, "
                    "MCU, USB_PROTECTION, ICSP, CLOCK, RF, MOTOR_DRIVER, "
                    "DISPLAY, AUDIO, COMM, etc."))

            # 6c. size --- single-component blocks are visual noise on
            # the parent's block diagram, EXCEPT when the single
            # component is a multi-pin connector belonging to a block
            # name on the single_connector_blocks whitelist (SWD /
            # ICSP / USB / etc.). A 6-pin programming header IS
            # self-contained --- forcing it to share a block with
            # CLOCK or MCU just splits unrelated sections visually
            # (the user's screenshot showed J2 in CLOCK splitting Y1
            # and its load caps).
            single_conn_blocks = {
                str(n).upper()
                for n in (rules_cfg.get("single_connector_blocks") or [])
            }
            connector_prefixes = tuple(
                str(p).upper()
                for p in (rules_cfg.get("connector_refdes_prefixes") or [])
            )
            min_anchor_pins = int(rules_cfg.get(
                "minimum_anchor_pin_count", 3))
            is_single_connector_exempt = bool(
                connector_prefixes
                and str(blk.name).upper() in single_conn_blocks
                and len(blk.component_refs) == 1
                and _refdes_prefix_letters(
                    blk.component_refs[0]) in connector_prefixes
                and _block_anchor_pin_count(
                    blk.component_refs[0], ir) >= min_anchor_pins
            )
            if (enforce_size and not size_allow_one
                    and not is_single_connector_exempt
                    and len(blk.component_refs) < size_min):
                issues.append(_issue(
                    "BLOCK_TOO_SMALL", "error", blk.name,
                    f"block {blk.name!r} has only "
                    f"{len(blk.component_refs)} component(s) "
                    f"(minimum is {size_min}). Merge it into a "
                    "sibling block, or delete it from blocks[]. "
                    "If this is a standalone programming header "
                    "(SWD/ICSP/JTAG/USB), add its name to "
                    "block_rules.json:single_connector_blocks so it "
                    "can stand alone."))

            # 6d. anchor --- every block must contain at least one
            # multi-pin IC / connector / crystal / etc., EXCEPT for
            # block names whitelisted in block_rules.json:
            # passives_only_blocks (INDICATOR / POWER_RAILS / FILTER /
            # RESET / etc.) which are legitimately passive-only per
            # the user's reference image.
            passives_only = {
                str(n).upper()
                for n in (rules_cfg.get("passives_only_blocks") or [])
            }
            is_passives_only_exempt = (
                str(blk.name).upper() in passives_only
            )
            if enforce_anchor and blk.component_refs \
                    and not is_passives_only_exempt \
                    and not _block_has_anchor(blk, ir, rules_cfg):
                issues.append(_issue(
                    "BLOCK_NO_ANCHOR", "error", blk.name,
                    f"block {blk.name!r} has no anchor component. "
                    "Every block must contain at least one component "
                    f"whose refdes letter is one of "
                    f"{rules_cfg.get('anchor_refdes_prefixes')!r} AND "
                    "whose pin count is >= "
                    f"{rules_cfg.get('minimum_anchor_pin_count', 3)}. "
                    "Blocks of only passives (R/C/L/D) are not "
                    "self-standing --- merge into a sibling block "
                    "that contains the anchor IC they belong to. "
                    "If this block is genuinely passive-only (e.g. "
                    "INDICATOR = LED + R), add its name to "
                    "block_rules.json:passives_only_blocks."))

            # 6e. justification --- did the user actually ask for
            # this block? RF when the prompt has no wireless mention
            # is the classic hallucination.
            if enforce_just \
                    and not _block_is_justified(
                        blk.name, prompt, ir, justifications):
                issues.append(_issue(
                    "BLOCK_NOT_JUSTIFIED", "error", blk.name,
                    f"block {blk.name!r} is not justified by the user "
                    "prompt or by any component lib_id. The "
                    "justification rules in "
                    "config/sheet_planner_rules.json:block_justifications "
                    "require at least one matching keyword in the prompt "
                    "OR at least one matching lib_id pattern. Remove "
                    f"{blk.name!r} from blocks[] or change the design "
                    "to include the parts that justify it."))

    # 7. design-checklist validators (per config/design_checklist.json).
    # Each is independent and non-breaking --- the IR keeps rendering
    # even if one fires; the architect retry loop picks up the error
    # and tries again with the relevant fix hint.
    issues.extend(_run_design_checklist(ir))

    # 8. cross-block net-name collision guard (Phase 0.2). Warning-only and a
    # no-op for flat / block-less IRs, so existing single-IC circuits are
    # byte-identical; only multi-block boards with generic cross-block names
    # (or a Layer-B auto-rename) produce an issue here.
    issues.extend(_check_cross_block_collisions(ir))

    # 9. requirement conformance (Phase 3): a part the user NAMED in the prompt
    # must appear in the circuit -- catches the architect dropping/substituting a
    # requested part. Warning-only; no-op on an empty prompt so the golden
    # harness (validate_ir called without a prompt) stays byte-identical.
    issues.extend(_check_requirement_conformance(ir, prompt))

    return issues


# ----- design-checklist validators -------------------------------------

def _has_component_satisfying(
    ir: TopologyIR,
    lib_patterns: List[str],
    must_be_on_nets: Optional[List[str]] = None,
    require_value_substr: Optional[str] = None,
) -> bool:
    """True when at least one component matches the lib_id patterns AND
    (optionally) sits on one of the named nets AND (optionally) has the
    given substring in its value."""
    for comp in ir.components:
        if not _comp_matches_any_lib_pattern(comp, lib_patterns):
            continue
        if require_value_substr and require_value_substr.lower() not in (comp.value or "").lower():
            continue
        if must_be_on_nets:
            on_any = False
            for net in ir.nets:
                if net.name in must_be_on_nets and any(p.startswith(f"{comp.ref}.") for p in net.pins):
                    on_any = True
                    break
            if not on_any:
                continue
        return True
    return False


def _find_lib_components(ir: TopologyIR, patterns: List[str]):
    for comp in ir.components:
        if _comp_matches_any_lib_pattern(comp, patterns):
            yield comp


_RES_VAL_RE = re.compile(
    r"^\s*([0-9]+(?:[.,][0-9]+)?)"      # integer or decimal mantissa
    r"\s*([rRkKmMgG]?)"                  # optional SI suffix
    r"([0-9]+)?\s*([rRkKmMgG]?)?"         # optional IEC-RKM trailing-digits + unit
    r"\s*(?:ohms?|R)?\s*$"
)


def _parse_resistance_to_ohms(value: str) -> Optional[float]:
    """Parse `"4k7"` / `"4.7k"` / `"4700"` / `"4700R"` / `"4.7kOhm"` /
    `"5K1"` (any case) to ohms. Returns None when unparseable so the
    caller can fall back to string equality.

    Handles both IEC RKM (`5k1` = 5100 R, where the unit letter sits in
    the decimal-point position) and engineering notation (`5.1k` = 5100 R).
    """
    if not value:
        return None
    v = value.strip()
    # Strip a trailing "Ohm" / "Ohms" / "R" if present after a unit letter
    v_clean = v.replace("ohms", "").replace("Ohms", "").replace("ohm", "").replace("Ohm", "")
    m = _RES_VAL_RE.match(v_clean)
    if not m:
        # Last-ditch: pure integer like "470" or "4700R"
        only_digits = v_clean.replace("R", "").replace("r", "").replace(",", ".").strip()
        try:
            return float(only_digits)
        except (TypeError, ValueError):
            return None
    mantissa_str, unit_a, trailing_digits, unit_b = m.groups()
    mantissa_str = mantissa_str.replace(",", ".")
    try:
        mantissa = float(mantissa_str)
    except (TypeError, ValueError):
        return None
    suffix_to_mul = {
        "": 1.0, "r": 1.0, "R": 1.0,
        "k": 1e3, "K": 1e3,
        # EIA/IEC: lowercase 'm' = milli (10^-3); uppercase 'M' = mega (10^6).
        # Milliohm is how real shunt resistors are written ("1m" = 1 mΩ on a
        # 100 A pack); nobody writes a megohm as lowercase "m" (that is "M").
        "m": 1e-3, "M": 1e6,
        "g": 1e9, "G": 1e9,
    }
    if trailing_digits:
        # IEC RKM: unit letter sits where the decimal point would be.
        # Example: "5K1" -> mantissa=5, unit="K", trailing="1" -> 5.1 * 1000.
        unit = unit_a or unit_b or ""
        mul = suffix_to_mul.get(unit, 1.0)
        decimal_part = float("0." + trailing_digits)
        return (mantissa + decimal_part) * mul
    # Engineering: "5.1k" -> mantissa=5.1, unit="k", no trailing.
    unit = unit_b or unit_a or ""
    mul = suffix_to_mul.get(unit, 1.0)
    return mantissa * mul


def _value_close_to(value: str, target_value: str, tol_pct: float) -> bool:
    """Tolerance comparison for resistor values. Handles all common
    spellings (`5.1k`, `5k1`, `5100`, `5100R`, case-insensitive) by
    parsing both to ohms and comparing numerically. Falls back to
    string equality for non-resistor values (caps, units the parser
    doesn't recognise)."""
    if not value or not target_value:
        return False
    v_norm = value.strip().lower()
    t_norm = target_value.strip().lower()
    if v_norm == t_norm:
        return True
    v_ohms = _parse_resistance_to_ohms(value)
    t_ohms = _parse_resistance_to_ohms(target_value)
    if v_ohms is None or t_ohms is None:
        return False
    if t_ohms == 0:
        return v_ohms == 0
    tol = max(0.001, float(tol_pct) / 100.0)
    return abs(v_ohms - t_ohms) / t_ohms <= tol


def _pin_net_by_patterns(ir: TopologyIR, ref: str, patterns):
    """Yield the net(s) on the FIRST of `patterns` that resolves to a real pin
    of `ref` (matched by exact pin name OR number via _ir_nets_for_pin). Used by
    the Phase-1 checks to locate CANH/CANL/IN+/IN- regardless of which alias a
    given symbol uses for the pin."""
    for pat in patterns or []:
        nets = list(_ir_nets_for_pin(ir, ref, pat))
        if nets:
            yield from nets
            return


def _part_bridges_nets(ir: TopologyIR, net_a, net_b, ref_prefix, value_pred) -> bool:
    """True when a 2-pin part whose ref starts with `ref_prefix` has ONE pin on
    `net_a` and its OTHER pin on `net_b` (it STRADDLES the two nets) and
    `value_pred(value)` holds. Reused by the CAN-termination (120R across
    CANH-CANL) and current-shunt (<= shunt_max_ohm across IN+/IN-) checks.
    net_a/net_b are IRNet objects; a part on the SAME net for both is ignored."""
    if net_a is None or net_b is None or net_a.name == net_b.name:
        return False
    a_refs = {p.split(".", 1)[0] for p in net_a.pins if "." in p}
    b_refs = {p.split(".", 1)[0] for p in net_b.pins if "." in p}
    for ref in (a_refs & b_refs):
        if not ref.startswith(ref_prefix):
            continue
        comp = ir.component_by_ref(ref)
        if comp is None:
            continue
        if value_pred(comp.value or ""):
            return True
    return False


def _can_split_terminated(ir: TopologyIR, net_h, net_l, split_val, split_tol,
                          ref_prefix) -> bool:
    """Detect SPLIT CAN termination: a `split_val`-ohm resistor from CANH to a
    midpoint M and another from CANL to the SAME M (M usually also carries a
    filter cap to GND). Returns True when CANH and CANL each reach a shared
    midpoint through a ~split_val resistor — the two in series = 120R."""
    def _mid_nets(net):
        mids = set()
        for p in net.pins:
            if "." not in p:
                continue
            ref = p.split(".", 1)[0]
            if not ref.startswith(ref_prefix):
                continue
            comp = ir.component_by_ref(ref)
            if comp is None or not _value_close_to(comp.value or "", split_val, split_tol):
                continue
            for n2 in ir.nets:                 # this resistor's OTHER net(s) = midpoint
                if n2.name == net.name:
                    continue
                if any(pp.startswith(f"{ref}.") for pp in n2.pins):
                    mids.add(n2.name)
        return mids
    return bool(_mid_nets(net_h) & _mid_nets(net_l))


def _run_design_checklist(ir: TopologyIR) -> List[Dict[str, Any]]:
    """Run every enabled rule in config/design_checklist.json against
    the IR. Returns a list of issue dicts. Pure JSON consumption ---
    no hardcoded part names, pin names, or values."""
    cfg = _load_design_checklist()
    if not cfg.get("enabled", True):
        return []
    rules = cfg.get("rules") or {}
    out: List[Dict[str, Any]] = []

    # USB-C UFP --- 5.1k CC pulldowns + input protection
    usb_rule = rules.get("usb_c_ufp") or {}
    if usb_rule.get("enabled", True):
        triggers = usb_rule.get("trigger_lib_id_patterns") or []
        for connector in _find_lib_components(ir, triggers):
            # 5.1k pulldowns on CC1 + CC2
            pull = usb_rule.get("required_pulldowns") or {}
            cc_pins = pull.get("pins") or []
            target_val = str(pull.get("value", "5.1k"))
            for cc_pin in cc_pins:
                cc_nets = list(_ir_nets_for_pin(ir, connector.ref, cc_pin))
                if not cc_nets:
                    out.append(_issue(
                        "USB_C_CC_PIN_UNCONNECTED", "error",
                        f"{connector.ref}.{cc_pin}",
                        f"USB-C UFP requires a {target_val} pulldown from "
                        f"{cc_pin} to GND, but {connector.ref}.{cc_pin} "
                        "is not in any net. Add a resistor between this pin and GND."))
                    continue
                # Find a resistor on cc_pin AND on GND with the target value
                resistor_ok = False
                for net in cc_nets:
                    for pinref in net.pins:
                        if not pinref.startswith("R"):
                            continue
                        if "." not in pinref:
                            continue
                        rref = pinref.split(".", 1)[0]
                        comp = ir.component_by_ref(rref)
                        if comp is None:
                            continue
                        if _value_close_to(comp.value or "", target_val, pull.get("tolerance_pct", 5)):
                            # also confirm OTHER pin of this resistor is on GND
                            for n2 in ir.nets:
                                if n2.name.upper() != "GND":
                                    continue
                                if any(p.startswith(f"{rref}.") for p in n2.pins):
                                    resistor_ok = True
                                    break
                        if resistor_ok:
                            break
                    if resistor_ok:
                        break
                if not resistor_ok:
                    out.append(_issue(
                        "USB_C_CC_PULLDOWN_MISSING", "error",
                        f"{connector.ref}.{cc_pin}",
                        f"USB-C UFP requires a {target_val} resistor from "
                        f"{connector.ref}.{cc_pin} to GND for sink-mode "
                        "advertisement. Add a resistor matching the "
                        "configured value/tolerance in design_checklist.json."))
            # Input protection: Polyfuse + ESD
            prot = usb_rule.get("required_input_protection") or {}
            poly = prot.get("polyfuse") or {}
            if poly.get("required", True):
                if not _has_component_satisfying(ir, poly.get("lib_id_patterns") or []):
                    out.append(_issue(
                        "USB_C_POLYFUSE_MISSING", "warning", connector.ref,
                        "USB-C input should have a polyfuse (PTC) for "
                        "overcurrent protection. Add a Polyfuse/PTC on the "
                        "VBUS line (typical 500 mA hold current for sink)."))
            tvs = prot.get("esd_tvs") or {}
            if tvs.get("required", True):
                if not _has_component_satisfying(ir, tvs.get("lib_id_patterns") or []):
                    out.append(_issue(
                        "USB_C_ESD_MISSING", "warning", connector.ref,
                        "USB-C VBUS should have an ESD/TVS diode to GND "
                        "(SMBJ5.0A or equivalent). Add a TVS across VBUS-GND."))

    # LDO regulator --- input + output bulk caps
    ldo_rule = rules.get("ldo_linear_regulator") or {}
    if ldo_rule.get("enabled", True):
        triggers = ldo_rule.get("trigger_lib_id_patterns") or []
        for ldo in _find_lib_components(ir, triggers):
            for slot_key, code_suffix, label in (
                ("required_input_cap", "INPUT", "input"),
                ("required_output_cap", "OUTPUT", "output"),
            ):
                slot = ldo_rule.get(slot_key) or {}
                if not slot:
                    continue
                pin_pats = slot.get("on_pin_patterns") or []
                min_uf = float(slot.get("min_uf", 10.0))
                # find ANY cap on a net that also has one of the LDO's IN/OUT pins
                cap_found = False
                cap_value_ok = False
                for net in ir.nets:
                    ldo_on_this = any(
                        p.split(".", 1)[1].upper() in (s.upper() for s in pin_pats)
                        for p in net.pins
                        if p.startswith(f"{ldo.ref}.") and "." in p
                    )
                    if not ldo_on_this:
                        continue
                    for pinref in net.pins:
                        ref_part = pinref.split(".", 1)[0]
                        if not ref_part.startswith("C"):
                            continue
                        cap = ir.component_by_ref(ref_part)
                        if cap is None:
                            continue
                        cap_found = True
                        if _pin_value_to_uf(cap.value or "") >= min_uf - 1e-6:
                            cap_value_ok = True
                            break
                    if cap_value_ok:
                        break
                if not cap_found:
                    out.append(_issue(
                        f"LDO_{code_suffix}_CAP_MISSING", "error", ldo.ref,
                        f"LDO {ldo.ref} ({ldo.value}) has no {label} cap. "
                        f"Add a >= {min_uf:g} uF ceramic on the {label} pin."))
                elif not cap_value_ok:
                    out.append(_issue(
                        f"LDO_{code_suffix}_CAP_TOO_SMALL", "warning", ldo.ref,
                        f"LDO {ldo.ref} {label} cap is below the "
                        f"datasheet minimum of {min_uf:g} uF."))

    # Crystal --- 2 load caps each to GND
    xtal_rule = rules.get("crystal_oscillator") or {}
    if xtal_rule.get("enabled", True):
        triggers = xtal_rule.get("trigger_lib_id_patterns") or []
        load_cfg = xtal_rule.get("required_load_caps") or {}
        need_count = int(load_cfg.get("count", 2))
        need_gnd = bool(load_cfg.get("must_connect_to_gnd", True))
        for xtal in _find_lib_components(ir, triggers):
            # find caps that share a net with xtal AND have a GND pin
            xtal_nets = [n for n in ir.nets if any(p.startswith(f"{xtal.ref}.") for p in n.pins)]
            shared_cap_refs = set()
            for net in xtal_nets:
                for pinref in net.pins:
                    rp = pinref.split(".", 1)[0]
                    if rp.startswith("C") and rp != xtal.ref:
                        shared_cap_refs.add(rp)
            if len(shared_cap_refs) < need_count:
                out.append(_issue(
                    "CRYSTAL_LOAD_CAPS_MISSING", "error", xtal.ref,
                    f"crystal {xtal.ref} ({xtal.value}) needs "
                    f"{need_count} load capacitors but only "
                    f"{len(shared_cap_refs)} cap(s) share a net with it. "
                    "Add the missing load caps (typical 18-22 pF for "
                    "16 MHz crystals)."))
                continue
            if need_gnd:
                for cref in shared_cap_refs:
                    has_gnd = False
                    for net in ir.nets:
                        if net.name.upper() in ("GND", "AGND", "DGND") \
                                and any(p.startswith(f"{cref}.") for p in net.pins):
                            has_gnd = True
                            break
                    if not has_gnd:
                        out.append(_issue(
                            "CRYSTAL_LOAD_CAP_NO_GND", "error", cref,
                            f"crystal load cap {cref} has no GND connection. "
                            "The non-crystal pin of each load cap must go to GND."))

    # Reset circuit --- NRST pullup required, button optional
    rst_rule = rules.get("reset_circuit") or {}
    if rst_rule.get("enabled", True):
        patterns = rst_rule.get("trigger_pin_name_patterns") or []
        if patterns:
            for comp in ir.components:
                try:
                    geom = load_symbol(comp.lib_id)
                except Exception:
                    continue
                nrst_pin = None
                for pin in geom.pins or []:
                    if _pin_name_matches(pin.name, patterns):
                        nrst_pin = pin
                        break
                if nrst_pin is None:
                    continue
                # Find the net carrying NRST
                nrst_net = None
                for net in ir.nets:
                    if f"{comp.ref}.{nrst_pin.number}" in net.pins \
                            or f"{comp.ref}.{nrst_pin.name}" in net.pins:
                        nrst_net = net
                        break
                if nrst_net is None:
                    continue
                # Pullup check
                pull = rst_rule.get("required_pullup") or {}
                if pull.get("required", True):
                    has_pullup = False
                    for pinref in nrst_net.pins:
                        rp = pinref.split(".", 1)[0]
                        if not rp.startswith("R"):
                            continue
                        rcomp = ir.component_by_ref(rp)
                        if rcomp is None:
                            continue
                        # Resistor's other pin should be on a power rail
                        for n2 in ir.nets:
                            if not n2.is_power:
                                continue
                            if n2.name.upper() in ("GND", "AGND", "DGND"):
                                continue
                            if any(p.startswith(f"{rp}.") for p in n2.pins):
                                has_pullup = True
                                break
                        if has_pullup:
                            break
                    if not has_pullup:
                        out.append(_issue(
                            "MCU_NO_RESET_PULLUP", "error", comp.ref,
                            f"MCU {comp.ref} reset pin {nrst_pin.name} "
                            "has no pullup resistor to VCC. Add a 10k "
                            "from NRST to +3V3 (or the MCU's VDD rail) "
                            "to prevent spurious resets."))
                # Button check (warning only)
                btn_cfg = rst_rule.get("required_button") or {}
                if btn_cfg.get("required", False):
                    has_btn = any(
                        _comp_matches_any_lib_pattern(
                            ir.component_by_ref(p.split(".", 1)[0]) or comp,
                            btn_cfg.get("lib_id_patterns") or []
                        )
                        for p in nrst_net.pins
                        if "." in p and ir.component_by_ref(p.split(".", 1)[0]) is not None
                    )
                    if not has_btn:
                        out.append(_issue(
                            "MCU_NO_RESET_BUTTON",
                            btn_cfg.get("severity", "warning"),
                            comp.ref,
                            f"MCU {comp.ref} has no manual reset button "
                            "to GND. Recommended for boards used during "
                            "development."))

    # LED indicator --- series resistor required
    led_rule = rules.get("led_indicator_circuit") or {}
    if led_rule.get("enabled", True):
        triggers = led_rule.get("trigger_lib_id_patterns") or []
        req = led_rule.get("required_series_resistor") or {}
        if req.get("required", True):
            for led in _find_lib_components(ir, triggers):
                led_nets = [n for n in ir.nets if any(p.startswith(f"{led.ref}.") for p in n.pins)]
                has_r = False
                for net in led_nets:
                    for pinref in net.pins:
                        rp = pinref.split(".", 1)[0]
                        if rp.startswith("R") and rp != led.ref:
                            has_r = True
                            break
                    if has_r:
                        break
                if not has_r:
                    out.append(_issue(
                        "LED_SERIES_RESISTOR_MISSING", "error", led.ref,
                        f"LED {led.ref} has no series current-limit "
                        "resistor in its net. Add a resistor (typical "
                        "330R for 3.3V rail) in series with the LED."))

    # SWD/JTAG header --- when MCU has SWDIO/SWCLK pins, a header should exist
    swd_rule = rules.get("swd_jtag_header") or {}
    if swd_rule.get("enabled", True):
        required_signals = swd_rule.get("swd_required_signals") or []
        conn_patterns = swd_rule.get("swd_connector_lib_id_patterns") or []
        # Does any MCU expose SWDIO + SWCLK pins?
        mcu_has_swd = False
        for comp in ir.components:
            try:
                geom = load_symbol(comp.lib_id)
            except Exception:
                continue
            pin_names = {(p.name or "").upper() for p in (geom.pins or [])}
            if "SWDIO" in pin_names and "SWCLK" in pin_names:
                mcu_has_swd = True
                break
        if mcu_has_swd:
            has_header = any(
                _comp_matches_any_lib_pattern(c, conn_patterns)
                for c in ir.components
            )
            if not has_header:
                out.append(_issue(
                    "MCU_SWD_NO_HEADER", "warning", "",
                    "MCU exposes SWDIO + SWCLK but no SWD programming "
                    "header was found. Add a 6-pin header carrying "
                    f"{required_signals!r} so the board can be flashed/debugged."))

    # CAN bus termination (Phase 1) --- 120R across CANH-CANL at a bus END.
    # Missing termination is a WARNING (a mid-bus stub legitimately has none);
    # a floating CANH/CANL transceiver pin is an ERROR.
    can_rule = rules.get("can_bus_termination") or {}
    if can_rule.get("enabled", True):
        triggers = can_rule.get("trigger_lib_id_patterns") or []
        h_pats = can_rule.get("can_high_pin_patterns") or []
        l_pats = can_rule.get("can_low_pin_patterns") or []
        term = can_rule.get("termination") or {}
        t_val, t_tol = str(term.get("value", "120")), float(term.get("tolerance_pct", 5))
        s_val, s_tol = str(term.get("split_value", "60")), float(term.get("split_tolerance_pct", 10))
        t_sev, t_pref = str(term.get("severity", "warning")), str(term.get("refdes_prefix", "R"))

        def _is_full_term(v):
            return _value_close_to(v, t_val, t_tol)

        for xcvr in _find_lib_components(ir, triggers):
            canh = next(iter(_pin_net_by_patterns(ir, xcvr.ref, h_pats)), None)
            canl = next(iter(_pin_net_by_patterns(ir, xcvr.ref, l_pats)), None)
            if canh is None or canl is None:
                missing = "CANH" if canh is None else "CANL"
                out.append(_issue(
                    "CAN_PIN_UNCONNECTED", "error", xcvr.ref,
                    f"CAN transceiver {xcvr.ref} has its {missing} pin "
                    "unconnected. Wire CANH and CANL to the bus."))
                continue
            terminated = (
                _part_bridges_nets(ir, canh, canl, t_pref, _is_full_term)       # single 120R
                or _can_split_terminated(ir, canh, canl, s_val, s_tol, t_pref))  # 2x60R + cap
            if not terminated:
                out.append(_issue(
                    "CAN_TERMINATION_MISSING", t_sev, xcvr.ref,
                    f"no {t_val}-ohm termination found across CANH-CANL on "
                    f"{xcvr.ref}. If this board sits at a physical END of the CAN "
                    "bus, add a 120-ohm across CANH-CANL (or split 2x60-ohm + cap); "
                    "a mid-bus stub needs none."))

    # ALERT / interrupt reaches an MCU GPIO (Phase 1) --- the BMS floating-ALERT
    # failure. Only a NON-MCU peripheral is the SOURCE; its net must carry a pin
    # of a DIFFERENT MCU component. Warning (the board still renders).
    alert_rule = rules.get("alert_interrupt_connected") or {}
    if alert_rule.get("enabled", True):
        src_pats = alert_rule.get("source_pin_name_patterns") or []
        mcu_pats = alert_rule.get("mcu_lib_id_patterns") or []
        a_sev = str(alert_rule.get("severity", "warning"))
        if src_pats:
            for comp in ir.components:
                if _comp_matches_any_lib_pattern(comp, mcu_pats):
                    continue  # an MCU is the interrupt DESTINATION, not source
                try:
                    geom = load_symbol(comp.lib_id)
                except Exception:
                    continue
                for pin in (geom.pins or []):
                    if not _pin_name_matches(pin.name, src_pats):
                        continue
                    net = (next(iter(_ir_nets_for_pin(ir, comp.ref, pin.name)), None)
                           or next(iter(_ir_nets_for_pin(ir, comp.ref, pin.number)), None))
                    reaches_mcu = False
                    if net is not None:
                        for pinref in net.pins:
                            rp = pinref.split(".", 1)[0] if "." in pinref else ""
                            if rp == comp.ref:
                                continue
                            other = ir.component_by_ref(rp)
                            if other is not None and _comp_matches_any_lib_pattern(other, mcu_pats):
                                reaches_mcu = True
                                break
                    if not reaches_mcu:
                        out.append(_issue(
                            "ALERT_NET_NOT_CONNECTED", a_sev,
                            f"{comp.ref}.{pin.name}",
                            f"interrupt output {comp.ref}.{pin.name} does not reach "
                            "an MCU GPIO. Wire it to a free MCU input so the "
                            "interrupt can be serviced (a floating ALERT is the "
                            "classic dead-interrupt defect)."))

    # Current-shunt amplifier IN+/IN- straddle the shunt (Phase 1). Inputs
    # floating or shorted = error; no shunt between them = polarity-suspect
    # warning. Only resistors already on an IN+/IN- net are inspected.
    shunt_rule = rules.get("current_shunt_amplifier") or {}
    if shunt_rule.get("enabled", True):
        triggers = shunt_rule.get("trigger_lib_id_patterns") or []
        inp_pats = shunt_rule.get("in_plus_pin_patterns") or []
        inn_pats = shunt_rule.get("in_minus_pin_patterns") or []
        max_ohm = float(shunt_rule.get("shunt_max_ohm", 1.0))
        s_sev = str(shunt_rule.get("severity", "warning"))

        def _is_shunt(v):
            r = _parse_resistance_to_ohms(v)
            return r is not None and r <= max_ohm + 1e-9

        for amp in _find_lib_components(ir, triggers):
            inp = next(iter(_pin_net_by_patterns(ir, amp.ref, inp_pats)), None)
            inn = next(iter(_pin_net_by_patterns(ir, amp.ref, inn_pats)), None)
            if inp is None or inn is None:
                out.append(_issue(
                    "SHUNT_AMP_INPUT_UNCONNECTED", "error", amp.ref,
                    f"current-sense amp {amp.ref} has a floating sense input "
                    "(IN+ or IN-). Both must connect across the shunt resistor."))
                continue
            if inp.name == inn.name:
                out.append(_issue(
                    "SHUNT_AMP_INPUTS_SHORTED", "error", amp.ref,
                    f"current-sense amp {amp.ref} has IN+ and IN- on the SAME net "
                    f"({inp.name}) — it would read 0 A. Connect IN+ and IN- to the "
                    "two OPPOSITE ends of the shunt resistor."))
                continue
            if not _part_bridges_nets(ir, inp, inn, "R", _is_shunt):
                out.append(_issue(
                    "SHUNT_POLARITY_SUSPECT", s_sev, amp.ref,
                    f"current-sense amp {amp.ref}: no shunt resistor (<= {max_ohm:g} "
                    "ohm) found straddling IN+ and IN-. Verify a low-value shunt "
                    "sits between the two inputs with the correct polarity."))

    # Generic pin-pattern bypass caps (DYNAMIC, part-agnostic). Any IC pin whose
    # NAME matches a regulator-output / reference / charge-pump / internal-LDO
    # pattern needs a local bypass cap to GND -- fires by pin name alone, so it
    # covers BQ76952 (REGIN/REG18/BREG/CP1...), STM32 (VCAP), op-amps, PMICs,
    # anything. No per-part template. Warning-only; a connected pin with no cap
    # on its net (whose other end is GND) is flagged.
    gpb = rules.get("generic_pin_bypass") or {}
    if gpb.get("enabled", True):
        pats = []
        for p in (gpb.get("bypass_pin_name_patterns") or []):
            try:
                pats.append(re.compile(p, re.I))
            except re.error:
                continue
        sev = str(gpb.get("severity", "warning"))
        cpref = str(gpb.get("cap_refdes_prefix", "C"))
        gnd_up = {"GND", "AGND", "DGND", "PGND", "VSS"}
        if pats:
            for comp in ir.components:
                try:
                    geom = load_symbol(comp.lib_id)
                except Exception:
                    continue
                pins = geom.pins or []
                if len(pins) < 3:
                    continue  # ICs only; skip 2-pin passives
                for pin in pins:
                    nm = pin.name or ""
                    if not nm or not any(p.search(nm) for p in pats):
                        continue
                    net = (next(iter(_ir_nets_for_pin(ir, comp.ref, nm)), None)
                           or next(iter(_ir_nets_for_pin(ir, comp.ref, pin.number)), None))
                    if net is None:
                        continue  # floating pin -> POWER_PIN_FLOATING etc. owns it
                    has_bypass = False
                    for pinref in net.pins:
                        rp = pinref.split(".", 1)[0] if "." in pinref else ""
                        if not rp.startswith(cpref):
                            continue
                        if ir.component_by_ref(rp) is None:
                            continue
                        for n2 in ir.nets:
                            if n2.name.upper() in gnd_up and any(
                                    pp.startswith(f"{rp}.") for pp in n2.pins):
                                has_bypass = True
                                break
                        if has_bypass:
                            break
                    if not has_bypass:
                        out.append(_issue(
                            "IC_PIN_NO_BYPASS", sev, f"{comp.ref}.{nm}",
                            f"pin {nm} of {comp.ref} ({comp.lib_id}) needs a local "
                            "bypass/decoupling cap to GND. Add a ceramic from this "
                            "pin to GND (datasheet-standard for this pin type)."))

    # Generic open-drain bus pull-up (DYNAMIC, part-agnostic). An I2C/SMBus net
    # (SCL/SDA) needs ONE pull-up per BUS NET to a logic rail. Detected by pin
    # name on any IC; flagged per NET (not per pin) so a shared bus -> one flag.
    gbp = rules.get("generic_bus_pullup") or {}
    if gbp.get("enabled", True):
        bpats = []
        for p in (gbp.get("bus_pin_name_patterns") or []):
            try:
                bpats.append(re.compile(p, re.I))
            except re.error:
                continue
        bsev = str(gbp.get("severity", "warning"))
        rpref = str(gbp.get("pullup_refdes_prefix", "R"))
        gnd_up2 = {"GND", "AGND", "DGND", "PGND", "VSS"}
        if bpats:
            bus_nets: Dict[str, Any] = {}
            for comp in ir.components:
                try:
                    geom = load_symbol(comp.lib_id)
                except Exception:
                    continue
                if len(geom.pins or []) < 3:
                    continue
                for pin in (geom.pins or []):
                    nm = pin.name or ""
                    if not nm or not any(p.search(nm) for p in bpats):
                        continue
                    net = (next(iter(_ir_nets_for_pin(ir, comp.ref, nm)), None)
                           or next(iter(_ir_nets_for_pin(ir, comp.ref, pin.number)), None))
                    if net is not None and not bool(getattr(net, "is_power", False)):
                        bus_nets[net.name] = net
            for net in bus_nets.values():
                has_pullup = False
                for pinref in net.pins:
                    rp = pinref.split(".", 1)[0] if "." in pinref else ""
                    if not rp.startswith(rpref) or ir.component_by_ref(rp) is None:
                        continue
                    for n2 in ir.nets:
                        if (bool(getattr(n2, "is_power", False))
                                and n2.name.upper() not in gnd_up2
                                and any(pp.startswith(f"{rp}.") for pp in n2.pins)):
                            has_pullup = True
                            break
                    if has_pullup:
                        break
                if not has_pullup:
                    out.append(_issue(
                        "IC_BUS_NO_PULLUP", bsev, net.name,
                        f"open-drain bus net {net.name!r} has no pull-up to a logic rail. "
                        "Add one pull-up per line (e.g. 4.7k to +3V3) — I2C/SMBus lines "
                        "need external pull-ups."))

    # Generic low-side current-sense RC filter (DYNAMIC, part-agnostic). A sense
    # pair (SRP/SRN, CSP/CSN, ISP/ISN, VSP/VSN, SENSEP/SENSEN — any IC, by pin
    # name) connects to the shunt through an external RC filter: a DIFFERENTIAL
    # cap belongs ACROSS the two sense nets, and caps from these pins to VSS are
    # FORBIDDEN by datasheet. Three codes: SENSE_NO_SHUNT (no low-R straddle —
    # detect-only), SENSE_NO_DIFF_FILTER (no cap across pair — warn + auto-add),
    # SENSE_CAP_TO_GND (cap ties a sense net to GND — detect-only anti-pattern).
    # INA-style IN+/IN- are NOT here (current_shunt_amplifier owns them) because
    # a diff cap there can wreck CMRR. The series filter R is the architect's job.
    gsf = rules.get("generic_sense_filter") or {}
    if gsf.get("enabled", True):
        fsev = str(gsf.get("severity", "warning"))
        cpref = str(gsf.get("cap_refdes_prefix", "C"))
        flag_gnd = bool(gsf.get("flag_cap_to_gnd", True))
        s_max = float(gsf.get("shunt_max_ohm", 1.0))
        dval = str(gsf.get("diff_cap_value", "100n"))
        gnd_up3 = {"GND", "AGND", "DGND", "PGND", "VSS"}

        def _is_sense_shunt(v):
            r = _parse_resistance_to_ohms(v)
            return r is not None and r <= s_max + 1e-9

        seen_pairs = set()
        for comp in ir.components:
            try:
                geom = load_symbol(comp.lib_id)
            except Exception:
                continue
            names = [p.name or "" for p in (geom.pins or [])]
            for pair in (gsf.get("sense_pin_pairs") or []):
                try:
                    pp = re.compile(pair.get("p", ""), re.I)
                    pn = re.compile(pair.get("n", ""), re.I)
                except re.error:
                    continue
                p_name = next((n for n in names if n and pp.search(n)), None)
                n_name = next((n for n in names if n and pn.search(n)), None)
                if not p_name or not n_name:
                    continue
                net_p = next(iter(_ir_nets_for_pin(ir, comp.ref, p_name)), None)
                net_n = next(iter(_ir_nets_for_pin(ir, comp.ref, n_name)), None)
                if net_p is None or net_n is None:
                    continue  # floating sense pin -> connectivity check owns it
                if net_p.name == net_n.name:
                    continue  # shorted pair -> shunt/connectivity check owns it
                if bool(getattr(net_p, "is_power", False)) or \
                        bool(getattr(net_n, "is_power", False)):
                    continue
                key = (comp.ref,) + tuple(sorted((net_p.name, net_n.name)))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                # cap-to-GND anti-pattern (datasheet forbids it on sense pins)
                if flag_gnd:
                    for snet in (net_p, net_n):
                        hit = any(
                            n2.name.upper() in gnd_up3
                            and _part_bridges_nets(ir, snet, n2, cpref, lambda v: True)
                            for n2 in ir.nets)
                        if hit:
                            out.append(_issue(
                                "SENSE_CAP_TO_GND", fsev, snet.name,
                                f"a {cpref}* cap ties current-sense net {snet.name!r} to "
                                "GND. The datasheet forbids caps from these sense pins to "
                                "VSS — use a DIFFERENTIAL cap across the pair instead."))
                # missing shunt straddling the pair (detect-only)
                if not _part_bridges_nets(ir, net_p, net_n, "R", _is_sense_shunt):
                    out.append(_issue(
                        "SENSE_NO_SHUNT", fsev, f"{comp.ref}.{p_name}",
                        f"current-sense pair {p_name}/{n_name} on {comp.ref}: no shunt "
                        f"(<= {s_max:g} ohm) straddles the two sense nets. Verify a "
                        "low-value sense resistor sits between them."))
                # missing differential filter cap (warn + auto-add)
                if not _part_bridges_nets(ir, net_p, net_n, cpref, lambda v: True):
                    out.append(_issue(
                        "SENSE_NO_DIFF_FILTER", fsev, f"{net_p.name}|{net_n.name}",
                        f"current-sense pair {p_name}/{n_name} on {comp.ref} has no "
                        f"differential filter cap across the sense nets. Add ~{dval} "
                        "across the pins (datasheet RC-filter; keep the series R)."))

    # Generic thermistor temperature input (DYNAMIC, part-agnostic, DETECT-ONLY).
    # A pin named TS/TSx/THERM*/NTC* should carry an EXTERNAL sensing element; a
    # net holding ONLY the IC pin (no other component) is a bare/unwired thermistor
    # input -> TEMP_SENSE_NO_THERMISTOR (warning). Deliberately NOT auto-synthesised:
    # the bias is part/voltage-dependent (AFEs bias internally; an external pull-up
    # draws continuous power and can exceed REG18), so the divider is the architect's
    # call. The check only surfaces a bare input; it never prescribes the topology.
    gth = rules.get("generic_thermistor") or {}
    if gth.get("enabled", True):
        tpats = []
        for p in (gth.get("thermistor_pin_patterns") or []):
            try:
                tpats.append(re.compile(p, re.I))
            except re.error:
                continue
        tsev = str(gth.get("severity", "warning"))
        if tpats:
            for comp in ir.components:
                try:
                    geom = load_symbol(comp.lib_id)
                except Exception:
                    continue
                if len(geom.pins or []) < 3:
                    continue  # ICs only; skip 2-pin passives
                for pin in (geom.pins or []):
                    nm = pin.name or ""
                    if not nm or not any(p.search(nm) for p in tpats):
                        continue
                    net = (next(iter(_ir_nets_for_pin(ir, comp.ref, nm)), None)
                           or next(iter(_ir_nets_for_pin(ir, comp.ref, pin.number)), None))
                    if net is None:
                        continue  # floating -> connectivity check owns it
                    others = {pr.split(".", 1)[0] for pr in net.pins
                              if "." in pr and pr.split(".", 1)[0] != comp.ref}
                    if not others:
                        out.append(_issue(
                            "TEMP_SENSE_NO_THERMISTOR", tsev, f"{comp.ref}.{nm}",
                            f"thermistor input {nm} of {comp.ref} has no external "
                            "sensing element on its net. Add an NTC (and bias per the "
                            "datasheet — internal pull-up on AFEs, divider on an MCU)."))

    return out


def has_errors(issues: List[Dict[str, Any]]) -> bool:
    return any(i["severity"] == "error" for i in issues)


def dedupe_issues(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse repeated (code, where) tuples — a missing lib_id surfaces
    once per pin in every net that references the component, which floods
    the error report. Keep first occurrence only."""
    seen = set()
    out = []
    for it in issues:
        key = (it.get("code"), it.get("where"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
