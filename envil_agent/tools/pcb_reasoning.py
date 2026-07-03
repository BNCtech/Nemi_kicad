"""tools/pcb_reasoning.py — Phase 1 of the Universal PCB AI Engine.

Stages 1-3 (+5) of ``PCB_AI_ARCHITECTURE.md``: read a circuit's electrical
topology, classify every component from **pin behaviour** (not its name), score
criticality, pick the placement anchor, and emit per-component constraints.

This is the *electrical-reasoning* layer the constraint-weighted placer (Phase 2)
and the priority router (Phase 3) consume. It is **read-only**: it never mutates
the board — it writes a ``<base>.envil-constraints.json`` sidecar next to the
source. Breaks nothing; with ``pcb_constraint_model.json:enabled=false`` it no-ops.

Input preference:
  1. ``<base>.envil-ir.json``  — richest: nets carry pin *names* (``U1.VCC``) and
     ``is_power`` flags, so pin-behaviour classification is exact.
  2. ``<base>.kicad_pcb``      — fallback: footprints + pads + net *names* only
     (pad numbers, not pin names), so classification leans on net-name signatures
     and pin counts.

Everything that decides a class, a score, or a constraint lives in
``config/pcb_constraint_model.json``. There are **no component-name lists and no
fixed coordinates** in this file — signatures are patterns, so an unseen part
classifies by how its pins behave. Universal NE555 → EV BMS.

Run standalone:
    python -m envil_agent.tools.pcb_reasoning  path/to/board.kicad_pcb
    python tools/pcb_reasoning.py  ../ne555_blinker/ne555_blinker.envil-ir.json
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "pcb_constraint_model.json"


def _load_cfg() -> Dict[str, Any]:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Component / net model (source-agnostic)
# --------------------------------------------------------------------------- #

class Comp:
    """One component as the reasoning engine sees it."""
    __slots__ = ("ref", "prefix", "lib_id", "value", "footprint",
                 "pins", "nets", "characters", "role", "score", "constraints",
                 "confidence", "role_reason")

    def __init__(self, ref: str) -> None:
        self.ref = ref
        self.prefix = _prefix_of(ref)
        self.lib_id = ""
        self.value = ""
        self.footprint = ""
        self.pins: List[str] = []          # pin NAMES (or numbers if that's all we have)
        self.nets: List[str] = []          # net NAMES this component touches
        self.characters: set = set()       # electrical character labels
        self.role: str = "passive"
        self.score: float = 0.0
        self.constraints: Dict[str, Any] = {}
        self.confidence: float = 0.0       # how sure the role decision is (0..1)
        self.role_reason: List[str] = []   # human-readable signals behind the role


def _prefix_of(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return m.group(1).upper() if m else ref.upper()


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #

def _load_from_ir(path: Path) -> Tuple[Dict[str, Comp], Dict[str, bool]]:
    """Parse ``.envil-ir.json``. Returns (comps_by_ref, power_net_names)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    ir = raw.get("ir", raw)
    comps: Dict[str, Comp] = {}
    for c in ir.get("components", []) or []:
        ref = c.get("ref")
        if not ref:
            continue
        comp = Comp(ref)
        comp.lib_id = c.get("lib_id", "") or ""
        comp.value = c.get("value", "") or ""
        comp.footprint = c.get("footprint", "") or ""
        comps[ref] = comp

    power_nets: Dict[str, bool] = {}
    for net in ir.get("nets", []) or []:
        name = net.get("name", "") or ""
        is_pwr = bool(net.get("is_power"))
        power_nets[name] = is_pwr
        for pin in net.get("pins", []) or []:
            # pin is "REF.PINNAME" (or "REF.NUM")
            ref, _, pinname = pin.partition(".")
            comp = comps.get(ref)
            if comp is None:
                # net references a part not in components (e.g. power flag) — skip
                continue
            if pinname:
                comp.pins.append(pinname)
            comp.nets.append(name)
    return comps, power_nets


