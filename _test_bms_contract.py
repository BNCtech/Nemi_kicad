"""Test-first: run the BMS PLAN through the CURRENT code (fresh process = all
fixes loaded incl. _complete_plan_contract) and report whether the cross-block
contract is now both-ended. Highest-signal, cheapest check for root cause #1."""
import truststore
truststore.inject_into_ssl()
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)
import os
os.environ.setdefault("ANTHROPIC_API_KEY", os.environ.get("CLAUDE_API_KEY", ""))

# 1. The BMS prompt (faithful to the user's build — the complex board that
# triggered the floating control-net failure).
prompt = (
    "48V 13S Li-ion EV Battery Management System with STM32G474VET6 MCU, "
    "BQ76952 monitoring all 13 cells with passive balancing resistors, INA240 "
    "current sense across a 0.5mOhm shunt, IRFB4110 charge/discharge MOSFETs "
    "with pre-charge contactor control, TJA1051 CAN transceiver, LM5164 buck "
    "(48V to 5V), AMS1117-3.3V regulator, input fuse with TVS and reverse-"
    "polarity protection diode, 10K NTC temperature sensors (2x), buzzer, status "
    "LEDs, and all decoupling/test points fully wired."
)
print("PROMPT (first 280):", prompt[:280], "\n")

# 2. run the plan call WITH the contract-completion fix (fresh code)
import importlib
BC = importlib.import_module("envil_agent.tools.build_circuit")
from envil_agent.intent.ir import TopologyIR

print(">>> running _architect_plan_call (plan + contract completion)…", flush=True)
raw = BC._architect_plan_call(prompt, "hierarchy")
if not raw:
    raise SystemExit("plan call returned empty")
ir = TopologyIR.from_json(BC._extract_ir_json(raw))
print(f"\ncomponents={len(ir.components)}  blocks={len(ir.blocks)}  nets={len(ir.nets)}")

# 3. THE check: any one-ended (floating) inter-block signal net left?
one_ended = BC._incomplete_signal_nets(ir)
print(f"\n=== ONE-ENDED signal nets (should be EMPTY): {len(one_ended)} ===")
for nm, pins in one_ended:
    print(f"  !! {nm}: {pins}")

print("\n=== ALL inter-block signal nets (non-power) ===")
for n in ir.nets:
    if not getattr(n, "is_power", False):
        flag = "  <-- ONE-ENDED" if len(n.pins) < 2 else ""
        print(f"  {n.name}: {len(n.pins)} pins {n.pins}{flag}")

print("\n=== power rails ===")
for n in ir.nets:
    if getattr(n, "is_power", False):
        print(f"  {n.name}: {len(n.pins)} pins")

print("\nVERDICT:", "CONTRACT COMPLETE (fix #1 works)" if not one_ended
      else f"STILL {len(one_ended)} ONE-ENDED — contract fix did NOT fully close")
