"""Verify the P0 Intake Brain (Stages 1-3): intent/risk triage,
completeness scoring, and the 3-way decision + safety override.

All offline -- no LLM, no KiCad. Exercises the exact pipeline the
assess_request tool runs (triage -> score_completeness -> _decide) plus
the tool registration and the flag-off byte-stability of the schematic
system prompt.

Run:  python tests/_verify_intake.py
"""
import sys
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

from envil_agent.intent.triage import triage, score_completeness, _load_rules
from envil_agent.tools.assess_request import _decide

R = []
def rec(name, ok, detail=""):
    R.append(ok)
    print(("PASS" if ok else "FAIL"), "-", name, ("-> " + detail) if detail else "")


def decide(prompt):
    """Run the full intake pipeline exactly as the tool does."""
    t = triage(prompt)
    sc = score_completeness(prompt, t)
    d, reason = _decide(t, sc, _load_rules())
    return t, sc, d, reason


# --- Stage 1: intent / risk triage -----------------------------------------
t = triage("Design Inverter")
rec("inverter -> critical risk", t["risk_level"] == "critical"
    and t["safety_critical"] and t["complexity"] == "Critical",
    f"risk={t['risk_level']} cx={t['complexity']}")

t = triage("Design a 13S EV BMS")
rec("BMS -> high risk + safety", t["project_type"] == "bms"
    and t["risk_level"] == "high" and t["safety_critical"],
    f"type={t['project_type']} risk={t['risk_level']}")

t = triage("Design STM32 LED circuit")
rec("STM32 LED -> mcu_board Low low-risk", t["project_type"] == "mcu_board"
    and t["complexity"] == "Low" and not t["safety_critical"],
    f"type={t['project_type']} cx={t['complexity']}")

t = triage("Design a CAN Data Logger")
rec("CAN logger -> medium, not safety", t["project_type"] == "can_logger"
    and not t["safety_critical"] and t["complexity"] in ("Medium", "High"),
    f"type={t['project_type']} cx={t['complexity']}")

t = triage("Design a BLDC motor driver")
rec("motor driver -> high risk + power", t["project_type"] == "motor_driver"
    and t["risk_level"] == "high" and t["power_electronics"],
    f"type={t['project_type']} risk={t['risk_level']}")


# --- Stage 2: completeness scoring ------------------------------------------
t = triage("Design a CAN Data Logger")
sc = score_completeness("Design a CAN Data Logger", t)
miss_ids = {m["id"] for m in sc["missing_fields"]}
rec("bare CAN logger -> low score, missing core fields",
    sc["score_pct"] < 40 and {"mcu", "storage"} <= miss_ids
    and len(sc["suggested_questions"]) > 0,
    f"score={sc['score_pct']}% missing={sorted(miss_ids)}")

p = "Build 48V 13S BMS using STM32G474 BQ76952 INA240 TJA1051 with CAN and MOSFET protection"
t = triage(p)
sc = score_completeness(p, t)
rec("fully-specified BMS -> high score", sc["score_pct"] >= 80,
    f"score={sc['score_pct']}% present={sc['present_fields']}")

# named part satisfies a satisfied_by_named_part field
p = "STM32F405 dev board"
t = triage(p)
rec("named MPN resolves a part", len(t["named_parts"]) >= 1,
    f"named_parts={t['named_parts']}")


# --- Stage 3: 3-way decision + safety override ------------------------------
_, _, d, why = decide("Design a CAN Data Logger")
rec("CAN logger -> ASK_QUESTIONS", d == "ASK_QUESTIONS", why)

_, _, d, why = decide("Design STM32 LED circuit")
rec("STM32 LED -> PROCEED (low complexity direct)", d == "PROCEED", why)

_, _, d, why = decide("Design a 13S EV BMS")
rec("bare BMS -> ASK_QUESTIONS (safety, missing)", d == "ASK_QUESTIONS", why)

# high-score safety design must NOT proceed -- forced to architecture review
p = "Build 48V 13S BMS using STM32G474 BQ76952 INA240 TJA1051 with CAN and MOSFET protection and passive balancing"
_, sc2, d, why = decide(p)
rec("complete BMS -> ARCHITECTURE_FIRST not PROCEED (safety override)",
    d == "ARCHITECTURE_FIRST", f"score={sc2['score_pct']}% {why}")

_, _, d, why = decide("Design a three-phase inverter")
rec("inverter -> never PROCEED (critical)", d in ("ASK_QUESTIONS", "ARCHITECTURE_FIRST"), why)


# --- flag-off byte stability ------------------------------------------------
import importlib
import envil_agent.agent as agent
base_sch = agent.SYSTEM_PROMPT + agent.PAGE_SCOPE_SCHEMATIC
with_intake = agent._system_prompt_for_app("schematic")
rec("flag ON appends INTAKE_RULE", with_intake == base_sch + agent.INTAKE_RULE
    and len(with_intake) > len(base_sch))

# simulate disabled: prompt must be byte-identical to the pre-intake build
orig = agent._intake_enabled
agent._intake_enabled = lambda: False
try:
    off = agent._system_prompt_for_app("schematic")
    rec("flag OFF -> schematic prompt byte-identical to pre-intake",
        off == base_sch)
finally:
    agent._intake_enabled = orig


# --- tool registration ------------------------------------------------------
from envil_agent.tools import SCHEMATIC_TOOLS, tools_for_app
names = {getattr(t, "name", None) for t in SCHEMATIC_TOOLS}
rec("assess_request registered in SCHEMATIC_TOOLS", "assess_request" in names)
sch_names = {getattr(t, "name", None) for t in tools_for_app("schematic")}
rec("assess_request exposed on schematic app", "assess_request" in sch_names)
pcb_names = {getattr(t, "name", None) for t in tools_for_app("pcb")}
rec("assess_request NOT on pcb app", "assess_request" not in pcb_names)


print("\n%d/%d passed" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
