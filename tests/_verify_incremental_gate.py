"""Verify the generation/render DECOUPLE fix (2026-06-09).

Problem 4: the proactive incremental 'draw bit by bit' path was hard-gated to
render_mode=='hierarchy', so a large board the keyword guess mis-tagged
SINGLE_SHEET_BLOCKS went one-shot (LLM drops nets). Fix: any render mode in
build_graph.incremental_trigger_render_modes attempts the PLAN call, and the
PLAN's real component count is the authoritative large-board gate.

These cases stub every LLM call (no live API) and assert which path architect_node
takes via the returned dict (incremental returns 'incremental_drawn').

Run:  python tests/_verify_incremental_gate.py
"""
import sys, json
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

import importlib
from envil_agent.intent.ir import TopologyIR, IRComponent, IRNet, IRBlock
# IMPORTANT: import the REAL module, NOT `from envil_agent.tools import
# build_circuit` -- that binds to the @tool SdkMcpTool that shadows the
# submodule (the exact bug that made incremental silently never run). Patching
# the SdkMcpTool would mask the bug (it just adds the missing attr), so we patch
# the same real module _try_incremental / block_repair_node resolve via importlib.
bc = importlib.import_module("envil_agent.tools.build_circuit")
from envil_agent.graphs.nodes import architect as arch

R = []
def rec(n, ok, d=""):
    R.append(ok); print(("PASS" if ok else "FAIL"), "-", n, ("-> " + d) if d else "")


# --- build skeleton plans of arbitrary size ---------------------------------
def skeleton(n_parts, n_blocks):
    comps = [IRComponent("U1", "MCU_ST_STM32F4:STM32F405RGTx", "STM32F405")]
    comps += [IRComponent(f"R{i}", "Device:R", "10k")
              for i in range(1, n_parts)]
    # spread parts across n_blocks
    blocks, nets = [], [IRNet("GND", ["U1.VSS"], is_power=True),
                        IRNet("+3V3", ["U1.VDD"], is_power=True)]
    per = max(1, (len(comps)) // n_blocks)
    for b in range(n_blocks):
        refs = [c.ref for c in comps[b * per:(b + 1) * per]] or [comps[0].ref]
        blocks.append(IRBlock(["POWER", "MCU", "COMM", "SENSE", "PROT"][b % 5]
                              + (str(b) if b > 4 else ""), "grp", refs))
    return TopologyIR("brd", "brd", comps, nets, blocks)


def install_stubs(plan_ir, *, track):
    """Patch the three LLM entry points; record which got called."""
    def fake_plan(prompt, render_mode="hierarchy"):
        track["plan"] = True
        return json.dumps(plan_ir.to_dict())
    def fake_block(bname, btype, comps, errs, boundary, prompt):
        track["block"] = True
        out = [IRComponent(r, lib, v) for (r, lib, v) in comps]
        nets = [IRNet("GND", [f"{r}.1" for (r, _l, _v) in comps], is_power=True)]
        return out, nets
    def fake_oneshot(prompt, feedback="", prior_attempt_json="", render_mode=""):
        track["oneshot"] = True
        return json.dumps(plan_ir.to_dict())
    bc._architect_plan_call = fake_plan
    bc._architect_block_call = fake_block
    arch._architect_call = fake_oneshot       # imported into arch's namespace


# ----------------------------------------------------------------------------
# Case 0 (REGRESSION GUARD for the import-shadow bug): the production code must
# resolve the REAL module (with _architect_plan_call), not the @tool SdkMcpTool
# that the tools package re-export shadows it with. If this regresses, _bc.
# attribute lookups raise AttributeError, are silently caught, and incremental
# NEVER runs in the live app (offline tests that patch the SdkMcpTool mask it).
# ----------------------------------------------------------------------------
from envil_agent.tools import build_circuit as _shadowed   # the SdkMcpTool
rec("import-shadow guard: real module has the architect calls, SdkMcpTool does not",
    hasattr(bc, "_architect_plan_call") and hasattr(bc, "_architect_block_call")
    and not hasattr(_shadowed, "_architect_plan_call"),
    f"real_module={type(bc).__name__} shadowed={type(_shadowed).__name__}")

# ----------------------------------------------------------------------------
# Case 1 (THE FIX): a 44-part board the keyword guess tagged SINGLE_SHEET_BLOCKS
# now ATTEMPTS incremental and goes down it (was stuck one-shot before).
# ----------------------------------------------------------------------------
track = {}
install_stubs(skeleton(44, 3), track=track)
out = arch.architect_node({"prompt": "big board", "attempt": 0,
                           "force_single_sheet": True})   # -> render_mode single_sheet_blocks
rec("44-part SINGLE_SHEET_BLOCKS board -> goes INCREMENTAL (the fix)",
    "incremental_drawn" in out and not track.get("oneshot"),
    f"drawn={out.get('incremental_drawn')} oneshot={track.get('oneshot', False)}")

# ----------------------------------------------------------------------------
# Case 2: hierarchy still works (regression guard).
# ----------------------------------------------------------------------------
track = {}
install_stubs(skeleton(60, 5), track=track)
out = arch.architect_node({"prompt": "huge board", "attempt": 0,
                           "force_hierarchy": True})
rec("60-part HIERARCHY board -> still goes INCREMENTAL",
    "incremental_drawn" in out and not track.get("oneshot"))

# ----------------------------------------------------------------------------
# Case 3 (no cost regression): a FLAT guess (render_mode=='') does NOT even
# attempt the PLAN call -- small/simple circuits skip the round-trip.
# ----------------------------------------------------------------------------
track = {}
install_stubs(skeleton(44, 3), track=track)
out = arch.architect_node({"prompt": "NE555 blinker", "attempt": 0})  # no force flag
rec("FLAT guess -> PLAN call NOT attempted (one-shot, no cost regression)",
    track.get("plan") is None and track.get("oneshot") is True,
    f"plan_called={bool(track.get('plan'))} oneshot={bool(track.get('oneshot'))}")

# ----------------------------------------------------------------------------
# Case 4 (part-count gate still authoritative): a SMALL block board (10 parts)
# attempts the PLAN call but falls BACK to one-shot (< 40 parts).
# ----------------------------------------------------------------------------
track = {}
install_stubs(skeleton(10, 3), track=track)
out = arch.architect_node({"prompt": "medium board", "attempt": 0,
                           "force_single_sheet": True})
rec("10-part block board -> PLAN attempted but FALLS BACK to one-shot",
    track.get("plan") is True and track.get("oneshot") is True
    and "incremental_drawn" not in out,
    f"plan={bool(track.get('plan'))} oneshot={bool(track.get('oneshot'))}")

# ----------------------------------------------------------------------------
# Case 5: on a RETRY (feedback present) incremental is never taken -- the
# reactive per-block repair owns retries.
# ----------------------------------------------------------------------------
track = {}
install_stubs(skeleton(44, 3), track=track)
out = arch.architect_node({"prompt": "big board", "attempt": 1,
                           "feedback": "fix X", "force_single_sheet": True})
rec("retry (feedback set) -> incremental NOT taken, one-shot regen",
    track.get("plan") is None and track.get("oneshot") is True)

print("\nSUMMARY:", sum(R), "/", len(R))
sys.exit(0 if all(R) else 1)
