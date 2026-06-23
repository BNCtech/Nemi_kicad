import copy
import shutil
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

from ._config_loader import load as _load_config
from .geom import grid_mm as _grid_mm, snap as _snap  # canonical grid + snap

Sym = sexpdata.Symbol


def _sym(name: str) -> Sym:
    return Sym(name)


def _to_str(token) -> str:
    return token.value() if isinstance(token, Sym) else str(token)


def _head(node) -> Optional[str]:
    if isinstance(node, list) and node and isinstance(node[0], Sym):
        return node[0].value()
    return None


def _find_by_property(root: list, head: str, prop_name: str, prop_value: str) -> Optional[list]:
    for child in root[1:]:
        if not isinstance(child, list) or _head(child) != head:
            continue
        for sub in child[1:]:
            if (
                isinstance(sub, list)
                and _head(sub) == "property"
                and len(sub) >= 3
                and _to_str(sub[1]) == prop_name
                and _to_str(sub[2]) == prop_value
            ):
                return child
    return None


def _get_property(symbol_node: list, prop_name: str) -> Optional[list]:
    for sub in symbol_node[1:]:
        if (
            isinstance(sub, list)
            and _head(sub) == "property"
            and len(sub) >= 3
            and _to_str(sub[1]) == prop_name
        ):
            return sub
    return None


def _atom_inline(a) -> str:
    if isinstance(a, Sym):
        return a.value()
    if isinstance(a, bool):
        return "yes" if a else "no"
    if isinstance(a, str):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(a, float):
        if a.is_integer():
            return f"{a:.1f}"
        return f"{a:g}"
    return str(a)


def _is_atom_list(node) -> bool:
    return isinstance(node, list) and all(not isinstance(x, list) for x in node)


def _format_sexpr(node, indent: int = 0) -> str:
    if not isinstance(node, list):
        return _atom_inline(node)
    if not node:
        return "()"
    pad = "\t" * indent
    if _is_atom_list(node) and len(node) <= 8:
        return "(" + " ".join(_atom_inline(x) for x in node) + ")"
    head = _atom_inline(node[0])
    parts = [pad + "(" + head]
    inline_atoms = []
    rest = list(node[1:])
    while rest and not isinstance(rest[0], list):
        inline_atoms.append(_atom_inline(rest.pop(0)))
    if inline_atoms:
        parts[0] = pad + "(" + head + " " + " ".join(inline_atoms)
    for child in rest:
        parts.append(_format_sexpr(child, indent + 1))
    return "\n".join(parts) + "\n" + pad + ")"


def _gen_uuid_node() -> list:
    return [_sym("uuid"), str(_uuid.uuid4())]


def _make_at(x: float, y: float, rot: float = 0) -> list:
    return [_sym("at"), _snap(x), _snap(y), float(rot)]


def _make_at_xy(x: float, y: float) -> list:
    """Two-arg (at x y) — for junctions, which reject a rotation token."""
    return [_sym("at"), _snap(x), _snap(y)]


def _make_property(name: str, value: str, x: float, y: float, hide: bool = False) -> list:
    node = [
        _sym("property"),
        name,
        value,
        _make_at(x, y),
        [
            _sym("effects"),
            [_sym("font"), [_sym("size"), 1.27, 1.27]],
        ],
    ]
    if hide:
        node[-1].append(_sym("hide"))
    return node


