"""Schematic correctness rule catalog AND deterministic rule applier.

This is the SINGLE source of truth for schematic-quality rules in this codebase.
It is consumed three ways:

  1. as PROSE in the LLM prompts (chat.py, validator.py) -> Claude follows them.
  2. as STRUCTURAL CHECKS in basic_checks.py -> reports violations.
  3. as DETERMINISTIC DETECT + FIX functions here -> turn detectable violations
     into the ops that fix them, without round-tripping Claude (free, fast).

Synthesized 2026-05-14 from KLC, IEEE 315 / ANSI Y32.2, IEC 60617, IEC 60062:2016,
IPC-2612, ASME Y14.35, and 35+ industry sources (TI / Analog Devices / Microchip /
NXP / Altium / Cadence / Sierra Circuits / Phil's Lab / Schemalyzer / EEVblog /
Littelfuse / onsemi / KiCad.info forum / KLC).

Severity:
  CRITICAL = ships dead/damaged hardware
  HIGH     = silicon survives but circuit malfunctions / regulatory fail
  MEDIUM   = readability / maintainability defect that bites later
  LOW      = style / convention
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import nets as _nets
from ._config_loader import load as _load_config
from .schematic_extractor import SchematicExtractor
from .schematic_modifier import SchematicDocument, apply_operation


# (rule_id, category, severity, title, one_line_description)
RULES = [
    # POWER
    ("POWER_001", "POWER", "CRITICAL", "Decoupling cap per power pin",          "Every IC power pin must have its own bypass cap (typ 100 nF ceramic) drawn next to that pin."),
    ("POWER_002", "POWER", "HIGH",     "Bulk cap on each rail entry",            "Each supply rail needs at least one bulk cap (1-10 uF ceramic, or larger electrolytic) at entry."),
    ("POWER_003", "POWER", "HIGH",     "LDO Cin & Cout per datasheet",           "LDO/regulator must show input cap on VIN and output cap on VOUT, type/value per datasheet."),
    ("POWER_004", "POWER", "HIGH",     "Multi-value decoupling for high-speed",  "High-speed digital ICs pair 100 nF with a smaller cap (e.g. 10 nF) to extend HF impedance."),
    ("POWER_005", "POWER", "CRITICAL", "PWR_FLAG on every supply net",           "KiCad ERC requires a PWR_FLAG on each net supplied by a non-power-output pin (connector, regulator)."),
    ("POWER_006", "POWER", "HIGH",     "Separate analog supply (AVDD)",          "Mixed-signal ICs with AVDD/DVDD must have AVDD ferrite-bead-isolated with its own decoupling."),
    ("POWER_007", "POWER", "MEDIUM",   "Power rail names match datasheet",       "VDD/VSS for FET/CMOS, VCC/VEE for BJT; qualify multi-rail (VCC_3V3, AVCC, VBAT)."),
    ("POWER_008", "POWER", "HIGH",     "No back-driving via ESD diodes",         "Do not power signal pins of an IC before VDD; use series R or controlled sequencing."),
    # GROUND
    ("GND_001",   "GROUND", "HIGH",    "Single-point AGND/DGND tie",             "AGND and DGND must merge at exactly one star point (typically under the converter)."),
    ("GND_002",   "GROUND", "MEDIUM",  "Distinct ground symbols per domain",     "Use distinct symbols for GND, AGND, DGND, PGND, EGND so the partition is visible."),
    ("GND_003",   "GROUND", "HIGH",    "No floating GND between sections",       "Every ground island must connect (eventually) to system reference; isolated GND is a defect."),
    ("GND_004",   "GROUND", "MEDIUM",  "Chassis ground separated from signal",   "Chassis (PE) ground uses its own symbol and meets signal GND only at the prescribed bond point."),
    # RESET
    ("RST_001",   "RESET", "HIGH",     "RESET line external pull-up",            "MCU /RESET must have an external pull-up (4.7k-10k) even if internal pull-up exists."),
    ("RST_002",   "RESET", "MEDIUM",   "RESET noise filter cap",                 "Add ~100 nF from /RESET to GND for noise immunity (DNP if PDI/SWD requires)."),
    ("RST_003",   "RESET", "HIGH",     "Reset switch series resistor",           "Momentary reset button needs a series R (100 ohm-1 k) to limit cap-discharge surge."),
    ("RST_004",   "RESET", "HIGH",     "Brown-Out Reset configured",             "Enable on-chip BOR or fit external supervisor (MCP100/TPS3839); bare MCUs misbehave on slow rails."),
    # OSCILLATOR
    ("OSC_001",   "OSCILLATOR", "HIGH",  "Crystal load caps present",            "Every parallel-resonant crystal must show two load caps from XIN/XOUT to GND."),
    ("OSC_002",   "OSCILLATOR", "HIGH",  "Load-cap value per CL formula",        "CL1=CL2=2*(CL_xtal - Cstray); use the crystal datasheet (typ 12-22 pF) — never hardcode 22 pF."),
    ("OSC_003",   "OSCILLATOR", "MEDIUM","Load caps are NP0/C0G",                "Crystal load caps must be C0G/NP0 dielectric (X7R/Y5V causes drift with T and V)."),
    ("OSC_004",   "OSCILLATOR", "MEDIUM","Oscillator pins isolated from noise",  "XIN/XOUT must not run beside high-speed digital nets; draw oscillator block compactly."),
    # PULL-UP / PULL-DOWN
    ("PUL_001",   "PULL", "CRITICAL", "I2C SCL & SDA pull-ups present",          "Every I2C bus must have exactly one pull-up pair (2.2k-10k, default 4.7k @ 5V); open-drain is non-functional without them."),
    ("PUL_002",   "PULL", "HIGH",     "I2C bus capacitance <= 400 pF",           "Total bus C must stay within spec (400 pF Std/Fast, 550 pF Fast+); else use bus buffer."),
    ("PUL_003",   "PULL", "HIGH",     "Single pull-up pair per I2C bus",         "Do not place Rp at every device — one pair per bus, near the master end."),
    ("PUL_004",   "PULL", "CRITICAL", "Unused CMOS input never floats",          "Every unused CMOS/HC/HCT/AHC input must be tied to VDD or GND (not left open)."),
    ("PUL_005",   "PULL", "HIGH",     "MCU strap pins held to required level",   "BOOT/MODE/strap pins (STM32 BOOT0, ESP32 GPIO0/2/15) must show explicit pull-up/down."),
    ("PUL_006",   "PULL", "HIGH",     "Open-drain output requires pull-up",      "Any pin declared open-drain/open-collector must have a pull-up to its bus rail."),
    # SIGNAL INTEGRITY
    ("SI_001",    "SI", "HIGH",       "Diff pair declared as a pair",            "USB D+/D-, Ethernet TX±/RX±, LVDS pairs labeled as a diff pair (suffix _P/_N) and routed as one."),
    ("SI_002",    "SI", "HIGH",       "Termination on transmission lines",       "Match line impedance with series/parallel termination (22-33 ohm series on USB, 100 ohm diff on RS-485/CAN)."),
    ("SI_003",    "SI", "MEDIUM",     "Length-matched pairs flagged",            "Differential or parallel buses (DDR, RGMII) must carry a length-match annotation."),
    ("SI_004",    "SI", "MEDIUM",     "Series R near driver for slow edges",     "High-speed CMOS outputs feeding long traces should have small series R (10-33 ohm) at the driver."),
    # ESD
    ("ESD_001",   "ESD", "CRITICAL",  "TVS on every external connector",         "USB, HDMI, audio, antenna, debug header — every line that leaves the box needs an ESD/TVS device."),
    ("ESD_002",   "ESD", "HIGH",      "TVS VRWM >= rail voltage",                "Pick TVS VRWM at or above operating rail (5V TVS for VBUS, 3.3V for 3V3 logic)."),
    ("ESD_003",   "ESD", "HIGH",      "Low-cap TVS on high-speed lines",         "USB 2.0 HS, USB 3, HDMI, MIPI need TVS with C <= ~1 pF (ideally < 0.5 pF) per line."),
    ("ESD_004",   "ESD", "MEDIUM",    "TVS placed before any other circuitry",   "TVS must be the first thing the external pin sees — drawn between connector and rest of net."),
    # PROTECTION
    ("PROT_001",  "PROTECTION", "HIGH", "Reverse polarity protection on DC in",  "Wall/battery DC inputs need reverse-polarity protection (series diode, P-FET, or bridge)."),
    ("PROT_002",  "PROTECTION", "HIGH", "Input fuse / PTC on power input",       "Each user-accessible power input should be fused (slow-blow / PTC) at <= 80% of board rating."),
    ("PROT_003",  "PROTECTION", "HIGH", "OVP on USB/charger inputs",             "Inputs that can be overdriven by a faulty charger need an OVP clamp (TVS or active OVP IC)."),
    ("PROT_004",  "PROTECTION", "HIGH", "Inductive load freewheel diode",        "Every relay coil, motor, solenoid driven by a transistor must have a flyback diode or RC snubber."),
    # LED (current limiting)
    ("LED_001",   "PROTECTION", "HIGH", "LED current-limit resistor",            "Every LED needs a series current-limit R (R = (Vsrc - Vf) / If). A bare LED across a rail self-destructs."),
    # COMPONENT FITNESS
    ("FIT_001",   "FIT", "CRITICAL",  "Voltage rating with derating",            "Cap rating >= ~2x rail for ceramics (Vdc derating); 50% rule for tantalum; never operate at Vrated."),
    ("FIT_002",   "FIT", "HIGH",      "Resistor power rating margin",            "P_actual <= 50-60% of P_rated (V^2/R or I^2*R)."),
    ("FIT_003",   "FIT", "HIGH",      "Diode I_F & V_R margin",                  "Diode IF_avg <= 70% of rated, V_R peak <= 80% of VRRM."),
    ("FIT_004",   "FIT", "HIGH",      "MOSFET Vds, Vgs, Id with derating",       "Vds <= 80% BVdss, Vgs within rated, Id with thermal margin."),
    ("FIT_005",   "FIT", "MEDIUM",    "Tantalum 50% voltage derating",           "Tantalum-MnO2 caps must be derated to 50% of Vrated (10 V part for 5 V rail max)."),
    ("FIT_006",   "FIT", "MEDIUM",    "Operating temp covers full range",        "Components must cover full board operating-temp range (-40 to +85 C industrial, etc.)."),
    # CONNECTIONS
    ("CON_001",   "CONN", "CRITICAL", "No floating nets",                        "Every net must terminate at >= 2 endpoints (or be deliberately marked NC)."),
    ("CON_002",   "CONN", "CRITICAL", "NC marker on intentionally unused pins",  "Unused IC pins not tied anywhere must carry a no-connect / X marker so ERC and reviewers see intent."),
    ("CON_003",   "CONN", "CRITICAL", "Junction dot at every electrical T",      "Three- or four-way wire meeting must show an explicit junction dot; missing dot = no connection."),
    ("CON_004",   "CONN", "HIGH",     "Avoid 4-way wire intersections",          "Prefer two T-junctions offset by one grid step over a single 4-way crossing."),
    ("CON_005",   "CONN", "HIGH",     "Crossing wires are not connected",        "Wires that simply cross with no dot are explicitly NOT connected; never rely on visual proximity."),
    ("CON_006",   "CONN", "HIGH",     "Pin-type compatibility (KiCad ERC)",      "Power-input pins must be driven by power-output pins or a PWR_FLAG; no two power-outputs on same net."),
    # LABELS
    ("LAB_001",   "LABEL", "HIGH",    "Important nets carry descriptive names",  "Replace auto-names (Net-(U1-Pad7)) with function names (SPI_MOSI, UART_TX, ADC_TEMP)."),
    ("LAB_002",   "LABEL", "MEDIUM",  "Active-low signals use overbar or _N",    "RESETn, /CS, ~OE notation must be consistent (KiCad uses ~{NAME}); pin-name overbar required."),
    ("LAB_003",   "LABEL", "MEDIUM",  "Diff pair suffix _P/_N (or +/-)",         "Diff signals named in matched pairs (USB_DP/USB_DN, CAN_H/CAN_L)."),
    ("LAB_004",   "LABEL", "HIGH",    "Hierarchical labels match parent pins",   "Child-sheet hierarchical labels must exactly match the sheet pins on the parent."),
    ("LAB_005",   "LABEL", "MEDIUM",  "Global labels only for true cross-sheet", "Use globals sparingly — only for power-like or truly project-wide signals; prefer hierarchical."),
    ("LAB_006",   "LABEL", "MEDIUM",  "Bus naming uses PREFIX[M..N]",            "Vector buses follow DATA[0..7] convention; group buses use { }."),
    ("LAB_007",   "LABEL", "LOW",     "No floating net labels",                  "A label not attached to any wire/pin is a documentation lie — remove or reattach."),
    # PLACEMENT
    ("PLA_001",   "PLACE", "HIGH",    "Signal flow left -> right",               "Inputs on left, outputs on right; voltage potentials decrease left to right."),
    ("PLA_002",   "PLACE", "HIGH",    "Power top, ground bottom",                "Positive supplies above the part, ground symbols below; never invert."),
    ("PLA_003",   "PLACE", "CRITICAL","Decoupling cap drawn next to its IC pin", "Decoupling cap visually sits on the same VDD pin it serves; piling caps in the corner is a defect."),
    ("PLA_004",   "PLACE", "MEDIUM",  "Group related parts",                     "Power section, MCU+crystal, analog front-end, connectors — each is a visually grouped block."),
    ("PLA_005",   "PLACE", "MEDIUM",  "One function per sheet",                  "Long schematics split by function; each sheet stands alone with its own labels."),
    ("PLA_006",   "PLACE", "LOW",     "Feedback paths drawn right -> left",      "Negative feedback / control loops drawn opposite to forward flow to make them visible."),
    ("PLA_007",   "PLACE", "MEDIUM",  "Pin-1 / polarity orientation visible",    "IC pin-1, electrolytic +, diode bar must be visible and unambiguous; not hidden behind labels."),
    # LAYOUT — prescriptive zoning + adjacency rules. These are the ones whose
    # violations the user observed: caps far from VDD, labels piled on top of
    # each other, 3V3 net spread everywhere, SWD header far from MCU, scattered
    # GND, cluttered reset/BOOT0 wiring, no functional grouping. Every rule
    # below is paired with a deterministic basic_check (where feasible) so the
    # validator surfaces the defect on the next turn and the AI gets a closed
    # feedback loop. Do NOT relax these without removing the corresponding check.
    ("LAY_001",   "LAYOUT", "HIGH",    "Decoupling cap within 5 mm of VDD pin",   "Each decoupling cap origin must sit within 5 mm of the IC VDD pin it serves; visual adjacency is the rule, not net membership."),
    ("LAY_002",   "LAYOUT", "HIGH",    "Functional zoning per page",              "Power tree top-left, MCU center, IO/connectors right edge, debug/programming header bottom-right, clock cluster left of MCU, reset cluster top-left of MCU. Never inter-leave zones."),
    ("LAY_003",   "LAYOUT", "MEDIUM",  "Crystal block compact",                   "Crystal + 2 load caps + (optional) Rs must fit inside a 15 mm × 15 mm box adjacent to the MCU's OSC_IN/OSC_OUT pins (no labels jumping the block)."),
    ("LAY_004",   "LAYOUT", "MEDIUM",  "Reset block compact and top-left of MCU", "RESET pull-up + filter cap + button live inside a 20 mm × 25 mm box at or above NRST; do not scatter R/C across the page connected by a long wire."),
    ("LAY_005",   "LAYOUT", "MEDIUM",  "Strap-pin pull resistor adjacent",        "BOOT0/MODE/strap-pin pull resistor sits within 7.5 mm of the strap pin; pull-down to GND drawn vertically below the resistor (not a long horizontal trip)."),
    ("LAY_006",   "LAYOUT", "MEDIUM",  "SWD/JTAG header next to MCU",             "Debug header within 25 mm of the MCU's SWDIO/SWCLK (or TCK/TMS) pins; do not route SWD across the page."),
    ("LAY_007",   "LAYOUT", "HIGH",    "Use power-port symbols, not long wires",  "Replace any 3V3/5V/VCC/GND wire longer than 25 mm with a power-port symbol pair. Long power rails crossing the page are a defect."),
    ("LAY_008",   "LAYOUT", "MEDIUM",  "Local GND port per block",                "Each functional block places its own GND port adjacent to the GND-bearing pin; do not run one GND wire from several distant blocks to a single shared symbol."),
    ("LAY_009",   "LAYOUT", "MEDIUM",  "One PWR_FLAG per rail, not per node",     "PWR_FLAG is a hint for ERC; put exactly one per supply rail (at the rail entry / regulator output), never per branch."),
    ("LAY_010",   "LAYOUT", "MEDIUM",  "Label density cap",                       "Within any 12 mm × 12 mm window keep ≤ 4 net labels; rest must use power-port symbols, hierarchical labels, or move to dedicated areas."),
    ("LAY_011",   "LAYOUT", "HIGH",    "No overlapping labels or label-on-pin",   "Two labels must not collide at one anchor; label text must not overlap an MCU pin number or a neighbouring symbol."),
    ("LAY_012",   "LAYOUT", "MEDIUM",  "LED current path drawn linearly",         "PAx -> Rseries -> LED anode -> LED cathode -> GND drawn as a single straight chain (horizontal or vertical), not bent twice around the MCU."),
    ("LAY_013",   "LAYOUT", "MEDIUM",  "Component spacing ≥ 2.54 mm",             "Minimum 2.54 mm (one grid) between any two component bodies on the same sheet; check the symbol_bbox_overlap defect list before placing new parts."),
    ("LAY_014",   "LAYOUT", "MEDIUM",  "Page-edge margin",                        "Keep components ≥ 10 mm from sheet edges so refdes / value text does not collide with the title block or sheet border."),
    ("LAY_015",   "LAYOUT", "LOW",     "Title block populated",                   "Title, project number, date, author, sheet N of M must be filled — empty title block is a readability defect even though ERC ignores it."),
    # IDIOMS — circuit-agnostic visual templates lifted from the reference
    # gold-standard schematic. Each idiom defines the canonical SHAPE of a
    # functional block so the AI emits the same geometry regardless of which
    # MCU / IC / topology the user requests. These are stronger than LAY_001..15
    # (which describe properties); these describe SHAPES.
    ("LAY_016",   "LAYOUT", "HIGH",    "Per-VDD decoupling column",               "Each VDD/VBAT/VDDA/AVDD pin gets its OWN vertical column: pin -> short up-wire -> 100n cap (horizontal) -> short up-wire -> dedicated power-port symbol (e.g. +3V3) above; cap's other terminal -> short down-wire -> dedicated GND port below. NO horizontal rail wire connects adjacent columns — same-name power ports merge electrically. This pattern eliminates wire-through-body errors by construction."),
    ("LAY_017",   "LAYOUT", "MEDIUM",  "Bulk cap leftmost in cap row",            "Bulk capacitor (>=1uF, often polarized electrolytic/tantalum) sits at the LEFT end of the cap row, on the rail entry node (regulator output or VBAT). Place 100n decoupling caps to its right, one per VDD pin. Polarized parts show the '+' terminal up toward the rail port."),
    ("LAY_018",   "LAYOUT", "MEDIUM",  "Reset cluster L-shape",                   "Reset block placed top-left of /RESET pin: pull-up R vertical from +VDD port (top) down to /RESET wire; reset switch SW horizontal between /RESET wire and GND; filter cap C between /RESET wire and GND, immediately right of the switch. Whole block fits in ~20x25 mm; do not separate the three parts across the page."),
    ("LAY_019",   "LAYOUT", "MEDIUM",  "Crystal block T-shape",                   "Crystal block placed adjacent to OSC pins: crystal Y1 horizontal at top (XIN/XOUT labels exit horizontally toward MCU); two load caps vertical directly below Y1, one under each terminal; each load cap's bottom terminal -> dedicated GND port. Block fits in ~15x15 mm. Same shape applies to ANY parallel-resonant crystal, not just MCU HSE."),
    ("LAY_020",   "LAYOUT", "MEDIUM",  "LED indicator vertical chain",            "LED+series-R indicator drawn vertically as a single straight chain: MCU pin (top) -> R (current limit) -> LED (anode toward R, cathode toward GND) -> GND port (bottom). Refdes + value visible on the right of each part. Applies to any single-LED-per-pin indicator (status LEDs, debug LEDs)."),
    ("LAY_021",   "LAYOUT", "MEDIUM",  "Comm-bus labels exit straight",           "Communication-interface net labels (SWDIO/SWDCLK, UART_TX/RX, I2C_SCL/SDA, SPI_*, USB_DP/DN, CAN_H/L) attach directly to their MCU pin and exit horizontally toward the nearest page edge in ONE straight segment. Do not route the label through bends; place its destination header on the same edge."),
    ("LAY_022",   "LAYOUT", "MEDIUM",  "No-connect on every unused IC pin",       "Every IC pin that is intentionally unused must carry an explicit no_connect marker (KiCad's X) at the pin tip. ERC requires this and the marker also doubles as visual documentation that the pin was reviewed, not forgotten."),
    ("LAY_023",   "LAYOUT", "MEDIUM",  "VSS/VSSA tied at bottom",                 "All VSS/AGND/DGND/GND pins of a single IC are routed downward and join one GND port placed below the IC. Do not run separate GND wires to distant GND symbols — one local star-tie per IC."),
    ("LAY_024",   "LAYOUT", "LOW",     "Refdes + value visible per part",         "Both Reference (e.g. R3) and Value (e.g. 10k) text fields are visible adjacent to each part (right side or above), not hidden behind other geometry. Hidden-by-default Footprint / Datasheet stays hidden."),
    # WIRING STYLE
    ("WIR_001",   "WIRE", "HIGH",     "Orthogonal wires only",                   "All wires drawn at right angles (Manhattan); no diagonals."),
    ("WIR_002",   "WIRE", "HIGH",     "No wire-through-component-body",          "A wire must never pass under/through a symbol body — re-route around."),
    ("WIR_003",   "WIRE", "MEDIUM",   "No overlapping/duplicate wires",          "Two wires on the same coordinates is ambiguous and trips junction logic."),
    ("WIR_004",   "WIRE", "MEDIUM",   "Min wire spacing >= 1 grid step",         "Adjacent parallel wires must keep >= one grid (50 mil) of clearance to read distinctly."),
    ("WIR_005",   "WIRE", "MEDIUM",   "Wire exits pin straight, then turns",     "First segment from a pin must run colinear with the pin for >= one grid before any 90-deg turn."),
    ("WIR_006",   "WIRE", "LOW",      "Use power symbols, not long power wires", "Connect to VCC/GND via power-port symbols rather than running a wire across the page."),
    # GRID
    ("GRD_001",   "GRID", "CRITICAL", "All pins on 50-mil grid",                 "KiCad library symbols use 50 mil (1.27 mm); pins/wire-ends off grid will not connect."),
    ("GRD_002",   "GRID", "HIGH",     "Wire endpoints snapped to grid",          "Off-grid wire endpoints cause silent unconnected nets."),
    ("GRD_003",   "GRID", "MEDIUM",   "Don't change grid mid-edit",              "Use one grid (50 mil) for symbols/wires; switch only for text positioning."),
    # VALUES
    ("VAL_001",   "VAL", "HIGH",      "Every R, C, L, D shows a value",          "No part may ship with empty Value field; '?', 'VAL', 'TBD' are defects."),
    ("VAL_002",   "VAL", "MEDIUM",    "IEC 60062 RKM coding",                    "Use 4k7, 100R, 2M2 (R) and 100n, 4n7, 22p (C) — letter replaces decimal. Pick one and apply project-wide."),
    ("VAL_003",   "VAL", "LOW",       "Capacitor units lowercase (IEC 2016)",    "IEC 60062:2016 prescribes lowercase for capacitor units (100n not 100N)."),
    ("VAL_004",   "VAL", "MEDIUM",    "Tolerance shown when not default",        "Show tolerance suffix (1%, 5%) when it matters (FB dividers, current sense, timing)."),
    ("VAL_005",   "VAL", "MEDIUM",    "Dielectric shown for critical caps",      "Tag X7R / C0G / X5R on caps where it matters (osc load, regulator, RF)."),
    # HIERARCHY
    ("HIE_001",   "HIE", "HIGH",      "Block diagram at top",                    "Multi-sheet projects must include a cover/block sheet that names every child + inter-sheet flow."),
    ("HIE_002",   "HIE", "HIGH",      "Sheet pins match child labels",           "Every sheet symbol pin on the parent must have a matching hierarchical label inside the child."),
    ("HIE_003",   "HIE", "MEDIUM",    "Power by global power port, not pin",     "Power nets propagate globally — don't pull them through hierarchical sheet pins."),
    ("HIE_004",   "HIE", "LOW",       "One signal direction per port",           "Set hierarchical-pin direction (input/output/bidir) per actual signal direction; helps ERC."),
    # MARKINGS
    ("MARK_001",  "MARK", "HIGH",     "Polarity bar on electrolytic / tantalum", "Polarized cap symbol must show the curved (-) plate and a + sign on the anode side per IEEE 315."),
    ("MARK_002",  "MARK", "HIGH",     "Diode/LED cathode bar visible",           "Diode bar (cathode) clearly shown; convention: anode left, cathode right when in signal flow."),
    ("MARK_003",  "MARK", "MEDIUM",   "Pin-1 indicator on IC symbols",           "The IC's pin 1 must be visually identifiable (pin number shown, or symbol notch on pin 1)."),
    ("MARK_004",  "MARK", "LOW",      "Reference designators on every part",     "R1, C2, U3 etc. — no part without a refdes; refdes follows IEEE 315 / IEEE 200 letters."),
    ("MARK_005",  "MARK", "LOW",      "Refdes numbering follows reading order",  "Numbering proceeds left-to-right, top-to-bottom (R1 top-left -> R37 bottom-right)."),
    # ERC
    ("ERC_001",   "ERC", "CRITICAL",  "ERC must run clean",                      "Project must pass KiCad ERC with zero errors; warnings reviewed and explicitly waived in notes."),
    ("ERC_002",   "ERC", "HIGH",      "No 'input power not driven' errors",      "Every power-input pin must trace to a power-output pin or PWR_FLAG."),
    ("ERC_003",   "ERC", "HIGH",      "No 'pin no driver' errors",               "Every signal net needs at least one driver (output / bidir)."),
    ("ERC_004",   "ERC", "MEDIUM",    "Connection-grid check enabled",           "Use KiCad's off-grid pin ERC check (Schematic Setup -> Connection width = 50 mil)."),
    # DOCUMENTATION
    ("DOC_001",   "DOC", "HIGH",      "Title block fully populated",             "Title, project number, date, revision, author, sheet N of M required on every sheet."),
    ("DOC_002",   "DOC", "MEDIUM",    "Revision letters skip I,O,Q,S,X,Z",       "ASME Y14.35 / IPC convention: skip ambiguous letters in revision codes."),
    ("DOC_003",   "DOC", "MEDIUM",    "Sheet-of-sheets numbering",               "Every sheet labels itself 'Sheet n of N'."),
    ("DOC_004",   "DOC", "MEDIUM",    "Critical design notes on schematic",      "Calcs (CL math, LED current, divider) annotated near the components they describe."),
    ("DOC_005",   "DOC", "LOW",       "Standard page size (A4/A3/Letter)",       "Sheet uses a standard size to print and review reliably."),
    ("DOC_006",   "DOC", "LOW",       "Designator/footprint visible per part",   "Refdes + value (and optionally footprint) shown next to each component."),
    # KLC SYMBOL
    ("KLC_001",   "KLC", "MEDIUM",    "Pin-name origin outside symbol body",     "Pin-connection points placed on or outside the symbol outline (KLC S3.5)."),
    ("KLC_002",   "KLC", "MEDIUM",    "Pins grouped by function",                "In a custom symbol, group pins by function (power, IO, control), not by package number (KLC S4.2)."),
    ("KLC_003",   "KLC", "LOW",       "Active-low pins use overbar OR bubble",   "Either bar-over-name or inverting-bubble — never both (KLC S4.4 / S4.7)."),
    ("KLC_004",   "KLC", "LOW",       "Footprint blank for generic symbols",     "Generic devices (R_Small, C, LED) leave Footprint blank; user picks (KLC S5.1)."),
    # ADDITIONS 2026-05-17 — synthesized from 20 reference designs (10 ARM Cortex-M
    # + 10 AVR/ESP/PIC/RISC-V). See research notes; each rule below either ships
    # with a deterministic detect/fix pair below or is detect-only (flag).
    ("POWER_009", "POWER", "HIGH",     "LDO/regulator input cap (Cin)",          "Every linear or switching regulator VIN pin needs an input bypass cap (typ 1-10 uF + 100 nF). POWER_003 covers Cout; POWER_009 covers Cin."),
    ("POWER_010", "POWER", "MEDIUM",   "VBAT pin local decoupling",              "Any MCU VBAT (RTC / backup-domain) pin needs its own local 100 nF cap to GND, even if the rail is well-decoupled upstream."),
    ("POWER_011", "POWER", "HIGH",     "VREF / AREF / VREF+ requires GND cap",   "Voltage-reference pins (VREF+, VREFH, AREF, ADC_REF) must have a low-ESR cap (100 nF + 1 uF C0G) to AGND/GND for ADC noise immunity per datasheet."),
    ("POWER_012", "POWER", "HIGH",     "Per-VDD-pin proximity decoupling",       "Every individual VDD/VCC/VDDIO/IOVDD/DVDD pin needs its OWN 100 nF cap within ~10 mm. POWER_001 fires only if the net has NO cap; POWER_012 fires when one cap is shared across many pins (RP2040 has 10 IOVDD pins → 10 caps, not 1)."),
    ("OSC_005",   "OSCILLATOR", "HIGH","32.768 kHz crystal load cap sanity",     "LSE / RTC tuning-fork crystals need 6-12.5 pF load caps (typ. CL=12.5 pF, Cstray ≈3 pF, so 18 pF each is too large). Using 22 pF (HSE value) over-loads the LSE and stops oscillation."),
    ("RST_005",   "RESET", "MEDIUM",   "Auto-reset coupling cap (DTR→RESET)",    "USB-UART bridges (CH340/CP210x/FT232/ATmega16U2) need a 100 nF AC-coupling cap between DTR and MCU /RESET so DTR pulses reset without holding it. For ESP variants, the 2-NPN cross-coupled network replaces this cap; do not draw both."),
    ("PUL_007",   "PULL", "MEDIUM",    "Exactly ONE I2C pull-up pair per bus",   "Each I2C bus must carry a single pull-up pair (one Rp on SDA + one on SCL). Putting Rp at every slave parallels them and crushes bus impedance. PUL_001 ensures the pair exists; PUL_007 ensures only one pair."),
    # DEBUG / PROGRAMMING HEADER
    ("DBG_001",   "DBG", "HIGH",       "SWD/JTAG header VTref local 100n",       "Any ARM Cortex 10-pin / 20-pin debug header (Conn_ARM_JTAG / Conn_ARM_SWD) must have a 100 nF decoupling cap on the VTref / VCC pin so the debugger gets a stable reference voltage during attach."),
    ("DBG_002",   "DBG", "HIGH",       "SWD SRST tied to MCU NRST",              "Cortex-M SWD/JTAG headers with an SRST/RESET pin must wire it to the MCU /NRST so 'Connect Under Reset' works; missing wire breaks recovery from a bricked firmware."),
    # USB INTERFACE
    ("USB_001",   "USB", "HIGH",       "USB D+/D- 22-33 Ω series resistors",     "Full-speed and high-speed USB needs 22-33 Ω series resistors on D+ and D- near the MCU/transceiver for impedance matching (90 Ω diff, 45 Ω SE). Bare wires from connector to MCU fail signal integrity and EMC."),
    ("USB_002",   "USB", "HIGH",       "USB TVS/ESD protection on D+/D-/VBUS",   "USB connectors must terminate D+/D- and VBUS in a low-capacitance TVS array (USBLC6-2, PESD5V0L5UY, SP0503BAHT, TPD2E001). Required for ESD survival per IEC 61000-4-2 and CE/FCC compliance."),
    # Additions #2 — strap-pin floating, AVDD ferrite isolation, polarized cap orientation
    ("STRAP_001", "PULL", "CRITICAL",  "Boot/strap pin must have defined pull",  "BOOT0 / BOOTSEL / GPIO0 / GPIO2 / GPIO15 / EN / CHIP_PU and similar strap pins must have an explicit pull resistor (or direct rail tie) — floating = random power-up behaviour, fried chip on ESP32 GPIO12."),
    ("PROT_006",  "PROTECTION", "MEDIUM", "Polarized cap orientation correct",   "Polarized caps (Device:CP, tantalum, electrolytic) must have '+' on the positive rail and '-' on GND; reversed orientation = bulging / venting cap."),
    # Additions #3 — visual-layout rules from neat-design quantitative study (10 reference designs measured)
    ("LAY_027",   "LAYOUT", "MEDIUM",   "All labels horizontal (0° / 180°)",     "Across every surveyed reference design, every net/global/hierarchical label is at 0° or 180°. Vertical (90°/270°) labels are an instant visual tell of AI-generated schematics."),
    ("LAY_028",   "LAYOUT", "HIGH",     "No 4-way wire intersections",            "Three+ wire arms meeting at one junction must be staggered as two T-junctions offset by one grid; 4-way crossings appear in 0/10 surveyed designs."),
    ("LAY_031",   "LAYOUT", "MEDIUM",   "All coordinates on 50-mil (1.27 mm) grid","Every reference design uses a single 50-mil grid throughout; off-grid pins/wire endpoints cause silent unconnected nets per ERC_004."),
    ("LAY_032",   "LAYOUT", "LOW",      "Power-port refdes/value text hidden",     "Power-port symbols (#PWR0n) and PWR_FLAGs (#FLG0n) MUST keep their Reference and Value text hidden. KiCad's library defaults set these hidden; if they're visible it's clutter and a sure visual tell of AI- or template-generated schematics. Universal rule, applies to every power symbol on every sheet."),
    ("LAY_033",   "LAYOUT", "LOW",      "No redundant rail label next to power symbol", "A (label \"GND\") (or +3V3 / +5V / VCC / ...) on a net that already carries a power-port symbol of the same name is visual clutter — the triangle/arrow IS the rail's name. Delete the label; keep the symbol. Universal rule, derives the rail names from the power-port symbols on each net (no hardcoded list)."),
]


CATEGORIES = {
    "POWER":       "POWER DISTRIBUTION",
    "GROUND":      "GROUND",
    "RESET":       "RESET / POWER-UP",
    "OSCILLATOR":  "OSCILLATOR / CLOCK",
    "PULL":        "PULL-UP / PULL-DOWN",
    "SI":          "SIGNAL INTEGRITY",
    "ESD":         "ESD PROTECTION",
    "PROTECTION":  "INPUT / SUPPLY PROTECTION",
    "USB":         "USB INTERFACE",
    "DBG":         "DEBUG / PROGRAMMING HEADER",
    "FIT":         "COMPONENT FITNESS / DERATING",
    "CONN":        "CONNECTIONS / JUNCTIONS",
    "LABEL":       "LABELS / NET NAMING",
    "PLACE":       "PLACEMENT",
    "LAYOUT":      "LAYOUT / VISUAL ZONING & ADJACENCY",
    "WIRE":        "WIRING STYLE",
    "GRID":        "GRID",
    "VAL":         "VALUES (IEC 60062 / RKM)",
    "HIE":         "HIERARCHY",
    "MARK":        "POLARITY / PIN-1 MARKINGS",
    "ERC":         "KICAD ERC",
    "DOC":         "DOCUMENTATION / TITLE BLOCK",
    "KLC":         "KLC SYMBOL CONVENTIONS",
}


METHODOLOGY = """\
RECOMMENDED DRAWING ORDER (judge whether the user followed this):
 1. Define the block diagram (functional blocks + inter-block signals) -> cover sheet.
 2. Pick main ICs; read each datasheet's Application Information / Typical Schematic.
 3. Set sheet structure & title block (page size, revision, author, sheet N of M).
 4. Set grid to 50 mil and leave it there for symbols and wires.
 5. Draw the power tree first: input -> reverse-polarity -> fuse/PTC -> bulk cap ->
    regulator -> output bulk + decoupling -> power port symbols + PWR_FLAGs.
 6. Place each main IC: VDD pins at top, GND pins at bottom, inputs left, outputs right.
 7. Add decoupling caps IMMEDIATELY on each VDD pin, drawn visually adjacent.
 8. Draw reset & clock circuitry: RESET pull-up + filter cap + button (with series R),
    crystal + load caps computed from datasheet (NP0/C0G).
 9. Add bias networks: pull-ups/-downs, strap pins, I2C pull-up pair (one per bus),
    open-drain pull-ups.
