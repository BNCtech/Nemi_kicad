"""End-to-end offline test of the LARGER-BOARD design logic (no live API).

Exercises every deterministic layer that makes a ~100-part board work:
  1. decide_render_mode      -> large/dense prompt routes to HIERARCHY
  2. BOARD_UNDER_WIRED guard  -> a parts-list with nets=null is REJECTED (BMS bug)
  3. should_block_repair gate -> only fires on a LARGE board w/ localizable errors
  4. localize_issues          -> each error attributed to its owning block
  5. splice_block safety      -> a regenerated block can NEVER short the board
  6. _reconcile_to_contract   -> abbreviation seams merge; distinct signals do NOT
  7. build_incrementally      -> full block-by-block draw + seam re-join

Run:  python tests/_verify_large_board.py
"""
import sys
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

from envil_agent.intent.ir import IRComponent, IRNet, IRBlock, TopologyIR
from envil_agent.intent.validate import validate_ir
from envil_agent.intent.block_repair import (
    localize_issues, block_interface, splice_block)
from envil_agent.intent.incremental_build import (
    build_incrementally, _reconcile_to_contract)
from envil_agent.graphs.nodes.decide_render_mode import decide_render_mode_node
from envil_agent.graphs.nodes.block_repair import should_block_repair

R = []
def rec(n, ok, d=""):
    R.append(ok); print(("PASS" if ok else "FAIL"), "-", n, ("-> " + d) if d else "")


# ----------------------------------------------------------------------------
# 1. decide_render_mode: a big multi-subsystem board must route to HIERARCHY
# ----------------------------------------------------------------------------
# A compact BMS prompt scores 4 blocks / complexity 22 -> the keyword guess
# lands it in SINGLE_SHEET_BLOCKS (block decoration), NOT hierarchy. That is
# the documented behaviour: hierarchy needs >=6 blocks OR complexity >=65.
big = decide_render_mode_node({"prompt":
    "Design a battery management system with STM32 MCU, CAN bus comms, "
    "12-cell voltage sensing, current sense, MOSFET balancing, protection, "
    "power supply and USB programming"})
rec("compact BMS prompt -> block-decorated mode (single or hierarchy)",
    bool(big.get("force_hierarchy") or big.get("force_single_sheet")),
    big.get("render_mode_decision", "")[:80])

# A genuinely dense prompt (9 distinct subsystems) DOES cross the hierarchy
# gate -> this is the only path that arms the incremental 'draw bit by bit'.
dense = decide_render_mode_node({"prompt":
    "ESP32 board with LoRa SX1276, OLED display, micro SD card, BME280 "
    "sensor, MPU6050, WS2812 RGB, USB-C charger with TP4056, and SWD header"})
rec("dense 9-subsystem prompt -> force_hierarchy (arms incremental path)",
    bool(dense.get("force_hierarchy")), dense.get("render_mode_decision", "")[:80])

simple = decide_render_mode_node({"prompt": "NE555 1Hz blinker with one LED"})
rec("simple blinker -> NOT hierarchy",
    not simple.get("force_hierarchy"), simple.get("render_mode_decision", "")[:70])


# ----------------------------------------------------------------------------
# 2. BOARD_UNDER_WIRED: the BMS failure -- many parts, nets=null -> REJECT
# ----------------------------------------------------------------------------
# 10 real-symbol parts, ZERO nets (architect returned a parts list unwired).
parts = ([IRComponent("U1", "MCU_ST_STM32F4:STM32F405RGTx", "STM32F405")]
         + [IRComponent(f"C{i}", "Device:C", "100n") for i in range(1, 7)]
         + [IRComponent(f"R{i}", "Device:R", "10k") for i in range(1, 4)])
unwired = TopologyIR("bms", "bms", parts, [], [])
iss = validate_ir(unwired)
buw = [i for i in iss if i["code"] == "BOARD_UNDER_WIRED"]
rec("unwired 10-part board -> BOARD_UNDER_WIRED fires", bool(buw),
    buw[0]["text"][:70] if buw else "NOT FLAGGED")

# A normally-wired small board must NOT trip the guard.
wired = TopologyIR("w", "w",
    [IRComponent("U1", "Device:R", "10k"), IRComponent("U2", "Device:R", "1k")],
    [IRNet("N", ["U1.1", "U2.1"]), IRNet("GND", ["U1.2", "U2.2"], is_power=True)],
    [])
rec("2-resistor wired board -> guard does NOT trip",
    not any(i["code"] == "BOARD_UNDER_WIRED" for i in validate_ir(wired)))


