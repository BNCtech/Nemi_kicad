"""Step 2b: LLM refinement of the heuristic classifier.

The heuristic in `classifier.py` is deterministic and free, but plateaus on
unknown ICs whose family isn't covered by the lib_id / net / refdes
patterns. This module takes that output and sends the unresolved (GENERIC)
nodes — or every node, with `reclassify_all` — to Claude in ONE batched
call. The model picks roles from a fixed allowlist and returns a JSON map;
we merge it back into the classified dict.

End-to-end pipeline (schematic -> graph -> heuristic -> LLM refine):

    python -m ai_backend.kicad_layout.llm_classifier design.kicad_sch --out classified.json

The kicad_claude.claude_client is imported read-only — kicad_layout doesn't
modify the repair side, it just reuses the existing Anthropic client.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import load_config
from .classifier import classify, pick_main_controller

try:
    from ai_backend.kicad_claude.claude_client import ClaudeClient
except ImportError:  # supports both `f:/Ki_CAD` and `ai_backend/` on sys.path
    from kicad_claude.claude_client import ClaudeClient  # type: ignore


_SYSTEM_PROMPT = """You are a KiCad component-role classifier. You are given a list of components from one schematic sheet. For EACH component, decide which functional role it plays in the circuit.

You may ONLY use a role from this allowlist:
{ALLOWLIST}

Role meanings (use the most specific role that fits):
- MAIN_CONTROLLER: the central MCU / MPU / FPGA / SoC of this sheet
- MEMORY: flash, EEPROM, SRAM, SDRAM, NVRAM, SD-card interface
- WIRELESS: BLE / WiFi / LoRa / Zigbee / GSM / NFC / RF transceiver
- DISPLAY: LCD / OLED / LED-matrix / driver IC for a panel
- SENSOR: IMU / temp / humidity / pressure / light / current sensor
- MOTOR: H-bridge / stepper / BLDC / brushed motor driver
- POWER_REGULATOR: LDO / buck / boost / charge pump / battery charger
- ANALOG: op-amp / comparator / ADC / DAC / analog switch / reference
- USB / CAN / UART / SPI / I2C / DEBUG: dedicated interface IC or transceiver
- RESET / BOOT / CRYSTAL / PROTECTION: support circuitry on those subsystems
- POWER: bulk / decoupling capacitor or ferrite bead on a power rail
- CONNECTOR: external connector (J*, P*, USB receptacle, header)
- GENERIC: ONLY when you genuinely cannot determine the role — do not use as a hedge

Inputs you get per component: refdes, lib_id, value, pin_count, signal_nets, power_rails. Use the signal_nets list — it reveals which buses the part touches (e.g. SDA/SCL means I2C, OSC_IN means CRYSTAL, USB_DP means USB).

Respond with a JSON object of the exact shape:
{"roles": {"<refdes>": "<ROLE>", "<refdes>": "<ROLE>", ...}}

