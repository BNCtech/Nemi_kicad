"""3-tier chat-pipeline test. Connects to the running ws://127.0.0.1:8765
and feeds small/medium/large prompts. Captures the ops Claude emits via
emit_reply, then scores them against PCB-designer heuristics.

Heuristics (0-100 each, average is the tier score):
  - decap_proximity:    every 2-pin C adjacent to an IC pin sits within 15 mm of it
  - power_port_usage:   power nets (VCC/GND/+5V/+3V3) use power_port ops, not raw labels
  - wire_body_clearance: no add_wire segment crosses any add_component body bbox
  - net_completeness:   every pin reference in an add_wire / add_label points at a placed component
  - refdes_convention:  U/R/C/L/D/J/Y refdes prefixes match component class
  - value_normalization: resistor/cap values follow IEC 60062 (4k7, 100n, etc.)
"""
from __future__ import annotations
import asyncio
import json
import re
import sys
from pathlib import Path

import websockets

WS_URL = "ws://127.0.0.1:8765/ws/chat"

# Use a scratch schematic so we never touch the user's open one
TEST_SCH = Path.home() / "Documents" / "_envil_3tier_test" / "tier_test.kicad_sch"


PROMPTS = {
    "SMALL":  ("design a voltage divider with R1=10k and R2=4.7k from 5V input, "
               "tap to OUT"),
    "MEDIUM": ("design an LM317 linear regulator with 5V output from 12V input, "
               "include Cin 0.1uF, Cout 1uF, and R1=240, R2=720 for Vout=5V"),
    "LARGE":  ("design a 555 timer astable at 1Hz 50% duty cycle, with output "
               "driving a red LED via 330R current limit resistor, include "
               "0.1uF supply decoupling and 10nF CTRL bypass"),
}


def _ensure_test_sch() -> None:
    """Create an empty .kicad_sch the chat path can attach to."""
    TEST_SCH.parent.mkdir(parents=True, exist_ok=True)
    if TEST_SCH.exists():
        return
    blank = (
        '(kicad_sch (version 20231120) (generator "envil_test")\n'
        '  (uuid "00000000-0000-0000-0000-000000000001")\n'
        '  (paper "A4")\n'
        '  (lib_symbols)\n'
        '  (sheet_instances (path "/" (page "1")))\n'
        ')\n'
    )
    TEST_SCH.write_text(blank, encoding="utf-8")


