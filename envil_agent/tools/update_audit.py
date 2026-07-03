"""Tool: update_audit — the deterministic gate for Schematic → PCB sync.

Step 4 of the PCB flow ends in a hard pass/fail: after "Update PCB from
Schematic", a validation pass must PROVE the board matches the schematic.
This tool is that pass. It parses the ``.kicad_sch`` and its sibling
``.kicad_pcb`` (KiCad 9 S-expression) and asserts:

  component_presence   — every schematic symbol has a board footprint, and
                         every board footprint has a schematic symbol
                         (added / removed / orphan diff).       [rule 4/5]
  unresolved_footprint — no schematic symbol carries an empty Footprint
                         (those can never sync).                 [rule 5]
  net_survival         — every named net in the schematic/IR appears in the
                         board net table, name-for-name.         [rule 8]
  unconnected_pads     — a pad the IR says belongs to a net is not sitting
                         on board net 0 (ratsnest proxy).        [rule 7]
  uuid_linkage         — footprints carry the schematic symbol UUID (path),
                         so a renumber can't wipe placement.     [rule 3]

Read-only: it never edits either file, keeping "apply the update" separate
from "verify the result". Expected connectivity comes from the ``.envil-ir.json``
sidecar (authoritative); without it the net checks are skipped and reported
as such rather than silently passing. Verdict is in words.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "update_audit.json"
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
# board (.kicad_pcb) parse
# --------------------------------------------------------------------------- #

def _parse_board(pcb_path: Path) -> Optional[Dict[str, Any]]:
    """Return {refs, pad_net, net_names, any_path, total_pads, unconnected_pads}
    from a .kicad_pcb, or None if it can't be parsed.

      refs        : {reference}
      pad_net     : {(ref, pad_number): (net_idx, net_name)}
      net_names   : {declared net names in the board net table}
      any_path    : True if any footprint carries a schematic (path ...) link
    """
    from ..layout.pcb_gen import _head, _child
    from .footprint_audit import _atom
    import sexpdata

    try:
        root = sexpdata.loads(pcb_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AssertionError):
        return None
    if not isinstance(root, list):
        return None

    refs: Set[str] = set()
    pad_net: Dict[Tuple[str, str], Tuple[int, str]] = {}
    net_names: Set[str] = set()
    any_path = False
    total_pads = 0
    unconnected_pads = 0

    for node in root[1:]:
        if not isinstance(node, list):
            continue
        head = _head(node)
        if head == "net" and len(node) >= 3:
            net_names.add(str(node[2]).strip('"'))
        elif head == "footprint":
            ref = ""
            for c in node[1:]:
                if (isinstance(c, list) and _head(c) == "property"
                        and len(c) >= 3 and str(c[1]).strip('"') == "Reference"):
                    ref = str(c[2]).strip('"')
                    break
            if ref:
                refs.add(ref)
            if _child(node, "path") is not None:
                any_path = True
            for c in node[1:]:
                if not (isinstance(c, list) and _head(c) == "pad"):
                    continue
                total_pads += 1
                padnum = _atom(c[1]).strip('"') if len(c) >= 2 else ""
                netchild = _child(c, "net")
                if netchild and len(netchild) >= 2:
                    try:
                        idx = int(netchild[1])
                    except (ValueError, TypeError):
                        idx = 0
                    nm = str(netchild[2]).strip('"') if len(netchild) >= 3 else ""
                else:
                    idx, nm = 0, ""
                if idx == 0:
                    unconnected_pads += 1
                if ref and padnum:
                    pad_net[(ref, padnum)] = (idx, nm)

    return {
        "refs": refs,
        "pad_net": pad_net,
        "net_names": net_names,
        "any_path": any_path,
        "total_pads": total_pads,
        "unconnected_pads": unconnected_pads,
    }


def _expected_from_ir(sch_path: Path) -> Optional[Dict[str, Any]]:
    """Expected connectivity from the .envil-ir.json sidecar:
    {net_names, pad_net:{(ref,pad):(idx,name)}}. None when no sidecar."""
    side = sch_path.with_suffix(".envil-ir.json")
    if not side.exists():
        return None
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        from ..intent.ir import TopologyIR
        from ..layout import pcb_gen
        ir = TopologyIR.from_dict(data.get("ir") or {})
        _table, pad_net_map, _warn = pcb_gen._build_net_map(ir)
        return {
            "net_names": {n.name for n in ir.nets},
            "pad_net": pad_net_map,          # {(ref, pad): (idx, name)}
        }
    except Exception:                                       # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# tool
# --------------------------------------------------------------------------- #

@tool(
    name="update_audit",
    description=(
        "Validate that a .kicad_pcb matches its .kicad_sch after an "
        "Update-PCB-from-Schematic sync. Read-only deterministic gate for "
        "step 4 of the PCB flow. Checks: every symbol has a board footprint "
        "and vice-versa (added/removed/orphan), no symbol has an empty "
        "footprint, every named net survived name-for-name, no pad that "
        "should be netted is unconnected, and footprints are UUID-linked to "
        "the schematic (not just reference-matched). Run it right after "
        "update_pcb / generate_pcb, before routing or DRC.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_sch"}   # or the .kicad_pcb / project dir\n'
        "Returns a compact card + structured diff. Verdict in words. Net "
        "checks use the .envil-ir.json sidecar; without it they're skipped "
        "and reported as skipped, never silently passed. Policy in "
        "config/update_audit.json."
    ),
    input_schema={"path": str},
)
async def update_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "update_audit disabled in config"}],
                "is_error": True}

    from .footprint_audit import _resolve_sch_path, _iter_components
    sch = _resolve_sch_path(str(args.get("path", "")).strip())
    if sch is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: no .kicad_sch found for "
                                     f"{args.get('path', '')!r}"}],
                "is_error": True}
    pcb = sch.with_suffix(".kicad_pcb")

    skip_prefixes = list((cfg.get("skip_ref_prefixes", {}) or {})
                         .get("prefixes", ["#PWR", "#FLG", "#"]))
    vt = cfg.get("verdict", {}) or {}
    cap = int(cfg.get("max_examples_per_rule", 15))

    try:
        comps = _iter_components(sch, skip_prefixes)
    except Exception as exc:                                # noqa: BLE001
        return {"content": [{"type": "text",
                             "text": f"ERROR: could not parse {sch.name}: "
                                     f"{type(exc).__name__}: {exc}"}],
                "is_error": True}

    sch_refs = {c["ref"] for c in comps}
    empty_fp = {c["ref"] for c in comps if not c["footprint"].strip()}

    # --- board present? ---
    if not pcb.exists():
        verdict = vt.get("no_board",
                         "NO BOARD — run generate/update PCB first")
        return {"content": [{"type": "text",
                             "text": f"# Sync audit — {sch.name}\n"
                                     f"  {len(sch_refs)} schematic parts · "
                                     f"no .kicad_pcb\n\n**{verdict}**"}],
                "ok": False, "verdict": verdict,
                "schematic": str(sch).replace("\\", "/"), "board": None}

    if not sch_refs:
        verdict = vt.get("empty_sch", "NO PARTS — nothing to sync")
        return {"content": [{"type": "text",
                             "text": f"# Sync audit — {sch.name}\n\n"
                                     f"**{verdict}**"}],
                "ok": True, "verdict": verdict}

    board = _parse_board(pcb)
    if board is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: could not parse {pcb.name}"}],
                "is_error": True}

    expected = _expected_from_ir(sch)

    findings: List[Dict[str, str]] = []

    def add(ref: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"ref": ref, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    # --- component presence diff ---
    missing_on_board = sorted(sch_refs - board["refs"])
    orphan_on_board = sorted(board["refs"] - sch_refs)
    for ref in missing_on_board:
        why = " (footprint empty upstream)" if ref in empty_fp else ""
        add(ref, "component_presence",
            f"in schematic but not on board{why}")
    for ref in orphan_on_board:
        add(ref, "component_presence",
            "on board but no schematic symbol — next sync would delete it")

    # --- unresolved footprint (root cause of most missing_on_board) ---
    for ref in sorted(empty_fp):
        add(ref, "unresolved_footprint",
            "empty Footprint field — assign upstream (footprint_audit) then re-sync")

    # --- net survival + unconnected (need the IR sidecar) ---
    net_checked = False
    if expected is not None:
        net_checked = True
        missing_nets = sorted(expected["net_names"] - board["net_names"])
        for nm in missing_nets:
            add("net:" + nm, "net_survival",
                f"net '{nm}' in schematic but missing from board net table")
        auto = sorted(n for n in board["net_names"] if n.startswith("Net-("))
        if auto and expected["net_names"]:
            add("board", "net_survival",
                f"board has {len(auto)} auto-named net(s) (Net-(...)) — "
                f"a label/power-flag may be missing: {', '.join(auto[:5])}")

        # unconnected: IR says this (ref,pad) is on a real net, board shows net 0
        unconn: List[str] = []
        for (ref, pad), (idx, nm) in expected["pad_net"].items():
            if not nm or ref not in board["refs"]:
                continue
            b = board["pad_net"].get((ref, pad))
            if b is None:
                continue                       # pad-level absence → presence issue
            if b[0] == 0:
                unconn.append(f"{ref}.{pad}({nm})")
        for u in unconn:
            add(u.split(".")[0], "unconnected_pads",
                f"pad {u} expected on a net but unconnected on board")

    # --- uuid linkage (board-level) ---
    if not board["any_path"]:
        add("board", "uuid_linkage",
            "footprints carry no schematic UUID link (path) — reference-"
            "matched only; a native Update-from-Schematic gives stable UUID "
            "links so a renumber won't wipe placement/routing")

    # --- aggregate ---
    counts = {"error": 0, "warning": 0, "info": 0, "review": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w = counts["error"], counts["warning"]
    n = len(sch_refs)
    nets = len(board["net_names"] - {""})

    if e == 0 and w == 0:
        verdict = vt.get("clean",
                         "SYNC VERIFIED — {n} parts, {nets} nets"
                         ).format(n=n, nets=nets)
        ok = True
    else:
        verdict = vt.get("issues",
                         "SYNC NOT CLEAN — {e} error(s), {w} warning(s)"
                         ).format(e=e, w=w)
        ok = (e == 0)

    # --- card ---
    icon = {"error": "✗", "warning": "!", "info": "·", "review": "?"}
    lines = [f"# Sync audit — {sch.name}",
             f"  schematic {n} parts · board {len(board['refs'])} footprints · "
             f"{nets} nets · {board['unconnected_pads']}/{board['total_pads']} "
             f"pads unconnected"]
    if missing_on_board:
        lines.append(f"  missing on board: {', '.join(missing_on_board)}")
    if orphan_on_board:
        lines.append(f"  orphan on board: {', '.join(orphan_on_board)}")
    if not net_checked:
        lines.append("  net/unconnected checks skipped: no .envil-ir.json sidecar")

    for sev in ["error", "warning", "info"]:
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
        "board": str(pcb).replace("\\", "/"),
        "verdict": verdict,
        "counts": counts,
        "net_checked": net_checked,
        "missing_on_board": missing_on_board,
        "orphan_on_board": orphan_on_board,
        "findings": findings,
    }