class SchematicDocument:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        self._original_text = self.path.read_text(encoding="utf-8")
        self.tree = sexpdata.loads(self._original_text)
        if _head(self.tree) != "kicad_sch":
            raise ValueError(f"{path}: not a kicad_sch document")
        self._history: List[list] = []

    def _snapshot(self):
        self._history.append(copy.deepcopy(self.tree))

    def undo(self) -> bool:
        if not self._history:
            return False
        self.tree = self._history.pop()
        return True

    def save(self, out_path=None, backup: bool = True) -> Path:
        out = Path(out_path) if out_path else self.path
        if backup and out.exists():
            shutil.copy2(out, out.with_suffix(out.suffix + ".bak"))
        text = _format_sexpr(self.tree, 0).rstrip() + "\n"
        out.write_text(text, encoding="utf-8")
        return out

    def find_component(self, reference: str) -> Optional[list]:
        return _find_by_property(self.tree, "symbol", "Reference", reference)

    def list_components(self) -> List[Dict[str, Any]]:
        out = []
        for child in self.tree[1:]:
            if isinstance(child, list) and _head(child) == "symbol":
                ref = _get_property(child, "Reference")
                val = _get_property(child, "Value")
                lib = next(
                    (
                        _to_str(sub[1])
                        for sub in child[1:]
                        if isinstance(sub, list) and _head(sub) == "lib_id" and len(sub) > 1
                    ),
                    None,
                )
                if ref:
                    out.append(
                        {
                            "reference": _to_str(ref[2]),
                            "value": _to_str(val[2]) if val else None,
                            "lib_id": lib,
                        }
                    )
        return out

    def add_component(
        self,
        lib_id: str,
        reference: str,
        value: str,
        x: float,
        y: float,
        rotation: float = 0,
        footprint: str = "",
        hide_value: bool = False,
    ) -> Dict[str, Any]:
        if self.find_component(reference):
            return {"ok": False, "message": f"component {reference} already exists"}
        if self._component_at_position_exists(lib_id, x, y, value=value):
            return {"ok": True,
                    "message": f"component {lib_id}={value!r} @ (~{x:.1f},{y:.1f}) "
                               f"already exists nearby; skipped duplicate"}
        # Inline the lib_symbol drawing definition into the file's
        # (lib_symbols ...) block — without this, KiCad has no artwork for the
        # symbol and renders a blank "?" placeholder rectangle. If the lib_id
        # doesn't exist in the user's libraries at all, refuse the op so the
        # user knows to add/create the symbol rather than getting silent junk.
        from ._lib_symbol_cache import ensure_lib_symbols_for_doc
        resolution = ensure_lib_symbols_for_doc(
            self.tree, [lib_id], project_dir=self.path.parent
        )
        info = resolution.get(lib_id, {"resolved": lib_id, "status": "missing"})
        status = info["status"]
        resolved_lib_id = info["resolved"]
        if status == "missing":
            return {
                "ok": False,
                "needs_user": True,
                "lib_id": lib_id,
                "message": (
                    f"lib_id '{lib_id}' not found in your configured KiCad libraries. "
                    f"Add the symbol to a library that your sym-lib-table points at, "
                    f"or correct the lib_id, then re-run."
                ),
            }
        # Fuzzy-resolution alternates are intentionally NOT surfaced as an
        # ask-back here. The resolver already chose its best candidate; chat
        # apply commits silently and the user can correct via a follow-up
        # prompt if needed. See [[project-fuzzy-alternates-ask-first]] memory.

        # Power-port symbols (lib_id 'power:*' or '#'-prefixed refdes) bake the
        # rail name into their own graphic, so the auto-placed Reference (#PWR0n)
        # and Value text are pure clutter. KiCad's library ships both hidden;
        # hide them here at placement time so the schematic shows only the
        # rail label (+3V / +5V / GND) — and so LAY_032 has nothing to fix.
        cfg = _load_config("conventions")["power_symbol"]
        is_power = (
            any(resolved_lib_id.startswith(p) for p in cfg["lib_id_prefixes"])
            or any(reference.startswith(p) for p in cfg["reference_prefixes"])
        )
        hide_ref = is_power
        hide_val = hide_value or is_power

<<<<<<< Updated upstream
        if not footprint:
            from .footprint_resolver import resolve_footprint
            footprint = resolve_footprint(resolved_lib_id, value, reference)
=======
        # Power-port instances should terminate on a real pin tip or wire
        # endpoint for KiCad's same-name net merge to fire. We do a
        # best-effort snap here — within snap_search_mm, snap silently.
        # Beyond that, place as-is; the post-apply normalize pass will
        # snap/delete once all ops in this turn are applied (caps and
        # wires that would have been the missing intermediate endpoints
        # may not exist yet at this call site).
        if is_power:
            snap_cfg = _load_config("conventions").get("chat_snap", {})
            snap_tol = float(snap_cfg.get("snap_search_mm", 5.08))
            target = self._nearest_pin_or_wire_endpoint(x, y, snap_tol)
            if target is not None:
                x, y = target
