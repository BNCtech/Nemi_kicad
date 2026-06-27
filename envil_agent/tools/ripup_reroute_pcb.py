"""Tool: ripup_reroute_pcb — pure-Python rip-up-and-reroute (no Java/GPU/deps).

The simple router abandons a net when other nets' already-laid tracks block every
path. This post-pass escalates: for each still-unconnected net it RIPS UP the
tracks that sit in its way, routes the net into the freed space, then re-routes
the ripped nets.

SAFETY — *never worse*: the entire board file is snapshotted before each net's
attempt; the attempt is committed ONLY if KiCad's own DRC then reports FEWER
unconnected pads, otherwise the snapshot is restored byte-for-byte. So this can
only improve connectivity, never degrade it. Bounded by ``max_rounds``.

SECURITY: pure Python, no external router, no network, no new dependency — it
only reads/writes the one .kicad_pcb and shells out to the same trusted kicad-cli
DRC the rest of the pipeline already uses. Gated by ``ripup_reroute.enabled``
(off -> no-op). Never raises.

Note: rip-up frees space taken by *tracks*. On all-through-hole boards the
blockers are often the *pads* (THT pads occupy every layer), which rip-up cannot
move — there the honest answer remains "needs better placement / a real router".
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import sexpdata
from claude_agent_sdk import tool

from .route_pcb_simple import _head, _net_table


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config()
    except Exception:
        return {}


async def _unconnected(pcb_path: Path) -> Tuple[int, Set[str]]:
    """(unconnected-pair count, set of net names) from a fresh DRC. (-1, set())
    if DRC can't run."""
    report_path = pcb_path.with_name(pcb_path.stem + "-drc.json")
    try:
        drc = importlib.import_module("envil_agent.tools.drc_check")
        await drc.drc_check.handler({"pcb_path": str(pcb_path)})
        d = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return (-1, set())
    pairs = 0
    nets: Set[str] = set()
    for u in d.get("unconnected_items", []) or []:
        items = u.get("items", []) or []
        pairs += len(items)
        for it in items:
            m = re.search(r"\[([^\]]+)\]", it.get("description", "") or "")
            if m:
                nets.add(m.group(1))
    return (pairs, nets)


def _seg_xy(seg: list, key: str) -> Optional[Tuple[float, float]]:
    for ch in seg[1:]:
        if isinstance(ch, list) and _head(ch) == key and len(ch) >= 3:
            try:
                return (float(ch[1]), float(ch[2]))
            except (TypeError, ValueError):
                return None
    return None


def _seg_net_id(seg: list) -> int:
    for ch in seg[1:]:
        if isinstance(ch, list) and _head(ch) == "net" and len(ch) >= 2:
            try:
                return int(ch[1])
            except (TypeError, ValueError):
                return 0
    return 0


def _net_pad_bbox(root: list, net_name: str
                  ) -> Optional[Tuple[float, float, float, float]]:
    """Board bbox of every pad on ``net_name`` (where its tracks must reach)."""
    xs: List[float] = []
    ys: List[float] = []
    for fp in root[1:]:
        if not (isinstance(fp, list) and _head(fp) == "footprint"):
            continue
        at = None
        for ch in fp[1:]:
            if isinstance(ch, list) and _head(ch) == "at":
                try:
                    at = (float(ch[1]), float(ch[2]))
                except (TypeError, ValueError):
                    at = (0.0, 0.0)
                break
        if at is None:
            continue
        for ch in fp[1:]:
            if not (isinstance(ch, list) and _head(ch) == "pad"):
                continue
            pad_at = None
            on_net = False
            for sub in ch[2:]:
                if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    try:
                        pad_at = (float(sub[1]), float(sub[2]))
                    except (TypeError, ValueError):
                        pad_at = (0.0, 0.0)
                if (isinstance(sub, list) and _head(sub) == "net"
                        and len(sub) >= 3 and str(sub[2]) == net_name):
                    on_net = True
            if on_net and pad_at is not None:
                xs.append(at[0] + pad_at[0])
                ys.append(at[1] + pad_at[1])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _rip_blockers(root: list, target: str, margin: float
                  ) -> Tuple[list, Set[str]]:
    """Drop every segment of OTHER nets that lies inside the target net's pad
    bbox (inflated by ``margin``). Returns (new_root, set of ripped net names)."""
    bbox = _net_pad_bbox(root, target)
    if bbox is None:
        return root, set()
    x0, y0, x1, y1 = bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin
    nt = _net_table(root)
    kept: list = [root[0]]
    ripped: Set[str] = set()
    for ch in root[1:]:
        if isinstance(ch, list) and _head(ch) == "segment":
            nm = nt.get(_seg_net_id(ch), "")
            if nm and nm != target:
                s = _seg_xy(ch, "start")
                e = _seg_xy(ch, "end")
                inside = False
                for pt in (s, e):
                    if pt and x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1:
                        inside = True
                        break
                if inside:
                    ripped.add(nm)
                    continue          # drop this segment
        kept.append(ch)
    return kept, ripped


