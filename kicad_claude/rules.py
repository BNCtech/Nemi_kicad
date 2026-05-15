"""Schematic correctness rule catalog.

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
    ("PROT_004",  "PROTECTION", "MEDIUM", "Inductive load freewheel diode",      "Every relay coil, motor, solenoid driven by a transistor must have a flyback diode or RC snubber."),
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
    "FIT":         "COMPONENT FITNESS / DERATING",
    "CONN":        "CONNECTIONS / JUNCTIONS",
    "LABEL":       "LABELS / NET NAMING",
    "PLACE":       "PLACEMENT",
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
