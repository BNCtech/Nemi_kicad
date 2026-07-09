"""Create a brand-new KiCad schematic symbol (.kicad_sym) from a datasheet.

Flow
----
1. Fetch the datasheet PDF / page.
2. ONE Claude call extracts: full pin table + package dimensions.
3. Layout pins (inputs/power_in on left, outputs/power_out on right).
4. Write .kicad_sym to Custom.kicad_symdir/<part>.kicad_sym.
5. Return the extracted info dict so the caller can pass it straight to
   create_footprint_from_info() without fetching the PDF a second time.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import urllib.request
from pathlib import Path
from typing import Optional

import anthropic

from .symbol_geom import _sym_roots


# ─────────────────────────────────────────────────────────────────────────────
# Formatter
# ─────────────────────────────────────────────────────────────────────────────

def _f(v: float, d: int = 4) -> str:
    s = f"{v:.{d}f}".rstrip("0").rstrip(".")
    return s if s else "0"


# ─────────────────────────────────────────────────────────────────────────────
# Claude prompt — one call for BOTH pins and package
# ─────────────────────────────────────────────────────────────────────────────

_FULL_PROMPT = """\
You are a KiCad library engineer. Read this component datasheet and extract
EVERYTHING needed to build a schematic symbol and PCB footprint.

Return ONLY valid JSON — no markdown, no explanation:
{
  "part_name": "<manufacturer part number, e.g. TMC2209-LA>",
  "description": "<one-line description, e.g. Stepper motor driver, 2A, SPI/UART>",
  "manufacturer": "<manufacturer name>",
  "ref_prefix": "<U for IC/driver/sensor, Q for transistor, R for resistor, C for cap>",
  "pins": [
    {
      "number": "<pin number as string>",
      "name": "<pin name exactly as in datasheet>",
      "etype": "<power_in|power_out|input|output|bidirectional|passive|no_connect|open_collector|open_emitter|unspecified>",
      "side": "<left|right|top|bottom — the edge of the package where THIS pin is physically drawn in the datasheet pinout figure; null only if the datasheet has no package drawing>",
      "order": "<1-based position of this pin along its side as a string; count left/right sides TOP->BOTTOM and top/bottom sides LEFT->RIGHT; null only if no package drawing>"
    }
  ],
  "package": {
    "type": "<DIP|SIP|SOIC|TSSOP|SSOP|SOT23|QFP|TQFP|LQFP|QFN|DFN|WQFN>",
    "pin_count": <int>,
    "pitch_mm": <float>,
    "row_spacing_mm": <float or null>,
    "pad_drill_mm": <float or null>,
    "pad_length_mm": <float>,
    "pad_width_mm": <float>,
    "body_width_mm": <float>,
    "body_height_mm": <float>,
    "is_smd": <bool>,
    "exposed_pad_mm": <float or 0>,
    "description": "<e.g. QFN-28 4x4mm 0.5mm pitch>",
    "tags": "<space-separated KiCad tags>"
  }
}

Rules:
- List EVERY pin from the pinout table — do not skip any.
- Pin numbers must be strings ("1" not 1).
- etype must be exactly one of the listed values.
- Use "bidirectional" for I/O, "passive" for analog with no clear direction.
- Use "no_connect" for NC or reserved pins.
- If a pin is both GND and a thermal pad, use "power_in" with name "GND".
- REPRODUCE THE DATASHEET PINOUT EXACTLY. Find the package pinout figure (the
  physical top-view drawing, e.g. "Figure N. <PART> <PKG> pinout") and, for
  each pin, record which edge it sits on ("side") and its position along that
  edge ("order"). This must match the drawing for THIS exact part number.
    * left / right edges: order 1 is the TOP-most pin, counting downward.
    * top / bottom edges: order 1 is the LEFT-most pin, counting rightward.
- Follow the pin NUMBERS around the package exactly as printed and pair each
  number with the name on the SAME lead — never shift a name to another lead.
- Do NOT regroup pins by function or move them to a "nicer" edge; keep every
  pin on the side and in the order the datasheet drawing shows it.