>>>>>>> Stashed changes

        self._snapshot()
        sym = [
            _sym("symbol"),
            [_sym("lib_id"), resolved_lib_id],
            _make_at(x, y, rotation),
            [_sym("unit"), 1],
            [_sym("exclude_from_sim"), _sym("no")],
            [_sym("in_bom"), _sym("yes")],
            [_sym("on_board"), _sym("yes")],
            [_sym("dnp"), _sym("no")],
            _gen_uuid_node(),
            _make_property("Reference", reference, x + 2.54, y - 1.27, hide=hide_ref),
            _make_property("Value", value, x + 2.54, y + 1.27, hide=hide_val),
            _make_property("Footprint", footprint, x, y, hide=True),
            _make_property("Datasheet", "~", x, y, hide=True),
        ]
        # Provenance tagging (P9.1). When the resolver used the fallback chain
        # (fuzzy / capability / pin_compat / generic_placeholder) the placed
        # symbol IS NOT a verbatim catalogue part — it's the closest fit. Tag
        # the placed instance with the chain so downstream tools (ERC
        # filtering, BOM enrichment, audit logs) can distinguish auto-resolved
        # parts from exact matches without re-running the resolver.
        if status and status not in ("exact", "alias_remapped"):
            sym.append(_make_property("envil_provenance", str(status),
                                        x, y, hide=True))
            if resolved_lib_id != lib_id:
                sym.append(_make_property("envil_original_lib_id", str(lib_id),
                                            x, y, hide=True))
            conf = info.get("confidence")
            if conf is not None:
                sym.append(_make_property("envil_confidence", str(conf),
                                            x, y, hide=True))
        self.tree.append(sym)
        msg = f"added {reference} ({resolved_lib_id}) = {value} @ ({x},{y})"
        result: Dict[str, Any] = {"ok": True, "message": msg}
        # Always surface the resolver strategy so callers can aggregate stats
        # (exact / normalized / family_renamed / fuzzy / capability / pin_compat
        # / generic_placeholder). Confidence is present for the new Phase 3-5
        # layers; status alone is enough for the older layers.
        result["resolver_status"] = status
        if resolved_lib_id != lib_id:
            result["lib_id_requested"] = lib_id
            result["lib_id_resolved"] = resolved_lib_id
        if "confidence" in info:
            result["resolver_confidence"] = info["confidence"]
        if status == "fuzzy":
            result["warning"] = (
                f"'{lib_id}' not found in your libraries; used closest match "
                f"'{resolved_lib_id}'. Tell me if you want the original instead — "
                f"you'll need to add it to a library first."
            )
        elif status in ("capability", "pin_compat", "generic_placeholder"):
            conf = info.get("confidence")
            result["warning"] = (
                f"'{lib_id}' not in your libraries; resolver used "
                f"{status} fallback → '{resolved_lib_id}' "
                f"(confidence {conf})."
            )
        return result

    def delete_component(self, reference: str) -> Dict[str, Any]:
        node = self.find_component(reference)
        if not node:
            return {"ok": False, "message": f"component {reference} not found"}
        # Compute this component's world pin tip positions BEFORE removing —
        # so we can sweep wires/junctions/labels/no_connects that would be
        # left stranded. Without this, "remove X" leaves a forest of dangling
        # wires that ERC then complains about.
        pin_positions = self._world_pin_positions(node)
        self._snapshot()
        self.tree.remove(node)
        # Only sweep positions where NO OTHER live component still has a pin —
        # multi-unit ICs share pins across units, and shared power-rail pin
        # positions should not be wiped just because one consumer was deleted.
        other_set = {self._snap_xy(x, y) for (x, y) in self._all_world_pin_positions()}
        targets = [p for p in pin_positions if self._snap_xy(*p) not in other_set]
        swept = self._sweep_orphans_at(targets)
        msg = f"deleted {reference}"
        if swept:
            msg += f" (+ cleaned {swept} stranded item{'s' if swept != 1 else ''})"
        return {"ok": True, "message": msg}

    # ------------------------------------------------------------------
    # Pin geometry helpers (used by delete_component auto-cleanup)
    # ------------------------------------------------------------------

    @staticmethod
    def _snap_xy(x: float, y: float, tol: float = 0.05) -> Tuple[int, int]:
        return (int(round(x / tol)), int(round(y / tol)))

    @staticmethod
    def _rot_xy(x: float, y: float, deg: float) -> Tuple[float, float]:
        q = (int(round(deg)) % 360) // 90
        for _ in range(q):
            x, y = -y, x
        return (x, y)

    def _lib_symbol_node(self, lib_id: str) -> Optional[list]:
        for child in self.tree[1:]:
            if isinstance(child, list) and _head(child) == "lib_symbols":
                for sym in child[1:]:
                    if (
                        isinstance(sym, list)
                        and _head(sym) == "symbol"
                        and len(sym) > 1
                        and _to_str(sym[1]) == lib_id
                    ):
                        return sym
        return None

    def _pin_defs_for_unit(self, lib_sym: list, unit: int) -> List[Tuple[float, float]]:
        """Return symbol-local (px, py) for every pin in unit `unit` plus unit 0
        (unit 0 holds shared pins like VCC/GND on multi-unit ICs)."""
        out: List[Tuple[float, float]] = []
        for sub in lib_sym[1:]:
            if not (isinstance(sub, list) and _head(sub) == "symbol"):
                continue
            sub_name = _to_str(sub[1]) if len(sub) > 1 else ""
            parts = sub_name.rsplit("_", 2)
            sub_unit = 0
            if len(parts) == 3:
                try:
                    sub_unit = int(parts[1])
                except ValueError:
                    sub_unit = 0
            if sub_unit not in (0, unit):
                continue
            for pin in sub[1:]:
                if not (isinstance(pin, list) and _head(pin) == "pin"):
                    continue
                for tag in pin[1:]:
                    if isinstance(tag, list) and _head(tag) == "at" and len(tag) >= 3:
                        out.append((float(tag[1]), float(tag[2])))
                        break
        return out

    def _world_pin_positions(self, comp_node: list) -> List[Tuple[float, float]]:
        cx = cy = 0.0
        crot = 0.0
        lib_id = ""
        unit = 1
        for sub in comp_node[1:]:
            if not isinstance(sub, list):
                continue
            tag = _head(sub)
            if tag == "at" and len(sub) >= 3:
                cx = float(sub[1]); cy = float(sub[2])
                if len(sub) > 3:
                    crot = float(sub[3])
            elif tag == "lib_id" and len(sub) > 1:
                lib_id = _to_str(sub[1])
            elif tag == "unit" and len(sub) > 1:
                try:
                    unit = int(sub[1])
                except (TypeError, ValueError):
                    unit = 1
        if not lib_id:
            # Power flag / no-symbol nodes still occupy a position — use `at`.
            return [(cx, cy)] if (cx or cy) else []
        lib_sym = self._lib_symbol_node(lib_id)
        if not lib_sym:
            return [(cx, cy)] if (cx or cy) else []
        out: List[Tuple[float, float]] = []
        for (px, py) in self._pin_defs_for_unit(lib_sym, unit):
            rx, ry = self._rot_xy(px, py, crot)
            out.append((cx + rx, cy - ry))
        if not out:
            out.append((cx, cy))
        return out

    def _all_world_pin_positions(self) -> List[Tuple[float, float]]:
        out: List[Tuple[float, float]] = []
        for child in self.tree[1:]:
            if isinstance(child, list) and _head(child) == "symbol":
                out.extend(self._world_pin_positions(child))
        return out

    def _sweep_orphans_at(self, positions: List[Tuple[float, float]]) -> int:
        """Remove wires/junctions/labels/no_connects whose anchor sits at one
        of the given positions (within snap tolerance). Wires are removed if
        EITHER endpoint matches. Returns number of nodes removed."""
        if not positions:
            return 0
        targets = {self._snap_xy(x, y) for (x, y) in positions}
        to_remove: List[list] = []
        for child in self.tree[1:]:
            if not isinstance(child, list):
                continue
            tag = _head(child)
            if tag == "wire":
                pts = self._wire_pts(child)
                if any(self._snap_xy(x, y) in targets for (x, y) in pts):
                    to_remove.append(child)
            elif tag in ("junction", "no_connect"):
                for sub in child[1:]:
                    if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                        if self._snap_xy(float(sub[1]), float(sub[2])) in targets:
                            to_remove.append(child)
                        break
            elif tag in ("label", "global_label", "hierarchical_label"):
                for sub in child[1:]:
                    if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                        if self._snap_xy(float(sub[1]), float(sub[2])) in targets:
                            to_remove.append(child)
                        break
        for n in to_remove:
            try:
                self.tree.remove(n)
            except ValueError:
                pass
        return len(to_remove)

    def edit_value(self, reference: str, new_value: str) -> Dict[str, Any]:
        node = self.find_component(reference)
        if not node:
            return {"ok": False, "message": f"component {reference} not found"}
        prop = _get_property(node, "Value")
        if not prop:
            return {"ok": False, "message": f"{reference}: Value property missing"}
        self._snapshot()
        prop[2] = new_value
        return {"ok": True, "message": f"{reference}: value -> {new_value}"}

    def hide_property(self, reference: str, property_name: str,
                      hidden: bool = True) -> Dict[str, Any]:
        """Set or clear the hide flag on one property of a placed symbol.

        Used by LAY_032 to silence the auto-generated #PWR refdes / value text
        that KiCad considers visible by default in some libraries. Idempotent
        — calling twice with the same value is a no-op."""
        node = self.find_component(reference)
        if not node:
            return {"ok": False, "message": f"component {reference} not found"}
        prop = _get_property(node, property_name)
        if not prop:
            return {"ok": False, "message": f"{reference}: property '{property_name}' missing"}

        effects = None
        for sub in prop[1:]:
            if isinstance(sub, list) and _head(sub) == "effects":
                effects = sub
                break
        if effects is None:
            effects = [_sym("effects"), [_sym("font"), [_sym("size"), 1.27, 1.27]]]
            prop.append(effects)

        existing_idx = None
        for i, eff in enumerate(effects[1:], start=1):
            if isinstance(eff, list) and eff and _head(eff) == "hide":
                existing_idx = i
                break
            if isinstance(eff, Sym) and eff.value() == "hide":
                existing_idx = i
                break

        was_hidden = existing_idx is not None
        if hidden and was_hidden:
            return {"ok": True, "message": f"{reference}.{property_name}: already hidden"}
        if not hidden and not was_hidden:
            return {"ok": True, "message": f"{reference}.{property_name}: already visible"}

        self._snapshot()
        if hidden:
            effects.append([_sym("hide"), _sym("yes")])
        else:
            effects.pop(existing_idx)
        verb = "hidden" if hidden else "shown"
        return {"ok": True, "message": f"{reference}.{property_name}: {verb}"}

    def move_component(self, reference: str, x: float, y: float, rotation: Optional[float] = None) -> Dict[str, Any]:
        node = self.find_component(reference)
        if not node:
            return {"ok": False, "message": f"component {reference} not found"}
        self._snapshot()
        for i, sub in enumerate(node[1:], start=1):
            if isinstance(sub, list) and _head(sub) == "at":
                rot = rotation if rotation is not None else (float(sub[3]) if len(sub) > 3 else 0)
                node[i] = _make_at(x, y, rot)
                return {"ok": True, "message": f"{reference}: moved to ({x},{y})"}
        return {"ok": False, "message": f"{reference}: at-block missing"}

    def dedup_collinear_wires(self) -> int:
        """Drop every wire whose axis-aligned interval is FULLY CONTAINED
        inside another wire on the same axis at the same perpendicular
        coordinate. Two wires share axis + perp value (e.g. both horizontal
        at y=50.8) and one's [x_lo, x_hi] ⊆ the other's [x_lo, x_hi] → the
        contained wire is redundant; KiCad treats the longer wire as the
        single conductor across that span anyway, regardless of net
        assignment, because they're the same geometric segment.

        Containment-only by design: partial overlaps (neither contains the
        other) are LEFT ALONE — they could belong to distinct nets that
        happen to share start/end on one axis, and merging would short
        them. The label_placer's same-net merge already handles that case
        upstream where net identity is known.

        Returns count of wires dropped."""
        wires: List[Tuple[int, str, float, float, float]] = []
        for idx, child in enumerate(self.tree[1:], start=1):
            if not (isinstance(child, list) and _head(child) == "wire"):
                continue
            pts = self._wire_pts(child)
            if len(pts) != 2:
                continue
            (x1, y1), (x2, y2) = pts[0], pts[1]
            if abs(y1 - y2) < 1e-3:
                axis = "h"
                perp = round(y1, 3)
                lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
            elif abs(x1 - x2) < 1e-3:
                axis = "v"
                perp = round(x1, 3)
                lo, hi = (y1, y2) if y1 <= y2 else (y2, y1)
            else:
                continue  # diagonal — out of scope
            wires.append((idx, axis, perp, lo, hi))

        drop: set = set()
        # O(N^2) — N is the wire count per sheet, typically < 500. The
        # spatial-hash speedup isn't worth the code for this many wires.
        for i, (idx_a, ax_a, p_a, lo_a, hi_a) in enumerate(wires):
            if idx_a in drop:
                continue
            for j, (idx_b, ax_b, p_b, lo_b, hi_b) in enumerate(wires):
                if i == j or idx_b in drop:
                    continue
                if ax_a != ax_b or abs(p_a - p_b) > 1e-3:
                    continue
                # B contains A?
                if lo_b - 1e-3 <= lo_a and hi_a <= hi_b + 1e-3:
                    # Skip exact-duplicate symmetric drop: only kill A if B
                    # is strictly larger, OR (equal length) only the higher
                    # index drops to keep determinism.
                    if (hi_b - lo_b) > (hi_a - lo_a) + 1e-3 or idx_a > idx_b:
                        drop.add(idx_a)
                        break
        if not drop:
            return 0
        self._snapshot()
        self.tree[:] = [c for k, c in enumerate(self.tree)
                        if k not in drop]
        return len(drop)

    def _wire_exists(self, p1: Tuple[float, float], p2: Tuple[float, float]) -> bool:
        """True iff a wire with these two endpoints (in either order) already
        exists at the snapped key. Same key collision = real duplicate."""
        a = self._snap_xy(_snap(p1[0]), _snap(p1[1]))
        b = self._snap_xy(_snap(p2[0]), _snap(p2[1]))
        if a == b:
            return True
        target = frozenset((a, b))
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "wire"):
                continue
            pts = self._wire_pts(child)
            if len(pts) < 2:
                continue
            ka = self._snap_xy(pts[0][0], pts[0][1])
            kb = self._snap_xy(pts[-1][0], pts[-1][1])
            if frozenset((ka, kb)) == target:
                return True
        return False

    def _label_exists(self, name: str, x: float, y: float, kind: str) -> bool:
        key = self._snap_xy(_snap(x), _snap(y))
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == kind):
                continue
            if len(child) < 3 or _to_str(child[1]) != name:
                continue
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    if self._snap_xy(float(sub[1]), float(sub[2])) == key:
                        return True
        return False

    def _junction_exists(self, x: float, y: float) -> bool:
        key = self._snap_xy(_snap(x), _snap(y))
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "junction"):
                continue
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    if self._snap_xy(float(sub[1]), float(sub[2])) == key:
                        return True
        return False

    def _component_at_position_exists(self, lib_id: str, x: float, y: float,
                                       value: str = "",
                                       proximity_mm: float = 5.08) -> bool:
        """True if a component with the same lib_id is already placed within
        `proximity_mm` of (x, y). When `value` is provided AND the candidate
        is a passive (R/C/L/D), the value must also match — this stops the
        check from collapsing two genuinely-different decoupling caps (100n
        and 10u) that happen to be near each other on a regulator output.

        Why proximity not exact-match: when the user re-prompts the same
        circuit on the same page, the LLM picks fresh refdes (R3 instead
        of R1) AND often shifts coordinates by 1-2 mm so neither the refdes
        check nor an exact-position check fires — duplicates ship. A 5 mm
        proximity radius (~2 grid units) catches the re-prompt case
        without colliding with legitimate dense layouts (decoupling caps
        in adjacent VDD columns sit ≥ 5.08 mm apart).
        """
        prox_sq = proximity_mm * proximity_mm
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "symbol"):
                continue
            comp_lib = None
            comp_xy = None
            comp_value = ""
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "lib_id" and len(sub) > 1:
                    comp_lib = _to_str(sub[1])
                elif isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    try:
                        comp_xy = (float(sub[1]), float(sub[2]))
                    except (TypeError, ValueError):
                        comp_xy = None
                elif isinstance(sub, list) and _head(sub) == "property" and len(sub) >= 3:
                    if _to_str(sub[1]) == "Value":
                        comp_value = _to_str(sub[2])
            if comp_lib != lib_id or comp_xy is None:
                continue
            dx = comp_xy[0] - x
            dy = comp_xy[1] - y
            if dx * dx + dy * dy > prox_sq:
                continue
            # For passives (R/C/L/D from Device:* or similar), require value
            # match too — two different-value parts close together is a real
            # layout pattern, not a duplicate.
            lib_low = (lib_id or "").lower()
            is_passive = any(p in lib_low for p in ("device:r", "device:c", "device:l",
                                                     "device:d", ":r_", ":c_", ":l_"))
            if is_passive and value and comp_value and comp_value != value:
                continue
            return True
        return False

    def add_wire(self, points: List[Tuple[float, float]]) -> Dict[str, Any]:
        if len(points) < 2:
            return {"ok": False, "message": "wire needs at least 2 points"}
        if len(points) == 2 and self._wire_exists(points[0], points[1]):
            return {"ok": True, "message": f"wire {points[0]}<->{points[1]} already exists; skipped duplicate"}
        # KiCad's parser requires each (wire ...) sexp to contain exactly TWO
        # (xy) points. A 3+ point path (e.g. an L-shape) must be expressed as
        # multiple (wire) sexps, one per segment. Without this split the file
        # fails to reload with "Expecting ')'".
        if len(points) > 2:
            self._snapshot()
            added = 0
            skipped = 0
            for i in range(len(points) - 1):
                p1 = points[i]; p2 = points[i + 1]
                if self._wire_exists(p1, p2):
                    skipped += 1
                    continue
                seg = [
                    _sym("wire"),
                    [_sym("pts"), [_sym("xy"), _snap(p1[0]), _snap(p1[1])],
                                  [_sym("xy"), _snap(p2[0]), _snap(p2[1])]],
                    [
                        _sym("stroke"),
                        [_sym("width"), 0],
                        [_sym("type"), _sym("default")],
                    ],
                    _gen_uuid_node(),
                ]
                self.tree.append(seg)
                added += 1
            msg = f"added {added} wire segment(s) along {len(points)} points"
            if skipped:
                msg += f" (skipped {skipped} duplicate segment(s))"
            return {"ok": True, "message": msg}
        self._snapshot()
        node = [
            _sym("wire"),
            [_sym("pts")] + [[_sym("xy"), _snap(x), _snap(y)] for x, y in points],
            [
                _sym("stroke"),
                [_sym("width"), 0],
                [_sym("type"), _sym("default")],
            ],
            _gen_uuid_node(),
        ]
        self.tree.append(node)
        return {"ok": True, "message": f"added wire with {len(points)} points"}

    def _nearest_pin_or_wire_endpoint(
        self, x: float, y: float, max_mm: float
    ) -> Optional[Tuple[float, float]]:
        """Return the world (x,y) of the closest pin tip or wire endpoint
        within max_mm of (x,y). None if nothing in range.

        Used to silently snap chat-emitted labels and power-port anchors
        onto real connection points so KiCad's net-merge by coordinate
        actually fires. Without this, every label Claude places "near" a
        pin renders as `(no net)` in eeschema."""
        candidates: List[Tuple[float, float]] = list(self._all_world_pin_positions())
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "wire"):
                continue
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "pts":
                    for xy in sub[1:]:
                        if (isinstance(xy, list) and _head(xy) == "xy"
                                and len(xy) >= 3):
                            candidates.append((float(xy[1]), float(xy[2])))
        best: Optional[Tuple[float, float]] = None
        best_d2 = (max_mm + 0.01) ** 2
        for (cx, cy) in candidates:
            d2 = (cx - x) ** 2 + (cy - y) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best = (cx, cy)
        return best

    def add_label(self, name: str, x: float, y: float, kind: str = "label") -> Dict[str, Any]:
        if kind not in ("label", "global_label", "hierarchical_label"):
            return {"ok": False, "message": f"unknown label kind: {kind}"}