10. Wire signal paths left-to-right, orthogonal only; prefer hierarchical/global labels
    over long wires across the page.
11. Add protection at every external interface (TVS/ESD on USB/HDMI/connectors, OVP,
    ferrites, common-mode chokes).
12. Place connectors and test points / programming headers on the right edge.
13. Annotate refdes left-to-right, top-to-bottom; lock the result.
14. Fill values in IEC 60062 form (4k7, 100n); add tolerances/dielectric where critical;
    add inline notes for non-obvious calculations (CL math, divider ratio, current sense).
15. Run ERC + DfM review; fix all errors, document waived warnings, fill final revision.

GOLD-STANDARD VISUAL IDIOMS (use these SHAPES verbatim — they apply to any
circuit topology, any MCU, any IC. Sizes given as approximate grid distances
on a 1.27 mm schematic grid; scale up for bigger packages):

  Decoupling column (one PER VDD / VBAT / VDDA / AVDD pin):
                          +VDD  <- dedicated power-port symbol, anchor up
                           |
                           |  (short up-stub, ~2.54 mm)
                           |
                         +---+
                         | C |  100nF  <- horizontal cap, value to the right
                         +---+
                           |
                           |  (short down-stub)
                           |
                          GND   <- dedicated GND port, anchor down
                           |
                           |
                       [VDD pin] of the IC, directly below this column
    Crucially: NO horizontal rail wire links adjacent columns. Same-name
    power ports merge into one net electrically; the visual cleanliness
    comes from refusing to draw the merge wire.

  Bulk cap (leftmost in cap row, on rail entry):
                          +VDD
                           |
                          +-+
                          |+|   <- '+' polarity marker on the top plate
                          | |   10uF  (polarized)
                          +-+
                           |
                          GND

  Reset block (top-left of MCU /RESET pin):
                          +VDD
                           |
                          +-+
                          | |   R = 10k  (pull-up)
                          +-+
                           |
                           +---------+---------+--->  /RESET (to MCU pin)
                           |         |
                          [SW]      +-+
                          push      | |  C = 100n  (filter)
                           |        +-+
                           |         |
                          GND       GND

  Crystal block (adjacent to OSC_IN / OSC_OUT pins):
                       Y1 (crystal, horizontal)
                  XIN o----[ ]----o XOUT   ->  XIN/XOUT labels exit
                       |          |            horizontally to MCU
                      +-+        +-+
                      | |        | |     C_load (NP0/C0G, value per
                      +-+        +-+      CL = 2*(CL_xtal - Cstray))
                       |          |
                      GND        GND

  LED indicator (one per status pin):
                         MCU pin
                           |
                          +-+
                          | |   R series  (220R..1k by rail)
                          +-+
                           |
                          \\ /          LED with anode UP, cathode DOWN
                           |
                          GND

  Communication-bus exit (SWD, UART, I2C, SPI, USB):
    MCU pin --[stub]-- LABEL ------------- (label runs horizontally to
                                            the nearest page edge, ONE
                                            straight segment, no bends)

  Strap pin (BOOT0, MODE, ENABLE, ...):
        [strap pin] o---[stub]---o LABEL
                                   |
                                  +-+
                                  | |   R = 10k  (pull to required level)
                                  +-+
                                   |
                            +VDD or GND  (whichever the strap level requires)

  Inductive load freewheel diode (relay coil, motor, solenoid):
                          +VDD
                           |
                           +-----+
                           |     |
                          +-+    |       D = freewheel (1N4148/1N400x)
                          | |   /-\\      cathode (bar) toward +VDD,
                          | |   \\-/      anode toward driver pin.
                       coil L1   |       Cathode at TOP across coil.
                          +-+    |
                           |     |
                           +-----+
                           |
                       [driver pin] (transistor / open-drain output)

  Reverse-polarity protection (DC barrel input):
       VIN_DC >--->|---+----- +VDD  (rail to load + reg)
                D       |
                schottky+--- bulk cap -- GND
                       (low Vf, e.g. SS14)
