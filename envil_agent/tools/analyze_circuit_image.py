"""Tool: analyze a circuit image and extract topology or component info.

Two modes:
  circuit   (default) — full circuit: returns [FROM IMAGE] description for build_circuit
  component           — single IC/component: returns part_number + pin list for
                        create_symbol / create_component (no part number needed from user)
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from claude_agent_sdk import tool

# ── Circuit mode ─────────────────────────────────────────────────────────────

_CIRCUIT_SYSTEM = """\
You are an expert circuit analyst. Examine the provided circuit image carefully.
Your task is to extract the complete circuit topology and produce a plain-English
description that a KiCad schematic generator can use to draw the circuit.

Identify and list:
1. Every component visible — ICs, resistors, capacitors, transistors, diodes, LEDs,
   connectors, inductors, crystals, switches, transformers, fuses, etc.
   Include reference designators (R1, U1, C1 …) and values where visible.
   Where a value is unclear, estimate a sensible default and note the assumption.
2. How components are connected — which pins/terminals share a net.
3. Power rails (VCC, 5V, 3.3V, GND, VBAT …) and which pins they connect to.
4. Any net labels, annotations, or bus names visible in the image.

Output format (exactly):
  [FROM IMAGE]: <short circuit name>

  <description — component by component, one or two sentences each>

  Assumptions: <list any values or connections you inferred>

Keep the description under 600 words. Plain English only. No JSON, no markdown fences."""

# ── Component mode ────────────────────────────────────────────────────────────

_COMPONENT_SYSTEM = """\
You are an expert IC and electronic component analyst. Examine the provided image
(which may be a chip photo, a datasheet page, a PCB footprint diagram, a pad layout,
a schematic symbol screenshot, or a PCB close-up).

Your task is to extract every piece of information visible so a KiCad symbol and/or
footprint can be created with minimal input from the user.

Extract ALL of the following that are visible:
1. Part number / MPN — the full manufacturer part number (e.g. "NE555P", "STM32F103C8T6").
   If only a generic name is visible (e.g. "555 timer"), use that.
   If the image is a footprint/pad diagram with NO part marking, set to "".
2. Manufacturer — if visible.
3. Package type — DIP-8, SOIC-16, QFN-14, QFP-48, etc. Infer from pad layout if not labelled.
4. Body dimensions — width × height in mm if shown (e.g. "3.0×2.5").
5. Pin count — count pads/pins visible.
6. Pin pitch — spacing between pads in mm if shown or measurable.
7. Pin list — number, name, electrical type for each pin.
   For footprint diagrams where pin NAMES are not shown, set name to "P<number>"
   and type to "passive". Never skip this field — always emit all pads as pins.
8. Image type — one of: "chip_photo", "datasheet_page", "footprint_diagram",
   "schematic_symbol", "pcb_closeup", "other".
9. A one-sentence description of what the component is or does.

Output valid JSON only — no prose, no markdown fences:
{
  "part_number": "NE555P",
  "manufacturer": "Texas Instruments",
  "package": "DIP-8",
  "body_mm": "9.8×6.2",
  "pin_count": 8,
  "pin_pitch_mm": 2.54,
  "description": "Precision timer IC",
  "image_type": "datasheet_page",
  "pins": [
    {"number": "1", "name": "GND",  "type": "power_in"},
    {"number": "2", "name": "TRIG", "type": "input"},
    {"number": "3", "name": "OUT",  "type": "output"},
    {"number": "4", "name": "RESET","type": "input"},
    {"number": "5", "name": "CTRL", "type": "input"},
    {"number": "6", "name": "THR",  "type": "input"},
    {"number": "7", "name": "DIS",  "type": "output"},
    {"number": "8", "name": "VCC",  "type": "power_in"}
  ],
  "assumptions": "Pin names inferred from standard NE555 datasheet."
}