No prose, no markdown fences outside the JSON, no extra keys. Include EVERY refdes from the input."""


def _extract_json(text: str) -> Dict[str, Any]:
    """Tolerant JSON extractor — strips ``` fences then walks brace depth to
    find the first complete object. Raises ValueError if nothing parses."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unclosed JSON object in response")


def _select_nodes_to_refine(classified: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Which nodes get sent to the LLM. reclassify_all=true sends every node
    (eval mode). Default: GENERIC nodes whose pin_count >= min_pin_count —
    passives stay with the heuristic since net-based voting is already
    authoritative for them."""
    min_pins = int(cfg.get("min_pin_count", 3))
    if cfg.get("reclassify_all"):
        return [n for n in classified["nodes"] if n["pin_count"] >= min_pins]
    return [
        n for n in classified["nodes"]
        if n["role"] == "GENERIC" and n["pin_count"] >= min_pins
    ]


def _format_user_block(nodes: List[Dict[str, Any]]) -> str:
    """One line per node, fixed-width columns — the model parses this far
    more reliably than a JSON dump and we waste no tokens on punctuation."""
    lines = ["COMPONENTS:"]
    for n in nodes:
        nets = ",".join(n.get("signal_nets") or []) or "(none)"
        rails = ",".join(n.get("power_rails") or []) or "(none)"
        lines.append(
            f"  {n['ref']}\tlib_id={n.get('lib_id','')}\t"
            f"value={n.get('value','')}\t"
            f"pins={n['pin_count']}\t"
            f"signal_nets=[{nets}]\t"
            f"power_rails=[{rails}]"
        )
    return "\n".join(lines)


def _strip_heuristic_main(classified: Dict[str, Any]) -> Optional[str]:
    """Undo the heuristic MAIN_CONTROLLER promotion so the LLM sees the node's
    natural role. Returns the refdes of the stripped main (or None). Mutates
    in place — caller is responsible for re-picking after refinement."""
    stripped: Optional[str] = None
    for n in classified["nodes"]:
        if n.get("role") == "MAIN_CONTROLLER":
            n["role"] = n.pop("heuristic_role_pre_main", "GENERIC")
            stripped = n["ref"]
            classified["main_controller"] = None
    return stripped


def llm_refine(
    classified: Dict[str, Any],
    *,
    client: Optional[ClaudeClient] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Send unresolved nodes to Claude, merge returned roles, re-pick main.
    Returns a NEW dict (input not mutated). When `dry_run=True` we build the
    prompt but skip the API call — useful for cost estimation and prompt tuning.

    The heuristic's MAIN_CONTROLLER promotion is STRIPPED before building the
    LLM batch so the model sees the node's natural role (e.g. a buck converter
    misclassified as MAIN by the heuristic shows up as POWER_REGULATOR and the
    LLM can confirm or correct it). MAIN_CONTROLLER is re-picked at the end
    from refined roles, respecting `main_controller.excluded_roles`."""
    cfg_all = load_config("classifier_config")
    cfg = cfg_all.get("llm", {})
    if not cfg.get("enabled", True):
        return dict(classified)

    working = {
        "main_controller": classified.get("main_controller"),
        "nodes": [dict(n) for n in classified["nodes"]],
        "edges": classified["edges"],
    }
    ex_main_ref = _strip_heuristic_main(working)

    targets = _select_nodes_to_refine(working, cfg)
    if ex_main_ref and not any(t["ref"] == ex_main_ref for t in targets):
        for n in working["nodes"]:
            if n["ref"] == ex_main_ref:
                targets.append(n)
                break

    if not targets:
        pick_main_controller(working)
        for n in working["nodes"]:
            n.setdefault("role_source", "heuristic")
        return working

    allow = cfg.get("role_allowlist") or cfg_all["role_priority"]["order"]
    system = _SYSTEM_PROMPT.replace("{ALLOWLIST}", ", ".join(allow))
    user = _format_user_block(targets)

    if dry_run:
        pick_main_controller(working)
        return {
            **working,
            "_llm_prompt_preview": {
                "system_chars": len(system),
                "user_chars": len(user),
                "target_count": len(targets),
                "user": user,
            },
        }

    if client is None:
        model = cfg.get("model") or None
        client = ClaudeClient(model=model) if model else ClaudeClient()
    max_tokens = int(cfg.get("max_tokens", 4000))

    response_text = client.ask(system, user, max_tokens=max_tokens)
    parsed = _extract_json(response_text)
    role_map: Dict[str, str] = parsed.get("roles") or {}

    allow_set = set(allow)
    refined_count = 0
    for node in working["nodes"]:
        new_role = role_map.get(node["ref"])
        if new_role and new_role in allow_set and new_role != node["role"]:
            node["role"] = new_role
            node["role_source"] = "llm"
            refined_count += 1
        elif new_role and new_role in allow_set:
            node["role_source"] = "llm_confirmed"
        else:
            node.setdefault("role_source", "heuristic")

    pick_main_controller(working)

    working["_llm_stats"] = {
        "targets_sent": len(targets),
        "roles_changed": refined_count,
        "response_chars": len(response_text),
        "ex_heuristic_main": ex_main_ref,
        "final_main": working["main_controller"],
    }
    return working


def _input_to_graph_dict(in_path: Path) -> Dict[str, Any]:
    if in_path.suffix == ".kicad_sch":
        from .connectivity_graph import build_graph, graph_to_dict
        return graph_to_dict(build_graph(in_path))
    with open(in_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.llm_classifier")
    ap.add_argument("input", help="graph.json (Step 1 output) or a .kicad_sch")
    ap.add_argument("--out", help="write classified JSON here (default: stdout)")
    ap.add_argument("--no-llm", action="store_true", help="skip LLM refinement (heuristic only)")
    ap.add_argument("--dry-run", action="store_true", help="build prompt, skip API call, dump prompt preview")
    ap.add_argument("--summary", action="store_true", help="print one-line-per-node summary to stderr")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    graph_dict = _input_to_graph_dict(in_path)
    classified = classify(graph_dict)

    if args.no_llm:
        final = classified
    else:
        final = llm_refine(classified, dry_run=args.dry_run)

    payload = json.dumps(final, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        roles: Dict[str, int] = {}
        for n in final["nodes"]:
            roles[n["role"]] = roles.get(n["role"], 0) + 1
        role_summary = ", ".join(f"{k}={v}" for k, v in sorted(roles.items(), key=lambda x: -x[1]))
        stats = final.get("_llm_stats") or {}
        suffix = f"  llm:sent={stats.get('targets_sent', 0)},changed={stats.get('roles_changed', 0)}" if stats else ""
        print(
            f"wrote {args.out}  main={final['main_controller']}  "
            f"roles: {role_summary}{suffix}"
        )
    else:
        sys.stdout.write(payload + "\n")

    if args.summary:
        for n in sorted(final["nodes"], key=lambda x: (x["role"], -x.get("score", 0))):
            src = n.get("role_source", "heuristic")
            sys.stderr.write(
                f"  {n['ref']:6s} {n['role']:16s} [{src:14s}] "
                f"pins={n['pin_count']:2d} deg={n['degree']:2d} "
                f"score={n.get('score', 0):.2f}  lib={n.get('lib_id', '')[:32]}\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
