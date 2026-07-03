"""Tool: connect_stray_pads — connect a stray unconnected pad to its net.

Root cause (diagnosed 2026-07-02): route_pcb_simple / route_pcb_astar treat a
net that has ANY copper as "routed", so a single pad left out of a partially-
routed net (e.g. C3's +5V pad, stranded behind +12V copper) is never routed by
anything — DRC reports it unconnected and the board can't reach GO.

This is the missing pass. It builds a CONNECTIVITY model of each net (union-find
over pads + track endpoints + vias, joined where copper touches), finds the
net's LARGEST connected component (its main tree), and for every pad NOT in that
component routes a stub — back layer first, diagonal then L, ending in a via —
to the nearest point that IS in the main component (guaranteed real, connected
copper). Every candidate is DRC-VALIDATED: kept only if it removes an
unconnected item and adds NO short. Honest — a pad with no legal stub is
reported as needing manual routing, never faked.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "connect_stray_pads.json"
        _CFG_CACHE = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CFG_CACHE = {}
    return _CFG_CACHE


def _connectivity_strays(root: list, eps: float
                         ) -> List[Tuple[int, str, Tuple[float, float], Tuple[float, float]]]:
    """Per net, union-find its copper; return (net_idx, net_name, stray_pad_xy,
    target_xy) for every pad NOT in the net's largest component, targeting the
    nearest point in that component."""
    from ..layout.place_refine import _head, _child, _children, _rotate
    names: Dict[int, str] = {}
    net_pads: Dict[int, List[Tuple[float, float, float, bool]]] = defaultdict(list)
    net_segs: Dict[int, List[Tuple[Tuple[float, float], Tuple[float, float]]]] = defaultdict(list)
    net_vias: Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    poured: set = set()          # nets served by a copper zone (plane) — GND etc.

    for n in root[1:]:
        if not isinstance(n, list):
            continue
        h = _head(n)
        if h == "zone":
            zc = _child(n, "net")
            if zc and len(zc) >= 2:
                try:
                    poured.add(int(zc[1]))
                except (TypeError, ValueError):
                    pass
        if h == "net" and len(n) >= 3:
            try:
                names[int(n[1])] = str(n[2]).strip('"')
            except (TypeError, ValueError):
                pass
        elif h == "footprint":
            at = _child(n, "at")
            fx, fy = (float(at[1]), float(at[2])) if at and len(at) >= 3 else (0.0, 0.0)
            rot = float(at[3]) if at and len(at) > 3 else 0.0
            for pad in _children(n, "pad"):
                netc = _child(pad, "net")
                if not (netc and len(netc) >= 2):
                    continue
                try:
                    nidx = int(netc[1])
                except (TypeError, ValueError):
                    continue
                if nidx == 0:
                    continue
                pa = _child(pad, "at")
                lx, ly = (float(pa[1]), float(pa[2])) if pa and len(pa) >= 3 else (0.0, 0.0)
                dx, dy = _rotate(lx, ly, -rot)
                sz = _child(pad, "size")
                r = max(float(sz[1]), float(sz[2])) / 2.0 if sz and len(sz) >= 3 else 0.5
                lyr = _child(pad, "layers")
                lys = {str(x) for x in (lyr[1:] if lyr else [])}
                ptype = str(pad[2]) if len(pad) > 2 else ""
                tht = (any("*.Cu" in s for s in lys)
                       or _child(pad, "drill") is not None or "thru" in ptype)
                net_pads[nidx].append((fx + dx, fy + dy, r, tht))
        elif h == "segment":
            netc = _child(n, "net")
            s, e = _child(n, "start"), _child(n, "end")
            if netc and len(netc) >= 2 and s and e and len(s) >= 3 and len(e) >= 3:
                try:
                    net_segs[int(netc[1])].append(((float(s[1]), float(s[2])),
                                                   (float(e[1]), float(e[2]))))
                except (TypeError, ValueError):
                    pass
        elif h == "via":
            netc = _child(n, "net")
            a = _child(n, "at")
            if netc and len(netc) >= 2 and a and len(a) >= 3:
                try:
                    net_vias[int(netc[1])].append((float(a[1]), float(a[2])))
                except (TypeError, ValueError):
                    pass

    out: List[Tuple[int, str, Tuple[float, float], Tuple[float, float]]] = []
    for nidx, pads in net_pads.items():
        if len(pads) < 2 or nidx in poured:
            continue           # a poured net (GND plane) connects via the fill,
                               # not tracks — its pads are never "stray"
        segs = net_segs.get(nidx, [])
        vias = net_vias.get(nidx, [])
        term: List[Tuple[float, float]] = []
        pad_ix: List[int] = []
        pad_tht: List[bool] = []
        for (x, y, _r, tht) in pads:
            pad_ix.append(len(term)); pad_tht.append(bool(tht)); term.append((x, y))
        seg_ix: List[Tuple[int, int]] = []
        for (a, b) in segs:
            ia = len(term); term.append(a)
            ib = len(term); term.append(b)
            seg_ix.append((ia, ib))
        for v in vias:
            term.append(v)

        parent = list(range(len(term)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            parent[find(i)] = find(j)

        for (i, j) in seg_ix:
            union(i, j)
        # coincident copper (endpoints/vias/pads that touch)
        for i in range(len(term)):
            for j in range(i + 1, len(term)):
                if math.hypot(term[i][0] - term[j][0], term[i][1] - term[j][1]) <= eps:
                    union(i, j)
        # a terminal inside a pad's copper is connected to that pad
        for pi, (x, y, r, _t) in zip(pad_ix, pads):
            for k in range(len(term)):
                if k != pi and math.hypot(term[k][0] - x, term[k][1] - y) <= r:
                    union(pi, k)

        comp: Dict[int, List[int]] = defaultdict(list)
        for i in range(len(term)):
            comp[find(i)].append(i)
        # main component = the one holding the most PADS (the real net tree)
        main = max(comp.keys(),
                   key=lambda c: sum(1 for pi in pad_ix if find(pi) == c))
        # Prefer a THROUGH-HOLE pad in the main component as the target: it
        # spans every layer, so a plain track (NO via) connects to it. Falls
        # back to any main-component point (then the stub adds a via).
        main_tht = [term[pi] for pi, t in zip(pad_ix, pad_tht)
                    if t and find(pi) == main]
        main_pts = [term[i] for i in comp[main]]
        for pi, (x, y, _r, _t) in zip(pad_ix, pads):
            if find(pi) != main:
                pool = main_tht or main_pts
                if not pool:
                    continue
                tgt = min(pool, key=lambda p: math.hypot(p[0] - x, p[1] - y))
                out.append((nidx, names.get(nidx, str(nidx)), (x, y), tgt))
    return out


async def _run_drc(pcb: Path) -> Tuple[int, int]:
    """(error_count, unconnected_items_count). unconnected is filed under
    warnings by drc_check, so read the raw report for its count."""
    from .drc_check import drc_check
    r = await drc_check.handler({"pcb_path": str(pcb)})
    err = int(r.get("error_count", -1))
    n_unc = 0
    try:
        rep = json.loads(pcb.with_name(pcb.stem + "-drc.json").read_text(encoding="utf-8"))
        n_unc = len(rep.get("unconnected_items", []) or [])
    except (OSError, ValueError):
        pass
    return err, n_unc


def _seg(a: Tuple[float, float], b: Tuple[float, float],
         width: float, layer: str, net: int) -> list:
    import sexpdata as S
    import uuid as U
    return [S.Symbol("segment"),
            [S.Symbol("start"), a[0], a[1]], [S.Symbol("end"), b[0], b[1]],
            [S.Symbol("width"), width], [S.Symbol("layer"), layer],
            [S.Symbol("net"), net], [S.Symbol("uuid"), str(U.uuid4())]]


def _via(pt: Tuple[float, float], net: int, dia: float, drill: float) -> list:
    import sexpdata as S
    import uuid as U
    return [S.Symbol("via"), [S.Symbol("at"), pt[0], pt[1]],
            [S.Symbol("size"), dia], [S.Symbol("drill"), drill],
            [S.Symbol("layers"), "F.Cu", "B.Cu"],
            [S.Symbol("net"), net], [S.Symbol("uuid"), str(U.uuid4())]]


def _candidates(p: Tuple[float, float], q: Tuple[float, float], width: float,
                layer: str, net: int, try_l: bool,
                via_dia: float, via_drill: float) -> List[List[list]]:
    """p->q on one layer. NO-VIA shapes first (direct then two L-routes) — these
    connect when q is a through-hole pad (spans all layers), the common case.
    Then the same shapes ENDING IN A VIA, for when q's copper is on the other
    layer. DRC-validation keeps the first shape that actually connects."""
    c1, c2 = (q[0], p[1]), (p[0], q[1])
    shapes = [[_seg(p, q, width, layer, net)]]
    if try_l:
        shapes.append([_seg(p, c1, width, layer, net), _seg(c1, q, width, layer, net)])
        shapes.append([_seg(p, c2, width, layer, net), _seg(c2, q, width, layer, net)])
    v = _via(q, net, via_dia, via_drill)
    return shapes + [s + [v] for s in shapes]


@tool(
    name="connect_stray_pads",
    description=(
        "Connect stray unconnected pads — a pad left out of an otherwise-routed "
        "net that the routers skip (they see the net has copper and call it "
        "done). Union-finds each net's copper, and routes each pad not in the "
        "net's main connected component to a point that IS in it (back layer "
        "first, via at the target), DRC-validating every candidate (kept only "
        "if it clears the unconnected and adds no short). Run AFTER routing + "
        "zones. Mutates the .kicad_pcb (adds tracks/vias only).\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}\n'
        "Honest: a pad with no legal stub is reported for manual routing."
    ),
    input_schema={"pcb_path": str},
)
async def connect_stray_pads(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "connect_stray_pads disabled"}],
                "ok": True}
    pcb = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb.exists() or pcb.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": f"ERROR: bad .kicad_pcb: {pcb}"}],
                "is_error": True}

    import sexpdata
    width = float(cfg.get("track_width_mm", 0.25))
    layers = list(cfg.get("layers", ["B.Cu", "F.Cu"]))
    try_l = bool(cfg.get("try_l_routes", True))
    cap = int(cfg.get("max_stray_pads", 20))
    eps = float(cfg.get("coincidence_mm", 0.05))
    via_dia = float(cfg.get("via_diameter_mm", 0.6))
    via_drill = float(cfg.get("via_drill_mm", 0.3))
    vt = cfg.get("verdict", {}) or {}

    root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    strays = _connectivity_strays(root, eps)[:cap]
    if not strays:
        return {"content": [{"type": "text",
                             "text": f"# connect_stray_pads — {pcb.name}\n\n"
                                     f"**{vt.get('none', 'NO STRAY PADS')}**"}],
                "ok": True, "fixed": 0, "total": 0}

    base_err, base_unc = await _run_drc(pcb)
    fixed: List[str] = []
    left: List[str] = []

    for (nidx, net, pad_pt, tgt) in strays:
        placed = False
        for layer in layers:
            for cand in _candidates(pad_pt, tgt, width, layer, nidx,
                                    try_l, via_dia, via_drill):
                for s in cand:
                    root.append(s)
                pcb.write_text(sexpdata.dumps(root), encoding="utf-8")
                err, unc = await _run_drc(pcb)
                if err >= 0 and err <= base_err and unc < base_unc:
                    base_err, base_unc = err, unc
                    fixed.append(f"{net}@({pad_pt[0]:.1f},{pad_pt[1]:.1f}) on {layer}")
                    placed = True
                    break
                del root[len(root) - len(cand):]
                pcb.write_text(sexpdata.dumps(root), encoding="utf-8")
            if placed:
                break
        if not placed:
            left.append(f"{net}@({pad_pt[0]:.1f},{pad_pt[1]:.1f})")

    total = len(fixed) + len(left)
    if not left:
        verdict = vt.get("clean", "STRAY PADS CONNECTED — {fixed}/{total}").format(
            fixed=len(fixed), total=total)
        ok = True
    else:
        verdict = vt.get("partial",
                         "STRAY PADS — {fixed}/{total} routed; {left} manual"
                         ).format(fixed=len(fixed), total=total, left=len(left))
        ok = (len(fixed) > 0)

    lines = [f"# connect_stray_pads — {pcb.name}",
             f"  {len(fixed)} connected, {len(left)} left · unconnected now {base_unc}"]
    for f in fixed:
        lines.append(f"  ✓ {f}")
    for l in left:
        lines.append(f"  ✗ {l}: no legal stub — route manually")
    lines.append("")
    lines.append(f"**{verdict}**")
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": ok, "pcb": str(pcb).replace("\\", "/"),
        "fixed": len(fixed), "left": len(left),
        "drc_errors": base_err, "unconnected": base_unc, "verdict": verdict,
    }
