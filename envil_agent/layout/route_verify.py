"""Post-route honest verification — read the truth back from the WRITTEN board.

Both routers (route_pcb_simple, route_pcb_astar) used to report success from
their own in-memory counters, never re-reading the file. That let the chat
narrate "all nets routed" while the saved .kicad_pcb had Track Segments = 0 and
a full ratsnest (the false-green). This module re-parses the board the router
just wrote and counts, for every multi-pad signal net, whether it actually has
copper (track / via / arc) or a poured zone. The routers fold this back into
their own message so even a lazy narration tells the truth.

Definition of "routed" mirrors tools/routing_audit.py exactly (track/via/arc OR
an assigned zone), so the router's self-report agrees with the audit gate. Pure,
defensive, never raises — on any parse failure it returns {"error": ...} and the
caller falls back to its old behaviour (non-breaking).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List


def verify_routing(pcb_path: Any) -> Dict[str, Any]:
    """Re-read ``pcb_path`` and return honest routed/unrouted counts.

    Returns ``{"total_nets", "routed_nets", "unrouted": [names], "tracks",
    "vias", "complete"}`` where ``total_nets`` counts multi-pad (>=2) signal
    nets and ``complete`` is ``unrouted == []``. Returns ``{"error": msg}`` if
    the file cannot be read/parsed (caller should degrade gracefully).
    """
    import sexpdata
    from .place_refine import _head, _child, _children, _pad_net_id

    p = Path(pcb_path)
    try:
        root = sexpdata.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"cannot parse {p.name}: {exc}"}

    net_name: Dict[int, str] = {}
    net_pads: Dict[int, int] = {}
    routed: set = set()
    tracks = 0
    vias = 0

    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list):
            continue
        h = _head(node)
        if h == "net" and len(node) >= 3:
            try:
                net_name[int(node[1])] = str(node[2]).strip('"')
            except (TypeError, ValueError):
                pass
        elif h == "footprint":
            for pad in _children(node, "pad"):
                nid = _pad_net_id(pad)
                if nid > 0:
                    net_pads[nid] = net_pads.get(nid, 0) + 1
        elif h in ("segment", "via", "arc"):
            if h == "segment":
                tracks += 1
            elif h == "via":
                vias += 1
            netc = _child(node, "net")
            if netc and len(netc) >= 2:
                try:
                    routed.add(int(netc[1]))
                except (TypeError, ValueError):
                    pass

    # A net with an ASSIGNED copper zone is routed by that pour (how GND/power
    # get connected). Mirror routing_audit: read the zones, don't require the
    # cached fill — KiCad fills on load/before DRC.
    try:
        from ..tools.power_audit import _parse_zones
        for z in _parse_zones(root):
            if z.get("net"):
                routed.add(int(z["net"]))
    except Exception:                                          # noqa: BLE001
        pass

    unrouted: List[str] = []
    total = 0
    for nid, cnt in net_pads.items():
        if nid == 0 or cnt < 2:
            continue
        total += 1
        if nid not in routed:
            unrouted.append(net_name.get(nid, f"net{nid}"))
    unrouted.sort()

    return {
        "total_nets": total,
        "routed_nets": total - len(unrouted),
        "unrouted": unrouted,
        "tracks": tracks,
        "vias": vias,
        "complete": not unrouted,
    }


def verify_line(v: Dict[str, Any], skip_nets: Any = None) -> str:
    """One honest chat line from a ``verify_routing`` result.

    ``skip_nets`` (names left to the copper pour, case-insensitive) are reported
    separately as "awaiting pour" rather than "router failed", so the message
    distinguishes a deferred ground/power net from a net the router couldn't
    reach. Empty string if ``v`` carried an error (caller keeps old behaviour).
    """
    if "error" in v or "total_nets" not in v:
        return ""
    r, t = v["routed_nets"], v["total_nets"]
    if v["complete"]:
        return f"  verified on board: {r}/{t} nets have copper — routing complete."

    skip = {str(s).strip().upper() for s in (skip_nets or [])}
    pour = [n for n in v["unrouted"] if n.upper() in skip]
    failed = [n for n in v["unrouted"] if n.upper() not in skip]
    parts = [f"  VERIFIED ON BOARD: {r}/{t} nets have copper"]
    if failed:
        shown = ", ".join(failed[:8]) + (" …" if len(failed) > 8 else "")
        parts[0] += f" — {len(failed)} STILL UNROUTED: {shown}."
    else:
        parts[0] += "."
    if pour:
        shown = ", ".join(pour[:8]) + (" …" if len(pour) > 8 else "")
        parts.append(f"  {len(pour)} net(s) awaiting copper pour (auto_zones_pcb): {shown}.")
    return "\n".join(parts)
