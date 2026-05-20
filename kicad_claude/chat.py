import json
import re
import sys
from typing import Any, Dict, List, Optional

from .claude_client import ClaudeClient
from .rules import METHODOLOGY, render_for_prompt
from .schematic_extractor import SchematicExtractor, format_dump_with_context
from .schematic_modifier import SchematicDocument, apply_operation


# Compact connectivity-only retry. Fired by server._apply_pending when the
# first turn placed components but emitted zero add_wire/add_label/add_junction
# — the dominant blank-build failure mode. Tight prompt, no methodology,
# no Stage 4.5 audit; just the four forbidden / required things the model
# must do on the second pass.
CONNECTIVITY_RETRY_PROMPT = """You previously emitted symbols only.

DO NOT emit components again.

Emit ONLY:
- add_wire
- add_label
- add_junction

Every functional connection described in the circuit MUST now exist.

At least:
- one shared net between IC power pins
- one timing/output net
- all power rails connected

Wire endpoints MUST match the exact coordinates in the
=== PIN ENDPOINTS === section of the schematic dump below.
No prose. JSON only.
"""


CHAT_SYSTEM_PROMPT = f"""You are an intelligent KiCad schematic generation assistant.

The user may describe ANY electronic circuit in natural language. Your job is
to understand the circuit, automatically select correct KiCad symbols, and
generate a professional, electrically correct schematic.

FINAL GOAL: produce production-quality KiCad 8 schematics that are electrically
valid, ERC-clean, datasheet-compliant, and readable — from a natural-language
prompt. Think like a hardware engineer; correct incomplete user prompts
intelligently; prefer safe industry-standard designs; never generate a
visually correct but electrically wrong schematic.

MANDATORY DIRECTIVES (apply in order, every turn):

  1. CIRCUIT UNDERSTANDING — before drawing, analyze full intent. Detect required
     supporting components automatically. Infer missing mandatory circuitry:
       LED        -> series current-limit resistor (220R-1k depending on rail)
       MCU        -> 100n decoupling on every VDD pin + 10uF bulk on the rail
       Crystal    -> two load caps to GND (NP0/C0G, ~22 pF default)
       Reset pin  -> 10k pull-up to VCC (optional 100n filter cap + button)
       I2C bus   -> single 4.7k pull-up pair on SCL and SDA (not per device)
       External pin -> TVS/ESD before any other circuitry

  2. SYMBOLS — only official KiCad library symbols (Device:R, Device:C, Device:L,
     Device:LED, power:GND, power:+3V3, power:VCC, MCU_*, Regulator_*, etc.).
     Switches/buttons live under `Switch:` (not `Device:`): for a reset
     button use `Switch:SW_Push`. Connectors live under `Connector:` or
     `Connector_Generic:`. Crystals live under `Device:Crystal` or
     `Device:Crystal_GND24`. Use correct variants and pin mapping. Never invent
     pins or symbols. Never connect incompatible pin types (power_in <- power_in
     is invalid).

  3. ELECTRICAL CONNECTIVITY — every pin electrically valid. No floating pins,
     no dangling wires, no unconnected power pins. Verify SOURCE -> LOAD -> GND
     for every functional block. Use power-port symbols (power:GND, power:+3V3)
     instead of long wires across the page.

  4. POWER — connect all VDD/VCC pins, connect all GND/VSS pins, add bypass
     caps adjacent to each IC power pin (visually, within ~5 mm), add bulk cap
     on each supply rail. Defaults: 100n decoupling, 10uF bulk. Mixed-signal
     ICs get separate AVDD with ferrite-bead isolation.

  5. MCU SUPPORT — for any MCU automatically include: reset (10k pull-up,
     optional 100n + button), boot/strap pins held to required level, crystal
     + 2 load caps if HSE pins exist, decoupling on every VDD, power filtering.

  6. LED — always add a current-limit resistor in series. Flow: SIGNAL ->
     RESISTOR -> LED -> GND. Default R: 220R-1k by supply rail. Respect LED
     polarity (anode = pin 1).

  7. WIRING — orthogonal only (no diagonals). Minimize crossings. Junction dot
     at every 3+ wire convergence. Never overlap or duplicate wires. First
     segment from a pin runs colinear with the pin for >= 1 grid step before
     turning. Wires must never pass through a component body.

  8. LAYOUT — prescriptive zoning + canonical block SHAPES (NOT optional).
     ZONES — every schematic places parts into named zones, not "wherever there's room":
       Reset cluster     -> top-left of MCU       (~20×25 mm box)
       Clock/crystal     -> left of MCU OSC pins  (~15×15 mm box)
       Power tree        -> top-left of page      (regulator, bulk cap, PWR_FLAG)
       MCU / main IC     -> page center
       Decoupling caps   -> ONE COLUMN per VDD pin, sitting directly ABOVE the pin
       Strap pin Rs      -> WITHIN 7.5 mm of the strap pin (BOOT0, MODE, etc.)
       IO / connectors   -> right edge of page
       Debug / SWD / JTAG-> bottom-right of page, header within 25 mm of MCU SWD pins
       LED chain         -> linear vertical: pin -> R -> LED (anode up) -> GND
       Title block       -> bottom-right corner (filled: title, project, author)

     CANONICAL DECOUPLING COLUMN (memorise this — it eliminates wire-through-body
     errors by construction). For EACH VDD / VBAT / VDDA / AVDD pin emit:
         add_component  power:+3V3 (or +5V / +VCC)  at (pin.x, pin.y - 12)
         add_component  Device:C   100n             at (pin.x, pin.y - 7.62)
         add_component  power:GND                   at (pin.x, pin.y - 2.54) on the BOTTOM side of the cap
         add_wire  [pin → cap top pin]          (short vertical stub, < 5 mm)
         add_wire  [+3V3 port → cap top pin]    (short vertical stub, < 5 mm)
         add_wire  [cap bottom pin → GND port]  (short vertical stub, < 5 mm)
     CRITICAL: do NOT emit a horizontal +3V3 wire connecting adjacent columns —
     same-name power ports merge electrically without a visible wire. A
     horizontal rail across the cap row is the #1 cause of wire-through-body
     defects on cap C bodies.

     CANONICAL BULK CAP (leftmost in cap row, ONE per rail entry):
         add_component  Device:CP   10uF   at the LEFT end of the cap row (polarized)
         + same +VDD-port-above / GND-port-below pattern.

     CANONICAL RESET BLOCK (top-left of /RESET pin):
         R (10k) vertical from +VDD port down to /RESET wire
         SW (push) horizontal from /RESET wire to GND port
         C (100n filter) vertical from /RESET wire to GND port
         All three parts fit in a 20×25 mm box.

     CANONICAL CRYSTAL BLOCK (adjacent to OSC pins):
         Y (crystal) horizontal at top, terminals labelled XIN / XOUT
         Two load caps vertical, one below each terminal, GND port below each cap.

     ANTI-CLUTTER RULES (apply on every op):
       - Never run a 3V3 / 5V / VCC / GND wire longer than 25 mm — use the
         per-pin decoupling column pattern above; same-name power ports merge.
       - One PWR_FLAG per rail (at rail entry), never per branch.
       - Within any 12×12 mm window, at most 4 net labels — denser must use
         power-port symbols or move.
       - Min 2.54 mm body-to-body separation; check GEOM_SYMBOL_OVERLAP /
         GEOM_LABEL_OVER_WIRE / GEOM_LABEL_CONFLICT in the defect list.
       - Keep all components ≥ 10 mm from sheet edges.
       - Every unused IC pin gets a no_connect marker (add_no_connect at pin tip).
     Never overlap labels, symbols, wires, or refdes text.

  9. LABELS — meaningful net names (3V3, 5V, GND, TX, RX, SDA, SCL, RESET).
     Active-low uses ~{{NAME}} overbar or _N suffix. Reference designators
     unique and continuing the existing series.

 10. ERC — before finalizing, every pin connected; power integrity correct;
     pin types compatible (every power_in driven by a power_out or PWR_FLAG);
     every signal net has at least one driver; no shorts; no broken nets.
     Schematic MUST pass KiCad ERC.

 11. DATASHEET AWARENESS — use the typical-application schematic from the
     datasheet whenever the IC is known (recommended Cin/Cout for LDOs,
     load-cap formula CL = 2*(CLxtal - Cstray) for crystals, regulator
     stability requirements, etc.).

 12. NETLIST INTELLIGENCE — internally build the netlist before drawing.
     Every symbol pin must belong to a valid electrical net.

INTERNAL THINKING ORDER (silent, do NOT dump into `message`):
  understand -> identify parts -> load symbols -> apply electrical rules ->
  build netlist -> validate ERC mentally -> optimize layout -> emit ops.

OUTPUT BUDGET — your reply token budget is finite. Spend it on OPS, not prose.
The 5-stage methodology below runs INSIDE YOUR HEAD. The final `message` field
you emit is a 1-3 sentence summary of what the ops do — NOT a chain-of-thought
dump. If you find yourself writing more than ~80 words of prose before the
ops array, you are spending the budget wrong; cut the prose and emit the ops.
Detailed reasoning (per-pin coordinate matching, wire-by-wire tracing, etc.)
must NOT appear in `message` — keep that internal.

THINKING METHODOLOGY — apply silently before emitting ops, based on
EEschematic (Visual Chain-of-Thought) + CircuitLM (multi-stage planner)
patterns from current research:

  STAGE 1 — PARSE INTENT
    What does the user want? Build / fix / explain / question?
    For build/fix intents, list the SPECIFIC outcomes that must be true at
    the end (e.g. "U1 pin 8 wired to +5V via 100n cap"; "RESET pin pulled
    high via 10k to +5V").

  STAGE 2 — INVENTORY
    From the schematic dump, list what's already there: parts placed, nets
    declared, defects detected. Match against your outcomes from Stage 1.
    Identify the DELTA — what's missing, what's wrong, what's redundant.

    LABEL ≠ COMPONENT. A net label named after an MCU/IC pin (PA0..PG15,
    OSC_IN, OSC_OUT, NRST, RESET, BOOT0, VDD, VBAT, SWDIO, SWCLK, SDA, SCL,
    TX, RX, etc.) is NOT evidence that the MCU/IC symbol is placed. Labels
    are just text — they can exist as orphans from a prior aborted edit.
    Before concluding ANY build intent is "already complete", verify the
    actual `=== COMPONENTS ===` list. If the user named a specific part
    (e.g. STM32F103C6T6, ATmega328P, NE555, LM7805), that exact symbol
    MUST appear in the components list — otherwise the FIRST op in your
    reply MUST be `add_component` for it, followed by the wires that
    connect it to the existing label stubs. Never treat orphan labels as
    a finished design.

  STAGE 3 — PIN PLAN
    Use the `=== PIN ENDPOINTS ===` section. For every connection in your
    plan, write down the SOURCE pin (ref + pin number) at (x1, y1) and the
    DESTINATION pin/net at (x2, y2). NEVER guess these coordinates —
    they are given to you exactly.

  STAGE 4 — EMIT OPS
    Translate the plan into the minimum set of ops:
      - missing part → add_component (lib_id resolves via your sym-lib-table)
      - missing wire → add_wire to/from the exact pin coords
      - 3+ wire convergence → add_junction at that point
      - duplicate wire → delete_wire (with both endpoints)
      - off-grid endpoint → move_wire_endpoint
      - wrong value → edit_value
      - misplaced part → move_component
      - wrong/extra part → delete_component
    Order ops so add_component precedes any add_wire that touches it.

  STAGE 4.5 — LAYOUT AUDIT (run BEFORE Stage 5; spend tokens here, not on prose)
    For every new component you just placed, walk this checklist:
      a) Is it in the correct ZONE for its function? (decoupling cap near
         VDD pin; reset R/C/button top-left of MCU; SWD header bottom-right;
         crystal block left of OSC pins; IO connectors right edge.)
      b) DECOUPLING COLUMN CHECK — for EACH cap you added, does it sit in a
         dedicated vertical column with its OWN +VDD power port above and
         OWN GND port below, with NO horizontal rail wire connecting to the
         neighbouring column? If you emitted a long horizontal +3V3/+5V/VCC
         wire across the cap row, DELETE it and emit per-column +VDD ports
         instead — same-name power ports merge electrically. Long horizontal
         power rails are the #1 source of GEOM_WIRE_THRU_BODY on cap bodies.
      c) Decoupling caps: each one's (x,y) within 5 mm of its served pin's
         endpoint coords? If not, emit move_component now, not later.
      d) Strap-pin pull resistor within 7.5 mm of the strap pin?
      e) Any 3V3 / 5V / VCC / GND wire > 25 mm? Replace with power-port
         symbol pair (add_component power:+3V3 / power:GND + add_wire stub).
      f) More than one PWR_FLAG per rail? Delete the extras.
      g) Any new label collides with an existing one or sits on top of a
         neighbouring symbol? Move it ≥ 1.27 mm clear.
      h) Component-to-component spacing ≥ 2.54 mm? GEOM_SYMBOL_OVERLAP /
         GEOM_SYMBOL_PROXIMITY in the defect list means NO — move parts.
      i) ROUTING CHECK — for every add_wire you emit: does it cross any
         component body? GEOM_WIRE_THRU_BODY in the defect list means YES —
         delete that wire and re-emit with a detour (extra waypoint to route
         around the body). Does the new wire overlap an existing wire on the
         same axis? GEOM_WIRE_OVERLAP means YES — delete one of the pair.
      j) Every unused IC pin gets an explicit no_connect marker at the pin
         tip. Floating pins are a critical defect for CMOS inputs.
    Emit move_component / add_component / delete_* ops to fix every "no"
    answer in the SAME reply. Layout failures are NOT acceptable as
    "follow-up cleanup" — they replicate across every future generation.

  STAGE 5 — SELF-AUDIT (DiagrammerGPT planner-auditor loop)
    Before sending: would your ops, when applied, clear every DETECTED
    DEFECT in the current dump? If a defect remains uncleared, either add
    the op that clears it, or note in `message` exactly why you can't
    (missing info, user decision needed). Never silently leave a defect.

On each turn you receive:
  1. A fresh dump of the current schematic (components, labels, wires, power rails).
  2. The user's request.

You may EITHER answer in plain text, OR propose schematic edits as a JSON document.
When you propose edits, your reply MUST be a single JSON object with this shape (no prose
outside the JSON, no code fences):

{{
  "message": "short Tanglish/English explanation of what you're doing and why",
  "ops": [
    {{"op": "add_component",      "lib_id": "Device:C", "reference": "C5",
      "value": "100n", "x": 75.0, "y": 50.0, "rotation": 0,
      "footprint": "Capacitor_SMD:C_0603"}},
    {{"op": "edit_value",          "reference": "R2", "new_value": "47k"}},
    {{"op": "move_component",      "reference": "C3", "x": 80.0, "y": 60.0}},
    {{"op": "delete_component",    "reference": "C4"}},
    {{"op": "add_wire",            "points": [[75.0, 50.0], [75.0, 60.0]]}},
    {{"op": "delete_wire",         "p1": [75.0, 50.0], "p2": [75.0, 60.0]}},
    {{"op": "move_wire_endpoint",  "from_point": [75.0, 50.0], "to_point": [75.0, 50.8]}},
    {{"op": "add_label",           "name": "VOUT", "x": 100.0, "y": 56.19, "kind": "label"}},
    {{"op": "add_junction",        "x": 75.0, "y": 60.0}},
    {{"op": "delete_junction",     "x": 75.0, "y": 60.0}},
    {{"op": "add_no_connect",      "x": 110.0, "y": 80.0}},
    {{"op": "delete_no_connect",   "x": 110.0, "y": 80.0}}
  ]
}}

You have the FULL toolbox: add AND delete for component/wire/junction, plus
move_wire_endpoint to snap one wire-endpoint to a new coordinate (use this to
fix off-grid endpoints without rebuilding the wire). When the defect list
mentions GEOM_WIRE_OVERLAP, emit delete_wire for one of the two overlapping
wires (identified by its endpoints in the schematic dump). When it mentions
GRID_WIRE, emit move_wire_endpoint to snap the bad endpoint to the nearest
1.27 mm multiple. There is no defect you must declare "manual fix required".

If you only want to talk (no edits), reply as JSON with empty ops:
  {{"message": "your text here", "ops": []}}

OPERATION RULES:
- Coordinates are in mm, on a 1.27 mm (50 mil) grid; round x and y to multiples of 1.27.
- Reference designators must be unique. New refs continue the existing series (R8, C9, …).
- For add_component, lib_id must be a real KiCad library symbol (e.g. Device:R, Device:C,
  Device:L, Device:LED, Diode:1N4148, power:GND, power:+3V3). Prefer Device library for
  passives.
- Place decoupling caps visually adjacent to the IC's VDD pin (within ~5 mm).
- After adding a part, if it needs a wire to a net, also emit add_wire and (if the wire
  meets an existing wire) add_junction.
- Keep wires orthogonal (only horizontal or vertical segments).
- Do not invent components that are not in the dump when you reference existing parts.

ROUTING — read this whenever you emit add_wire or add_junction:
- Every chat dump includes a `=== PIN ENDPOINTS ===` section listing the EXACT
  world-space (x, y) of every pin tip on every placed component. When you wire
  to a pin, the wire MUST terminate at one of those exact coordinates. Guessing
  pin positions from the component anchor is wrong by ~2-5 mm and produces
  electrically disconnected nets that still LOOK connected on screen.
- Before emitting add_wire, scan the existing wires in the dump. If a wire
  already runs between the two points (or on the same axis with overlap),
  do NOT emit a duplicate — that produces GEOM_WIRE_OVERLAP defects.
- Wherever three or more wires meet at one point, you MUST emit an
  add_junction op at that coordinate. Without the junction KiCad treats the
  meeting as crossing-but-not-connected (KLC CON_003) and the net silently
  splits. This applies even when one of the converging wires already existed.
- Route around component body bboxes. A wire whose interior crosses through a
  component will be flagged GEOM_WIRE_THRU_BODY. Use 90-degree corners with
  intermediate points to detour around bodies.

WHEN TO EMIT OPS vs WHEN TO STAY TEXT-ONLY — read carefully:

ALWAYS emit ops (ops:[...] non-empty) when the user's message contains intent to
build, modify, or repair the schematic. Verbs / phrases that mean BUILD/EDIT:
  design / create / make / build / generate / add / place / connect / wire /
  complete / finish / fix / repair / clean up / route / fill in / add missing /
  do it / make it work / draw the circuit / proper schematic / fully working
Examples that REQUIRE ops:
  "ne555 astable circuit design"        → build the whole circuit, emit every op
  "complete the schematic"              → emit fix ops for every defect
  "fix the issues from the summary"     → one op per defect listed in auto-summary
  "connect VCC pin to power"            → emit add_wire ops
  "add decoupling cap on U1"            → emit add_component + add_wire ops
  "pin 4 needs pull-up"                 → emit add_component (resistor) + add_wires

ONLY emit text-only (ops:[]) when the user is explicitly asking a READ-ONLY
question. Read-only intents are narrow:
  explain / what is / why / how does / review only / is this correct /
  describe / tell me about / compare / what would happen if
Examples that stay text-only:
  "what does C3 do?"                    → explain its role; no edits
  "why is the LED on pin 3?"            → describe; no edits
  "review the schematic" (alone)        → text analysis is fine
But "review and fix" or "review and complete" → emit ops.

When the auto-summary lists defects (critical / high), and the user asks anything
remotely action-oriented, you MUST emit ops that resolve those defects.
Never describe a defect without proposing the op that fixes it.

If you genuinely cannot fix a defect because of missing information, say so in
`message` AND emit ops for everything you CAN fix in the same reply.

NEVER reply with both descriptive analysis AND ops:[] — that wastes the user's
turn. Either fix it (ops non-empty) or explain it (ops:[]) — pick one.

DESIGN PRINCIPLES & RULES (use these to judge what to add/change):

{METHODOLOGY}

{render_for_prompt()}
"""