def _load_from_pcb(path: Path, cfg: Dict[str, Any]) -> Tuple[Dict[str, Comp], Dict[str, bool]]:
    """Fallback: parse a ``.kicad_pcb`` with sexpdata. Pins are pad numbers; net
    names carry the semantics. Power nets are detected from the config power/ground
    signatures (no hardcoded net-name list)."""
    import sexpdata

    power_or_gnd = (cfg.get("pin_signatures", {}).get("power", [])
                    + cfg.get("pin_signatures", {}).get("ground", []))

    def head(n: Any) -> Optional[str]:
        if isinstance(n, list) and n:
            f = n[0]
            return f.value() if isinstance(f, sexpdata.Symbol) else (f if isinstance(f, str) else None)
        return None

    def prop(fp: list, key: str) -> str:
        for ch in fp[1:]:
            if isinstance(ch, list) and head(ch) == "property" and len(ch) >= 3 and str(ch[1]) == key:
                return str(ch[2])
        return ""

    root = sexpdata.loads(path.read_text(encoding="utf-8"))
    comps: Dict[str, Comp] = {}
    power_nets: Dict[str, bool] = {}
    for fp in root[1:] if isinstance(root, list) else []:
        if not (isinstance(fp, list) and head(fp) == "footprint"):
            continue
        ref = prop(fp, "Reference")
        if not ref:
            continue
        comp = comps.setdefault(ref, Comp(ref))
        comp.value = prop(fp, "Value")
        for ch in fp[1:]:
            if isinstance(ch, list) and head(ch) == "pad":
                num = str(ch[1]) if len(ch) >= 2 else ""
                for sub in ch[2:]:
                    if isinstance(sub, list) and head(sub) == "net" and len(sub) >= 3:
                        netname = str(sub[2])
                        comp.pins.append(num)
                        comp.nets.append(netname)
                        if _matches_any(power_or_gnd, [netname], cfg):
                            power_nets[netname] = True
    return comps, power_nets


# --------------------------------------------------------------------------- #
# Reasoning
# --------------------------------------------------------------------------- #

