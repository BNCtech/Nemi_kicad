"""Load, mutate, and write back a .kicad_sym file.

Supports both the symdir layout (one symbol per .kicad_sym file inside a
<nick>.kicad_symdir/ folder) and the flat layout (one kicad_symbol_lib
file containing many symbols).

Call :func:`apply_symbol_ops` with a ``lib_id`` like ``"Timer:NE555"``
and a list of operation dicts.  Each op is applied in order; any that
succeed cause the file to be written back and the ``load_symbol`` LRU
cache to be invalidated so the rest of the agent reflects the change.

Supported ops
-------------
rename_pin          {"op": "rename_pin",         "number": "3", "new_name": "PGND"}
rename_pin_number   {"op": "rename_pin_number",   "old_number": "3", "new_number": "9"}
change_pin_etype    {"op": "change_pin_etype",    "number": "3", "etype": "power_in"}
move_pin            {"op": "move_pin",             "number": "3", "x": 0, "y": -5.08, "rot": 270}
change_pin_length   {"op": "change_pin_length",   "number": "3", "length": 2.54}
set_property        {"op": "set_property",         "key": "MPN", "value": "LM555CN"}
add_pin             {"op": "add_pin", "number": "9", "name": "NC", "etype": "no_connect",
                     "x": 0, "y": -7.62, "rot": 270, "length": 2.54}
remove_pin          {"op": "remove_pin",           "number": "3"}   # or "name": "GND"

KLC pin dimension rules (applied automatically on add_pin and move_pin)
-----------------------------------------------------------------------
KLC S4.1  x, y positions are snapped to the nearest 50 mil (1.27 mm) grid.
          This matches the through-hole / SMD pin pitch on component datasheets
          and ensures wires land on KiCad's schematic grid without a jump.
KLC S4.2  Pin length defaults to 100 mil (2.54 mm) for all normal pins.
          50 mil (1.27 mm) is allowed only for hidden power pins
          (etype power_in / power_out). Values below 1.27 mm are rejected.
KLC S4.3  Rotation is snapped to the nearest multiple of 90°
          (0°, 90°, 180°, 270°). Diagonal pins are not permitted.
Any automatic correction is reported in the op result message.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

from .symbol_geom import _load_aliases, _sym_roots, load_symbol


# ---------------------------------------------------------------------------
# Internal sexpdata helpers (kept local — avoid coupling to apply_ops.py)
# ---------------------------------------------------------------------------

def _head(node: Any) -> Optional[str]:
    """Return the keyword name of an s-expression node, or None."""
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _first_child(node: list, name: str) -> Optional[list]:
    """Return the first direct child list whose head equals ``name``."""
    if not isinstance(node, list):
        return None
    for child in node[1:]:
        if isinstance(child, list) and _head(child) == name:
            return child
    return None


# ---------------------------------------------------------------------------
# Compact serializer — mirrors _compact in apply_ops.py
# ---------------------------------------------------------------------------

def _compact(node: Any) -> str:
    """One-line s-expression serializer.  Preserves int vs float so KiCad
    version fields (integers) are never promoted to floats."""
    if isinstance(node, list):
        return "(" + " ".join(_compact(c) for c in node) + ")"
    if isinstance(node, sexpdata.Symbol):
        return node.value()
    if isinstance(node, str):
        escaped = node.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return '"' + escaped + '"'
    if isinstance(node, bool):
        return "yes" if node else "no"
    if isinstance(node, int):
        return str(node)
    if isinstance(node, float):
        if abs(node - round(node)) < 1e-9:
            return f"{node:.1f}"
        return f"{node:.10f}".rstrip("0").rstrip(".")
    return str(node)


# ---------------------------------------------------------------------------
# KiCad-style symbol-file formatter
# ---------------------------------------------------------------------------

# Node heads whose children are each placed on their own indented line.
_BLOCK_HEADS = frozenset({"kicad_symbol_lib", "symbol"})


def _fmt_sym(node: Any, depth: int = 0) -> str:
    """Render `node` to a KiCad-indented string.

    ``kicad_symbol_lib`` and ``symbol`` blocks are multi-line; everything
    else (pins, properties, graphics) is emitted on a single line so
    kicad-cli parses them cleanly.
    """
    if not isinstance(node, list):
        return "  " * depth + _compact(node)

    head = _head(node)
    if head not in _BLOCK_HEADS:
        return "  " * depth + _compact(node)

    # Collect leading scalar atoms that stay on the opening line.
    leading: List[str] = [_compact(node[0])]
    child_start = 1
    for child in node[1:]:
        if isinstance(child, list):
            break
        leading.append(_compact(child))
        child_start += 1

    first_line = "  " * depth + "(" + " ".join(leading)
    list_children = node[child_start:]
    if not list_children:
        return first_line + ")"

    lines = [first_line]
    for child in list_children:
        lines.append(_fmt_sym(child, depth + 1))
    lines.append("  " * depth + ")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Symbol locator
# ---------------------------------------------------------------------------

def _locate_symbol(lib_id: str) -> Tuple[Path, list, list]:
    """Find the on-disk .kicad_sym file for ``lib_id``.

    Returns ``(path, full_tree, sym_node)`` where ``sym_node`` is a direct
    reference into ``full_tree`` — mutations on it propagate to the tree
    before write-back.

    Raises ``ValueError`` when the symbol cannot be found.
    """
    if ":" not in lib_id:
        raise ValueError(f"lib_id must be 'LibNick:PartName', got {lib_id!r}")

    aliases = _load_aliases()
    resolved = aliases.get(lib_id, lib_id)
    libnick, part = resolved.split(":", 1)

    for root in _sym_roots():
        candidates = [
            root / f"{libnick}.kicad_symdir" / f"{part}.kicad_sym",
            root / f"{libnick}.kicad_sym",
        ]
        for path in candidates:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            try:
                tree = sexpdata.loads(text)
            except Exception as exc:
                raise ValueError(f"Failed to parse {path}: {exc}") from exc
            if not isinstance(tree, list):
                continue
            h = _head(tree)
            if h == "kicad_symbol_lib":
                for child in tree[1:]:
                    if isinstance(child, list) and _head(child) == "symbol":
                        if len(child) > 1 and str(child[1]) == part:
                            return path, tree, child
            elif h == "symbol":
                if len(tree) > 1 and str(tree[1]) == part:
                    return path, tree, tree

    roots_listed = "\n  ".join(str(r) for r in _sym_roots())
    raise ValueError(
        f"Symbol {lib_id!r} not found. Searched roots:\n  {roots_listed}"
    )


# ---------------------------------------------------------------------------
# Pin finder (searches top-level and nested unit subblocks)
# ---------------------------------------------------------------------------

def _find_pin(node: list, *, number: Optional[str] = None,
              name: Optional[str] = None) -> Optional[Tuple[list, list]]:
    """Return ``(parent, pin_node)`` for the first matching pin.

    ``parent`` is the list that directly contains the pin, enabling the
    caller to call ``parent.remove(pin_node)`` for ``remove_pin``.
    Match on ``number`` OR ``name`` (not both at once).
    """
    for child in node[1:]:
        if not isinstance(child, list):
            continue
        ch = _head(child)
        if ch == "pin":
            num_n = _first_child(child, "number")
            name_n = _first_child(child, "name")
            pin_num = str(num_n[1]) if num_n and len(num_n) > 1 else ""
            pin_name = str(name_n[1]) if name_n and len(name_n) > 1 else ""
            if number is not None and pin_num == number:
                return node, child
            if name is not None and pin_name == name:
                return node, child
        elif ch == "symbol":
            result = _find_pin(child, number=number, name=name)
            if result is not None:
                return result
    return None


# ---------------------------------------------------------------------------
# KiCad Library Convention (KLC) pin dimension rules
# ---------------------------------------------------------------------------
# Reference: KLC S4.1, S4.2, S4.3 — https://klc.kicad.org/symbol/s4/
#
# S4.1  All pin positions must lie on a 50 mil (1.27 mm) grid.
# S4.2  Standard pin length is 100 mil (2.54 mm).  The only allowed
#       shorter length is 50 mil (1.27 mm), which is reserved for hidden
#       power pins (power_in / power_out).  Minimum enforced: 1.27 mm.
# S4.3  Pin rotation must be a multiple of 90° (0, 90, 180, 270).
#
# These numbers come straight from component datasheets: through-hole and
# SMD footprints use a 100 mil / 50 mil pitch grid, so every pin on a
# symbol must align to that grid for wires to connect without a grid jump.

_KLC_GRID_MM: float = 1.27          # 50 mil — coarsest allowed grid
_KLC_STD_LENGTH_MM: float = 2.54    # 100 mil — standard pin stub
_KLC_MIN_LENGTH_MM: float = 1.27    # 50  mil — minimum (hidden power pins)
_KLC_VALID_ROTS: tuple = (0.0, 90.0, 180.0, 270.0)


def _collect_existing_pins(sym: list) -> List[Dict[str, Any]]:
    """Walk the symbol tree and return a list of existing pin dicts.

    Each dict has: number, name, x, y, rot, length, etype.
    Searches both top-level pins and nested unit subblocks.
    """
    pins: List[Dict[str, Any]] = []

    def _walk(node: list) -> None:
        for child in node[1:]:
            if not isinstance(child, list):
                continue
            h = _head(child)
            if h == "pin":
                at_n  = _first_child(child, "at")
                len_n = _first_child(child, "length")
                num_n = _first_child(child, "number")
                nam_n = _first_child(child, "name")
                etype_raw = child[1] if len(child) > 1 else None
                pins.append({
                    "number": str(num_n[1]) if num_n and len(num_n) > 1 else "?",
                    "name":   str(nam_n[1]) if nam_n and len(nam_n) > 1 else "~",
                    "x":      float(at_n[1]) if at_n and len(at_n) > 1 else 0.0,
                    "y":      float(at_n[2]) if at_n and len(at_n) > 2 else 0.0,
                    "rot":    float(at_n[3]) if at_n and len(at_n) > 3 else 0.0,
                    "length": float(len_n[1]) if len_n and len(len_n) > 1 else _KLC_STD_LENGTH_MM,
                    "etype":  etype_raw.value() if isinstance(etype_raw, sexpdata.Symbol)
                              else str(etype_raw) if etype_raw else "passive",
                })
            elif h == "symbol":
                _walk(child)

    _walk(sym)
    return pins


def _detect_pitch_and_length(existing_pins: List[Dict[str, Any]]
                              ) -> tuple:
    """Analyse existing pin positions to detect the symbol's pin pitch and
    stub length — both of which are set by the component datasheet.

    Strategy:
      1. Collect all unique non-zero absolute coordinate differences between
         pairs of pins that share the same rotation axis (i.e. differ only
         in the perpendicular axis).
      2. The most common difference is the pin pitch.
      3. The most common pin stub length across all existing pins is used as
         the datasheet-derived stub length for any new pin.

    Returns ``(pitch_mm, stub_length_mm)`` where pitch is the detected
    spacing or the KLC default (2.54 mm) if fewer than 2 pins exist, and
    stub_length is the most-common existing stub or the KLC default.
    """
    import math
    from collections import Counter

    # Stub length: majority vote across all existing pins.
    if existing_pins:
        lengths = [round(p["length"] / _KLC_GRID_MM) * _KLC_GRID_MM
                   for p in existing_pins]
        stub = Counter(lengths).most_common(1)[0][0]
        stub = max(stub, _KLC_MIN_LENGTH_MM)
    else:
        stub = _KLC_STD_LENGTH_MM

    if len(existing_pins) < 2:
        return _KLC_STD_LENGTH_MM, stub

    # Pin pitch: look at all pairwise differences.
    diffs: List[float] = []
    for i, pa in enumerate(existing_pins):
        for pb in existing_pins[i + 1:]:
            dx = abs(pa["x"] - pb["x"])
            dy = abs(pa["y"] - pb["y"])
            # Only count if the pins are collinear on one axis (pure row/col).
            if dx < 1e-4 and dy > 1e-4:
                diffs.append(round(dy / _KLC_GRID_MM) * _KLC_GRID_MM)
            elif dy < 1e-4 and dx > 1e-4:
                diffs.append(round(dx / _KLC_GRID_MM) * _KLC_GRID_MM)

    if not diffs:
        return _KLC_STD_LENGTH_MM, stub

    # The minimum non-zero diff seen more than once (or just the min) is pitch.
    diff_counts = Counter(diffs)
    # Prefer the smallest diff that appears most frequently — that's the
    # pin-to-pin pitch, not a multiple of it.
    candidates = sorted(diff_counts.keys())
    pitch = candidates[0]   # smallest spacing = fundamental pitch
    pitch = max(pitch, _KLC_GRID_MM)
    return pitch, stub


def _snap(value: float, grid: float) -> float:
    """Snap *value* to the nearest multiple of *grid*."""
    return round(round(value / grid) * grid, 10)


def _snap_rot(rot: float) -> float:
    """Snap *rot* (degrees) to the nearest multiple of 90°, range [0, 360)."""
    r = round(rot % 360.0)
    return float((round(r / 90) * 90) % 360)


def _klc_default_length(etype: str) -> float:
    """Return the KLC-correct default pin stub length for *etype*.

    Hidden power pins (power_in / power_out) may use 50 mil; all others
    use the standard 100 mil stub so they reach the schematic grid cleanly.
    """
    return _KLC_MIN_LENGTH_MM if etype in ("power_in", "power_out") else _KLC_STD_LENGTH_MM


def _klc_validate_pin(
    x: float, y: float, rot: float, length: float, etype: str,
    pitch: float = _KLC_STD_LENGTH_MM,
) -> tuple:
    """Apply KLC rules to pin dimensions and return corrected values with notes.

    ``pitch`` is the pin-to-pin spacing detected from the existing pins in
    this symbol (see :func:`_detect_pitch_and_length`).  x and y are first
    snapped to the symbol's own pitch so the new pin lines up with the
    datasheet-defined pin grid, then the result is also snapped to the
    coarser 50-mil KLC grid as a safety net.

    Returns ``(x, y, rot, length, notes)`` where *notes* is a (possibly
    empty) list of human-readable strings describing any correction made.
    Raises ``ValueError`` if *length* is below the absolute minimum.
    """
    notes: List[str] = []

    # Snap to symbol pitch first (datasheet grid), then to KLC minimum grid.
    effective_grid = max(_snap(pitch, _KLC_GRID_MM), _KLC_GRID_MM)
    sx = _snap(x, effective_grid)
    sy = _snap(y, effective_grid)
    if abs(sx - x) > 1e-6 or abs(sy - y) > 1e-6:
        notes.append(
            f"position ({x:.4f}, {y:.4f}) snapped to symbol pitch grid "
            f"({sx:.4f}, {sy:.4f}) [{effective_grid:.4f} mm = "
            f"{round(effective_grid / 0.0254)} mil]"
        )
    x, y = sx, sy

    # S4.3 — rotation must be a multiple of 90°
    sr = _snap_rot(rot)
    if abs(sr - (rot % 360.0)) > 0.5:
        notes.append(
            f"KLC S4.3: rotation {rot}deg snapped to nearest 90deg -> {sr}deg"
        )
    rot = sr

    # S4.2 — pin length
    default_len = _klc_default_length(etype)
    if length < _KLC_MIN_LENGTH_MM - 1e-6:
        raise ValueError(
            f"KLC S4.2: pin length {length} mm is below the minimum "
            f"{_KLC_MIN_LENGTH_MM} mm (50 mil). Use {default_len} mm."
        )
    if abs(length - _KLC_STD_LENGTH_MM) > 1e-6 and abs(length - _KLC_MIN_LENGTH_MM) > 1e-6:
        notes.append(
            f"KLC S4.2: non-standard pin length {length} mm. "
            f"Standard is {_KLC_STD_LENGTH_MM} mm (100 mil); "
            f"only {_KLC_MIN_LENGTH_MM} mm (50 mil) is an allowed alternative."
        )

    return x, y, rot, length, notes


# ---------------------------------------------------------------------------
# Datasheet reader — fetches the symbol's Datasheet URL and asks Claude to
# extract the pin table.  Used by add_pin when no existing pins are present
# (i.e. a brand-new symbol that was never in the KiCad library).
# ---------------------------------------------------------------------------

def _get_datasheet_url(sym: list) -> Optional[str]:
    """Return the Datasheet property value from a symbol node, or None."""
    for child in sym[1:]:
        if isinstance(child, list) and _head(child) == "property":
            if len(child) >= 3 and str(child[1]) == "Datasheet":
                val = str(child[2]).strip()
                if val and val not in ("~", ""):
                    return val
    return None


def _read_datasheet_pins(sym: list, part_name: str) -> Optional[Dict[str, Any]]:
    """Fetch the symbol's Datasheet URL and use Claude to extract its pin table.

    Called only when the symbol has NO existing pins (brand-new symbol not
    yet in the KiCad library).  Returns a dict::

        {
            "pitch_mm":  2.54,          # pin-to-pin spacing from the datasheet
            "stub_mm":   2.54,          # pin stub length
            "pins": [
                {"number": "1", "name": "VCC",  "etype": "power_in"},
                {"number": "2", "name": "GND",  "etype": "power_in"},
                ...
            ]
        }

    Returns ``None`` when the datasheet URL is missing, unreachable, or the
    extraction fails so the caller can fall back to KLC defaults.
    """
    import base64
    import json
    import os
    import urllib.request

    url = _get_datasheet_url(sym)
    if not url or not url.startswith("http"):
        return None

    # --- download the datasheet ---
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "").lower()
            raw = resp.read()
    except Exception as exc:
        return None

    # --- send to Claude and ask for pin table ---
    try:
        import anthropic
        client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY", "")
        )

        is_pdf = "pdf" in content_type or url.lower().endswith(".pdf")

        if is_pdf:
            b64 = base64.standard_b64encode(raw).decode("utf-8")
            user_content = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        f"This is the datasheet for {part_name}.\n"
                        "Extract the complete pin list and return ONLY a JSON object "
                        "in exactly this format — no markdown, no explanation:\n"
                        "{\n"
                        '  "pitch_mm": <pin-to-pin spacing in mm, e.g. 2.54>,\n'
                        '  "stub_mm": <pin stub length in mm, typically 2.54>,\n'
                        '  "pins": [\n'
                        '    {"number": "1", "name": "VCC",  "etype": "power_in"},\n'
                        '    {"number": "2", "name": "GND",  "etype": "power_in"},\n'
                        "    ...\n"
                        "  ]\n"
                        "}\n"
                        "For etype use exactly one of: input, output, bidirectional, "
                        "tri_state, passive, power_in, power_out, open_collector, "
                        "open_emitter, no_connect, unspecified.\n"
                        "pitch_mm comes from the package pin pitch (e.g. DIP = 2.54, "
                        "SOIC = 1.27, QFP = 0.5 or 0.8)."
                    ),
                },
            ]
        else:
            # HTML / text datasheet
            try:
                text = raw.decode("utf-8", errors="replace")[:12000]
            except Exception:
                return None
            user_content = [
                {
                    "type": "text",
                    "text": (
                        f"This is datasheet content for {part_name}:\n\n{text}\n\n"
                        "Extract the complete pin list and return ONLY a JSON object "
                        "in exactly this format — no markdown, no explanation:\n"
                        "{\n"
                        '  "pitch_mm": <pin-to-pin spacing in mm>,\n'
                        '  "stub_mm": 2.54,\n'
                        '  "pins": [\n'
                        '    {"number": "1", "name": "VCC", "etype": "power_in"},\n'
                        "    ...\n"
                        "  ]\n"
                        "}\n"
                        "For etype use exactly one of: input, output, bidirectional, "
                        "tri_state, passive, power_in, power_out, open_collector, "
                        "open_emitter, no_connect, unspecified."
                    ),
                }
            ]

        response = client.messages.create(
            # Sonnet 4.6, not Haiku — Haiku misreads datasheet pin tables, which
            # produced incomplete/incorrect pin lists on symbol edits from images.
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_text = response.content[0].text.strip()

        # Strip ```json ... ``` fences if Claude added them.
        if raw_text.startswith("```"):
            parts = raw_text.split("```")
            raw_text = parts[1].lstrip("json").strip() if len(parts) > 1 else raw_text

        data = json.loads(raw_text)

        # Validate minimal structure.
        if "pins" not in data or not isinstance(data["pins"], list):
            return None
        data.setdefault("pitch_mm", _KLC_STD_LENGTH_MM)
        data.setdefault("stub_mm",  _KLC_STD_LENGTH_MM)
        return data

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Op dispatcher
# ---------------------------------------------------------------------------

_VALID_ETYPES = frozenset({
    "input", "output", "bidirectional", "tri_state", "passive",
    "free", "unspecified", "power_in", "power_out",
    "open_collector", "open_emitter", "no_connect",
})


def _apply_op(sym: list, op_dict: Dict[str, Any]) -> str:
    """Apply one edit operation to *sym* (in-place).  Returns a human-readable
    success message, or raises ``ValueError`` on any error."""
    op = str(op_dict.get("op", "")).strip()
    S = sexpdata.Symbol  # shorthand

    # ---- rename_pin ----------------------------------------------------------
    if op == "rename_pin":
        number = str(op_dict["number"])
        new_name = str(op_dict["new_name"])
        result = _find_pin(sym, number=number)
        if result is None:
            raise ValueError(f"Pin number {number!r} not found in {sym[1]!r}.")
        _, pin_node = result
        name_n = _first_child(pin_node, "name")
        if name_n is None or len(name_n) < 2:
            raise ValueError(f"Pin {number} has no name node.")
        old = str(name_n[1])
        name_n[1] = new_name
        return f"Pin {number}: name {old!r} -> {new_name!r}"

    # ---- rename_pin_number ---------------------------------------------------
    elif op == "rename_pin_number":
        old_num = str(op_dict["old_number"])
        new_num = str(op_dict["new_number"])
        result = _find_pin(sym, number=old_num)
        if result is None:
            raise ValueError(f"Pin number {old_num!r} not found.")
        _, pin_node = result
        num_n = _first_child(pin_node, "number")
        if num_n is None or len(num_n) < 2:
            raise ValueError(f"Pin {old_num} has no number node.")
        num_n[1] = new_num
        return f"Pin number {old_num!r} -> {new_num!r}"

    # ---- change_pin_etype ----------------------------------------------------
    elif op == "change_pin_etype":
        number = str(op_dict["number"])
        etype = str(op_dict["etype"]).strip()
        if etype not in _VALID_ETYPES:
            raise ValueError(
                f"Invalid etype {etype!r}. Valid: {sorted(_VALID_ETYPES)}"
            )
        result = _find_pin(sym, number=number)
        if result is None:
            raise ValueError(f"Pin number {number!r} not found.")
        _, pin_node = result
        if len(pin_node) < 2:
            raise ValueError(f"Malformed pin node for pin {number}.")
        old = pin_node[1].value() if isinstance(pin_node[1], S) else str(pin_node[1])
        pin_node[1] = S(etype)
        return f"Pin {number}: etype {old!r} -> {etype!r}"

    # ---- move_pin ------------------------------------------------------------
    elif op == "move_pin":
        number = str(op_dict["number"])
        result = _find_pin(sym, number=number)
        if result is None:
            raise ValueError(f"Pin number {number!r} not found.")
        _, pin_node = result
        at_n = _first_child(pin_node, "at")
        if at_n is None:
            raise ValueError(f"Pin {number} has no (at ...) node.")

        # Read current values as fallback when caller omits a field.
        cur_x   = float(at_n[1]) if len(at_n) > 1 else 0.0
        cur_y   = float(at_n[2]) if len(at_n) > 2 else 0.0
        cur_rot = float(at_n[3]) if len(at_n) > 3 else 0.0
        new_x   = float(op_dict["x"])   if "x"   in op_dict else cur_x
        new_y   = float(op_dict["y"])   if "y"   in op_dict else cur_y
        new_rot = float(op_dict["rot"]) if "rot" in op_dict else cur_rot

        len_n     = _first_child(pin_node, "length")
        cur_len   = float(len_n[1]) if len_n and len(len_n) > 1 else _KLC_STD_LENGTH_MM
        etype_raw = pin_node[1]
        cur_etype = etype_raw.value() if isinstance(etype_raw, S) else str(etype_raw)

        # Detect pitch from existing pins so the moved pin stays on the
        # datasheet-defined grid of this particular symbol.
        existing = _collect_existing_pins(sym)
        pitch, _ = _detect_pitch_and_length(existing)

        new_x, new_y, new_rot, _, notes = _klc_validate_pin(
            new_x, new_y, new_rot, cur_len, cur_etype, pitch=pitch
        )

        at_n[1] = new_x
        at_n[2] = new_y
        if len(at_n) > 3:
            at_n[3] = new_rot
        else:
            at_n.append(new_rot)

        note_str = ("  [" + "; ".join(notes) + "]") if notes else ""
        return f"Pin {number} moved to ({new_x}, {new_y}) rot={new_rot}.{note_str}"

    # ---- change_pin_length ---------------------------------------------------
    elif op == "change_pin_length":
        number = str(op_dict["number"])
        length = float(op_dict["length"])
        if length < _KLC_MIN_LENGTH_MM - 1e-6:
            raise ValueError(
                f"KLC S4.2: length {length} mm is below the minimum "
                f"{_KLC_MIN_LENGTH_MM} mm (50 mil)."
            )
        result = _find_pin(sym, number=number)
        if result is None:
            raise ValueError(f"Pin number {number!r} not found.")
        _, pin_node = result
        len_n = _first_child(pin_node, "length")
        if len_n is None or len(len_n) < 2:
            raise ValueError(f"Pin {number} has no length node.")
        old_len = len_n[1]
        len_n[1] = length
        note = ""
        if abs(length - _KLC_STD_LENGTH_MM) > 1e-6 and abs(length - _KLC_MIN_LENGTH_MM) > 1e-6:
            note = (
                f"  [KLC S4.2: non-standard length. "
                f"Standard is {_KLC_STD_LENGTH_MM} mm; "
                f"alternative is {_KLC_MIN_LENGTH_MM} mm]"
            )
        return f"Pin {number} length {old_len} -> {length} mm.{note}"

    # ---- set_property --------------------------------------------------------
    elif op == "set_property":
        key = str(op_dict["key"])
        value = str(op_dict["value"])
        for child in sym[1:]:
            if isinstance(child, list) and _head(child) == "property":
                if len(child) >= 3 and str(child[1]) == key:
                    old = str(child[2])
                    child[2] = value
                    return f"Property {key!r}: {old!r} -> {value!r}"
        # Key doesn't exist yet — append a minimal property node.
        sym.append([S("property"), key, value])
        return f"Property {key!r} added with value {value!r}."

    # ---- remove_pin ----------------------------------------------------------
    elif op == "remove_pin":
        number = op_dict.get("number")
        name = op_dict.get("name")
        if number is None and name is None:
            raise ValueError("remove_pin requires 'number' or 'name'.")
        result = _find_pin(
            sym,
            number=str(number) if number is not None else None,
            name=str(name) if name is not None else None,
        )
        if result is None:
            key = f"number={number!r}" if number is not None else f"name={name!r}"
            raise ValueError(f"Pin ({key}) not found.")
        parent, pin_node = result
        num_n = _first_child(pin_node, "number")
        name_n = _first_child(pin_node, "name")
        pin_num = str(num_n[1]) if num_n and len(num_n) > 1 else "?"
        pin_name = str(name_n[1]) if name_n and len(name_n) > 1 else "?"
        parent.remove(pin_node)
        return f"Pin {pin_num} ({pin_name!r}) removed."

    # ---- add_pin -------------------------------------------------------------
    elif op == "add_pin":
        number = str(op_dict["number"])
        name   = str(op_dict.get("name", "~"))
        etype  = str(op_dict.get("etype", "passive"))
        shape  = str(op_dict.get("shape", "line"))
        x      = float(op_dict.get("x", 0.0))
        y      = float(op_dict.get("y", 0.0))
        rot    = float(op_dict.get("rot", 0.0))

        if etype not in _VALID_ETYPES:
            raise ValueError(
                f"Invalid etype {etype!r}. Valid: {sorted(_VALID_ETYPES)}"
            )
        # Duplicate pin number guard.
        if _find_pin(sym, number=number) is not None:
            raise ValueError(f"Pin number {number!r} already exists.")

        existing = _collect_existing_pins(sym)
        notes: List[str] = []
        ds_data: Optional[Dict[str, Any]] = None

        if not existing:
            # No existing pins — this is a brand-new symbol not in the KiCad
            # library.  Read the real datasheet to get pin name, etype, pitch
            # and stub length instead of guessing from defaults.
            part_name = str(sym[1]) if len(sym) > 1 else "unknown"
            ds_data = _read_datasheet_pins(sym, part_name)

            if ds_data:
                pitch   = float(ds_data.get("pitch_mm", _KLC_STD_LENGTH_MM))
                ds_stub = float(ds_data.get("stub_mm",  _KLC_STD_LENGTH_MM))
                notes.append(
                    f"datasheet read: pitch={pitch:.4f} mm "
                    f"({round(pitch/0.0254)} mil), "
                    f"stub={ds_stub:.4f} mm ({round(ds_stub/0.0254)} mil)"
                )
                # If caller did not explicitly provide name/etype, fill them
                # from the datasheet pin table for this pin number.
                ds_pin = next(
                    (p for p in ds_data["pins"] if str(p.get("number")) == number),
                    None,
                )
                if ds_pin:
                    if "name" not in op_dict or op_dict["name"] in ("~", ""):
                        name = str(ds_pin.get("name", name))
                        notes.append(f"name from datasheet: {name!r}")
                    ds_etype = str(ds_pin.get("etype", ""))
                    if "etype" not in op_dict and ds_etype in _VALID_ETYPES:
                        etype = ds_etype
                        notes.append(f"etype from datasheet: {etype!r}")
            else:
                # Datasheet fetch failed — fall back to KLC defaults and warn.
                pitch   = _KLC_STD_LENGTH_MM
                ds_stub = _KLC_STD_LENGTH_MM
                notes.append(
                    "datasheet not available — using KLC defaults "
                    f"(pitch={pitch} mm, stub={ds_stub} mm)"
                )
        else:
            # Existing pins present — infer pitch/stub from the symbol itself
            # (which was already drawn from the datasheet).
            pitch, ds_stub = _detect_pitch_and_length(existing)
            notes.append(
                f"pitch from existing pins: {pitch:.4f} mm "
                f"({round(pitch/0.0254)} mil)"
            )

        # Use the datasheet/detected stub unless the caller explicitly set one.
        length = float(op_dict["length"]) if "length" in op_dict else ds_stub

        # Validate and snap all dimensions to the symbol's pitch grid.
        x, y, rot, length, klc_notes = _klc_validate_pin(
            x, y, rot, length, etype, pitch=pitch
        )
        notes.extend(klc_notes)

        # Build a (pin ...) node with hidden name/number labels (KLC S4.4).
        hide_effects = [S("effects"), [S("font"), [S("size"), 1.27, 1.27]], S("hide")]
        new_pin = [
            S("pin"), S(etype), S(shape),
            [S("at"), x, y, rot],
            [S("length"), length],
            [S("name"), name, hide_effects],
            [S("number"), number, hide_effects],
        ]

        # Append to the first unit subblock if one exists, else to the symbol.
        unit_blocks = [
            c for c in sym[1:]
            if isinstance(c, list) and _head(c) == "symbol"
        ]
        target = unit_blocks[0] if unit_blocks else sym
        target.append(new_pin)

        note_str = "  [" + "; ".join(notes) + "]" if notes else ""
        return (
            f"Pin {number} ({name!r}, {etype}) added at ({x}, {y}) "
            f"rot={rot} length={length} mm.{note_str}"
        )

    else:
        raise ValueError(
            f"Unknown op {op!r}. Valid ops: rename_pin, rename_pin_number, "
            "change_pin_etype, move_pin, change_pin_length, "
            "set_property, remove_pin, add_pin."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _find_sym_lib_tables() -> List[Path]:
    """KiCad's GLOBAL sym-lib-table(s) — one per installed KiCad version under
    %APPDATA%/kicad/<ver>/, plus any KICAD_CONFIG_HOME override. Mirror of
    ``create_symbol._find_sym_lib_tables`` so delete can undo the registration
    create_symbol performed."""
    import os

    out: List[Path] = []
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        base = Path(appdata) / "kicad"
        if base.exists():
            for ver in sorted(base.iterdir()):
                t = ver / "sym-lib-table"
                if t.is_file():
                    out.append(t)
    cfg = os.environ.get("KICAD_CONFIG_HOME")
    if cfg:
        t = Path(cfg) / "sym-lib-table"
        if t.is_file() and t not in out:
            out.append(t)
    return out


def _unregister_from_lib_tables(library: str) -> List[str]:
    """Remove the ``(lib (name "<library>") ...)`` entry from KiCad's global
    sym-lib-table(s). The reverse of ``create_symbol._register_in_lib_tables``:
    idempotent (skips a table that never had the entry) and safe (backs the
    table up to <name>.envil-bak before the first edit). Matches the one-line
    entry format the registrar writes."""
    import re as _re

    notes: List[str] = []
    for tbl in _find_sym_lib_tables():
        try:
            text = tbl.read_text(encoding="utf-8")
        except OSError as exc:
            notes.append(f"could not read {tbl.parent.name}/sym-lib-table: {exc}")
            continue
        pat = _re.compile(r'\(lib\s+\(name\s+"' + _re.escape(library) + r'"')
        lines = text.splitlines(keepends=True)
        kept = [ln for ln in lines if not pat.search(ln)]
        if len(kept) == len(lines):
            notes.append(f"not present in {tbl.parent.name}")
            continue
        try:
            backup = tbl.with_name(tbl.name + ".envil-bak")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            tbl.write_text("".join(kept), encoding="utf-8")
            notes.append(f"unregistered from {tbl.parent.name}")
        except OSError as exc:
            notes.append(f"could not update {tbl.parent.name}: {exc}")
    return notes


def _refresh_symbol_caches() -> None:
    """Drop the symbol-resolution lru_caches so a deleted symbol stops
    resolving without a restart (mirror of create_symbol._refresh_caches)."""
    try:
        from . import symbol_geom as sg
        for fn in ("load_symbol", "_all_symbols", "resolve_lib_id_by_value"):
            obj = getattr(sg, fn, None)
            if obj is not None and hasattr(obj, "cache_clear"):
                obj.cache_clear()
    except Exception:
        pass


# Library nicks envil creates and is allowed to delete from without force.
# Every other nick in KiCad's sym-lib-table is a stock/shared library that must
# be protected. create_symbol writes new parts under the "Custom" nick.
_CUSTOM_LIB_NICKS = frozenset({"Custom", "envil_generated"})

# Generator tags envil stamps into files it writes — the two create_symbol
# paths use different strings, so both are accepted as proof of an envil part.
_ENVIL_GEN_TAGS = ("envil-sym-gen", "envil_create_symbol")


def delete_symbol(
    lib_id: str,
    unregister: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Delete a symbol from the local library — the reverse of create_symbol.

    Behaviour depends on the on-disk layout:

    * symdir layout (``<nick>.kicad_symdir/<part>.kicad_sym`` — how
      create_symbol writes) or any single-symbol file: the whole .kicad_sym
      file is removed. If that empties the ``.kicad_symdir`` folder it is
      removed too and, when ``unregister`` is set, the library is stripped
      from KiCad's global sym-lib-table(s).
    * flat multi-symbol library (many symbols in one file): only the requested
      symbol node is removed and the file is written back. This mutates a
      shared library file, so it is refused unless ``force`` is True.

    Safety: only symbols in an envil-managed custom library (nick in
    ``_CUSTOM_LIB_NICKS``, or a file carrying an envil generator tag) may be
    deleted without ``force``. Every other nick is a stock/shared KiCad library
    and is refused, so a stray call cannot delete a stock part. NOTE: the
    ``.kicad_symdir`` folder suffix is deliberately NOT used as a "custom"
    signal — in current KiCad builds stock libraries ship as symdirs too.

    Args:
        lib_id:     'LibNick:PartName' (e.g. 'Custom:BQ76952').
        unregister: also remove the library from KiCad's sym-lib-table when the
                    delete empties it (default True).
        force:      allow deleting a non-envil symbol / editing a shared
                    multi-symbol library file (default False).

    Returns a dict describing what was removed. Raises ``ValueError`` if the
    symbol cannot be found or a guard blocks the delete.
    """
    path, tree, sym = _locate_symbol(lib_id)   # raises ValueError if missing

    aliases = _load_aliases()
    resolved = aliases.get(lib_id, lib_id)
    libnick, part = resolved.split(":", 1)

    text = path.read_text(encoding="utf-8")
    # Decide whether this symbol lives in an envil-managed *custom* library.
    # Only custom libraries may be deleted without force — every other nick in
    # KiCad's sym-lib-table is a stock/shared library we must not touch.
    #
    # The ".kicad_symdir" folder suffix is NOT a "custom" signal: in current
    # KiCad builds every stock library (Device, Timer, MCU_*, ...) also ships
    # as a .kicad_symdir, so a folder-suffix check would happily wipe stock
    # parts. Identify custom by the library nick, or by envil's generator tag.
    is_custom_nick = libnick in _CUSTOM_LIB_NICKS
    envil_made = any(tag in text for tag in _ENVIL_GEN_TAGS)
    if not (is_custom_nick or envil_made) and not force:
        raise ValueError(
            f"{lib_id!r} is not in an envil-managed custom library — it lives "
            f"in stock/shared library {libnick!r} at {path}. Refusing to delete "
            f"a non-custom part; pass force=true if you really mean to."
        )

    sym_nodes = [c for c in tree[1:]
                 if isinstance(c, list) and _head(c) == "symbol"]
    single_file = _head(tree) == "symbol" or len(sym_nodes) <= 1

    library_removed = False
    unregister_notes: List[str] = []

    if single_file:
        try:
            path.unlink()
        except OSError as exc:
            raise ValueError(f"could not delete {path}: {exc}") from exc
        removed = f"deleted file {path}"
        parent = path.parent
        if parent.name.endswith(".kicad_symdir"):
            try:
                if not any(parent.iterdir()):
                    parent.rmdir()
                    library_removed = True
            except OSError:
                pass
    else:
        if not force:
            raise ValueError(
                f"{lib_id!r} is one of {len(sym_nodes)} symbols in a shared "
                f"library file ({path.name}). Deleting it edits that shared "
                f"file — pass force=true to proceed."
            )
        tree.remove(sym)
        out_text = _fmt_sym(tree)
        path.write_text(out_text, encoding="utf-8")
        removed = f"removed symbol from {path} ({len(sym_nodes) - 1} remaining)"

    if library_removed and unregister:
        unregister_notes = _unregister_from_lib_tables(libnick)

    _refresh_symbol_caches()

    return {
        "ok": True,
        "lib_id": lib_id,
        "removed": removed,
        "library_removed": library_removed,
        "unregister": unregister_notes,
        "note": (
            f"Deleted symbol {lib_id}. " + removed + "."
            + (f" Library {libnick!r} was empty and removed"
               + (" and unregistered from KiCad's symbol-library table"
                  if unregister_notes and any(
                      n.startswith("unregistered") for n in unregister_notes)
                  else "")
               + " — RESTART KiCad to refresh the symbol chooser."
               if library_removed else "")
        ),
    }