"""


def render_for_prompt() -> str:
    """Render the full rule list grouped by category, for embedding in the system prompt."""
    by_cat: dict = {}
    for rid, cat, sev, title, desc in RULES:
        by_cat.setdefault(cat, []).append((rid, sev, title, desc))
    out = []
    for cat_key, cat_label in CATEGORIES.items():
        rows = by_cat.get(cat_key, [])
        if not rows:
            continue
        out.append(f"### {cat_label}")
        for rid, sev, title, desc in rows:
            out.append(f"- [{rid}] ({sev}) {title}: {desc}")
        out.append("")
    return "\n".join(out).rstrip()


def all_ids() -> list:
    return [r[0] for r in RULES]


# ===========================================================================
# DETERMINISTIC DETECT + FIX MACHINERY
# ===========================================================================
#
# Every detector returns Finding objects; every fixer turns a Finding into a
# list of apply_operation ops. Pure local; no LLM round-trip; runs in ms.
# A fixer may return [] to mean "detect-only" — the finding still surfaces in
# the report so the LLM sees it next turn, but no automatic ops are emitted
# (used for fixes that are too risky to apply blindly).
#
# Coordinates are placed using a satellite pattern: support parts go at fixed
# grid-aligned offsets from their trigger. The placement is functional, not
# beautiful — the LLM placement pass or KiCad's "Move" can prettify after.
# ===========================================================================

# Module-level constants, sourced from conventions.json so the values are
# editable without touching code. See conventions.json -> defaults.footprints,
# grid.schematic_mm, wire.long_threshold_mm.
_CONV = _load_config("conventions")
GRID_MM = float(_CONV["grid"]["schematic_mm"])
_FP = _CONV.get("defaults", {}).get("footprints", {})
DEFAULT_CAP_FOOTPRINT = _FP.get("capacitor", "Capacitor_SMD:C_0603_1608Metric")
DEFAULT_RES_FOOTPRINT = _FP.get("resistor", "Resistor_SMD:R_0603_1608Metric")
DEFAULT_DIODE_FOOTPRINT = _FP.get("diode", "Diode_SMD:D_SOD-123")
LONG_WIRE_THRESHOLD_MM = float(_CONV.get("wire", {}).get("long_threshold_mm", 25.0))  # LAY_007

# Pin-name patterns. Lowercase; substring match.
RESET_PIN_NAMES = ("nrst", "reset", "~reset", "/reset", "mclr", "~mclr")
SCL_NAMES = ("scl", "i2c_scl", "sclk_i2c")
SDA_NAMES = ("sda", "i2c_sda")
GND_PIN_NAMES = ("gnd", "vss", "vee", "agnd", "dgnd", "pgnd", "egnd")

# POWER_003 — IC family patterns that mean "this is a linear regulator / LDO".
REGULATOR_LIB_HINTS = ("regulator_linear", "regulator_switching", "ldo", "_ldo_", ":ldo")
REGULATOR_VALUE_PATS = [
    re.compile(r"\bLM\s*78\s*\d{2}\b", re.I),
    re.compile(r"\bLM\s*79\s*\d{2}\b", re.I),
    re.compile(r"\bLM\s*317\b", re.I),
    re.compile(r"\bLM\s*337\b", re.I),
    re.compile(r"\bLM\s*1117\b", re.I),
    re.compile(r"\bAMS\s*1117\b", re.I),
    re.compile(r"\bAP\s*2112\b", re.I),
    re.compile(r"\bLD\s*1117\b", re.I),
    re.compile(r"\bLP\s*\d{4}\b", re.I),
    re.compile(r"\bMCP\s*170\d\b", re.I),
    re.compile(r"\bMIC\s*\d{4}\b", re.I),
    re.compile(r"\bTPS\s*\d{4,5}\b", re.I),
    re.compile(r"\bMAX\s*\d{3,4}\b", re.I),
    re.compile(r"\bTL\s*431\b", re.I),
    re.compile(r"\bREG\s*\d{3,4}\b", re.I),
    re.compile(r"\bLDO\b", re.I),
]

VOUT_PIN_NAMES = ("vout", "out", "vo", "v_out", "vreg")
VIN_PIN_NAMES = ("vin", "in", "vi", "v_in")

# PROT_004 — coil-bearing parts that need a freewheel diode.
RELAY_LIB_HINTS = ("relay", ":coil")


from .geom import snap as _snap  # canonical grid snap; reads conventions.json


def _next_ref(used_refs: set, prefix: str) -> str:
    """Allocate the next free refdes for a given letter code."""
    n = 1
    while f"{prefix}{n}" in used_refs:
        n += 1
    return f"{prefix}{n}"


# ---------------------------------------------------------------------------
# Context: everything detectors and fixers need, computed once.
# ---------------------------------------------------------------------------

@dataclass
class RuleContext:
    path: Path
    doc: SchematicDocument
    extractor: SchematicExtractor
    components: List[Dict[str, Any]]
    nets: List[Dict[str, Any]]               # from nets.build_sheet_nets
    pin_endpoints: Dict[Tuple[str, str], Dict[str, Any]]
    # (refdes, pin_number) -> {x, y, name, electrical_type}
    used_refs: set
    component_pins_by_ref: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # ref -> [{number, name, electrical_type, x, y, ...}] from lib_symbol_pins, joined.
    pending_ops: List[Dict[str, Any]] = field(default_factory=list)
    pending_pwr_flags: set = field(default_factory=set)
    placed_satellites: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class Finding:
    rule_id: str
    severity: str
    message: str
    refs: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


def build_context(path) -> RuleContext:
    """Build the full reasoning context for one schematic file (single-sheet)."""
    path = Path(path)
    extractor = SchematicExtractor(path)
    components = extractor.components()
    doc = SchematicDocument(path)

    lib_pins = extractor.lib_symbol_pins()
    pin_endpoints: Dict[Tuple[str, str], Dict[str, Any]] = {}
    component_pins_by_ref: Dict[str, List[Dict[str, Any]]] = {}
    for c in components:
        lib_id = c.get("lib_id", "")
        by_unit = lib_pins.get(lib_id) or {}
        if not by_unit:
            continue
        unit_no = int(c.get("unit", 1))
        pin_defs: List[Dict[str, Any]] = []
        if 0 in by_unit:
            pin_defs.extend(by_unit[0])
        if unit_no in by_unit and unit_no != 0:
            pin_defs.extend(by_unit[unit_no])
        if not pin_defs:
            for u_pins in by_unit.values():
                pin_defs.extend(u_pins)
        ref = c.get("reference", "")
        eps = _nets.placed_pin_endpoints(c, pin_defs)
        component_pins_by_ref.setdefault(ref, []).extend(eps)
        for ep in eps:
            pin_endpoints[(ep["ref"], str(ep["number"]))] = ep

    nets = _nets.build_sheet_nets(extractor)["nets"]
    used_refs = {c.get("reference", "") for c in components if c.get("reference")}

    return RuleContext(
        path=path,
        doc=doc,
        extractor=extractor,
        components=components,
        nets=nets,
        pin_endpoints=pin_endpoints,
        component_pins_by_ref=component_pins_by_ref,
        used_refs=used_refs,
    )


# ---------------------------------------------------------------------------
# Helpers shared by detectors / fixers
# ---------------------------------------------------------------------------

def _net_of_pin(ctx: RuleContext, ref: str, pin_number: str) -> Optional[Dict[str, Any]]:
    for net in ctx.nets:
        for m in net["members"]:
            if m.get("kind") == "pin" and m.get("ref") == ref \
                    and str(m.get("pin_number")) == str(pin_number):
                return net
    return None


def _power_port_value_for_net(net: Dict[str, Any]) -> Optional[str]:
    for m in net["members"]:
        if m.get("kind") == "power" and m.get("name"):
            return m["name"]
    return None


def _has_part_with_prefix(net: Dict[str, Any], prefix: str) -> bool:
    pat = re.compile(rf"^{prefix}\d", re.IGNORECASE)
    for m in net["members"]:
        if m.get("kind") == "pin" and pat.match(m.get("ref", "")):
            return True
    return False


def _find_satellite_slot(ctx: RuleContext, anchor: Tuple[float, float],
                        preferred_offsets: List[Tuple[float, float]]) -> Tuple[float, float]:
    for dx, dy in preferred_offsets:
        slot = (_snap(anchor[0] + dx), _snap(anchor[1] + dy))
        if all(abs(slot[0] - p[0]) > GRID_MM * 1.5 or abs(slot[1] - p[1]) > GRID_MM * 1.5
               for p in ctx.placed_satellites):
            ctx.placed_satellites.append(slot)
            return slot
    last = (_snap(anchor[0] + preferred_offsets[-1][0]),
            _snap(anchor[1] + preferred_offsets[-1][1]))
    ctx.placed_satellites.append(last)
    return last


def _power_lib_id(rail: str) -> str:
    if not rail:
        return "power:VCC"
    r = rail.upper().strip()
    if r in ("GND", "VSS", "DGND", "AGND", "PGND"):
        return f"power:{r}" if r != "VSS" else "power:GND"
    if r in ("+3V3", "+5V", "+12V", "+1V8", "+2V5", "VCC", "VDD", "VBUS", "VBAT", "VIN"):
        return f"power:{r}"
    return f"power:{r}"


# Rails whose Value text we HIDE on the power-port symbol. Per community
# practice (Sparkfun / Adafruit / Phil's Lab / Bald Engineer) confirmed by
# KLC S7.1 (visibility is stylistic, not mandated): the GND triangle is
# universally understood, so the literal letters "GND" beside it are visual
# noise. Named positive/negative rails (+3V3, +5V, VBAT, ...) MUST stay
# visible — the symbol shape alone does not encode the voltage, so the
# label is electrically meaningful.
GND_FAMILY_RAILS = {
    "GND", "VSS", "GNDA", "GNDD", "AGND", "DGND", "PGND", "EGND", "EARTH",
}


def _hide_value_for_rail(rail: str) -> bool:
    """Return True when this rail name should be drawn WITHOUT the value text."""
    if not rail:
        return False
    return rail.upper().strip() in GND_FAMILY_RAILS


def _component_center(ctx: RuleContext, ref: str) -> Optional[Tuple[float, float]]:
    """World (x, y) of the symbol origin for `ref`, or None if not placed."""
    for c in ctx.components:
        if c.get("reference") == ref and c.get("at"):
            return (float(c["at"][0]), float(c["at"][1]))
    return None


def _outward_axis(anchor: Tuple[float, float],
                  center: Optional[Tuple[float, float]]
                  ) -> Tuple[bool, float]:
    """Decide which axis the satellite should grow along, away from `center`.

    Returns (use_vertical, sign):
      use_vertical=True  -> place along Y (cap pins top/bottom, rotation=0)
      use_vertical=False -> place along X (cap pins left/right, rotation=90)
      sign=+1 / -1       -> direction along that axis (KiCad Y is down).
    Without a known center we default to "above" (sign=-1, vertical) because
    most IC VDD pins point up.
    """
    if center is None:
        return True, -1.0
    dx = anchor[0] - center[0]
    dy = anchor[1] - center[1]
    if abs(dy) >= abs(dx):
        return True, (1.0 if dy >= 0 else -1.0)
    return False, (1.0 if dx >= 0 else -1.0)


def _satellite_two_terminal(ctx: RuleContext, anchor: Tuple[float, float],
                            center: Optional[Tuple[float, float]],
                            part_lib_id: str, part_value: str, part_footprint: str,
                            part_prefix: str,
                            port_lib_id: str, port_value: str, port_prefix: str,
                            ) -> List[Dict[str, Any]]:
    """Drop a 2-pin satellite (cap or pull-up R) between `anchor` (a pin tip)
    and a fresh power-port symbol, oriented OUTWARD from `center`.

    Geometry is grid-snapped and dynamic — works for any pin direction:
      - The part's NEAR pin sits ONE grid (2.54 mm) from the anchor.
      - The part's body spans 2.54 mm beyond that.
      - The part's FAR pin connects via a short stub to the port symbol,
        which sits 2.54 mm past the far pin.
    Net wire from near-pin -> anchor and far-pin -> port stays SHORT and
    NEVER crosses the part body.
    """
    use_vertical, sign = _outward_axis(anchor, center)
    if use_vertical:
        # Slot is 5.08 mm along Y from anchor; part_pin1 sits 2.54 mm closer to
        # the anchor and part_pin2 sits 2.54 mm further away. Device:* default
        # has pin1 at top (smaller Y) and pin2 at bottom (larger Y).
        slot = _find_satellite_slot(ctx, anchor, [
            (0, sign * 5.08), (0, sign * 7.62),
            (sign * 5.08, sign * 5.08), (-sign * 5.08, sign * 5.08),
        ])
        rotation = 0.0
        if sign < 0:                       # part above anchor
            near_xy = (slot[0], _snap(slot[1] + 2.54))   # pin2 (bottom)
            far_xy  = (slot[0], _snap(slot[1] - 2.54))   # pin1 (top)
            port_xy = (slot[0], _snap(slot[1] - 5.08))
        else:                              # part below anchor
            near_xy = (slot[0], _snap(slot[1] - 2.54))   # pin1 (top)
            far_xy  = (slot[0], _snap(slot[1] + 2.54))   # pin2 (bottom)
            port_xy = (slot[0], _snap(slot[1] + 5.08))
    else:
        # Rotated 90°: pin1 ends up on the LEFT, pin2 on the RIGHT in schematic
        # coords (KiCad rotation is CCW; lib pin at local (0, -2.54) -> world
        # (-2.54, 0) after rotation and Y-inversion).
        slot = _find_satellite_slot(ctx, anchor, [
            (sign * 5.08, 0), (sign * 7.62, 0),
            (sign * 5.08, 5.08), (sign * 5.08, -5.08),
        ])
        rotation = 90.0
        if sign > 0:                       # part to the right of anchor
            near_xy = (_snap(slot[0] - 2.54), slot[1])   # pin1 (left)
            far_xy  = (_snap(slot[0] + 2.54), slot[1])   # pin2 (right)
            port_xy = (_snap(slot[0] + 5.08), slot[1])
        else:                              # part to the left of anchor
            near_xy = (_snap(slot[0] + 2.54), slot[1])   # pin2 (right)
            far_xy  = (_snap(slot[0] - 2.54), slot[1])   # pin1 (left)
            port_xy = (_snap(slot[0] - 5.08), slot[1])

    part_ref = _next_ref(ctx.used_refs, part_prefix)
    ctx.used_refs.add(part_ref)
    return [
        {"op": "add_component", "lib_id": part_lib_id, "reference": part_ref,
         "value": part_value, "x": slot[0], "y": slot[1], "rotation": rotation,
         "footprint": part_footprint},
        {"op": "add_wire", "points": [list(near_xy), list(anchor)]},
        {"op": "add_component", "lib_id": port_lib_id,
         "reference": f"#{port_prefix}_{port_value}_{part_ref}",
         "value": port_value, "x": port_xy[0], "y": port_xy[1],
         "rotation": 0, "footprint": "",
         "hide_value": _hide_value_for_rail(port_value)},
        {"op": "add_wire", "points": [list(far_xy), list(port_xy)]},
    ]


def _cap_to_gnd(ctx: RuleContext, anchor: Tuple[float, float],
                center: Optional[Tuple[float, float]], value: str = "100n"
                ) -> List[Dict[str, Any]]:
    return _satellite_two_terminal(
        ctx, anchor, center,
        part_lib_id="Device:C", part_value=value, part_footprint=DEFAULT_CAP_FOOTPRINT,
        part_prefix="C",
        port_lib_id="power:GND", port_value="GND", port_prefix="PWR",
    )


def _pullup_to_rail(ctx: RuleContext, anchor: Tuple[float, float],
                    center: Optional[Tuple[float, float]],
                    value: str = "10k", rail: str = "VCC"
                    ) -> List[Dict[str, Any]]:
    return _satellite_two_terminal(
        ctx, anchor, center,
        part_lib_id="Device:R", part_value=value, part_footprint=DEFAULT_RES_FOOTPRINT,
        part_prefix="R",
        port_lib_id=_power_lib_id(rail), port_value=rail, port_prefix="PWR",
    )


def _is_regulator(c: Dict[str, Any]) -> bool:
    lib = (c.get("lib_id") or "").lower()
    val = c.get("value") or ""
    if any(h in lib for h in REGULATOR_LIB_HINTS):
        return True
    return any(p.search(val) for p in REGULATOR_VALUE_PATS)


def _is_relay_or_coil(c: Dict[str, Any]) -> bool:
    lib = (c.get("lib_id") or "").lower()
    ref = c.get("reference", "") or ""
    val = (c.get("value") or "").lower()
    if any(h in lib for h in RELAY_LIB_HINTS):
        return True
    if ref.startswith("K") and not ref.startswith("KEY"):
        return True
    if "relay" in val or "solenoid" in val:
        return True
    return False


# ---------------------------------------------------------------------------
# POWER_001 — decoupling cap on every IC power-input pin
# ---------------------------------------------------------------------------

def detect_POWER_001(ctx: RuleContext) -> List[Finding]:
    cfg = _load_config("basic_checks_config")["functional"]["missing_decoupling"]
    if not cfg.get("enabled", True):
        return []
    cap_prefixes = {p.upper() for p in cfg["cap_prefixes"]}
    pin_types = set(cfg["pin_types"])
    skip_pin_names = {n.lower() for n in cfg["skip_pin_names"]}
    min_pins = int(cfg["min_pins"])

    pin_counts: Dict[str, int] = {}
    for net in ctx.nets:
        for m in net["members"]:
            if m.get("kind") == "pin":
                pin_counts[m["ref"]] = pin_counts.get(m["ref"], 0) + 1

    findings: List[Finding] = []
    for net in ctx.nets:
        has_cap = any(
            m.get("kind") == "pin"
            and re.match(r"^([A-Za-z]+)", m.get("ref") or "")
            and re.match(r"^([A-Za-z]+)", m.get("ref") or "").group(1).upper() in cap_prefixes
            for m in net["members"]
        )
        if has_cap:
            continue
        rail = _power_port_value_for_net(net)

        for m in net["members"]:
            if m.get("kind") != "pin":
                continue
            ref = m["ref"]
            if not ref.startswith(("U", "IC")):
                continue
            if m.get("electrical_type") not in pin_types:
                continue
            pname = (m.get("pin_name") or "").lower()
            if any(g in pname for g in GND_PIN_NAMES):
                continue
            if any(skip in pname for skip in skip_pin_names):
                continue
            if pin_counts.get(ref, 0) < min_pins:
                continue
            findings.append(Finding(
                rule_id="POWER_001",
                severity="high",
                message=f"{ref} pin {m['pin_number']} ({m.get('pin_name','?')}) on rail "
                        f"'{net['name']}' has no decoupling capacitor",
                refs=[ref],
                extra={
                    "ic_ref": ref,
                    "pin_number": str(m["pin_number"]),
                    "rail": rail or net["name"],
                },
            ))
    return findings


def fix_POWER_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    """Drop a 100n decoupling cap between the IC VDD pin (anchor) and a fresh
    GND port. Placement direction is OUTWARD from the IC body center, so the
    cap sits clear of the symbol regardless of which edge the VDD pin is on:

        VDD pin on top of IC    -> cap placed above the pin   (vertical cap)
        VDD pin on bottom of IC -> cap placed below the pin   (vertical cap)
        VDD pin on left of IC   -> cap placed left of the pin (horizontal cap)
        VDD pin on right of IC  -> cap placed right of the pin (horizontal cap)

    Electrically: cap plate A ties to the IC pin (the VDD net), cap plate B
    ties to the new GND port. NEVER shorts VDD to GND. Adding a separate +VDD
    rail port is left to POWER_005 / LAY_016 — keeping this fix narrow avoids
    redundant rail ports near the IC body.
    """
    ic_ref = f.extra["ic_ref"]
    pin = ctx.pin_endpoints.get((ic_ref, f.extra["pin_number"]))
    if not pin:
        return []
    return _cap_to_gnd(ctx, (pin["x"], pin["y"]),
                       _component_center(ctx, ic_ref), value="100n")


def _is_standard_rail(rail: str) -> bool:
    """True if the rail name maps cleanly onto a stock power-port symbol.
    Used by POWER_005-style rules that emit rail ports — POWER_001 itself no
    longer creates rail ports (keeps placement narrow + electrically safe)."""
    if not rail:
        return False
    r = rail.upper().strip()
    return r in {
        "+3V3", "+5V", "+12V", "+1V8", "+2V5", "+24V", "+15V", "-12V", "-5V",
        "VCC", "VDD", "VBUS", "VBAT", "VIN", "+VDC",
    }


# ---------------------------------------------------------------------------
# POWER_002 — bulk cap on each rail entry
# ---------------------------------------------------------------------------

def detect_POWER_002(ctx: RuleContext) -> List[Finding]:
    """Bulk cap on rail ENTRY. Selectively fires only when the rail needs one:

      * Skip GND-family rails (bulk cap concept doesn't apply).
      * Skip rails the user marked as having a bulk cap (>= 1uF) already.
      * Skip rails driven by a regulator's power_out pin — POWER_003 owns
        the regulator's Cout; adding a second bulk cap there is redundant
        and visually noisy.
      * Skip rails with no IC consumer (intermediate scratch nets don't
        need a bulk cap).

    What remains is the "rail enters the board from a connector / battery"
    case, which is exactly where IPC-2612 / TI app notes prescribe a bulk
    cap. So POWER_002 fires only on real rail-entry nodes — not on every
    +3V3/+5V net that happens to have an IC on it.
    """
    findings: List[Finding] = []
    bulk_pat = re.compile(r"(\d+(?:\.\d+)?)\s*[uµU]", re.IGNORECASE)
    seen_rails: set = set()
    for net in ctx.nets:
        rail = _power_port_value_for_net(net)
        if not rail:
            continue
        rail_u = rail.upper()
        if rail_u in seen_rails:
            continue
        if rail_u in ("GND", "VSS", "AGND", "DGND", "PGND", "EGND"):
            continue
        has_ic = any(
            m.get("kind") == "pin" and (m.get("ref", "") or "").startswith(("U", "IC"))
            for m in net["members"]
        )
        if not has_ic:
            continue
        # Regulator output? POWER_003 handles Cout for that case.
        has_power_out = any(
            m.get("kind") == "pin" and m.get("electrical_type") == "power_out"
            for m in net["members"]
        )
        if has_power_out:
            continue
        has_bulk = False
        cap_refs = [m["ref"] for m in net["members"]
                    if m.get("kind") == "pin" and (m.get("ref", "") or "").startswith("C")]
        for cref in cap_refs:
            for c in ctx.components:
                if c.get("reference") != cref:
                    continue
                val = c.get("value") or ""
                m = bulk_pat.search(val)
                if m and float(m.group(1)) >= 1.0:
                    has_bulk = True
                    break
            if has_bulk:
                break
        if has_bulk:
            continue
        anchor = None
        for m in net["members"]:
            if m.get("kind") == "power" and m.get("name") == rail:
                for c in ctx.components:
                    if c.get("reference") == m.get("ref") and c.get("at"):
                        anchor = (float(c["at"][0]), float(c["at"][1]))
                        break
                if anchor:
                    break
        if not anchor:
            continue
        seen_rails.add(rail_u)
        findings.append(Finding(
            rule_id="POWER_002",
            severity="high",
            message=f"rail '{rail}' has no bulk capacitor (>= 1 uF) at rail entry",
            refs=[rail],
            extra={"rail": rail, "anchor": anchor},
        ))
    return findings


def fix_POWER_002(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Bulk cap at the rail-entry power-port location. The "outward" direction
    # from the port is just "downward" by convention (rail port symbol stem
    # points down), so we pass center=anchor+(0, -1) to bias placement below.
    anchor = f.extra["anchor"]
    return _cap_to_gnd(ctx, anchor, (anchor[0], anchor[1] - 1.0), value="10u")


# ---------------------------------------------------------------------------
# POWER_003 — LDO / regulator Cout (Cin is already covered by POWER_001 since
# VIN of a regulator is a power_in pin; Cout pin is power_out so it slips past
# POWER_001 — this rule plugs that gap).
# ---------------------------------------------------------------------------

def detect_POWER_003(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        if not _is_regulator(c):
            continue
        ref = c.get("reference", "")
        for pin in ctx.component_pins_by_ref.get(ref, []):
            pname = (pin.get("name") or "").lower()
            if not any(n == pname or pname.startswith(n) for n in VOUT_PIN_NAMES):
                continue
            pn = str(pin.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None:
                continue
            if _has_part_with_prefix(net, "C"):
                continue
            findings.append(Finding(
                rule_id="POWER_003",
                severity="high",
                message=f"regulator {ref} pin {pn} ({pname}) has no output capacitor",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn,
                       "anchor": (pin["x"], pin["y"])},
            ))
    return findings


def fix_POWER_003(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    anchor = f.extra["anchor"]
    return _cap_to_gnd(ctx, anchor,
                       _component_center(ctx, f.extra["ic_ref"]),
                       value="10u")


# ---------------------------------------------------------------------------
# POWER_005 — PWR_FLAG on every supply net
# ---------------------------------------------------------------------------

def detect_POWER_005(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for net in ctx.nets:
        rail = _power_port_value_for_net(net)
        if not rail:
            continue
        if rail in ctx.pending_pwr_flags:
            continue
        has_flag = any(
            m.get("kind") == "power" and (m.get("ref", "") or "").startswith("#FLG")
            for m in net["members"]
        ) or any(
            m.get("kind") == "power" and (m.get("name", "") or "").upper() == "PWR_FLAG"
            for m in net["members"]
        )
        if has_flag:
            continue
        has_power_out = any(
            m.get("kind") == "pin" and m.get("electrical_type") == "power_out"
            for m in net["members"]
        )
        if has_power_out:
            continue
        anchor = None
        for m in net["members"]:
            if m.get("kind") == "pin":
                ep = ctx.pin_endpoints.get((m["ref"], str(m["pin_number"])))
                if ep:
                    anchor = (ep["x"], ep["y"])
                    break
        if not anchor:
            continue
        findings.append(Finding(
            rule_id="POWER_005",
            severity="critical",
            message=f"rail '{rail}' has no PWR_FLAG and no power_out driver — ERC will fail",
            refs=[rail],
            extra={"rail": rail, "anchor": anchor},
        ))
        ctx.pending_pwr_flags.add(rail)
    return findings


def fix_POWER_005(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    anchor = f.extra["anchor"]
    rail = f.extra["rail"]
    slot = _find_satellite_slot(ctx, anchor, [(-7.62, 0), (7.62, 0), (0, 7.62)])
    return [
        {"op": "add_component", "lib_id": "power:PWR_FLAG",
         "reference": f"#FLG_{rail}", "value": rail,
         "x": slot[0], "y": slot[1], "rotation": 0, "footprint": ""},
        {"op": "add_wire", "points": [[slot[0], slot[1]], [anchor[0], anchor[1]]]},
    ]


# ---------------------------------------------------------------------------
# OSC_001 — load caps on every parallel-resonant crystal
# ---------------------------------------------------------------------------

def detect_OSC_001(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        ref = c.get("reference", "")
        if not (ref.startswith("Y") or ref.startswith("X")):
            continue
        for pin_num in ("1", "2"):
            ep = ctx.pin_endpoints.get((ref, pin_num))
            if not ep:
                continue
            net = _net_of_pin(ctx, ref, pin_num)
            if not net:
                continue
            if _has_part_with_prefix(net, "C"):
                continue
            findings.append(Finding(
                rule_id="OSC_001",
                severity="high",
                message=f"crystal {ref} pin {pin_num} has no load cap to GND",
                refs=[ref],
                extra={"crystal_ref": ref, "pin_number": pin_num,
                       "anchor": (ep["x"], ep["y"])},
            ))
    return findings


def fix_OSC_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Load cap goes OUTWARD from the crystal body in the direction of this
    # pin. _cap_to_gnd handles the L/R or U/D placement automatically.
    anchor = f.extra["anchor"]
    return _cap_to_gnd(ctx, anchor,
                       _component_center(ctx, f.extra["crystal_ref"]),
                       value="22p")


# ---------------------------------------------------------------------------
# RST_001 — pull-up on /RESET pin
# ---------------------------------------------------------------------------

def detect_RST_001(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        ref = c.get("reference", "")
        if not ref.startswith(("U", "IC")):
            continue
        for (r, pn), ep in ctx.pin_endpoints.items():
            if r != ref:
                continue
            pname = (ep.get("name") or "").lower()
            if not any(rn in pname for rn in RESET_PIN_NAMES):
                continue
            net = _net_of_pin(ctx, ref, pn)
            if not net:
                continue
            if _has_part_with_prefix(net, "R"):
                continue
            findings.append(Finding(
                rule_id="RST_001",
                severity="high",
                message=f"{ref} reset pin {pn} ({pname}) has no external pull-up",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn, "anchor": (ep["x"], ep["y"])},
            ))
    return findings


def fix_RST_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    anchor = f.extra["anchor"]
    return _pullup_to_rail(ctx, anchor,
                           _component_center(ctx, f.extra["ic_ref"]),
                           value="10k", rail="VCC")


# ---------------------------------------------------------------------------
# PUL_001 — pull-ups on I2C bus (SCL + SDA)
# ---------------------------------------------------------------------------

def detect_PUL_001(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    seen_nets: set = set()
    for net in ctx.nets:
        name_l = (net.get("name") or "").lower()
        is_scl = "scl" in name_l
        is_sda = "sda" in name_l
        for m in net["members"]:
            if m.get("kind") != "pin":
                continue
            pname = (m.get("pin_name") or "").lower()
            if any(p in pname for p in SCL_NAMES):
                is_scl = True
            if any(p in pname for p in SDA_NAMES):
                is_sda = True
        if not (is_scl or is_sda):
            continue
        if net["name"] in seen_nets:
            continue
        seen_nets.add(net["name"])
        if _has_part_with_prefix(net, "R"):
            continue
        anchor = None
        for m in net["members"]:
            if m.get("kind") == "pin":
                ep = ctx.pin_endpoints.get((m["ref"], str(m["pin_number"])))
                if ep:
                    anchor = (ep["x"], ep["y"])
                    break
        if not anchor:
            continue
        line = "SCL" if is_scl and not is_sda else ("SDA" if is_sda and not is_scl else "I2C")
        findings.append(Finding(
            rule_id="PUL_001",
            severity="critical",
            message=f"I2C line '{net['name']}' ({line}) has no pull-up resistor",
            refs=[net["name"]],
            extra={"net": net["name"], "line": line, "anchor": anchor},
        ))
    return findings


def fix_PUL_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # No IC ref recorded in the finding — the bus may span multiple ICs. Bias
    # placement upward (smaller Y) by passing a synthetic center below the
    # anchor, which is the typical convention for I2C pull-ups on a top-running
    # bus.
    anchor = f.extra["anchor"]
    return _pullup_to_rail(ctx, anchor, (anchor[0], anchor[1] + 1.0),
                           value="4k7", rail="VCC")


# ---------------------------------------------------------------------------
# LED_001 — current-limit R in series with every LED
# ---------------------------------------------------------------------------

def detect_LED_001(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        ref = c.get("reference", "")
        lib = (c.get("lib_id") or "").lower()
        val = (c.get("value") or "").lower()
        is_led = ("led" in lib) or ("led" in val) or ref.startswith("LED")
        if not is_led:
            continue
        ep = ctx.pin_endpoints.get((ref, "1")) or ctx.pin_endpoints.get((ref, "2"))
        if not ep:
            continue
        net = _net_of_pin(ctx, ref, "1") or _net_of_pin(ctx, ref, "2")
        if net and _has_part_with_prefix(net, "R"):
            continue
        findings.append(Finding(
            rule_id="LED_001",
            severity="high",
            message=f"LED {ref} has no series current-limit resistor",
            refs=[ref],
            extra={"led_ref": ref, "anchor": (ep["x"], ep["y"])},
        ))
    return findings


def fix_LED_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    """Series 330 R one grid outward from the LED, with its near pin tied
    to the LED's anode and its far pin LEFT DANGLING — the bridge layer
    or user will route the far pin to whichever MCU / signal drives the
    indicator. We pick R orientation from the LED body so the resistor
    aligns with the LED stem (matches LAY_020's vertical-chain idiom)."""
    px, py = f.extra["anchor"]
    led_center = _component_center(ctx, f.extra["led_ref"])
    use_vertical, sign = _outward_axis((px, py), led_center)

    if use_vertical:
        slot_x, slot_y = _snap(px), _snap(py + sign * 5.08)
        near_xy = (slot_x, _snap(slot_y - sign * 2.54))
        rotation = 0.0
    else:
        slot_x, slot_y = _snap(px + sign * 5.08), _snap(py)
        near_xy = (_snap(slot_x - sign * 2.54), slot_y)
        rotation = 90.0
    ctx.placed_satellites.append((slot_x, slot_y))

    r_ref = _next_ref(ctx.used_refs, "R")
    ctx.used_refs.add(r_ref)
    return [
        {"op": "add_component", "lib_id": "Device:R", "reference": r_ref,
         "value": "330", "x": slot_x, "y": slot_y, "rotation": rotation,
         "footprint": DEFAULT_RES_FOOTPRINT},
        {"op": "add_wire", "points": [list(near_xy), [px, py]]},
    ]


# ---------------------------------------------------------------------------
# PROT_004 — freewheel diode across every relay coil / inductive load
# ---------------------------------------------------------------------------

def detect_PROT_004(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        if not _is_relay_or_coil(c):
            continue
        ref = c.get("reference", "")
        pins = ctx.component_pins_by_ref.get(ref, [])
        # Coil pins typically pin 1+2 or named A1/A2; fall back to the first
        # two pins with electrical_type "passive".
        coil_pins = [p for p in pins
                     if (p.get("name") or "").lower() in ("a1", "a2", "coil1", "coil2", "~", "")
                     or str(p.get("number")) in ("1", "2")]
        if len(coil_pins) < 2:
            coil_pins = pins[:2]
        if len(coil_pins) < 2:
            continue
        # Already protected if either coil net has a diode (D*).
        protected = False
        for p in coil_pins:
            net = _net_of_pin(ctx, ref, str(p["number"]))
            if net and _has_part_with_prefix(net, "D"):
                protected = True
                break
        if protected:
            continue
        # Anchor diode placement between the two coil pin tips.
        a, b = coil_pins[0], coil_pins[1]
        anchor = (_snap((a["x"] + b["x"]) / 2), _snap((a["y"] + b["y"]) / 2))
        findings.append(Finding(
            rule_id="PROT_004",
            severity="high",
            message=f"inductive load {ref} has no freewheel diode across the coil",
            refs=[ref],
            extra={
                "coil_ref": ref,
                "pin_a": (a["x"], a["y"], str(a["number"])),
                "pin_b": (b["x"], b["y"], str(b["number"])),
                "anchor": anchor,
            },
        ))
    return findings


def fix_PROT_004(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    pa = f.extra["pin_a"]
    pb = f.extra["pin_b"]
    anchor = f.extra["anchor"]
    slot = _find_satellite_slot(ctx, anchor, [
        (7.62, 0), (-7.62, 0), (0, 7.62), (0, -7.62),
    ])
    d_ref = _next_ref(ctx.used_refs, "D")
    ctx.used_refs.add(d_ref)
    # Diode pin 1 (cathode K side typically, by Device:D convention pin 1 = K,
    # pin 2 = A — we wire cathode to whichever coil pin sits higher so the
    # diode reverse-biases under normal current). Higher Y in KiCad = lower
    # visually; use the lower-Y (more negative) pin as the cathode side.
    if pa[1] <= pb[1]:
        cathode_xy = (pa[0], pa[1])
        anode_xy = (pb[0], pb[1])
    else:
        cathode_xy = (pb[0], pb[1])
        anode_xy = (pa[0], pa[1])
    # Place diode vertically beside the coil; wire its two terminals to the
    # two coil pin tips. Device:D rotation=0: pin1 (K) at top, pin2 (A) at bottom.
    return [
        {"op": "add_component", "lib_id": "Device:D", "reference": d_ref,
         "value": "1N4148", "x": slot[0], "y": slot[1], "rotation": 0,
         "footprint": DEFAULT_DIODE_FOOTPRINT},
        # cathode (top) -> upper coil pin
        {"op": "add_wire", "points": [[slot[0], _snap(slot[1] - 2.54)],
                                       [slot[0], cathode_xy[1]],
                                       [cathode_xy[0], cathode_xy[1]]]},
        # anode (bottom) -> lower coil pin
        {"op": "add_wire", "points": [[slot[0], _snap(slot[1] + 2.54)],
                                       [slot[0], anode_xy[1]],
                                       [anode_xy[0], anode_xy[1]]]},
    ]


# ---------------------------------------------------------------------------
# CON_002 — no_connect marker on every intentionally unused IC pin
# ---------------------------------------------------------------------------

# Pin electrical-types we'll mark with NC if they're floating. Conservative:
# we skip power_in / power_out (those need real connections, not NC) and
# anything already marked no_connect in the symbol definition.
NC_ELIGIBLE_PIN_TYPES = {
    "input", "output", "bidirectional", "tri_state", "passive",
    "open_collector", "open_emitter", "unspecified",
}

def detect_CON_002(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    # Build a set of (ref, pin_number) that ARE wired to some net.
    on_net: set = set()
    for net in ctx.nets:
        for m in net["members"]:
            if m.get("kind") == "pin":
                on_net.add((m["ref"], str(m["pin_number"])))
    # Existing no_connect markers in the file — skip pins already covered.
    existing_nc_coords = _existing_no_connect_coords(ctx)

    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        for p in pins:
            pn = str(p.get("number"))
            etype = (p.get("electrical_type") or "").lower()
            if etype == "no_connect":
                continue  # symbol itself marks it NC; no marker needed
            if etype not in NC_ELIGIBLE_PIN_TYPES:
                continue
            if (ref, pn) in on_net:
                continue
            xy = (p["x"], p["y"])
            if any(abs(xy[0] - nx) < 0.05 and abs(xy[1] - ny) < 0.05
                   for nx, ny in existing_nc_coords):
                continue
            findings.append(Finding(
                rule_id="CON_002",
                severity="critical",
                message=f"{ref} pin {pn} ({p.get('name','?')}) is unused and has no no_connect marker",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn, "anchor": xy},
            ))
    return findings


def fix_CON_002(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    anchor = f.extra["anchor"]
    return [{"op": "add_no_connect", "x": anchor[0], "y": anchor[1]}]


def _existing_no_connect_coords(ctx: RuleContext) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for child in ctx.doc.tree[1:]:
        if not (isinstance(child, list) and len(child) >= 1):
            continue
        head = child[0]
        head_str = getattr(head, "value", lambda: str(head))() if hasattr(head, "value") else str(head)
        if head_str != "no_connect":
            continue
        for sub in child[1:]:
            if isinstance(sub, list) and len(sub) >= 3:
                sub_head = sub[0]
                sub_head_str = getattr(sub_head, "value", lambda: str(sub_head))() if hasattr(sub_head, "value") else str(sub_head)
                if sub_head_str == "at":
                    try:
                        out.append((float(sub[1]), float(sub[2])))
                    except (TypeError, ValueError):
                        pass
    return out


# ---------------------------------------------------------------------------
# LAY_007 — long power-rail wire (>25 mm) should be replaced by power-port pair.
# Detection only: auto-rewriting wires risks corrupting hand-routed paths;
# surfacing the finding gives the LLM (or user) a concrete defect to clean up.
# ---------------------------------------------------------------------------

def detect_LAY_007(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    # Build per-net wire segments (kicad nets carry wire members too in our schema).
    for net in ctx.nets:
        rail = _power_port_value_for_net(net)
        if not rail:
            continue
        for m in net["members"]:
            if m.get("kind") != "wire":
                continue
            pts = m.get("points") or []
            if len(pts) < 2:
                continue
            # Sum of segment lengths.
            length = 0.0
            for i in range(len(pts) - 1):
                ax, ay = pts[i]
                bx, by = pts[i + 1]
                length += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            if length <= LONG_WIRE_THRESHOLD_MM:
                continue
            findings.append(Finding(
                rule_id="LAY_007",
                severity="high",
                message=f"long power wire on rail '{rail}' (~{length:.1f} mm > "
                        f"{LONG_WIRE_THRESHOLD_MM:.0f} mm) — replace with power-port pair",
                refs=[rail],
                extra={"rail": rail, "length_mm": length, "points": pts},
            ))
    return findings


def fix_LAY_007(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Detection-only for now — see header. The wire endpoints often participate
    # in T-junctions and a blind delete+port substitution risks orphaning the
    # branch geometry. Surface it to the LLM, let it author the rewrite.
    return []


# ---------------------------------------------------------------------------
# LAY_021 — MCU pin signals exit via NET LABELS, not long wires.
# Confirmed against KLC + Phil's Lab / Bald Engineer / EEVblog / Schemalyzer
# (2026-05-17 web research). Rules encoded:
#   * threshold: any signal wire from an MCU pin that runs > 25.4 mm AND has
#     no net label attached anywhere is a defect — auto-fix adds a label at
#     the pin tip using the existing net name.
#   * "MCU" heuristic: any IC (refdes U*/IC*) with >= 8 placed pins.
#   * comm-bus signals (SWD/UART/I2C/SPI/USB/CAN/JTAG) get labels regardless
#     of length — encoded by lowering the threshold to 0 mm for those nets.
#   * power nets never use plain labels (KiCad doc: power propagation needs
#     a power_port). Skip nets driven by a power-port symbol.
# ---------------------------------------------------------------------------

MCU_MIN_PINS = 8
LABEL_LENGTH_THRESHOLD_MM = 25.4
COMM_BUS_PATTERNS = (
    "swdio", "swclk", "swo", "nrst",
    "tdi", "tdo", "tck", "tms", "trst",
    "tx", "rx", "uart", "usart",
    "sda", "scl", "i2c",
    "mosi", "miso", "sck", "ss",
    "spi",
    "usb", "d+", "d-", "dp", "dn",
    "can", "cantx", "canrx",
)


def _is_comm_bus_net(net_name: str) -> bool:
    n = (net_name or "").lower()
    return any(p in n for p in COMM_BUS_PATTERNS)


def _net_already_labeled(net: Dict[str, Any]) -> bool:
    """Does this net already carry a label/global_label/hierarchical_label?"""
    for m in net["members"]:
        if m.get("kind") in ("label", "global_label", "hierarchical_label"):
            return True
    return False


def _mcu_refs(ctx: RuleContext) -> set:
    """ICs with enough pins to count as an MCU / large peripheral."""
    out = set()
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        if len(pins) >= MCU_MIN_PINS:
            out.add(ref)
    return out


def detect_LAY_021(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    mcu_refs = _mcu_refs(ctx)
    if not mcu_refs:
        return findings
    seen_net_pin: set = set()
    for net in ctx.nets:
        # Power nets stay on power_port symbols — never label them.
        if _power_port_value_for_net(net):
            continue
        if _net_already_labeled(net):
            continue
        net_name = net.get("name") or ""
        is_bus = _is_comm_bus_net(net_name)
        # Sum the wire path length on this net.
        wire_len = 0.0
        for m in net["members"]:
            if m.get("kind") != "wire":
                continue
            pts = m.get("points") or []
            for i in range(len(pts) - 1):
                ax, ay = pts[i]
                bx, by = pts[i + 1]
                wire_len += ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        threshold = 0.0 if is_bus else LABEL_LENGTH_THRESHOLD_MM
        if wire_len <= threshold:
            continue
        # Find an MCU-side pin endpoint on this net to anchor the label.
        for m in net["members"]:
            if m.get("kind") != "pin":
                continue
            ref = m.get("ref") or ""
            if ref not in mcu_refs:
                continue
            etype = (m.get("electrical_type") or "").lower()
            if etype in ("power_in", "power_out", "no_connect"):
                continue
            pn = str(m.get("pin_number"))
            if (ref, pn) in seen_net_pin:
                continue
            ep = ctx.pin_endpoints.get((ref, pn))
            if not ep:
                continue
            seen_net_pin.add((ref, pn))
            findings.append(Finding(
                rule_id="LAY_021",
                severity="high" if is_bus else "medium",
                message=(f"{ref} pin {pn} ({m.get('pin_name','?')}) drives net "
                         f"'{net_name}' over ~{wire_len:.1f} mm with no label — "
                         f"attach a net label at the pin tip"
                         + (" (comm-bus signal: labels required)" if is_bus else "")),
                refs=[ref, net_name],
                extra={
                    "ic_ref": ref, "pin_number": pn,
                    "label": net_name or f"NET_{ref}_{pn}",
                    "anchor": (ep["x"], ep["y"]),
                },
            ))
            break  # one finding per net is enough
    return findings


def fix_LAY_021(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    anchor = f.extra["anchor"]
    label = f.extra["label"]
    return [{
        "op": "add_label",
        "name": label,
        "x": anchor[0], "y": anchor[1],
        "kind": "label",
    }]


# ===========================================================================
# 2026-05-17 ADDITIONS — synthesized from 20 reference-design teardowns.
# Each new rule has a deterministic detector. Where the fix is mechanically
# safe (drop a cap / resistor / wire), it ships with a real fix; where the
# fix needs topology judgement (e.g. rerouting AVDD through a ferrite, or
# wiring SRST across nets that may already be intentionally separated), it
# returns [] so the finding still surfaces but no auto-ops are applied.
# ===========================================================================

# Pin / net pattern catalogs used by the new detectors.
VBAT_PIN_NAMES   = ("vbat",)
VREF_PIN_NAMES   = ("vref", "vref+", "vrefh", "vref_p", "vref_pos", "aref", "adc_ref", "vrefint+")
USB_DP_NAMES     = ("d+", "dp", "usb_dp", "usb_d+", "usbdp")
USB_DM_NAMES     = ("d-", "dm", "usb_dm", "usb_d-", "usbdm")
SWD_PIN_NAMES    = ("swdio", "swclk", "swo", "tck", "tms", "tdi", "tdo", "vtref", "vref")
SRST_PIN_NAMES   = ("srst", "nsrst", "reset", "nreset", "~reset", "/reset")
STRAP_PIN_NAMES  = (
    # generic
    "boot", "boot0", "boot1", "bootsel", "mode", "strap", "test",
    # ESP-family (commonly hand-rolled wrong)
    "gpio0", "gpio2", "gpio12", "gpio15", "en", "chip_pu",
    # MSP / AVR
    "tck", "tdi",
)

USB_CONN_LIB_HINTS = ("usb_a", "usb_b", "usb_c", "usb_mini", "usb_micro", "usb_otg", "conn_usb")
SWD_CONN_LIB_HINTS = ("conn_arm_jtag", "conn_arm_swd", "conn_cortex", "conn_swd", "jtag")
TVS_VALUE_PAT = re.compile(r"(USBLC6|PESD\d|SP0503|TPD2E|RClamp|NUP\d|TVS|ESD\b)", re.I)
TVS_LIB_HINT  = ("power_protection", "diode_tvs", "esd")

LSE_FREQ_PAT  = re.compile(r"32\.?768\s*k", re.I)
PF_VALUE_PAT  = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*p\s*F?\s*$", re.I)
OHM_VALUE_PAT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(R|Ω|ohm|Ohm)?\s*$", re.I)

UART_BRIDGE_VALUE_PAT = re.compile(r"(CH340|CP210\d|FT23\d|FT232|CH9102|MCP2200|ATmega16U2|ATmega8U2)", re.I)


def _is_usb_connector(c: Dict[str, Any]) -> bool:
    lib = (c.get("lib_id") or "").lower()
    return any(h in lib for h in USB_CONN_LIB_HINTS)


def _is_swd_header(c: Dict[str, Any]) -> bool:
    lib = (c.get("lib_id") or "").lower()
    if any(h in lib for h in SWD_CONN_LIB_HINTS):
        return True
    # Generic Conn_02x05 / Conn_01x10 that carry SWDIO / SWCLK pins are common
    # in hobby designs — detect by pin-name set instead of lib_id.
    return False


def _net_has_part_value_matching(net: Dict[str, Any], comps: List[Dict[str, Any]],
                                  pattern: re.Pattern) -> bool:
    for m in net["members"]:
        if m.get("kind") != "pin":
            continue
        ref = m.get("ref", "")
        for c in comps:
            if c.get("reference") == ref and pattern.search(c.get("value") or ""):
                return True
    return False


def _net_has_lib_id_matching(net: Dict[str, Any], comps: List[Dict[str, Any]],
                              hints: Tuple[str, ...]) -> bool:
    for m in net["members"]:
        if m.get("kind") != "pin":
            continue
        ref = m.get("ref", "")
        for c in comps:
            if c.get("reference") != ref:
                continue
            lib = (c.get("lib_id") or "").lower()
            if any(h in lib for h in hints):
                return True
    return False


def _component_value(ctx: RuleContext, ref: str) -> str:
    for c in ctx.components:
        if c.get("reference") == ref:
            return c.get("value") or ""
    return ""


# ---------------------------------------------------------------------------
# POWER_009 — LDO / regulator Cin (input bypass cap)
# Mirror of POWER_003 but on the VIN side.
# ---------------------------------------------------------------------------

def detect_POWER_009(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        if not _is_regulator(c):
            continue
        ref = c.get("reference", "")
        for pin in ctx.component_pins_by_ref.get(ref, []):
            pname = (pin.get("name") or "").lower()
            if not any(n == pname or pname.startswith(n) for n in VIN_PIN_NAMES):
                continue
            pn = str(pin.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None:
                continue
            if _has_part_with_prefix(net, "C"):
                continue
            findings.append(Finding(
                rule_id="POWER_009",
                severity="high",
                message=f"regulator {ref} pin {pn} ({pname}) has no input bypass cap",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn,
                       "anchor": (pin["x"], pin["y"])},
            ))
    return findings


def fix_POWER_009(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    return _cap_to_gnd(ctx, f.extra["anchor"],
                       _component_center(ctx, f.extra["ic_ref"]),
                       value="10u")


# ---------------------------------------------------------------------------
# POWER_010 — VBAT pin local 100n cap
# ---------------------------------------------------------------------------

def detect_POWER_010(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        for p in pins:
            pname = (p.get("name") or "").lower()
            if pname not in VBAT_PIN_NAMES:
                continue
            pn = str(p.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None or _has_part_with_prefix(net, "C"):
                continue
            findings.append(Finding(
                rule_id="POWER_010",
                severity="medium",
                message=f"{ref} VBAT pin {pn} has no local decoupling cap",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn,
                       "anchor": (p["x"], p["y"])},
            ))
    return findings


def fix_POWER_010(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    return _cap_to_gnd(ctx, f.extra["anchor"],
                       _component_center(ctx, f.extra["ic_ref"]),
                       value="100n")


# ---------------------------------------------------------------------------
# POWER_011 — VREF / AREF / VREF+ filter cap
# ---------------------------------------------------------------------------

def detect_POWER_011(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        for p in pins:
            pname = (p.get("name") or "").lower()
            if pname not in VREF_PIN_NAMES:
                continue
            pn = str(p.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None or _has_part_with_prefix(net, "C"):
                continue
            findings.append(Finding(
                rule_id="POWER_011",
                severity="high",
                message=f"{ref} reference pin {pn} ({pname}) has no filter cap to GND",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn,
                       "anchor": (p["x"], p["y"])},
            ))
    return findings


def fix_POWER_011(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    return _cap_to_gnd(ctx, f.extra["anchor"],
                       _component_center(ctx, f.extra["ic_ref"]),
                       value="100n")


# ---------------------------------------------------------------------------
# POWER_012 — per-VDD-pin proximity check
# POWER_001 fires only when a net has zero caps. POWER_012 fires when one
# shared cap is asked to decouple multiple distant VDD pins (RP2040 needs 10,
# nRF52840 needs 9, STM32F4 needs 5 — LLMs typically drop one).
# ---------------------------------------------------------------------------

PROXIMITY_RADIUS_MM = 10.16  # 4 grid units; matches LAY_001 + slack for ARM packages

def detect_POWER_012(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    cfg = _load_config("basic_checks_config")["functional"]["missing_decoupling"]
    pin_types = set(cfg["pin_types"])
    cap_prefixes = {p.upper() for p in cfg["cap_prefixes"]}
    # Build a list of (ref, x, y) for every cap so we can do proximity queries.
    cap_xy: List[Tuple[str, float, float]] = []
    for c in ctx.components:
        ref = c.get("reference", "") or ""
        prefix_match = re.match(r"^([A-Za-z]+)", ref)
        if prefix_match and prefix_match.group(1).upper() in cap_prefixes and c.get("at"):
            cap_xy.append((ref, float(c["at"][0]), float(c["at"][1])))

    seen_anchors: set = set()
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        for p in pins:
            if (p.get("electrical_type") or "") not in pin_types:
                continue
            pname = (p.get("name") or "").lower()
            if any(g in pname for g in GND_PIN_NAMES):
                continue
            pn = str(p.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None:
                continue
            # Cap on the SAME net AND within proximity?
            on_net_refs = {m["ref"] for m in net["members"]
                           if m.get("kind") == "pin"
                           and re.match(r"^([A-Za-z]+)", m.get("ref") or "")
                           and re.match(r"^([A-Za-z]+)", m.get("ref") or "").group(1).upper()
                           in cap_prefixes}
            near = any(cref in on_net_refs and
                       (cx - p["x"]) ** 2 + (cy - p["y"]) ** 2
                       <= PROXIMITY_RADIUS_MM ** 2
                       for cref, cx, cy in cap_xy)
            if near:
                continue
            anchor_key = (ref, pn)
            if anchor_key in seen_anchors:
                continue
            seen_anchors.add(anchor_key)
            findings.append(Finding(
                rule_id="POWER_012",
                severity="high",
                message=f"{ref} pin {pn} ({pname}) — no decoupling cap within "
                        f"{PROXIMITY_RADIUS_MM:.1f} mm (per-pin proximity defect)",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn,
                       "anchor": (p["x"], p["y"])},
            ))
    return findings


def fix_POWER_012(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    return _cap_to_gnd(ctx, f.extra["anchor"],
                       _component_center(ctx, f.extra["ic_ref"]),
                       value="100n")


# ---------------------------------------------------------------------------
# OSC_005 — 32.768 kHz LSE crystal load-cap value sanity
# ---------------------------------------------------------------------------

def detect_OSC_005(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        ref = c.get("reference", "") or ""
        if not (ref.startswith("Y") or ref.startswith("X")):
            continue
        if not LSE_FREQ_PAT.search(c.get("value") or ""):
            continue
        # Walk both pins of this LSE crystal; check load cap values.
        for pin_num in ("1", "2"):
            net = _net_of_pin(ctx, ref, pin_num)
            if not net:
                continue
            for m in net["members"]:
                if m.get("kind") != "pin":
                    continue
                cap_ref = m.get("ref", "")
                if not cap_ref.startswith("C"):
                    continue
                val = _component_value(ctx, cap_ref)
                pf = PF_VALUE_PAT.match(val)
                if not pf:
                    continue
                pf_val = float(pf.group(1))
                if pf_val > 15.0:  # 12.5 pF is the typical max; allow 15 pF slack
                    findings.append(Finding(
                        rule_id="OSC_005",
                        severity="high",
                        message=f"LSE crystal {ref}: load cap {cap_ref}={val} is too large "
                                f"({pf_val:.0f} pF > 15 pF). Use 6-12.5 pF NP0/C0G for 32.768 kHz.",
                        refs=[ref, cap_ref],
                        extra={"crystal_ref": ref, "cap_ref": cap_ref,
                               "current_value": val},
                    ))
    return findings


def fix_OSC_005(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Edit the offending cap's value down to 12 pF (a safe LSE default).
    return [{
        "op": "edit_value",
        "reference": f.extra["cap_ref"],
        "value": "12p",
    }]


# ---------------------------------------------------------------------------
# DBG_001 — SWD/JTAG header VTref/VCC local 100n decoupling
# ---------------------------------------------------------------------------

def _swd_header_components(ctx: RuleContext) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Return (component, pins) for every part that looks like a debug header
    (either by lib_id hint or by carrying SWDIO + SWCLK pin names)."""
    out = []
    for c in ctx.components:
        ref = c.get("reference", "") or ""
        pins = ctx.component_pins_by_ref.get(ref, [])
        if not pins:
            continue
        is_hdr = _is_swd_header(c)
        if not is_hdr:
            pin_names = {(p.get("name") or "").lower() for p in pins}
            if "swdio" in pin_names and "swclk" in pin_names:
                is_hdr = True
            elif "tck" in pin_names and "tms" in pin_names:
                is_hdr = True
        if is_hdr:
            out.append((c, pins))
    return out


def detect_DBG_001(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for hdr, pins in _swd_header_components(ctx):
        ref = hdr.get("reference", "")
        for p in pins:
            pname = (p.get("name") or "").lower()
            if pname not in ("vtref", "vcc", "vdd", "vref", "3v3", "+3v3"):
                continue
            pn = str(p.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None or _has_part_with_prefix(net, "C"):
                continue
            findings.append(Finding(
                rule_id="DBG_001",
                severity="high",
                message=f"debug header {ref} VTref pin {pn} has no local decoupling cap",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn,
                       "anchor": (p["x"], p["y"])},
            ))
    return findings


def fix_DBG_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    return _cap_to_gnd(ctx, f.extra["anchor"],
                       _component_center(ctx, f.extra["ic_ref"]),
                       value="100n")


# ---------------------------------------------------------------------------
# DBG_002 — SWD/JTAG SRST tied to MCU /NRST
# ---------------------------------------------------------------------------

def detect_DBG_002(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    headers = _swd_header_components(ctx)
    if not headers:
        return findings
    # Collect MCU NRST pin nets.
    mcu_nrst_nets: List[str] = []
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        for p in pins:
            pname = (p.get("name") or "").lower()
            if any(n in pname for n in RESET_PIN_NAMES):
                net = _net_of_pin(ctx, ref, str(p["number"]))
                if net:
                    mcu_nrst_nets.append(net["name"])
    if not mcu_nrst_nets:
        return findings
    for hdr, pins in headers:
        ref = hdr.get("reference", "")
        for p in pins:
            pname = (p.get("name") or "").lower()
            if pname not in SRST_PIN_NAMES:
                continue
            pn = str(p.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None:
                continue
            if net["name"] in mcu_nrst_nets:
                continue  # already wired correctly
            findings.append(Finding(
                rule_id="DBG_002",
                severity="high",
                message=f"debug header {ref} SRST pin {pn} not connected to MCU /NRST "
                        f"(net '{net['name']}' vs MCU nets {sorted(set(mcu_nrst_nets))})",
                refs=[ref],
                extra={"hdr_ref": ref, "pin_number": pn},
            ))
    return findings


def fix_DBG_002(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Cross-net rewrite (rename one net to the other, or add a long wire)
    # is risky to apply without seeing intent. Flag-only.
    return []


# ---------------------------------------------------------------------------
# USB_001 — D+/D- 22-33 Ω series resistors near MCU/transceiver
# ---------------------------------------------------------------------------

def detect_USB_001(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        if not _is_usb_connector(c):
            continue
        ref = c.get("reference", "") or ""
        for p in ctx.component_pins_by_ref.get(ref, []):
            pname = (p.get("name") or "").lower()
            if pname not in USB_DP_NAMES and pname not in USB_DM_NAMES:
                continue
            pn = str(p.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None:
                continue
            # Is there a small (15-50 Ω) resistor on this net?
            has_series_R = False
            for m in net["members"]:
                if m.get("kind") != "pin":
                    continue
                rref = m.get("ref", "")
                if not rref.startswith("R"):
                    continue
                rval = _component_value(ctx, rref)
                om = OHM_VALUE_PAT.match(rval)
                if om:
                    ohms = float(om.group(1))
                    if 15.0 <= ohms <= 50.0:
                        has_series_R = True
                        break
            if has_series_R:
                continue
            findings.append(Finding(
                rule_id="USB_001",
                severity="high",
                message=f"USB connector {ref} pin {pn} ({pname}) has no 22-33 Ω series resistor "
                        f"on net '{net['name']}' — required for impedance matching",
                refs=[ref],
                extra={"conn_ref": ref, "pin_number": pn,
                       "anchor": (p["x"], p["y"]), "line": pname},
            ))
    return findings


def fix_USB_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Drop a 22Ω satellite series resistor with one end on the USB pin,
    # other end dangling — the bridge layer (or user) reroutes downstream.
    # Same shape as LED_001 (single-terminal satellite).
    px, py = f.extra["anchor"]
    conn_center = _component_center(ctx, f.extra["conn_ref"])
    use_vertical, sign = _outward_axis((px, py), conn_center)
    if use_vertical:
        slot_x, slot_y = _snap(px), _snap(py + sign * 5.08)
        near_xy = (slot_x, _snap(slot_y - sign * 2.54))
        rotation = 0.0
    else:
        slot_x, slot_y = _snap(px + sign * 5.08), _snap(py)
        near_xy = (_snap(slot_x - sign * 2.54), slot_y)
        rotation = 90.0
    ctx.placed_satellites.append((slot_x, slot_y))
    r_ref = _next_ref(ctx.used_refs, "R")
    ctx.used_refs.add(r_ref)
    return [
        {"op": "add_component", "lib_id": "Device:R", "reference": r_ref,
         "value": "22R", "x": slot_x, "y": slot_y, "rotation": rotation,
         "footprint": DEFAULT_RES_FOOTPRINT},
        {"op": "add_wire", "points": [list(near_xy), [px, py]]},
    ]


# ---------------------------------------------------------------------------
# USB_002 — USB D+/D-/VBUS TVS / ESD protection present
# ---------------------------------------------------------------------------

def detect_USB_002(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        if not _is_usb_connector(c):
            continue
        ref = c.get("reference", "") or ""
        # Walk D+, D-, VBUS nets and check if ANY of them carries a TVS-like
        # component (by value pattern or by lib_id hint). One TVS array
        # protecting the connector counts for all three lines.
        protected = False
        for p in ctx.component_pins_by_ref.get(ref, []):
            pname = (p.get("name") or "").lower()
            if pname not in USB_DP_NAMES and pname not in USB_DM_NAMES \
                    and pname not in ("vbus", "v_bus", "+5v"):
                continue
            net = _net_of_pin(ctx, ref, str(p["number"]))
            if net is None:
                continue
            if _net_has_part_value_matching(net, ctx.components, TVS_VALUE_PAT) \
                    or _net_has_lib_id_matching(net, ctx.components, TVS_LIB_HINT):
                protected = True
                break
        if protected:
            continue
        anchor = None
        if c.get("at"):
            anchor = (float(c["at"][0]), float(c["at"][1]))
        findings.append(Finding(
            rule_id="USB_002",
            severity="high",
            message=f"USB connector {ref} has no TVS / ESD array on D+/D-/VBUS — "
                    f"add USBLC6-2 / PESD5V0L5UY / SP0503BAHT or equivalent",
            refs=[ref],
            extra={"conn_ref": ref, "anchor": anchor},
        ))
    return findings


def fix_USB_002(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # TVS part choice is vendor-specific (USBLC6 for FS, SP0503 for HS,
    # PESD5V0L5UY for cost-sensitive). Don't auto-pick — flag only.
    return []


# ---------------------------------------------------------------------------
# PUL_007 — exactly ONE pull-up pair per I2C bus
# ---------------------------------------------------------------------------

def detect_PUL_007(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for net in ctx.nets:
        name_l = (net.get("name") or "").lower()
        is_i2c = "scl" in name_l or "sda" in name_l
        if not is_i2c:
            for m in net["members"]:
                if m.get("kind") != "pin":
                    continue
                pname = (m.get("pin_name") or "").lower()
                if any(p in pname for p in SCL_NAMES + SDA_NAMES):
                    is_i2c = True
                    break
        if not is_i2c:
            continue
        # Count resistors that LOOK like pull-ups: R on this net + same R also
        # touches a VCC/VDD/+3V3/+5V power port. We approximate by counting
        # all Rs on the net whose value is in the typical pull-up range.
        pullup_count = 0
        rrefs = [m["ref"] for m in net["members"]
                 if m.get("kind") == "pin"
                 and (m.get("ref") or "").startswith("R")]
        for rref in set(rrefs):
            val = _component_value(ctx, rref)
            om = OHM_VALUE_PAT.match(val)
            if not om and not re.match(r"^\s*\d+(\.\d+)?\s*[kK]", val):
                continue
            pullup_count += 1
        if pullup_count <= 1:
            continue  # 0 caught by PUL_001; exactly 1 is correct for SDA or SCL
        findings.append(Finding(
            rule_id="PUL_007",
            severity="medium",
            message=f"I2C net '{net['name']}' has {pullup_count} pull-up resistors — "
                    f"keep exactly ONE per line (Rp at master end only)",
            refs=[net["name"]],
            extra={"net": net["name"], "count": pullup_count},
        ))
    return findings


def fix_PUL_007(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Removing duplicates requires knowing which is "the master" — flag only.
    return []


# ---------------------------------------------------------------------------
# RST_005 — USB-UART DTR → MCU /RESET auto-reset coupling cap
# ---------------------------------------------------------------------------

def detect_RST_005(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    # Are there ANY USB-UART bridges in this schematic?
    bridges = [c for c in ctx.components
               if UART_BRIDGE_VALUE_PAT.search(c.get("value") or "")]
    if not bridges:
        return findings
    # Find MCU /RESET nets.
    mcu_reset_nets: set = set()
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        for p in pins:
            pname = (p.get("name") or "").lower()
            if any(n in pname for n in RESET_PIN_NAMES):
                net = _net_of_pin(ctx, ref, str(p["number"]))
                if net:
                    mcu_reset_nets.add(net["name"])
    if not mcu_reset_nets:
        return findings
    # For each bridge, look for a DTR pin and check if its net joins /RESET
    # via a 100 nF cap.
    for bridge in bridges:
        bref = bridge.get("reference", "")
        for p in ctx.component_pins_by_ref.get(bref, []):
            pname = (p.get("name") or "").lower()
            if pname not in ("dtr", "ndtr", "~dtr", "dtr#"):
                continue
            net = _net_of_pin(ctx, bref, str(p["number"]))
            if net is None:
                continue
            # If the DTR net already contains a small cap whose other terminal
            # is on a reset net, we're good.
            has_coupler = False
            for m in net["members"]:
                if m.get("kind") != "pin":
                    continue
                if (m.get("ref") or "").startswith("C"):
                    has_coupler = True
                    break
            if has_coupler:
                continue
            findings.append(Finding(
                rule_id="RST_005",
                severity="medium",
                message=f"USB-UART {bref} has DTR but no 100 nF coupling cap to MCU /RESET — "
                        f"upload-from-IDE auto-reset will not work",
                refs=[bref],
                extra={"bridge_ref": bref},
            ))
    return findings


def fix_RST_005(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # The cap goes BETWEEN two named nets (DTR and /RESET) — placing it
    # requires picking endpoints on both. Defer to the LLM layer for now.
    return []


# ---------------------------------------------------------------------------
# STRAP_001 — Boot / strap pin must have a defined pull (no floating).
# Single highest LLM-mistake-catcher per the research synthesis: ESP32 GPIO12
# pulled the wrong way fries the chip; STM32 BOOT0 floating boots into the
# wrong mode randomly. Conservative pin-name catalog — does NOT include
# /RESET (covered by RST_001) or generic GPIOn (only ESP-strap GPIOs).
# ---------------------------------------------------------------------------

STRAP_PIN_NAMES_STRICT = (
    "boot", "boot0", "boot1", "bootsel", "mode", "strap", "test",
    "gpio0", "gpio2", "gpio12", "gpio15",
    "en", "chip_pu",
)

POSITIVE_RAIL_HINTS = ("vcc", "vdd", "+3v3", "+5v", "+1v8", "+2v5", "+12v",
                       "vbat", "vbus", "vin", "+vdc")
NEGATIVE_RAIL_HINTS = ("gnd", "vss", "agnd", "dgnd", "pgnd", "egnd")


def _net_ties_to_rail(net: Dict[str, Any], rail_hints: Tuple[str, ...]) -> bool:
    for m in net["members"]:
        if m.get("kind") != "power":
            continue
        name = (m.get("name") or "").lower()
        if any(h in name for h in rail_hints):
            return True
    return False


def detect_STRAP_001(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        for p in pins:
            pname = (p.get("name") or "").lower()
            if pname not in STRAP_PIN_NAMES_STRICT:
                continue
            pn = str(p.get("number"))
            net = _net_of_pin(ctx, ref, pn)
            if net is None:
                continue
            # Pulled if the net has a resistor AND that resistor's other end
            # ties to either a positive rail or GND. We approximate: any R on
            # the net, plus a power port on the net OR an R that bridges to a
            # power port (single-hop — good enough since most pulls are 1 R).
            has_r = _has_part_with_prefix(net, "R")
            ties_rail = _net_ties_to_rail(net, POSITIVE_RAIL_HINTS) \
                        or _net_ties_to_rail(net, NEGATIVE_RAIL_HINTS)
            if has_r and ties_rail:
                continue  # pulled to a rail via R — fine
            if ties_rail and not has_r:
                # Hard-tied to a rail with no resistor: explicit but inflexible.
                # For BOOT0/BOOT1 on STM32 this is OK; for ESP EN it's wrong
                # (no RC delay). Flag at LOW so reviewers can audit.
                findings.append(Finding(
                    rule_id="STRAP_001",
                    severity="low",
                    message=f"{ref} strap pin {pn} ({pname}) is hard-tied to a rail with no "
                            f"series resistor — review whether RC delay or pull-R is needed",
                    refs=[ref],
                    extra={"ic_ref": ref, "pin_number": pn,
                           "anchor": (p["x"], p["y"]), "strap": pname},
                ))
                continue
            if has_r and not ties_rail:
                # R present but no rail visible — possibly pulls to a labelled
                # net that we can't trace. Defer to flag-only at MEDIUM.
                findings.append(Finding(
                    rule_id="STRAP_001",
                    severity="medium",
                    message=f"{ref} strap pin {pn} ({pname}) has a pull-R but no visible rail tie "
                            f"on net '{net['name']}'",
                    refs=[ref],
                    extra={"ic_ref": ref, "pin_number": pn,
                           "anchor": (p["x"], p["y"]), "strap": pname},
                ))
                continue
            # Neither pulled nor tied — floating strap pin. CRITICAL.
            findings.append(Finding(
                rule_id="STRAP_001",
                severity="critical",
                message=f"{ref} strap/boot pin {pn} ({pname}) is FLOATING — chip behaviour "
                        f"on power-up is undefined; add 10k pull to required level",
                refs=[ref],
                extra={"ic_ref": ref, "pin_number": pn,
                       "anchor": (p["x"], p["y"]), "strap": pname},
            ))
    return findings


_STRAP_CATALOG_CACHE: Optional[Dict[str, Any]] = None


def _load_strap_catalog() -> Dict[str, Any]:
    """Lazy-load the per-MCU strap-pin direction catalog from JSON."""
    global _STRAP_CATALOG_CACHE
    if _STRAP_CATALOG_CACHE is None:
        import json
        from pathlib import Path
        path = Path(__file__).parent / "strap_pin_catalog.json"
        with open(path, "r", encoding="utf-8") as fh:
            _STRAP_CATALOG_CACHE = json.load(fh)
    return _STRAP_CATALOG_CACHE


def _resolve_strap_family(lib_id: str) -> Optional[str]:
    """Match an IC's lib_id against the strap catalog's family hints."""
    if not lib_id:
        return None
    lib_low = lib_id.lower()
    cat = _load_strap_catalog()
    for family, hints in cat["_match_lib_id_substrings"].items():
        if any(h in lib_low for h in hints):
            return family
    return None


def fix_STRAP_001(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    """Auto-fix: drop the catalog-specified pull resistor (10k typ) in the
    direction the datasheet requires. Wrong direction on ESP32 GPIO12 destroys
    the flash chip — so we ONLY fix when the IC's lib_id matches a catalog
    family AND the pin name matches a catalog entry. Anything else returns []
    (flag-only) so the user / LLM decides."""
    ic_ref = f.extra["ic_ref"]
    pname = (f.extra.get("strap") or "").lower()
    if not ic_ref or not pname:
        return []
    # Find this component's lib_id.
    lib_id = ""
    for c in ctx.components:
        if c.get("reference") == ic_ref:
            lib_id = c.get("lib_id") or ""
            break
    family = _resolve_strap_family(lib_id)
    if not family:
        return []  # MCU not in catalog — flag only
    cat = _load_strap_catalog()
    entries = cat["families"].get(family, [])
    spec = next((e for e in entries if e["pin_name"].lower() == pname), None)
    if not spec:
        return []
    pull = spec.get("pull", "unknown")
    if pull not in ("up", "down"):
        return []  # unknown / hard-tie-rail / either — too risky to auto-fix
    rail = "VCC" if pull == "up" else "GND"
    rail_lib = "power:VCC" if pull == "up" else "power:GND"
    r_value = spec.get("r_ohm", "10k")
    # _pullup_to_rail does VCC by default; for "down" we need GND port and a
    # resistor between the strap pin and GND — same satellite shape.
    return _satellite_two_terminal(
        ctx, f.extra["anchor"], _component_center(ctx, ic_ref),
        part_lib_id="Device:R", part_value=r_value,
        part_footprint=DEFAULT_RES_FOOTPRINT, part_prefix="R",
        port_lib_id=rail_lib, port_value=rail, port_prefix="PWR",
    )


# ---------------------------------------------------------------------------
# POWER_007 — Analog supply (AVDD / VDDA / AVCC) ferrite-bead isolated
# from digital VDD. Catches the very common LLM mistake of tying AVDD
# directly to the same net as VDD with no LC filter.
# ---------------------------------------------------------------------------

AVDD_PIN_NAMES = ("avdd", "vdda", "avcc", "vddana", "vdd_ana", "vdd_a", "adc_avdd")
ISOLATION_LIB_HINTS = ("device:l", "device:fb", "ferritebead", "ferrite_bead",
                       "_ferrite", ":fb_", ":fb ", "inductor")


def _net_has_isolation_part(net: Dict[str, Any], comps: List[Dict[str, Any]]) -> bool:
    for m in net["members"]:
        if m.get("kind") != "pin":
            continue
        ref = m.get("ref", "")
        if ref.startswith(("L", "FB", "FL")):
            return True
        for c in comps:
            if c.get("reference") != ref:
                continue
            lib = (c.get("lib_id") or "").lower()
            if any(h in lib for h in ISOLATION_LIB_HINTS):
                return True
    return False


def detect_POWER_007(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for ref, pins in ctx.component_pins_by_ref.items():
        if not ref.startswith(("U", "IC")):
            continue
        # Find this IC's AVDD pin AND its digital VDD pin (any matching).
        avdd_pins = [(p, _net_of_pin(ctx, ref, str(p["number"]))) for p in pins
                     if (p.get("name") or "").lower() in AVDD_PIN_NAMES]
        vdd_pins = [(p, _net_of_pin(ctx, ref, str(p["number"]))) for p in pins
                    if (p.get("name") or "").lower() in ("vdd", "vcc", "vddio", "dvdd", "iovdd")]
        if not avdd_pins or not vdd_pins:
            continue
        vdd_nets = {n["name"] for _p, n in vdd_pins if n is not None}
        for p, net in avdd_pins:
            if net is None:
                continue
            # Shared net with digital VDD AND no isolation part on net?
            if net["name"] in vdd_nets and not _net_has_isolation_part(net, ctx.components):
                findings.append(Finding(
                    rule_id="POWER_007",
                    severity="high",
                    message=f"{ref} AVDD pin {p['number']} ({p.get('name')}) is tied directly "
                            f"to digital VDD net '{net['name']}' with no ferrite-bead isolation",
                    refs=[ref],
                    extra={"ic_ref": ref, "pin_number": str(p["number"]),
                           "anchor": (p["x"], p["y"])},
                ))
    return findings


def fix_POWER_007(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Rewiring AVDD through a new ferrite bead means splitting an existing
    # net + reassigning every pin on the AVDD side — risky to do blindly.
    # Surface only; let the bridge layer do the rewrite.
    return []


# ---------------------------------------------------------------------------
# PROT_006 — Polarized cap orientation: '+' terminal must be on the higher-
# potential net (positive rail), '-' on GND. Reversed = bulging cap.
# ---------------------------------------------------------------------------

POLARIZED_CAP_LIB_HINTS = ("device:cp", "device:c_polarized", "capacitor:cp",
                           "_polarized", "tantalum", "_electrolytic")


def _is_polarized_cap(c: Dict[str, Any]) -> bool:
    lib = (c.get("lib_id") or "").lower()
    if any(h in lib for h in POLARIZED_CAP_LIB_HINTS):
        return True
    val = (c.get("value") or "").lower()
    if "tant" in val or "elec" in val:
        return True
    return False


def detect_PROT_006(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        if not _is_polarized_cap(c):
            continue
        ref = c.get("reference", "") or ""
        # Device:CP convention: pin 1 = anode (+), pin 2 = cathode (-).
        net_plus  = _net_of_pin(ctx, ref, "1")
        net_minus = _net_of_pin(ctx, ref, "2")
        if net_plus is None or net_minus is None:
            continue
        plus_is_rail  = _net_ties_to_rail(net_plus,  POSITIVE_RAIL_HINTS)
        plus_is_gnd   = _net_ties_to_rail(net_plus,  NEGATIVE_RAIL_HINTS)
        minus_is_rail = _net_ties_to_rail(net_minus, POSITIVE_RAIL_HINTS)
        minus_is_gnd  = _net_ties_to_rail(net_minus, NEGATIVE_RAIL_HINTS)
        # Reversed iff '+' touches GND OR '-' touches a positive rail.
        if plus_is_gnd or minus_is_rail:
            findings.append(Finding(
                rule_id="PROT_006",
                severity="medium",
                message=f"polarized cap {ref}: '+' pin on '{net_plus['name']}', "
                        f"'-' pin on '{net_minus['name']}' — orientation reversed "
                        f"(rotate symbol 180°)",
                refs=[ref],
                extra={"cap_ref": ref},
            ))
            continue
        # Either correct (plus->rail, minus->gnd) or ambiguous (neither pin on
        # an explicit power port). Don't flag the ambiguous case.
    return findings


def fix_PROT_006(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # No edit_rotation op exists in schematic_modifier; flag-only.
    return []


# ===========================================================================
# 2026-05-17 VISUAL-LAYOUT ADDITIONS — synthesized from 10 reference designs
# measured for neat-design quantitative metrics. The user asked specifically
# how professional schematics look so neat; these rules close the gap.
# ===========================================================================

# ---------------------------------------------------------------------------
# LAY_027 — all labels horizontal (angle == 0 or 180; never 90 or 270)
# Across 10 measured reference designs (ST NUCLEO, Pico, Feather, Arduino UNO,
# ESP32-DevKitC, Teensy 4.0, nRF52840-DK, MSP-EXP430G2, Pro Micro,
# ESP32-S3-DevKitC) ALL labels were 0° or 180°. Vertical labels are an
# instant visual tell of AI-generated schematics.
# ---------------------------------------------------------------------------

def _iter_label_nodes(doc: SchematicDocument):
    """Yield every (kind, node) tuple from the schematic where kind is one
    of label / global_label / hierarchical_label."""
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and len(child) >= 1):
            continue
        head = child[0]
        head_str = head.value() if hasattr(head, "value") else str(head)
        if head_str in ("label", "global_label", "hierarchical_label"):
            yield head_str, child


def _label_at_and_text(node: list) -> Tuple[Optional[Tuple[float, float, float]], Optional[str]]:
    """Return ((x, y, angle), text) for a label node — or (None, None)."""
    text = None
    at = None
    if len(node) >= 2 and isinstance(node[1], str):
        text = node[1]
    for sub in node[2:]:
        if isinstance(sub, list) and len(sub) >= 4:
            sub_head = sub[0]
            sub_head_str = sub_head.value() if hasattr(sub_head, "value") else str(sub_head)
            if sub_head_str == "at":
                try:
                    at = (float(sub[1]), float(sub[2]), float(sub[3]))
                except (TypeError, ValueError):
                    pass
    return at, text


def detect_LAY_027(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for _kind, node in _iter_label_nodes(ctx.doc):
        at, text = _label_at_and_text(node)
        if at is None:
            continue
        angle = at[2] % 360.0
        if angle in (0.0, 180.0):
            continue
        findings.append(Finding(
            rule_id="LAY_027",
            severity="medium",
            message=f"label '{text}' at ({at[0]:.2f},{at[1]:.2f}) is rotated {angle:.0f}° — "
                    f"all labels must be horizontal (0° or 180°)",
            refs=[text or ""],
            extra={"x": at[0], "y": at[1], "angle": angle, "text": text or ""},
        ))
    return findings


def fix_LAY_027(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # No edit_label op exists in schematic_modifier yet. Detect-only.
    # Future: add `edit_label_angle` op that snaps angle to the nearest of
    # 0 or 180, preserving the label's pin-touching geometry.
    return []


# ---------------------------------------------------------------------------
# LAY_028 — no 4-way wire intersections (always stagger as two T-junctions
# offset by one grid). Every reference design surveyed had ZERO 4-way
# junctions. Detection: for each junction, count how many wire segments
# touch its coordinate; flag if 4+.
# ---------------------------------------------------------------------------

def _iter_junction_xys(doc: SchematicDocument):
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and len(child) >= 1):
            continue
        head = child[0]
        head_str = head.value() if hasattr(head, "value") else str(head)
        if head_str != "junction":
            continue
        for sub in child[1:]:
            if isinstance(sub, list) and len(sub) >= 3:
                sub_head = sub[0]
                sub_head_str = sub_head.value() if hasattr(sub_head, "value") else str(sub_head)
                if sub_head_str == "at":
                    try:
                        yield (float(sub[1]), float(sub[2]))
                    except (TypeError, ValueError):
                        pass


def _iter_wire_segments(doc: SchematicDocument):
    """Yield (x1, y1, x2, y2) for every wire segment."""
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and len(child) >= 1):
            continue
        head = child[0]
        head_str = head.value() if hasattr(head, "value") else str(head)
        if head_str != "wire":
            continue
        for sub in child[1:]:
            if isinstance(sub, list) and len(sub) >= 1:
                sub_head = sub[0]
                sub_head_str = sub_head.value() if hasattr(sub_head, "value") else str(sub_head)
                if sub_head_str == "pts":
                    pts = []
                    for pt in sub[1:]:
                        if isinstance(pt, list) and len(pt) >= 3:
                            pt_head = pt[0]
                            pt_head_str = pt_head.value() if hasattr(pt_head, "value") else str(pt_head)
                            if pt_head_str == "xy":
                                try:
                                    pts.append((float(pt[1]), float(pt[2])))
                                except (TypeError, ValueError):
                                    pass
                    for i in range(len(pts) - 1):
                        yield (pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])


def _segment_touches_point(x1, y1, x2, y2, px, py, tol=0.05) -> bool:
    """True if the axis-aligned segment from (x1,y1) to (x2,y2) passes through
    or terminates at (px, py) within tolerance."""
    if abs(y1 - y2) <= tol:  # horizontal
        return abs(py - y1) <= tol and (min(x1, x2) - tol) <= px <= (max(x1, x2) + tol)
    if abs(x1 - x2) <= tol:  # vertical
        return abs(px - x1) <= tol and (min(y1, y2) - tol) <= py <= (max(y1, y2) + tol)
    return False


def detect_LAY_028(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    segments = list(_iter_wire_segments(ctx.doc))
    for jx, jy in _iter_junction_xys(ctx.doc):
        # Count how many distinct segment-arms terminate AT or pass THROUGH this junction.
        arms = 0
        for x1, y1, x2, y2 in segments:
            if _segment_touches_point(x1, y1, x2, y2, jx, jy):
                # Pass-through counts as 2 arms (both directions); endpoint counts as 1.
                if (abs(x1 - jx) <= 0.05 and abs(y1 - jy) <= 0.05) or \
                   (abs(x2 - jx) <= 0.05 and abs(y2 - jy) <= 0.05):
                    arms += 1
                else:
                    arms += 2
        if arms >= 4:
            findings.append(Finding(
                rule_id="LAY_028",
                severity="high",
                message=f"4-way junction at ({jx:.2f},{jy:.2f}) — stagger as two "
                        f"T-junctions offset by one grid",
                refs=[],
                extra={"x": jx, "y": jy, "arms": arms},
            ))
    return findings


def fix_LAY_028(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Splitting a 4-way junction into two T-junctions requires rerouting one of
    # the four wire arms by 1 grid — risky without knowing which arm is the
    # "least important" to bend. Detect-only; surface to LLM / user.
    return []


# ---------------------------------------------------------------------------
# LAY_031 — 50-mil grid compliance. Every reference design uses 1.27 mm
# (50 mil) for all symbols and wires. Off-grid pins/wire-ends cause silent
# unconnected nets (GRD_002 prose rule).
# ---------------------------------------------------------------------------

def detect_LAY_031(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []

    def off(v: float) -> bool:
        return abs(round(v / GRID_MM) * GRID_MM - v) > 0.001

    # Component positions
    for c in ctx.components:
        at = c.get("at") or []
        if len(at) < 2:
            continue
        try:
            x, y = float(at[0]), float(at[1])
        except (TypeError, ValueError):
            continue
        if off(x) or off(y):
            findings.append(Finding(
                rule_id="LAY_031",
                severity="medium",
                message=f"{c.get('reference', '?')} off 50-mil grid at ({x:.4f}, {y:.4f})",
                refs=[c.get("reference", "")],
                extra={"kind": "component", "ref": c.get("reference", ""),
                       "x": x, "y": y},
            ))

    # Wire endpoints
    for x1, y1, x2, y2 in _iter_wire_segments(ctx.doc):
        for (px, py, which) in ((x1, y1, "start"), (x2, y2, "end")):
            if off(px) or off(py):
                findings.append(Finding(
                    rule_id="LAY_031",
                    severity="medium",
                    message=f"wire {which} off 50-mil grid at ({px:.4f}, {py:.4f})",
                    refs=[],
                    extra={"kind": "wire_endpoint", "x": px, "y": py},
                ))
    return findings


def fix_LAY_031(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Auto-snap only when it's safe (component move). Snapping wire endpoints
    # requires move_wire_endpoint which can desync from the pin it lands on;
    # safer to flag the wire-endpoint case and only auto-fix components.
    if f.extra.get("kind") != "component":
        return []
    ref = f.extra["ref"]
    x = _snap(f.extra["x"])
    y = _snap(f.extra["y"])
    return [{"op": "move_component", "reference": ref, "x": x, "y": y}]


# ---------------------------------------------------------------------------
# LAY_032 — Hide visible power-port (#PWRxx / #FLGxx) refdes + value text.
# The KiCad library ships those properties hidden; if they're visible the
# user / AI un-hid them and the schematic carries page-wide #PWR03 / PWR_FLAG
# noise. Universal rule — applies to every '#'-prefixed symbol regardless of
# circuit, IC family, or block. Power-symbol prefix is read from
# conventions.json so it stays in sync with the BOM / basic-checks filters.
# ---------------------------------------------------------------------------

LAY_032_TARGET_PROPERTIES = ("Reference", "Value")


def _is_power_symbol(comp: Dict[str, Any]) -> bool:
    """Power-port symbol or PWR_FLAG marker — never a real BOM part. Recognised
    by the universal filter in conventions.json (lib_id prefix 'power:' or
    refdes prefix '#'). Mirrors basic_checks._is_ignored to keep the two
    layers in lockstep."""
    cfg = _load_config("conventions")["power_symbol"]
    lib_id = comp.get("lib_id", "") or ""
    ref = comp.get("reference", "") or ""
    if any(lib_id.startswith(p) for p in cfg["lib_id_prefixes"]):
        return True
    return any(ref.startswith(p) for p in cfg["reference_prefixes"])


def detect_LAY_032(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for c in ctx.components:
        if not _is_power_symbol(c):
            continue
        ref = c.get("reference", "") or ""
        hidden = c.get("property_hidden") or {}
        for pname in LAY_032_TARGET_PROPERTIES:
            if pname not in (c.get("properties") or {}):
                continue
            if hidden.get(pname, False):
                continue
            findings.append(Finding(
                rule_id="LAY_032",
                severity="low",
                message=f"{ref}: '{pname}' text is visible — power symbols must "
                        f"keep Reference and Value hidden (KiCad library default)",
                refs=[ref],
                extra={"ref": ref, "property": pname},
            ))
    return findings


def fix_LAY_032(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    return [{
        "op": "hide_property",
        "reference": f.extra["ref"],
        "property_name": f.extra["property"],
        "hidden": True,
    }]


# ---------------------------------------------------------------------------
# CON_003 — Missing junction dot where 3+ wire endpoints meet.
# Already detected in basic_checks; the fix here closes the loop so the auto-
# repair pipeline can plant the dot without an LLM round-trip. Universal — no
# part-type assumption; any T-intersection without a dot is a silent net split.
# ---------------------------------------------------------------------------

CON_003_TOL_MM = 0.05


def detect_CON_003(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    # Bucket every wire endpoint by snapped coord; a coord shared by 3+ distinct
    # wire indices needs a junction. (Pass-throughs and 4-way crossings are
    # LAY_028's job — CON_003 only fires when there are >=3 ENDPOINTS at the
    # same point, which is the unambiguous T-junction case.)
    tol = CON_003_TOL_MM
    endpoint_wires: Dict[Tuple[float, float], set] = {}
    for wi, (x1, y1, x2, y2) in enumerate(_iter_wire_segments(ctx.doc)):
        for (x, y) in ((x1, y1), (x2, y2)):
            kx = round(x / tol) * tol
            ky = round(y / tol) * tol
            endpoint_wires.setdefault((kx, ky), set()).add(wi)

    existing = {(round(jx / tol) * tol, round(jy / tol) * tol)
                for jx, jy in _iter_junction_xys(ctx.doc)}

    for (kx, ky), wires in endpoint_wires.items():
        if len(wires) < 3:
            continue
        if (kx, ky) in existing:
            continue
        findings.append(Finding(
            rule_id="CON_003",
            severity="critical",
            message=f"{len(wires)} wires meet at ({kx:.2f}, {ky:.2f}) without a "
                    f"junction dot — silent net split (KLC CON_003)",
            refs=[],
            extra={"x": kx, "y": ky},
        ))
    return findings


def fix_CON_003(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    return [{"op": "add_junction",
             "x": _snap(f.extra["x"]),
             "y": _snap(f.extra["y"])}]


# ---------------------------------------------------------------------------
# LAY_009 — More than one PWR_FLAG on the same supply net. KiCad ERC needs
# exactly ONE per rail (placed near the rail entry / regulator output); extras
# are visual clutter and a sign the generator dropped a flag at every branch.
# Detector mirrors basic_checks.check_excess_pwr_flag; the fix keeps the first
# PWR_FLAG on each net and deletes the rest. Gated by fixer_config.safety.
# allow_delete_component because deleting is destructive (off by default —
# leave the finding visible to the LLM and let the user/operator confirm).
# ---------------------------------------------------------------------------

LAY_009_PWR_FLAG_VALUES = {"PWR_FLAG"}


def detect_LAY_009(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for net in ctx.nets:
        flag_refs: List[str] = []
        for m in net["members"]:
            if m.get("kind") != "power":
                continue
            if (m.get("name") or "").upper() not in LAY_009_PWR_FLAG_VALUES:
                continue
            ref = m.get("ref")
            if ref:
                flag_refs.append(ref)
        if len(flag_refs) <= 1:
            continue
        # Keep the first, mark the rest for deletion. Ordering is whatever
        # order net['members'] surfaced, which is stable per build_sheet_nets.
        keep, extras = flag_refs[0], flag_refs[1:]
        findings.append(Finding(
            rule_id="LAY_009",
            severity="medium",
            message=f"{len(flag_refs)} PWR_FLAGs on net '{net['name']}' "
                    f"(keep {keep}, drop {', '.join(extras)})",
            refs=flag_refs,
            extra={"net": net["name"], "keep": keep, "extras": extras},
        ))
    return findings


def fix_LAY_009(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # delete_component is gated by fixer_config.safety.allow_delete_component;
    # if disabled the fixer will reject these ops and the finding still
    # surfaces — that is the intended behaviour. We always EMIT the op so a
    # caller that opts in (e.g. validator with safety override) can act.
    return [{"op": "delete_component", "reference": ref}
            for ref in f.extra["extras"]]


# ---------------------------------------------------------------------------
# LAY_033 — Redundant power-rail label on a net that already has a matching
# power-port symbol. The triangle/arrow IS the rail name; the text label is
# visual noise. Universal: rail names are DERIVED from each net's power
# members (no hardcoded GND/VCC/+3V3 list) so any custom rail (+12V, V_BATT,
# +VCC_SENSE, etc.) is handled identically. Detect-only for now — auto-
# deletion of labels needs a delete_label op we haven't added yet; the LLM
# repair loop / user can act on the finding.
# ---------------------------------------------------------------------------


def detect_LAY_033(ctx: RuleContext) -> List[Finding]:
    findings: List[Finding] = []
    for net in ctx.nets:
        # Collect every power-port name on this net (the canonical rail names).
        rail_names = {(m.get("name") or "").upper()
                      for m in net["members"]
                      if m.get("kind") == "power" and m.get("name")}
        # Ignore PWR_FLAG — it's a marker, not a rail name worth comparing.
        rail_names -= LAY_009_PWR_FLAG_VALUES
        if not rail_names:
            continue
        # Any text label on this net whose name matches a rail = redundant.
        for m in net["members"]:
            if m.get("kind") not in ("label", "global_label", "hierarchical_label"):
                continue
            lbl_name = (m.get("name") or "")
            if lbl_name.upper() in rail_names:
                findings.append(Finding(
                    rule_id="LAY_033",
                    severity="low",
                    message=f"label '{lbl_name}' on net '{net['name']}' duplicates "
                            f"a power-port symbol of the same name — delete the "
                            f"label, keep the symbol",
                    refs=[lbl_name],
                    extra={"label_name": lbl_name, "net": net["name"]},
                ))
    return findings


def fix_LAY_033(ctx: RuleContext, f: Finding) -> List[Dict[str, Any]]:
    # Detect-only. add a delete_label op to schematic_modifier when we want
    # auto-removal — needs (name, x, y) per label to disambiguate duplicates.
    return []


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

DETECTORS = [
    # POWER_003 runs BEFORE POWER_002 — the regulator's Cout (10u, added by
    # POWER_003) satisfies the bulk-cap requirement on the regulator-output
    # rail, so POWER_002 then correctly skips it instead of stacking a
    # second 10u right next to the first. Order is load-bearing here.
    ("POWER_001", detect_POWER_001, fix_POWER_001),
    ("POWER_003", detect_POWER_003, fix_POWER_003),
    ("POWER_009", detect_POWER_009, fix_POWER_009),
    ("POWER_002", detect_POWER_002, fix_POWER_002),
    ("POWER_005", detect_POWER_005, fix_POWER_005),
    ("POWER_007", detect_POWER_007, fix_POWER_007),
    ("POWER_010", detect_POWER_010, fix_POWER_010),
    ("POWER_011", detect_POWER_011, fix_POWER_011),
    ("POWER_012", detect_POWER_012, fix_POWER_012),
    ("OSC_001",   detect_OSC_001,   fix_OSC_001),
    ("OSC_005",   detect_OSC_005,   fix_OSC_005),
    ("RST_001",   detect_RST_001,   fix_RST_001),
    ("RST_005",   detect_RST_005,   fix_RST_005),
    ("PUL_001",   detect_PUL_001,   fix_PUL_001),
    ("PUL_007",   detect_PUL_007,   fix_PUL_007),
    ("LED_001",   detect_LED_001,   fix_LED_001),
    ("PROT_004",  detect_PROT_004,  fix_PROT_004),
    ("PROT_006",  detect_PROT_006,  fix_PROT_006),
    ("STRAP_001", detect_STRAP_001, fix_STRAP_001),
    ("CON_002",   detect_CON_002,   fix_CON_002),
    ("USB_001",   detect_USB_001,   fix_USB_001),
    ("USB_002",   detect_USB_002,   fix_USB_002),
    ("DBG_001",   detect_DBG_001,   fix_DBG_001),
    ("DBG_002",   detect_DBG_002,   fix_DBG_002),
    ("LAY_007",   detect_LAY_007,   fix_LAY_007),
    ("LAY_021",   detect_LAY_021,   fix_LAY_021),
    ("LAY_027",   detect_LAY_027,   fix_LAY_027),
    ("LAY_028",   detect_LAY_028,   fix_LAY_028),
    ("LAY_031",   detect_LAY_031,   fix_LAY_031),
    ("LAY_032",   detect_LAY_032,   fix_LAY_032),
    ("CON_003",   detect_CON_003,   fix_CON_003),
    ("LAY_009",   detect_LAY_009,   fix_LAY_009),
    ("LAY_033",   detect_LAY_033,   fix_LAY_033),
]


def detect_all(path) -> Dict[str, Any]:
    """Pure read-only pass: list every detectable rule violation."""
    ctx = build_context(path)
    findings: List[Finding] = []
    for _rid, det, _fix in DETECTORS:
        findings.extend(det(ctx))
    return {
        "path": str(ctx.path),
        "findings": [
            {"rule_id": f.rule_id, "severity": f.severity,
             "message": f.message, "refs": f.refs}
            for f in findings
        ],
        "by_rule": _by_rule(findings),
    }


def _by_rule(findings: List[Finding]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for f in findings:
        out[f.rule_id] = out.get(f.rule_id, 0) + 1
    return out


def apply_all(path, rules: Optional[List[str]] = None,
              dry_run: bool = False) -> Dict[str, Any]:
    """Detect + fix every rule we know how to fix.

    Returns a structured trace: per-rule counts of findings, the ops emitted,
    and the per-op apply result. With dry_run=True nothing is mutated.
    Detection-only rules (fix returns []) still appear in findings; they
    just contribute zero ops.
    """
    ctx = build_context(path)
    enabled = set(rules) if rules else None

    proposals: List[Tuple[Finding, List[Dict[str, Any]]]] = []
    for rule_id, det, fix in DETECTORS:
        if enabled is not None and rule_id not in enabled:
            continue
        for f in det(ctx):
            ops = fix(ctx, f)
            proposals.append((f, ops))

    apply_results: List[Dict[str, Any]] = []
    has_ops = any(ops for _, ops in proposals)
    if not dry_run and has_ops:
        for f, ops in proposals:
            for op in ops:
                res = apply_operation(ctx.doc, op)
                apply_results.append({
                    "rule": f.rule_id,
                    "op": op.get("op"),
                    "ok": res.get("ok"),
                    "message": res.get("message"),
                })
        ctx.doc.save()

    return {
        "path": str(ctx.path),
        "dry_run": dry_run,
        "findings": [
            {"rule_id": f.rule_id, "severity": f.severity, "message": f.message,
             "refs": f.refs, "op_count": len(ops)}
            for f, ops in proposals
        ],
        "apply_results": apply_results,
        "summary": {
            "total_findings": len(proposals),
            "total_ops": sum(len(ops) for _, ops in proposals),
            "applied_ok": sum(1 for r in apply_results if r["ok"]),
            "applied_fail": sum(1 for r in apply_results if not r["ok"]),
        },
    }


def to_text(report: Dict[str, Any]) -> str:
    lines = [
        f"CIRCUIT RULES {'(DRY RUN)' if report.get('dry_run') else ''} - {report['path']}",
        f"  findings: {report['summary']['total_findings']}, "
        f"ops: {report['summary']['total_ops']}, "
        f"applied ok: {report['summary']['applied_ok']}, "
        f"failed: {report['summary']['applied_fail']}",
    ]
    for f in report["findings"]:
        lines.append(f"  [{f['severity']:8s}] {f['rule_id']:10s} {', '.join(f['refs'])}: {f['message']}")
    if report["apply_results"]:
        lines.append("")
        lines.append("APPLY RESULTS:")
        for r in report["apply_results"]:
            tag = "ok" if r["ok"] else "FAIL"
            lines.append(f"  [{tag}] {r['rule']} {r['op']}: {r['message']}")
    return "\n".join(lines)