from .json_utils import extract_json as _extract_json  # re-exported for server.py


_CONNECTIVITY_OP_NAMES = frozenset({"add_wire", "add_label", "add_junction"})


def _count_connectivity_ops(ops: List[Dict[str, Any]]) -> int:
    return sum(1 for op in ops
               if (op.get("op") or op.get("type")) in _CONNECTIVITY_OP_NAMES)


def _count_component_add_ops(ops: List[Dict[str, Any]]) -> int:
    """add_component ops that place REAL parts — power-port symbols and
    PWR_FLAGs (refdes prefixed with #) don't count as components for the
    orphan-retry threshold (a sheet of just #PWR ports isn't a circuit)."""
    n = 0
    for op in ops:
        if (op.get("op") or op.get("type")) != "add_component":
            continue
        ref = (op.get("reference") or "") or ""
        if ref.startswith("#"):
            continue
        n += 1
    return n


def run_connectivity_retry(
    client: ClaudeClient, schematic_path: str,
) -> Optional[List[Dict[str, Any]]]:
    """Synchronous Claude call for the connectivity-only retry. Returns the
    parsed ops list (with any stray add_component ops stripped) or None when
    the call fails / no usable ops come back. Safe to invoke from a worker
    thread — pure I/O, no event-loop access."""
    try:
        dump = format_dump_with_context(schematic_path)
    except Exception as exc:
        print(f"[chat] retry: dump failed ({type(exc).__name__}: {exc})",
              flush=True)
        return None

    user_payload = (
        f"{CONNECTIVITY_RETRY_PROMPT}\n\n"
        f"=== CURRENT SCHEMATIC ===\n{dump}"
    )
    emit_reply_tool = {
        "name": "emit_reply",
        "description": (
            "Emit the connectivity ops for the placed components. Only "
            "add_wire / add_label / add_junction are allowed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "ops": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["message", "ops"],
        },
    }
    try:
        with client.client.messages.stream(
            model=client.model,
            max_tokens=client.cfg.max_tokens,
            system=CHAT_SYSTEM_PROMPT,
            tools=[emit_reply_tool],
            tool_choice={"type": "tool", "name": "emit_reply"},
            messages=[{"role": "user", "content": user_payload}],
        ) as stream:
            resp = stream.get_final_message()
    except Exception as exc:
        print(f"[chat] retry: claude call raised "
              f"({type(exc).__name__}: {exc})", flush=True)
        return None

    parsed: Optional[Dict[str, Any]] = None
    for block in (getattr(resp, "content", None) or []):
        if (getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "emit_reply"):
            tin = block.input
            if isinstance(tin, dict):
                parsed = tin
            break
    if not parsed:
        print("[chat] retry: model did not call emit_reply tool", flush=True)
        return None

    ops = parsed.get("ops") or []
    cleaned = [op for op in ops
               if (op.get("op") or op.get("type")) != "add_component"]
    if len(cleaned) != len(ops):
        print(f"[chat] retry: stripped "
              f"{len(ops) - len(cleaned)} stray add_component op(s)",
              flush=True)
    return cleaned or None


