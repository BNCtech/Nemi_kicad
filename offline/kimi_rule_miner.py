"""OFFLINE Kimi (Moonshot) support-rule miner — NOT part of the runtime.

Purpose (per the user's plan: Kimi offline -> human verify -> rules): given a
part, ask Kimi which pins REQUIRE external support, GROUNDED on the REAL pin
names from the KiCad symbol (so Kimi can't invent pins). Output SUGGESTED
pin-pattern support rules as JSON for a HUMAN to review and translate into
config/design_checklist.json. It NEVER writes config and is NEVER imported by
the build path — it lives OUTSIDE envil_agent/ on purpose.

    python offline/kimi_rule_miner.py "Battery_Management:BQ7695201PFBR"
    python offline/kimi_rule_miner.py "STM32G474" --out suggestions.json

Runtime stays Claude-only; Kimi is offline tooling. The key is read from
ai_backend/.env (MOONSHOT_API_KEY), never hardcoded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_AI_BACKEND = Path(__file__).resolve().parent.parent          # ai_backend
sys.path.insert(0, str(_AI_BACKEND))

try:
    import truststore                                          # Avast MITM
    truststore.inject_into_ssl()
except Exception:                                              # noqa: BLE001
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(_AI_BACKEND / ".env")
except Exception:                                              # noqa: BLE001
    pass

import httpx


def _kimi(messages, max_tokens: int = 2500) -> str:
    key = os.environ.get("MOONSHOT_API_KEY")
    if not key:
        raise SystemExit("MOONSHOT_API_KEY not set (add it to ai_backend/.env)")
    base = os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1")
    model = os.environ.get("KIMI_MODEL", "moonshot-v1-32k")
    # kimi-k2 reasoning models REQUIRE temperature=1 (the API 400s on 0) and
    # spend tokens on `reasoning_content` BEFORE the final `content` — without
    # generous headroom the answer is truncated to empty. The older
    # moonshot-v1-* models allow temperature 0 for deterministic extraction.
    is_k2 = model.lower().startswith("kimi-k2")
    temp = 1 if is_k2 else 0.0
    if is_k2:
        max_tokens = max(max_tokens, 8000)
    r = httpx.post(
        base + "/chat/completions",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": temp,
              "max_tokens": max_tokens},
        timeout=240,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _resolve_pins(part: str):
    """Return (lib_id, [real pin names]) so Kimi is grounded on the actual
    symbol and cannot invent a pin. Accepts a lib_id or a bare part number."""
    from envil_agent.kicad.symbol_geom import load_symbol
    libs = [part]
    if ":" not in part:
        try:
            from envil_agent.intent.pin_catalog import resolve_lib_ids_from_prompt
            libs = resolve_lib_ids_from_prompt(part) or [part]
        except Exception:                                      # noqa: BLE001
            pass
    for lib in libs:
        try:
            g = load_symbol(lib)
            pins = [(p.name, p.etype or "") for p in (g.pins or [])
                    if p.name and p.name != "~"]
            if pins:
                return lib, pins
        except Exception:                                      # noqa: BLE001
            continue
    return part, []


_SYS = (
    "You are a meticulous hardware datasheet expert. You receive ONE IC (part "
    "number + domain) and its REAL pins as name(etype) from the actual KiCad "
    "symbol. Work in two steps, then output JSON.\n\n"
    "STEP 1 - classify each pin's FUNCTION from the datasheet (use the etype as "
    "a strong hint): power_in | ground | regulator_or_charge_pump_output | "
    "analog_reference | current_sense | comms_bus | digital_io | "
    "control_output | clock_xtal | thermistor | pack_or_load_terminal | "
    "no_connect.\n\n"
    "STEP 2 - emit a support rule ONLY for pins that genuinely need an EXTERNAL "
    "passive per the datasheet. Map by FUNCTION (not by guessing):\n"
    "  regulator/charge-pump output (REG*/CP*/VCAP/BREG/VREG) -> bypass_cap_to_gnd\n"
    "  power_in rail -> bypass_cap_to_gnd (decoupling)\n"
    "  open-drain comms bus (I2C SCL/SDA) -> pullup\n"
    "  clock_xtal (OSC*/XTAL*) -> load_cap_to_gnd (a pair)\n"
    "  current_sense PAIR (SRP/SRN, IN+/IN-, SENSE+/-) -> ONE shunt_across rule "
    "naming BOTH pins (they straddle a milliohm shunt) -- NEVER a pull-up\n"
    "  thermistor (TS*/THERM*) -> thermistor + pullup\n"
    "  reset (NRST/MCLR/~RESET) -> pullup\n\n"
    "HARD RULES (accuracy OVER coverage):\n"
    "  - Use ONLY the exact pin names given; NEVER invent a pin.\n"
    "  - NEVER put a cap on a GROUND pin (VSS/GND/VSSA).\n"
    "  - NEVER call a current_sense pin (SRP/SRN/IN+/IN-) a comms/I2C pin.\n"
    "  - If NOT confident a pin needs an external part, OMIT it.\n\n"
    "Output STRICT JSON only, no prose:\n"
    '{ "part": "<part>", "rules": [ {"pin": "<exact name, or \\"A,B\\" for a '
    'pair>", "function": "<step-1 class>", "support": '
    '"bypass_cap_to_gnd|pullup|pulldown|load_cap_to_gnd|shunt_across|thermistor|termination", '
    '"value": "<typical>", "to": "GND|VCC|<rail or pin>", "why": "<short '
    'datasheet reason>"} ] }'
)


def mine(part: str) -> dict:
    lib, pins = _resolve_pins(part)
    if not pins:
        return {"part": part, "error": "could not resolve symbol pins", "rules": []}
    domain = lib.split(":", 1)[0].replace("_", " ") if ":" in lib else ""
    pin_lines = ", ".join(f"{n}({et})" for n, et in pins)
    user = (f"IC: {part}\nlib_id: {lib}\nDomain (from KiCad library): {domain}\n"
            f"Real pins as name(etype) [{len(pins)}]: {pin_lines}\n\n"
            "Classify each pin (Step 1), then output the required-support rules (Step 2).")
    raw = _kimi([{"role": "system", "content": _SYS},
                 {"role": "user", "content": user}])
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        out = json.loads(m.group(0) if m else raw)
        out["lib_id"] = lib
        return out
    except Exception:                                          # noqa: BLE001
        return {"part": part, "lib_id": lib, "raw_unparsed": raw, "rules": []}


def main() -> int:
    ap = argparse.ArgumentParser(description="OFFLINE Kimi support-rule miner")
    ap.add_argument("part", help="lib_id (Lib:Symbol) or bare part number")
    ap.add_argument("--out", default="", help="write JSON here instead of stdout")
    args = ap.parse_args()
    res = mine(args.part)
    txt = json.dumps(res, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(txt + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(txt)
    print("\n# These are SUGGESTIONS from Kimi. REVIEW them, then translate the "
          "verified ones into config/design_checklist.json pin-pattern rules. "
          "Nothing is auto-applied; runtime never calls Kimi.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
