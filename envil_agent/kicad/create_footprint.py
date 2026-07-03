"""Create a KiCad footprint (.kicad_mod) from a component datasheet.

Flow
----
1. Resolve the symbol (.kicad_sym) to get the Datasheet URL and part name.
2. Fetch the PDF / HTML page and send to Claude -> JSON package dimensions.
3. Generate the .kicad_mod content (pads, silkscreen, courtyard) for that
   package type.
4. Write to  fp_lib_dir()/<fp_lib_nick>.pretty/<fp_name>.kicad_mod
5. Optionally update the symbol's Footprint property to link them.

Supported package types
-----------------------
DIP   Dual In-Line Package (THT, 2 rows)
SIP   Single In-Line Package (THT, 1 row)
SOIC  Small-Outline IC  (SMD, 2 rows, 1.27 mm pitch)
TSSOP Thin SSOP / SSOP  (SMD, 2 rows, 0.65 mm pitch)
SOT23 SOT-23-3/5/6      (SMD, asymmetric)
QFP   QFP/TQFP/LQFP    (SMD, 4 sides)
QFN   QFN/DFN           (SMD, 4 sides, optional exposed pad)
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Optional

import anthropic

from ..settings import fp_lib_dir, fp_lib_table


# ─────────────────────────────────────────────────────────────────────────────
# Float formatter
# ─────────────────────────────────────────────────────────────────────────────

def _f(v: float, d: int = 4) -> str:
    s = f"{v:.{d}f}".rstrip("0").rstrip(".")
    return s if s else "0"


# ─────────────────────────────────────────────────────────────────────────────
# .kicad_mod node builders  (string-based — no sexpdata needed for write-once)
# ─────────────────────────────────────────────────────────────────────────────

def _header(fp_name: str, descr: str, tags: str, is_smd: bool) -> str:
    attr = "smd" if is_smd else "through_hole"
    return (
        f'(footprint "{fp_name}"\n'
        f'\t(version 20260206)\n'
        f'\t(generator "envil-fp-gen")\n'
        f'\t(generator_version "1.0")\n'
        f'\t(layer "F.Cu")\n'
        f'\t(descr "{descr}")\n'
        f'\t(tags "{tags}")\n'
        f'\t(attr {attr})'
    )


def _prop(name: str, val: str, x: float, y: float, layer: str) -> str:
    return (
        f'\t(property "{name}" "{val}"\n'
        f'\t\t(at {_f(x)} {_f(y)} 0)\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(effects (font (size 1 1) (thickness 0.15)))\n'
        f'\t)'
    )


def _line(x1: float, y1: float, x2: float, y2: float,
          layer: str, w: float = 0.12) -> str:
    return (
        f'\t(fp_line\n'
        f'\t\t(start {_f(x1)} {_f(y1)})\n'
        f'\t\t(end {_f(x2)} {_f(y2)})\n'
        f'\t\t(stroke (width {_f(w)}) (type solid))\n'
        f'\t\t(layer "{layer}")\n'
        f'\t)'
    )


def _rect(x1: float, y1: float, x2: float, y2: float,
          layer: str, w: float = 0.05, fill: bool = False) -> str:
    fill_s = "yes" if fill else "no"
    return (
        f'\t(fp_rect\n'
        f'\t\t(start {_f(x1)} {_f(y1)})\n'
        f'\t\t(end {_f(x2)} {_f(y2)})\n'
        f'\t\t(stroke (width {_f(w)}) (type solid))\n'
        f'\t\t(fill {fill_s})\n'
        f'\t\t(layer "{layer}")\n'
        f'\t)'
    )


def _circle(cx: float, cy: float, r: float,
            layer: str, w: float = 0.12) -> str:
    return (
        f'\t(fp_circle\n'
        f'\t\t(center {_f(cx)} {_f(cy)})\n'
        f'\t\t(end {_f(cx + r)} {_f(cy)})\n'
        f'\t\t(stroke (width {_f(w)}) (type solid))\n'
        f'\t\t(fill no)\n'
        f'\t\t(layer "{layer}")\n'
        f'\t)'
    )


def _tht(num: str, x: float, y: float,
         sx: float, sy: float, drill: float, pin1: bool = False) -> str:
    shape = "rect" if pin1 else "circle"
    return (
        f'\t(pad "{num}" thru_hole {shape}\n'
        f'\t\t(at {_f(x)} {_f(y)})\n'
        f'\t\t(size {_f(sx)} {_f(sy)})\n'
        f'\t\t(drill {_f(drill)})\n'
        f'\t\t(layers "*.Cu" "*.Mask")\n'
        f'\t)'
    )


def _smd(num: str, x: float, y: float, sx: float, sy: float,
         rot: float = 0.0) -> str:
    rot_s = f" {_f(rot)}" if rot else ""
    return (
        f'\t(pad "{num}" smd rect\n'
        f'\t\t(at {_f(x)} {_f(y)}{rot_s})\n'
        f'\t\t(size {_f(sx)} {_f(sy)})\n'
        f'\t\t(layers "F.Cu" "F.Paste" "F.Mask")\n'
        f'\t)'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Package generators
# ─────────────────────────────────────────────────────────────────────────────

def _gen_dip(name: str, info: dict) -> str:
    n = int(info.get("pin_count", 8))
    pitch = float(info.get("pitch_mm", 2.54))
    row_sp = float(info.get("row_spacing_mm", 7.62))
    drill = float(info.get("pad_drill_mm", 0.8))
    pad_l = float(info.get("pad_length_mm", 1.6))
    pad_w = float(info.get("pad_width_mm", 1.6))
    body_w = float(info.get("body_width_mm", row_sp * 0.75))
    descr = str(info.get("description", f"{n}-pin DIP"))
    tags = str(info.get("tags", "DIP THT"))

    n_side = n // 2
    half_h = (n_side - 1) / 2.0 * pitch
    lx = -row_sp / 2.0
    rx = row_sp / 2.0
    bx1, bx2 = -body_w / 2.0, body_w / 2.0
    by1 = -half_h - pitch / 2.0
    by2 = half_h + pitch / 2.0
    notch = pitch * 0.5

    p = [_header(name, descr, tags, False)]
    p.append(_prop("Reference", "REF**", 0.0, by1 - 1.5, "F.SilkS"))
    p.append(_prop("Value", name, 0.0, by2 + 1.5, "F.Fab"))

    # Silkscreen body with pin-1 notch
    p.append(_line(bx1, by1 + notch, bx1, by2, "F.SilkS"))
    p.append(_line(bx2, by1, bx2, by2, "F.SilkS"))
    p.append(_line(bx1, by2, bx2, by2, "F.SilkS"))
    p.append(_line(bx1 + notch, by1, bx2, by1, "F.SilkS"))
    p.append(_line(bx1, by1 + notch, bx1 + notch, by1, "F.SilkS"))

    # Courtyard
    cm = 0.5
    p.append(_rect(lx - pad_l / 2 - cm, -half_h - pad_w / 2 - cm,
                   rx + pad_l / 2 + cm, half_h + pad_w / 2 + cm,
                   "F.CrtYd", 0.05))

    # Fab
    p.append(_rect(bx1, by1, bx2, by2, "F.Fab", 0.1))

    # Pads
    for i in range(n_side):
        y = -half_h + i * pitch
        p.append(_tht(str(i + 1), lx, y, pad_l, pad_w, drill, pin1=(i == 0)))
    for i in range(n_side):
        y = half_h - i * pitch
        p.append(_tht(str(n_side + i + 1), rx, y, pad_l, pad_w, drill))

    p.append(")")
    return "\n".join(p)


def _gen_sip(name: str, info: dict) -> str:
    n = int(info.get("pin_count", 9))
    pitch = float(info.get("pitch_mm", 2.54))
    drill = float(info.get("pad_drill_mm", 0.8))
    pad_l = float(info.get("pad_length_mm", 1.6))
    pad_w = float(info.get("pad_width_mm", 1.6))
    descr = str(info.get("description", f"SIP-{n}"))
    tags = str(info.get("tags", "SIP SIL THT"))

    total_w = (n - 1) * pitch
    body_h = 4.0

    p = [_header(name, descr, tags, False)]
    p.append(_prop("Reference", "REF**", total_w / 2, -body_h - 1.5, "F.SilkS"))
    p.append(_prop("Value", name, total_w / 2, pad_w / 2 + 1.5, "F.Fab"))

    # Body above pads
    p.append(_rect(-pitch / 2, -body_h, total_w + pitch / 2, -pad_w / 2 - 0.3,
                   "F.SilkS", 0.12))
    # Pin-1 marker: short bar on left
    p.append(_line(-pad_l / 2 - 0.1, -body_h, -pad_l / 2 - 0.1, -pad_w / 2 - 0.3,
                   "F.SilkS", 0.12))

    cm = 0.5
    p.append(_rect(-pad_l / 2 - cm, -body_h - cm,
                   total_w + pad_l / 2 + cm, pad_w / 2 + cm,
                   "F.CrtYd", 0.05))

    for i in range(n):
        p.append(_tht(str(i + 1), i * pitch, 0.0, pad_l, pad_w, drill, pin1=(i == 0)))

    p.append(")")
    return "\n".join(p)


def _gen_soic(name: str, info: dict) -> str:
    """Handles SOIC, TSSOP, SSOP — all two-row SMD packages."""
    n = int(info.get("pin_count", 8))
    pitch = float(info.get("pitch_mm", 1.27))
    row_sp = float(info.get("row_spacing_mm", 5.4))
    # pad_length = radial dim (toward body), pad_width = tangential (along pitch)
    pad_l = float(info.get("pad_length_mm", 1.55))
    pad_w = float(info.get("pad_width_mm", 0.6))
    body_w = float(info.get("body_width_mm", row_sp - pad_l))
    body_h = float(info.get("body_height_mm", (n // 2 - 1) * pitch + pitch))
    descr = str(info.get("description", f"SOIC-{n}"))
    tags = str(info.get("tags", "SOIC SMD"))

    n_side = n // 2
    half_h = (n_side - 1) / 2.0 * pitch
    lx = -row_sp / 2.0
    rx = row_sp / 2.0
    bx1, bx2 = -body_w / 2.0, body_w / 2.0
    by1, by2 = -body_h / 2.0, body_h / 2.0

    # Gap on body sides to avoid pad overlap in silkscreen
    pad_gap = pad_w / 2.0 + 0.15

    p = [_header(name, descr, tags, True)]
    p.append(_prop("Reference", "REF**", 0.0, by1 - 1.5, "F.SilkS"))
    p.append(_prop("Value", name, 0.0, by2 + 1.5, "F.Fab"))

    # Silkscreen sides (split around pads)
    for sx in (bx1, bx2):
        p.append(_line(sx, by1, sx, -half_h - pad_gap, "F.SilkS"))
        p.append(_line(sx, half_h + pad_gap, sx, by2, "F.SilkS"))
    p.append(_line(bx1, by1, bx2, by1, "F.SilkS"))   # top
    p.append(_line(bx1, by2, bx2, by2, "F.SilkS"))   # bottom
    # Pin-1 chamfer indicator at top-left corner
    chamfer = min(0.6, body_w * 0.15, body_h * 0.15)
    p.append(_line(bx1, by1 + chamfer, bx1 + chamfer, by1, "F.SilkS"))

    cm = 0.5
    p.append(_rect(lx - pad_l / 2 - cm, -half_h - pad_w / 2 - cm,
                   rx + pad_l / 2 + cm, half_h + pad_w / 2 + cm,
                   "F.CrtYd", 0.05))
    p.append(_rect(bx1, by1, bx2, by2, "F.Fab", 0.1))

    # Pads: left side top->bottom (pin 1 top-left), right side bottom->top
    for i in range(n_side):
        y = -half_h + i * pitch
        # For left/right side pads: size_x=pad_l (radial), size_y=pad_w (tangential)
        p.append(_smd(str(i + 1), lx, y, pad_l, pad_w))
    for i in range(n_side):
        y = half_h - i * pitch
        p.append(_smd(str(n_side + i + 1), rx, y, pad_l, pad_w))

    p.append(")")
    return "\n".join(p)


# SOT-23 standard pad layouts (center-to-center from origin)
_SOT23_LAYOUTS: dict[int, list[tuple[float, float, str]]] = {
    3: [(-0.95, 1.3, "1"), (0.95, 1.3, "2"), (0.0, -1.3, "3")],
    5: [(-0.95, 1.3, "1"), (0.0, 1.3, "2"), (0.95, 1.3, "3"),
        (0.95, -1.3, "4"), (-0.95, -1.3, "5")],
    6: [(-0.95, 1.3, "1"), (0.0, 1.3, "2"), (0.95, 1.3, "3"),
        (0.95, -1.3, "4"), (0.0, -1.3, "5"), (-0.95, -1.3, "6")],
}


def _gen_sot23(name: str, info: dict) -> str:
    n = int(info.get("pin_count", 3))
    layout = _SOT23_LAYOUTS.get(n, _SOT23_LAYOUTS[3])
    # Standard SOT-23 pad: 0.55 x 0.85mm
    pad_x = float(info.get("pad_length_mm", 0.85))
    pad_y = float(info.get("pad_width_mm", 0.55))
    descr = str(info.get("description", f"SOT-23-{n}"))
    tags = str(info.get("tags", "SOT-23 SMD"))

    bw, bh = 1.3, 2.9   # body dims
    p = [_header(name, descr, tags, True)]
    p.append(_prop("Reference", "REF**", 0.0, -2.5, "F.SilkS"))
    p.append(_prop("Value", name, 0.0, 2.5, "F.Fab"))

    # Body outline
    bx1, bx2 = -bw / 2, bw / 2
    by1, by2 = -bh / 2, bh / 2
    p.append(_rect(bx1, by1, bx2, by2, "F.SilkS"))
    p.append(_rect(bx1, by1, bx2, by2, "F.Fab", 0.1))

    # Pin-1 dot
    p.append(_circle(-0.95 - pad_x / 2 - 0.4, 1.3, 0.2, "F.SilkS", 0.12))

    # Courtyard
    xs = [x for x, y, _ in layout]
    ys = [y for x, y, _ in layout]
    cm = 0.5
    p.append(_rect(min(xs) - pad_x / 2 - cm, min(ys) - pad_y / 2 - cm,
                   max(xs) + pad_x / 2 + cm, max(ys) + pad_y / 2 + cm,
                   "F.CrtYd", 0.05))

    for x, y, num in layout:
        p.append(_smd(num, x, y, pad_x, pad_y))

    p.append(")")
    return "\n".join(p)


def _gen_qfp(name: str, info: dict) -> str:
    """QFP / TQFP / LQFP — 4-sided, equal pins per side."""
    n = int(info.get("pin_count", 32))
    pitch = float(info.get("pitch_mm", 0.8))
    # pad_length = radial (toward body), pad_width = tangential (along pitch)
    pad_l = float(info.get("pad_length_mm", 1.5))
    pad_w = float(info.get("pad_width_mm", 0.4))
    body_w = float(info.get("body_width_mm", 7.0))
    body_h = float(info.get("body_height_mm", 7.0))
    descr = str(info.get("description", f"QFP-{n}"))
    tags = str(info.get("tags", "QFP SMD"))

    n_side = n // 4
    half_p = (n_side - 1) / 2.0 * pitch

    # Pad row edges (from center)
    left_x = -(body_w / 2 + pad_l / 2)
    right_x = body_w / 2 + pad_l / 2
    top_y = -(body_h / 2 + pad_l / 2)
    bot_y = body_h / 2 + pad_l / 2

    p = [_header(name, descr, tags, True)]
    p.append(_prop("Reference", "REF**", 0.0, top_y - 2.0, "F.SilkS"))
    p.append(_prop("Value", name, 0.0, bot_y + 2.0, "F.Fab"))

    # Silkscreen body — split on all 4 sides to leave room for pads
    pad_gap = pad_w / 2.0 + 0.15
    bx1, bx2 = -body_w / 2, body_w / 2
    by1, by2 = -body_h / 2, body_h / 2

    # Left / right vertical sides
    for sx in (bx1, bx2):
        p.append(_line(sx, by1, sx, -half_p - pad_gap, "F.SilkS"))
        p.append(_line(sx, half_p + pad_gap, sx, by2, "F.SilkS"))
    # Top / bottom horizontal sides
    for sy in (by1, by2):
        p.append(_line(bx1, sy, -half_p - pad_gap, sy, "F.SilkS"))
        p.append(_line(half_p + pad_gap, sy, bx2, sy, "F.SilkS"))

    # Pin-1 chamfer (top-left corner)
    chamfer = min(1.0, body_w * 0.12)
    p.append(_line(bx1, by1 + chamfer, bx1 + chamfer, by1, "F.SilkS"))

    # Fab outline
    p.append(_rect(bx1, by1, bx2, by2, "F.Fab", 0.1))

    # Courtyard
    cm = 0.5
    p.append(_rect(left_x - pad_l / 2 - cm, top_y - pad_l / 2 - cm,
                   right_x + pad_l / 2 + cm, bot_y + pad_l / 2 + cm,
                   "F.CrtYd", 0.05))

    # Pads (KiCad QFP convention: counter-clockwise from pin 1 at top-left of left side)
    # Left side  (pins 1..n_side):  x=left_x, y from top to bottom
    for i in range(n_side):
        y = -half_p + i * pitch
        p.append(_smd(str(i + 1), left_x, y, pad_l, pad_w))

    # Bottom side (pins n_side+1 .. 2*n_side): y=bot_y, x from left to right
    base = n_side
    for i in range(n_side):
        x = -half_p + i * pitch
        # Swap so radial dim is along y (vertical) for top/bottom pads
        p.append(_smd(str(base + i + 1), x, bot_y, pad_w, pad_l))

    # Right side (pins 2*n_side+1 .. 3*n_side): x=right_x, y from bottom to top
    base = 2 * n_side
    for i in range(n_side):
        y = half_p - i * pitch
        p.append(_smd(str(base + i + 1), right_x, y, pad_l, pad_w))

    # Top side (pins 3*n_side+1 .. 4*n_side): y=top_y, x from right to left
    base = 3 * n_side
    for i in range(n_side):
        x = half_p - i * pitch
        p.append(_smd(str(base + i + 1), x, top_y, pad_w, pad_l))

    p.append(")")
    return "\n".join(p)


def _gen_qfn(name: str, info: dict) -> str:
    """QFN / DFN — same as QFP but pads flush to body edge."""
    n = int(info.get("pin_count", 16))
    pitch = float(info.get("pitch_mm", 0.5))
    pad_l = float(info.get("pad_length_mm", 0.35))
    pad_w = float(info.get("pad_width_mm", 0.25))
    body_w = float(info.get("body_width_mm", 3.0))
    body_h = float(info.get("body_height_mm", 3.0))
    exp_pad = float(info.get("exposed_pad_mm", 0))
    descr = str(info.get("description", f"QFN-{n}"))
    tags = str(info.get("tags", "QFN SMD"))

    n_side = n // 4
    half_p = (n_side - 1) / 2.0 * pitch

    # QFN pads sit at the body edge: center at body_edge - pad_l/2
    left_x = -(body_w / 2 - pad_l / 2)
    right_x = body_w / 2 - pad_l / 2
    top_y = -(body_h / 2 - pad_l / 2)
    bot_y = body_h / 2 - pad_l / 2

    p = [_header(name, descr, tags, True)]
    p.append(_prop("Reference", "REF**", 0.0, -(body_h / 2) - 1.5, "F.SilkS"))
    p.append(_prop("Value", name, 0.0, body_h / 2 + 1.5, "F.Fab"))

    bx1, bx2 = -body_w / 2, body_w / 2
    by1, by2 = -body_h / 2, body_h / 2
    pad_gap = pad_w / 2.0 + 0.15

    # Silkscreen (split sides)
    for sx in (bx1, bx2):
        p.append(_line(sx, by1, sx, -half_p - pad_gap, "F.SilkS"))
        p.append(_line(sx, half_p + pad_gap, sx, by2, "F.SilkS"))
    for sy in (by1, by2):
        p.append(_line(bx1, sy, -half_p - pad_gap, sy, "F.SilkS"))
        p.append(_line(half_p + pad_gap, sy, bx2, sy, "F.SilkS"))

    chamfer = min(0.5, body_w * 0.1)
    p.append(_line(bx1, by1 + chamfer, bx1 + chamfer, by1, "F.SilkS"))

    p.append(_rect(bx1, by1, bx2, by2, "F.Fab", 0.1))

    cm = 0.5
    p.append(_rect(bx1 - cm, by1 - cm, bx2 + cm, by2 + cm, "F.CrtYd", 0.05))

    # Pads (same counter-clockwise order as QFP)
    for i in range(n_side):
        p.append(_smd(str(i + 1), left_x, -half_p + i * pitch, pad_l, pad_w))
    base = n_side
    for i in range(n_side):
        p.append(_smd(str(base + i + 1), -half_p + i * pitch, bot_y, pad_w, pad_l))
    base = 2 * n_side
    for i in range(n_side):
        p.append(_smd(str(base + i + 1), right_x, half_p - i * pitch, pad_l, pad_w))
    base = 3 * n_side
    for i in range(n_side):
        p.append(_smd(str(base + i + 1), half_p - i * pitch, top_y, pad_w, pad_l))

    # Exposed thermal pad
    if exp_pad and exp_pad > 0:
        ep = float(exp_pad)
        p.append(_smd(str(n + 1), 0.0, 0.0, ep, ep))

    p.append(")")
    return "\n".join(p)


# ─────────────────────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch(fp_name: str, info: dict) -> str:
    """Pick the right generator based on package_type."""
    pkg = str(info.get("package_type", "")).upper()
    if pkg in ("DIP", "PDIP", "CDIP"):
        return _gen_dip(fp_name, info)
    if pkg in ("SIP", "SIL", "ZIP"):
        return _gen_sip(fp_name, info)
    if pkg in ("SOIC", "SOP", "SOJ"):
        return _gen_soic(fp_name, info)
    if pkg in ("TSSOP", "SSOP", "MSOP", "QSOP", "TSOP"):
        # Same generator as SOIC, Claude will provide correct pitch/row_sp
        return _gen_soic(fp_name, info)
    if pkg in ("SOT23", "SOT-23"):
        return _gen_sot23(fp_name, info)
    if pkg in ("QFP", "TQFP", "LQFP", "PQFP", "FQFP"):
        return _gen_qfp(fp_name, info)
    if pkg in ("QFN", "DFN", "WQFN", "MLPQ", "MLP"):
        return _gen_qfn(fp_name, info)
    # Fallback: choose by is_smd and pin count
    is_smd = bool(info.get("is_smd", False))
    n = int(info.get("pin_count", 8))
    if not is_smd:
        return _gen_dip(fp_name, info) if n > 1 and n % 2 == 0 else _gen_sip(fp_name, info)
    if n <= 6:
        return _gen_sot23(fp_name, info)
    if n % 4 == 0 and n > 16:
        return _gen_qfp(fp_name, info)
    return _gen_soic(fp_name, info)


# ─────────────────────────────────────────────────────────────────────────────
# Datasheet reader
# ─────────────────────────────────────────────────────────────────────────────

_PACKAGE_PROMPT = """\
You are a KiCad footprint engineer. Read this component datasheet and extract
the FIRST (or main) package / footprint dimensions.

