"""Offline verification of the schematic_quality reviewer + its wiring into
the agent's review routing.

Runs the real tool against a real .kicad_sch in the repo (ne555_blinker).
ERC needs kicad-cli — if it's missing the Electrical dimension degrades to
n/a and the Wiring dimension (pure-Python lint) still scores, which the test
asserts. Run:

    python tests/_verify_schematic_quality.py     # exit 0 = all pass
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.tools import SCHEMATIC_TOOLS, schematic_quality          # noqa: E402
from envil_agent.tools.schematic_quality import _cfg, _compose_card, _band_for  # noqa: E402

_FAILS: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    mark = "ok  " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _FAILS.append(name)


def _live_sch() -> Path:
    for cand in ("ne555_blinker/ne555_blinker.kicad_sch",
                 "ne555_led_blinker/ne555_led_blinker.kicad_sch",
                 "lm7805_regulator/lm7805_regulator.kicad_sch"):
        p = Path(__file__).resolve().parents[1] / cand
        if p.exists():
            return p
    raise FileNotFoundError("no sample .kicad_sch found in repo")


async def main() -> None:
    print("schematic_quality verification\n")

    # 1. registered on the schematic page
    _check("registered in SCHEMATIC_TOOLS", schematic_quality in SCHEMATIC_TOOLS)

    # 2. config loads + enabled + has weights/bands
    cfg = _cfg()
    _check("config loads", isinstance(cfg, dict) and bool(cfg))
    _check("config enabled", cfg.get("enabled") is True)
    _check("config has weights", "Electrical" in (cfg.get("weights") or {}))
    _check("config has bands", isinstance(cfg.get("bands"), list))

    # 3. banding is monotone
    bands = [(float(t), str(l)) for t, l in cfg.get("bands")]
    _check("band 95 -> EXCELLENT", _band_for(95, bands) == "EXCELLENT")
    _check("band 0 -> NEEDS WORK", _band_for(0, bands) == "NEEDS WORK")

    # 4. pure card composition (no I/O) — renormalises over ran dims
    card, overall = _compose_card("x.kicad_sch",
                                  {"Electrical": 100.0, "Wiring": 70.0}, [], cfg)
    _check("card overall in range", 0 <= overall <= 100, str(overall))
    _check("n/a dimension dropped from avg",
           _compose_card("x", {"Electrical": None, "Wiring": 80.0}, [], cfg)[1] == 80.0)

    # 5. live run against a real schematic
    sch = _live_sch()
    r = await schematic_quality.handler({"sch_path": str(sch)})
    _check("live run ok", r.get("ok") is True and not r.get("is_error"),
           json.dumps(r.get("content"))[:200])
    dims = r.get("dimensions") or {}
    _check("has Electrical + Wiring dims",
           "Electrical" in dims and "Wiring" in dims)
    _check("at least one dimension ran",
           dims.get("Electrical") is not None or dims.get("Wiring") is not None)
    _check("overall is a number", isinstance(r.get("overall"), (int, float)))
    _check("fixes is a list", isinstance(r.get("fixes"), list))
    _check("card text present", bool(r.get("content", [{}])[0].get("text")))

    # 6. negative paths
    bad = await schematic_quality.handler({"sch_path": "C:/nope/missing.kicad_sch"})
    _check("missing file -> is_error", bad.get("is_error") is True)
    wrong = await schematic_quality.handler({"sch_path": str(sch).replace(".kicad_sch", ".kicad_pcb")})
    _check("wrong suffix -> is_error", wrong.get("is_error") is True)

    print()
    if _FAILS:
        print(f"FAILED ({len(_FAILS)}): {', '.join(_FAILS)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