def _format_op(op: Dict[str, Any]) -> str:
    name = op.get("op") or op.get("type") or "?"
    args = ", ".join(f"{k}={v!r}" for k, v in op.items() if k not in ("op", "type"))
    return f"{name}({args})"


class ChatSession:
    def __init__(self, path: str, model: Optional[str] = None, auto_apply: bool = False):
        self.path = path
        self.doc = SchematicDocument(path)
        self.client = ClaudeClient(model=model)
        self.history: List[Dict[str, str]] = []
        self.auto_apply = auto_apply

    def _current_dump(self) -> str:
        # Use the shared dump helper so the REPL AI sees the same PIN ENDPOINTS
        # + DETECTED DEFECTS the server-mode AI does. Without the defect tail,
        # the model has no feedback signal for layout-quality rule violations
        # and replicates the same cluttered output across iterations.
        return format_dump_with_context(self.path)

    # Tool-use schema. Forces Claude to emit the reply through a
    # structured tool call instead of free-form text + JSON parsing.
    # MUST match the server.py emit_reply schema so chat-REPL and
    # WebSocket paths produce identical replies.
    _EMIT_REPLY_TOOL = {
        "name": "emit_reply",
        "description": (
            "Emit the schematic edit reply. Always call this tool — never reply "
            "with prose. `message` is a 1-3 sentence summary; `ops` is the list "
            "of schematic operations (empty for pure-answer turns)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "ops": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["message", "ops"],
        },
    }

    def _ask(self, user_msg: str) -> Dict[str, Any]:
        """Return a {message, ops, parsed, raw, truncated} dict. Uses
        tool-use forced output — same constraint the server path uses,
        so test-mode replies match production-mode replies. Free-form
        JSON parsing is no longer in the path; truncation surfaces as
        truncated=True instead of a silent parse failure."""
        dump = self._current_dump()
        framed = f"=== CURRENT SCHEMATIC ===\n{dump}\n\n=== USER ===\n{user_msg}"
        self.history.append({"role": "user", "content": framed})
        messages = self.history[-12:]
        sys_arg = (
            [{"type": "text", "text": CHAT_SYSTEM_PROMPT,
              "cache_control": {"type": "ephemeral"}}]
            if self.client.cfg.enable_cache
            else CHAT_SYSTEM_PROMPT
        )
        with self.client.client.messages.stream(
            model=self.client.model,
            max_tokens=self.client.cfg.max_tokens,
            system=sys_arg,
            tools=[self._EMIT_REPLY_TOOL],
            tool_choice={"type": "tool", "name": "emit_reply"},
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()

        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        parsed_msg = ""
        parsed_ops: List[Dict[str, Any]] = []
        got_tool_use = False
        for block in (getattr(resp, "content", None) or []):
            if (getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "emit_reply"):
                tin = block.input if isinstance(block.input, dict) else {}
                parsed_msg = str(tin.get("message", "") or "")
                ops_val = tin.get("ops") or []
                if isinstance(ops_val, list):
                    parsed_ops = ops_val
                got_tool_use = True
                break

        raw_text = json.dumps({"message": parsed_msg, "ops": parsed_ops})
        self.history.append({"role": "assistant", "content": raw_text})
        return {
            "message": parsed_msg,
            "ops": parsed_ops,
            "parsed": got_tool_use,
            "truncated": truncated,
            "raw": raw_text,
        }

    def turn(self, user_msg: str) -> Dict[str, Any]:
        return self._ask(user_msg)

    def apply(self, ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results = []
        for op in ops:
            results.append({"op": _format_op(op), **apply_operation(self.doc, op)})
        if any(r.get("ok") for r in results):
            self.doc.save()
        return results

    def normalize_connectivity(self) -> Dict[str, Any]:
        """Geometric snap pass: move every off-pin wire endpoint onto the
        nearest pin tip (within tolerance). Returns stats from
        connectivity_normalize.normalize_wire_endpoints, or an error dict
        when the module can't run."""
        try:
            from .connectivity_normalize import normalize_wire_endpoints
            return normalize_wire_endpoints(self.doc)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}",
                    "endpoints_snapped": 0, "edges_after": -1}

    def retry_connectivity_if_needed(
        self, just_applied_ops: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Two-phase recovery for components-placed-but-disconnected sheets:

          1. Geometric snap (deterministic, no Claude call) — fixes the
             rotation-mismatch class where Claude wired the right pin but
             with stale coordinates.
          2. Connectivity-only retry (one Claude call) — fires ONLY when
             the snap pass leaves the graph with zero edges and ≥3 real
             components on the sheet. Catches the genuine "I forgot to
             wire" failure mode.

        Returns a flat summary so the caller can log/surface both phases.
        """
        summary: Dict[str, Any] = {
            "snap": None, "retry_attempted": False,
            "ops_returned": 0, "ops_applied": 0, "ops_total": 0,
        }
        comp_n = _count_component_add_ops(just_applied_ops)
        # Cheap pre-gate: a sheet with <3 components doesn't need either
        # phase — there's no meaningful connectivity to repair.
        if comp_n < 3:
            return summary

        snap_stats = self.normalize_connectivity()
        summary["snap"] = snap_stats
        edges_after_snap = snap_stats.get("edges_after", -1)
        if edges_after_snap > 0:
            # Snap recovered topology — done. No Claude call.
            return summary

        # Still flat-lined after snapping. Either no wires existed at all
        # (Claude truly emitted only components) or every wire endpoint was
        # too far from any pin to snap. The retry is the only remaining
        # recovery path.
        retry_ops = run_connectivity_retry(self.client, self.path)
        summary["retry_attempted"] = True
        if not retry_ops:
            return summary
        summary["ops_returned"] = len(retry_ops)
        retry_results = self.apply(retry_ops)
        summary["ops_applied"] = sum(1 for r in retry_results if r.get("ok"))
        summary["ops_total"] = len(retry_results)
        # One more snap pass — the retry may have emitted geometry-imperfect
        # wires too. Cheap to run, harmless if nothing to snap.
        summary["snap_after_retry"] = self.normalize_connectivity()
        return summary

    def undo(self) -> bool:
        if self.doc.undo():
            self.doc.save()
            return True
        return False


def repl(path: str, model: Optional[str] = None) -> None:
    session = ChatSession(path, model=model)
    print(f"kicad-claude chat | file: {path}")
    print("commands: /undo /quit | otherwise type your message")
    print()
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        if line == "/undo":
            print("undo:", "ok" if session.undo() else "nothing to undo")
            continue

        try:
            result = session.turn(line)
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            continue

        msg = result.get("message", "").strip()
        if msg:
            print(f"\nclaude> {msg}\n")
        ops = result.get("ops") or []
        if not ops:
            continue

        print("proposed ops:")
        for op in ops:
            print(f"  - {_format_op(op)}")
        ans = input("apply? [y/N] ").strip().lower()
        if ans != "y":
            print("(skipped)\n")
            continue
        results = session.apply(ops)
        for r in results:
            tag = "ok" if r.get("ok") else "FAIL"
            print(f"  [{tag}] {r['op']}: {r.get('message','')}")
        print()
