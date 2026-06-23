"""Full assembly test: plan (with contract completion) -> draw every block ->
reassert seams -> normalize -> validate. Reports drawn/skipped + the validation
errors by code, with the PIN_IN_MULTIPLE_NETS / NET_FLOATING specifics. This is
the definitive 'does the assembled board pass' test."""
import truststore
truststore.inject_into_ssl()
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=True)
import os, json
os.environ.setdefault("ANTHROPIC_API_KEY", os.environ.get("CLAUDE_API_KEY", ""))

prompt = (
    "48V 13S Li-ion EV Battery Management System with STM32G474VET6 MCU, "
    "BQ76952 monitoring all 13 cells with passive balancing resistors, INA240 "
    "current sense across a 0.5mOhm shunt, IRFB4110 charge/discharge MOSFETs "
    "with pre-charge contactor control, TJA1051 CAN transceiver, LM5164 buck "
    "(48V to 5V), AMS1117-3.3V regulator, input fuse with TVS and reverse-"
    "polarity protection diode, 10K NTC temperature sensors (2x), buzzer, status "
    "LEDs, and all decoupling/test points fully wired."
)

import importlib
BC = importlib.import_module("envil_agent.tools.build_circuit")
from envil_agent.intent.ir import TopologyIR
from envil_agent.intent.incremental_build import build_incrementally
from envil_agent.intent.normalize import normalize_ir
from envil_agent.intent.validate import validate_ir, dedupe_issues

print(">>> PLAN…", flush=True)
raw = BC._architect_plan_call(prompt, "hierarchy")
plan_ir = TopologyIR.from_json(BC._extract_ir_json(raw))
print(f"plan: comps={len(plan_ir.components)} blocks={len(plan_ir.blocks)} nets={len(plan_ir.nets)}",
      flush=True)

print(">>> DRAW BLOCKS (incremental)…", flush=True)
ir, drawn, skipped = build_incrementally(plan_ir, prompt, BC._architect_block_call)
print(f"drawn={drawn}", flush=True)
print(f"skipped={skipped}", flush=True)

print(">>> NORMALIZE + VALIDATE…", flush=True)
normalize_ir(ir)
issues = dedupe_issues(validate_ir(ir, prompt=prompt))
errs = [i for i in issues if i.get("severity") == "error"]

from collections import Counter
by_code = Counter(i["code"] for i in errs)
print(f"\n=== VALIDATION: {len(errs)} errors ===")
for code, n in by_code.most_common():
    print(f"  {code}: {n}")

print("\n=== error detail (first 25) ===")
for i in errs[:25]:
    print(f"  [{i['code']}] @ {i['where']}: {i['text'][:120]}")

# specifically: any NET_FLOATING / PIN_IN_MULTIPLE_NETS left?
floating = [i for i in errs if i["code"] == "NET_FLOATING"]
multinet = [i for i in errs if i["code"] == "PIN_IN_MULTIPLE_NETS"]
print(f"\nNET_FLOATING left: {len(floating)}  | PIN_IN_MULTIPLE_NETS left: {len(multinet)}")
print("VERDICT:", "ASSEMBLED BOARD CLEAN" if not errs else f"{len(errs)} errors remain")