CRITICAL: always emit all pads as entries in pins[], even for footprint diagrams
where only pad positions are shown and no pin names are labelled. Use "P1","P2"…
as names and "passive" as type in that case. Never return an empty pins array."""

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _load_image(image_path: str):
    """Read image from disk and return (b64_data, mime_type) or raise."""
    p = Path(image_path)
    if not p.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")
    image_bytes = p.read_bytes()
    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    mime = _EXT_MIME.get(p.suffix.lower())
    if mime is None:
        guessed, _ = mimetypes.guess_type(str(p))
        mime = (guessed
                if guessed in ("image/png", "image/jpeg", "image/gif", "image/webp")
                else "image/png")
    return image_b64, mime


def _make_client():
    import anthropic
    return anthropic.Anthropic()


def _model() -> str:
    """Vision model for image analysis. Defaults to Sonnet 4.6 — far more
    accurate at reading pin tables, chip markings and pad layouts than Haiku,
    which is why image-based symbol creation was missing/misreading pins.

    An env override is honoured, EXCEPT Opus, which we deliberately never use
    here — a resolved Opus id falls back to Sonnet 4.6."""
    m = (
        os.environ.get("CLAUDE_MODEL_VISION")
        or os.environ.get("CLAUDE_MODEL_DEEP")
        or os.environ.get("ENVIL_MODEL")
        or "claude-sonnet-4-6"
    )
    return "claude-sonnet-4-6" if "opus" in m.lower() else m


@tool(
    name="analyze_circuit_image",
    description=(
        "Analyze an uploaded image and extract circuit or component information.\n\n"
        "mode='circuit' (default): full schematic image → plain-English circuit "
        "description prefixed with [FROM IMAGE] ready for build_circuit. Use when "
        "the user uploads a schematic/PCB/breadboard image and wants to build/draw/recreate it.\n\n"
        "mode='component': single IC/component image (chip photo, datasheet page, "
        "symbol screenshot) → extracts part_number, pin names, pin types as JSON. "
        "Use when the user uploads a component image and wants to create a symbol "
        "or footprint WITHOUT having to type the part number or find a datasheet URL."
    ),
    input_schema={
        "image_path": str,
        "mode": str,
        "extra_context": str,
    },
)
async def analyze_circuit_image(args: Dict[str, Any]) -> Dict[str, Any]:
    image_path = (args.get("image_path") or "").strip()
    mode = (args.get("mode") or "circuit").strip().lower()
    extra_context = (args.get("extra_context") or "").strip()

    if not image_path:
        return {
            "content": [{"type": "text", "text": "ERROR: image_path is required"}],
            "is_error": True,
        }

    try:
        image_b64, mime = _load_image(image_path)
    except FileNotFoundError as exc:
        return {
            "content": [{"type": "text", "text": f"ERROR: {exc}"}],
            "is_error": True,
        }
    except OSError as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: Could not read image file: {exc}"}],
            "is_error": True,
        }

    extra_note = f"\n\nExtra context from user: {extra_context}" if extra_context else ""

    if mode == "component":
        return await _analyze_component(image_b64, mime, extra_note, image_path)
    return await _analyze_circuit(image_b64, mime, extra_note, image_path)


async def _analyze_circuit(image_b64: str, mime: str,
                            extra_note: str, image_path: str) -> Dict[str, Any]:
    user_content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": mime, "data": image_b64}},
        {"type": "text",
         "text": ("Please analyze this circuit image and describe the full topology "
                  "so it can be reproduced as a KiCad schematic." + extra_note)},
    ]
    try:
        client = _make_client()
        resp = client.messages.create(
            model=_model(), max_tokens=1024, system=_CIRCUIT_SYSTEM,
            messages=[{"role": "user", "content": user_content}], timeout=60.0,
        )
        description = resp.content[0].text.strip()
        result = {
            "is_error": False,
            "mode": "circuit",
            "description": description,
            "image_path": image_path,
        }
        return {"content": [{"type": "text", "text": json.dumps(result)}],
                "is_error": False}
    except Exception as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: Vision analysis failed: "
                                  f"{type(exc).__name__}: {exc}"}],
            "is_error": True,
        }


async def _analyze_component(image_b64: str, mime: str,
                              extra_note: str, image_path: str) -> Dict[str, Any]:
    user_content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": mime, "data": image_b64}},
        {"type": "text",
         "text": ("Identify this component and extract its full pin list as JSON "
                  "so a KiCad symbol can be created automatically." + extra_note)},
    ]
    try:
        client = _make_client()
        resp = client.messages.create(
            model=_model(), max_tokens=1024, system=_COMPONENT_SYSTEM,
            messages=[{"role": "user", "content": user_content}], timeout=60.0,
        )
        raw = resp.content[0].text.strip()

        # Parse the JSON the model returned
        try:
            component_info = json.loads(raw)
        except json.JSONDecodeError:
            # Model may have wrapped in fences — strip and retry
            import re
            cleaned = re.sub(r"^```[a-z]*\n?|```$", "", raw, flags=re.MULTILINE).strip()
            component_info = json.loads(cleaned)

        result = {
            "is_error": False,
            "mode": "component",
            "part_number": component_info.get("part_number", ""),
            "manufacturer": component_info.get("manufacturer", ""),
            "package": component_info.get("package", ""),
            "body_mm": component_info.get("body_mm", ""),
            "pin_count": component_info.get("pin_count", 0),
            "pin_pitch_mm": component_info.get("pin_pitch_mm", 0),
            "description": component_info.get("description", ""),
            "image_type": component_info.get("image_type", "other"),
            "pins": component_info.get("pins", []),
            "assumptions": component_info.get("assumptions", ""),
            "image_path": image_path,
        }
        return {"content": [{"type": "text", "text": json.dumps(result)}],
                "is_error": False}

    except json.JSONDecodeError as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: Could not parse component JSON from vision model: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: Vision analysis failed: "
                                  f"{type(exc).__name__}: {exc}"}],
            "is_error": True,
        }
