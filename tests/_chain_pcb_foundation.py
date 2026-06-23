"""Apply the standard-rules foundation chain (outline -> design rules -> GND
pour) to a COPY of a generated board and report. Run from ai_backend/:
    python tests/_chain_pcb_foundation.py
"""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from envil_agent.tools.auto_outline_pcb import auto_outline_pcb     # noqa: E402
from envil_agent.tools.set_design_rules import set_design_rules     # noqa: E402
from envil_agent.tools.auto_zones_pcb import auto_zones_pcb         # noqa: E402

SRC = Path("F:/Ki_CAD/_envil_out/ne555_blinker/ne555_blinker.kicad_pcb")
DST_DIR = Path("F:/Ki_CAD/_envil_out/_pcbfoundation_test")


def _text(r):
    try:
        return r.get("content", [{}])[0].get("text", "")
    except Exception:
        return str(r)


async def main() -> int:
    DST_DIR.mkdir(parents=True, exist_ok=True)
    pcb = DST_DIR / SRC.name
    shutil.copy2(SRC, pcb)
    for ext in (".kicad_pro", ".kicad_sch"):
        s = SRC.with_suffix(ext)
        if s.exists():
            shutil.copy2(s, pcb.with_suffix(ext))

    steps = [
        ("outline", auto_outline_pcb, {"pcb_path": str(pcb)}),
        ("design_rules", set_design_rules, {"pcb_path": str(pcb)}),
        ("gnd_pour", auto_zones_pcb, {"pcb_path": str(pcb), "net": "GND"}),
    ]
    for name, tool, args in steps:
        r = await tool.handler(args)
        print(f"--- {name}: is_error={r.get('is_error')} ---")
        print(_text(r)[:300])
        print()

    print("BOARD:", pcb)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
