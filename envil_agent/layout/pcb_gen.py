"""layout/pcb_gen.py — generate a populated ``.kicad_pcb`` directly from the IR.

Cursor-style, file-based PCB generation. The composer writes the board file
itself — footprints placed + nets assigned — exactly like ``intent/engine.py``
writes the ``.kicad_sch``. There is NO "Update PCB from Schematic" / F8 step and
no running KiCad app is required; KiCad merely reloads the file to display it.

Pipeline (all config-driven via ``layout_config.json:pcb_gen``, no hardcoding):
  1. For each IR component, resolve its footprint id (``IRComponent.footprint``
     or the engine's dynamic ``_resolve_default_footprint``).
  2. Locate the ``.kicad_mod`` on disk via the fp-lib-table (``${KIPRJMOD}`` and
     ``${ENV}`` substitution) with a flat ``<lib_root>/<Lib>.pretty/`` fallback.
  3. Parse the ``.kicad_mod`` and transform it into a *board* footprint node:
       - set the full ``"Lib:Name"`` id, add ``(uuid ...)`` + ``(at x y [rot])``,
       - set the Reference (``R1``) and Value properties,
       - inject ``(net <idx> "<name>")`` into each pad using the IR's
         pin→net map (a symbol pin name like ``U1.VCC`` is resolved to its
         footprint pad number via the symbol geometry's ``resolve_pin``).
  4. Build the board net table: ``(net 0 "")`` then one entry per IR net.
  5. Grid-place footprints (grouped by refdes prefix) so they don't stack at 0,0.
  6. Serialize a valid ``.kicad_pcb`` reusing the battle-tested s-expr emitter
     from ``auto_place_pcb`` (hardened against kicad-cli "Failed to load board").

The function never raises on a single bad part: an unresolved footprint or a
pin/pad mismatch is recorded in the returned report and the rest of the board is
still written, so the build degrades gracefully.
"""
from __future__ import annotations

import os
import uuid as _uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

# Reuse the proven serializer (its float/string escaping is tuned so kicad-cli
# accepts the output) and the engine's footprint resolver + symbol loader.
from ..tools.auto_place_pcb import _compact, _emit
from ..kicad.symbol_geom import load_symbol


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("pcb_gen", {}) or {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# s-expr helpers
# --------------------------------------------------------------------------- #

def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _sym(name: str) -> sexpdata.Symbol:
    return sexpdata.Symbol(name)


def _child(node: list, name: str) -> Optional[list]:
    for c in node[1:] if isinstance(node, list) else []:
        if isinstance(c, list) and _head(c) == name:
            return c
    return None


def _parse_kicad_mod(path: Path) -> Optional[list]:
    try:
        node = sexpdata.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AssertionError):
        return None
    return node if _head(node) == "footprint" else None


# --------------------------------------------------------------------------- #
# Footprint library resolution (fp-lib-table)
# --------------------------------------------------------------------------- #

def _subst_vars(uri: str, kiprjmod: str) -> str:
    out = uri.replace("${KIPRJMOD}", kiprjmod).replace("$(KIPRJMOD)", kiprjmod)
    # Resolve any remaining ${VAR} / $(VAR) from the environment so installs
    # that use ${KICAD9_FOOTPRINT_DIR} etc. still resolve when the env is set.
    for key, val in os.environ.items():
        out = out.replace("${" + key + "}", val).replace("$(" + key + ")", val)
    return out


def _fp_lib_dirs(cfg: Dict[str, Any], kiprjmod: str) -> Dict[str, Path]:
    """Map footprint-library nickname -> .pretty directory from fp-lib-table."""
    table_path = Path(cfg.get("fp_lib_table", "F:/Ki_CAD/fp-lib-table"))
    dirs: Dict[str, Path] = {}
    if not table_path.is_file():
        return dirs
    try:
        root = sexpdata.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, AssertionError):
        return dirs
    for lib in root[1:] if isinstance(root, list) else []:
        if not isinstance(lib, list) or _head(lib) != "lib":
            continue
        name_node = _child(lib, "name")
        uri_node = _child(lib, "uri")
        if not name_node or not uri_node or len(name_node) < 2 or len(uri_node) < 2:
            continue
        name = str(name_node[1])
        uri = _subst_vars(str(uri_node[1]), kiprjmod)
        dirs[name] = Path(uri)
    return dirs