def apply_symbol_ops(
    lib_id: str,
    ops: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply a list of edit operations to a symbol and write the file back.

    Args:
        lib_id: KiCad lib_id string, e.g. ``"Timer:NE555"``.
        ops:    List of operation dicts — see module docstring for shapes.

    Returns:
        List of per-op result dicts::

            {"op": "rename_pin", "ok": True,  "msg": "Pin 3: name 'GND' -> 'PGND'"}
            {"op": "bad_op",     "ok": False, "msg": "Unknown op 'bad_op'. ..."}

    The file is written only when at least one op succeeds.  On success the
    ``load_symbol`` LRU cache is cleared so subsequent reads see the change.
    """
    path, tree, sym = _locate_symbol(lib_id)

    results: List[Dict[str, Any]] = []
    for op_dict in ops:
        op_name = str(op_dict.get("op", ""))
        try:
            msg = _apply_op(sym, op_dict)
            results.append({"op": op_name, "ok": True, "msg": msg})
        except (KeyError, TypeError) as exc:
            results.append({
                "op": op_name,
                "ok": False,
                "msg": f"Missing or wrong parameter: {exc}",
            })
        except ValueError as exc:
            results.append({"op": op_name, "ok": False, "msg": str(exc)})

    if any(r["ok"] for r in results):
        # Render and write back.
        out_text = _fmt_sym(tree)
        path.write_text(out_text, encoding="utf-8")
        # Clear the load_symbol LRU cache so callers see the updated symbol.
        try:
            load_symbol.cache_clear()
        except AttributeError:
            pass

    return results