def _tokens(name: str) -> List[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", name.upper()) if t]


def _matches(pat: str, names: List[str], cfg: Dict[str, Any]) -> bool:
    """Token-aware match: avoids 'IN' matching inside 'TIMING'. A pattern hits a
    name if it is a whole token, a token prefix (>= token_prefix_min chars), or a
    substring of the full name (>= substring_min chars). Thresholds come from
    config.match (the matcher's only algorithm constants, no component knowledge)."""
    m = cfg.get("match", {})
    pmin = int(m.get("token_prefix_min", 3))
    smin = int(m.get("substring_min", 4))
    P = pat.upper()
    for nm in names:
        H = nm.upper()
        toks = _tokens(H)
        if P in toks:
            return True
        if len(P) >= pmin and any(t.startswith(P) for t in toks):
            return True
        if len(P) >= smin and P in H:
            return True
    return False


def _matches_any(patterns: List[str], names: List[str], cfg: Dict[str, Any]) -> bool:
    return any(_matches(p, names, cfg) for p in patterns)


def _is_ground_name(name: str, cfg: Dict[str, Any]) -> bool:
    gnd = cfg.get("pin_signatures", {}).get("ground", [])
    return _matches_any(gnd, [name], cfg)


def _characters_for(comp: Comp, cfg: Dict[str, Any],
                    power_nets: Dict[str, bool]) -> set:
    """Accumulate electrical-character labels from pin + net name signatures."""
    chars: set = set()
    pin_sigs: Dict[str, List[str]] = cfg.get("pin_signatures", {})
    net_sigs: Dict[str, List[str]] = cfg.get("net_signatures", {})
    pin_only: set = set(cfg.get("pin_only_chars", []))
    haystacks = [p for p in comp.pins] + [n for n in comp.nets]
    for label, patterns in pin_sigs.items():
        # pin-only characters (e.g. power_switch) must come from the part's OWN
        # pins, not from a net name it merely touches (the buck 'SW' node).
        hs = comp.pins if label in pin_only else haystacks
        if _matches_any(patterns, hs, cfg):
            chars.add(label)
    for label, patterns in net_sigs.items():
        if _matches_any(patterns, comp.nets, cfg):
            chars.add(label)
    # A net flagged is_power confers the 'power' character — but GND is also
    # is_power in KiCad, so EXCLUDE ground-named nets (else every grounded part
    # looks like a supply part and 2-pin caps mis-read as decoupling).
    for n in comp.nets:
        if power_nets.get(n) and not _is_ground_name(n, cfg):
            chars.add("power")
            break
    return chars


def _two_pin_across(comp: Comp, a: str, b: str) -> bool:
    """True if a ~2-pin part bridges character class a and class b."""
    return len(set(comp.nets)) == 2 and a in comp.characters and b in comp.characters


def _lib_role(comp: Comp, cfg: Dict[str, Any]) -> Optional[str]:
    """Device class read from the lib_id category (legitimate device-class
    signal, like the refdes prefix or the footprint package). Matches the
    library part AND the symbol part with the token-aware matcher so 'L'
    (inductor) does not also catch 'LED'."""
    if not cfg.get("use_lib_class_hints", True):
        return None
    lib = comp.lib_id or ""
    if not lib:
        return None
    parts = lib.split(":")
    names = [parts[0], parts[-1]] if len(parts) > 1 else [parts[0]]
    for role, pats in cfg.get("lib_class_hints", {}).items():
        if any(_matches(str(p), names, cfg) for p in pats):
            return role
    return None


def _is_power_pkg(comp: Comp, cfg: Dict[str, Any]) -> bool:
    if not cfg.get("use_package_tier", True):
        return False
    fp = (comp.footprint or "").upper()
    return any(str(p).upper() in fp for p in cfg.get("power_packages", []))


def _conf_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return cfg.get("role_confidence", {}) or {}


def _set_conf(comp: Comp, cfg: Dict[str, Any], base_key: str,
              default: float, *reasons: str) -> None:
    """Record confidence + reason on the comp. Base value comes from
    config.role_confidence[base_key]; a small corroboration bonus is added per
    matching electrical character (capped) so a role backed by several agreeing
    signals reads as more certain than one backed by a single pattern. No-op on
    confidence when the block is disabled (reasons still recorded, cheap)."""
    cc = _conf_cfg(cfg)
    comp.role_reason = list(reasons)
    if not cc.get("enabled", True):
        return
    base = float(cc.get(base_key, default))
    per_char = float(cc.get("corroboration_per_char", 0.0))
    max_bonus = float(cc.get("corroboration_max_bonus", 0.0))
    cap = float(cc.get("max_confidence", 0.99))
    bonus = min(max_bonus, per_char * max(0, len(comp.characters)))
    comp.confidence = round(min(cap, base + bonus), 3)
    if comp.characters:
        comp.role_reason.append("signals: " + ", ".join(sorted(comp.characters)))


def _role_for(comp: Comp, cfg: Dict[str, Any]) -> Tuple[str, bool]:
    """Return (role, protected). A 'protected' role (lib-id MCU) is never demoted
    by the controller->peripheral post-pass. Side effect: sets comp.confidence +
    comp.role_reason for the branch that decided the role."""
    # 1. Device class from lib_id wins (a TVS is a TVS, an inductor an inductor).
    lr = _lib_role(comp, cfg)
    if lr == "mosfet":
        if _is_power_pkg(comp, cfg):
            _set_conf(comp, cfg, "lib_class_hint", 0.95,
                      "lib_class:mosfet", "power package")
            return ("power_switch", False)
        _set_conf(comp, cfg, "lib_class_hint", 0.95, "lib_class:mosfet")
        return ("small_signal_switch", False)
    if lr == "controller":
        _set_conf(comp, cfg, "lib_class_hint", 0.95, "lib_class:controller")
        return ("controller", True)
    if lr:
        _set_conf(comp, cfg, "lib_class_hint", 0.95, f"lib_class:{lr}")
        return (lr, False)
    # 2. Pin-behaviour role rules.
    for rule in cfg.get("role_rules", []):
        if "prefix" in rule and comp.prefix not in [p.upper() for p in rule["prefix"]]:
            continue
        if "pins_min" in rule and len(comp.pins) < rule["pins_min"]:
            continue
        if "pins_max" in rule and len(comp.pins) > rule["pins_max"]:
            continue
        if "has" in rule and not all(c in comp.characters for c in rule["has"]):
            continue
        if "any" in rule and not any(c in comp.characters for c in rule["any"]):
            continue
        if "footprint_any" in rule:
            fp = (comp.footprint or "").upper()
            if not any(str(p).upper() in fp for p in rule["footprint_any"]):
                continue
        if "two_pin_across" in rule:
            a, b = rule["two_pin_across"]
            if not _two_pin_across(comp, a, b):
                continue
        if "two_pin_across_any" in rule:
            if not any(_two_pin_across(comp, a, b) for a, b in rule["two_pin_across_any"]):
                continue
        _set_conf(comp, cfg, "pin_behaviour_rule", 0.8,
                  f"pin_rule:{rule['role']}")
        return (rule["role"], False)
    _set_conf(comp, cfg, "fallback_passive", 0.3, "fallback:no signature matched")
    return ("passive", False)


def _score(comp: Comp, cfg: Dict[str, Any]) -> float:
    # Weights and the default criticality come from config; the literal fallbacks
    # here are neutral (0 = no contribution, 1.0 = "present" indicator), never
    # component knowledge.
    w = cfg.get("score_weights", {})
    crit = cfg.get("criticality_class", {}).get(
        comp.role, cfg.get("default_criticality", 1))
    power_level = 1.0 if "power" in comp.characters else 0.0
    return (w.get("connection_count", 0.0) * len(set(comp.nets))
            + w.get("pin_count", 0.0) * len(comp.pins)
            + w.get("criticality", 0.0) * crit
            + w.get("power_level", 0.0) * power_level)


def _emergent_domains(comps: List[Comp], cfg: Dict[str, Any]) -> List[str]:
    roles = {c.role for c in comps}
    chars = set().union(*[c.characters for c in comps]) if comps else set()
    out: List[str] = []
    fallback: Optional[str] = None
    for name, trig in cfg.get("emergent_domains", {}).items():
        if trig.get("fallback"):
            fallback = name
            continue
        ok = True
        if "any_role" in trig:
            ok = ok and bool(roles & set(trig["any_role"]))
        if "any_char" in trig:
            ok = ok and bool(chars & set(trig["any_char"]))
        if "any_net" in trig:
            ok = ok and bool(chars & set(trig["any_net"]))   # net-sig labels live in chars
        if ok:
            out.append(name)
    if not out and fallback:
        out.append(fallback)
    return out


def _recommend_layers(comp_list: List[Comp], domains: List[str],
                      cfg: Dict[str, Any]) -> Optional[int]:
    """R0b: 2 vs 4 layer decision — DYNAMIC, from board signals only (part count,
    high-speed characters, emergent domains), never a component list. 4-layer when
    the board needs a solid reference plane (high-speed) or is dense; else 2."""
    lr = cfg.get("layer_recommendation", {})
    if not isinstance(lr, dict) or not lr:
        return None
    if len(comp_list) >= int(lr.get("parts_for_4layer", 60)):
        return 4
    chars = set().union(*[c.characters for c in comp_list]) if comp_list else set()
    if chars & set(lr.get("chars_force_4layer", [])):
        return 4
    if set(domains) & set(lr.get("domains_force_4layer", [])):
        return 4
    return 2


def analyze(path: Path) -> Dict[str, Any]:
    """Run the electrical-reasoning pass. Returns the constraint report dict."""
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"ok": False, "reason": "pcb_constraint_model disabled"}

    suffix = path.suffix.lower()
    if path.name.endswith(".envil-ir.json"):
        comps, power_nets = _load_from_ir(path)
        source = "ir"
    elif suffix == ".kicad_pcb":
        ir_side = path.with_suffix(".envil-ir.json")
        if ir_side.exists():
            comps, power_nets = _load_from_ir(ir_side)
            source = "ir(sidecar)"
        else:
            comps, power_nets = _load_from_pcb(path, cfg)
            source = "pcb"
    elif suffix == ".json":
        comps, power_nets = _load_from_ir(path)
        source = "ir"
    else:
        return {"ok": False, "reason": f"unsupported input: {path.name}"}

    comp_list = list(comps.values())
    if not comp_list:
        return {"ok": False, "reason": "no components found"}

    # Stage 3: classify + characters
    protected: Dict[str, bool] = {}
    for c in comp_list:
        c.characters = _characters_for(c, cfg, power_nets)
        c.role, protected[c.ref] = _role_for(c, cfg)
    # Stage 5: score + anchor
    for c in comp_list:
        c.score = _score(c, cfg)
    anchor = max(comp_list, key=lambda c: c.score)
    # A peripheral IC (sensor, expander) often reads as 'controller' from pins
    # (power+ground+comm). Demote any non-anchor, unprotected controller that
    # lacks a real controller signature (config: controller_signature_chars).
    ctrl_sig = set(cfg.get("controller_signature_chars", []))
    for c in comp_list:
        if (c.role == "controller" and c.ref != anchor.ref
                and not protected.get(c.ref)
                and not (c.characters & ctrl_sig)):
            c.role = "peripheral_ic"
            cc = _conf_cfg(cfg)
            if cc.get("enabled", True):
                c.confidence = round(float(cc.get("controller_demoted", 0.5)), 3)
            c.role_reason = (["demoted: non-anchor controller lacking a "
                              "controller signature"] + c.role_reason)
    # Confidence feedback loop: fold in the learned per-lib_id bias so a part
    # whose role has repeatedly caused ROUTING conflicts on past boards reads less
    # certain now (adaptive reasoning). Gated + empty-store-safe -> byte-stable
    # until the loop has actually learned something. Never blocks analysis.
    try:
        from ..intent import confidence_feedback as _cf
        if _cf.is_enabled():
            _floor = float((_cf.load_cfg().get("attribution", {}) or {}).get("floor", 0.1))
            _cap = float((cfg.get("role_confidence", {}) or {}).get("max_confidence", 0.99))
            for c in comp_list:
                b = _cf.learned_bias(c.lib_id)
                if b:
                    c.confidence = round(min(_cap, max(_floor, c.confidence + b)), 3)
                    c.role_reason.append(f"learned bias {b:+.2f} (prior routing conflicts)")
    except Exception:                                       # noqa: BLE001
        pass

    # constraints per role
    role_constraints = cfg.get("constraints", {})
    merge_chars = set(cfg.get("constraint_merge_chars", []))
    for c in comp_list:
        cons = dict(role_constraints.get(c.role, {}))
        # Only isolation/routing characters add constraints on top of the role —
        # NOT power/power_switch (those are role-decided, footprint-tiered), so a
        # small-signal FET never inherits high-current width/thermal.
        for ch in (c.characters & merge_chars):
            if ch in role_constraints:
                for k, v in role_constraints[ch].items():
                    cons.setdefault(k, v)
        if c.ref == anchor.ref:
            cons["is_anchor"] = True
        c.constraints = cons

    domains = _emergent_domains(comp_list, cfg)

    cc = _conf_cfg(cfg)
    emit_conf = bool(cc.get("enabled", True))
    low_thresh = float(cc.get("low_confidence_threshold", 0.5))

    def _comp_entry(c: Comp) -> Dict[str, Any]:
        e = {
            "ref": c.ref,
            "role": c.role,
            "characters": sorted(c.characters),
            "pins": len(c.pins),
            "nets": sorted(set(c.nets)),
            "score": round(c.score, 1),
            "constraints": c.constraints,
        }
        # Additive + gated: only emit the confidence fields when the block is on,
        # so an existing project's sidecar stays byte-identical with it off.
        if emit_conf:
            e["confidence"] = c.confidence
            e["role_reason"] = c.role_reason
            e["lib_id"] = c.lib_id   # stable key the feedback loop persists by
        return e

    report = {
        "ok": True,
        "source": source,
        "anchor": anchor.ref,
        "emergent_domains": domains,
        "recommended_layers": _recommend_layers(comp_list, domains, cfg),
        "components": [_comp_entry(c)
                       for c in sorted(comp_list, key=lambda c: -c.score)],
    }
    # Surface the parts the engine is unsure about so a placer/router (or the
    # user) can treat their role as a guess rather than fact.
    if emit_conf:
        report["low_confidence"] = [
            {"ref": c.ref, "role": c.role, "confidence": c.confidence,
             "reason": c.role_reason}
            for c in sorted(comp_list, key=lambda c: c.confidence)
            if c.confidence < low_thresh
        ]
    return report