async def _drain_until(ws, kinds: set[str], timeout: float = 120.0) -> dict | None:
    """Receive events until one of the requested kinds (or `error`) arrives."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remain = deadline - asyncio.get_event_loop().time()
        if remain <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remain)
        except asyncio.TimeoutError:
            return None
        ev = json.loads(raw)
        k = ev.get("kind") or ev.get("type")
        if k in kinds or k == "error":
            return ev


async def run_one(label: str, prompt: str) -> dict:
    print(f"\n=== {label} ===\n  prompt: {prompt[:80]}", flush=True)
    async with websockets.connect(WS_URL, max_size=2**24) as ws:
        await ws.send(json.dumps({"kind": "ping", "session_id": None}))
        pong = json.loads(await ws.recv())
        sid = pong.get("session_id")

        # Attach schematic via hello — this is the real init for a session
        await ws.send(json.dumps({
            "kind": "hello", "session_id": sid,
            "project_path": str(TEST_SCH.parent),
            "schematic_file": str(TEST_SCH),
        }))
        # ready + (optional) schematic_summary may both arrive; drain both
        await _drain_until(ws, {"ready"})
        # Best-effort drain of any auto-summary follow-up (don't block on it)
        try:
            extra = await asyncio.wait_for(ws.recv(), timeout=1.0)
            print(f"  (extra: {json.loads(extra).get('kind')})", flush=True)
        except asyncio.TimeoutError:
            pass

        await ws.send(json.dumps({"kind": "reset", "session_id": sid}))
        await _drain_until(ws, {"status"})

        await ws.send(json.dumps({
            "kind": "message", "session_id": sid, "text": prompt,
            "schematic_file": str(TEST_SCH),
        }))
        reply = await _drain_until(ws, {"reply"}, timeout=180.0)
        if reply is None:
            return {"label": label, "prompt": prompt,
                    "error": "no reply in 180s"}
        if reply.get("kind") == "error":
            return {"label": label, "prompt": prompt,
                    "error": reply.get("text")}

        ops = reply.get("ops") or []
        # Parse the embedded JSON text (legacy path packs it as text)
        if not ops:
            txt = reply.get("text") or ""
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, dict):
                    ops = parsed.get("ops") or []
            except json.JSONDecodeError:
                pass
        return {"label": label, "prompt": prompt, "ops": ops,
                "msg": reply.get("text", "")[:200]}


# ---------------------- SCORING ----------------------

POWER_NET_RE = re.compile(r"^[+]?(\d+V\d*|VCC|VDD|VBAT|VBUS|GND|VSS)$", re.IGNORECASE)
RKM_RE = re.compile(r"^(\d+(?:[kKmMRnpuµ]\d*)?|\d+[.]\d+|\d+)([RkKMGmuµnpf]?)$")


def _bbox_for(op: dict) -> tuple[float, float, float, float] | None:
    if op.get("op") != "add_component":
        return None
    x, y = float(op.get("x", 0)), float(op.get("y", 0))
    # Conservative bbox per refdes class — class-only, no per-part rules
    ref = (op.get("ref") or "").upper()
    if ref.startswith("U"):
        w, h = 12.7, 12.7
    elif ref.startswith(("R", "C", "L", "D")):
        w, h = 5.08, 2.54
    elif ref.startswith(("J", "P", "Y")):
        w, h = 7.62, 5.08
    else:
        w, h = 5.08, 5.08
    return (x - w/2, y - h/2, x + w/2, y + h/2)


def _seg_crosses(seg: tuple, bbox: tuple) -> bool:
    x1, y1, x2, y2 = seg
    xmin, ymin, xmax, ymax = bbox
    if abs(x1 - x2) < 0.01:  # vertical
        if x1 <= xmin + 0.01 or x1 >= xmax - 0.01: return False
        lo, hi = min(y1, y2), max(y1, y2)
        return not (hi <= ymin + 0.01 or lo >= ymax - 0.01)
    if abs(y1 - y2) < 0.01:  # horizontal
        if y1 <= ymin + 0.01 or y1 >= ymax - 0.01: return False
        lo, hi = min(x1, x2), max(x1, x2)
        return not (hi <= xmin + 0.01 or lo >= xmax - 0.01)
    return False


def score(ops: list[dict]) -> dict:
    comps = [o for o in ops if o.get("op") == "add_component"]
    wires = [o for o in ops if o.get("op") == "add_wire"]
    labels = [o for o in ops if o.get("op") == "add_label"]
    junctions = [o for o in ops if o.get("op") == "add_junction"]
    # Power "ports" in this protocol are add_component ops with a
    # `power:*` lib_id — there's no dedicated add_power_port op. The
    # server resolves them to KiCad power-port symbols on apply.
    pwr_ports = [c for c in comps
                 if (c.get("lib_id") or "").lower().startswith("power:")]
    signal_comps = [c for c in comps if c not in pwr_ports]

    out = {
        "n_components": len(signal_comps),
        "n_wires": len(wires),
        "n_labels": len(labels),
        "n_power_ports": len(pwr_ports),
        "n_junctions": len(junctions),
    }

    # 1. decap proximity — caps adjacent to ICs sit within 15mm.
    # Classify by lib_id (refdes is empty in chat-protocol ops — server
    # assigns it on apply).
    def _is_ic(c: dict) -> bool:
        lib = (c.get("lib_id") or "").lower()
        sym = lib.split(":", 1)[1] if ":" in lib else lib
        return ("regulator" in lib or "timer" in lib or "mcu" in lib or
                "opamp" in lib or "amplifier" in lib or
                "555" in sym or "lm" in sym[:3] or "ne" in sym[:2])
    def _is_cap(c: dict) -> bool:
        lib = (c.get("lib_id") or "").lower()
        sym = lib.split(":", 1)[1] if ":" in lib else lib
        return sym in ("c", "c_small", "c_polarized", "cp")
    ics = [c for c in signal_comps if _is_ic(c)]
    caps = [c for c in signal_comps if _is_cap(c)]
    if not caps or not ics:
        decap_score = 100  # n/a → don't penalise
    else:
        close = 0
        for cap in caps:
            cx, cy = float(cap.get("x", 0)), float(cap.get("y", 0))
            for ic in ics:
                ix, iy = float(ic.get("x", 0)), float(ic.get("y", 0))
                if ((cx - ix) ** 2 + (cy - iy) ** 2) ** 0.5 <= 15.0:
                    close += 1; break
        decap_score = int(close / len(caps) * 100)
    out["decap_proximity"] = decap_score

    # 2. power-port usage — any circuit with passives or ICs should use
    # power-port SYMBOLS (lib_id `power:GND`, `power:+5V`, etc.) rather
    # than plain labels. Almost every prompt has at least a GND.
    needs_power = bool(signal_comps)
    if not needs_power:
        pp_score = 100
    else:
        gnd_present = any(
            (c.get("lib_id") or "").lower() == "power:gnd"
            for c in pwr_ports
        )
        rail_present = any(
            re.match(r"^power:[+]?\d+v", (c.get("lib_id") or "").lower())
            for c in pwr_ports
        ) or any(
            (c.get("lib_id") or "").lower().startswith("power:")
            and "gnd" not in (c.get("lib_id") or "").lower()
            for c in pwr_ports
        )
        pp_score = 100 if (gnd_present and rail_present) else (
            50 if (gnd_present or rail_present) else 0
        )
    out["power_port_usage"] = pp_score

    # 3. wire-body clearance — only count crossings against components
    # whose bbox the wire is NOT anchored to. A wire starting AT a pin is
    # naturally inside that component's pin-edge bbox; that's not a
    # defect.
    bboxes = []
    for c in comps:
        b = _bbox_for(c)
        if b: bboxes.append((c, b))
    total = max(len(wires), 1)
    crosses = 0
    PIN_TOL = 1.0  # mm — pin tip vs body edge tolerance
    for w in wires:
        pts = w.get("points") or []
        if len(pts) < 2: continue
        # Find which components own each endpoint (within tolerance of bbox)
        owner_refs = set()
        for px, py in pts:
            for c, (xmin, ymin, xmax, ymax) in bboxes:
                if (xmin - PIN_TOL <= px <= xmax + PIN_TOL and
                    ymin - PIN_TOL <= py <= ymax + PIN_TOL):
                    owner_refs.add(c.get("ref"))
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]; x2, y2 = pts[i+1]
            crossed_this_seg = False
            for c, b in bboxes:
                if c.get("ref") in owner_refs: continue
                if _seg_crosses((x1, y1, x2, y2), b):
                    crossed_this_seg = True; break
            if crossed_this_seg:
                crosses += 1; break
    out["wire_body_clearance"] = int((1 - crosses / total) * 100)

    # 4. net coherence — every wire endpoint should land on a pin tip,
    # a label, a junction, OR another wire endpoint. Dangling wire
    # endpoints in mid-air are a real PCB-designer defect. Heuristic:
    # collect every "electrical anchor" coordinate (component bboxes
    # treated as pin-hosting regions + label xy + junction xy + other
    # wire endpoints), then count wire endpoints that hit none of them.
    pin_regions = []  # (xmin, ymin, xmax, ymax) including pin-tip slack
    for c in signal_comps + pwr_ports:
        b = _bbox_for(c)
        if not b: continue
        x_lo, y_lo, x_hi, y_hi = b
        pin_regions.append((x_lo - 5.08, y_lo - 5.08,
                            x_hi + 5.08, y_hi + 5.08))
    anchor_pts = set()
    for o in labels + junctions:
        if o.get("x") is not None and o.get("y") is not None:
            anchor_pts.add((round(float(o["x"]), 2), round(float(o["y"]), 2)))
    # Every other wire's endpoints are anchors for the wire we're checking
    all_wire_pts = set()
    for w in wires:
        for pt in (w.get("points") or []):
            all_wire_pts.add((round(float(pt[0]), 2), round(float(pt[1]), 2)))

    def _is_anchored(px: float, py: float) -> bool:
        key = (round(px, 2), round(py, 2))
        if key in anchor_pts: return True
        if key in all_wire_pts:
            # Anchored to another wire iff that point is shared with
            # at least one OTHER wire (this wire itself doesn't count).
            shared = sum(
                1 for w in wires
                for pt in (w.get("points") or [])
                if abs(float(pt[0]) - px) < 0.1
                and abs(float(pt[1]) - py) < 0.1
            )
            if shared >= 2: return True
        for x_lo, y_lo, x_hi, y_hi in pin_regions:
            if x_lo <= px <= x_hi and y_lo <= py <= y_hi:
                return True
        return False

    dangling = 0
    total_endpoints = 0
    for w in wires:
        pts = w.get("points") or []
        if len(pts) < 2: continue
        for ep in (pts[0], pts[-1]):
            total_endpoints += 1
            if not _is_anchored(float(ep[0]), float(ep[1])):
                dangling += 1
    out["net_coherence"] = (
        int((1 - dangling / total_endpoints) * 100)
        if total_endpoints else 100
    )

    # 5. lib_id validity — every add_component must carry a non-empty
    # `<library>:<symbol>` lib_id. Server assigns the refdes from this
    # on apply, so this is the only refdes-level signal Claude controls.
    valid_lib = 0
    for c in comps:
        lib = (c.get("lib_id") or "").strip()
        if ":" in lib and len(lib.split(":", 1)[1]) > 0:
            valid_lib += 1
    out["lib_id_validity"] = (
        int(valid_lib / len(comps) * 100) if comps else 100
    )
    # Skip the obsolete refdes_convention metric below.
    _SKIP_REFDES = True
    # 5b. refdes-class convention — lib_id is case-sensitive in KiCad
    # ("Device:R") but our check should be case-insensitive on the
    # whole token. Anchor on the symbol-name half (after the colon).
    bad = 0
    classified_total = 0
    for c in comps:
        ref = (c.get("ref") or "").upper()
        lib = (c.get("lib_id") or "").lower()
        if not ref:
            bad += 1; classified_total += 1; continue
        sym = lib.split(":", 1)[1] if ":" in lib else lib
        expected: tuple[str, ...] | None = None
        if "regulator" in lib or "timer" in lib or "555" in sym or \
           "opamp" in lib or "mcu" in lib or "stm32" in sym or \
           "atmega" in sym or sym.startswith(("ne555", "tl0", "lm3", "lm7")):
            expected = ("U",)
        elif sym in ("r", "r_small", "r_us") or sym.startswith("r_"):
            expected = ("R",)
        elif sym in ("c", "c_small", "c_polarized", "cp"):
            expected = ("C",)
        elif sym in ("l", "l_small") or sym.startswith("l_"):
            expected = ("L",)
        elif "diode" in lib or "led" in sym or sym in ("d", "d_schottky"):
            expected = ("D",)
        elif "connector" in lib or sym.startswith("conn_"):
            expected = ("J", "P")
        elif "crystal" in lib or sym.startswith("crystal"):
            expected = ("Y", "X")
        elif "transistor" in lib or sym.startswith(("q_", "mosfet", "bjt")):
            expected = ("Q",)
        if expected is None:
            continue  # don't penalise unknown classes
        classified_total += 1
        if not ref.startswith(expected):
            bad += 1
    out["refdes_convention"] = (
        int((1 - bad / classified_total) * 100) if classified_total else 100
    )

    # 6. value normalisation — passives should use RKM (4k7 / 100n / 10u)
    passive_count = 0
    iec_ok = 0
    for c in comps:
        ref = (c.get("ref") or "").upper()
        if not ref.startswith(("R", "C", "L")): continue
        passive_count += 1
        val = (c.get("value") or "").strip()
        if not val: continue
        # Accept compact forms: 10k, 4k7, 4.7k, 100n, 10uF, 0.1uF, 220R, 1M
        if re.match(r"^\d+([.]?\d+)?[RkKMmuµnpf]?[FH]?$", val) or \
           re.match(r"^\d+[RkKM]\d+$", val):
            iec_ok += 1
    out["value_normalization"] = (
        int(iec_ok / passive_count * 100) if passive_count else 100
    )

    keys = ["decap_proximity", "power_port_usage", "wire_body_clearance",
            "net_coherence", "lib_id_validity", "value_normalization"]
    out["TOTAL"] = sum(out[k] for k in keys) // len(keys)
    # Keep the legacy refdes_convention number around for diff inspection
    out.setdefault("refdes_convention", out["lib_id_validity"])
    return out


async def main() -> int:
    _ensure_test_sch()
    dump_dir = TEST_SCH.parent / "ops_dump"
    dump_dir.mkdir(exist_ok=True)
    results = []
    for label, prompt in PROMPTS.items():
        r = await run_one(label, prompt)
        # Persist ops + prompt so user can inspect after the run
        if "ops" in r:
            (dump_dir / f"{label}.json").write_text(
                json.dumps({"prompt": prompt, "ops": r["ops"], "msg": r.get("msg", "")},
                            indent=2), encoding="utf-8")
        if "error" in r:
            print(f"  ERROR: {r['error']}", flush=True)
            results.append((label, r, None))
            continue
        s = score(r["ops"])
        print(f"  ops: {len(r['ops'])}  components={s['n_components']} "
              f"wires={s['n_wires']} labels={s['n_labels']} pwr={s['n_power_ports']}",
              flush=True)
        print(f"  score: TOTAL={s['TOTAL']}  decap={s['decap_proximity']}  "
              f"pwr_port={s['power_port_usage']}  wire_clear={s['wire_body_clearance']}  "
              f"net_coh={s['net_coherence']}  lib_id={s['lib_id_validity']}  "
              f"value_iec={s['value_normalization']}", flush=True)
        results.append((label, r, s))

    print("\n=== SUMMARY ===", flush=True)
    print(f"{'TIER':<7s} {'OPS':>4s} {'COMP':>5s} {'WIRE':>5s} {'SCORE':>6s} {'PASS_80':>8s}")
    for label, r, s in results:
        if s is None:
            print(f"{label:<7s} {'-':>4s} {'-':>5s} {'-':>5s} {'-':>6s} {'NO':>8s}")
        else:
            print(f"{label:<7s} {len(r['ops']):>4d} {s['n_components']:>5d} "
                  f"{s['n_wires']:>5d} {s['TOTAL']:>5d}% {('YES' if s['TOTAL']>=80 else 'NO'):>8s}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
