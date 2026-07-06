"""Tool: create_symbol — research a part's pinout and write a NEW .kicad_sym.

The missing capability: when the user asks for a part that is in NO installed
symbol library, ``build_circuit`` / ``apply_ops`` cannot place it (the lib_id
fails to load and the add is refused). This tool fills that gap end-to-end.

Standards basis (Part A) — every symbol follows:
  - IEEE 315-1975 / ANSI Y32.2 "Graphic Symbols for Electrical and Electronics
    Diagrams (Including Reference Designation Letters)" — connection points on a
    modular grid; reference-designation class letters (clause 22).
  - IEEE 200 — reference designations (folded into IEEE 315 cl.22).
  - IEC 60617 / KiCad Library Convention (KLC) — the machine-readable rules:
    100 mil pin grid, pin length >=100 mil scaling +50 mil per extra pin-number
    digit (max 300 mil), 50 mil text, 10 mil body outline, 20 mil pin-name
    offset, background fill for IC black-boxes / no fill for discretes, power
    top / ground bottom / inputs left / outputs right.

Correctness basis (Part B) — grounded in 2022-2026 literature:
  - "ML-aided Schematic Symbol Generation" (MLCAD 2024) validates the four-side
    rectangular layout + pin number-name pairing + side recognition used here.
  - PCBSchemaGen (2026): multi-check verification with error localization +
    bounded self-heal retry.
  - CircuitLM (2026): retrieve-before-create (don't recreate an existing part)
    and an out-of-distribution / hallucination guard (refuse + flag rather than
    invent a pinout). TableFormer (CVPR 2022) etc. show datasheet extraction is
    error-prone, which is why the verify + confidence guard exist.

Pipeline:
  1. RETRIEVE — check the installed libraries first; if the exact part already
     exists, return it instead of writing a duplicate (CircuitLM).
  2. RESEARCH — call Claude INTERNALLY with the server-side ``web_search`` tool
     (low temperature, closed pin ontology) so it surfs the web, opens the
     official datasheet, and reads the real pin table + a confidence score.
     Falls back to model knowledge if web search is unavailable; skipped when
     the caller supplies ``pins_json`` directly.
  3. VERIFY + SELF-HEAL — score the extracted pinout (duplicate numbers, IC with
     no power pin, mostly-unnamed pins, low confidence); on failure re-research
     once with the issues fed back (PCBSchemaGen).
  4. DRAW — pins laid out to MIRROR THE DATASHEET pinout (package order, not
     regrouped by function) by default; IEEE-315/KLC grid/length/text rules;
     reference designator forced to the IEEE 315 class letter for the part's
     class. (pin_layout='functional' opts into the tidy in/out/power grouping.)
  5. WRITE — emit a KiCad ``.kicad_sym`` into the GLOBAL shared symbol library
     (the first root ``symbol_geom._sym_roots()`` scans) AND register the
     library in KiCad's global sym-lib-table so it shows in the GUI chooser.
  6. REFRESH — clear ``load_symbol`` / ``_all_symbols`` caches so the next
     ``build_circuit`` call resolves the new lib_id with no restart.

Returns the resolved ``lib_id`` (``<library>:<part>``) so the agent can pass it
straight to build_circuit / apply_ops.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool


# Avast / corporate-proxy TLS re-signing breaks the anthropic SDK's bundled
# certifi store — switch to the OS trust store, same workaround build_circuit
# and agent.py use.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# LangSmith wrapper so the internal research call shows up in the trace tree
# with tokens + cost; no-ops when langsmith isn't installed.
try:
    from langsmith import traceable
    from langsmith.wrappers import wrap_anthropic
except ImportError:
    def traceable(*args, **kwargs):  # type: ignore
        def _wrap(fn):
            return fn
        return _wrap if not args else args[0]

    def wrap_anthropic(client, **kwargs):  # type: ignore
        return client


# ---------------------------------------------------------------------------
# Geometry constants — all KiCad symbol coords are local, Y-UP, in mm.
# ---------------------------------------------------------------------------

GRID = 1.27          # 50 mil — every coord must land on this grid
PITCH = 2.54         # 100 mil — pin-to-pin spacing
PIN_LEN = 2.54       # 100 mil — pin line length (anchor -> body edge)
TEXT = 1.27          # 50 mil — name / number text size
SYM_VERSION = 20251024   # matches the installed KiCad-9 symbol libraries

# Valid KiCad pin electrical types (eeschema/sch_pin.cpp).
_VALID_ETYPES = {
    "input", "output", "bidirectional", "tri_state", "passive", "free",
    "unspecified", "power_in", "power_out", "open_collector",
    "open_emitter", "no_connect",
}

# Name tokens that mark a pin as a GROUND rail (-> bottom of the body).
_GND_TOKENS = ("GND", "VSS", "VEE", "AGND", "DGND", "PGND", "GROUND", "VSSA")
# Name tokens that mark a pin as a SUPPLY rail (-> top of the body).
_VCC_TOKENS = ("VCC", "VDD", "VIN", "VBAT", "VSYS", "AVDD", "DVDD", "VDDA",
               "VDDIO", "V+", "VS", "VPP", "VREF", "VBUS", "VVCC")

PIN_LEN_MAX = 7.62   # 300 mil — KLC pin-length ceiling
PIN_NAME_OFFSET = 0.508   # 20 mil — KLC preferred pin-name offset

# IEEE 315 (cl.22) / IEEE 200 reference-designation class letters. Maps a
# component CLASS to its standard refdes prefix. The agent / research often
# returns a generic "U"; we re-derive the correct letter from the part name +
# description so the symbol's Reference field is standards-correct.
_IEEE315_VALID_PREFIXES = {
    "R", "C", "U", "Q", "D", "J", "P", "L", "Y", "X", "K", "F", "FB", "SW",
    "S", "T", "BT", "M", "DS", "TP", "AE", "BR", "FL", "DL", "CB", "RV", "RN",
    "CN", "MK", "LS", "HS",
}

# Keyword -> IEEE 315 prefix, checked in order (most specific first). Tokens are
# matched against the lower-cased "<name> <description>" text.
_IEEE315_KEYWORD_PREFIX = [
    (("crystal", "oscillator", "resonator", "xtal", "ceramic resonator"), "Y"),
    (("ferrite bead", "ferrite"), "FB"),
    (("inductor", "choke", " coil"), "L"),
    (("relay", "contactor"), "K"),
    (("fuse", "polyfuse", "resettable fuse"), "F"),
    (("bridge rectifier",), "BR"),
    (("transformer",), "T"),
    (("varistor", "mov"), "RV"),
    (("resistor network", "resistor array"), "RN"),
    (("capacitor network", "capacitor array"), "CN"),
    (("battery", "coin cell", "cell holder"), "BT"),
    (("motor", "servo", "stepper"), "M"),
    (("speaker", "buzzer", "loudspeaker"), "LS"),
    (("microphone",), "MK"),
    (("antenna",), "AE"),
    (("test point",), "TP"),
    (("fiducial",), "FD"),
    (("filter",), "FL"),
    (("delay line",), "DL"),
    (("circuit breaker",), "CB"),
    (("display", "lcd", "oled", "seven segment", "7 segment", "lamp"), "DS"),
    (("switch", "button", "pushbutton", "push button", "dip switch",
      "encoder switch", "tactile"), "SW"),
    (("led", "light emitting", "diode", "zener", "schottky", "rectifier",
      "tvs", "varactor"), "D"),
    (("transistor", "mosfet", "bjt", " fet", "igbt", "jfet"), "Q"),
    (("connector", "header", "receptacle", "jack", "usb", "terminal block",
      "socket", "plug", "rj45", "molex", "jst"), "J"),
    (("resistor", "potentiometer", "rheostat", "thermistor", "shunt"), "R"),
    (("capacitor", "supercap"), "C"),
    # Active silicon / modules default to U (IC).
    (("regulator", "ldo", "ic", "microcontroller", "mcu", "amplifier",
      "op-amp", "opamp", "gate", "driver", "controller", "sensor", "adc",
      "dac", "transceiver", "memory", "eeprom", "flash", "mux", "comparator",
      "timer", "processor", "module", "converter", "monitor", "expander",
      "multiplexer", "logic", "buffer", "shift register"), "U"),
]


# Classes that are physically 2-3 terminal discretes. A keyword hit for one of
# these on a part with MANY pins is almost always a false positive — e.g. an ADC
# IC whose description mentions an internal "oscillator" must NOT become a
# crystal (Y). So these only win when the pin count is small.
_FEW_PIN_CLASSES = {"Y", "R", "C", "L", "D", "FB", "F", "BT", "RV", "DL", "MK"}


def _ieee315_refdes(research_ref: str, name: str, description: str,
                    pin_count: int = 0) -> str:
    """Return the IEEE-315 reference-designator prefix for the part. Keyword
    inference from the part name/description wins (most specific); discrete
    2-3-terminal classes are rejected when the part has many pins (so an IC that
    merely mentions 'oscillator'/'comparator' in its description isn't misread
    as a crystal); else a valid research-supplied prefix; else 'U'."""
    text = f"{name} {description}".lower()
    for tokens, prefix in _IEEE315_KEYWORD_PREFIX:
        if any(tok in text for tok in tokens):
            # Skip a discrete-class hit on a clearly multi-pin part and keep
            # scanning for a better (IC/connector/etc.) match.
            if prefix in _FEW_PIN_CLASSES and pin_count > 4:
                continue
            return prefix
    rr = (research_ref or "").strip().upper()
    # Keep only the leading alpha part (strip any digits the model appended).
    m = re.match(r"^[A-Z]{1,3}", rr)
    if m and m.group(0) in _IEEE315_VALID_PREFIXES:
        if not (m.group(0) in _FEW_PIN_CLASSES and pin_count > 4):
            return m.group(0)
    return "U"


def _pin_length_for(pins: List[dict]) -> float:
    """KLC pin length: 100 mil for <=2-char pin numbers, +50 mil per extra
    character (so BGA pads like 'A12' get 150 mil and longer numbers don't
    collide), capped at 300 mil. Uniform across the symbol so pins align."""
    max_digits = max((len(str(p.get("number", ""))) for p in pins), default=2)
    extra = max(0, max_digits - 2)
    return min(PITCH + extra * GRID, PIN_LEN_MAX)


def _grid_ceil(v: float) -> float:
    """Round a positive length UP to the next 1.27 mm grid step."""
    return math.ceil(round(v / GRID, 6)) * GRID


def _esc(s: str) -> str:
    """Escape a string for a KiCad double-quoted s-expr atom."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _safe_part(name: str) -> str:
    """Filesystem-safe part / file name (the symbol's own name keeps the
    user's spelling; this is only for the .kicad_sym filename)."""
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", name).strip("_") or "Symbol"