def _find_kicad_mod(fpid: str, lib_dirs: Dict[str, Path],
                    lib_root: Path) -> Optional[Path]:
    """Resolve "Lib:Name" -> a .kicad_mod path. Tries the fp-lib-table dir
    first, then a flat <lib_root>/<Lib>.pretty/<Name>.kicad_mod fallback."""
    if ":" not in fpid:
        return None
    lib, name = fpid.split(":", 1)
    candidates: List[Path] = []
    if lib in lib_dirs:
        candidates.append(lib_dirs[lib] / f"{name}.kicad_mod")
    candidates.append(lib_root / f"{lib}.pretty" / f"{name}.kicad_mod")
    for c in candidates:
        if c.is_file():
            return c
    return None


# --------------------------------------------------------------------------- #
# IR -> net table + pin/pad net map
# --------------------------------------------------------------------------- #

def _build_net_map(ir: Any) -> Tuple[List[Tuple[int, str]],
                                     Dict[Tuple[str, str], Tuple[int, str]],
                                     List[str]]:
    """Return (net_table, pad_net_map, warnings).

      net_table   : [(0, ""), (1, name), ...] for the board's (net ...) block
      pad_net_map : {(ref, pad_number): (net_idx, net_name)}
      warnings    : human-readable notes for pins that couldn't be mapped
    """
    warnings: List[str] = []
    lib_of: Dict[str, str] = {c.ref: c.lib_id for c in ir.components}

    # Net 0 is the mandatory unconnected net. Real nets get 1..N. Identical
    # net names collapse onto one index (KiCad treats same-name as one net).
    name_to_idx: "OrderedDict[str, int]" = OrderedDict()
    net_table: List[Tuple[int, str]] = [(0, "")]
    pad_net_map: Dict[Tuple[str, str], Tuple[int, str]] = {}

    for net in ir.nets:
        nm = net.name
        if nm not in name_to_idx:
            idx = len(name_to_idx) + 1
            name_to_idx[nm] = idx
            net_table.append((idx, nm))
        idx = name_to_idx[nm]

        for pinref in net.pins:
            if "." not in pinref:
                warnings.append(f"net {nm}: malformed pin ref '{pinref}'")
                continue
            ref, pinkey = pinref.split(".", 1)
            lib_id = lib_of.get(ref)
            if not lib_id:
                warnings.append(f"net {nm}: pin '{pinref}' references unknown "
                                f"component {ref}")
                continue
            try:
                geom = load_symbol(lib_id)
                pin = geom.resolve_pin(pinkey)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"net {nm}: {ref} ({lib_id}) pin lookup failed: "
                                f"{type(exc).__name__}")
                continue
            if pin is None:
                warnings.append(f"net {nm}: pin '{pinref}' not found on {lib_id}")
                continue
            pad_net_map[(ref, pin.number)] = (idx, nm)

    return net_table, pad_net_map, warnings


# --------------------------------------------------------------------------- #
# Placement (prefix-grouped grid)
# --------------------------------------------------------------------------- #

def _prefix(ref: str) -> str:
    i = len(ref) - 1
    while i >= 0 and ref[i].isdigit():
        i -= 1
    return ref[: i + 1] if i >= 0 else ref


def _ref_sort_key(ref: str) -> Tuple[str, int]:
    pre = _prefix(ref)
    num = ref[len(pre):]
    return (pre, int(num) if num.isdigit() else 0)


