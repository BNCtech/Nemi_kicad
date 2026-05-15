import copy
import shutil
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

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
    return [_sym("at"), float(x), float(y), float(rot)]


def _make_at_xy(x: float, y: float) -> list:
    """Two-arg (at x y) — for junctions, which reject a rotation token."""
    return [_sym("at"), float(x), float(y)]


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
    ) -> Dict[str, Any]:
        if self.find_component(reference):
            return {"ok": False, "message": f"component {reference} already exists"}
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
            _make_property("Reference", reference, x + 2.54, y - 1.27),
            _make_property("Value", value, x + 2.54, y + 1.27),
            _make_property("Footprint", footprint, x, y, hide=True),
            _make_property("Datasheet", "~", x, y, hide=True),
        ]
        self.tree.append(sym)
        msg = f"added {reference} ({resolved_lib_id}) = {value} @ ({x},{y})"
        result: Dict[str, Any] = {"ok": True, "message": msg}
        if status == "fuzzy":
            result["warning"] = (
                f"'{lib_id}' not found in your libraries; used closest match "
                f"'{resolved_lib_id}'. Tell me if you want the original instead — "
                f"you'll need to add it to a library first."
            )
            result["lib_id_requested"] = lib_id
            result["lib_id_resolved"] = resolved_lib_id
        return result

    def delete_component(self, reference: str) -> Dict[str, Any]:
        node = self.find_component(reference)
        if not node:
            return {"ok": False, "message": f"component {reference} not found"}
        self._snapshot()
        self.tree.remove(node)
        return {"ok": True, "message": f"deleted {reference}"}

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

    def add_wire(self, points: List[Tuple[float, float]]) -> Dict[str, Any]:
        if len(points) < 2:
            return {"ok": False, "message": "wire needs at least 2 points"}
        # KiCad's parser requires each (wire ...) sexp to contain exactly TWO
        # (xy) points. A 3+ point path (e.g. an L-shape) must be expressed as
        # multiple (wire) sexps, one per segment. Without this split the file
        # fails to reload with "Expecting ')'".
        if len(points) > 2:
            self._snapshot()
            for i in range(len(points) - 1):
                p1 = points[i]; p2 = points[i + 1]
                seg = [
                    _sym("wire"),
                    [_sym("pts"), [_sym("xy"), float(p1[0]), float(p1[1])],
                                  [_sym("xy"), float(p2[0]), float(p2[1])]],
                    [
                        _sym("stroke"),
                        [_sym("width"), 0],
                        [_sym("type"), _sym("default")],
                    ],
                    _gen_uuid_node(),
                ]
                self.tree.append(seg)
            return {"ok": True, "message": f"added {len(points)-1} wire segment(s) along {len(points)} points"}
        self._snapshot()
        node = [
            _sym("wire"),
            [_sym("pts")] + [[_sym("xy"), float(x), float(y)] for x, y in points],
            [
                _sym("stroke"),
                [_sym("width"), 0],
                [_sym("type"), _sym("default")],
            ],
            _gen_uuid_node(),
        ]
        self.tree.append(node)
        return {"ok": True, "message": f"added wire with {len(points)} points"}

    def add_label(self, name: str, x: float, y: float, kind: str = "label") -> Dict[str, Any]:
        if kind not in ("label", "global_label", "hierarchical_label"):
            return {"ok": False, "message": f"unknown label kind: {kind}"}
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
        return {"ok": True, "message": f"added {kind} '{name}' @ ({x},{y})"}

    def add_junction(self, x: float, y: float) -> Dict[str, Any]:
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
        to_t = (float(to_point[0]), float(to_point[1]))
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


OPERATION_HANDLERS = {
    "add_component":     "add_component",
    "delete_component":  "delete_component",
    "edit_value":        "edit_value",
    "move_component":    "move_component",
    "add_wire":          "add_wire",
    "delete_wire":       "delete_wire",
    "move_wire_endpoint":"move_wire_endpoint",
    "add_label":         "add_label",
    "add_junction":      "add_junction",
    "delete_junction":   "delete_junction",
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
