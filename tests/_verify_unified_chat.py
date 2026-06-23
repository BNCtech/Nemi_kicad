"""Verify ONE-AI mode (unified_chat): a single assistant exposes ALL tools
(schematic + PCB) and a unified, no-redirect system prompt regardless of
which editor opened the panel.

Run:
    python tests/_verify_unified_chat.py     # exit 0 = all pass

Proves the "now only one ai" change:
- the PCB panel ALSO gets schematic tools (e.g. build_circuit) and vice-versa,
- the prompt is the unified one (no "switch to the other editor" redirect),
- the legacy page-scope still exists behind the flag (control).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent import agent                       # noqa: E402
from envil_agent.tools import tools_for_app          # noqa: E402


def main() -> int:
    results = []

    def check(label, cond):
        results.append((label, bool(cond)))

    check("unified_chat enabled (config default on)", agent._unified_chat_enabled())

    pcb_tools = {t.name for t in agent._active_tools_for("pcb")}
    sch_tools = {t.name for t in agent._active_tools_for("schematic")}

    # One AI: every panel can do everything.
    check("PCB panel HAS build_circuit (a schematic tool)", "build_circuit" in pcb_tools)
    check("PCB panel HAS route_pcb_simple (a PCB tool)", "route_pcb_simple" in pcb_tools)
    check("Schematic panel HAS route_pcb_simple (a PCB tool)", "route_pcb_simple" in sch_tools)
    check("PCB tool set == Schematic tool set (one AI, same tools)", pcb_tools == sch_tools)

    # Control: the legacy page-scope WOULD have excluded build_circuit from PCB
    # — proves the unification actually changed behaviour.
    legacy_pcb = {t.name for t in tools_for_app("pcb")}
    check("legacy page-scope excluded build_circuit from PCB (proves change)",
          "build_circuit" not in legacy_pcb)

    # Prompt: unified, no "switch editors" redirect.
    p_pcb = agent._system_prompt_for_app("pcb")
    p_sch = agent._system_prompt_for_app("schematic")
    check("unified prompt used on both panels ('Unified mode')",
          "Unified mode" in p_pcb and "Unified mode" in p_sch)
    check("no 'switch to the Schematic Editor' redirect", "switch to the Schematic Editor" not in p_pcb)
    check("no 'switch to the PCB Editor' redirect", "switch to the PCB Editor" not in p_sch)

    ok = True
    for label, passed in results:
        ok = ok and passed
        print(("PASS" if passed else "FAIL"), label)
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