# ---------------------------------------------------------------------------
# Pin normalisation
# ---------------------------------------------------------------------------

def _norm_etype(raw: str) -> str:
    """Map a free-text datasheet pin type onto a valid KiCad etype. Defaults
    to ``passive`` (the ERC-safe choice) when nothing matches."""
    t = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if t in _VALID_ETYPES:
        return t
    table = {
        "power": "power_in", "supply": "power_in", "vcc": "power_in",
        "vdd": "power_in", "gnd": "power_in", "ground": "power_in",
        "in": "input", "i": "input", "din": "input",
        "out": "output", "o": "output", "dout": "output",
        "io": "bidirectional", "i/o": "bidirectional", "i_o": "bidirectional",
        "inout": "bidirectional", "bidir": "bidirectional", "gpio": "bidirectional",
        "analog": "passive", "sense": "passive", "nc": "no_connect",
        "no_connect": "no_connect", "open_drain": "open_collector",
        "od": "open_collector", "tristate": "tri_state", "3state": "tri_state",
    }
    return table.get(t, "passive")


def _heuristic_side(name: str, etype: str) -> str:
    """Pick a body side for a pin that didn't carry an explicit ``side``."""
    up = (name or "").upper()
    if etype == "power_in" and any(tok in up for tok in _GND_TOKENS):
        return "bottom"
    if etype in ("power_in", "power_out") and any(tok in up for tok in _VCC_TOKENS):
        return "top"
    if etype == "output":
        return "right"
    if etype in ("input", "bidirectional", "tri_state"):
        return "left"
    return "left"


def _norm_pin(raw: dict) -> Optional[dict]:
    """Validate + normalise ONE pin dict from research / caller input.
    Returns None when there's no usable number."""
    number = str(raw.get("number", raw.get("pin", raw.get("num", "")))).strip()
    if not number:
        return None
    name = str(raw.get("name", raw.get("function", "~"))).strip() or "~"
    name = re.sub(r"\s+", "_", name)
    etype = _norm_etype(str(raw.get("type", raw.get("etype", ""))))
    side = str(raw.get("side", "")).strip().lower()
    if side not in ("left", "right", "top", "bottom"):
        side = ""   # filled later by _assign_sides per the chosen layout mode
    return {"number": number, "name": name, "etype": etype, "side": side}


def _assign_sides(pins: List[dict], mode: str) -> List[dict]:
    """Decide which body side each pin sits on, per the layout mode.

    "datasheet" (default) — draw the symbol AS IN THE DATASHEET pinout: keep
        the package pin order, do NOT regroup by function. If the research
        already tagged every pin with a physical side, honour it. Otherwise
        fall back to the standard dual-row IC convention (DIP/SOIC/SOP): first
        half of the pins down the LEFT (top->bottom), second half up the RIGHT
        (so pin numbers run counter-clockwise exactly like the package
        drawing).
    "functional" — the classic EDA tidy-up: inputs left, outputs right,
        supplies top, grounds bottom (the behaviour the user did NOT want by
        default, kept as an explicit opt-in)."""
    valid = ("left", "right", "top", "bottom")
    if mode == "functional":
        for p in pins:
            if p.get("side") not in valid:
                p["side"] = _heuristic_side(p["name"], p["etype"])
        return pins
    # datasheet mode --------------------------------------------------------
    if pins and all(p.get("side") in valid for p in pins):
        return pins   # research gave a full physical side map — trust it
    n = len(pins)
    if n <= 2:
        for i, p in enumerate(pins):
            p["side"] = "left" if i == 0 else "right"
        return pins
    half = (n + 1) // 2
    left, right = pins[:half], pins[half:]
    for p in left:
        p["side"] = "left"
    for p in right:
        p["side"] = "right"
    # Right column is emitted top->bottom in list order; reverse it so the
    # pin numbers ascend bottom->top (DIP/SOIC convention from the datasheet).
    return left + list(reversed(right))


# ---------------------------------------------------------------------------
# Layout — place pins on the four sides, size the body
# ---------------------------------------------------------------------------