@tool(
    name="ripup_reroute_pcb",
    description=(
        "Pure-Python rip-up-and-reroute. For each still-unconnected net, rip up "
        "the tracks blocking it, route it, and re-route the ripped nets — keeping "
        "the change ONLY if DRC shows fewer unconnected pads (else revert). Never "
        "makes the board worse. No Java / GPU / external router. Run after the "
        'main route + GND fallback. Args: {"pcb_path": "C:/.../board.kicad_pcb"}'
    ),
    input_schema={"pcb_path": str},
)
async def ripup_reroute_pcb(args: dict[str, Any]) -> dict[str, Any]:
    cfg = (_load_cfg().get("ripup_reroute", {}) or {})
    if not cfg.get("enabled", False):
        return {"content": [{"type": "text", "text": "ripup_reroute disabled"}],
                "ok": True, "improved": 0}

    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists() or pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": f"ERROR: bad .kicad_pcb: {pcb_path}"}],
                "is_error": True}

    max_rounds = int(cfg.get("max_rounds", 2))
    margin = float(cfg.get("ripup_margin_mm", 2.0))
    route_mod = importlib.import_module("envil_agent.tools.route_pcb_simple")

    start_pairs, _ = await _unconnected(pcb_path)
    if start_pairs <= 0:
        return {"content": [{"type": "text",
                             "text": "ripup_reroute: board already connected (or DRC "
                                     "unavailable); nothing to do."}],
                "ok": True, "improved": 0}

    log: List[str] = []
    improved = 0
    for rnd in range(1, max_rounds + 1):
        pairs, unconnected = await _unconnected(pcb_path)
        if pairs <= 0 or not unconnected:
            break
        round_improved = False
        for net in sorted(unconnected):
            snapshot = pcb_path.read_text(encoding="utf-8")
            base_pairs, _ = await _unconnected(pcb_path)
            try:
                root = sexpdata.loads(snapshot)
                new_root, ripped = _rip_blockers(root, net, margin)
            except Exception:                               # noqa: BLE001
                continue
            if not ripped:
                continue                                    # nothing blocking — rip-up can't help
            # commit the rip, then route the freed net + the ripped nets
            pcb_path.write_text(route_mod._emit(new_root), encoding="utf-8")
            try:
                await route_mod.route_pcb_simple.handler({
                    "pcb_path": str(pcb_path),
                    "force_route_nets": [net] + sorted(ripped),
                    "skip_nets": [],
                    "max_pads_per_net": int(cfg.get("max_pads_per_net", 64)),
                })
            except Exception:                               # noqa: BLE001
                pcb_path.write_text(snapshot, encoding="utf-8")
                continue
            new_pairs, _ = await _unconnected(pcb_path)
            if 0 <= new_pairs < base_pairs:
                improved += (base_pairs - new_pairs)
                round_improved = True
                log.append(f"round {rnd}: {net} — ripped {len(ripped)} net(s), "
                           f"unconnected {base_pairs}->{new_pairs}")
            else:
                pcb_path.write_text(snapshot, encoding="utf-8")  # NEVER worse
        if not round_improved:
            break

    end_pairs, _ = await _unconnected(pcb_path)
    head = (f"ripup_reroute: unconnected pads {start_pairs} -> {end_pairs} "
            f"(recovered {improved})")
    return {"content": [{"type": "text", "text": head + ("\n  " + "\n  ".join(log) if log else "")}],
            "ok": True, "improved": improved,
            "unconnected_before": start_pairs, "unconnected_after": end_pairs}