def _grid_place(refs: List[str], cfg: Dict[str, Any]
                ) -> Dict[str, Tuple[float, float, float]]:
    origin_x = float(cfg.get("origin_x_mm", 30.0))
    origin_y = float(cfg.get("origin_y_mm", 30.0))
    pitch_x = float(cfg.get("pitch_x_mm", 12.7))
    pitch_y = float(cfg.get("pitch_y_mm", 12.7))
    cols = max(1, int(cfg.get("columns", 8)))

    groups: "OrderedDict[str, List[str]]" = OrderedDict()
    for ref in sorted(refs, key=_ref_sort_key):
        groups.setdefault(_prefix(ref), []).append(ref)

    placements: Dict[str, Tuple[float, float, float]] = {}
    row = 0
    for grp in groups.values():
        col = 0
        for ref in grp:
            placements[ref] = (round(origin_x + col * pitch_x, 4),
                               round(origin_y + row * pitch_y, 4), 0.0)
            col += 1
            if col >= cols:
                col = 0
                row += 1
        if col != 0:        # finish a partially-filled row before the next group
            row += 1
        row += 1            # blank band between prefix groups
    return placements


# --------------------------------------------------------------------------- #
# .kicad_mod -> board footprint node
# --------------------------------------------------------------------------- #

def _transform_footprint(fp: list, fpid: str, ref: str, value: str,
                         pos: Tuple[float, float, float],
                         pad_nets: Dict[str, Tuple[int, str]]) -> int:
    """Mutate a parsed (footprint ...) node into a board instance in place.
    Returns the number of pads that got a net assigned."""
    x, y, rot = pos

    # 1. Full library id as the footprint name.
    fp[1] = fpid

    # 2. Drop library-file-only metadata that boards don't carry per-footprint.
    fp[:] = [fp[0], fp[1]] + [c for c in fp[2:]
                              if _head(c) not in ("version", "generator")]

    # 3. Ensure (layer "F.Cu"); insert (uuid) + (at x y [rot]) right after it.
    layer = _child(fp, "layer")
    if layer is None:
        layer = [_sym("layer"), "F.Cu"]
        fp.insert(2, layer)
    insert_at = fp.index(layer) + 1
    at_node = [_sym("at"), x, y] + ([rot] if rot else [])
    fp.insert(insert_at, [_sym("uuid"), str(_uuid.uuid4())])
    fp.insert(insert_at + 1, at_node)

    # 4. Reference / Value properties.
    for prop in fp[1:]:
        if isinstance(prop, list) and _head(prop) == "property" and len(prop) >= 3:
            if str(prop[1]) == "Reference":
                prop[2] = ref
            elif str(prop[1]) == "Value":
                prop[2] = value

    # 5. Inject nets onto pads (by pad number == symbol pin number).
    assigned = 0
    for pad in fp[1:]:
        if not (isinstance(pad, list) and _head(pad) == "pad" and len(pad) >= 2):
            continue
        padnum = str(pad[1])
        net = pad_nets.get(padnum)
        if net is None:
            continue
        idx, nm = net
        if _child(pad, "net") is None:
            pad.append([_sym("net"), idx, nm])
            assigned += 1
    return assigned


# --------------------------------------------------------------------------- #
# Board header
# --------------------------------------------------------------------------- #

_LAYERS_BLOCK = (
    '\t(layers\n'
    '\t\t(0 "F.Cu" signal)\n'
    '\t\t(31 "B.Cu" signal)\n'
    '\t\t(32 "B.Adhes" user "B.Adhesive")\n'
    '\t\t(33 "F.Adhes" user "F.Adhesive")\n'
    '\t\t(34 "B.Paste" user)\n'
    '\t\t(35 "F.Paste" user)\n'
    '\t\t(36 "B.SilkS" user "B.Silkscreen")\n'
    '\t\t(37 "F.SilkS" user "F.Silkscreen")\n'
    '\t\t(38 "B.Mask" user)\n'
    '\t\t(39 "F.Mask" user)\n'
    '\t\t(40 "Dwgs.User" user "User.Drawings")\n'
    '\t\t(41 "Cmts.User" user "User.Comments")\n'
    '\t\t(42 "Eco1.User" user "User.Eco1")\n'
    '\t\t(43 "Eco2.User" user "User.Eco2")\n'
    '\t\t(44 "Edge.Cuts" user)\n'
    '\t\t(45 "Margin" user)\n'
    '\t\t(46 "B.CrtYd" user "B.Courtyard")\n'
    '\t\t(47 "F.CrtYd" user "F.Courtyard")\n'
    '\t\t(48 "B.Fab" user)\n'
    '\t\t(49 "F.Fab" user)\n'
    '\t)\n'
)


