"""LIVE self-test of ONE-AI mode through the real agent path.

Calls agent.run_turn() directly (the exact function the chat WebSocket
handler calls) with a real LLM turn, so it exercises the NEW code
(_active_tools_for -> ALL_TOOLS + PAGE_SCOPE_UNIFIED) WITHOUT starting a
server or touching the running instance's IPC port files.

Proves: a PCB-panel chat does NOT redirect a schematic request, and a
schematic-panel chat does NOT redirect a PCB request — i.e. one AI.

Run:  python tests/_live_unified_chat.py     # exit 0 = all pass
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

AIB = Path(__file__).resolve().parents[1]  # ai_backend
sys.path.insert(0, str(AIB))

try:
    from dotenv import load_dotenv
    load_dotenv(AIB / ".env")
except Exception as e:  # noqa: BLE001
    print("dotenv load skipped:", e)
try:
    import truststore
    truststore.inject_into_ssl()
except Exception as e:  # noqa: BLE001
    print("truststore skipped:", e)

print("ANTHROPIC_API_KEY present:", bool(os.environ.get("ANTHROPIC_API_KEY")))

from envil_agent.agent import run_turn  # noqa: E402


async def one_turn(app: str, prompt: str, timeout: float = 180.0):
    texts, tools = [], []

    async def _drain():
        async for ev in run_turn(prompt, app=app):
            if ev.kind == "text" and ev.text:
                texts.append(ev.text)
            elif ev.kind == "tool_use" and ev.tool_name:
                tools.append(str(ev.tool_name).split("__")[-1])

    try:
        await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        texts.append("<TIMEOUT>")
    return " ".join(texts), tools


async def main() -> int:
    results = []

    # 1) PCB panel asked a SCHEMATIC task -> one AI must NOT redirect.
    reply1, tools1 = await one_turn(
        "pcb",
        "I'm in the PCB editor. Can you add a 10k pull-up resistor on the "
        "RESET net in the SCHEMATIC for me, or do I have to switch to the "
        "schematic editor? Answer briefly.")
    print(f"\n--- TURN 1 (app=pcb -> schematic request)  tools={tools1} ---\n{reply1}\n")
    r1 = reply1.lower()
    redirect1 = ("switch to the schematic editor" in r1) or ("schematic-editor task" in r1)
    results.append(("PCB panel does NOT redirect a schematic request", not redirect1))

    # 2) Schematic panel asked a PCB task -> one AI must NOT redirect.
    reply2, tools2 = await one_turn(
        "schematic",
        "I'm in the schematic editor. Can you autoroute / lay out the PCB "
        "for me, or do I have to switch to the PCB editor? Answer briefly.")
    print(f"\n--- TURN 2 (app=schematic -> PCB request)  tools={tools2} ---\n{reply2}\n")
    r2 = reply2.lower()
    redirect2 = ("switch to the pcb editor" in r2) or ("pcb-editor task" in r2)
    results.append(("Schematic panel does NOT redirect a PCB request", not redirect2))

    ok = True
    for label, passed in results:
        ok = ok and passed
        print(("PASS" if passed else "FAIL"), label)
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
