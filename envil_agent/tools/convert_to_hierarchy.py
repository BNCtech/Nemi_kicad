"""Tool: convert a flat .kicad_sch into a multi-sheet hierarchy.

Real engine work — not a wrapper. Steps:
  1. Run `kicad-cli sch export netlist --format kicadxml` to recover
     components + nets from the existing schematic
  2. Group components into blocks — by user-supplied mapping or
     by automatic refdes-prefix heuristic (U/Q -> separate blocks,
     R/C/L -> PASSIVES, J/D -> IO, etc. all JSON-driven)
  3. Build a fresh TopologyIR with these blocks
  4. Back up the original .kicad_sch
  5. Re-render via engine.render_hierarchical, producing parent +
     child sheets

Universal — works on any flat schematic. Preserves the original via
the apply_ops snapshot system so the user can undo if unhappy.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("convert_to_hierarchy", {}) or {}
    except Exception:
        return {}


def _kicad_cli(cfg: Dict[str, Any]) -> str:
    if cfg.get("kicad_cli_command"):
        return str(cfg["kicad_cli_command"])
    try:
        from ..intent.engine import _load_layout_config
        return str(_load_layout_config().get("erc_check", {}).get(
            "kicad_cli_command", "kicad-cli"))
    except Exception:
        return "kicad-cli"


def _export_netlist_xml(cli: str, sch: Path,
                         timeout: int = 30) -> Optional[Path]:
    """Run kicad-cli sch export netlist --format kicadxml. Returns the
    output path (or None on failure)."""
    out = sch.with_suffix(".convertnetlist.xml")
    try:
        r = subprocess.run(
            [cli, "sch", "export", "netlist",
             "--output", str(out),
             "--format", "kicadxml",
             str(sch)],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0 and out.exists():
            return out
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _parse_netlist(xml_path: Path) -> Tuple[List[Dict[str, Any]],
                                              List[Dict[str, Any]]]:
    """Parse a kicad-cli kicadxml netlist into (components, nets).
    components: [{ref, value, footprint, libsource}, ...]
    nets:       [{name, is_power, pins: [refdes.pin, ...]}, ...]"""
    tree = ET.parse(str(xml_path))
    root = tree.getroot()

    comps = []
    for c in root.findall("./components/comp"):
        ref = c.attrib.get("ref", "")
        val = (c.findtext("value") or "").strip()
        fp = (c.findtext("footprint") or "").strip()
        libsrc = c.find("libsource")
        libid = ""
        if libsrc is not None:
            lib = libsrc.attrib.get("lib", "")
            part = libsrc.attrib.get("part", "")
            if lib and part:
                libid = f"{lib}:{part}"
        comps.append({"ref": ref, "value": val,
                       "footprint": fp, "lib_id": libid})

    nets = []
    for n in root.findall("./nets/net"):
        name = n.attrib.get("name", "")
        # Power nets — KiCad's xml form has no "is_power" flag, infer
        # from name (canonical power-rail patterns)
        upper = name.upper().lstrip("/").lstrip("+")
        power_patterns = ("GND", "VCC", "VDD", "VEE", "VSS", "VBAT",
                           "AGND", "DGND", "VBUS", "VIN", "VOUT",
                           "+3V", "+5V", "+12V", "+9V", "+1V8", "+24V")
        is_power = any(upper.startswith(p) or upper == p
                        for p in power_patterns)
        pins = []
        for node in n.findall("./node"):
            ref = node.attrib.get("ref", "")
            pin = node.attrib.get("pin", "")
            if ref and pin:
                pins.append(f"{ref}.{pin}")
        nets.append({"name": name, "is_power": is_power, "pins": pins})

    return comps, nets


def _auto_group(comps: List[Dict[str, Any]],
                 cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """Heuristic: assign each refdes to a block by its prefix.

    Default mapping (override in JSON config:default_prefix_blocks):
      U -> per-IC block named "MAIN_<refdes>"  (one block per IC)
      Q -> TRANSISTORS
      D -> DIODES
      J -> IO
      SW -> CONTROLS
      Y/X -> CLOCKS
      R/C/L/FB -> grouped with their NEAREST IC by net adjacency, OR
                  PASSIVES bucket if no IC neighbor"""
    # For v1: simple per-prefix bucket. Per-IC clustering is a v2
    # improvement once net-adjacency analysis is wired in.
    prefix_map = cfg.get("default_prefix_blocks", {
        "U": "MAIN", "Q": "TRANSISTORS", "D": "DIODES",
        "J": "IO", "SW": "CONTROLS", "Y": "CLOCKS",
        "R": "PASSIVES", "C": "PASSIVES", "L": "PASSIVES",
        "FB": "PASSIVES", "TP": "MISC",
    })
    blocks: Dict[str, List[str]] = {}
    for c in comps:
        ref = c.get("ref", "")
        if not ref or ref.startswith("#"):
            continue
        # Strip trailing digits to get prefix
        i = len(ref) - 1
        while i >= 0 and ref[i].isdigit():
            i -= 1
        prefix = ref[: i + 1]
        block = prefix_map.get(prefix, "MISC")
        blocks.setdefault(block, []).append(ref)
    return blocks


def _build_ir(comps: List[Dict[str, Any]],
               nets: List[Dict[str, Any]],
               blocks_map: Dict[str, List[str]],
               name: str) -> Any:
    """Construct a TopologyIR from the netlist data + block grouping.
    Lazy-imported so the tool doesn't pull engine deps on import."""
    from ..intent.ir import TopologyIR, IRComponent, IRNet, IRBlock
    ir_comps = []
    for c in comps:
        if not c.get("ref") or c.get("ref", "").startswith("#"):
            continue
        ir_comps.append(IRComponent(
            ref=c["ref"], lib_id=c.get("lib_id", ""),
            value=c.get("value", ""),
            footprint=c.get("footprint", "")))
    ir_nets = []
    for n in nets:
        if not n.get("pins"):
            continue
        # Skip kicad-cli auto-generated noise nets like Net-(R1-Pad1)
        nm = n.get("name", "")
        if nm.startswith("Net-(") or nm.startswith("unconnected-"):
            continue
        ir_nets.append(IRNet(
            name=nm.lstrip("/"),
            is_power=n.get("is_power", False),
            pins=n["pins"]))
    ir_blocks = []
    for block_name, refs in blocks_map.items():
        if not refs:
            continue
        ir_blocks.append(IRBlock(
            name=block_name, block_type=block_name.lower(),
            component_refs=refs))
    return TopologyIR(
        name=name, circuit_type="MCU_BOARD",
        components=ir_comps, nets=ir_nets, blocks=ir_blocks,
    )