def write_sidecar(path: Path, report: Dict[str, Any]) -> Path:
    base = path
    for suf in (".envil-ir.json", ".kicad_pcb", ".json"):
        if path.name.endswith(suf):
            base = Path(str(path)[: -len(suf)])
            break
    out = base.with_name(base.name + ".envil-constraints.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_report(report: Dict[str, Any]) -> None:
    if not report.get("ok"):
        print("reasoning:", report.get("reason"))
        return
    print(f"source={report['source']}  anchor={report['anchor']}  "
          f"domains={', '.join(report['emergent_domains']) or '-'}")
    print(f"{'REF':<6}{'ROLE':<14}{'SCORE':>6}{'CONF':>6}  CHARACTERS / CONSTRAINTS")
    for c in report["components"]:
        cons = ", ".join(f"{k}={v}" for k, v in c["constraints"].items()
                         if k not in ("is_anchor",))
        anchor = "  <-- ANCHOR" if c["constraints"].get("is_anchor") else ""
        conf = c.get("confidence")
        conf_s = f"{conf:>6.2f}" if isinstance(conf, (int, float)) else " " * 6
        print(f"{c['ref']:<6}{c['role']:<14}{c['score']:>6.0f}{conf_s}  "
              f"[{', '.join(c['characters'])}]{anchor}")
        if cons:
            print(f"        {cons}")
    low = report.get("low_confidence") or []
    if low:
        print(f"\nLOW CONFIDENCE ({len(low)}) — role is a guess, verify before trusting:")
        for c in low:
            print(f"  {c['ref']:<6}{c['role']:<14}conf={c['confidence']:.2f}  "
                  f"({'; '.join(c['reason'])})")


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    path = Path(argv[0]).expanduser()
    if not path.exists():
        print(f"ERROR: not found: {path}")
        return 1
    report = analyze(path)
    _print_report(report)
    if report.get("ok") and "--no-write" not in argv:
        out = write_sidecar(path, report)
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