def _layout(pins: List[dict], pin_len: float) -> Tuple[float, float, List[dict]]:
    """Assign every pin an (x, y, rot, length) and return (half_w, half_h,
    placed). ``pin_len`` is the IEEE/KLC-derived uniform pin length.

    Sides:  left rot=0 (line -> +x into body), right rot=180,
            top rot=270 (line -> -y down into body), bottom rot=90.
    """
    by = {"left": [], "right": [], "top": [], "bottom": []}
    for p in pins:
        by[p["side"]].append(p)

    n_v = max(len(by["left"]), len(by["right"]), 1)
    n_h = max(len(by["top"]), len(by["bottom"]), 1)

    # Half-height fits the taller of the two vertical columns; half-width
    # fits the wider of the two horizontal rows AND the longest pin names.
    half_h = _grid_ceil((n_v - 1) / 2 * PITCH + PITCH)
    name_left = max((len(p["name"]) for p in by["left"]), default=0)
    name_right = max((len(p["name"]) for p in by["right"]), default=0)
    half_w_name = (name_left + name_right) * TEXT / 2 + PITCH
    half_w_pins = (n_h - 1) / 2 * PITCH + PITCH
    half_w = _grid_ceil(max(half_w_name, half_w_pins, 5.08))

    placed: List[dict] = []

    def _col(side_pins, x, rot):
        n = len(side_pins)
        top = (n - 1) / 2 * PITCH
        for i, p in enumerate(side_pins):
            q = dict(p)
            q["x"] = x
            q["y"] = top - i * PITCH
            q["rot"] = rot
            q["length"] = pin_len
            placed.append(q)

    def _row(side_pins, y, rot):
        n = len(side_pins)
        left = -(n - 1) / 2 * PITCH
        for i, p in enumerate(side_pins):
            q = dict(p)
            q["x"] = left + i * PITCH
            q["y"] = y
            q["rot"] = rot
            q["length"] = pin_len
            placed.append(q)

    _col(by["left"], -(half_w + pin_len), 0)
    _col(by["right"], half_w + pin_len, 180)
    _row(by["top"], half_h + pin_len, 270)
    _row(by["bottom"], -(half_h + pin_len), 90)
    return half_w, half_h, placed


# ---------------------------------------------------------------------------
# Serialiser — emit KiCad-9 .kicad_sym text
# ---------------------------------------------------------------------------

def _num(v: float) -> str:
    """Format a coord: integers stay integral, else trim trailing zeros."""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _prop(key: str, value: str, x: float, y: float, hide: bool,
          justify: str = "") -> str:
    j = f"\n\t\t\t\t(justify {justify})" if justify else ""
    h = "\n\t\t\t(hide yes)" if hide else ""
    return (
        f'\t\t(property "{_esc(key)}" "{_esc(value)}"\n'
        f"\t\t\t(at {_num(x)} {_num(y)} 0)\n"
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size {_num(TEXT)} {_num(TEXT)})\n\t\t\t\t)"
        f"{j}\n\t\t\t)"
        f"{h}\n\t\t)\n"
    )


def _pin(p: dict) -> str:
    return (
        f'\t\t\t(pin {p["etype"]} line\n'
        f'\t\t\t\t(at {_num(p["x"])} {_num(p["y"])} {_num(p["rot"])})\n'
        f"\t\t\t\t(length {_num(p.get('length', PIN_LEN))})\n"
        f'\t\t\t\t(name "{_esc(p["name"])}"\n'
        f"\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size {_num(TEXT)} {_num(TEXT)})\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n"
        f"\t\t\t\t)\n"
        f'\t\t\t\t(number "{_esc(p["number"])}"\n'
        f"\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size {_num(TEXT)} {_num(TEXT)})\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t)\n"
    )


def _build_kicad_sym(name: str, reference: str, description: str,
                     datasheet: str, footprint: str, half_w: float,
                     half_h: float, placed: List[dict]) -> str:
    """Assemble the full single-symbol library file text."""
    out = []
    out.append("(kicad_symbol_lib")
    out.append(f"\t(version {SYM_VERSION})")
    out.append('\t(generator "envil_create_symbol")')
    out.append('\t(generator_version "10.0")')
    out.append(f'\t(symbol "{_esc(name)}"')
    out.append("\t\t(pin_numbers\n\t\t\t(hide no)\n\t\t)")
    out.append(f"\t\t(pin_names\n\t\t\t(offset {_num(PIN_NAME_OFFSET)})\n\t\t)")
    out.append("\t\t(exclude_from_sim no)")
    out.append("\t\t(in_bom yes)")
    out.append("\t\t(on_board yes)")
    # Properties (Reference + Value visible, the rest hidden). Push the
    # labels clear of any TOP / BOTTOM pin tips so text doesn't sit on a pin.
    top_ext = max([half_h] + [p["y"] for p in placed if p["rot"] == 270])
    bot_ext = min([-half_h] + [p["y"] for p in placed if p["rot"] == 90])
    ref_y = _grid_ceil(top_ext) + PITCH
    val_y = -(_grid_ceil(abs(bot_ext)) + PITCH)
    body = ""
    body += _prop("Reference", reference, 0, ref_y, False)
    body += _prop("Value", name, 0, val_y, False)
    body += _prop("Footprint", footprint or "", 0, 0, True)
    body += _prop("Datasheet", datasheet or "", 0, 0, True)
    body += _prop("Description", description or "", 0, 0, True)
    out.append(body.rstrip("\n"))
    # Body graphics unit: "<name>_0_1".
    out.append(f'\t\t(symbol "{_esc(name)}_0_1"')
    out.append("\t\t\t(rectangle")
    out.append(f"\t\t\t\t(start {_num(-half_w)} {_num(half_h)})")
    out.append(f"\t\t\t\t(end {_num(half_w)} {_num(-half_h)})")
    out.append("\t\t\t\t(stroke\n\t\t\t\t\t(width 0.254)\n\t\t\t\t\t(type default)\n\t\t\t\t)")
    # KLC fill rule: IC black-boxes filled with background colour; simple
    # discrete parts (<=2 pins) left unfilled.
    fill_type = "none" if len(placed) <= 2 else "background"
    out.append(f"\t\t\t\t(fill\n\t\t\t\t\t(type {fill_type})\n\t\t\t\t)")
    out.append("\t\t\t)")
    out.append("\t\t)")
    # Pin unit: "<name>_1_1".
    out.append(f'\t\t(symbol "{_esc(name)}_1_1"')
    pins_txt = "".join(_pin(p) for p in placed).rstrip("\n")
    if pins_txt:
        out.append(pins_txt)
    out.append("\t\t)")
    out.append("\t\t(embedded_fonts no)")
    out.append("\t)")
    out.append(")")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Research — internal Claude call with server-side web search
# ---------------------------------------------------------------------------

