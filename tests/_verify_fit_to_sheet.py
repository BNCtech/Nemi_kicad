"""_verify_fit_to_sheet.py — proof for the fit-to-sheet layout repair.

Enforces the HARD 'design never crosses the sheet border / title block' rule:
content that sits off the page (or over the title block) is uniformly
translated back on-sheet (net list byte-identical), and the paper is upsized
when the design genuinely does not fit.

Hermetic — a hand-built minimal .kicad_sch + an injected context (the `ctx=`
param), so no symbol library is needed. The critical invariant checked here:
coordinates inside (lib_symbols ...) are symbol-RELATIVE and must NOT move,
while every absolute instance/wire/rectangle/text coordinate must shift by the
SAME (dx, dy).

Run from ai_backend:
    PYTHONPATH=. python tests/_verify_fit_to_sheet.py
"""
from __future__ import annotations
import sys, re, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from envil_agent.lint.repair import fit_design_to_sheet   # noqa: E402

_fails = []
def check(c, m):
    print(("  PASS " if c else "  FAIL ") + m)
    if not c: _fails.append(m)

# A minimal sheet: a lib_symbols rect (must stay put) + a top-level block
# rectangle, a wire and a title text that all sit partly ABOVE the page top.
SCH = """(kicad_sch
	(paper "A4")
	(lib_symbols
		(symbol "Device:R"
			(symbol "R_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54)))
		)
	)
	(wire (pts (xy 50 -5) (xy 80 -5)) (stroke (width 0)) (uuid "w1"))
	(rectangle (start 40 -8) (end 90 20) (stroke (width 0.1)) (fill (type none)))
	(text "1. BLK" (at 45 -8 0) (effects (font (size 1.27 1.27))))
)
"""

def _read(p): return p.read_text(encoding="utf-8")

def test_translate_onto_sheet():
    print("[1] off-page design is translated back on-sheet")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "b.kicad_sch"; p.write_text(SCH, encoding="utf-8")
        # ctx bbox from the top-level content: x[40,90] y[-8,20] (off top).
        ctx = {"wires": [((50.0,-5.0),(80.0,-5.0))],
               "bboxes": [], "text_bboxes": [],
               "blocks": [("BLK",40.0,-8.0,90.0,20.0)],
               "labels": [], "power_ports": [], "junctions": []}
        res = fit_design_to_sheet(p, ctx=ctx)
        t = _read(p)
        check(res["translated"] and res["was_outside"], "reports a translate")
        # dx = 15-40 = -25 ; dy = 15-(-8) = 23
        check("(start 15 15)" in t and "(end 65 43)" in t,
              "block rectangle shifted uniformly (start/end)")
        check("(xy 25 18)" in t and "(xy 55 18)" in t, "wire xy shifted uniformly")
        check("(at 20 15 0)" in t, "title text (at) shifted uniformly")
        check("(start -1.016 -2.54)" in t and "(end 1.016 2.54)" in t,
              "lib_symbols rect UNCHANGED (symbol-relative coords not moved)")
        # idempotent
        r2 = fit_design_to_sheet(p, ctx={"wires":[((25.0,18.0),(55.0,18.0))],
             "bboxes":[],"text_bboxes":[],"blocks":[("BLK",15.0,15.0,65.0,43.0)],
             "labels":[],"power_ports":[],"junctions":[]})
        check(not r2["translated"], "idempotent: fitted design is a no-op")

def test_paper_upsize():
    print("[2] design too big for the page -> paper upsized")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "b.kicad_sch"; p.write_text(SCH, encoding="utf-8")
        # ~360 mm wide content cannot fit A4 (drawable ~267) -> expect A3+.
        ctx = {"wires": [((10.0,10.0),(370.0,10.0))],
               "bboxes": [], "text_bboxes": [],
               "blocks": [("BLK",10.0,10.0,370.0,120.0)],
               "labels": [], "power_ports": [], "junctions": []}
        res = fit_design_to_sheet(p, ctx=ctx)
        check(res["paper_from"] == "A4" and res["paper_to"] != "A4",
              f"paper upsized A4 -> {res['paper_to']}")
        check(f'(paper "{res["paper_to"]}")' in _read(p), "paper token rewritten")

def test_already_fits_byte_stable():
    print("[3] design already on-sheet -> byte-identical no-op")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "b.kicad_sch"
        good = SCH.replace("(xy 50 -5) (xy 80 -5)", "(xy 50 50) (xy 80 50)") \
                  .replace("(start 40 -8) (end 90 20)", "(start 40 40) (end 90 80)") \
                  .replace("(at 45 -8 0)", "(at 45 40 0)")
        p.write_text(good, encoding="utf-8")
        before = p.read_bytes()
        ctx = {"wires": [((50.0,50.0),(80.0,50.0))],
               "bboxes": [], "text_bboxes": [],
               "blocks": [("BLK",40.0,40.0,90.0,80.0)],
               "labels": [], "power_ports": [], "junctions": []}
        res = fit_design_to_sheet(p, ctx=ctx)
        check(not res["translated"], "already-fitting design: no translate")
        check(p.read_bytes() == before, "file byte-identical")

if __name__ == "__main__":
    test_translate_onto_sheet(); test_paper_upsize(); test_already_fits_byte_stable()
    print()
    print("RESULT:", "PASS (all checks)" if not _fails else f"FAIL ({len(_fails)})")
    sys.exit(1 if _fails else 0)
