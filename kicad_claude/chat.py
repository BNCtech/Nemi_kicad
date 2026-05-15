import json
import re
import sys
from typing import Any, Dict, List, Optional

from .claude_client import ClaudeClient
from .rules import METHODOLOGY, render_for_prompt
from .schematic_extractor import SchematicExtractor
from .schematic_modifier import SchematicDocument, apply_operation


CHAT_SYSTEM_PROMPT = f"""You are an electronics engineer assistant editing a KiCAD schematic in collaboration with the user.

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
    {{"op": "delete_junction",     "x": 75.0, "y": 60.0}}
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


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    candidates = []
    m = _JSON_FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    candidates.append(text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


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
        return SchematicExtractor(self.path).format_for_claude()

    def _ask(self, user_msg: str) -> str:
        dump = self._current_dump()
        framed = f"=== CURRENT SCHEMATIC ===\n{dump}\n\n=== USER ===\n{user_msg}"
        self.history.append({"role": "user", "content": framed})
        messages = self.history[-12:]
        resp = self.client.client.messages.create(
            model=self.client.model,
            max_tokens=self.client.cfg.max_tokens,
            system=(
                [{"type": "text", "text": CHAT_SYSTEM_PROMPT,
                  "cache_control": {"type": "ephemeral"}}]
                if self.client.cfg.enable_cache
                else CHAT_SYSTEM_PROMPT
            ),
            messages=messages,
        )
        text = resp.content[0].text
        self.history.append({"role": "assistant", "content": text})
        return text

    def turn(self, user_msg: str) -> Dict[str, Any]:
        raw = self._ask(user_msg)
        parsed = _extract_json(raw)
        if not parsed:
            return {"message": raw, "ops": [], "raw": raw, "parsed": False}
        parsed.setdefault("ops", [])
        parsed.setdefault("message", "")
        parsed["raw"] = raw
        parsed["parsed"] = True
        return parsed

    def apply(self, ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results = []
        for op in ops:
            results.append({"op": _format_op(op), **apply_operation(self.doc, op)})
        if any(r.get("ok") for r in results):
            self.doc.save()
        return results

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