_RESEARCH_SYSTEM = (
    "You are a KiCad component librarian. Given a part number, find its "
    "OFFICIAL datasheet (manufacturer PDF preferred) and extract the COMPLETE, "
    "EXACT pin list. Use web search to confirm the real pinout — never guess "
    "pin counts or names. Report EVERY pin of the main package.\n\n"
    "Return ONLY a single JSON object (optionally inside a ```json fence), no "
    "prose before or after, with this exact shape:\n"
    "{\n"
    '  "symbol_name": "<canonical part name, e.g. BQ76952>",\n'
    '  "reference": "<refdes prefix: U for ICs, Q transistor, D diode, '
    'J connector, ...>",\n'
    '  "description": "<one short line>",\n'
    '  "datasheet": "<datasheet URL>",\n'
    '  "footprint": "<KiCad footprint lib_id for the package, e.g. '
    'Package_TO_SOT_THT:TO-220-3_Vertical; \\"\\" if unsure>",\n'
    '  "confidence": <0.0-1.0: how sure you are the pinout is COMPLETE and '
    'CORRECT, grounded in the real datasheet (1.0 = read the official '
    'datasheet pin table; <0.5 = guessing / could not confirm)>,\n'
    '  "pins": [\n'
    '    {"number": "1", "name": "VCC", "type": "power_in", "side": "top"},\n'
    '    {"number": "2", "name": "GND", "type": "power_in", "side": "bottom"}\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "- type is one of: input, output, bidirectional, tri_state, passive, "
    "power_in, power_out, open_collector, open_emitter, no_connect, unspecified. "
    "Treat GND/VSS and VCC/VDD/supply pins as power_in.\n"
    "- side is one of left, right, top, bottom and MUST match where the pin "
    "physically sits in the datasheet's pinout diagram — preserve the package "
    "pin order, do NOT regroup pins by function. For a dual-row package "
    "(DIP/SOIC/SOP/TSSOP) put the first half of the pin numbers on the left "
    "(top->bottom) and the rest on the right; for a quad package (QFP/QFN) "
    "use all four sides counter-clockwise from pin 1; for single-row use one "
    "side. List pins in ascending package pin-number order.\n"
    "- number is the package pin number as a string (keep BGA names like 'A1').\n"
    "- Set confidence HONESTLY: only >=0.8 when you actually confirmed the pinout "
    "from the manufacturer datasheet. If you could not find/read it, set <0.5 "
    "rather than inventing pins.\n"
    "- Include ALL pins; do not truncate."
)