- Only if the datasheet has no package drawing at all, set "side"/"order" to
  null and list pins in numeric order.
"""


def _read_datasheet_full(part_name: str, url: str) -> Optional[dict]:
    """Fetch *url* and ask Claude for the complete pin+package info in one call."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (envil-kicad-agent/1.0)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception as exc:
        print(f"[create_symbol] fetch {url}: {exc}")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    ext = url.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        b64 = base64.b64encode(raw).decode()
        user_content: list = [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": f"Part: {part_name}\n\n{_FULL_PROMPT}"},
        ]
    else:
        text_body = raw.decode("utf-8", errors="replace")[:40_000]
        user_content = [
            {"type": "text",
             "text": f"Part: {part_name}\n\nDatasheet text:\n{text_body}\n\n{_FULL_PROMPT}"},
        ]

    try:
        resp = client.messages.create(
            # Sonnet 4.6, not Haiku — Haiku misreads datasheet pin tables and
            # chip markings, which broke image/datasheet-based symbol creation.
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": user_content}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"^```[^\n]*\n?", "", text)
        text = re.sub(r"```$", "", text.rstrip())
        return json.loads(text)
    except Exception as exc:
        print(f"[create_symbol] Claude extraction failed: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Pin layout
# ─────────────────────────────────────────────────────────────────────────────

_LEFT_ETYPES = {"power_in", "input"}
_RIGHT_ETYPES = {"output", "power_out", "open_collector", "open_emitter"}
_PIN_ORDER = {
    "power_in": 0, "power_out": 0,
    "input": 1, "output": 1,
    "bidirectional": 2,
    "passive": 3,
    "unspecified": 4,
    "no_connect": 9,
}

# Package types that have pins on all 4 sides
_FOUR_SIDED_PKGS = {"QFN", "DFN", "WQFN", "VQFN", "HVQFN", "QFP", "TQFP", "LQFP", "PQFP"}


def _is_four_sided(pkg_type: str) -> bool:
    t = pkg_type.upper()
    return any(k in t for k in _FOUR_SIDED_PKGS)


def _layout_pins(pins: list) -> tuple[list, list]:
    """Split pins into (left_list, right_list) by electrical type, then balance."""
    left = [p for p in pins if p["etype"] in _LEFT_ETYPES]
    right = [p for p in pins if p["etype"] in _RIGHT_ETYPES]
    other = [p for p in pins if p["etype"] not in _LEFT_ETYPES | _RIGHT_ETYPES]

    half = (len(other) + 1) // 2
    left += other[:half]
    right += other[half:]

    def _key(p):
        return (_PIN_ORDER.get(p["etype"], 5), p["number"].zfill(4))

    left.sort(key=_key)
    right.sort(key=_key)
    return left, right


def _layout_pins_4side(pins: list) -> tuple[list, list, list, list]:
    """For QFN/QFP: distribute pins around 4 sides by sequential pin number.

    Returns (left, top, right, bottom) where each list is ordered as it
    appears visually on that edge (left: bottom→top, top: left→right,
    right: top→bottom, bottom: right→left).
    """
    def _num(p):
        try:
            return int(p["number"])
        except (ValueError, TypeError):
            return 9999

    sorted_pins = sorted(pins, key=_num)
    n = len(sorted_pins)
    q, r = divmod(n, 4)

    # Distribute remainder pins to the first sides
    sizes = [q + (1 if i < r else 0) for i in range(4)]
    groups, idx = [], 0
    for s in sizes:
        groups.append(sorted_pins[idx: idx + s])
        idx += s

    # Standard QFN JEDEC counter-clockwise from pin 1 (bottom-left corner):
    #   group 0 (1..N/4)   = LEFT side,   physical order bottom→top
    #   group 1 (N/4..N/2) = TOP side,    physical order left→right
    #   group 2 (N/2..3N/4)= RIGHT side,  physical order top→bottom
    #   group 3 (3N/4..N)  = BOTTOM side, physical order right→left
    #
    # KiCad symbol display convention:
    #   left pins:   i=0 is TOP, increasing i goes DOWN  → reverse group 0
    #   top pins:    i=0 is LEFT, increasing i goes RIGHT → group 1 as-is
    #   right pins:  i=0 is TOP, increasing i goes DOWN  → group 2 as-is
    #   bottom pins: i=0 is LEFT, increasing i goes RIGHT, but physical is right→left → reverse group 3
    left   = list(reversed(groups[0]))  # pin 1 at bottom-display, pin N/4 at top-display
    top    = groups[1]                   # pin N/4+1 at left-display
    right  = groups[2]                   # pin N/2+1 at top-display
    bottom = list(reversed(groups[3]))   # pin N at left-display, pin 3N/4+1 at right-display
    return left, top, right, bottom


def _layout_pins_by_side(pins: list) -> Optional[tuple[list, list, list, list]]:
    """Place pins exactly where the datasheet package figure draws them.

    Uses each pin's extracted ``side`` (left/right/top/bottom) and ``order``
    (1-based position along that side) so the generated symbol matches the
    physical pinout of the specific part number instead of guessing how the
    numbering wraps around the package.  Returns ``(left, top, right, bottom)``
    already in KiCad display order, or ``None`` when the datasheet gave no
    usable side info (caller then falls back to the etype heuristic).
    """
    valid = {"left", "right", "top", "bottom"}
    sided = [p for p in pins if str(p.get("side", "")).lower() in valid]
    # Need almost every pin to carry a side, else the drawing wasn't really read.
    if len(sided) < max(4, int(len(pins) * 0.75)):
        return None

    def _key(p):
        o = p.get("order")
        try:
            return (0, int(str(o)))
        except (ValueError, TypeError):
            pass
        try:                       # fall back to pin number when order missing
            return (1, int(str(p.get("number"))))
        except (ValueError, TypeError):
            return (2, 0)

    buckets: dict = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        s = str(p.get("side", "")).lower()
        if s not in valid:         # stray sideless pin (e.g. thermal pad)
            s = "left" if p.get("etype") in _LEFT_ETYPES else "right"
        buckets[s].append(p)
    for lst in buckets.values():
        lst.sort(key=_key)

    # datasheet order already equals KiCad display order:
    #   left/right: order 1 = top-most   -> index 0 at top  ✓
    #   top/bottom: order 1 = left-most   -> index 0 at left ✓
    return buckets["left"], buckets["top"], buckets["right"], buckets["bottom"]


# ─────────────────────────────────────────────────────────────────────────────
# .kicad_sym generator
# ─────────────────────────────────────────────────────────────────────────────

_SPACING = 2.54   # mm between pin rows (100 mil)
_PIN_LEN = 2.54   # mm pin stub length
_BODY_W  = 10.16  # mm default body width (400 mil) — wide enough for long names


def _gen_sym_content(part_name: str, info: dict) -> str:
    pins    = info.get("pins", [])
    ref     = info.get("ref_prefix", "U")
    desc    = info.get("description", part_name)
    pkg     = info.get("package", {})
    pkg_type = pkg.get("type", "")

    four_sided = _is_four_sided(pkg_type)

    # Prefer the datasheet's own physical placement (side + order) so the symbol
    # matches the exact part number's pinout drawing.  Fall back to the etype
    # heuristic only when no package figure was available to read sides from.
    by_side = _layout_pins_by_side(pins)
    if by_side is not None:
        left_pins, top_pins, right_pins, bottom_pins = by_side
    elif four_sided:
        left_pins, top_pins, right_pins, bottom_pins = _layout_pins_4side(pins)
    else:
        left_pins, right_pins = _layout_pins(pins)
        top_pins, bottom_pins = [], []

    n_lr = max(len(left_pins), len(right_pins), 1)
    n_tb = max(len(top_pins), len(bottom_pins), 0)

    # Body must be big enough that opposing pin names don't collide in the
    # middle.  ~1.1 mm per character is a safe advance width at 1.27 mm font.
    _CH = 1.1

    def _maxname(lst):
        return max((len(str(p.get("name", ""))) for p in lst), default=0)

    def _rows(v):  # smallest whole number of 100-mil rows covering v
        return int(math.ceil(max(v, 0.0) / _SPACING - 1e-9))

    need_w = (_maxname(left_pins) + _maxname(right_pins)) * _CH + 2 * _SPACING
    need_h = (_maxname(top_pins) + _maxname(bottom_pins)) * _CH + 2 * _SPACING

    n_w = max(_rows(_BODY_W), n_tb + 1, _rows(need_w))
    n_h = max(n_lr + 1, _rows(need_h))
    body_w = n_w * _SPACING
    body_h = n_h * _SPACING
    cx     = body_w / 2.0

    # KiCad symbol coords: y=0 at top, y decreases downward
    by1 = 0.0
    by2 = -body_h
    bx1 = 0.0
    bx2 = body_w

    # Left/right pin tip x
    lx = -_PIN_LEN
    rx = body_w + _PIN_LEN
    # centre the left/right rows vertically, staying on the 100-mil grid
    first_y = -(1 + (n_h - (n_lr + 1)) // 2) * _SPACING

    # Top/bottom pin tip y (stubs point inward toward body)
    ty   = +_PIN_LEN            # top tips above body (rotation=270 → stub goes down)
    boty = -(body_h + _PIN_LEN) # bottom tips below body (rotation=90 → stub goes up)
    # centre the top/bottom rows horizontally, staying on the 100-mil grid
    first_x = (1 + (n_w - (n_tb + 1)) // 2) * _SPACING  # x of first top/bottom pin

    L = []

    def line(s: str) -> None:
        L.append(s)

    line("(kicad_symbol_lib")
    line("\t(version 20251024)")
    line('\t(generator "envil-sym-gen")')
    line('\t(generator_version "1.0")')
    line(f'\t(symbol "{part_name}"')
    line("\t\t(exclude_from_sim no)")
    line("\t\t(in_bom yes)")
    line("\t\t(on_board yes)")
    line("\t\t(in_pos_files yes)")

    def _prop(name, val, x, y, hide=False, size=1.27):
        h = "\n\t\t\t(hide yes)" if hide else ""
        line(f'\t\t(property "{name}" "{val}"')
        line(f"\t\t\t(at {_f(x)} {_f(y)} 0)")
        line(f"\t\t\t(show_name no)")
        line(f"\t\t\t(do_not_autoplace no){h}")
        line(f"\t\t\t(effects")
        line(f"\t\t\t\t(font")
        line(f"\t\t\t\t\t(size {_f(size)} {_f(size)})")
        line(f"\t\t\t\t)")
        line(f"\t\t\t)")
        line("\t\t)")

    _prop("Reference", ref,      cx, 1.27)
    _prop("Value",     part_name, cx, by2 - 1.5)
    _prop("Footprint", "",        0,  0,    hide=True)
    _prop("Datasheet", "",        0,  0,    hide=True)
    _prop("Description", desc,    0,  0,    hide=True)

    # ── graphics unit (body rectangle)
    line(f'\t\t(symbol "{part_name}_0_1"')
    line(f"\t\t\t(rectangle")
    line(f"\t\t\t\t(start {_f(bx1)} {_f(by1)})")
    line(f"\t\t\t\t(end   {_f(bx2)} {_f(by2)})")
    line(f"\t\t\t\t(stroke (width 0) (type default))")
    line(f"\t\t\t\t(fill   (type none))")
    line(f"\t\t\t)")
    line(f"\t\t)")

    # ── pins unit
    line(f'\t\t(symbol "{part_name}_1_1"')

    def _pin(etype, name, number, x, y, rot):
        line(f"\t\t\t(pin {etype} line")
        line(f"\t\t\t\t(at {_f(x)} {_f(y)} {rot})")
        line(f"\t\t\t\t(length {_f(_PIN_LEN)})")
        line(f'\t\t\t\t(name "{name}"')
        line(f"\t\t\t\t\t(effects (font (size 1.27 1.27)))")
        line(f"\t\t\t\t)")
        line(f'\t\t\t\t(number "{number}"')
        line(f"\t\t\t\t\t(effects (font (size 1.27 1.27)))")
        line(f"\t\t\t\t)")
        line(f"\t\t\t)")

    for i, p in enumerate(left_pins):
        _pin(p["etype"], p["name"], p["number"], lx, first_y - i * _SPACING, 0)

    for i, p in enumerate(right_pins):
        _pin(p["etype"], p["name"], p["number"], rx, first_y - i * _SPACING, 180)

    # 4-sided packages: top and bottom rows
    for i, p in enumerate(top_pins):
        _pin(p["etype"], p["name"], p["number"], first_x + i * _SPACING, ty, 270)

    for i, p in enumerate(bottom_pins):
        _pin(p["etype"], p["name"], p["number"], first_x + i * _SPACING, boty, 90)

    line("\t\t)")   # close symbol_1_1
    line("\t\t(embedded_fonts no)")
    line("\t)")     # close symbol
    line(")")       # close kicad_symbol_lib

    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# sym-lib-table updater
# ─────────────────────────────────────────────────────────────────────────────

def _register_sym_lib(nick: str, uri: str) -> None:
    """Add a (lib ...) entry to the KiCad user sym-lib-table if not present."""
    import os
    table = Path.home() / "AppData" / "Roaming" / "kicad"
    # Find the versioned dir
    if table.exists():
        for ver in sorted(table.iterdir(), reverse=True):
            t = ver / "sym-lib-table"
            if t.exists():
                text = t.read_text(encoding="utf-8")
                if f'(name "{nick}")' in text:
                    return
                entry = (
                    f'\t(lib (name "{nick}") (type "KiCad") '
                    f'(uri "{uri}") (options "") (descr "Envil custom symbols"))\n'
                )
                idx = text.rfind(")")
                if idx >= 0:
                    text = text[:idx] + entry + text[idx:]
                t.write_text(text, encoding="utf-8")
                return


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def create_symbol(
    lib_id: str,
    datasheet_url: str,
    lib_nick: str = "Custom",
) -> dict:
    """Create a new symbol from *datasheet_url* and write it to disk.

    Args:
        lib_id:       'LibNick:PartName'  (e.g. 'Custom:TMC2209')
        datasheet_url: direct URL to the component datasheet PDF.
        lib_nick:     Which symbol library nick to save under (default 'Custom').

    Returns:
        {
          "ok": True,
          "lib_id": "Custom:TMC2209",
          "path": "<path to .kicad_sym>",
          "info": <full extracted dict with pins + package>,
        }
        or {"ok": False, "error": "<message>"}
    """
    nick, _, part = lib_id.partition(":")
    if not part:
        return {"ok": False, "error": f"lib_id must be Nick:Part, got {lib_id!r}"}

    # Fetch datasheet + extract everything in one Claude call
    info = _read_datasheet_full(part, datasheet_url)
    if not info:
        return {"ok": False,
                "error": f"Could not extract component info from: {datasheet_url}"}

    # Generate .kicad_sym content
    content = _gen_sym_content(part, info)

    # Find the write-target directory (first existing sym root that contains nick.kicad_symdir,
    # or the Envil CAD one which we know is writable)
    target_dir: Optional[Path] = None
    for root in _sym_roots():
        d = root / f"{lib_nick}.kicad_symdir"
        if d.exists():
            target_dir = d
            break

    if target_dir is None:
        # Create under the first writable sym root
        for root in _sym_roots():
            try:
                d = root / f"{lib_nick}.kicad_symdir"
                d.mkdir(parents=True, exist_ok=True)
                target_dir = d
                break
            except PermissionError:
                continue

    if target_dir is None:
        return {"ok": False, "error": "No writable symbol root found."}

    out_path = target_dir / f"{part}.kicad_sym"
    out_path.write_text(content, encoding="utf-8")

    # Register the library in sym-lib-table if needed
    uri = str(target_dir).replace("\\", "/")
    try:
        _register_sym_lib(lib_nick, uri)
    except Exception as exc:
        print(f"[create_symbol] sym-lib-table update skipped: {exc}")

    return {
        "ok": True,
        "lib_id": f"{lib_nick}:{part}",
        "path": str(out_path),
        "info": info,
    }
