"""Offline verification of the Phase 2 verified-block expansion mechanism
(intent/verified_blocks.py). No LLM, no render. Run:

    python tests/_verify_verified_blocks.py     # exit 0 = all pass

Proves: the real INA240 template injects Vs decoupling onto the EXISTING rails
(no new rail, no short); a multi-part template's internal nets are namespaced by
anchor refdes so two instances never bond; skip_if avoids double-injection;
refdes allocation is collision-free; and the pipeline gate is OFF by default.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent, IRNet  # noqa: E402
from envil_agent.intent import verified_blocks as VB              # noqa: E402

# A synthetic multi-part template with a genuine INTERNAL node (MID between r1
# and c1) — exercises namespacing + multi-part injection + skip, independent of
# any real symbol library (anchor lib_id "X:FAKEIC*").
_SYNTH = {
    "enabled": True, "block_id": "synth",
    "anchor": {"lib_id_patterns": ["*:FAKEIC*"], "anchor_role": "anchor"},
    "support_parts": [
        {"role": "r1", "ref_prefix": "R", "lib_id": "Device:R", "value": "1k"},
        {"role": "c1", "ref_prefix": "C", "lib_id": "Device:C", "value": "100n"},
    ],
    "internal_nets": [{"name": "MID", "pins": ["r1.2", "c1.1"]}],
    "power_attach": [
        {"support": "r1.1", "follow_anchor_pin": "OUT", "fallback_rail_pattern": "+3V3"},
        {"support": "c1.2", "follow_anchor_pin": "GND", "fallback_rail_role": "GND"},
    ],
    "skip_if": {"any_part_bridges": [{"pin_a": "OUT", "pin_b": "GND", "prefix": "C"}]},
}


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    _orig = VB._load_templates

    # === 1. Real INA240 template: Vs decap onto EXISTING V+/GND rails ===
    ina = json.loads(
        (Path(VB.__file__).resolve().parent.parent / "config" /
         "verified_blocks" / "ina240_current_sense.json").read_text(encoding="utf-8"))
    VB._load_templates = lambda: (ina,)
    ir = TopologyIR(name="t", circuit_type="X", components=[
        IRComponent("U7", "Amplifier_Current:INA240A1", "INA240A1")],
        nets=[IRNet("+5V", ["U7.V+"], is_power=True),
              IRNet("GND", ["U7.GND"], is_power=True),
              IRNet("ISENSE", ["U7.5", "U1.PA0"], is_power=False)])  # OUT->ADC boundary
    n_nets_before = len(ir.nets)
    added = VB.expand_verified_blocks(ir)
    new_caps = [r for r in added if r.startswith("C")]
    v5 = next(n for n in ir.nets if n.name == "+5V")
    gnd = next(n for n in ir.nets if n.name == "GND")
    isense = next(n for n in ir.nets if n.name == "ISENSE")
    check("INA240: one Vs decap injected onto existing +5V/GND, no new rail",
          len(new_caps) == 1,
          any(p == f"{new_caps[0]}.1" for p in v5.pins),
          any(p == f"{new_caps[0]}.2" for p in gnd.pins),
          len(ir.nets) == n_nets_before,              # no new net created
          isense.pins == ["U7.5", "U1.PA0"])          # boundary untouched

    # === 1b. idempotency: re-running on the SAME ir adds nothing more ===
    VB._load_templates = lambda: (ina,)
    ir = TopologyIR(name="t", circuit_type="X", components=[
        IRComponent("U7", "Amplifier_Current:INA240A1", "INA240A1")],
        nets=[IRNet("+5V", ["U7.V+"], is_power=True),
              IRNet("GND", ["U7.GND"], is_power=True)])
    a1 = VB.expand_verified_blocks(ir)
    a2 = VB.expand_verified_blocks(ir)            # re-run on the same object
    check("idempotent: re-run adds nothing (no decap accumulation)",
          len(a1) == 1, a2 == [],
          sum(1 for c in ir.components if c.ref.startswith("C")) == 1)

    # === 1c. unnamed OUT pin (name '' / '~') never yields a bare/'~' token ===
    ir = TopologyIR(name="t", circuit_type="X", components=[
        IRComponent("U7", "Amplifier_Current:INA240A1", "INA240A1")], nets=[])
    toks = VB._pin_tokens(ir, "U7", "5")          # OUT is pin 5, unnamed
    check("unnamed OUT pin -> token set is number-only (no '~' / '')",
          "U7.5" in toks, "U7.~" not in toks, "U7." not in toks)

    # === 2. Namespacing: two FAKEIC instances -> internal nets DON'T bond ===
    VB._load_templates = lambda: (_SYNTH,)
    ir = TopologyIR(name="t", circuit_type="X", components=[
        IRComponent("U7", "X:FAKEIC", "F"), IRComponent("U9", "X:FAKEIC", "F")],
        nets=[])
    VB.expand_verified_blocks(ir)
    mids = sorted(n.name for n in ir.nets if n.name.endswith("_MID"))
    check("two instances -> distinct namespaced internal nets, no bare 'MID'",
          mids == ["U7_MID", "U9_MID"],
          not any(n.name == "MID" for n in ir.nets),
          # the two MID nets share NO pin (no silent bond)
          set(next(n.pins for n in ir.nets if n.name == "U7_MID")).isdisjoint(
              next(n.pins for n in ir.nets if n.name == "U9_MID")))

    # === 3. skip_if: a cap already bridges OUT/GND -> skip (no duplicate) ===
    VB._load_templates = lambda: (_SYNTH,)
    ir = TopologyIR(name="t", circuit_type="X", components=[
        IRComponent("U1", "X:FAKEIC", "F"), IRComponent("C1", "Device:C", "100n")],
        nets=[IRNet("OUTN", ["U1.OUT", "C1.1"]),
              IRNet("GND", ["C1.2", "U1.GND"], is_power=True)])
    n_comp_before = len(ir.components)
    added = VB.expand_verified_blocks(ir)
    check("skip_if: cap already bridges OUT/GND -> no expansion",
          added == [], len(ir.components) == n_comp_before)

    # === 4. refdes allocation is collision-free ===
    VB._load_templates = lambda: (_SYNTH,)
    ir = TopologyIR(name="t", circuit_type="X", components=[
        IRComponent("U1", "X:FAKEIC", "F"),
        IRComponent("R1", "Device:R", "1k"), IRComponent("R2", "Device:R", "1k"),
        IRComponent("C1", "Device:C", "1u"), IRComponent("C2", "Device:C", "1u"),
        IRComponent("C3", "Device:C", "1u")], nets=[])
    added = VB.expand_verified_blocks(ir)
    check("refdes allocation avoids existing R1-2 / C1-3",
          "R3" in added, "C4" in added,
          len(set(c.ref for c in ir.components)) == len(ir.components))  # all unique

    VB._load_templates = _orig  # restore

    # === 5. pipeline gate is OFF by default (byte-stable) ===
    cfg = json.loads(
        (Path(VB.__file__).resolve().parent.parent / "config" /
         "layout_config.json").read_text(encoding="utf-8"))
    check("expand_verified_blocks defaults OFF in config (byte-stable)",
          cfg.get("normalize", {}).get("expand_verified_blocks", False) is False)

    ok = True
    for name, passed, conds in results:
        ok = ok and passed
        print(("PASS" if passed else "FAIL"), name)
        if not passed:
            print("   conds:", [bool(c) for c in conds])
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