Return ONLY valid JSON — no markdown, no explanation:
{
  "package_type": "<DIP|SIP|SOIC|TSSOP|SSOP|MSOP|SOT23|QFP|TQFP|LQFP|QFN|DFN|TO92|TO220|TO263>",
  "pin_count": <int>,
  "pitch_mm": <float, pin-to-pin center spacing>,
  "row_spacing_mm": <float or null, center-to-center row distance — null for SOT23/QFP/QFN>,
  "pad_drill_mm": <float or null, drill diameter for THT — null for SMD>,
  "pad_length_mm": <float, pad dimension toward body (radial)>,
  "pad_width_mm": <float, pad dimension along pitch direction (tangential)>,
  "body_width_mm": <float, IC body width>,
  "body_height_mm": <float, IC body height/length>,
  "is_smd": <bool>,
  "exposed_pad_mm": <float, thermal exposed pad size — 0 if none>,
  "description": "<brief package description, e.g. 8-pin DIP 300mil>",
  "tags": "<space-separated KiCad tags, e.g. DIP THT 300mil>"
}
If a value is truly unknown use null (not 0) and I will apply the package standard default.
"""


def _read_datasheet_package(part_name: str, url: str) -> Optional[dict]:
    """Fetch the datasheet at *url* and ask Claude for package dimensions.

    Returns a dict matching the JSON schema above, or None on any failure.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (envil-kicad-agent/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception as exc:
        print(f"[create_footprint] fetch {url}: {exc}")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    content_type = url.rsplit(".", 1)[-1].lower()
    if content_type == "pdf":
        b64 = base64.b64encode(raw).decode()
        user_content: list = [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            },
            {"type": "text", "text": f"Part: {part_name}\n\n{_PACKAGE_PROMPT}"},
        ]
    else:
        # HTML / text — send as plain text (truncated to 40k chars)
        text_body = raw.decode("utf-8", errors="replace")[:40_000]
        user_content = [
            {"type": "text",
             "text": f"Part: {part_name}\n\nDatasheet text:\n{text_body}\n\n{_PACKAGE_PROMPT}"},
        ]

    try:
        resp = client.messages.create(
            # Sonnet 4.6, not Haiku — Haiku misreads package/pad tables from
            # datasheet images, giving wrong footprint dimensions.
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_content}],
        )
        text = resp.content[0].text.strip()
        # Strip any accidental markdown fences
        text = re.sub(r"^```[^\n]*\n?", "", text)
        text = re.sub(r"```$", "", text.rstrip())
        return json.loads(text)
    except Exception as exc:
        print(f"[create_footprint] Claude package extraction failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Symbol reader helper — get Datasheet property without importing privates
# ─────────────────────────────────────────────────────────────────────────────

def _get_symbol_props(lib_id: str) -> dict[str, str]:
    """Parse the .kicad_sym for *lib_id* and return all (key, value) properties."""
    import sexpdata  # only needed here
    from .symbol_geom import _sym_roots

    nick, _, part = lib_id.partition(":")
    if not part:
        return {}

    src = None
    for root in _sym_roots():
        for cand in (
            root / f"{nick}.kicad_symdir" / f"{part}.kicad_sym",
            root / f"{nick}.kicad_sym",
        ):
            if cand.exists():
                src = cand
                break
        if src:
            break
    if not src:
        return {}

    try:
        tree = sexpdata.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return {}

    def _head(n):
        return str(n[0]) if isinstance(n, list) and n else ""

    def _find_sym(node):
        if _head(node) == "symbol":
            raw = str(node[1]) if len(node) > 1 else ""
            if raw == part or raw.startswith(part + "_"):
                return node
        for child in (node if isinstance(node, list) else []):
            if isinstance(child, list):
                r = _find_sym(child)
                if r is not None:
                    return r
        return None

    sym = _find_sym(tree)
    if sym is None:
        return {}

    props: dict[str, str] = {}
    for child in sym:
        if isinstance(child, list) and _head(child) == "property":
            key = str(child[1]) if len(child) > 1 else ""
            val = str(child[2]) if len(child) > 2 else ""
            props[key] = val
    return props


# ─────────────────────────────────────────────────────────────────────────────
# fp-lib-table updater
# ─────────────────────────────────────────────────────────────────────────────

def _kicad_user_fp_table() -> Optional[Path]:
    """Return the writable KiCad user fp-lib-table path (AppData/Roaming/kicad)."""
    base = Path.home() / "AppData" / "Roaming" / "kicad"
    if not base.exists():
        return None
    for ver in sorted(base.iterdir(), reverse=True):
        if ver.is_dir():
            t = ver / "fp-lib-table"
            if t.exists():
                return t
    return None


def _register_fp_lib(nick: str, pretty_path: Path) -> None:
    """Add a (lib ...) entry to the fp-lib-table if not already present."""
    # Prefer the KiCad user AppData table (writable); fall back to envil_home table.
    table = _kicad_user_fp_table() or fp_lib_table()
    uri = str(pretty_path).replace("\\", "/")

    if table.exists():
        text = table.read_text(encoding="utf-8")
        if f'(name "{nick}")' in text:
            return  # already registered
        # Insert before last closing paren
        entry = f'  (lib (name "{nick}")(type "KiCad")(uri "{uri}")(options "")(descr ""))\n'
        idx = text.rfind(")")
        if idx >= 0:
            text = text[:idx] + entry + text[idx:]
        table.write_text(text, encoding="utf-8")
    else:
        # Create from scratch
        table.parent.mkdir(parents=True, exist_ok=True)
        table.write_text(
            f'(fp_lib_table\n'
            f'  (version 7)\n'
            f'  (lib (name "{nick}")(type "KiCad")(uri "{uri}")(options "")(descr ""))\n'
            f')\n',
            encoding="utf-8",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def create_footprint(
    lib_id: str,
    fp_lib_nick: str = "",
    fp_name: str = "",
    datasheet_url: str = "",
    link_symbol: bool = True,
) -> dict:
    """Generate a .kicad_mod file for the component identified by *lib_id*.

    Args:
        lib_id:        e.g. "Timer:NE555" — the KiCad library:part identifier.
        fp_lib_nick:   Footprint library nickname (creates <nick>.pretty dir).
                       Defaults to the symbol library nick.
        fp_name:       Base name for the .kicad_mod file.
                       Defaults to the part name.
        datasheet_url: Override URL; if empty, read from symbol's Datasheet prop.
        link_symbol:   If True, update the symbol's Footprint property to point
                       at the new footprint.

    Returns:
        {"ok": True, "path": <str>, "package": <dict>}
        or {"ok": False, "error": <str>}
    """
    nick, _, part = lib_id.partition(":")
    if not part:
        return {"ok": False, "error": f"lib_id must be 'Nick:Part', got: {lib_id!r}"}

    fp_lib_nick = fp_lib_nick.strip() or nick
    fp_name = fp_name.strip() or part

    # Resolve Datasheet URL from symbol if not provided
    url = datasheet_url.strip()
    props: dict[str, str] = {}
    if not url or link_symbol:
        props = _get_symbol_props(lib_id)
    if not url:
        url = props.get("Datasheet", "").strip()
    if not url:
        return {"ok": False,
                "error": f"No Datasheet URL found in symbol {lib_id}. "
                         "Please set the Datasheet property first "
                         "(use edit_symbol with op=set_property, key=Datasheet)."}

    # Fetch & extract package info
    pkg_info = _read_datasheet_package(part, url)
    if not pkg_info:
        return {"ok": False,
                "error": f"Could not extract package info from datasheet: {url}"}

    # Generate .kicad_mod content
    content = _dispatch(fp_name, pkg_info)

    # Write to file
    pretty_dir = fp_lib_dir() / f"{fp_lib_nick}.pretty"
    pretty_dir.mkdir(parents=True, exist_ok=True)
    out_file = pretty_dir / f"{fp_name}.kicad_mod"
    out_file.write_text(content, encoding="utf-8")

    # Register in fp-lib-table
    try:
        _register_fp_lib(fp_lib_nick, pretty_dir)
    except Exception as exc:
        print(f"[create_footprint] fp-lib-table update skipped: {exc}")

    # Update symbol's Footprint property
    fp_ref = f"{fp_lib_nick}:{fp_name}"
    if link_symbol:
        try:
            from .edit_symbol import apply_symbol_ops
            apply_symbol_ops(lib_id, [
                {"op": "set_property", "key": "Footprint", "value": fp_ref}
            ])
        except Exception as exc:
            print(f"[create_footprint] symbol Footprint link failed: {exc}")

    return {
        "ok": True,
        "path": str(out_file),
        "fp_ref": fp_ref,
        "package": pkg_info,
    }


def _unregister_fp_lib(nick: str) -> list:
    """Remove the ``(lib (name "<nick>") ...)`` entry from the footprint
    library table(s). Reverse of ``_register_fp_lib``; backs each table up to
    <name>.envil-bak before the first edit. Returns human-readable notes."""
    notes: list = []
    seen: set = set()
    for table in (_kicad_user_fp_table(), fp_lib_table()):
        if not table or not table.exists():
            continue
        key = str(table)
        if key in seen:
            continue
        seen.add(key)
        text = table.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        kept = [ln for ln in lines if f'(name "{nick}")' not in ln]
        if len(kept) == len(lines):
            continue
        bak = table.with_name(table.name + ".envil-bak")
        if not bak.exists():
            bak.write_text(text, encoding="utf-8")
        table.write_text("".join(kept), encoding="utf-8")
        notes.append(f"unregistered {nick!r} from {table}")
    return notes


def delete_footprint(
    fp_ref: str,
    unlink_symbol: str = "",
    unregister_if_empty: bool = True,
    force: bool = False,
) -> dict:
    """Delete a footprint (.kicad_mod) — the reverse of create_footprint.

    Only footprints in the writable envil footprint library
    (``fp_lib_dir()/<nick>.pretty``) can be deleted; stock KiCad footprints
    live elsewhere and are never touched. When the delete empties the
    ``.pretty`` folder it is removed and (with ``unregister_if_empty``) the
    library is stripped from the fp-lib-table.

    Args:
        fp_ref:              'FpLibNick:FpName' (e.g. 'Custom:BQ76952').
        unlink_symbol:       optional symbol lib_id whose Footprint property
                             should be cleared (undo create_footprint's link).
        unregister_if_empty: remove the library from the fp-lib-table when the
                             delete empties its .pretty folder (default True).
        force:               delete even a footprint not generated by envil
                             (no ``envil-fp-gen`` tag) (default False).

    Returns ``{"ok": True, ...}`` or ``{"ok": False, "error": ...}``.
    """
    nick, _, name = fp_ref.partition(":")
    if not name:
        return {"ok": False, "error": f"fp_ref must be 'Nick:Name', got {fp_ref!r}"}

    pretty = fp_lib_dir() / f"{nick}.pretty"
    mod = pretty / f"{name}.kicad_mod"
    if not mod.exists():
        return {"ok": False,
                "error": f"footprint {fp_ref} not found at {mod}. Only "
                         f"footprints in the envil footprint library "
                         f"({fp_lib_dir()}) can be deleted."}

    try:
        text = mod.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if "envil-fp-gen" not in text and not force:
        return {"ok": False,
                "error": f"{fp_ref} was not generated by envil. Refusing to "
                         f"delete — pass force=true to proceed."}

    try:
        mod.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"could not delete {mod}: {exc}"}

    library_removed = False
    unregister_notes: list = []
    try:
        if pretty.exists() and not any(pretty.iterdir()):
            pretty.rmdir()
            library_removed = True
    except OSError:
        pass
    if library_removed and unregister_if_empty:
        try:
            unregister_notes = _unregister_fp_lib(nick)
        except Exception as exc:  # noqa: BLE001
            unregister_notes = [f"unregister skipped: {exc}"]

    symbol_unlinked = False
    if unlink_symbol:
        try:
            from .edit_symbol import apply_symbol_ops
            apply_symbol_ops(unlink_symbol, [
                {"op": "set_property", "key": "Footprint", "value": ""}
            ])
            symbol_unlinked = True
        except Exception as exc:  # noqa: BLE001
            print(f"[delete_footprint] symbol Footprint unlink failed: {exc}")

    return {
        "ok": True,
        "fp_ref": fp_ref,
        "deleted": str(mod),
        "library_removed": library_removed,
        "unregister": unregister_notes,
        "symbol_unlinked": symbol_unlinked,
    }


def create_footprint_from_info(
    lib_id: str,
    pkg_info: dict,
    fp_lib_nick: str = "",
    fp_name: str = "",
) -> dict:
    """Generate a .kicad_mod using a pre-extracted package dict (no PDF fetch).

    Used by create_component so the datasheet is fetched only once.

    Args:
        lib_id:      e.g. 'Custom:TMC2209'
        pkg_info:    package dict already extracted by Claude (same schema as
                     _read_datasheet_package returns).
        fp_lib_nick: footprint library nickname (default = symbol lib nick).
        fp_name:     .kicad_mod base name (default = part name).

    Returns same shape as create_footprint().
    """
    nick, _, part = lib_id.partition(":")
    if not part:
        return {"ok": False, "error": f"lib_id must be Nick:Part, got {lib_id!r}"}

    fp_lib_nick = fp_lib_nick.strip() or nick
    fp_name = fp_name.strip() or part

    content = _dispatch(fp_name, pkg_info)

    pretty_dir = fp_lib_dir() / f"{fp_lib_nick}.pretty"
    pretty_dir.mkdir(parents=True, exist_ok=True)
    out_file = pretty_dir / f"{fp_name}.kicad_mod"
    out_file.write_text(content, encoding="utf-8")

    try:
        _register_fp_lib(fp_lib_nick, pretty_dir)
    except Exception as exc:
        print(f"[create_footprint_from_info] fp-lib-table update skipped: {exc}")

    return {
        "ok": True,
        "path": str(out_file),
        "fp_ref": f"{fp_lib_nick}:{fp_name}",
        "package": pkg_info,
    }