<<<<<<< Updated upstream
        if self._label_exists(name, x, y, kind):
            return {"ok": True, "message": f"{kind} '{name}' @ ({x},{y}) already exists; skipped duplicate"}
=======
        # Best-effort snap to nearest pin tip / wire endpoint so port-name
        # merging fires. If nothing is within snap_search_mm at this call
        # site, place as-is — the post-apply normalize pass will snap or
        # delete once every op in this turn is applied (the connecting
        # wire/cap may be queued for a later op in the same batch).
        snap_cfg = _load_config("conventions").get("chat_snap", {})
        snap_tol = float(snap_cfg.get("snap_search_mm", 5.08))
        snap_note = ""
        target = self._nearest_pin_or_wire_endpoint(x, y, snap_tol)
        if target is not None:
            d = ((target[0] - x) ** 2 + (target[1] - y) ** 2) ** 0.5
            if d > 0.01:
                snap_note = (f" (snapped from ({x:.2f},{y:.2f}) to "
                             f"({target[0]:.2f},{target[1]:.2f}); "
                             f"Δ={d:.2f}mm)")
            x, y = target
        if self._label_exists(name, x, y, kind):
            return {"ok": True, "message": f"{kind} '{name}' @ ({x},{y}) already exists; skipped duplicate{snap_note}"}
