"""Format kicad-cli ERC/DRC violations into a compact LLM-feedback string.

Used by the self_heal node (graphs/nodes/self_heal.py) when residual
errors survive the deterministic autofix pass and need to be escalated
back to the architect for an IR-level redesign.

Output shape (under ~2 KB by default):

    [FIX_MODE=ERC]
    The IR you produced was rendered and the schematic passed IR-level
    validation, but `kicad-cli sch erc` found these violations on the
    resulting .kicad_sch. Adjust the IR (pin lists, net membership,
    missing PWR_FLAG-equivalent drivers) so the next render is clean.

    Violations:
      - 2x power_pin_not_driven e.g. Pin "VO" of U1 ...
        -> A power rail has no power-output driver pin ...
      - 1x isolated_pin_label e.g. Label "+3V3" near U2.21
        -> A net label sits next to a pin that ...

Hints come from `self_heal_config.json:fix_hints` so wording is tunable
without touching code.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "self_heal_config.json"
)


def load_self_heal_config() -> dict:
    """Read self_heal_config.json. Returns {} on any failure so callers
    fall back to defaults instead of crashing."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def summarize_erc_for_llm(
    violations: List[Dict[str, Any]],
    ir: Any = None,
    max_chars: int = 0,
) -> str:
    """Build a compact, IR-actionable summary of ERC violations.

    `violations`: list of dicts shaped like erc_autofix._parse_erc_report
    output. Each has {type, severity, message, locations:[{x,y,descr}, ...]}.

    `ir`: optional parsed TopologyIR. Currently unused but reserved so a
    future revision can mention specific component refs the architect
    used (e.g. "U1 is your AMS1117 -- its VO pin is the power source for
    +3V3"). Kept in the signature so callers don't have to change.

    `max_chars`: 0 means read from config (default 1800). Hard-truncated
    so the architect's context budget doesn't blow up on a runaway
    report.
    """
    if not violations:
        return ""
    cfg = load_self_heal_config()
    hints = cfg.get("fix_hints") or {}
    if max_chars <= 0:
        max_chars = int(cfg.get("summary_max_chars", 1800))

    # Group violations by type. Track count and a couple of sample
    # location descriptors so the architect knows WHICH pin/net failed.
    by_type: Dict[str, Dict[str, Any]] = {}
    for v in violations:
        vtype = v.get("type") or "unknown"
        bucket = by_type.setdefault(vtype, {"count": 0, "locs": []})
        bucket["count"] += 1
        for loc in (v.get("locations") or [])[:3]:
            descr = (loc.get("descr") or "").strip()
            if descr and descr not in bucket["locs"] and len(bucket["locs"]) < 3:
                bucket["locs"].append(descr)

    lines = [
        "[FIX_MODE=ERC]",
        "The IR you produced was rendered and the schematic passed IR-level",
        "validation, but `kicad-cli sch erc` found these violations on the",
        "resulting .kicad_sch. Adjust the IR (pin lists, net membership,",
        "missing PWR_FLAG-equivalent drivers) so the next render is clean.",
        "Re-emit the FULL TopologyIR JSON -- do not patch the previous one.",
        "",
        "Violations:",
    ]
    # Stable order: sort by count desc so the most-frequent issue is
    # surfaced first; tie-break by type name for determinism.
    for vtype, data in sorted(
        by_type.items(), key=lambda kv: (-kv[1]["count"], kv[0])
    ):
        hint = hints.get(vtype) or "Review this violation and adjust the IR."
        sample_locs = data["locs"][:2]
        loc_str = (" e.g. " + " | ".join(sample_locs)) if sample_locs else ""
        lines.append(f"  - {data['count']}x {vtype}{loc_str}")
        lines.append(f"    -> {hint}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 3] + "..."
    return out
