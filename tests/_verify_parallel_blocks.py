"""Parallel block-draw — proves the gated concurrent draw path (build_graph.
parallel_block_draw) produces the SAME board as the sequential loop, actually
runs the draws concurrently, splices in plan order, and still re-joins seams.

Run:  python tests/_verify_parallel_blocks.py     # exit 0 = all pass
"""
import sys, time, threading
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

from envil_agent.intent.ir import IRComponent, IRNet, IRBlock, TopologyIR
from envil_agent.intent import incremental_build as IB
from envil_agent.intent.incremental_build import build_incrementally

R = []
def rec(n, ok, d=""):
    R.append(ok); print(("PASS" if ok else "FAIL"), "—", n, ("— " + d) if d else "")


_BLOCKS = [("POWER", "regulator"), ("BMS", "sensor"), ("MCU", "mcu"),
           ("COMM", "comm"), ("SENSOR", "analog"), ("OUTPUT", "driver")]


def plan():
    comps, nets, blks = [], [], []
    # one IC per block + a shared rail + a cross-block seam POWER<->MCU
    for i, (bn, bt) in enumerate(_BLOCKS, 1):
        comps.append(IRComponent(f"U{i}", bn, f"U{i}"))
        blks.append(IRBlock(bn, bt, [f"U{i}"]))
    nets.append(IRNet("+3V3", [f"U{i}.1" for i in range(1, len(_BLOCKS) + 1)],
                      is_power=True))
    nets.append(IRNet("EN_NET", ["U1.EN", "U3.PA0"]))   # POWER(U1) <-> MCU(U3) seam
    return TopologyIR("x", "x", comps, nets, blks)


# concurrency probe: count how many draws are in-flight at once
_live = {"n": 0, "peak": 0}
_lk = threading.Lock()

def make_draw(sleep_s):
    def draw(bname, btype, comps, errs, boundary, prompt):
        with _lk:
            _live["n"] += 1
            _live["peak"] = max(_live["peak"], _live["n"])
        try:
            time.sleep(sleep_s)
            i = next(c[0] for c in comps)  # the U-ref of this block
            # MCU deliberately DROPS its EN_NET endpoint -> seam re-join must fix it
            nets = [IRNet("+3V3", [f"{i}.1"], is_power=True)]
            if bname != "MCU":
                if bname == "POWER":
                    nets.append(IRNet("EN_NET", [f"{i}.EN"]))
            else:
                pass  # MCU drops EN_NET on purpose
            # an internal node so the block isn't empty
            nets.append(IRNet(f"{bname}_INT", [f"{i}.2"]))
            return ([IRComponent(i, bname, i)], nets)
        finally:
            with _lk:
                _live["n"] -= 1
    return draw


def run(parallel, sleep_s):
    _live["peak"] = 0
    orig = IB._parallel_cfg
    IB._parallel_cfg = (lambda: (True, 8)) if parallel else (lambda: (False, 6))
    try:
        t0 = time.time()
        ir, drawn, skipped = build_incrementally(plan(), "x", make_draw(sleep_s),
                                                  normalize=False)
        dt = time.time() - t0
    finally:
        IB._parallel_cfg = orig
    adj = {n.name: tuple(sorted(n.pins)) for n in ir.nets}
    return drawn, skipped, adj, dt, _live["peak"]


# sequential baseline
seq_drawn, seq_skip, seq_adj, seq_dt, seq_peak = run(False, 0.20)
# parallel
par_drawn, par_skip, par_adj, par_dt, par_peak = run(True, 0.20)

rec("same drawn set + plan order", seq_drawn == par_drawn,
    f"seq={seq_drawn} par={par_drawn}")
rec("same skipped set", seq_skip == par_skip, f"seq={seq_skip} par={par_skip}")
rec("IDENTICAL net adjacency (same board)", seq_adj == par_adj,
    "" if seq_adj == par_adj else
    f"diff={ {k: (seq_adj.get(k), par_adj.get(k)) for k in set(seq_adj)|set(par_adj) if seq_adj.get(k)!=par_adj.get(k)} }")
rec("sequential ran ONE draw at a time", seq_peak == 1, f"peak={seq_peak}")
rec("parallel ran draws CONCURRENTLY (peak>1)", par_peak > 1,
    f"peak={par_peak} of {len(_BLOCKS)}")
rec("parallel WALL-TIME well under sequential", par_dt < seq_dt * 0.6,
    f"seq={seq_dt:.2f}s par={par_dt:.2f}s")
# seam: POWER(U1.EN) <-> MCU(U3.PA0) must still bridge despite MCU dropping it
seam = par_adj.get("EN_NET", ())
rec("cross-block seam re-joined in parallel path",
    "U1.EN" in seam and "U3.PA0" in seam, f"EN_NET={seam}")

# HIGH-fix: a worker raising a BaseException (MemoryError) must degrade to ONE
# skipped block, NOT abort the whole build (the "Never raises" contract).
def boom_draw(bname, btype, comps, errs, boundary, prompt):
    if bname == "MCU":
        raise MemoryError("simulated worker fault")
    i = next(c[0] for c in comps)
    return ([IRComponent(i, bname, i)],
            [IRNet("+3V3", [f"{i}.1"], is_power=True),
             IRNet(f"{bname}_INT", [f"{i}.2"])])

_orig = IB._parallel_cfg
IB._parallel_cfg = lambda: (True, 8)
crashed = False
try:
    ir_b, drawn_b, skip_b = build_incrementally(plan(), "x", boom_draw,
                                                normalize=False)
except BaseException as e:                  # noqa: BLE001
    crashed = True; drawn_b, skip_b = [], [f"RAISED {e!r}"]
finally:
    IB._parallel_cfg = _orig
rec("worker BaseException -> build still returns (never raises)", not crashed)
rec("faulting block skipped, the OTHER blocks still drawn",
    not crashed and "MCU" in skip_b and len(drawn_b) == len(_BLOCKS) - 1,
    f"drawn={drawn_b} skipped={skip_b}")

print("\nSUMMARY:", sum(R), "/", len(R))
sys.exit(0 if all(R) else 1)
