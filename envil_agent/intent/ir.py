"""TopologyIR — what the architect (Claude) emits and the engine consumes.

The IR is intentionally tiny: list of components, list of nets, optional
list of blocks (only when hierarchy is needed). Every other concern
(positions, wires, sheet partitioning, label justify) is the engine's
problem — the IR carries WHAT, not WHERE.

Pin references use the form ``<ref>.<pin>`` where ``<pin>`` is either a
pin NUMBER (``U1.8``) or a pin NAME (``U1.VCC``); the engine resolves
either through SymbolGeom.resolve_pin().
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class IRComponent:
    ref: str            # "U1", "R1", "C1" — must be unique
    lib_id: str         # "Timer:NE555", "Device:R"
    value: str          # "NE555", "47k", "100n"
    footprint: str = ""  # optional, leave blank for now

    @classmethod
    def from_dict(cls, d: dict) -> "IRComponent":
        return cls(ref=d["ref"], lib_id=d["lib_id"],
                   value=d.get("value", ""), footprint=d.get("footprint", ""))


@dataclass
class IRNet:
    name: str                # "+5V", "GND", "TIMING", "OUT"
    pins: List[str]          # ["U1.8", "C1.1", "R1.1"]
    is_power: bool = False   # globals + PWR_FLAG if true

    @classmethod
    def from_dict(cls, d: dict) -> "IRNet":
        return cls(name=d["name"], pins=list(d["pins"]),
                   is_power=bool(d.get("is_power", False)))


@dataclass
class IRBlock:
    """One functional block — becomes one child sheet when hierarchy is on.

    `flow_role` and `anchor_pins` are optional architect-provided layout
    hints (Phase 2). When empty, the engine derives defaults from
    `block_type` + component analysis. Architects emit them when they want
    to override the default — e.g. an "io" block that's actually the power
    input (USB-C) needs `flow_role="source"` to land on the left."""
    name: str                                       # "POWER", "MCU", "USB"
    block_type: str                                 # "power" | "mcu" | "io" | "comm" | "sense" | ...
    component_refs: List[str]                       # which components belong to this block
    flow_role: str = ""                             # "source" | "regulator" | "compute" | "sink" — left-to-right ordering hint
    anchor_pins: List[str] = field(default_factory=list)  # IC pins for incoming-power proximity (e.g. ["U2.VDD"])

    @classmethod
    def from_dict(cls, d: dict) -> "IRBlock":
        return cls(
            name=d["name"],
            block_type=d.get("block_type", "generic"),
            component_refs=list(d.get("component_refs", [])),
            flow_role=d.get("flow_role", ""),
            anchor_pins=list(d.get("anchor_pins", [])),
        )


@dataclass
class TopologyIR:
    name: str                            # human-readable circuit name
    circuit_type: str                    # "OSCILLATOR" | "REGULATOR" | "MCU_BOARD" | ...
    components: List[IRComponent] = field(default_factory=list)
    nets: List[IRNet] = field(default_factory=list)
    blocks: List[IRBlock] = field(default_factory=list)
    # P2/R9: bus declarations. Optional; default empty preserves
    # backwards-compatibility with every IR emitted before this field
    # existed. Engine render-side integration is a follow-up
    # (intent/engine.py needs to suppress per-net labels for bus
    # members and emit bus entries at the breakout points).
    buses: List[Any] = field(default_factory=list)
    notes: str = ""                      # architect's design rationale (debug aid)

    @classmethod
    def from_dict(cls, d: dict) -> "TopologyIR":
        # Lazy import keeps bus_ir optional --- old IRs without `buses`
        # keep parsing even if `intent.bus_ir` becomes unavailable.
        bus_objs: List[Any] = []
        if d.get("buses"):
            try:
                from .bus_ir import BusDef
                bus_objs = [BusDef.from_dict(b) for b in d.get("buses", [])]
            except Exception:
                bus_objs = []
        return cls(
            name=d.get("name", "unnamed"),
            circuit_type=d.get("circuit_type", "UNKNOWN"),
            components=[IRComponent.from_dict(c) for c in d.get("components", [])],
            nets=[IRNet.from_dict(n) for n in d.get("nets", [])],
            blocks=[IRBlock.from_dict(b) for b in d.get("blocks", [])],
            buses=bus_objs,
            notes=d.get("notes", ""),
        )

    @classmethod
    def from_json(cls, raw: str) -> "TopologyIR":
        return cls.from_dict(json.loads(raw))

    def to_dict(self) -> dict:
        return asdict(self)

    def component_by_ref(self, ref: str) -> Optional[IRComponent]:
        for c in self.components:
            if c.ref == ref:
                return c
        return None
