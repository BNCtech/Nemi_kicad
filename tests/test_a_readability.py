"""Phase A readability spread pass --- geometric checks (no renderer needed).

Run directly:  python tests/test_a_readability.py

Verifies the pass spreads crowded columns so their field-bboxes (+ net-label
reserve) no longer overlap, only ever increases spacing, and is a strict no-op
when disabled or when there are too few columns.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import envil_agent.intent.engine as eng  # noqa: E402
from envil_agent.intent.engine import (  # noqa: E402
    PlacedComp, _abs_bbox_with_fields, _readability_spread_columns, _snap_grid,
)
from envil_agent.kicad.symbol_geom import load_symbol  # noqa: E402

_fail = 0


def check(name, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


def make_column(ref_r, ref_d, x, y):
    """A vertical R (top) + LED (below) chain at column x."""
    out = []
    for ref, lib, dy in ((ref_r, "Device:R", 0.0), (ref_d, "Device:LED", 10.0)):
        g = load_symbol(lib)
        out.append(PlacedComp(ref=ref, lib_id=lib, value="330R",
                              footprint="", pos=_snap_grid((x, y + dy)),
                              rotation=0.0, geom=g))
    return out


def x_overlap(a, b, reserve):
    ax1, _, ax2, _ = _abs_bbox_with_fields(a)
    bx1, _, bx2, _ = _abs_bbox_with_fields(b)
    return min(ax2 + reserve, bx2 + reserve) - max(ax1 - reserve, bx1 - reserve)


# Build 4 columns crammed 6mm apart (tighter than label width -> overlap).
def build():
    placed = []
    for i in range(4):
        placed += make_column(f"R{i}", f"D{i}", 100.0 + i * 6.0, 100.0)
    return placed


# --- Disabled -> exact no-op ---------------------------------------------
eng._LAYOUT_CONFIG_CACHE = None
cfg = eng._load_layout_config()
cfg["readability"] = {"enabled": False}
before = build()
before_pos = [p.pos for p in before]
after = _readability_spread_columns(before)
check("disabled pass is a strict no-op",
      [p.pos for p in after] == before_pos)

# --- Enabled -> columns spread, no field-bbox overlap --------------------
cfg["readability"] = {"enabled": True, "min_columns": 3, "column_gap_mm": 5.08,
                      "net_label_reserve_mm": 8.0, "column_quantize_mm": 2.54,
                      "align_baseline": False}
placed = build()
orig_x = [p.pos[0] for p in placed]
_readability_spread_columns(placed)
new_x = [p.pos[0] for p in placed]

check("no column moved LEFT (spacing only increases)",
      all(n >= o - 0.01 for n, o in zip(new_x, orig_x)))
check("leftmost column is unchanged (anchor)",
      abs(new_x[0] - orig_x[0]) < 0.01)

# Representative component per column (the resistors R0..R3, indices 0,2,4,6).
resistors = [placed[i] for i in (0, 2, 4, 6)]
worst = max(x_overlap(resistors[i], resistors[i + 1], 8.0)
            for i in range(len(resistors) - 1))
check("adjacent columns no longer overlap in X (incl. reserve)", worst <= 0.01)

# --- Too few columns -> no-op --------------------------------------------
placed2 = make_column("R9", "D9", 100.0, 100.0) + make_column("R8", "D8", 106.0, 100.0)
p2_before = [p.pos for p in placed2]
_readability_spread_columns(placed2)
check("below min_columns is a no-op", [p.pos for p in placed2] == p2_before)

eng._LAYOUT_CONFIG_CACHE = None  # restore real config for other callers
print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
sys.exit(1 if _fail else 0)