def _board_header(paper: str) -> str:
    return (
        '(kicad_pcb\n'
        '\t(version 20241229)\n'
        '\t(generator "envil_agent")\n'
        '\t(general\n'
        '\t\t(thickness 1.6)\n'
        '\t\t(legacy_teardrops no)\n'
        '\t)\n'
        f'\t(paper "{paper}")\n'
        + _LAYERS_BLOCK
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def generate_pcb_from_ir(ir: Any, sch_path: str,
                         out_path: Optional[str] = None) -> Dict[str, Any]:
    """Write a populated ``.kicad_pcb`` next to ``sch_path`` from the IR.

    Returns a report dict: ``{pcb_path, footprints_placed, footprints_missing,
    pads_netted, nets, warnings, error?}``. Never raises on per-part failures.
    """
    cfg = _load_cfg()
    sch = Path(sch_path)
    pcb = Path(out_path) if out_path else sch.with_suffix(".kicad_pcb")

    # ${KIPRJMOD} in a footprint table normally means the project dir; the
    # project-style fp-lib-table here roots libraries at the Ki_CAD base, so
    # default KIPRJMOD to that (config-overridable). Falls back to the project
    # folder when no base is configured.
    kiprjmod = str(cfg.get("kiprjmod_base") or "F:/Ki_CAD")
    lib_root = Path(cfg.get("lib_root", "F:/Ki_CAD/kicad-fp-lib"))
    paper = str(cfg.get("paper", "A4"))

    lib_dirs = _fp_lib_dirs(cfg, kiprjmod)
    net_table, pad_net_map, warnings = _build_net_map(ir)

    placements = _grid_place([c.ref for c in ir.components], cfg)

    # Resolve the engine's dynamic default footprint only when the IR left it blank.
    try:
        from ..intent.engine import _resolve_default_footprint
    except Exception:                                   # pragma: no cover
        _resolve_default_footprint = None  # type: ignore

    fp_blocks: List[str] = []
    missing: List[str] = []
    pads_netted = 0

    for c in ir.components:
        fpid = (c.footprint or "").strip()
        if not fpid and _resolve_default_footprint is not None:
            try:
                geom = load_symbol(c.lib_id)
            except Exception:                           # noqa: BLE001
                geom = None
            try:
                fpid = _resolve_default_footprint(c.lib_id, geom) or ""
            except Exception:                           # noqa: BLE001
                fpid = ""
        if not fpid:
            missing.append(f"{c.ref} ({c.lib_id}): no footprint")
            continue

        mod_path = _find_kicad_mod(fpid, lib_dirs, lib_root)
        if mod_path is None:
            missing.append(f"{c.ref}: footprint '{fpid}' not found on disk")
            continue
        fp = _parse_kicad_mod(mod_path)
        if fp is None:
            missing.append(f"{c.ref}: failed to parse '{mod_path.name}'")
            continue

        pad_nets = {padnum: net for (ref, padnum), net in pad_net_map.items()
                    if ref == c.ref}
        try:
            pads_netted += _transform_footprint(
                fp, fpid, c.ref, c.value,
                placements.get(c.ref, (30.0, 30.0, 0.0)), pad_nets)
            fp_blocks.append(_emit(fp, 1))
        except Exception as exc:                        # noqa: BLE001
            missing.append(f"{c.ref}: transform failed "
                           f"({type(exc).__name__}: {exc})")

    nets_block = "".join(f'\t(net {i} {_compact(nm)})\n' for i, nm in net_table)
    board = (_board_header(paper)
             + nets_block
             + ("\n".join(fp_blocks) + "\n" if fp_blocks else "")
             + ")\n")

    try:
        pcb.parent.mkdir(parents=True, exist_ok=True)
        pcb.write_text(board, encoding="utf-8")
    except OSError as exc:
        return {"error": f"write failed: {exc}", "pcb_path": str(pcb)}

    return {
        "pcb_path": str(pcb).replace("\\", "/"),
        "footprints_placed": len(fp_blocks),
        "footprints_missing": missing,
        "pads_netted": pads_netted,
        "nets": len(net_table) - 1,        # exclude net 0
        "warnings": warnings,
    }
