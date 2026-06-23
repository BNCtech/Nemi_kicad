"""LIVE end-to-end test of the large-board incremental fix (uses the real API).

Builds a COMPACT-prompt board that the keyword guess tags SINGLE_SHEET_BLOCKS but
that is actually ~45-55 parts -- the exact case the fix targets. Invokes the real
router_graph_detailed (same path the app uses), so the architect's INCREMENTAL
line prints if the fix engaged.

Costs API tokens. Needs ai_backend/.env with CLAUDE_API_KEY (or ANTHROPIC_API_KEY).

Run:  python tests/_live_large_board.py
Watch stdout for:  [architect] INCREMENTAL large-board path: planned N parts ...
                   ^ that line = the fix fired. Absent = it went one-shot.
"""
import sys, asyncio
from pathlib import Path
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

# Load .env the same way python -m envil_agent does (CLAUDE_API_KEY -> ANTHROPIC).
try:
    import os
    from dotenv import load_dotenv
    _env = Path("f:/Ki_CAD/ai_backend/.env")
    if _env.exists():
        load_dotenv(_env, override=True)
    if os.environ.get("CLAUDE_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]
except ImportError:
    pass

from envil_agent.graphs.router_graph import router_graph_detailed

# The real CAN-logger board (force_hierarchy=True). A hierarchy board has ALWAYS
# been incremental-eligible -- this run proves incremental fires in a FRESH
# process (the live app failed to fire it only because its server process was
# stale). Watch for the "[architect] INCREMENTAL" line + a "Plan blocks" span.
PROMPT = ("Generate a CAN data logger: STM32F405RGT6 MCU, SN65HVD230 CAN "
          "transceiver, microSD card slot, dual power regulators (5V and 3.3V), "
          "three status LEDs and protection circuits. Multi-sheet hierarchy with "
          "blocks for power, MCU, CAN, SD card and protection.")


async def main():
    state = {"prompt": PROMPT, "out_dir": "F:/Ki_CAD/_envil_out/hier_inc_test",
             "out_path": "",
             "force_hierarchy": True,      # match the real CAN-logger build (hierarchy)
             "attempt": 0, "feedback": "", "decision_log": []}
    print(f"Building: {PROMPT}\n" + "-" * 70)
    final = await router_graph_detailed().ainvoke(state)

    stats = final.get("stats") or {}
    issues = final.get("best_issues") or final.get("issues") or []
    errs = [i for i in issues if i.get("severity") == "error"]
    print("-" * 70)
    print("layout_final     :", final.get("layout_final"))
    print("components_total :", stats.get("components_total", stats.get("components_emitted", "?")))
    print("nets_total       :", stats.get("nets_total", "?"))
    print("hierarchical     :", "blocks" in stats)
    print("path             :", stats.get("path", "(none - build failed)"))
    print("error count      :", len(errs))
    under = [i for i in errs if i.get("code") == "BOARD_UNDER_WIRED"]
    print("BOARD_UNDER_WIRED:", "YES (still under-wired!)" if under else "no")
    if errs:
        print("first errors     :")
        for i in errs[:6]:
            print(f"   [{i['code']}] @ {i['where']}: {i['text'][:70]}")
    print("\nDecision trace:")
    for ln in (final.get("decision_log") or [])[-14:]:
        print("  ", ln)
    print("\n==> If you saw a '[architect] INCREMENTAL large-board path:' line "
          "above, the fix fired.\n    If not, it went one-shot (check the part "
          "count cleared the >=40 gate).")


if __name__ == "__main__":
    asyncio.run(main())
