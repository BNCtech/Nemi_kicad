"""Layer 5 of the classifier: circuit-topology detection.

The four lower layers assign a role to every component. This module reads
that role distribution and infers what KIND of circuit the sheet is —
MCU board, switching power supply, audio analog, motor driver, RF —
so the placer can pick a SHEET-LAYOUT TEMPLATE that fits.

Without this layer the placer uses one MCU-centric zone map for every
sheet. Pure-analog or pure-power designs end up with their main parts
in the GENERIC bottom-center bucket because no MCU exists to anchor
`center`. This module fixes that universally — no per-circuit hardcoding,
the type is detected from role counts + simple signal features.

Public surface:

  detect_circuit_type(classified_dict, cfg) -> str

`classified_dict` is the output of `classifier.classify`. `cfg` is the
`circuit_detection` block of layout_config.json (thresholds + alias map).

Returns one of: `MCU_BOARD`, `POWER_SUPPLY`, `ANALOG`, `MOTOR_DRIVER`,
`RF_CIRCUIT`. MCU_BOARD is the fallback so older layouts (and any sheet
that genuinely has an MCU at the centre) behave exactly as before.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List


_DEFAULT_THRESHOLDS = {
    "power_supply_min_regulators": 1,
    "power_supply_max_mcus": 0,
    "analog_min_analog_role": 3,
    "analog_max_mcus": 0,
    "motor_keywords": ["MOTOR", "DRIVER", "L293", "L298", "DRV8", "TB6", "A4988", "TMC"],
    "motor_min_analog_role": 2,
    "rf_min_wireless_role": 1,
}


def _role_counts(nodes: List[Dict[str, Any]]) -> Counter:
    return Counter(n.get("role", "GENERIC") for n in nodes)


def _has_motor_value(nodes: List[Dict[str, Any]], keywords: List[str]) -> bool:
    """True if any component's value or lib_id mentions a known motor-driver
    family. Keyword list lives in config — adding a new chip family is a
    one-line JSON edit, not a code change."""
    kws = [k.upper() for k in keywords]
    for n in nodes:
        text = f"{n.get('value', '')} {n.get('lib_id', '')}".upper()
        if any(k in text for k in kws):
            return True
    return False


def detect_circuit_type(classified: Dict[str, Any],
                         cfg: Dict[str, Any] | None = None) -> str:
    """Pick a circuit-type tag from the classified-graph role distribution.

    Decision order (first match wins, MCU_BOARD is the fallback):

      1. POWER_SUPPLY — at least N regulators present AND no MAIN_CONTROLLER
         (a board with a regulator AND an MCU is an MCU board with onboard
         regulation, not a standalone supply).
      2. MOTOR_DRIVER — any component's value/lib_id matches the motor-driver
         keyword list AND has ≥ N analog-role parts (gate drivers / sense).
      3. RF_CIRCUIT — at least one WIRELESS-role part. RF modules are the
         centre of any board that includes them.
      4. ANALOG — at least N analog-role parts AND no MAIN_CONTROLLER. Op-amp
         and discrete-analog circuits.
      5. MCU_BOARD — everything else."""
    cfg = {**_DEFAULT_THRESHOLDS, **(cfg or {})}
    nodes = classified.get("nodes", []) or []
    if not nodes:
        return "MCU_BOARD"

    counts = _role_counts(nodes)
    has_mcu = counts.get("MAIN_CONTROLLER", 0) >= 1

    if (counts.get("POWER_REGULATOR", 0) >= int(cfg["power_supply_min_regulators"])
            and counts.get("MAIN_CONTROLLER", 0) <= int(cfg["power_supply_max_mcus"])):
        return "POWER_SUPPLY"

    if (_has_motor_value(nodes, cfg.get("motor_keywords") or [])
            and counts.get("ANALOG", 0) >= int(cfg["motor_min_analog_role"])):
        return "MOTOR_DRIVER"

    if counts.get("WIRELESS", 0) >= int(cfg["rf_min_wireless_role"]):
        return "RF_CIRCUIT"

    if (counts.get("ANALOG", 0) >= int(cfg["analog_min_analog_role"])
            and counts.get("MAIN_CONTROLLER", 0) <= int(cfg["analog_max_mcus"])):
        return "ANALOG"

    return "MCU_BOARD"


def pick_zone_map(circuit_type: str,
                   role_zone_maps: Dict[str, Dict[str, str]],
                   fallback: Dict[str, str] | None = None) -> Dict[str, str]:
    """Look up the zone map for the detected type. If the type has no
    entry in role_zone_maps, fall back to MCU_BOARD's map (the historical
    default). `fallback` is the legacy single-map config used when
    role_zone_maps is entirely absent (back-compat with pre-Layer-5
    layout_config.json)."""
    cleaned = {k: v for k, v in (role_zone_maps or {}).items()
               if not k.startswith("_") and isinstance(v, dict)}
    if circuit_type in cleaned:
        return {k: v for k, v in cleaned[circuit_type].items() if not k.startswith("_")}
    if "MCU_BOARD" in cleaned:
        return {k: v for k, v in cleaned["MCU_BOARD"].items() if not k.startswith("_")}
    return {k: v for k, v in (fallback or {}).items() if not k.startswith("_")}
