"""Forward incremental ("draw bit by bit") construction for LARGE boards.

Asking the architect to wire a ~100-part board in ONE emit_ir call overflows the
model's reliable output length -> context rot / lost-in-the-middle -> a tangled,
under-wired board (the BMS failure). Instead we draw the board the way a human
does -- one block at a time:

  1. PLAN   -- one small call emits the SKELETON: components[], blocks[], and ONLY
     the boundary nets (global power rails + inter-block signals). No dense
     intra-block wiring, so the plan stays small and reliable even at 100 parts.
  2. DRAW   -- for EACH block in turn, a focused call draws THAT block's internal
     wiring against the frozen boundary interface, and the result is spliced into
     the accumulating IR. The board grows one block at a time.
  3. ASSEMBLE -- normalise the seams.

Every LLM call stays small (one block ~5 parts), so the board can be arbitrarily
large without any single generation degrading. Reuses the proven per-block
machinery (block_interface + splice_block + _architect_block_call). The block
draw fn is injected so this orchestrator is unit-testable with no live API.

Gated by build_graph.incremental_large_board; only taken for boards the size gate
([[block_repair_min_components]]) marks as large. Strictly additive -- small and
medium boards keep the one-shot architect path unchanged.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from .block_repair import block_interface, splice_block


# --------------------------------------------------------------------------
# Seam reconciliation -- bind a block's emitted net names back to the interface
# CONTRACT so independently-drawn blocks always meet at the same signal.
# Research basis: a runtime validation layer at each block's output boundary that
# reconciles (not naively retries) names that violate the declared interface
# (see VeriGraphi / Agent Behavioral Contracts). Deterministic, no extra LLM.
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def _is_subseq(short: str, lng: str) -> bool:
    """True when every char of `short` appears in order within `lng` -- i.e.
    `short` is an ABBREVIATION/truncation of `lng` (DRV<-DRIVE, CLK<-CLOCK).
    This is the safe discriminator: SPI_MOSI is NOT a subsequence of SPI_MISO
    (same letters, reordered), so distinct signals are never merged, while a
    real abbreviation always is."""
    it = iter(lng)
    return all(ch in it for ch in short)


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _reconcile_to_contract(new_nets: list, boundary_nets: list) -> int:
    """Rename any block-emitted SIGNAL net whose name is a case/underscore or
    ABBREVIATION variant of a contracted boundary signal back to the EXACT
    contract name, so splice_block binds it to the shared net. Conservative:
      - power rails are left alone (they bind by exact canonical name already);
      - an exact normalized match -> canonicalise to the contract's raw name
        (fixes case/underscore seams: gate_drive -> GATE_DRIVE);
      - else a SUBSEQUENCE match with a >=3-char shared prefix and a UNIQUE
        candidate -> rename (GATE_DRV -> GATE_DRIVE). Ambiguous / non-subsequence
        names (SPI_MOSI vs SPI_MISO) are LEFT untouched -- never a wrong merge."""
    sig_contracts = [b["name"] for b in (boundary_nets or [])
                     if not b.get("is_power")]
    if not sig_contracts:
        return 0
    norm = {c: _norm(c) for c in sig_contracts}
    renamed = 0
    for net in new_nets or []:
        if getattr(net, "is_power", False):
            continue
        nnm = _norm(net.name)
        if not nnm:
            continue
        # 1) exact normalized match -> canonicalise to the contract's raw name.
        exact = next((c for c in sig_contracts if norm[c] == nnm), None)
        if exact is not None:
            if net.name != exact:
                net.name = exact
                renamed += 1
            continue
        # 2) abbreviation / truncation match (subsequence both ways).
        cands = []
        for c in sig_contracts:
            cn = norm[c]
            short, lng = (nnm, cn) if len(nnm) <= len(cn) else (cn, nnm)
            if (len(short) >= 3
                    and _common_prefix_len(nnm, cn) >= 3
                    and _is_subseq(short, lng)):
                cands.append(c)
        if len(cands) == 1:
            net.name = cands[0]
            renamed += 1
    return renamed


# Signature of the per-block draw fn (build_circuit._architect_block_call):
#   (block_name, block_type, components, errors, boundary_nets, prompt)
#   -> (IRComponent[] | None, IRNet[] | None)
BlockDrawFn = Callable[..., Tuple[Optional[list], Optional[list]]]


def _reassert_plan_seams(ir, planned: dict) -> int:
    """JOIN + VERIFY the cross-block connections.

    `planned` = {net_name: (is_power, [pins])} snapshotted from the PLAN BEFORE
    any block was drawn. The plan's nets ARE the cross-block contract -- the
    rails and the inter-block signals (U1<->U2). When block 2 is regenerated it
    can accidentally drop its endpoint of such a net, silently breaking the
    seam. This re-adds every planned pin that went missing (recreating a net a
    block deleted entirely), so EVERY U1<->U2 connection the plan declared is
    actually present after assembly -- verified, not assumed. Returns the number
    of endpoints re-joined (0 = every seam survived intact)."""
    from .ir import IRNet
    # GLOBAL pin-ownership guard (gated, default on). A planned seam pin is
    # re-added ONLY when it is currently on NO net (genuinely DROPPED by a block
    # redraw -- the case this function exists to repair). A pin a block already
    # wired to some net was NOT dropped; re-adding it to the planned net would
    # put one physical pin on two nets -- the exact way the incremental path
    # MANUFACTURES PIN_IN_MULTIPLE_NETS. So we never re-add an already-owned pin.
    # Off -> legacy always-re-add behaviour (byte-identical).
    try:
        from .engine import _load_layout_config as _lc_so
        _own = bool((_lc_so().get("build_graph") or {}).get(
            "seam_reassert_ownership", True))
    except Exception:
        _own = True
    owned: set = set()
    if _own:
        for n in ir.nets:
            owned.update(n.pins)
    by_name = {n.name: n for n in ir.nets}
    rejoined = 0
    for name, (is_power, pins) in planned.items():
        net = by_name.get(name)
        if net is None:
            net = IRNet(name=name, pins=[], is_power=is_power)
            ir.nets.append(net)
            by_name[name] = net
        have = set(net.pins)
        for p in pins:
            if p in have:
                continue
            if _own and p in owned:
                continue            # already wired elsewhere -> never double-list
            net.pins.append(p)
            have.add(p)
            owned.add(p)            # claim it so a later seam can't re-add it
            rejoined += 1
    return rejoined


def _block_audit_cfg():
    """(enabled, max_attempts) from layout_config.json:build_graph. Default
    (True, 3). Off -> the block is spliced as first drawn (byte-identical)."""
    try:
        from .engine import _load_layout_config
        bg = _load_layout_config().get("build_graph", {}) or {}
        return (bool(bg.get("block_audit", True)),
                max(1, int(bg.get("block_audit_attempts", 3))))
    except Exception:
        return (True, 3)


def _parallel_cfg():
    """(enabled, workers) from layout_config.json:build_graph. Default
    (False, 6). When enabled the independent per-block DRAWS run concurrently
    (splice stays serial). Off -> the sequential loop runs, byte-identical."""
    try:
        from .engine import _load_layout_config
        bg = _load_layout_config().get("build_graph", {}) or {}
        return (bool(bg.get("parallel_block_draw", False)),
                max(2, int(bg.get("parallel_block_workers", 3))))
    except Exception:
        return (False, 3)


def _audit_block(new_comps, new_nets, boundary_nets) -> list:
    """Validate a freshly-drawn block IN ISOLATION and return the block-local
    ERROR issues it can fix on a redraw: a pin name that isn't on the symbol
    (PIN_NOT_ON_SYMBOL), a power pin wired to NOTHING (POWER_PIN_FLOATING), or a
    pin the block put on two of its own nets (PIN_IN_MULTIPLE_NETS).

    Builds a sub-IR from the block's own components + its internal nets + the
    boundary rails' MY-side pins (so a power pin that lands on +3V3 isn't a false
    floater). Excludes external (other-block) pins to avoid PIN_UNKNOWN_REF, and
    deliberately does NOT report NET_FLOATING (a boundary signal looks one-ended
    in isolation but isn't). Reuses validate_ir -> the PIN_NOT_ON_SYMBOL issues
    already carry the real available_pins, which the redraw prompt renders. Never
    raises (returns [] on any failure) so the audit can't break a build."""
    try:
        from .ir import TopologyIR
        from .validate import validate_ir, dedupe_issues
        comps = [{"ref": c.ref, "lib_id": c.lib_id, "value": c.value}
                 for c in (new_comps or [])]
        # Merge nets BY NAME (union pins, dedupe) — a net name is ONE electrical
        # node, exactly as splice_block assembles the real board. The block's OWN
        # drawn rail (+3V3/GND/a named signal) and the boundary net of the SAME
        # name are the SAME net; listing them as two separate nets made every
        # shared pin look like it sat on "multiple nets" (['+3V3','+3V3']) and
        # produced a FLOOD of FALSE PIN_IN_MULTIPLE_NETS that no redraw could
        # clear — silently holding out cap-heavy blocks (BQ76952/STM32/TJA1051,
        # many +3V3/GND decouplers). Merging here matches the assembled board, so
        # only a pin on two DIFFERENTLY-named nets (a real conflict) still flags.
        _by_name: Dict[str, Dict[str, Any]] = {}

        def _slot(_name, _is_power):
            s = _by_name.get(_name)
            if s is None:
                s = {"name": _name, "pins": [], "is_power": bool(_is_power)}
                _by_name[_name] = s
            elif _is_power:
                s["is_power"] = True
            return s

        for n in (new_nets or []):
            _slot(n.name, getattr(n, "is_power", False))["pins"].extend(n.pins)
        for b in (boundary_nets or []):
            _slot(b["name"], b.get("is_power"))["pins"].extend(b.get("my_pins", []))
        nets = []
        for _s in _by_name.values():
            _s["pins"] = list(dict.fromkeys(_s["pins"]))   # dedupe, keep order
            nets.append(_s)
        sub = TopologyIR.from_dict({"name": "_block", "circuit_type": "x",
                                    "components": comps, "nets": nets})
        keep = {"PIN_NOT_ON_SYMBOL", "PIN_IN_MULTIPLE_NETS", "POWER_PIN_FLOATING"}
        return [i for i in dedupe_issues(validate_ir(sub))
                if i.get("severity") == "error" and i.get("code") in keep]
    except Exception:                          # noqa: BLE001 - audit never blocks
        return []


def build_incrementally(
    plan_ir: Any,
    prompt: str,
    block_draw: BlockDrawFn,
    *,
    normalize: bool = True,
    on_block: Optional[Callable[[str, bool, int, int], None]] = None,
) -> Tuple[Any, List[str], List[str]]:
    """Draw a planned board block-by-block, mutating ``plan_ir`` in place.

    ``plan_ir`` is the SKELETON IR from the plan call: its ``components`` and
    ``blocks`` are populated and its ``nets`` hold ONLY the boundary nets (rails +
    inter-block signals). For each block we compute its frozen interface from the
    CURRENT IR (so a later block sees the pins earlier blocks already attached to
    a shared net), draw the block's internal wiring with ``block_draw``, and
    splice it back. Returns ``(ir, drawn_blocks, skipped_blocks)``.

    Never raises: a block whose draw fails keeps its skeleton (boundary stubs)
    and is reported in ``skipped`` -- the normal validate/repair loop downstream
    then sees only that block's residual, not a whole-board failure.

    ``on_block(name, ok, idx, total)`` is an OPTIONAL progress hook called once
    per block as it resolves (``ok`` = drew+spliced cleanly). It lets the caller
    stream live "drew block 3/8" status to the chat. ``None`` (default) makes the
    loop byte-identical to before; the hook is best-effort (its exceptions are
    swallowed) so a flaky sink can never break a build.
    """
    drawn: List[str] = []
    skipped: List[str] = []
    _blocks = list(getattr(plan_ir, "blocks", []) or [])
    _total = len(_blocks)

    def _emit(name: str, ok: bool, idx: int) -> None:
        if on_block is None:
            return
        try:
            on_block(name, ok, idx, _total)
        except Exception:              # noqa: BLE001 - a flaky sink never breaks a build
            pass
    # Subset of `skipped` that are GENUINE draw/splice FAILURES (interface
    # error, block_draw gave nothing after retries, or splice raised) — as
    # opposed to an intentionally-empty plan block. Stamped onto the IR below
    # (gated) so validate can escalate them to repair instead of letting an
    # unwired block ship green. Kept separate so the empty-block case is NOT
    # hard-failed.
    _draw_failed: List[str] = []

    # Per-block draw retry. A transient block_draw failure (180s timeout,
    # Anthropic 529 overload, or an over-budget block whose decoder closes the
    # JSON empty) otherwise SILENTLY drops that block's entire intra-block
    # wiring with no error raised -- the board then ships under-wired and
    # validate can't see it (a dense monitor block's cell-sense pins are
    # `input`, not power, so POWER_PIN_FLOATING / BOARD_UNDER_WIRED stay quiet).
    # One cheap redraw recovers the common transient case. Config-driven; set
    # build_graph.block_draw_attempts to 1 to restore single-attempt behaviour.
    try:
        from .engine import _load_layout_config
        _bg = _load_layout_config().get("build_graph", {}) or {}
        _draw_attempts = max(1, int(_bg.get("block_draw_attempts", 2)))
    except Exception:
        _draw_attempts = 2

    # Snapshot the PLAN's cross-block contract (rails + inter-block signals)
    # BEFORE drawing, so we can re-join any seam a block drops during redraw.
    planned_seams = {
        n.name: (bool(getattr(n, "is_power", False)), list(n.pins))
        for n in (getattr(plan_ir, "nets", []) or [])
    }

    # PARALLEL DRAW (gated build_graph.parallel_block_draw, default off). The
    # per-block draws are INDEPENDENT — each block's interface is the plan's
    # frozen boundary contract and _reassert_plan_seams (below) re-joins every
    # cross-block seam afterwards — so we draw them CONCURRENTLY and keep only
    # the SPLICE serial (it mutates the shared plan_ir). Wall-time collapses
    # from sum-of-blocks to ~slowest single block. Same drawn/skipped semantics
    # as the sequential loop; the @traceable parent ctx is copied into each
    # worker so "Fix block" spans still nest. _did_parallel skips the loop below
    # (the loop stays byte-identical for the default sequential path).
    _did_parallel = False
    _par_on, _par_workers = _parallel_cfg()
    if _par_on and _total > 1:
        import contextvars
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _audit_on, _audit_max = _block_audit_cfg()
        _tries = max(_draw_attempts, _audit_max) if _audit_on else _draw_attempts

        def _draw_block(block):
            """Draw ONE block in isolation; no plan_ir mutation (splice is serial
            below). Returns (iface, new_comps, new_nets, status). FULLY guarded:
            any failure (interface, malformed comps, draw) -> a fail status, never
            an escaping exception (so one worker fault never aborts the build)."""
            try:
                iface = block_interface(plan_ir, block)
                comps = [(c.ref, c.lib_id, c.value) for c in iface["components"]]
                if not comps:
                    return (iface, None, None, "empty")
                new_comps, new_nets, block_errs = None, None, []
                for _t in range(_tries):
                    try:
                        new_comps, new_nets = block_draw(
                            block.name, getattr(block, "block_type", "") or "",
                            comps, block_errs, iface["boundary_nets"], prompt)
                    except Exception:      # noqa: BLE001
                        new_comps, new_nets = None, None
                    if not new_comps:
                        continue
                    if not _audit_on:
                        break
                    block_errs = _audit_block(new_comps, new_nets,
                                              iface["boundary_nets"])
                    if not block_errs:
                        break
                return (iface, new_comps, new_nets, "drawn")
            except Exception:              # noqa: BLE001
                return (None, None, None, "iface_fail")

        # DRAW all blocks concurrently; collect via as_completed so the on_block
        # progress hook fires LIVE as each block finishes (not in a burst after
        # the join -> preserves the anti-black-box heartbeat the WS-timeout
        # defense relies on). Per-future guard catches a BaseException (e.g.
        # MemoryError) so a single worker fault degrades to ONE skipped block
        # instead of discarding the whole board (the "Never raises" contract).
        _res_by_name: Dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=_par_workers) as _ex:
            _f2b = {}
            for _b in _blocks:
                _ctx = contextvars.copy_context()   # preserve @traceable parent
                _f2b[_ex.submit(_ctx.run, _draw_block, _b)] = _b
            _done = 0
            for _f in as_completed(_f2b):
                _b = _f2b[_f]
                try:
                    _res = _f.result()
                except BaseException:      # noqa: BLE001 - one fault = one skip
                    _res = (None, None, None, "iface_fail")
                _res_by_name[_b.name] = _res
                _done += 1
                _emit(_b.name, bool(_res[1]), _done)   # live "drew N/total"

        # SPLICE serially, in PLAN order (mutates plan_ir; order kept deterministic
        # so the assembled board is identical to the sequential path).
        for block in _blocks:
            iface, new_comps, new_nets, _status = _res_by_name.get(
                block.name, (None, None, None, "iface_fail"))
            if _status == "iface_fail":
                skipped.append(block.name)
                _draw_failed.append(block.name)
                continue
            if _status == "empty":
                skipped.append(block.name)
                continue
            if new_comps:
                try:
                    _reconcile_to_contract(new_nets or [], iface["boundary_nets"])
                    _sp_rep = splice_block(plan_ir, block.name, new_comps,
                                           new_nets or [])
                    _cr = (_sp_rep or {}).get("collision_renames") or []
                    if _cr:
                        setattr(plan_ir, "_cross_block_collisions",
                                (getattr(plan_ir, "_cross_block_collisions", None)
                                 or []) + _cr)
                    drawn.append(block.name)
                except Exception:          # noqa: BLE001
                    skipped.append(block.name)
                    _draw_failed.append(block.name)
            else:
                skipped.append(block.name)
                _draw_failed.append(block.name)
        _did_parallel = True

    for _idx, block in enumerate([] if _did_parallel else _blocks, 1):
        try:
            iface = block_interface(plan_ir, block)
        except Exception:
            skipped.append(block.name)
            _draw_failed.append(block.name)
            _emit(block.name, False, _idx)
            continue
        comps = [(c.ref, c.lib_id, c.value) for c in iface["components"]]
        if not comps:
            skipped.append(block.name)
            _emit(block.name, False, _idx)
            continue
        # PER-BLOCK AUDIT (gated build_graph.block_audit, default on): draw the
        # block, then validate IT IN ISOLATION; if it has fixable errors (wrong
        # pin name, undecoupled power pin, a pin on two of its own nets) redraw
        # it WITH those errors fed back (block_draw already renders the real
        # available_pins) until it's clean or the attempt budget runs out --
        # catching generation slips per-block, where the model can fix them with
        # focused context, BEFORE they pile onto the assembled board. When off
        # this is the original single forward draw (block_errs stays []).
        _audit_on, _audit_max = _block_audit_cfg()
        _tries = max(_draw_attempts, _audit_max) if _audit_on else _draw_attempts
        new_comps, new_nets = None, None
        block_errs: list = []
        for _draw_try in range(_tries):
            try:
                new_comps, new_nets = block_draw(
                    block.name,
                    getattr(block, "block_type", "") or "",
                    comps,
                    block_errs,               # [] on the first draw; audit errors on a redraw
                    iface["boundary_nets"],
                    prompt,
                )
            except Exception:              # noqa: BLE001
                new_comps, new_nets = None, None
            if not new_comps:
                continue                   # draw failed -> retry (transient)
            if not _audit_on:
                break                      # got a draw, audit off -> done
            block_errs = _audit_block(new_comps, new_nets, iface["boundary_nets"])
            if not block_errs:
                break                      # block is clean -> done
            # else: loop and redraw this block WITH block_errs (+ available_pins)

        if new_comps:
            try:
                # Bind the block's emitted signal names to the interface contract
                # BEFORE splicing, so an abbreviation/variant (GATE_DRV) meets the
                # rest of the board at the contracted net (GATE_DRIVE).
                _reconcile_to_contract(new_nets or [], iface["boundary_nets"])
                _sp_rep = splice_block(plan_ir, block.name, new_comps, new_nets or [])
                # Phase 0.2: surface any incidental cross-block name collision the
                # splice guard auto-renamed (only non-empty when that gate is on),
                # mirroring the `_incremental_skipped` stamp -> validate_ir emits a
                # CROSS_BLOCK_NET_COLLISION warning. Empty -> attribute never set ->
                # byte-stable.
                _cr = (_sp_rep or {}).get("collision_renames") or []
                if _cr:
                    setattr(plan_ir, "_cross_block_collisions",
                            (getattr(plan_ir, "_cross_block_collisions", None) or []) + _cr)
                drawn.append(block.name)
                _emit(block.name, True, _idx)
            except Exception:              # noqa: BLE001
                skipped.append(block.name)
                _draw_failed.append(block.name)
                _emit(block.name, False, _idx)
        else:
            skipped.append(block.name)
            _draw_failed.append(block.name)
            _emit(block.name, False, _idx)

    # JOIN + VERIFY every cross-block connection the plan declared (the U1<->U2
    # seams). Re-adds any endpoint a block dropped -- the real check, not an
    # assumption -- BEFORE normalize cleans the result.
    _reassert_plan_seams(plan_ir, planned_seams)

    if normalize:
        try:
            from .normalize import normalize_ir
            normalize_ir(plan_ir)
        except Exception:                  # noqa: BLE001
            pass

    # Gated (build_graph.fail_on_block_skip, default true): record genuine
    # draw/splice failures on the IR so validate_ir raises INCREMENTAL_BLOCK_
    # SKIPPED and the repair loop redraws them, instead of shipping an unwired
    # block as a green success. In-memory attribute only (not serialized);
    # block_repair prunes it on a successful redraw. Empty -> no attribute set,
    # so the IR is unchanged when nothing failed or the gate is off.
    if _draw_failed:
        try:
            from .engine import _load_layout_config as _lc_fb
            if bool((_lc_fb().get("build_graph") or {}).get(
                    "fail_on_block_skip", True)):
                setattr(plan_ir, "_incremental_skipped", list(_draw_failed))
        except Exception:                  # noqa: BLE001
            pass

    return plan_ir, drawn, skipped