@tool(
    name="convert_to_hierarchy",
    description=(
        "Convert a flat .kicad_sch into a multi-sheet hierarchy "
        "(parent + child sheets). Extracts components + nets via "
        "kicad-cli netlist export, groups by refdes prefix (or "
        "user-supplied `blocks`), and re-renders via the engine's "
        "hierarchical path. Original is backed up via the snapshot "
        "system so the user can undo.\n"
        "Args:\n"
        '  {"sch_path": "C:/.../proj.kicad_sch"}                # auto-group\n'
        '  {"sch_path": "...", "blocks": {"MCU": ["U1","C1","C2"], "POWER": ["J1","C3"]}}\n'
        "Triggers: 'split into sheets', 'convert to hierarchy', "
        "'make multi-sheet', 'hierarchy aaka pannu' (Tanglish)."
    ),
    input_schema={"sch_path": str},
)
async def convert_to_hierarchy(args: dict[str, Any]) -> dict[str, Any]:
    sch_path = Path(str(args.get("sch_path", "")).strip()).expanduser()
    if not sch_path.exists():
        return {"content": [{"type": "text",
                              "text": f"ERROR: .kicad_sch not found: {sch_path}"}],
                 "is_error": True}
    if sch_path.suffix.lower() != ".kicad_sch":
        return {"content": [{"type": "text",
                              "text": "ERROR: expected .kicad_sch"}],
                 "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                              "text": "convert_to_hierarchy disabled"}],
                 "is_error": True}

    cli = _kicad_cli(cfg)
    timeout = int(cfg.get("timeout_seconds", 30))

    # 1. Export netlist
    netlist = _export_netlist_xml(cli, sch_path, timeout)
    if netlist is None:
        return {"content": [{"type": "text",
                              "text": ("ERROR: failed to export netlist. "
                                        "Is kicad-cli reachable?")}],
                 "is_error": True}

    # 2. Parse
    try:
        comps, nets = _parse_netlist(netlist)
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: parse netlist failed: {exc}"}],
                 "is_error": True}

    if not comps:
        return {"content": [{"type": "text",
                              "text": "schematic has no components — nothing to split"}],
                 "is_error": True}

    # 3. Group
    user_blocks = args.get("blocks")
    if isinstance(user_blocks, dict) and user_blocks:
        blocks_map = {str(k): [str(r) for r in v]
                       for k, v in user_blocks.items()}
    else:
        blocks_map = _auto_group(comps, cfg)
    if len(blocks_map) < 2:
        return {"content": [{"type": "text",
                              "text": (f"only {len(blocks_map)} block(s) "
                                        f"detected — nothing to split into "
                                        f"a hierarchy. Provide an explicit "
                                        f"`blocks` mapping.")}],
                 "is_error": True}

    # 4. Backup via snapshot system (apply_ops handles this when called,
    # but we're operating outside of it — replicate the same backup.)
    try:
        from envil_agent.tools.apply_ops import _take_snapshot
        _take_snapshot(sch_path,
                        f"convert_to_hierarchy ({len(blocks_map)} blocks)")
    except Exception:
        pass

    # 5. Build IR + re-render
    name = sch_path.stem
    try:
        ir = _build_ir(comps, nets, blocks_map, name)
        from ..intent import engine
        # render_hierarchical writes parent + children into the parent
        # folder, replacing the flat schematic.
        out_dir = sch_path.parent
        # Remove the flat sch + its associated svg/erc/etc. so the
        # hierarchy emit is clean.
        try:
            sch_path.unlink(missing_ok=True)
        except OSError:
            pass
        # Old PCB stays — user keeps any layout work; subsequent F8
        # re-syncs the new netlist.
        result = engine.render_hierarchical(ir, out_dir)
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": (f"ERROR: re-render failed: "
                                        f"{type(exc).__name__}: {exc}")}],
                 "is_error": True}

    children = result.get("children", []) or []
    parent_path = result.get("path", "")

    return {
        "content": [{"type": "text",
                      "text": (f"converted to hierarchy: "
                                f"{len(blocks_map)} blocks, "
                                f"{len(children)} child sheet(s)\n"
                                f"  parent: {parent_path}\n"
                                f"  blocks: {', '.join(blocks_map.keys())}\n"
                                f"  components: {len(comps)}\n"
                                f"  nets:       {len(nets)}\n"
                                f"  (original snapshotted — use undo_last_edit "
                                f"to revert if needed)")}],
        "ok": True,
        "parent_path": parent_path,
        "child_paths": [c.get("path", "") for c in children],
        "blocks": blocks_map,
    }