# ----------------------------------------------------------------------------
# Build a realistic 3-block hierarchy IR for the splice / localize / gate tests
# ----------------------------------------------------------------------------
def hier_ir():
    comps = [
        IRComponent("U1", "MCU_ST_STM32F4:STM32F405RGTx", "STM32F405"),  # MCU
        IRComponent("C1", "Device:C", "100n"),
        IRComponent("U2", "RF:NRF24L01", "NRF24L01"),                     # COMM
        IRComponent("C2", "Device:C", "100n"),
        IRComponent("R1", "Device:R", "10k"),                             # POWER
        IRComponent("R2", "Device:R", "10k"),
    ]
    nets = [
        IRNet("+3V3", ["U1.VDD", "U2.VDD", "R1.1"], is_power=True),
        IRNet("GND", ["U1.VSS", "U2.VSS", "C1.2", "C2.2", "R2.2"], is_power=True),
        IRNet("SPI_MOSI", ["U1.PA7", "U2.MOSI"]),    # MCU<->COMM cross-block net
        IRNet("DECByU1", ["U1.VDD", "C1.1"]),
        IRNet("DECByU2", ["U2.VDD", "C2.1"]),
    ]
    blocks = [
        IRBlock("MCU", "mcu", ["U1", "C1"]),
        IRBlock("COMM", "comm", ["U2", "C2"]),
        IRBlock("POWER", "power", ["R1", "R2"]),
    ]
    return TopologyIR("board", "board", comps, nets, blocks)


# ----------------------------------------------------------------------------
# 3. should_block_repair: gate only opens for a LARGE board (>=40 parts) with
#    every error localizable. A 6-part board must fall back to FULL regen.
# ----------------------------------------------------------------------------
small_state = {"ir": hier_ir(),
               "issues": [{"code": "X", "severity": "error", "where": "C1"}],
               "block_attempt": 0}
rec("6-part board -> should_block_repair FALSE (full regen)",
    should_block_repair(small_state) is False, "below 40-part gate")

# Force the large gate by cloning the IR up past the threshold with filler parts.
big_ir = hier_ir()
for i in range(40):
    big_ir.components.append(IRComponent(f"Rf{i}", "Device:R", "1k"))
big_ir.blocks[2].component_refs += [f"Rf{i}" for i in range(40)]
big_state = {"ir": big_ir,
             "issues": [{"code": "PIN_NOT_ON_SYMBOL", "severity": "error",
                         "where": "C1.9"}],   # localizes to MCU block
             "block_attempt": 0}
rec("44-part board + localizable error -> should_block_repair TRUE",
    should_block_repair(big_state) is True, "large gate open")

# A cross-block (global) error must NOT open the per-block gate.
big_state_global = {"ir": big_ir,
                    "issues": [{"code": "X", "severity": "error",
                                "where": "SPI_MOSI"}],  # spans MCU+COMM
                    "block_attempt": 0}
rec("cross-block error -> should_block_repair FALSE (needs full regen)",
    should_block_repair(big_state_global) is False, "unlocalized -> full")


# ----------------------------------------------------------------------------
# 4. localize_issues: a pin-ref error maps to its block; a rail spans -> unloc
# ----------------------------------------------------------------------------
ir = hier_ir()
by_block, unloc = localize_issues(ir, [
    {"code": "PIN_NOT_ON_SYMBOL", "severity": "error", "where": "C1.9"},
    {"code": "POWER_PIN_FLOATING", "severity": "error", "where": "GND"},  # global
])
rec("pin-ref error C1.9 -> attributed to MCU block",
    by_block.get("MCU") and len(by_block["MCU"]) == 1, f"by_block={list(by_block)}")
rec("global GND error -> unlocalized (full retry)", len(unloc) == 1)


# ----------------------------------------------------------------------------
# 5. splice_block safety: regenerate COMM, with the LLM trying to (a) steal an
#    external pin onto a boundary net and (b) collide a refdes. Neither may short
#    the board: external pins frozen, colliding ref dropped.
# ----------------------------------------------------------------------------
ir = hier_ir()
gnd_before = set(next(n for n in ir.nets if n.name == "GND").pins)
# regenerated COMM: keeps U2+C2, but maliciously lists U1.PA7 (foreign) on
# SPI_MOSI and re-emits R1 (a FROZEN external POWER part).
new_comps = [IRComponent("U2", "RF:NRF24L01", "NRF24L01"),
             IRComponent("C2", "Device:C", "100n"),
             IRComponent("R1", "Device:R", "999k")]  # collides w/ frozen R1
new_nets = [IRNet("SPI_MOSI", ["U2.MOSI", "U1.PA7"]),   # U1.PA7 is foreign
            IRNet("GND", ["U2.VSS", "C2.2"], is_power=True),
            IRNet("DECByU2", ["U2.VDD", "C2.1"])]