>>>>>>> Stashed changes
        self._snapshot()
        node = [
            _sym(kind),
            name,
            _make_at(x, y, 0),
            [_sym("fields_autoplaced")],
            [
                _sym("effects"),
                [_sym("font"), [_sym("size"), 1.27, 1.27]],
                [_sym("justify"), _sym("left"), _sym("bottom")],
            ],
            _gen_uuid_node(),
        ]
        self.tree.append(node)
        return {"ok": True, "message": f"added {kind} '{name}' @ ({x},{y}){snap_note}"}

    def add_junction(self, x: float, y: float) -> Dict[str, Any]:
        if self._junction_exists(x, y):
            return {"ok": True, "message": f"junction @ ({x},{y}) already exists; skipped duplicate"}
        self._snapshot()
        # KiCad's parser accepts (at x y rot) for symbols/labels but rejects
        # the rotation token inside (junction ...) — use the 2-arg form here
        # or the file fails to reload with "Expecting ')'".
        node = [
            _sym("junction"),
            _make_at_xy(x, y),
            [_sym("diameter"), 0],
            [_sym("color"), 0, 0, 0, 0],
            _gen_uuid_node(),
        ]
        self.tree.append(node)
        return {"ok": True, "message": f"added junction @ ({x},{y})"}

    def _wire_pts(self, wire_node: list) -> List[Tuple[float, float]]:
        for sub in wire_node[1:]:
            if isinstance(sub, list) and _head(sub) == "pts":
                pts = []
                for pt in sub[1:]:
                    if isinstance(pt, list) and _head(pt) == "xy" and len(pt) >= 3:
                        pts.append((float(pt[1]), float(pt[2])))
                return pts
        return []

    def _find_wire_by_endpoints(
        self, p1: Tuple[float, float], p2: Tuple[float, float], tol: float = 0.05
    ) -> Optional[list]:
        """Return the first (wire ...) node whose endpoints match (p1, p2) in
        either order, within tol mm. Used by delete_wire / move_wire_endpoint."""
        def _close(a, b):
            return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "wire"):
                continue
            pts = self._wire_pts(child)
            if len(pts) < 2:
                continue
            a, b = pts[0], pts[-1]
            if (_close(a, p1) and _close(b, p2)) or (_close(a, p2) and _close(b, p1)):
                return child
        return None

    def delete_wire(
        self, p1: Tuple[float, float], p2: Tuple[float, float]
    ) -> Dict[str, Any]:
        """Remove the wire whose endpoints match (p1, p2). Use this to clean
        duplicate / overlapping wires the AI doesn't want anymore."""
        node = self._find_wire_by_endpoints(tuple(p1), tuple(p2))
        if not node:
            return {"ok": False, "message": f"no wire found with endpoints {p1} <-> {p2}"}
        self._snapshot()
        self.tree.remove(node)
        return {"ok": True, "message": f"deleted wire {p1} <-> {p2}"}

    def move_wire_endpoint(
        self,
        from_point: Tuple[float, float],
        to_point: Tuple[float, float],
        tol: float = 0.05,
    ) -> Dict[str, Any]:
        """Move every wire endpoint within tol mm of from_point to to_point.
        Used to snap off-grid endpoints to grid without rebuilding the wire."""
        moved = 0
        snapshotted = False
        from_t = (float(from_point[0]), float(from_point[1]))
        to_t = (_snap(to_point[0]), _snap(to_point[1]))
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "wire"):
                continue
            for sub in child[1:]:
                if not (isinstance(sub, list) and _head(sub) == "pts"):
                    continue
                for k, pt in enumerate(sub[1:], start=1):
                    if not (isinstance(pt, list) and _head(pt) == "xy" and len(pt) >= 3):
                        continue
                    px, py = float(pt[1]), float(pt[2])
                    if abs(px - from_t[0]) <= tol and abs(py - from_t[1]) <= tol:
                        if not snapshotted:
                            self._snapshot()
                            snapshotted = True
                        sub[k] = [_sym("xy"), to_t[0], to_t[1]]
                        moved += 1
        if moved == 0:
            return {"ok": False, "message": f"no wire endpoint near {from_point}"}
        return {"ok": True, "message": f"moved {moved} wire endpoint(s) {from_point} -> {to_point}"}

    def delete_junction(self, x: float, y: float, tol: float = 0.05) -> Dict[str, Any]:
        """Remove a junction at (x, y) — counterpart to add_junction."""
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "junction"):
                continue
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    if abs(float(sub[1]) - x) <= tol and abs(float(sub[2]) - y) <= tol:
                        self._snapshot()
                        self.tree.remove(child)
                        return {"ok": True, "message": f"deleted junction @ ({x},{y})"}
        return {"ok": False, "message": f"no junction found near ({x},{y})"}

    def _no_connect_exists(self, x: float, y: float, tol: float = 0.05) -> bool:
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "no_connect"):
                continue
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    if abs(float(sub[1]) - x) <= tol and abs(float(sub[2]) - y) <= tol:
                        return True
        return False

    def add_no_connect(self, x: float, y: float) -> Dict[str, Any]:
        """Place a no_connect (X) marker at a pin tip, per LAY_022 / CON_002.
        KiCad ERC requires every intentionally unused IC pin to carry one of
        these or be tied to a net — without it, ERC flags 'unconnected pin'
        and the reviewer can't tell 'forgotten' from 'reviewed-unused'.
        Same 2-arg (at x y) form as junctions — adding a rotation token here
        makes KiCad's parser reject the file with 'Expecting )'.
        """
        if self._no_connect_exists(x, y):
            return {"ok": True, "message": f"no_connect @ ({x},{y}) already exists; skipped duplicate"}
        self._snapshot()
        node = [
            _sym("no_connect"),
            _make_at_xy(x, y),
            _gen_uuid_node(),
        ]
        self.tree.append(node)
        return {"ok": True, "message": f"added no_connect @ ({x},{y})"}

    def delete_no_connect(self, x: float, y: float, tol: float = 0.05) -> Dict[str, Any]:
        """Remove a no_connect marker at (x, y) — counterpart to add_no_connect."""
        for child in self.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "no_connect"):
                continue
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    if abs(float(sub[1]) - x) <= tol and abs(float(sub[2]) - y) <= tol:
                        self._snapshot()
                        self.tree.remove(child)
                        return {"ok": True, "message": f"deleted no_connect @ ({x},{y})"}
        return {"ok": False, "message": f"no no_connect found near ({x},{y})"}


OPERATION_HANDLERS = {
    "add_component":     "add_component",
    "delete_component":  "delete_component",
    "edit_value":        "edit_value",
    "hide_property":     "hide_property",
    "move_component":    "move_component",
    "add_wire":          "add_wire",
    "delete_wire":       "delete_wire",
    "move_wire_endpoint":"move_wire_endpoint",
    "add_label":         "add_label",
    "add_junction":      "add_junction",
    "delete_junction":   "delete_junction",
    "add_no_connect":    "add_no_connect",
    "delete_no_connect": "delete_no_connect",
}


def apply_operation(doc: SchematicDocument, op: Dict[str, Any]) -> Dict[str, Any]:
    name = op.get("op") or op.get("type")
    if name not in OPERATION_HANDLERS:
        return {"ok": False, "message": f"unknown op: {name}"}
    args = {k: v for k, v in op.items() if k not in ("op", "type")}
    method = getattr(doc, OPERATION_HANDLERS[name])
    try:
        return method(**args)
    except TypeError as e:
        return {"ok": False, "message": f"{name}: bad arguments: {e}"}