@traceable(run_type="llm", name="Research symbol")
def _research_part(part_number: str, datasheet_url: str, hint: str,
                   feedback: str = "") -> dict:
    """Call Claude with web search to extract the pinout. Returns the parsed
    JSON dict. Raises on hard failure (no JSON / no pins).

    Low temperature + closed pin ontology (PCBSchemaGen-style structured
    grounding). ``feedback`` is the verifier's issue list on a self-heal retry,
    prepended so the model fixes the specific problems."""
    import anthropic

    client = wrap_anthropic(anthropic.Anthropic(), chat_name="Claude")
    model = (os.environ.get("CLAUDE_MODEL_DEEP")
             or os.environ.get("ENVIL_MODEL")
             or "claude-sonnet-4-6")
    user = ""
    if feedback:
        user += ("Your previous extraction had these problems — FIX them and "
                 "re-extract from the datasheet:\n" + feedback + "\n\n")
    user += f"Part number: {part_number}\n"
    if datasheet_url:
        user += f"Datasheet URL (read this first): {datasheet_url}\n"
    if hint:
        user += f"Extra context: {hint}\n"
    user += "\nResearch this part and return the JSON pin list."

    web_tool = [{"type": "web_search_20250305", "name": "web_search",
                 "max_uses": 6}]
    resp = None
    try:
        resp = client.messages.create(
            model=model, max_tokens=8000, system=_RESEARCH_SYSTEM,
            tools=web_tool, temperature=0,
            messages=[{"role": "user", "content": user}],
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001 — web search may be unavailable
        print(f"[create_symbol] web_search unavailable, using model "
              f"knowledge: {type(exc).__name__}: {exc}", flush=True)
        resp = client.messages.create(
            model=model, max_tokens=8000, system=_RESEARCH_SYSTEM,
            temperature=0,
            messages=[{"role": "user", "content": user}],
            timeout=120,
        )
    text = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", None) == "text")
    return _extract_json(text)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of model text (handles ```json fences
    and surrounding prose)."""
    if not text:
        raise ValueError("research returned empty text")
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = m.group(1) if m else None
    if blob is None:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in research output")
        blob = text[start:end + 1]
    return json.loads(blob)


# ---------------------------------------------------------------------------
# Library target + cache refresh
# ---------------------------------------------------------------------------

def _target_root() -> Path:
    """The GLOBAL symbol-library root to write into — the first existing root
    symbol_geom scans (so build_circuit's resolver sees it immediately).
    Falls back to the configured sym_lib_dir, creating it if needed."""
    try:
        from ..kicad.symbol_geom import _sym_roots
        roots = _sym_roots()
        if roots:
            return roots[0]
    except Exception:
        pass
    from ..settings import sym_lib_dir
    root = sym_lib_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _refresh_caches() -> None:
    """Drop the lru_caches so the new symbol resolves without a restart.

    All THREE resolution caches must be cleared, not just load_symbol:
    ``_all_symbols`` is the flat index the value-keyed resolver scans, and
    ``resolve_lib_id_by_value`` is itself memoized (maxsize=2048) — and the
    RETRIEVE step above already called it for this part and cached a
    '(None) not found' miss. Without clearing it, the architect's value-keyed
    lookup keeps returning that stale miss even though the symbol now exists,
    so the just-created part still looks missing on the next build."""
    try:
        from ..kicad import symbol_geom as sg
        for fn in ("load_symbol", "_all_symbols", "resolve_lib_id_by_value",
                   "_lib_nick_map"):
            obj = getattr(sg, fn, None)
            if obj is not None and hasattr(obj, "cache_clear"):
                obj.cache_clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Footprint resolution — attach a REAL, existing footprint (the physical match)
#
# The model reliably knows the PACKAGE (VSSOP-10, TO-220-3, SOIC-8) but often
# emits a footprint lib_id that does not exist verbatim (e.g.
# "Package_SO:VSSOP-10_3.0x3.0mm_P0.5mm" when the real one is
# "Package_SO:MSOP-10_3x3mm_P0.5mm"). Attaching a non-existent name gives KiCad's
# "Footprint not found". So we VERIFY the proposed footprint against the installed
# libraries and, if it is missing, fuzzy-match to the closest real one —
# preferring a candidate whose PAD COUNT equals the symbol's pin count (so we
# never grab a thermal-pad variant like HVSSOP-10-1EP for a 10-pin part).
# ---------------------------------------------------------------------------

def _fp_roots() -> List[Path]:
    """Footprint-library roots (folders that contain *.pretty dirs). Derived
    from $KICAD_FOOTPRINT_DIR, the sibling 'footprints' of each symbol root,
    and the configured fp_lib_dir — whichever exist."""
    out: List[Path] = []
    env = os.environ.get("KICAD_FOOTPRINT_DIR", "")
    out += [Path(p) for p in env.split(os.pathsep) if p.strip()]
    try:
        from ..kicad.symbol_geom import _sym_roots
        for sr in _sym_roots():
            out.append(sr.parent / "footprints")
    except Exception:
        pass
    try:
        from ..settings import fp_lib_dir
        out.append(fp_lib_dir())
    except Exception:
        pass
    seen, roots = set(), []
    for p in out:
        rp = str(p)
        if rp not in seen and p.exists():
            seen.add(rp)
            roots.append(p)
    return roots


@lru_cache(maxsize=1)
def _all_footprints() -> List[Tuple[str, str, Path]]:
    """Index every installed footprint as (lib_nick, name, path)."""
    out: List[Tuple[str, str, Path]] = []
    for root in _fp_roots():
        for pretty in root.glob("*.pretty"):
            lib = pretty.name[: -len(".pretty")]
            for mod in pretty.glob("*.kicad_mod"):
                out.append((lib, mod.stem, mod))
    return out


@lru_cache(maxsize=20000)
def _fp_pad_count(path: Path) -> int:
    """Count distinct numbered pads in a .kicad_mod (so a thermal-pad variant
    with an extra EP pad is distinguishable from the plain package)."""
    try:
        txt = path.read_text(encoding="utf-8")
    except OSError:
        return -1
    nums = set(re.findall(r'\(pad\s+"([^"]+)"', txt))
    # Mechanical pads are often "" — ignore them; count real numbered pads.
    return len([n for n in nums if n and n not in ("", "MP")])


def _pad_matched_candidates(query: str, pin_count: int,
                            scan: int = 400, keep: int = 60) -> List[str]:
    """Real installed footprints that have EXACTLY `pin_count` pads, ranked by
    name similarity to `query`. Pad count is a hard physical constraint, so the
    list only ever contains footprints that physically fit — the model then
    picks the right PACKAGE from it (retrieval-augmented selection)."""
    try:
        from ..kicad.symbol_geom import _tokenize_part, _score_candidate
    except Exception:
        return []
    req = _tokenize_part(query)
    index = _all_footprints()
    scored = sorted(
        ((_score_candidate(req, nm) if req else 0.0, lib, nm, path)
         for lib, nm, path in index),
        key=lambda t: -t[0],
    )
    # Pool = top-N by name similarity UNION footprints whose name carries the
    # exact pin-count number (MSOP-10, SOIC-8...). The union is what surfaces a
    # right-but-low-scoring family like MSOP-10 for a "VSSOP-10" query.
    pool = list(scored[:scan])
    have = {(lib, nm) for _s, lib, nm, _p in pool}
    pat = re.compile(r"(?<!\d)" + str(pin_count) + r"(?!\d)")
    for s, lib, nm, path in scored:
        if (lib, nm) in have:
            continue
        if pat.search(nm):
            pool.append((s, lib, nm, path))
            have.add((lib, nm))
    pool.sort(key=lambda t: -t[0])
    out: List[str] = []
    for _s, lib, nm, path in pool:
        if _fp_pad_count(path) == pin_count:
            out.append(f"{lib}:{nm}")
            if len(out) >= keep:
                break
    return out


@traceable(run_type="llm", name="Pick footprint")
def _pick_footprint_llm(part: str, package: str, datasheet: str,
                        candidates: List[str]) -> str:
    """Ask Claude to choose the ONE footprint that matches the part's package
    from a list of REAL installed footprints (all already pad-count-correct).
    Returns a lib_id from the list, or "" for NONE. This is what gets VSSOP→
    MSOP and SOT-23 (not SOT-223) right — string matching can't, but the model
    knows the package equivalences."""
    if not candidates:
        return ""
    import anthropic
    client = wrap_anthropic(anthropic.Anthropic(), chat_name="Claude")
    model = (os.environ.get("CLAUDE_MODEL_DEEP")
             or os.environ.get("ENVIL_MODEL") or "claude-sonnet-4-6")
    listing = "\n".join(f"  {c}" for c in candidates)
    sys = ("You are a KiCad footprint selector. You are given a part, its "
           "package (from the datasheet), and a list of REAL installed KiCad "
           "footprints that ALL have the correct pad count. Return the SINGLE "
           "lib_id from the list whose land pattern matches the part's package "
           "(account for equivalences, e.g. VSSOP==MSOP (JEDEC MO-187), "
           "DGS/DGK suffixes, etc.). Reply with ONLY the exact lib_id from the "
           "list, nothing else, or the single word NONE if none truly fit.")
    user = (f"Part: {part}\nPackage: {package}\n"
            f"Datasheet: {datasheet or '(none)'}\n\n"
            f"Candidate footprints (all pad-count-correct):\n{listing}")
    try:
        resp = client.messages.create(
            model=model, max_tokens=200, system=sys, temperature=0,
            messages=[{"role": "user", "content": user}], timeout=60)
    except Exception:  # noqa: BLE001
        return ""
    text = "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", None) == "text").strip()
    cand_set = set(candidates)
    if text in cand_set:
        return text
    for c in candidates:               # tolerate minor wrapping/quotes
        if c in text:
            return c
    return ""


def _resolve_footprint(proposed: str, pin_count: int, part: str = "",
                       datasheet: str = "") -> Tuple[str, str]:
    """Resolve `proposed` ('Lib:Name' or a package hint) to a REAL installed
    footprint lib_id. Returns (lib_id, note); lib_id is "" when nothing fits
    (caller leaves the field blank + warns — never emits a name KiCad can't
    find, and never a confidently-wrong package).

    Strategy: (1) exact match wins. (2) Otherwise gather the installed
    footprints that have EXACTLY pin_count pads (a hard physical filter), then
    let the model pick the one whose PACKAGE matches — string matching alone
    picks wrong families (USON for VSSOP, SOT-223 for SOT-23), so the model
    resolves the equivalence. (3) If nothing fits, blank + warn."""
    proposed = (proposed or "").strip()
    index = _all_footprints()
    if not index:
        return proposed, ("could not scan footprint libraries — left the "
                          "proposed value as-is")
    by_libname = {f"{lib}:{nm}" for lib, nm, _ in index}
    if proposed in by_libname:
        return proposed, "exact"
    if pin_count <= 0:
        return "", f"no pin count to constrain the footprint match for {proposed!r}"
    query = proposed.split(":", 1)[1] if ":" in proposed else proposed
    candidates = _pad_matched_candidates(query, pin_count)
    if not candidates:
        return "", (f"no installed footprint with {pin_count} pads resembles "
                    f"{proposed!r} — likely a non-standard package")
    if proposed in candidates:
        return proposed, "exact"
    pick = _pick_footprint_llm(part or query, proposed, datasheet, candidates)
    if pick:
        return pick, (f"matched {proposed!r} -> {pick} "
                      f"(real footprint, {pin_count} pads)")
    return "", (f"could not confidently match {proposed!r} to an installed "
                f"footprint — left blank for manual assignment")


def _project_sym_lib_table(project_path: str) -> Optional[Path]:
    """Return the sym-lib-table path inside the KiCad project directory.
    Accepts a .kicad_pro/.kicad_sch path or a folder path."""
    p = Path(project_path).expanduser()
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        return None
    return p / "sym-lib-table"


def _register_in_project_lib_table(library: str, symdir: Path,
                                    project_path: str) -> List[str]:
    """Register `library` in the project-level sym-lib-table, creating the
    file when it does not yet exist. Uses the absolute path (not a token)
    since project libraries are local to one machine. Idempotent + backed up."""
    notes: List[str] = []
    tbl = _project_sym_lib_table(project_path)
    if tbl is None:
        return [f"project directory not found for {project_path!r} — "
                "symbol written but not registered in project table"]
    if tbl.exists():
        try:
            text = tbl.read_text(encoding="utf-8")
        except OSError as exc:
            return [f"could not read project sym-lib-table: {exc}"]
    else:
        text = "(sym_lib_table\n)\n"
    if re.search(r'\(lib\s+\(name\s+"' + re.escape(library) + r'"', text):
        notes.append("already registered in project sym-lib-table")
        return notes
    uri = str(symdir).replace("\\", "/")
    entry = (f'\t(lib (name "{library}") (type "KiCad") '
             f'(uri "{uri}") (options "") '
             f'(descr "Envil custom symbols"))\n')
    idx = text.rstrip().rfind(")")
    if idx == -1:
        notes.append("malformed project table — left as-is")
        return notes
    new_text = text[:idx] + entry + text[idx:]
    try:
        if tbl.exists():
            backup = tbl.with_name(tbl.name + ".envil-bak")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
        tbl.write_text(new_text, encoding="utf-8")
        notes.append(f"registered in project sym-lib-table ({tbl})")
    except OSError as exc:
        notes.append(f"could not update project sym-lib-table: {exc}")
    return notes


def _find_sym_lib_tables() -> List[Path]:
    """Locate KiCad's GLOBAL sym-lib-table(s) — one per installed KiCad
    version under %APPDATA%/kicad/<ver>/, plus any KICAD_CONFIG_HOME
    override. These are what the eeschema GUI reads at startup to populate
    the symbol chooser; writing a .kicad_sym file is invisible until the
    library is listed in one of these."""
    out: List[Path] = []
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        base = Path(appdata) / "kicad"
        if base.exists():
            for ver in sorted(base.iterdir()):
                t = ver / "sym-lib-table"
                if t.is_file():
                    out.append(t)
    cfg = os.environ.get("KICAD_CONFIG_HOME")
    if cfg:
        t = Path(cfg) / "sym-lib-table"
        if t.is_file() and t not in out:
            out.append(t)
    return out


def _uri_for(table_text: str, library: str, symdir: Path) -> str:
    """Build the URI for the new lib entry. Reuse the path token the table
    already uses for its .kicad_symdir libraries (e.g. ${KICAD10_SYMBOL_DIR})
    so the new library resolves to the SAME symbols root as the standard
    libs; fall back to the absolute symdir path when no token is found."""
    m = re.search(r'\(uri\s+"([^"]*?)/[^"/]+\.kicad_symdir"', table_text)
    if m:
        return f"{m.group(1)}/{library}.kicad_symdir"
    return str(symdir).replace("\\", "/")


def _register_in_lib_tables(library: str, symdir: Path,
                            abs_uri: bool = False) -> List[str]:
    """Ensure `library` is listed in KiCad's global sym-lib-table(s) so the
    new symbol appears in the eeschema chooser / Symbol Editor. Idempotent
    (skips a table that already has the entry) and safe (backs the table up
    to <name>.envil-bak before the first edit). Returns status notes.

    `abs_uri`: write the absolute symdir path as the URI instead of reusing a
    ${KICADn_SYMBOL_DIR}-style token. Required for a custom `lib_path` that
    lives OUTSIDE the standard symbols root — otherwise _uri_for would point
    KiCad at ${KICADn_SYMBOL_DIR}/<library>.kicad_symdir (the wrong place)."""
    notes: List[str] = []
    tables = _find_sym_lib_tables()
    if not tables:
        return ["no KiCad sym-lib-table found — symbol written but not "
                "registered in the GUI library list"]
    for tbl in tables:
        try:
            text = tbl.read_text(encoding="utf-8")
        except OSError as exc:
            notes.append(f"could not read {tbl.parent.name}/sym-lib-table: {exc}")
            continue
        if re.search(r'\(lib\s+\(name\s+"' + re.escape(library) + r'"', text):
            notes.append(f"already registered in {tbl.parent.name}")
            continue
        uri = (str(symdir).replace("\\", "/") if abs_uri
               else _uri_for(text, library, symdir))
        entry = (f'\t(lib (name "{library}") (type "KiCad") '
                 f'(uri "{uri}") (options "") '
                 f'(descr "Envil custom symbols"))\n')
        idx = text.rstrip().rfind(")")
        if idx == -1:
            notes.append(f"malformed table {tbl.parent.name} — left as-is")
            continue
        new_text = text[:idx] + entry + text[idx:]
        try:
            backup = tbl.with_name(tbl.name + ".envil-bak")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            tbl.write_text(new_text, encoding="utf-8")
            notes.append(f"registered in {tbl.parent.name}")
        except OSError as exc:
            notes.append(f"could not update {tbl.parent.name}: {exc}")
    return notes


# ---------------------------------------------------------------------------
# Part B — retrieve-before-create + verify (grounded in CircuitLM / PCBSchemaGen)
# ---------------------------------------------------------------------------

CONFIDENCE_MIN = 0.5          # below this we refuse + flag (OOD guard)
EXISTS_SCORE = 0.95          # >= this vs an installed part => don't duplicate
SIMILAR_SCORE = 0.75        # >= this => note the close match but still create


def _is_kicad_std_root(root: Path) -> bool:
    """True when `root` is a KiCad-shipped standard library — not writable
    by the user and not a custom/project lib. Detected by path pattern:
    Program Files/KiCad installs and Documents/KiCad/<ver>/symbols."""
    norm = str(root).replace("\\", "/").lower()
    return ("program files/kicad" in norm or
            "/documents/kicad/" in norm)


def _custom_sym_roots() -> List[Path]:
    """Symbol roots that belong to the user's custom libraries — excludes
    KiCad's read-only standard library directories."""
    try:
        from ..kicad.symbol_geom import _sym_roots
        return [r for r in _sym_roots() if not _is_kicad_std_root(r)]
    except Exception:
        return []


def _find_existing(part_number: str) -> Optional[Tuple[str, float]]:
    """Search CUSTOM libs only for a symbol matching `part_number`.
    Ignores KiCad standard libraries so the user can create their own
    version of any part without being blocked by an existing KiCad symbol.
    Returns (lib_id, score) or None."""
    try:
        from ..kicad.symbol_geom import _tokenize_part, _score_candidate
        req = _tokenize_part(part_number)
        if not req:
            return None
        best_score, best_lid = 0.0, None
        for root in _custom_sym_roots():
            for symdir in root.glob("*.kicad_symdir"):
                if not symdir.name.endswith(".kicad_symdir"):
                    continue
                libnick = symdir.name[: -len(".kicad_symdir")]
                for p in symdir.glob("*.kicad_sym"):
                    s = _score_candidate(req, p.stem)
                    if s >= SIMILAR_SCORE and s > best_score:
                        best_score = s
                        best_lid = f"{libnick}:{p.stem}"
        return (best_lid, best_score) if best_lid else None
    except Exception:
        return None


def _verify_pinout(reference: str, pins: List[dict], confidence: float
                   ) -> List[str]:
    """Deterministic correctness checks on the extracted pinout, with errors
    localised to the offending pins (PCBSchemaGen-style). Returns a list of
    human-readable issue strings (empty = clean). These drive the single
    self-heal retry."""
    issues: List[str] = []
    # Duplicate pin numbers — KiCad rejects, and it signals a bad extraction.
    seen, dupes = set(), set()
    for p in pins:
        n = p["number"]
        if n in seen:
            dupes.add(n)
        seen.add(n)
    if dupes:
        issues.append(f"duplicate pin numbers {sorted(dupes)} — each package "
                      f"pin must appear once")
    # An IC (refdes U) with no power pin at all is almost certainly incomplete.
    if reference == "U":
        power_pins = [p for p in pins if p["etype"] in ("power_in", "power_out")]
        if not power_pins:
            issues.append("no power/ground pin found on an IC — datasheet "
                          "extraction likely incomplete (expect at least a "
                          "supply + ground pin)")
    # Mostly-unnamed pins => the name column wasn't read.
    unnamed = sum(1 for p in pins if p["name"] in ("~", "", "NC") )
    if pins and unnamed > len(pins) * 0.5:
        issues.append(f"{unnamed}/{len(pins)} pins have no real name — the pin "
                      f"name column was probably not extracted")
    # Self-reported low confidence.
    if confidence < CONFIDENCE_MIN:
        issues.append(f"low extraction confidence ({confidence:.2f}) — confirm "
                      f"against the official datasheet")
    return issues


# ---------------------------------------------------------------------------
# Tool entry
# ---------------------------------------------------------------------------

@tool(
    name="create_symbol",
    description=(
        "Create a NEW KiCad schematic symbol (.kicad_sym) for a part that is "
        "NOT in any installed library, by researching its datasheet. Use this "
        "when build_circuit / apply_ops can't find a symbol for a part the user "
        "needs (the 'no symbol in your library' case) — call create_symbol "
        "FIRST, then build/add using the returned lib_id.\n\n"
        "Flow: it calls Claude with web search internally, reads the official "
        "datasheet pinout, draws the pins on a 100-mil grid (inputs left, "
        "outputs right, supplies top, grounds bottom), and writes the symbol "
        "into the GLOBAL shared symbol library. It ALSO registers the library "
        "in KiCad's global symbol-library table so the part shows up in the "
        "eeschema symbol chooser (KiCad must be restarted to see it).\n\n"
        "Args:\n"
        "  part_number: the part / MPN to create (required, e.g. 'BQ76952', "
        "'ESP-12F', 'INA240A1D').\n"
        "  library: target library nickname (default 'Custom'). The symbol is "
        "written as <library>.kicad_symdir/<part>.kicad_sym; the resolved "
        "lib_id is '<library>:<part>'.\n"
        "  datasheet_url: optional direct datasheet URL to read first.\n"
        "  description: optional extra context to disambiguate the part "
        "(package, variant, what it is).\n"
        "  footprint: optional KiCad footprint lib_id to attach (e.g. "
        "'Package_TO_SOT_THT:TO-220-3_Vertical'); if omitted, research tries "
        "to pick one.\n"
        "  pins_json: optional — supply the pin list yourself to SKIP web "
        "research. JSON: {\"symbol_name\":..,\"reference\":\"U\",\"pins\":"
        "[{\"number\":\"1\",\"name\":\"VCC\",\"type\":\"power_in\",\"side\":"
        "\"top\"}, ...]}.\n"
        "  overwrite: bool — replace an existing symbol FILE of the same name "
        "(default false; refuses if the file already exists).\n"
        "  force: bool — (a) create even if a matching part already exists in "
        "an installed library (otherwise it returns status='exists' with that "
        "lib_id), and (b) create even when datasheet confidence is low "
        "(otherwise it refuses and asks for a datasheet_url / pins_json). "
        "Default false.\n"
        "  pin_layout: 'datasheet' (default) draws the pins in the SAME layout/"
        "order as the datasheet pinout diagram (package order, not regrouped); "
        "'functional' regroups them tidily (inputs left, outputs right, "
        "supplies top, grounds bottom). Use 'datasheet' to mirror the part "
        "exactly as drawn in its datasheet.\n\n"
        "  scope: 'global' (default) writes into the shared global symbol "
        "library visible to ALL KiCad projects on this machine. 'project' "
        "writes into the active KiCad project's local library folder "
        "(<project_dir>/<library>.kicad_symdir/) and registers it in the "
        "project-level sym-lib-table — the symbol is only available to that "
        "project. Requires project_path when scope='project'.\n"
        "  project_path: path to the .kicad_pro file (or project folder). "
        "Required when scope='project'; ignored otherwise.\n"
        "  lib_path: optional explicit output path for the symbol library. "
        "When provided this overrides scope/project_path entirely. Two forms "
        "are accepted: (a) a plain directory, e.g. 'C:/MyProject/libs/' — the "
        "tool creates '<library>.kicad_symdir' inside it; (b) a directory "
        "whose name already ends in '.kicad_symdir', e.g. "
        "'C:/MyProject/libs/Custom.kicad_symdir' — used as-is. The symbol "
        "is registered in the global sym-lib-table with an absolute URI so "
        "KiCad can find it, and the in-session resolver is updated immediately "
        "so subsequent build_circuit / apply_ops calls work without a "
        "restart.\n\n"
        "Returns JSON: {status:'created'|'exists', lib_id, path, pin_count, "
        "reference, footprint, confidence, warnings, library_registered, scope}. "
        "status='exists' means use the returned lib_id (no symbol written). "
        "Pass `lib_id` to build_circuit / apply_ops to place the part."
    ),
    input_schema={
        "part_number": str, "library": str, "datasheet_url": str,
        "description": str, "footprint": str, "pins_json": str,
        "overwrite": bool, "force": bool, "pin_layout": str,
        "scope": str, "project_path": str, "lib_path": str,
    },
)
async def create_symbol(args: Dict[str, Any]) -> Dict[str, Any]:
    def _err(msg: str) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": f"ERROR: {msg}"}],
                "is_error": True}

    part = (args.get("part_number") or "").strip()
    if not part:
        return _err("part_number required")
    library = (args.get("library") or "Custom").strip() or "Custom"
    library = re.sub(r"[^A-Za-z0-9_+-]+", "_", library).strip("_") or "Custom"
    datasheet_url = (args.get("datasheet_url") or "").strip()
    hint = (args.get("description") or "").strip()
    pins_json = (args.get("pins_json") or "").strip()
    overwrite = bool(args.get("overwrite", False))
    force = bool(args.get("force", False))
    pin_layout = (args.get("pin_layout") or "datasheet").strip().lower()
    if pin_layout not in ("datasheet", "functional"):
        pin_layout = "datasheet"
    scope = (args.get("scope") or "global").strip().lower()
    if scope not in ("global", "project"):
        scope = "global"
    project_path = (args.get("project_path") or "").strip()
    lib_path_raw = (args.get("lib_path") or "").strip()
    if scope == "project" and not project_path and not lib_path_raw:
        return _err("scope='project' requires project_path (path to the "
                    ".kicad_pro file or project folder)")
    researched = not pins_json

    def _parse(data: dict):
        """Pull (pins, name, description, reference, confidence) from a
        research/caller dict, applying IEEE-315 refdes normalisation."""
        pl = [p for p in (_norm_pin(rp) for rp in (data.get("pins") or [])) if p]
        nm = str(data.get("symbol_name") or part).strip() or part
        desc = str(data.get("description") or hint).strip()
        ref = _ieee315_refdes(str(data.get("reference") or ""), nm, desc,
                              pin_count=len(pl))
        try:
            conf = float(data.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        return pl, nm, desc, ref, conf

    # ---- 1. RETRIEVE-before-create: don't duplicate an existing part ----
    similar_note = ""
    if researched and not force:
        existing = _find_existing(part)
        if existing:
            elib, escore = existing
            if escore >= EXISTS_SCORE:
                res = {
                    "status": "exists",
                    "lib_id": elib,
                    "match_score": round(escore, 2),
                    "note": (f"{part!r} already exists as {elib} "
                             f"(match {escore:.2f}). Use THAT lib_id in "
                             f"build_circuit / apply_ops — no new symbol "
                             f"created. Pass force=true to make a new variant "
                             f"anyway."),
                }
                return {"content": [{"type": "text",
                                     "text": json.dumps(res, indent=2)}]}
            if escore >= SIMILAR_SCORE:
                similar_note = (f"note: a similar part already exists "
                                f"({elib}, match {escore:.2f}); created the "
                                f"requested variant anyway")

    # ---- 2. Get the pin data: caller-supplied or web research ----
    try:
        if pins_json:
            data = json.loads(pins_json)
        else:
            data = await asyncio.to_thread(_research_part, part,
                                           datasheet_url, hint)
    except json.JSONDecodeError as exc:
        return _err(f"pins_json is not valid JSON: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _err(f"could not research {part!r}: {type(exc).__name__}: {exc}")

    pins, name, description, reference, confidence = _parse(data)
    if not pins:
        return _err(f"no usable pins found for {part!r} — supply pins_json "
                    f"or a clearer part_number / datasheet_url")

    # ---- 3. VERIFY + one bounded SELF-HEAL retry (research path only) ----
    issues = _verify_pinout(reference, pins, confidence)
    if issues and researched:
        feedback = "\n".join(f"- {i}" for i in issues)
        try:
            data2 = await asyncio.to_thread(_research_part, part,
                                            datasheet_url, hint, feedback)
            p2, n2, d2, r2, c2 = _parse(data2)
            # Accept the retry only if it has pins and is no worse.
            if p2 and len(_verify_pinout(r2, p2, c2)) <= len(issues):
                data, pins, name, description, reference, confidence = \
                    data2, p2, n2, d2, r2, c2
                issues = _verify_pinout(reference, pins, confidence)
        except Exception:  # noqa: BLE001 — keep the first attempt
            pass

    # ---- 4. OOD / hallucination GUARD ----
    # Hard-block duplicate pin numbers (KiCad rejects them outright).
    seen, dupes = set(), set()
    for p in pins:
        if p["number"] in seen:
            dupes.add(p["number"])
        seen.add(p["number"])
    if dupes:
        return _err(f"duplicate pin numbers {sorted(dupes)} for {part!r} — "
                    f"extraction inconsistent, not writing. Supply pins_json "
                    f"with the correct pinout to override.")
    # Refuse to fabricate a low-confidence pinout unless explicitly forced.
    if researched and confidence < CONFIDENCE_MIN and not force:
        return _err(
            f"low confidence ({confidence:.2f}) that the {part!r} pinout is "
            f"correct — I could not reliably confirm it from a datasheet. "
            f"Options: give a datasheet_url, supply pins_json with the real "
            f"pinout, or pass force=true to create it from the best guess. "
            f"Issues: {'; '.join(issues) or 'none'}")

    datasheet = str(data.get("datasheet") or datasheet_url).strip()
    # Footprint (physical match): explicit arg wins, else whatever research
    # found — then RESOLVE it to a real installed footprint so KiCad never shows
    # "Footprint not found". Pad-count == pin-count keeps us off thermal-pad
    # variants.
    fp_proposed = (args.get("footprint") or data.get("footprint") or "").strip()
    footprint, fp_note = "", ""
    if fp_proposed:
        footprint, fp_note = await asyncio.to_thread(
            _resolve_footprint, fp_proposed, len(pins), part, datasheet)
        if not footprint:
            fp_note = (f"no installed footprint matched {fp_proposed!r} — left "
                       f"blank; assign one in KiCad or create it")

    # ---- 5. Lay out + serialise (IEEE 315 / KLC) ----
    # Assign body sides per the layout mode (datasheet-faithful by default).
    pins = _assign_sides(pins, pin_layout)
    pin_len = _pin_length_for(pins)
    half_w, half_h, placed = _layout(pins, pin_len)
    text = _build_kicad_sym(name, reference, description, datasheet,
                            footprint, half_w, half_h, placed)

    # ---- 6. Write into the target library (global, project-scoped, or custom) ----
    if lib_path_raw:
        lib_path_obj = Path(lib_path_raw).expanduser().resolve()
        if lib_path_obj.name.endswith(".kicad_symdir"):
            symdir = lib_path_obj
        else:
            symdir = lib_path_obj / f"{library}.kicad_symdir"
        effective_scope = "custom"
    elif scope == "project":
        proj_dir = Path(project_path).expanduser()
        if proj_dir.is_file():
            proj_dir = proj_dir.parent
        symdir = proj_dir / f"{library}.kicad_symdir"
        effective_scope = "project"
    else:
        symdir = _target_root() / f"{library}.kicad_symdir"
        effective_scope = "global"
    out_path = symdir / f"{_safe_part(name)}.kicad_sym"
    if out_path.exists() and not overwrite:
        return _err(f"symbol file already exists at {out_path} — pass "
                    f"overwrite=true to replace it")
    try:
        symdir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return _err(f"could not write symbol file: {exc}")

    # ---- 7. Register the library in KiCad's GUI table + refresh caches ----
    if effective_scope == "custom":
        # Register with an absolute URI in the global sym-lib-table so KiCad
        # finds it, then inject the parent root into the in-session resolver.
        register_notes = _register_in_lib_tables(library, symdir, abs_uri=True)
        try:
            from ..kicad.symbol_geom import inject_sym_root
            inject_sym_root(str(symdir.parent))
        except Exception:
            pass
    elif effective_scope == "project":
        register_notes = _register_in_project_lib_table(library, symdir,
                                                        project_path)
        # Inject the project root so subsequent load_symbol / edit_symbol
        # calls in this session can find the newly created symbol.
        try:
            from ..kicad.symbol_geom import inject_project_sym_roots
            inject_project_sym_roots(project_path)
        except Exception:
            pass
    else:
        register_notes = _register_in_lib_tables(library, symdir)
    _refresh_caches()
    lib_id = f"{library}:{name}"
    load_note = "ok"
    try:
        from ..kicad.symbol_geom import load_symbol
        geom = load_symbol(lib_id)
        if len(geom.pins) != len(pins):
            load_note = (f"warning: wrote {len(pins)} pins but reloaded "
                         f"{len(geom.pins)}")
    except Exception as exc:  # noqa: BLE001 — file written, but verify failed
        load_note = f"warning: written but failed to reload ({exc})"

    sides = {}
    for p in placed:
        sides[p["side"]] = sides.get(p["side"], 0) + 1
    gui_registered = any(n.startswith(("registered in", "already registered"))
                         for n in register_notes)
    warnings = list(issues)
    if similar_note:
        warnings.append(similar_note)
    if fp_note and fp_note not in ("exact",):
        warnings.append(f"footprint: {fp_note}")
    if fp_proposed and not footprint:
        warnings.append("no footprint attached — physical package unresolved")
    if effective_scope == "custom":
        scope_note = (
            f"Created symbol {lib_id} with {len(pins)} pins at custom path "
            f"{str(out_path).replace(chr(92), '/')}. "
            + ("Registered in KiCad's global library table with an absolute "
               f"URI — RESTART KiCad (or Preferences > Manage Symbol "
               f"Libraries) to see it under the '{library}' library. "
               if gui_registered else
               "NOTE: could not auto-register in KiCad's library table; "
               "add it manually via Preferences > Manage Symbol Libraries. ")
            + "The in-session resolver is already updated — you can use "
            + f"lib_id={lib_id} in build_circuit / apply_ops right away."
        )
    elif effective_scope == "project":
        scope_note = (
            f"Created project-local symbol {lib_id} with {len(pins)} pins, "
            f"saved to {str(out_path).replace(chr(92), '/')}. "
            + ("Registered in the project sym-lib-table — RESTART KiCad "
               "(or Preferences > Manage Symbol Libraries) to see it. "
               if gui_registered else
               "NOTE: could not auto-register in the project sym-lib-table; "
               "add it manually via Preferences > Manage Symbol Libraries. ")
            + f"This symbol is LOCAL to this project. "
            + f"Use lib_id={lib_id} in build_circuit / apply_ops to place it."
        )
    else:
        scope_note = (
            f"Created global symbol {lib_id} with {len(pins)} pins, saved to "
            f"{str(out_path).replace(chr(92), '/')}. "
            + ("Registered in KiCad's global library table — RESTART KiCad "
               "(or Preferences > Manage Symbol Libraries) to see it in the "
               f"symbol chooser under the '{library}' library. "
               if gui_registered else
               "NOTE: could not auto-register it in KiCad's library table; "
               "add it manually via Preferences > Manage Symbol Libraries. ")
            + f"Use lib_id={lib_id} in build_circuit / apply_ops to place it."
        )
    result = {
        "status": "created",
        "scope": effective_scope,
        "lib_id": lib_id,
        "path": str(out_path).replace("\\", "/"),
        "symbol_name": name,
        "reference": reference,
        "footprint": footprint,
        "pin_count": len(pins),
        "sides": sides,
        "datasheet": datasheet,
        "confidence": round(confidence, 2),
        "warnings": warnings,
        "verify": load_note,
        "library_registered": gui_registered,
        "library_table": register_notes,
        "note": scope_note,
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