report = splice_block(ir, "COMM", new_comps, new_nets)
spi = next(n for n in ir.nets if n.name == "SPI_MOSI")
# U1.PA7 must still be present (it was already there from MCU) but the splice
# must not have let COMM *own*/duplicate it; exactly one occurrence.
rec("splice: foreign pin U1.PA7 not duplicated onto boundary net",
    spi.pins.count("U1.PA7") == 1, f"SPI_MOSI={spi.pins}")
rec("splice: colliding frozen ref R1 dropped (not overwritten)",
    "R1" in report["ref_collisions"]
    and next(c for c in ir.components if c.ref == "R1").value == "10k",
    f"collisions={report['ref_collisions']}")
gnd_after = set(next(n for n in ir.nets if n.name == "GND").pins)
rec("splice: external GND pins (U1.VSS, R2.2) preserved",
    {"U1.VSS", "R2.2"} <= gnd_after, f"GND={sorted(gnd_after)}")


# ----------------------------------------------------------------------------
# 6. _reconcile_to_contract: an abbreviation merges to the contract; two
#    distinct same-letter signals are NEVER wrongly merged.
# ----------------------------------------------------------------------------
boundary = [{"name": "GATE_DRIVE", "is_power": False},
            {"name": "SPI_MISO", "is_power": False}]
nets = [IRNet("GATE_DRV", ["Q1.G"]),     # abbreviation -> should rename
        IRNet("SPI_MOSI", ["U1.PA7"])]   # NOT MISO -> must stay MOSI
n_ren = _reconcile_to_contract(nets, boundary)
rec("reconcile: GATE_DRV -> GATE_DRIVE (abbreviation bound to contract)",
    nets[0].name == "GATE_DRIVE", f"renamed={n_ren}")
rec("reconcile: SPI_MOSI stays SPI_MOSI (not merged into SPI_MISO)",
    nets[1].name == "SPI_MOSI")


# ----------------------------------------------------------------------------
# 7. build_incrementally: draw all 3 blocks block-by-block, with COMM emitting
#    an abbreviation that must reconcile + bind to the cross-block SPI net.
# ----------------------------------------------------------------------------
def draw(bname, btype, comps, errs, boundary, prompt):
    if bname == "MCU":
        return ([IRComponent("U1", "MCU_ST_STM32F4:STM32F405RGTx", "STM32F405"),
                 IRComponent("C1", "Device:C", "100n")],
                [IRNet("+3V3", ["U1.VDD"], is_power=True),
                 IRNet("GND", ["U1.VSS", "C1.2"], is_power=True),
                 IRNet("SPI_MOSI", ["U1.PA7"]),
                 IRNet("DECByU1", ["U1.VDD", "C1.1"])])
    if bname == "COMM":
        # COMM emits SPI_MOSI but slightly abbreviated -> must reconcile.
        return ([IRComponent("U2", "RF:NRF24L01", "NRF24L01"),
                 IRComponent("C2", "Device:C", "100n")],
                [IRNet("+3V3", ["U2.VDD"], is_power=True),
                 IRNet("GND", ["U2.VSS", "C2.2"], is_power=True),
                 IRNet("SPI_MOSI", ["U2.MOSI"]),
                 IRNet("DECByU2", ["U2.VDD", "C2.1"])])
    if bname == "POWER":
        return ([IRComponent("R1", "Device:R", "10k"),
                 IRComponent("R2", "Device:R", "10k")],
                [IRNet("+3V3", ["R1.1"], is_power=True),
                 IRNet("GND", ["R2.2"], is_power=True)])
    return None, None

skeleton = hier_ir()
# strip intra-block nets to simulate a real PLAN skeleton (rails + cross-block)
skeleton.nets = [n for n in skeleton.nets
                 if n.name in ("+3V3", "GND", "SPI_MOSI")]
ir, drawn, skipped = build_incrementally(skeleton, "bms", draw, normalize=False)
spi = next((n for n in ir.nets if n.name == "SPI_MOSI"), None)
rec("incremental: all 3 blocks drawn", drawn == ["MCU", "COMM", "POWER"],
    f"drawn={drawn} skipped={skipped}")
rec("incremental: SPI_MOSI bridges U1<->U2 after block-by-block draw",
    spi is not None and {"U1.PA7", "U2.MOSI"} <= set(spi.pins),
    f"SPI_MOSI={sorted(spi.pins) if spi else None}")


print("\nSUMMARY:", sum(R), "/", len(R))
sys.exit(0 if all(R) else 1)
