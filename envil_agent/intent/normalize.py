"""Value / label / refdes normalisation — professional schematic conventions.

Three pure functions applied to an IR before render. Safety net for the
architect: even when the LLM emits a value in a non-canonical form
(``4700``, ``0.1uF``, ``nRESET``, ``RESET_N``, ``/CS``), we coerce it to
the standard form expected by readers, design reviewers, and downstream
BOM tools.

Standards followed
------------------
- **IEC 60062 (RKM code)**: ``4k7`` not ``4.7k``, ``100n`` not ``0.1u``,
  ``4n7`` not ``4.7n``. Prefix replaces the decimal point. Resistors use
  ``R`` for the bare-ohm column.
- **KLC S4.7**: active-low signals use overbar form ``~{NAME}``. Input
  forms ``nXxx``, ``/Xxx``, ``#Xxx``, ``~Xxx``, ``Xxx_N``, ``Xxx#`` all
  collapse to ``~{Xxx}``. Differential-pair suffixes ``_P/_N`` left as-is
  (they're not active-low markers).
- **ASME Y14.44**: refdes is one or two LETTERS followed by digits.
  Recognised letters: R, C, L, U, Q, D, J, P, K, S, T, Y, X, F, FB, FL,
  TP, MP, MH, BT, LS, M, BR, RT, RV, TC, VR, W. Anything else triggers
  a warning (not an error — vendor-specific designators do exist).
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, Optional, Set


# ---------------------------------------------------------------------------
# Config loaders (layout_config.json -> normalize)
# ---------------------------------------------------------------------------

def _normalize_cfg() -> Dict:
    """Read the `normalize` section from layout_config.json. Falls back
    to the hardcoded defaults below when JSON is unreadable so a missing
    config file still yields a working normaliser."""
    try:
        from .engine import _load_layout_config
        return _load_layout_config().get("normalize", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Value normalisation (IEC 60062 / RKM)
# ---------------------------------------------------------------------------

# Per-component-type unit hints. Read from JSON; the dict below is the
# fallback used only when `layout_config.json -> normalize.unit_by_refdes_prefix`
# is missing or invalid.
_UNIT_BY_PREFIX_DEFAULT = {
    "R": "ohm",  "RV": "ohm", "RT": "ohm",
    "C": "farad",
    "L": "henry", "FB": "ohm",   # ferrite beads spec'd in ohm @ MHz
}


def _unit_by_prefix() -> Dict[str, str]:
    cfg = _normalize_cfg().get("unit_by_refdes_prefix")
    if isinstance(cfg, dict) and cfg:
        return {k: str(v) for k, v in cfg.items()}
    return dict(_UNIT_BY_PREFIX_DEFAULT)

# IEC 60062 prefix tables (multiplier → letter)
_OHM_PREFIXES = [
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1.0, "R"),
    (1e-3, "m"),  # rare for R, but legal
]

_CAP_PREFIXES = [
    (1.0, "F"),
    (1e-3, "m"),
    (1e-6, "u"),
    (1e-9, "n"),
    (1e-12, "p"),
]

_IND_PREFIXES = [
    (1.0, "H"),
    (1e-3, "m"),
    (1e-6, "u"),
    (1e-9, "n"),
    (1e-12, "p"),
]

# Parse "<number><opt-multiplier-letter><opt-unit>"
_NUM_RE = re.compile(
    r"""^\s*                          # leading ws
        (?P<num>[\d.]+)                # number
        \s*
        (?P<mult>[pPnNuUµMmKkG])?      # optional SI multiplier
        \s*
        (?P<unit>F|f|H|h|R|r|Ohm|ohm|Ω|Hz)?  # optional unit
        \s*$""",
    re.VERBOSE,
)

_MULT_MAP = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
    "m": 1e-3,
    "k": 1e3, "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}


def _parse_quantity(raw: str) -> Optional[tuple[float, str]]:
    """Return (numeric_value_in_base_SI, unit_letter) or None if unparseable.

    Handles:
      "4k7"     → (4700,    "")        ← compact RKM
      "4.7k"    → (4700,    "")
      "4700"    → (4700,    "")
      "10u"     → (1e-5,    "")        ← bare prefix, no unit
      "10uF"    → (1e-5,    "F")
      "0.1uF"   → (1e-7,    "F")
      "100n"    → (1e-7,    "")
    """
    if not raw:
        return None
    s = raw.strip()
    # Compact RKM form: digit + prefix + digit  (e.g. 4k7, 4n7, 4u7)
    m = re.match(r"^(\d+)([pPnNuUµMmKkG])(\d+)$", s)
    if m:
        whole, prefix, frac = m.groups()
        value = float(f"{whole}.{frac}") * _MULT_MAP.get(prefix.lower(), 1.0)
        if prefix == "M":  # capital M is mega for resistors
            value = float(f"{whole}.{frac}") * 1e6
        return (value, "")
    # Decimal form
    m = _NUM_RE.match(s)
    if not m:
        return None
    num = float(m["num"])
    mult = m["mult"] or ""
    unit = (m["unit"] or "").lower()
    factor = _MULT_MAP.get(mult, 1.0)
    # Disambiguate "M" — for capacitors "m" is milli; the regex normalises
    # to lowercase only when explicit; capital M from user means mega.
    if mult == "M":
        factor = 1e6
    return (num * factor, unit)


def _format_rkm(value: float, prefixes: list) -> str:
    """Pick the most compact RKM representation for the given value."""
    if value == 0:
        return "0"
    abs_v = abs(value)
    for mult, letter in prefixes:
        if abs_v >= mult:
            scaled = value / mult
            if abs(scaled - round(scaled)) < 1e-9:
                return f"{int(round(scaled))}{letter}"
            # 4.7 → "4k7"; 1.234 → "1k234" (rare); generally keep 1 decimal
            int_part = int(scaled)
            frac = scaled - int_part
            # Express fractional as digits after the prefix letter
            frac_str = f"{frac:.3f}".lstrip("0.").rstrip("0") or ""
            if frac_str:
                return f"{int_part}{letter}{frac_str}"
            return f"{int_part}{letter}"
    # Smaller than smallest prefix → leave as-is
    return f"{value}"


def normalize_value(value: str, ref: str = "") -> str:
    """Convert a free-form value to IEC 60062 RKM format.

    `ref` is the component reference (R1, C3, L7); its alpha prefix
    picks the prefix table. Unknown prefixes → return input unchanged.

    Leaves non-numeric values alone: ``"LED"``, ``"NE555"``, ``"DNP"``,
    transistor part numbers, etc."""
    if not value:
        return value
    ref_letters = re.match(r"^([A-Za-z]+)", ref or "")
    prefix_key = ref_letters.group(1).upper() if ref_letters else ""
    # JSON-driven refdes prefix -> SI unit map, with unit -> prefix-
    # table dispatch. Add a new prefix (e.g. "NTC": "ohm" for negative-
    # temperature-coefficient thermistors) in layout_config.json under
    # normalize.unit_by_refdes_prefix; no code edit required.
    unit_map = _unit_by_prefix()
    unit = unit_map.get(prefix_key)
    table = {"ohm":   _OHM_PREFIXES,
             "farad": _CAP_PREFIXES,
             "henry": _IND_PREFIXES}.get(unit)
    if table is None:
        return value  # not a passive — leave as-is
    parsed = _parse_quantity(value)
    if parsed is None:
        return value
    base_v, _ = parsed
    return _format_rkm(base_v, table)


# ---------------------------------------------------------------------------
# Label normalisation (KLC S4.7 overbar form)
# ---------------------------------------------------------------------------

# Patterns that mark a net as active-low:
#   nRESET        ← lowercase n prefix
#   /RESET        ← slash prefix
#   #CS           ← hash prefix
#   ~CS           ← tilde prefix (without braces)
#   RESET_N       ← _N suffix
#   RESET#        ← hash suffix
#   ~{RESET}      ← already in KLC form (leave alone)
#
# Special case: differential-pair suffixes _P / _N are NOT active-low
# unless the leading word is in a known active-low role list.

_DIFF_PAIR_BASES_DEFAULT = {"USB", "CAN", "RS485", "ENET", "LVDS", "DP", "DM",
                              "TX", "RX", "CLK", "DATA", "PCIE"}


def _diff_pair_bases() -> Set[str]:
    cfg = _normalize_cfg().get("differential_pair_prefixes")
    if isinstance(cfg, list) and cfg:
        return {str(s).upper() for s in cfg}
    return set(_DIFF_PAIR_BASES_DEFAULT)


def _is_diff_pair(base: str) -> bool:
    """Heuristic: if base is a known differential-pair signal name,
    treat _N as the negative leg, not active-low. Prefix list comes
    from layout_config.json -> normalize.differential_pair_prefixes;
    add project-specific protocol names there without editing code."""
    up = base.upper()
    bases = _diff_pair_bases()
    if up in bases:
        return True
    # USB_D, CAN_, LVDS_ prefixes — anything that starts with a known
    # diff pair prefix (same JSON-driven list as the exact-match check)
    for prefix in bases:
        if up.startswith(prefix + "_") or up.startswith(prefix):
            return True
    return False


def normalize_label(name: str) -> str:
    """Convert any active-low naming variant to KLC overbar form
    ``~{NAME}``. Non-active-low names pass through unchanged. Differential
    pair suffixes (USB_DP/USB_DN) are recognised and NOT converted."""
    if not name:
        return name
    s = name.strip()

    # Already in overbar form
    if s.startswith("~{") and s.endswith("}"):
        return s

    # Suffix forms: RESET_N or RESET#
    m = re.match(r"^([A-Za-z][A-Za-z0-9_]*?)_N$", s)
    if m:
        base = m.group(1)
        if not _is_diff_pair(base):
            return "~{" + base + "}"
    if s.endswith("#"):
        return "~{" + s[:-1] + "}"

    # Prefix forms: n + UPPER, / , #, ~ (without braces)
    # nXxx where X is uppercase = active-low
    m = re.match(r"^n([A-Z][A-Za-z0-9_]*)$", s)
    if m:
        return "~{" + m.group(1) + "}"
    if s.startswith("/"):
        return "~{" + s[1:] + "}"
    if s.startswith("#"):
        return "~{" + s[1:] + "}"
    if s.startswith("~") and not s.startswith("~{"):
        return "~{" + s[1:] + "}"

    return s


# ---------------------------------------------------------------------------
# Reference designator validation (ASME Y14.44)
# ---------------------------------------------------------------------------

# Letters → category. Lookup informs both validation and BOM grouping.
# ASME Y14.44 refdes prefix -> category map. Read from JSON; the dict
# below is the fallback used only when `layout_config.json ->
# normalize.refdes_categories` is missing.
_REFDES_LETTERS_DEFAULT = {
    "A":  "assembly",
    "AT": "attenuator",
    "B":  "motor",
    "BR": "bridge rectifier",
    "BT": "battery",
    "C":  "capacitor",
    "D":  "diode / led",
    "F":  "fuse",
    "FB": "ferrite bead",
    "FL": "filter",
    "H":  "hardware",
    "HY": "hybrid",
    "J":  "connector (jack)",
    "K":  "relay",
    "L":  "inductor",
    "LS": "speaker",
    "M":  "motor (alt)",
    "MK": "microphone",
    "MH": "mounting hole",
    "MP": "mechanical part",
    "P":  "connector (plug)",
    "Q":  "transistor",
    "R":  "resistor",
    "RT": "thermistor",
    "RV": "varistor",
    "S":  "switch",
    "T":  "transformer",
    "TC": "thermocouple",
    "TP": "test point",
    "U":  "integrated circuit",
    "V":  "vacuum tube",
    "VR": "voltage regulator",
    "W":  "wire / cable",
    "X":  "socket",
    "Y":  "crystal / oscillator",
    "Z":  "zener (rare)",
}


def _refdes_letters() -> Dict[str, str]:
    cfg = _normalize_cfg().get("refdes_categories")
    if isinstance(cfg, dict) and cfg:
        return {k.upper(): str(v) for k, v in cfg.items()}
    return dict(_REFDES_LETTERS_DEFAULT)


def _refdes_aliases() -> Dict[str, str]:
    """Map of informal refdes prefix -> ASME letter from
    normalize.refdes_prefix_aliases. The `_comment` key is ignored.
    Default empty = no aliasing."""
    cfg = _normalize_cfg().get("refdes_prefix_aliases")
    if not isinstance(cfg, dict):
        return {}
    return {str(k).upper(): str(v) for k, v in cfg.items()
            if k != "_comment" and v}


def _tie_groups() -> list:
    """Net-tie groups from normalize.tie_nets.groups (first entry of each
    group = canonical). Default empty = no auto-merge."""
    cfg = _normalize_cfg().get("tie_nets")
    if isinstance(cfg, dict):
        cfg = cfg.get("groups")
    if isinstance(cfg, list):
        return [list(g) for g in cfg if isinstance(g, list) and g]
    return []


def _polarity_free_prefixes() -> Set[str]:
    """Refdes prefixes the multinet resolver may pin-swap (MOVE). Config:
    normalize.polarity_free_passive_prefixes; inline fallback below."""
    cfg = _normalize_cfg().get("polarity_free_passive_prefixes")
    if isinstance(cfg, list) and cfg:
        return {str(s).upper() for s in cfg}
    return {"R", "C", "L", "FB", "RV", "RT"}


def _polarized_markers() -> list:
    """Uppercase lib_id substrings marking a polarity-sensitive part (no
    pin-swap). Config: normalize.polarized_lib_markers; fallback ['POLAR']."""
    cfg = _normalize_cfg().get("polarized_lib_markers")
    if isinstance(cfg, list) and cfg:
        return [str(s).upper() for s in cfg]
    return ["POLAR"]


def _ground_aliases() -> Set[str]:
    """Net names treated as ground. Config: normalize.ground_rail_aliases;
    fallback {GND,AGND,DGND,PGND}."""
    cfg = _normalize_cfg().get("ground_rail_aliases")
    if isinstance(cfg, list) and cfg:
        return {str(s).upper() for s in cfg}
    return {"GND", "AGND", "DGND", "PGND"}


def _split_ground_bases() -> Set[str]:
    """Uppercase BASE names in a `<BASE>_GND` net that denote a DELIBERATE
    split ground (analog/digital/power/chassis/earth) joined to the global
    ground only at a single star point -- these must NOT be auto-folded into
    GND. Everything else (`CAN_GND`, `RS485_GND`, `SHIELD_GND`, `LIN_GND`...)
    is a bus / connector ground REFERENCE that, on a non-isolated transceiver,
    is the same node as the board ground (ISO 11898-2 requires the shared
    reference). Config: normalize.split_ground_bases; inline fallback below."""
    cfg = _normalize_cfg().get("split_ground_bases")
    if isinstance(cfg, list) and cfg:
        return {str(s).upper() for s in cfg}
    return {"ANALOG", "DIGITAL", "POWER", "PWR", "CHASSIS", "EARTH", "FRAME",
            "A", "D", "P", "AGND", "DGND", "PGND", "FGND", "CGND", "EGND"}


def _vcc_rail_priority() -> list:
    """Logic-rail priority for folding a generic *_VCC connector net on a
    multi-rail board. Config: normalize.connector_vcc_rail_priority;
    fallback [+3V3,+3.3V,+5V,+1V8,+2V5]."""
    cfg = _normalize_cfg().get("connector_vcc_rail_priority")
    if isinstance(cfg, list) and cfg:
        return [str(s).upper() for s in cfg]
    return ["+3V3", "+3.3V", "+5V", "+1V8", "+2V5"]


def _resolve_multinet_passives(ir) -> int:
    """Deterministically resolve PIN_IN_MULTIPLE_NETS on 2-terminal parts.

    A pin on two nets is only ever correct when a physical pin serves two
    roles on a MULTI-pin device (PB0 = MOSI + PWM). On a 2-TERMINAL part
    (R, C, L, ferrite, diode) it is always an architect slip, and the
    correct repair is decided by ONE question -- is the part's OTHER pin
    already wired?

      * OTHER PIN FREE  -> MOVE: the part is meant to BRIDGE the two nets;
        the architect just stacked both on one pin. Relocate one net to the
        free pin -- keep the power rail on the original pin, move the
        functional net across. Only for polarity-FREE parts (R/C/L/FB);
        swapping a diode's or electrolytic's pins would flip it.
            R5.2 on [GND, LED_PWR], R5.1 free
              -> R5.1 on LED_PWR, R5.2 on GND.

      * OTHER PIN WIRED -> DROP a redundant membership (never a pin swap,
        so safe for diodes too):
            rule 1  drop the net that EQUALS the other pin's net -- keeping
                    it would short the part end-to-end.            (any part)
            rule 2  else drop the canonical power rail, keep the functional
                    net -- a decoupling/VCAP/VBAT cap whose hot pin the
                    architect also dumped onto the supply rail.  (R/C/L/FB)
            C17.1 on [+3V3, VCAP_1], C17.2 on GND
              -> C17.1 on VCAP_1 (the +3V3 was spurious).
            D1.A on [VIN_FUSED, GND], D1.K on VIN_FUSED
              -> D1.A on GND (rule 1: VIN_FUSED would short the diode).

    Anything that does not match a rule (3+ nets on the pin, both nets
    rails, a multi-pin device, an unloadable symbol) is LEFT untouched for
    the validator, so this is strictly additive: it only ever removes a
    real error, never creates a short. Returns the number of pins repaired.
    """
    from ..kicad.symbol_geom import load_symbol

    rails = _canonical_rails()
    ground_names = _ground_aliases()
    symmetric_prefixes = _polarity_free_prefixes()
    polarized_markers = _polarized_markers()

    # Group every net-pin occurrence by the pin's CANONICAL identity
    # (ref, pin-number) rather than its raw token, so the SAME physical pin
    # written as a name in one net (``D1.A``) and a number in another
    # (``D1.2``) is recognised as one pin -- otherwise a cross-token
    # double-list is invisible to a string-keyed map and slips through as a
    # short. occ[(ref, number)] = [(net, token), ...].
    _geom_cache: Dict[str, Any] = {}

    def _geom(lib_id: str):
        if lib_id not in _geom_cache:
            try:
                _geom_cache[lib_id] = load_symbol(lib_id)
            except Exception:
                _geom_cache[lib_id] = None
        return _geom_cache[lib_id]

    occ: Dict[tuple, list] = {}
    info: Dict[tuple, tuple] = {}            # (ref,num) -> (comp, geom, pin)
    for net in ir.nets:
        for token in list(net.pins):
            if "." not in token:
                continue
            ref, key = token.split(".", 1)
            comp = ir.component_by_ref(ref)
            if comp is None:
                continue
            geom = _geom(comp.lib_id)
            if geom is None:
                continue
            p = geom.resolve_pin(key)
            if p is None:
                continue
            ck = (ref, p.number)
            occ.setdefault(ck, []).append((net, token))
            info[ck] = (comp, geom, p)

    nets_of_key: Dict[tuple, list] = {}
    for ck, places in occ.items():
        seen, distinct = set(), []
        for net, _tok in places:
            if id(net) not in seen:
                seen.add(id(net))
                distinct.append(net)
        nets_of_key[ck] = distinct

    fixed = 0
    for ck, nets in nets_of_key.items():
        if len(nets) != 2:
            continue
        ref, num = ck
        comp, geom, dup = info[ck]
        pins = list(geom.pins or [])
        if len(pins) != 2:
            continue  # multi-pin device -> a genuine dual-role pin; leave it
        other = next((p for p in pins if p.number != dup.number), None)
        if other is None:
            continue
        other_nets = nets_of_key.get((ref, other.number), [])
        prefix = "".join(c for c in ref if c.isalpha()).upper()
        lib_up = (comp.lib_id or "").upper()
        symmetric = (prefix in symmetric_prefixes
                     and not any(m in lib_up for m in polarized_markers))
        net_a, net_b = nets[0], nets[1]
        a_rail = net_a.name.upper() in rails
        b_rail = net_b.name.upper() in rails
        a_gnd = net_a.name.upper() in ground_names
        b_gnd = net_b.name.upper() in ground_names

        # Tokens this physical pin appears under, per net (handles name OR
        # number form); used to remove the right strings on a mutation.
        toks_in = lambda target: [t for (n, t) in occ[ck] if n is target]

        if not other_nets:
            # --- MOVE: other pin is free; the part is meant to BRIDGE the two
            # nets. Pin-swap, so restrict to polarity-free symmetric parts.
            # Two bridge shapes are unambiguous and safe to auto-wire:
            #   (1) SIGNAL <-> RAIL: exactly one net is a power rail, the other
            #       a signal -- a series/indicator element (R5.2 on [GND,
            #       LED_PWR] with R5.1 free -> R5.1=LED_PWR, R5.2=GND).
            #   (2) SUPPLY <-> GND: BOTH nets are rails but one is GROUND and
            #       the other a non-ground supply -- the canonical decoupling/
            #       bypass/bulk-cap shape the architect dumped onto one pin
            #       (C11.2 on [GND, VBAT] with C11.1 free -> C11.1=GND,
            #       C11.2=VBAT). A 2-terminal part touching a supply AND ground
            #       can only be a cap/bleeder between them, so the bridge is
            #       correct.
            # Everything else stays for the architect: two SIGNALS (R4.1 on
            # [BUCK_ON, SD_DET]) or two non-ground supplies (+3V3 <-> +5V) would
            # invent a wrong connection; two grounds (GND <-> AGND) are a
            # deliberate star-point split.
            if not symmetric:
                continue
            if a_rail != b_rail:
                _keep, move = (net_a, net_b) if a_rail else (net_b, net_a)
            elif a_rail and b_rail and (a_gnd != b_gnd):
                # keep the SUPPLY on the original pin, move GROUND to the free one
                _keep, move = (net_b, net_a) if a_gnd else (net_a, net_b)
            else:
                continue
            move_toks = set(toks_in(move))
            move.pins = [p for p in move.pins if p not in move_toks]
            other_ref = f"{ref}.{other.number}"
            if other_ref not in move.pins:
                move.pins.append(other_ref)
            fixed += 1
        else:
            # --- DROP a redundant membership; no pin-swap, safe for diodes. ---
            other_names = {n.name for n in other_nets}
            drop = None
            if net_a.name in other_names and net_b.name not in other_names:
                drop = net_a                       # rule 1
            elif net_b.name in other_names and net_a.name not in other_names:
                drop = net_b                       # rule 1
            elif symmetric and a_rail and not b_rail:
                drop = net_a                       # rule 2
            elif symmetric and b_rail and not a_rail:
                drop = net_b                       # rule 2
            if drop is None:
                continue
            drop_toks = set(toks_in(drop))
            drop.pins = [p for p in drop.pins if p not in drop_toks]
            fixed += 1
    return fixed


def _resolve_pin_multinet(ir) -> int:
    """Unified classify->act resolver for PIN_IN_MULTIPLE_NETS — the single
    entry point that consolidates the per-pin repairs previously split across
    `_merge_shared_pin_ground_nets`, `_fold_shunt_tap_aliases` and
    `_resolve_multinet_passives`. For every PHYSICAL pin on >1 net, classify by
    the part + what its OTHER terminal connects to, then act, iterating until
    stable (one MERGE can clear several conflicts):

      MERGE   the two nets are ONE node:
                * connector/IC ground alias [GND, <BASE>_GND]; or
                * a SHUNT tap (TVS/filter/decoupling — other pin on GND/rail)
                  whose two names are provably the same node: both POWER
                  (rail + power-like alias) or two RELATED signal names
                  (CAN_H / CAN_TVS).
      DROP    a SERIES 2-terminal part (other pin on one of the two nets) ->
                drop the redundant membership so it sits in series (rule 1);
                or a decoupling cap's spurious power rail (rule 2, symmetric).
      MOVE    a symmetric 2-terminal part bridging the two nets with its other
                pin FREE -> relocate one net to the free terminal (signal<->rail
                or supply<->GND decoupling shape).
      REVIEW  3+ nets, a genuine multi-pin dual-role IC pin, two distinct rails,
                unrelated signals, or anything ambiguous -> left for the
                validator/architect.

    Every guard from the scattered resolvers is preserved (GND never folded
    into, two distinct rails never merged, unrelated signals never merged, a
    diode/electrolytic never pin-swapped), so it can never short the board.
    Gated by normalize.unified_multinet_resolver; returns the number of fixes.
    """
    from ..kicad.symbol_geom import load_symbol

    rails = _canonical_rails()
    gnd = _ground_aliases()
    sym_prefixes = _polarity_free_prefixes()
    polar_markers = _polarized_markers()
    split_g = _split_ground_bases()
    geom_cache: Dict[str, Any] = {}

    def _geom(lib_id: str):
        if lib_id not in geom_cache:
            try:
                geom_cache[lib_id] = load_symbol(lib_id)
            except Exception:
                geom_cache[lib_id] = None
        return geom_cache[lib_id]

    def _build_occ():
        occ: Dict[tuple, list] = {}
        info: Dict[tuple, tuple] = {}
        for net in ir.nets:
            for tok in list(net.pins):
                if "." not in tok:
                    continue
                ref, key = tok.split(".", 1)
                comp = ir.component_by_ref(ref)
                if comp is None:
                    continue
                g = _geom(comp.lib_id)
                if g is None:
                    continue
                p = g.resolve_pin(key)
                if p is None:
                    continue
                ck = (ref, p.number)
                lst = occ.setdefault(ck, [])
                if not any(n is net for (n, _t) in lst):
                    lst.append((net, tok))
                info[ck] = (comp, g, p)
        return occ, info

    def _distinct(places):
        out = []
        for net, _t in places:
            if not any(net is n for n in out):
                out.append(net)
        return out

    def _toks_on(places, target):
        return [t for (n, t) in places if n is target]

    fixes = 0
    for _ in range(200):                       # bounded iterate-until-stable
        occ, info = _build_occ()
        acted = False
        for ck, places in occ.items():
            nets = _distinct(places)
            if len(nets) != 2:
                continue                       # 3+/1 nets -> REVIEW
            ref, _num = ck
            comp, g, dup = info[ck]
            A, B = nets[0], nets[1]
            Aup, Bup = A.name.upper(), B.name.upper()
            A_rail, B_rail = Aup in rails, Bup in rails
            A_gnd, B_gnd = Aup in gnd, Bup in gnd
            pins = list(g.pins or [])
            two_term = len(pins) == 2
            prefix = "".join(c for c in ref if c.isalpha()).upper()
            lib_up = (comp.lib_id or "").upper()
            sym = (prefix in sym_prefixes) and not any(m in lib_up for m in polar_markers)

            action = None
            data = None

            # MERGE 1 — connector/IC ground alias [GND, <BASE>_GND]
            if A_gnd != B_gnd:
                gnd_net, other = (A, B) if A_gnd else (B, A)
                oup = other.name.upper()
                if oup.endswith("_GND"):
                    base = oup[:-4]
                    if base and base not in split_g:
                        action, data = "MERGE", gnd_net.name

            if action is None and two_term:
                other_pin = next((p for p in pins if p.number != dup.number), None)
                other_places = occ.get((ref, other_pin.number), []) if other_pin else []
                other_nets = _distinct(other_places)
                other_names = {n.name for n in other_nets}
                other_on_railgnd = any(
                    (n.name.upper() in rails or n.name.upper() in gnd)
                    for n in other_nets)

                if not other_nets:
                    # MOVE — bridge, other pin free (symmetric parts only)
                    if sym and A_rail != B_rail:
                        move = B if A_rail else A
                        action, data = "MOVE", (move, f"{ref}.{other_pin.number}")
                    elif sym and A_rail and B_rail and (A_gnd != B_gnd):
                        move = A if A_gnd else B
                        action, data = "MOVE", (move, f"{ref}.{other_pin.number}")
                else:
                    # SERIES DROP rule 1 — other pin shares a net with A/B
                    if A.name in other_names and B.name not in other_names:
                        action, data = "DROP", A
                    elif B.name in other_names and A.name not in other_names:
                        action, data = "DROP", B
                    # MERGE 2 — shunt tap (other pin on GND/rail, not one of A/B)
                    elif (other_on_railgnd and not (A_gnd or B_gnd)
                          and not (A_rail and B_rail and A.name != B.name)):
                        ok = True
                        if A_rail != B_rail:
                            other_side = B if A_rail else A
                            ok = _is_power_like_net(other_side)
                        elif not A_rail and not B_rail:
                            ok = _names_related(A.name, B.name)
                        if ok:
                            if A_rail and not B_rail:
                                data = A.name
                            elif B_rail and not A_rail:
                                data = B.name
                            elif (len(A.pins), -len(A.name)) >= (len(B.pins), -len(B.name)):
                                data = A.name
                            else:
                                data = B.name
                            action = "MERGE"
                    # DROP rule 2 — decoupling cap, drop the spurious rail
                    if action is None and sym:
                        if A_rail and not B_rail:
                            action, data = "DROP", A
                        elif B_rail and not A_rail:
                            action, data = "DROP", B

            if action == "MERGE":
                keep_net = A if A.name == data else B
                lose_net = B if keep_net is A else A
                if lose_net is keep_net:
                    continue
                seen = set(keep_net.pins)
                for p in lose_net.pins:
                    if p not in seen:
                        keep_net.pins.append(p)
                        seen.add(p)
                if getattr(lose_net, "is_power", False) or getattr(keep_net, "is_power", False):
                    keep_net.is_power = True
                ir.nets.remove(lose_net)
                fixes += 1
                acted = True
                break
            if action == "DROP":
                drop_toks = set(_toks_on(places, data))
                data.pins = [p for p in data.pins if p not in drop_toks]
                fixes += 1
                acted = True
                break
            if action == "MOVE":
                move_net, free_ref = data
                move_toks = set(_toks_on(places, move_net))
                move_net.pins = [p for p in move_net.pins if p not in move_toks]
                if free_ref not in move_net.pins:
                    move_net.pins.append(free_ref)
                fixes += 1
                acted = True
                break
        if not acted:
            break
    return fixes


def _canonical_rails() -> Set[str]:
    """Upper-cased canonical power-rail names from
    validate.canonical_power_rails (layout_config.json). Used to recognise
    a bare rail token ("GND", "+3V3") that the architect dropped into
    net.pins as an endpoint instead of `<ref>.<pin>`. Falls back to a
    minimal set when the config is unreadable."""
    try:
        from .engine import _load_layout_config
        rails = (_load_layout_config().get("validate", {}) or {}).get(
            "canonical_power_rails", [])
    except Exception:
        rails = []
    out = {str(r).upper() for r in rails if r}
    return out or {"GND", "AGND", "DGND", "PGND",
                   "+3V3", "+5V", "+12V", "+9V", "VBUS", "VBAT", "VCC", "VDD"}


_REFDES_RE = re.compile(r"^([A-Z]{1,2})(\d+)$")


def parse_refdes(ref: str) -> Optional[tuple[str, int]]:
    """Split a refdes into (letter-prefix, number). Returns None if the
    string doesn't match ASME Y14.44 (letters + digits)."""
    if not ref:
        return None
    m = _REFDES_RE.match(ref.strip())
    if not m:
        return None
    return (m.group(1), int(m.group(2)))


def validate_refdes(ref: str) -> Optional[str]:
    """Return an error string if `ref` violates ASME Y14.44, else None."""
    parsed = parse_refdes(ref)
    if parsed is None:
        return (f"refdes {ref!r} doesn't match ASME Y14.44 "
                f"(must be letters+digits, e.g. R1, U3, FB2)")
    letters, _ = parsed
    refdes_map = _refdes_letters()
    if letters not in refdes_map:
        return (f"refdes prefix {letters!r} not in ASME Y14.44 standard "
                f"set; recognised: {sorted(refdes_map.keys())}")
    return None


def _canonical_pin_name(ir, comp_ref: str, pin_key: str) -> Optional[str]:
    """Resolve `pin_key` against comp_ref's symbol and return the symbol's
    canonical (primary) pin name, or None if it can't be resolved.

    This collapses the datasheet/CubeMX naming the architect tends to emit
    ("PC14-OSC32_IN", "VCAP1", "PH0-OSC_IN") onto the library's short
    primary names ("PC14", "VCAP_1", "PH0"), and resolves alternate-function
    names onto their pin. Part-agnostic — relies entirely on the symbol's
    own pin table. Never raises and only returns on a confident resolve, so
    an unresolvable token is left untouched for the validator to report
    (see [feedback_non_breaking_changes])."""
    if pin_key.isdigit():
        return None  # pin numbers are already canonical
    try:
        comp = ir.component_by_ref(comp_ref)
        if comp is None:
            return None
        from ..kicad.symbol_geom import load_symbol
        pin = load_symbol(comp.lib_id).resolve_pin(pin_key)
        if pin is None or not pin.name or pin.name == "~":
            return None
        return pin.name
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Structural net repairs (Layer A — deterministic, see canlogger_retry_fix)
#
# The architect prompt already FORBIDS every one of these slips (rules 1, 4,
# 10c, 10d, 13), yet the LLM still commits them once a board passes ~30 parts
# — instruction-following degrades as the generation grows. Each slip has
# exactly ONE structurally-correct repair derivable from the IR + the symbol
# library, so we fix it here every build instead of bouncing the architect
# through a non-converging retry. All four are strictly additive: they only
# MERGE two nets that are provably the same node, or ADD a connection the
# validator already demands — never remove a legitimate one, never create a
# short. See [feedback_non_breaking_changes].
# ---------------------------------------------------------------------------

def _ground_pin_markers() -> list:
    """Substrings in a PIN name that mark it as a ground pin (GND/VSS/VEE...).
    Config: normalize.ground_pin_name_markers; inline fallback below. Substring
    match so VSSA matches VSS, AGND/DGND match GND — works for any IC."""
    cfg = _normalize_cfg().get("ground_pin_name_markers")
    if isinstance(cfg, list) and cfg:
        return [str(s).upper() for s in cfg]
    return ["GND", "VSS", "VEE"]


def _power_pin_etypes() -> Set[str]:
    """KiCad symbol electrical-types that count as a SINK power pin needing a
    rail. Config: normalize.power_pin_etypes; fallback {'power_in'}. power_out
    is deliberately excluded — a source pin has no derivable rail."""
    cfg = _normalize_cfg().get("power_pin_etypes")
    if isinstance(cfg, list) and cfg:
        return {str(s) for s in cfg}
    return {"power_in"}


def _capacitor_prefixes() -> Set[str]:
    """Refdes letter-prefixes that denote a capacitor. Config:
    normalize.capacitor_refdes_prefixes; fallback {'C'}."""
    cfg = _normalize_cfg().get("capacitor_refdes_prefixes")
    if isinstance(cfg, list) and cfg:
        return {str(s).upper() for s in cfg}
    return {"C"}


def _net_alias_min_shared_pins() -> int:
    """Pins two nets must SHARE before they're treated as one node. Config:
    normalize.net_alias_min_shared_pins; fallback 2 (sharing one pin is the
    ambiguous bridge case the multinet resolver owns). Floored at 2 — merging
    on a single shared pin would collapse legitimately-bridged nets."""
    try:
        return max(2, int(_normalize_cfg().get("net_alias_min_shared_pins", 2)))
    except (TypeError, ValueError):
        return 2


def _crystal_trigger_patterns() -> list:
    """lib_id glob patterns identifying a crystal/resonator — read from the SAME
    source the validator uses (design_checklist.json -> rules.crystal_oscillator
    .trigger_lib_id_patterns) so repair and validation never disagree. Inline
    fallback mirrors that default."""
    try:
        from .validate import _load_design_checklist
        rule = (((_load_design_checklist().get("rules") or {})
                 .get("crystal_oscillator")) or {})
        pats = rule.get("trigger_lib_id_patterns")
        if isinstance(pats, list) and pats:
            return [str(p) for p in pats]
    except Exception:
        pass
    return ["Device:Crystal*", "*:Crystal*", "*XTAL*"]


def _lib_matches_any(lib_id: str, patterns: list) -> bool:
    """Case-insensitive glob match of a lib_id against any pattern."""
    lid = (lib_id or "").lower()
    return any(fnmatch.fnmatch(lid, str(p).lower()) for p in patterns)


def _logic_rails_present(ir) -> list:
    """Non-ground power rails currently in the IR (original-cased names)."""
    gnd = _ground_aliases()
    out: Dict[str, str] = {}
    for n in ir.nets:
        if getattr(n, "is_power", False) and n.name.upper() not in gnd:
            out.setdefault(n.name.upper(), n.name)
    return list(out.values())


def _dominant_logic_rail(ir) -> Optional[str]:
    """The single logic rail a stray supply pin should fold into: one rail ->
    that rail; several -> the highest-priority present (connector_vcc_rail_
    priority); none/ambiguous -> None so the pin is left for the architect."""
    logic = _logic_rails_present(ir)
    if len(logic) == 1:
        return logic[0]
    present = {r.upper(): r for r in logic}
    for pref in _vcc_rail_priority():
        if pref in present:
            return present[pref]
    return None


def _net_by_name(ir, name: Optional[str]):
    if not name:
        return None
    for n in ir.nets:
        if n.name == name:
            return n
    return None


def _pin_canon_key(ir, token: str, geom_cache: dict):
    """Canonical (ref, pin-number) identity for a `<ref>.<pin>` token so the
    same physical pin written as a name in one net and a number in another
    compares equal. Falls back to the raw (ref, key) when the symbol/pin
    can't be resolved. Returns None for a no-dot (malformed) token."""
    if "." not in token:
        return None
    ref, key = token.split(".", 1)
    comp = ir.component_by_ref(ref)
    if comp is None:
        return (ref, key)
    if comp.lib_id not in geom_cache:
        try:
            from ..kicad.symbol_geom import load_symbol
            geom_cache[comp.lib_id] = load_symbol(comp.lib_id)
        except Exception:
            geom_cache[comp.lib_id] = None
    geom = geom_cache[comp.lib_id]
    if geom is not None:
        p = geom.resolve_pin(key)
        if p is not None:
            return (ref, p.number)
    return (ref, key)


def _merge_net_aliases(ir) -> int:
    """Merge two nets that share >= 2 physical pins — the unambiguous net-alias
    case (NRST + SWD_NRST both holding the reset pull-up pin AND the SWD header
    reset pin). Two pins shared across two names is ALWAYS one electrical node:
    no legitimate topology lists the same two pins on two different nets — that
    would short them at two points. Sharing exactly ONE pin is the bridge case
    left to _resolve_multinet_passives. Keeps the power name when exactly one
    side is a rail, else the more-connected / shorter name. Never merges two
    DISTINCT power rails (a real short — leave it for the validator). Returns
    the number of merges performed."""
    geom_cache: Dict[str, Any] = {}

    def keyset(net) -> set:
        ks = set()
        for tok in net.pins:
            k = _pin_canon_key(ir, tok, geom_cache)
            if k is not None:
                ks.add(k)
        return ks

    min_shared = _net_alias_min_shared_pins()
    merged_count = 0
    changed = True
    while changed:
        changed = False
        n = len(ir.nets)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = ir.nets[i], ir.nets[j]
                if len(keyset(a) & keyset(b)) < min_shared:
                    continue
                a_pow = bool(getattr(a, "is_power", False))
                b_pow = bool(getattr(b, "is_power", False))
                if a_pow and b_pow and a.name != b.name:
                    continue  # two real rails shorted -> validator's call
                if a_pow and not b_pow:
                    keep, lose = a, b
                elif b_pow and not a_pow:
                    keep, lose = b, a
                elif (len(a.pins), -len(a.name)) >= (len(b.pins), -len(b.name)):
                    keep, lose = a, b           # more pins, then shorter name
                else:
                    keep, lose = b, a
                seen = set(keep.pins)
                for pr in lose.pins:
                    if pr not in seen:
                        keep.pins.append(pr)
                        seen.add(pr)
                if getattr(lose, "is_power", False):
                    keep.is_power = True
                ir.nets.remove(lose)
                merged_count += 1
                changed = True
                break
            if changed:
                break
    return merged_count


def _merge_shared_pin_ground_nets(ir) -> int:
    """Resolve PIN_IN_MULTIPLE_NETS where a SINGLE physical pin sits on the
    global GND and a derived `<BASE>_GND` bus/connector ground reference.

    This is the connector/multi-pin analogue of the single-shared-pin case
    `_resolve_multinet_passives` deliberately leaves alone for 2-terminal
    parts. A pin physically listed on two nets is a GALVANIC tie between
    them -- it proves the two grounds are one electrical node, so they cannot
    be isolated from each other. On a non-isolated transceiver/connector the
    bus ground reference (CAN_GND, RS485_GND, LIN_GND, SHIELD_GND...) IS the
    board ground; the architect just spelled the one node with two names and
    dropped the connector's ground pin on both. We rename the `<BASE>_GND`
    net to the canonical ground so the by-name merge collapses them.

    Strictly conservative, so it only ever removes a real PIN_IN_MULTIPLE_NETS
    short and never collapses an intentional split:
      * fires ONLY when exactly ONE of the pin's two nets is the canonical
        ground and the OTHER matches `<BASE>_GND` (underscore form);
      * a deliberate split ground (ANALOG_GND, DGND, CHASSIS_GND -> see
        split_ground_bases) is excluded -- those meet GND at one star point
        on purpose;
      * a pin on 3+ nets, or on two unrelated names, is left for the
        validator.
    Part- and bus-agnostic: any future `<BASE>_GND` reference folds with no
    code edit. Returns the number of nets folded into ground."""
    gnd = _ground_aliases()
    split = _split_ground_bases()
    geom_cache: Dict[str, Any] = {}

    # canonical (ref, pin-number) identity -> distinct nets the pin appears on,
    # so the same physical pin written as a name in one net and a number in
    # another (J4.Pin_2 vs J4.2) is recognised as one pin.
    pin_nets: Dict[tuple, list] = {}
    for net in ir.nets:
        for tok in net.pins:
            ck = _pin_canon_key(ir, tok, geom_cache)
            if ck is None:
                continue
            lst = pin_nets.setdefault(ck, [])
            if not any(n is net for n in lst):
                lst.append(net)

    rename: Dict[str, str] = {}
    for _ck, nets in pin_nets.items():
        if len(nets) != 2:
            continue
        a, b = nets
        a_gnd = a.name.upper() in gnd
        b_gnd = b.name.upper() in gnd
        if a_gnd == b_gnd:
            continue  # need exactly one canonical ground side
        gnd_net, other = (a, b) if a_gnd else (b, a)
        up = other.name.upper()
        if not up.endswith("_GND"):
            continue
        base = up[:-4]
        if not base or base in split:
            continue  # deliberate split ground -> leave it for the validator
        rename[other.name] = gnd_net.name
    if rename:
        for net in ir.nets:
            if net.name in rename:
                net.name = rename[net.name]
                net.is_power = True
    return len(rename)


def _names_related(a: str, b: str) -> bool:
    """True when two net names are plausibly the SAME node under decorated
    spellings — a shared stem of >=3 chars (``CAN_H``/``CAN_TVS`` -> ``CAN``),
    or one name's stem contained in the other (``CANH``/``CANH_FILT``). Used to
    gate the both-SIGNAL shunt-tap merge so unrelated signals an architect
    dumped onto one pin (``NRST`` + ``VCAP_1`` on a decoupling cap) are NOT
    merged into a short."""
    a, b = (a or "").upper(), (b or "").upper()
    if a == b:
        return True
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    if i >= 3:
        return True
    sa = a.rstrip("_0123456789")
    sb = b.rstrip("_0123456789")
    return bool(sa and sb and (sa in b or sb in a))


def _is_power_like_net(net) -> bool:
    """True when a net is a power node (so a non-canonical name like ``J4_PWR``
    can be recognised as an alias of a real rail). is_power flag, a supply token
    in the name (PWR/VCC/VDD/VBAT/VSYS/VAUX/VBUS/VIN), or a ``+<n>V`` rail
    spelling. Deliberately does NOT include GND — ground is handled separately so
    a signal is never folded into the ground rail."""
    if getattr(net, "is_power", False):
        return True
    up = (net.name or "").upper()
    if any(tok in up for tok in ("PWR", "VCC", "VDD", "VBAT", "VSYS",
                                  "VAUX", "VBUS", "VIN")):
        return True
    return re.match(r"^[+\-]?\d+V", up) is not None


def _enforce_pin_ownership(ir) -> int:
    """GLOBAL one-net-per-pin guarantee — the final backstop, run after every
    targeted resolver. A physical pin IS one electrical node, so a pin on >1 net
    is ALWAYS wrong (even a 'dual-function' pin is electrically one net). For any
    over-assigned pin this merges only the pairs of its nets that are PROVABLY the
    same node. The test is PURELY STRUCTURAL — no net-name heuristics, no power-
    token lists, no per-circuit data:
      * the two nets are literally the same name, or
      * they SHARE TWO-OR-MORE physical pins — a two-point tie can only be one
        electrical node (the invariant _merge_net_aliases uses). A SINGLE shared
        pin is the ambiguous bridge case and is deliberately NOT merged (CAN_H /
        CAN_L share one pin yet are different nodes).
    Pin identity comes from the LIVE symbol via _pin_canon_key, so a pin written
    as a name in one net and a number/alias in another compares equal — fully
    dynamic, works for any part. Genuinely-DISTINCT nodes (a real architect
    mis-wire onto two different rails, or rail+signal) are LEFT untouched for the
    validator + repair loop: guessing which membership to drop could silently
    mis-wire the board, so prevention here is merge-only, never a blind drop.
    Returns merges performed. Gated normalize.global_pin_ownership."""
    geom_cache: Dict[str, Any] = {}

    def canon_set(net) -> set:
        out = set()
        for tok in net.pins:
            k = _pin_canon_key(ir, tok, geom_cache)
            if k is not None:
                out.add(k)
        return out

    def same_node(a, b) -> bool:
        # Structural only: identical name, or a >=2-pin tie (one node by physics).
        # No name heuristics / power-token lists -> nothing circuit-specific.
        if a.name == b.name:
            return True
        return len(canon_set(a) & canon_set(b)) >= 2

    merged = 0
    changed = True
    while changed:
        changed = False
        owners: Dict[Any, list] = {}
        for net in ir.nets:
            for tok in net.pins:
                k = _pin_canon_key(ir, tok, geom_cache)
                if k is None:
                    continue
                lst = owners.setdefault(k, [])
                if net not in lst:
                    lst.append(net)
        for _k, nets in owners.items():
            if len(nets) < 2:
                continue
            pair = None
            for i in range(len(nets)):
                for j in range(i + 1, len(nets)):
                    if same_node(nets[i], nets[j]):
                        pair = (nets[i], nets[j])
                        break
                if pair:
                    break
            if pair is None:
                continue
            a, b = pair
            a_pow = bool(getattr(a, "is_power", False))
            b_pow = bool(getattr(b, "is_power", False))
            if a_pow and not b_pow:
                keep, lose = a, b
            elif b_pow and not a_pow:
                keep, lose = b, a
            elif (len(a.pins), -len(a.name)) >= (len(b.pins), -len(b.name)):
                keep, lose = a, b
            else:
                keep, lose = b, a
            seen = set(keep.pins)
            for pr in lose.pins:
                if pr not in seen:
                    keep.pins.append(pr)
                    seen.add(pr)
            if getattr(lose, "is_power", False):
                keep.is_power = True
            ir.nets.remove(lose)
            merged += 1
            changed = True
            break
    return merged


def _fold_shunt_tap_aliases(ir) -> int:
    """Merge two nets that meet at the SIGNAL pin of a SHUNT 2-terminal part
    (TVS / filter / decoupling), recognised because the part's OTHER pin lands
    on GND or a power rail. A shunt part TAPS a node, it does not separate it —
    so a pin double-listed on ``[MAIN, MAIN_alias]`` proves the two names are
    ONE electrical node, and the fix is to fold the alias into the canonical /
    more-connected name. This is the dominant attempt-1 PIN_IN_MULTIPLE_NETS on
    multi-block boards (a protection/decoupling part the architect tapped onto a
    bus under a second descriptive net name):

        D2.A on [CAN_H, CAN_TVS], D2.K on GND  -> CAN_TVS folds into CAN_H
        C3.1 on [+5V, J4_PWR],    C3.2 on GND  -> J4_PWR  folds into +5V

    It is the SHUNT complement to `_resolve_multinet_passives`' DROP rule, which
    owns the SERIES case (the other pin sits on one of the two signal nets, e.g.
    an LED series resistor R7.2 on [LED_DRV, LED_A] with R7.1 on LED_DRV — there
    the two nets are DIFFERENT nodes and a membership is dropped, never folded).

    Strictly conservative so it can never short the board:
      * fires only when the part is 2-terminal AND its other pin is provably on
        GND / a rail (an unwired or signal other-pin is left alone);
      * never merges two DISTINCT power rails (+5V vs +3V3 — a real short);
      * skips the series case (other pin's net is one of the two) — that's the
        DROP rule's job.
    Renames the lower-priority net to the kept name; the by-name merge in
    `normalize_ir` then unions their pins. Returns the number of folds.
    """
    from ..kicad.symbol_geom import load_symbol

    rails = _canonical_rails()
    gnd = _ground_aliases()
    shunt_targets = rails | gnd          # other pin here => the part is a shunt

    geom_cache: Dict[str, Any] = {}

    def _geom(lib_id: str):
        if lib_id not in geom_cache:
            try:
                geom_cache[lib_id] = load_symbol(lib_id)
            except Exception:
                geom_cache[lib_id] = None
        return geom_cache[lib_id]

    # canonical (ref, pin-number) -> distinct nets it appears on
    occ: Dict[tuple, list] = {}
    info: Dict[tuple, tuple] = {}
    for net in ir.nets:
        for tok in net.pins:
            if "." not in tok:
                continue
            ref, key = tok.split(".", 1)
            comp = ir.component_by_ref(ref)
            if comp is None:
                continue
            g = _geom(comp.lib_id)
            if g is None:
                continue
            p = g.resolve_pin(key)
            if p is None:
                continue
            ck = (ref, p.number)
            lst = occ.setdefault(ck, [])
            if not any(n is net for n in lst):
                lst.append(net)
            info[ck] = (comp, g, p)

    folds = 0
    for ck, nets in list(occ.items()):
        if len(nets) != 2:
            continue
        ref, _num = ck
        comp, g, dup = info[ck]
        pins = list(g.pins or [])
        if len(pins) != 2:
            continue  # multi-pin device -> genuine dual-role pin, leave it
        other = next((p for p in pins if p.number != dup.number), None)
        if other is None:
            continue
        other_nets = occ.get((ref, other.number), [])
        if not other_nets:
            continue  # other pin free -> not a confirmed shunt
        if not any(n.name.upper() in shunt_targets for n in other_nets):
            continue  # other pin not on GND/rail -> not a shunt (series part)
        a, b = nets
        # A legit shunt-tap alias merges two SIGNAL / POWER node names; GND is
        # only ever on the part's OTHER pin, never one of the double-listed
        # nets. A pin double-listed on [SIGNAL, GND] is the erroneous-extra case
        # (an LED cathode the architect also dropped onto GND) -> the DROP rule
        # / validator owns it; folding the signal into GND would short it.
        if a.name.upper() in gnd or b.name.upper() in gnd:
            continue
        a_rail = a.name.upper() in rails
        b_rail = b.name.upper() in rails
        if a_rail and b_rail and a.name != b.name:
            continue  # two distinct rails -> a real short, leave for validator
        # When exactly ONE side is a power rail, the OTHER must be POWER-LIKE to
        # be an alias of it (J4_PWR ~ +5V). A plain signal double-listed with a
        # rail (LED_net + +5V) is an erroneous extra, not an alias -> leave it.
        if a_rail != b_rail:
            other_side = b if a_rail else a
            if not _is_power_like_net(other_side):
                continue
        # Both-SIGNAL fold (neither side a rail) requires the two names to be
        # RELATED -- a real tap is the same bus under decorated names (CAN_H /
        # CAN_TVS). Two UNRELATED signals on one pin (NRST + VCAP_1 on a
        # decoupling cap) are a spurious extra, not an alias; merging them would
        # short. Leave those for the DROP rule / validator.
        if not a_rail and not b_rail and not _names_related(a.name, b.name):
            continue
        other_names = {n.name for n in other_nets}
        if a.name in other_names or b.name in other_names:
            continue  # SERIES case -> _resolve_multinet_passives DROP rule owns it
        # keep the rail / more-connected / shorter name; fold the other in.
        if a_rail and not b_rail:
            keep, lose = a, b
        elif b_rail and not a_rail:
            keep, lose = b, a
        elif (len(a.pins), -len(a.name)) >= (len(b.pins), -len(b.name)):
            keep, lose = a, b
        else:
            keep, lose = b, a
        if lose.name == keep.name:
            continue
        lose.name = keep.name
        if getattr(keep, "is_power", False) or getattr(lose, "is_power", False):
            keep.is_power = True
            lose.is_power = True
        folds += 1
    return folds


def _regulator_markers() -> list:
    """Uppercase lib_id substrings marking a voltage regulator (its power_out
    pin DEFINES a rail). Config: normalize.regulator_lib_markers; fallback
    ['REGULATOR']."""
    cfg = _normalize_cfg().get("regulator_lib_markers")
    if isinstance(cfg, list) and cfg:
        return [str(s).upper() for s in cfg]
    return ["REGULATOR"]


def _is_regulator(comp) -> bool:
    up = (getattr(comp, "lib_id", "") or "").upper()
    return any(m in up for m in _regulator_markers())


def _power_input_markers() -> list:
    """Uppercase substrings in a PIN name that mark a board power INPUT /
    source (USB VBUS, a barrel-jack/regulator VIN) rather than a logic-rail
    load. The floating-power tie must NOT fold these into the logic rail --
    their upstream voltage is a design decision. Config:
    normalize.power_input_pin_markers; fallback ['VBUS', 'VIN']."""
    cfg = _normalize_cfg().get("power_input_pin_markers")
    if isinstance(cfg, list) and cfg:
        return [str(s).upper() for s in cfg]
    return ["VBUS", "VIN"]


def _fmt_rail(v: float) -> str:
    """Canonical rail name for a voltage: 3.3 -> '+3V3', 5 -> '+5V',
    1.8 -> '+1V8', 12 -> '+12V'."""
    if abs(v - round(v)) < 1e-9:
        return f"+{int(round(v))}V"
    whole = int(v)
    frac = int(round((v - whole) * 10))
    return f"+{whole}V{frac}"


def _parse_voltage_to_rail(*texts) -> Optional[str]:
    """Derive a regulator's OUTPUT rail from its part number / value:
    'AMS1117-3.3' -> '+3V3', 'LM2596S-5' -> '+5V', 'LM7812' -> '+12V'.
    Returns None when no FIXED voltage is parseable (e.g. an adjustable
    '-ADJ' regulator) so nothing is synthesised on a guess."""
    for t in texts:
        if not t:
            continue
        s = str(t).upper()
        if "ADJ" in s or "VAR" in s:
            return None
        # 78xx / 79xx fixed linear regs: LM7805 -> 5, L7812 -> 12
        m = re.search(r"7[89](\d\d)", s)
        if m:
            v = int(m.group(1))
            if 1 <= v <= 48:
                return _fmt_rail(float(v))
        # decimal forms: 3.3 / 3V3 / 2.5 / 1.8 (single leading digit)
        m = re.search(r"(?<!\d)(\d)[.V](\d)(?!\d)", s)
        if m:
            return _fmt_rail(float(f"{m.group(1)}.{m.group(2)}"))
        # trailing integer volts after a separator: AMS1117-5, ...-12
        m = re.search(r"[-_ ](\d{1,2})V?$", s)
        if m:
            v = int(m.group(1))
            if v in (3, 5, 9, 12, 15, 24):
                return _fmt_rail(float(v))
    return None


def _complete_power_tree(ir) -> int:
    """Complete the power tree a DEGRADED architect IR left open, without
    guessing the ambiguous parts:
      * synthesise a GND net when one is missing but ground pins exist, so
        the floating-ground tie (below) has a target; and
      * for each voltage REGULATOR, tie its floating power_OUT pin to the
        rail its part number defines (AMS1117-3.3 -> +3V3, LM2596S-5 ->
        +5V), creating that rail net if absent -- the regulator IS the
        authority on its own output rail.
    A regulator's INPUT pin is deliberately NOT auto-tied: its upstream rail
    (VIN_RAW / +5V / +12V) is a design decision, not derivable, so it stays
    for the architect (see the guard in _tie_floating_power_pins). Strictly
    additive -- only wires a pin that is currently floating. Returns the
    count wired."""
    from ..kicad.symbol_geom import load_symbol
    from .ir import IRNet

    gnd = _ground_aliases()
    gnd_markers = _ground_pin_markers()
    covered = {pr for net in ir.nets for pr in net.pins}
    fixed = 0

    # 1) synthesise GND when missing but some component exposes a ground pin.
    gnd_net = next((n for n in ir.nets if n.name.upper() in gnd), None)
    if gnd_net is None:
        has_ground = False
        for comp in ir.components:
            try:
                geom = load_symbol(comp.lib_id)
            except Exception:
                continue
            for pin in geom.pins or []:
                up = (pin.name or "").upper()
                if up in gnd or any(m in up for m in gnd_markers):
                    has_ground = True
                    break
            if has_ground:
                break
        if has_ground:
            gnd_net = IRNet(name="GND", pins=[], is_power=True)
            ir.nets.append(gnd_net)

    # 2) regulator output rail: tie a floating power_out to its derived rail.
    for comp in ir.components:
        if not _is_regulator(comp):
            continue
        try:
            geom = load_symbol(comp.lib_id)
        except Exception:
            continue
        rail_name = _parse_voltage_to_rail(getattr(comp, "value", ""),
                                           getattr(comp, "lib_id", ""))
        if rail_name is None:
            continue
        for pin in geom.pins or []:
            if pin.etype != "power_out":
                continue
            num_ref = f"{comp.ref}.{pin.number}"
            name_ref = f"{comp.ref}.{pin.name}"
            if num_ref in covered or name_ref in covered:
                continue
            rail_net = _net_by_name(ir, rail_name)
            if rail_net is None:
                rail_net = IRNet(name=rail_name, pins=[], is_power=True)
                ir.nets.append(rail_net)
            rail_net.pins.append(num_ref)
            covered.add(num_ref)
            fixed += 1

    # 3) tie ANY floating ground-named pin to GND regardless of etype -- a
    # USB/DC connector exposes GND as power_OUT, which the power_in-only tie
    # in _tie_floating_power_pins misses, but a ground pin is unambiguously
    # GND (never a source). VBUS/VIN are NOT ground-named so stay untouched.
    if gnd_net is not None:
        for comp in ir.components:
            try:
                geom = load_symbol(comp.lib_id)
            except Exception:
                continue
            for pin in geom.pins or []:
                up = (pin.name or "").upper()
                if not (up in gnd or any(m in up for m in gnd_markers)):
                    continue
                num_ref = f"{comp.ref}.{pin.number}"
                name_ref = f"{comp.ref}.{pin.name}"
                if num_ref in covered or name_ref in covered:
                    continue
                gnd_net.pins.append(num_ref)
                covered.add(num_ref)
                fixed += 1
    return fixed


def _tie_floating_power_pins(ir) -> int:
    """Tie any IC `power_in` pin absent from every net to the correct rail:
    ground-type names (VSS/AGND/...) -> GND; supply names (VDD/VCC/VBAT/VDDA/
    AVDD) -> the dominant logic rail. Mirrors the validator's POWER_PIN_FLOATING
    check exactly, so it clears that error. Only `power_in` is auto-tied — a
    floating `power_out` (a regulator output) has no derivable rail and is left
    for the architect. Fires only when the target rail already exists; an
    ambiguous/absent rail leaves the pin untouched. Returns the count tied."""
    from ..kicad.symbol_geom import load_symbol

    gnd = _ground_aliases()
    gnd_markers = _ground_pin_markers()
    sink_etypes = _power_pin_etypes()
    covered = {pr for net in ir.nets for pr in net.pins}
    gnd_net = next((n for n in ir.nets if n.name.upper() in gnd), None)
    rail_net = _net_by_name(ir, _dominant_logic_rail(ir))

    # A power SOURCE pin must not be auto-folded into the logic rail (that
    # would short an upstream input to the regulated output): a regulator's
    # supply input, and any pin named VBUS/VIN (USB / barrel-jack input).
    # Only its GND pin is safe to tie. Gated with complete_power_tree.
    guard_reg = _normalize_cfg().get("complete_power_tree", True)
    input_markers = _power_input_markers() if guard_reg else []
    fixed = 0
    for comp in ir.components:
        try:
            geom = load_symbol(comp.lib_id)
        except Exception:
            continue
        is_reg = guard_reg and _is_regulator(comp)
        for pin in geom.pins or []:
            if pin.etype not in sink_etypes:
                continue
            num_ref = f"{comp.ref}.{pin.number}"
            name_ref = f"{comp.ref}.{pin.name}"
            if num_ref in covered or name_ref in covered:
                continue
            up = (pin.name or "").upper()
            is_gnd = up in gnd or any(m in up for m in gnd_markers)
            if not is_gnd and (is_reg or any(mk in up for mk in input_markers)):
                continue  # source/input rail is upstream — a design decision
            target = gnd_net if is_gnd else rail_net
            if target is None:
                continue
            target.pins.append(num_ref)
            covered.add(num_ref)
            fixed += 1
    return fixed


def _tie_crystal_caps_gnd(ir) -> int:
    """For each crystal, tie each adjacent load cap's free (non-crystal) pin to
    GND. Mirrors the validator's CRYSTAL_LOAD_CAP_NO_GND check. Strictly
    additive: only adds the GND membership when the cap's other pin is not
    already wired anywhere (so it never double-lists a pin / creates a short).
    Returns the count of cap pins grounded."""
    from ..kicad.symbol_geom import load_symbol

    gnd = _ground_aliases()
    gnd_net = next((n for n in ir.nets if n.name.upper() in gnd), None)
    if gnd_net is None:
        return 0
    used = {pr for net in ir.nets for pr in net.pins}
    xtal_patterns = _crystal_trigger_patterns()
    cap_prefixes = _capacitor_prefixes()

    def _is_cap_ref(ref: str) -> bool:
        parsed = parse_refdes(ref)
        return parsed is not None and parsed[0] in cap_prefixes

    fixed = 0
    for xtal in ir.components:
        if not _lib_matches_any(xtal.lib_id, xtal_patterns):
            continue
        xtal_nets = [n for n in ir.nets
                     if any(p.startswith(f"{xtal.ref}.") for p in n.pins)]
        cap_refs = {p.split(".", 1)[0] for net in xtal_nets for p in net.pins
                    if _is_cap_ref(p.split(".", 1)[0])
                    and p.split(".", 1)[0] != xtal.ref}
        for cref in cap_refs:
            comp = ir.component_by_ref(cref)
            if comp is None:
                continue
            try:
                g = load_symbol(comp.lib_id)
            except Exception:
                continue
            pins = list(g.pins or [])
            if len(pins) != 2:
                continue
            if any(p.startswith(f"{cref}.") for p in gnd_net.pins):
                continue  # already grounded
            xtal_pin_nums = set()
            for net in xtal_nets:
                for pr in net.pins:
                    if pr.startswith(f"{cref}."):
                        rp = g.resolve_pin(pr.split(".", 1)[1])
                        if rp is not None:
                            xtal_pin_nums.add(rp.number)
            other = next((p for p in pins if p.number not in xtal_pin_nums), None)
            if other is None:
                continue
            other_ref = f"{cref}.{other.number}"
            if other_ref in used or f"{cref}.{other.name}" in used:
                continue  # other pin already wired -> leave for the architect
            gnd_net.pins.append(other_ref)
            used.add(other_ref)
            fixed += 1
    return fixed


def _tie_floating_led_cathode(ir) -> int:
    """Return a floating LED CATHODE to GND. DYNAMIC + symbol-grounded -- not net-
    name based: any 2-pin part whose lib_id contains 'LED', whose CATHODE pin
    (name 'K'/'CATHODE', else pin number '1' on Device:LED) is UNWIRED or on a
    single-pin NET_FLOATING stub WHILE its ANODE is wired, gets its cathode tied
    to GND. Indicator LEDs are active-high (cathode->GND); fires ONLY on a
    genuinely floating cathode, so it can only clear a NET_FLOATING error and
    never alters a working LED topology. Mirrors the live CAN-logger failure
    (CAN_LED_K / SD_LED_K floating). Config-gated; never raises."""
    from .ir import IRNet
    from ..kicad.symbol_geom import load_symbol

    gnd_aliases = _ground_aliases()
    gnd_net = next((n for n in ir.nets if n.name.upper() in gnd_aliases), None)
    if gnd_net is None:
        gnd_net = IRNet(name="GND", pins=[], is_power=True)
        ir.nets.append(gnd_net)
    fixed = 0
    emptied_ids = set()
    for comp in ir.components:
        if "LED" not in (comp.lib_id or "").upper():
            continue
        try:
            geom = load_symbol(comp.lib_id)
        except Exception:
            continue
        pins = geom.pins or []
        if len(pins) != 2:
            continue
        kpin = next((p for p in pins
                     if (p.name or "").upper() in ("K", "CATHODE", "KATHODE", "C")), None)
        if kpin is None:
            kpin = next((p for p in pins if str(p.number) == "1"), None)
        if kpin is None:
            continue
        apin = next((p for p in pins if p is not kpin), None)
        ktok = {f"{comp.ref}.{kpin.number}", f"{comp.ref}.{kpin.name}"}
        atok = ({f"{comp.ref}.{apin.number}", f"{comp.ref}.{apin.name}"}
                if apin else set())
        cath_nets = [n for n in ir.nets if any(t in n.pins for t in ktok)]
        anode_wired = any(any(t in n.pins for t in atok) for n in ir.nets)
        if not anode_wired:
            continue  # whole LED floating -> leave for the architect
        if any(n is gnd_net for n in cath_nets):
            continue  # already grounded
        if not cath_nets:
            gnd_net.pins.append(f"{comp.ref}.{kpin.number}")
            fixed += 1
        elif len(cath_nets) == 1 and len(cath_nets[0].pins) == 1:
            stub = cath_nets[0]
            gnd_net.pins.append(stub.pins[0])
            stub.pins = []
            emptied_ids.add(id(stub))
            fixed += 1
    if emptied_ids:
        ir.nets = [n for n in ir.nets if id(n) not in emptied_ids]
    return fixed


def _intent_stub_targets() -> dict:
    """Token -> rail-kind map for _fold_intent_named_stubs. Config override
    at normalize.intent_stub_targets; inline fallback covers the architect's
    standard pull/strap/analog-supply net-naming. 'vcc' = dominant logic
    rail, 'gnd' = board ground."""
    cfg = _normalize_cfg().get("intent_stub_targets")
    if isinstance(cfg, dict) and cfg:
        return {str(k).upper(): str(v).lower() for k, v in cfg.items()}
    return {
        "PU": "vcc", "PUP": "vcc", "PULLUP": "vcc",
        "PD": "gnd", "PDN": "gnd", "PULLDOWN": "gnd", "STRAP": "gnd",
        "VDDA": "vcc", "AVDD": "vcc", "VREF": "vcc",
    }


def _fold_intent_named_stubs(ir) -> int:
    """Complete a SINGLE-PIN net whose NAME encodes a rail INTENT by renaming
    it to that rail, so the by-name merge folds its lone pin into the real
    rail and finishes the connection the architect started:

      *_PU / *_PULLUP   -> the dominant logic rail (a pull-up returns to VCC)
      *_PD / *_STRAP    -> GND          (a pull-down / boot strap returns low)
      *_VDDA / *_AVDD   -> the dominant logic rail (analog supply rail stub)

    This is the dominant residual the self-test surfaced on large data-logger
    boards: the architect declares `NRST_PU` / `SD_DET_PU` / `BOOT0_PD` and
    leaves it floating (NET_FLOATING), or splits the analog supply onto a
    `VDDA_RAIL` stub AND lists VDDA on +3V3 too (PIN_IN_MULTIPLE_NETS). Folding
    the stub into its target rail completes the pull-up resistor's missing leg
    AND collapses the alias in one move.

    Conservative + provably non-destructive: fires ONLY on a single-pin,
    non-power net carrying an intent TOKEN, and only when the target rail
    already exists (never invents a rail). A single-pin net is already a hard
    error, so folding it can only remove errors, never create a short. Matches
    on `_`-delimited tokens (not substrings) so an unrelated signal name is
    never caught. Config-gated; never raises."""
    targets = _intent_stub_targets()
    if not targets:
        return 0
    gnd_aliases = _ground_aliases()
    dom_vcc = _dominant_logic_rail(ir)
    gnd_name = next((n.name for n in ir.nets
                     if n.name.upper() in gnd_aliases), None)
    changed = 0
    for net in ir.nets:
        if getattr(net, "is_power", False) or len(net.pins) != 1:
            continue
        up = net.name.upper()
        toks = [t for t in re.split(r"[^A-Z0-9]+", up) if t]
        kind = None
        for tok in toks:
            if tok in targets:
                kind = targets[tok]
                break
        if kind is None:
            continue
        target = dom_vcc if kind == "vcc" else gnd_name
        if not target or target == net.name:
            continue
        net.name = target
        net.is_power = True
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# Top-level: normalise an entire IR in place
# ---------------------------------------------------------------------------

def _resolve_ic_lib_ids(ir) -> list:
    """Correct hallucinated IC lib_ids against the real symbol libraries,
    keyed on each part's VALUE (MPN). The architect names the part right in
    ``value`` but frequently guesses a wrong/fake ``lib_id`` — a wrong
    library ('Sensor_Battery:BQ76952' vs the real 'Battery_Management:
    BQ7695201PFBR') or a different real part ('Amplifier_Operational:TL072'
    for an INA240). Both wreck the build: a wrong-but-valid symbol loads,
    then every real pin wired onto it fails PIN_NOT_ON_SYMBOL and the repair
    loop grinds for minutes on an unfixable mismatch. Here we resolve each
    IC (refdes 'U', or any component whose current lib_id won't load) to the
    best VALUE-matching symbol across ALL libraries and rewrite the lib_id
    BEFORE validate sees it. Conservative: an IC whose lib_id already loads
    is replaced only when the value matches a different symbol clearly
    better (by ``margin``), so a correct generic part is never churned.
    Returns warning strings naming each correction. Config:
    normalize.resolve_lib_ids (default true)."""
    try:
        from .engine import _load_layout_config
        _cfg = (_load_layout_config().get("normalize", {}) or {})
        if not _cfg.get("resolve_lib_ids", True):
            return []
        margin = float(_cfg.get("resolve_lib_ids_margin", 0.15))
    except Exception:
        margin = 0.15
    try:
        from ..kicad.symbol_geom import (load_symbol, resolve_lib_id_by_value,
                                          _tokenize_part, _score_candidate)
    except Exception:
        return []
    warns: list = []
    for comp in ir.components:
        lib_id = getattr(comp, "lib_id", "") or ""
        value = getattr(comp, "value", "") or ""
        ref = getattr(comp, "ref", "") or ""
        if ":" not in lib_id or not value:
            continue
        is_ic = ref[:1].upper() == "U"
        cur_loads = True
        try:
            load_symbol(lib_id)
        except Exception:
            cur_loads = False
        # A non-IC that already loads (passive, generic Q/D, connector) is
        # left untouched — only ICs and genuinely-broken ids are resolved.
        if cur_loads and not is_ic:
            continue
        best, score, _cands = resolve_lib_id_by_value(value, lib_id)
        if not best or best == lib_id:
            continue
        cur_part = lib_id.split(":", 1)[1]
        cur_score = _score_candidate(_tokenize_part(value), cur_part)
        if (not cur_loads) or (score > cur_score + margin):
            # Issue DICT (matches every other normalize warning). Previously this
            # appended a bare STRING, which crashed validate_only_node's
            # `i["severity"]` iteration (TypeError: string indices) whenever an IC
            # lib_id was value-matched — e.g. AMS1117.
            warns.append({
                "code": "LIB_ID_RESOLVED",
                "severity": "warning",
                "where": "normalize",
                "text": (f"lib_id resolved: {ref} '{value}' {lib_id} -> {best} "
                         f"(value-match {score:.2f} vs current {cur_score:.2f})"),
            })
            comp.lib_id = best
    return warns


def _normalize_block_names(ir) -> list:
    """Map an unregistered architect block name onto the closest REGISTERED
    name BEFORE validate, so a descriptive name doesn't hard-fail as
    BLOCK_NAME_NOT_IN_REGISTRY and spin the repair loop.

    FULLY DYNAMIC — no per-circuit synonym table. For each unregistered
    block we score every registered name by token overlap (the same scorer
    symbol_geom uses for parts) against TWO generic signals the architect
    already emits: the block's own ``name`` and its ``block_type`` (a
    controlled vocabulary: regulator / sensor / analog / mcu / comm /
    driver / io / protection / ...). The block_type is what makes this
    universal: 'BATTERY_MONITOR'(sensor)->SENSOR, 'CURRENT_SENSE'(analog)->
    ANALOG, 'LOAD_CONTROL'(driver)->MOTOR_DRIVER, 'MCU_CORE'(mcu)->MCU —
    every board, no hardcoded list. ``block_name_aliases`` in
    block_naming.json is consulted FIRST only as an optional manual override
    (empty by default). A rename colliding with an existing block name is
    skipped (avoids BLOCK_NAME_DUPLICATE). Gated by
    normalize.normalize_block_names (default true)."""
    blocks = getattr(ir, "blocks", None) or []
    if not blocks:
        return []
    try:
        from .engine import _load_layout_config
        _ncfg = (_load_layout_config().get("normalize", {}) or {})
        if not _ncfg.get("normalize_block_names", True):
            return []
        min_score = float(_ncfg.get("block_name_match_min_score", 0.6))
    except Exception:
        min_score = 0.6
    try:
        import json as _json
        from pathlib import Path as _Path
        cfg_dir = _Path(__file__).resolve().parent.parent / "config"
        naming = _json.loads(
            (cfg_dir / "block_naming.json").read_text(encoding="utf-8"))
        aliases = {k.upper(): v
                   for k, v in (naming.get("block_name_aliases") or {}).items()}
        registry = list((naming.get("blocks") or {}).keys())
    except Exception:
        return []
    if not registry:
        return []
    try:
        from ..kicad.symbol_geom import _tokenize_part, _score_candidate
    except Exception:
        return []
    registry_set = set(registry)

    def _best_registered(source: str):
        """Best registered match for a free-text token source (a block name
        or a block_type). Returns a sort key (exact, score, -len, cand) so
        callers can compare candidates from different sources directly.
        Tie-break order: an EXACT normalized equality wins first (so the
        short token 'io' picks 'IO', not 'PROTECTION' which merely CONTAINS
        'io'), then higher token-overlap score, then the shorter registered
        name (most fully consumed). Returns None when nothing matches."""
        req = _tokenize_part(source or "")
        if not req:
            return None
        src_norm = "".join(req)
        best = None  # (exact, score, -len, cand)
        for cand in registry:
            s = _score_candidate(req, cand)
            cand_norm = "".join(_tokenize_part(cand))
            exact = 1 if cand_norm == src_norm else 0
            key = (exact, s, -len(cand_norm), cand)
            if best is None or key > best:
                best = key
        return best

    existing = {getattr(b, "name", "") for b in blocks}
    warns: list = []
    for blk in blocks:
        raw = getattr(blk, "name", "") or ""
        name = raw.upper()
        if name in registry_set:
            continue  # already a valid registered name
        # 1. optional manual override (empty by default — kept as an escape
        #    hatch for a name the fuzzy resolver gets wrong, NOT a per-circuit
        #    table).
        target = aliases.get(name)
        source = "alias"
        if not target:
            # 2. dynamic: best registered match of the NAME or the BLOCK_TYPE.
            #    block_type carries the semantic intent a descriptive name
            #    often hides; the shared sort key picks the better of the two.
            btype = getattr(blk, "block_type", "") or ""
            cands = [c for c in (_best_registered(raw), _best_registered(btype))
                     if c is not None]
            if not cands:
                continue
            exact, score, _nl, target = max(cands)
            source = "type/name"
            # Accept an exact token-equality always; otherwise require the
            # fuzzy score to clear the confidence floor.
            if not exact and score < min_score:
                continue  # no confident match -> leave for the validator
        if not target or target == name:
            continue
        if target in existing and target != raw:
            continue  # would duplicate an existing block -> leave for validator
        warns.append(f"block name normalized ({source}): {raw!r} -> {target!r}")
        existing.discard(raw)
        blk.name = target
        existing.add(target)
    return warns


def normalize_ir(ir) -> list:
    """Apply value + label + refdes normalisation to a TopologyIR. Returns
    a list of WARNINGS (refdes outside ASME Y14.44 etc.) — never raises;
    silent normalisation is preferred so the architect's IR survives
    minor stylistic flubs."""
    warnings = []
    # Correct hallucinated IC lib_ids against the real symbol libraries
    # (keyed on the part VALUE) BEFORE any downstream step. A wrong/fake
    # symbol id ('Sensor_Battery:BQ76952', or 'Amplifier_Operational:TL072'
    # standing in for an INA240) otherwise cascades into PIN_NOT_ON_SYMBOL
    # and a futile multi-minute repair loop — the build's #1 hard-failure.
    try:
        warnings.extend(_resolve_ic_lib_ids(ir))
    except Exception:
        pass
    # Map block-name synonyms onto registered names (MCU_CORE -> MCU) so an
    # architect's descriptive name doesn't hard-fail as BLOCK_NAME_NOT_IN_
    # REGISTRY and spin the repair loop.
    try:
        warnings.extend(_normalize_block_names(ir))
    except Exception:
        pass
    # Refdes prefix aliasing: map informal/vendor prefixes onto their ASME
    # Y14.44 letter (e.g. KiCad's default switch prefix "SW" -> "S"). Runs
    # FIRST so validation/render see the corrected ref. Renames the
    # component AND every site its ref appears (net pins, block membership,
    # anchor pins) so the IR stays internally consistent. A rename that
    # would collide with an existing ref is skipped (keeps the original +
    # its warning rather than silently merging two parts). Config-driven
    # (normalize.refdes_prefix_aliases) — generalises to any prefix.
    alias = _refdes_aliases()
    if alias:
        ref_rename: Dict[str, str] = {}
        used = {c.ref for c in ir.components}
        for comp in ir.components:
            parsed = parse_refdes(comp.ref)
            if parsed is None:
                continue
            prefix, num = parsed
            new_prefix = alias.get(prefix.upper())
            if not new_prefix or new_prefix == prefix:
                continue
            candidate = f"{new_prefix}{num}"
            if candidate in used:
                continue  # collision -> leave as-is for the validator
            ref_rename[comp.ref] = candidate
            used.add(candidate)
        if ref_rename:
            def _remap_pinref(pr: str) -> str:
                if "." in pr:
                    r, k = pr.split(".", 1)
                    if r in ref_rename:
                        return f"{ref_rename[r]}.{k}"
                return pr
            for comp in ir.components:
                comp.ref = ref_rename.get(comp.ref, comp.ref)
            for net in ir.nets:
                net.pins = [_remap_pinref(pr) for pr in net.pins]
            for blk in getattr(ir, "blocks", []) or []:
                blk.component_refs = [ref_rename.get(r, r) for r in blk.component_refs]
                blk.anchor_pins = [_remap_pinref(pr) for pr in blk.anchor_pins]
    # Components: values + refdes
    for comp in ir.components:
        comp.value = normalize_value(comp.value, comp.ref)
        warn = validate_refdes(comp.ref)
        if warn:
            warnings.append({"code": "REFDES_NONSTANDARD",
                              "severity": "warning",
                              "where": comp.ref, "text": warn})
    # Nets: label names
    for net in ir.nets:
        net.name = normalize_label(net.name)
        # Pin references on this net — only the part AFTER the dot might
        # carry an active-low name; preserve the ref part.
        new_pins = []
        for pinref in net.pins:
            if "." in pinref:
                comp_ref, pin_key = pinref.split(".", 1)
                # Don't normalise pin numbers ("8" stays "8") or names
                # already in overbar form. Other forms like "nRESET" on
                # an IC's pin do get normalised so they match how the
                # symbol library exposes the name.
                normalised_key = normalize_label(pin_key) if not pin_key.isdigit() else pin_key
                # Canonicalise to the symbol's actual pin name so every
                # downstream stage (validate, render, ERC) sees a name the
                # symbol really exposes. Leaves the token untouched when it
                # can't be resolved.
                canon = _canonical_pin_name(ir, comp_ref, normalised_key)
                if canon:
                    normalised_key = canon
                new_pins.append(f"{comp_ref}.{normalised_key}")
            else:
                new_pins.append(pinref)
        net.pins = new_pins

    # Resolve PIN_IN_MULTIPLE_NETS for project-declared net ties. A pin may
    # legitimately bridge two nets only when those nets are the SAME
    # electrical node (e.g. VBAT tied to +3V3 on a no-coin-cell STM32
    # board). When the project declares that tie (normalize.tie_nets), both
    # nets are renamed to the group's canonical rail so the name-merge below
    # collapses them into one. A pin spanning nets NOT in any declared group
    # is left untouched -> the validator still reports it as a real wiring
    # error. Opt-in; default [] changes nothing. See
    # [feedback_clean_correct_circuits].
    tie_groups = _tie_groups()
    if tie_groups:
        canon_of: dict = {}
        for group in tie_groups:
            canonical = normalize_label(group[0])
            for nm in group:
                canon_of[normalize_label(nm)] = canonical
        pin_nets: dict = {}
        for net in ir.nets:
            for pr in net.pins:
                pin_nets.setdefault(pr, set()).add(net.name)
        tie_rename: dict = {}
        for pr, names in pin_nets.items():
            if len(names) < 2:
                continue
            canons = {canon_of.get(nm) for nm in names}
            if len(canons) == 1 and None not in canons:
                target = next(iter(canons))
                for nm in names:
                    if nm != target:
                        tie_rename[nm] = target
        if tie_rename:
            for net in ir.nets:
                if net.name in tie_rename:
                    net.name = tie_rename[net.name]

    # Rail-alias collapse — deterministic safety net for the two architect
    # slips that dominate multi-block MCU boards (see the CAN-logger build):
    #   (a) a BARE rail token used as a pin endpoint -- "GND" with no dot --
    #       which fires PIN_MALFORMED and strands the rail across two nets; and
    #   (b) a single-pin "*_GND" / "*_VCC" connector net (UART_GND, SD_VCC)
    #       the architect forgot to fold into the global rail, firing
    #       NET_FLOATING.
    # Each has exactly ONE correct resolution -- the named global rail -- so
    # we fix it here instead of bouncing the architect through a retry. The
    # genuinely AMBIGUOUS overlaps (a decoupling/VCAP cap pin double-listed on
    # +3V3 AND VCAP_x, an LED resistor pin on GND AND the LED node) are left
    # for the validator/architect, since auto-picking a side would be wrong
    # for at least one topology. Config-gated; see
    # [feedback_non_breaking_changes].
    if _normalize_cfg().get("merge_floating_connector_rails", True):
        rails_upper = _canonical_rails()
        # (a) bare rail-token endpoints -> rename the net to that rail and
        # drop the token; the by-name merge below folds the real pins in.
        # Also folds a NUMBERED ground token (GND2 / GND3 / AGND2 -> base ground)
        # -- the architect's duplicate-ground slip on big multi-ground boards
        # (the 48V BMS PIN_MALFORMED @ GND2/3/4). General: any <ground><digits>.
        _gnd_bare = _ground_aliases()

        def _bare_rail_base(tok):
            up = (tok or "").upper()
            if up in rails_upper:
                return up
            m = re.fullmatch(r"([A-Z+]+)(\d+)", up)   # GND2 -> GND (ground only)
            if m and m.group(1) in _gnd_bare:
                return m.group(1)
            return None

        for net in ir.nets:
            bare, base = None, None
            for pr in net.pins:
                if "." in pr:
                    continue
                base = _bare_rail_base(pr)
                if base is not None:
                    bare = pr
                    break
            if bare is None:
                continue
            net.pins = [pr for pr in net.pins if pr != bare]
            net.name = base
            net.is_power = True
        # (a2) a no-dot token that is a rail name WITH A QUALIFIER suffix
        # ("+3V3_NRST_PU", "+5V_SENSE", "GND_ANALOG") -- the architect appended
        # a role to a rail name and then dropped it in as a pin endpoint. The
        # host net IS that rail; rename it + drop the token, generalising (a)
        # to compound names. Longest rail prefix wins so "+3V3" beats "+3".
        for net in ir.nets:
            comp_tok, matched_rail = None, None
            for pr in net.pins:
                if "." in pr:
                    continue
                up = pr.upper()
                if up in rails_upper:
                    continue  # plain rail token -> handled by (a) above
                best = None
                for rail in rails_upper:
                    if up.startswith(rail + "_") and (best is None or len(rail) > len(best)):
                        best = rail
                if best is not None:
                    comp_tok, matched_rail = pr, best
                    break
            if comp_tok is not None:
                net.pins = [pr for pr in net.pins if pr != comp_tok]
                net.name = matched_rail
                net.is_power = True
        # (b) single-pin connector rail nets -> the matching global rail.
        # Only merge when the target rail already exists (never invent one)
        # and, for the VCC side, only when there's a single unambiguous
        # logic rail to fold into.
        present = {n.name.upper(): n.name
                   for n in ir.nets if getattr(n, "is_power", False)}
        gnd_aliases = _ground_aliases()
        logic_rails = [orig for up, orig in present.items()
                       if up not in gnd_aliases]
        # A connector stub's rail keyword can sit anywhere in the name and
        # carry an index or qualifier the architect appended (SWD_GND2,
        # CAN_VCC_CONN, SD_VDD_3). Match it per `_`-delimited token, tolerating
        # a trailing index, instead of a rigid end-of-string suffix -- so every
        # naming variant of the same floating rail stub folds, not just the
        # canonical `*_GND` / `*_VCC` spellings.
        def _tok_is(tok: str, kw: str) -> bool:
            return tok == kw or re.fullmatch(re.escape(kw) + r"\d+", tok) is not None

        for net in ir.nets:
            if len(net.pins) != 1:
                continue
            up = net.name.upper()
            toks = [t for t in up.split("_") if t]
            target = None
            if up in gnd_aliases or any(_tok_is(t, "GND") or t in gnd_aliases
                                        for t in toks):
                target = present.get("GND")
            elif any(_tok_is(t, "3V3") for t in toks) and "+3V3" in present:
                target = "+3V3"
            elif any(_tok_is(t, "5V") for t in toks) and "+5V" in present:
                target = "+5V"
            elif any(_tok_is(t, "VCC") or _tok_is(t, "VDD") or _tok_is(t, "VBAT")
                     for t in toks):
                # A generic connector supply pin (UART_VCC, SD_VDD). On a
                # single-rail board it can only mean that rail. On a complex
                # multi-rail board (the CAN-logger has VIN_RAW/VIN_FUSED/
                # +3V3/VDDA) pick the dominant LOGIC rail by canonical
                # priority -- a connector's VCC is the MCU 3V3/5V rail, never
                # the raw input or analog rail. Still conservative: if none of
                # the preferred rails is present, leave it for the architect.
                if len(logic_rails) == 1:
                    target = logic_rails[0]
                else:
                    present_logic = {r.upper(): r for r in logic_rails}
                    for pref in _vcc_rail_priority():
                        if pref in present_logic:
                            target = present_logic[pref]
                            break
            if target and target != net.name:
                net.name = target
                net.is_power = True

    # Intent-named single-pin stub fold -- the architect declares a pull/strap/
    # analog-supply net (NRST_PU, SD_DET_PU, BOOT0_PD, VDDA_RAIL) but leaves it
    # with one pin (NET_FLOATING), or splits VDDA onto a stub AND +3V3
    # (PIN_IN_MULTIPLE_NETS). Renaming the stub to its target rail (PU->VCC,
    # PD/STRAP->GND, VDDA->VCC) lets the by-name merge complete the pull-up's
    # missing leg and collapse the alias. The dominant residual on large
    # data-logger boards (self-test 2026-06-06). Runs BEFORE the by-name merge.
    # Config-gated; never raises.
    if _normalize_cfg().get("fold_intent_named_stubs", True):
        try:
            _fold_intent_named_stubs(ir)
        except Exception:
            pass

    # Shared-pin ground fold -- a single physical pin on BOTH the global GND
    # and a `<BASE>_GND` bus/connector reference (CAN_GND, RS485_GND, SHIELD_GND)
    # is a galvanic tie that proves the two grounds are one node. On a non-
    # isolated transceiver the bus ground reference IS the board ground (ISO
    # 11898-2 requires the shared reference), so we fold `<BASE>_GND` into GND.
    # Deliberate split grounds (ANALOG_GND / DGND / CHASSIS_GND) are excluded.
    # This was the lone recurring PIN_IN_MULTIPLE_NETS that bounced the
    # STM32F405 CAN-logger architect through three non-converging retries
    # (J4.Pin_2 on [GND, CAN_GND]). Runs BEFORE the merges below so the renamed
    # net collapses. Config-gated; never raises. See _merge_shared_pin_ground_nets.
    _unified_multinet = _normalize_cfg().get("unified_multinet_resolver", False)
    if (not _unified_multinet
            and _normalize_cfg().get("merge_shared_pin_ground_nets", True)):
        try:
            _merge_shared_pin_ground_nets(ir)
        except Exception:
            pass

    # Net-alias merge -- two nets that share >= 2 physical pins are the SAME
    # electrical node under two names (NRST + SWD_NRST, the dominant reset-net
    # slip on the CAN-logger build). Runs BEFORE the by-name merge so the alias
    # collapses to one net; the single-shared-pin bridge case is left to
    # _resolve_multinet_passives. Config-gated; never raises.
    if _normalize_cfg().get("merge_net_aliases", True):
        try:
            _merge_net_aliases(ir)
        except Exception:
            pass

    # Shunt-tap alias fold -- a 2-terminal SHUNT part (TVS/filter/decoupling,
    # other pin on GND/rail) whose signal pin is double-listed on [MAIN, alias]
    # proves the two names are one node; fold the alias into the canonical name.
    # This is the dominant attempt-1 PIN_IN_MULTIPLE_NETS (D2.A on [CAN_H,
    # CAN_TVS]; C3.1 on [+5V, J4_PWR]). The SERIES case (LED series resistor) is
    # left to _resolve_multinet_passives' DROP rule. Runs BEFORE the by-name
    # merge so the rename collapses. Config-gated; never raises.
    if (not _unified_multinet
            and _normalize_cfg().get("fold_shunt_tap_aliases", True)):
        try:
            _fold_shunt_tap_aliases(ir)
        except Exception:
            pass

    # Merge nets that share a name after normalisation. When the architect
    # decomposes a board into functional blocks it routinely emits the
    # shared rail (GND, +3V3, +5V) as ONE net entry PER BLOCK. Those are
    # the SAME KiCad net (a net is identified by name), but the validator
    # rejected them as NET_DUPLICATE — which made MCU block-layouts fail
    # validation and fall back to the flat star. Merging is always correct:
    # union the pins, OR the is_power flag. Pure data normalisation, no
    # per-circuit logic. See [feedback_non_breaking_changes].
    by_name: dict = {}
    merged = []
    for net in ir.nets:
        existing = by_name.get(net.name)
        if existing is None:
            by_name[net.name] = net
            merged.append(net)
            continue
        seen = set(existing.pins)
        for pr in net.pins:
            if pr not in seen:
                existing.pins.append(pr)
                seen.add(pr)
        if getattr(net, "is_power", False):
            existing.is_power = True
    # De-duplicate pins WITHIN each surviving net too (a pin listed twice
    # on the same net is the other half of the NET_DUPLICATE symptom).
    for net in merged:
        net.pins = list(dict.fromkeys(net.pins))
    if len(merged) != len(ir.nets):
        ir.nets = merged

    # Tie floating IC power_in pins (the VBAT/VDDA/VDD the architect forgot) to
    # the right rail, and tie each crystal load cap's free pin to GND. Both
    # mirror the validator's POWER_PIN_FLOATING / CRYSTAL_LOAD_CAP_NO_GND checks
    # exactly and only ADD a connection the rule already demands, so they are
    # strictly additive. Run AFTER the by-name merge so the global GND / rail
    # nets are already consolidated. Config-gated; never raise.
    # Complete the power tree FIRST (synthesise GND + regulator output rails)
    # so the floating-power tie below has rails to fold the loads into even
    # when a degraded architect attempt dropped them. Config-gated; never raise.
    if _normalize_cfg().get("complete_power_tree", True):
        try:
            _complete_power_tree(ir)
        except Exception:
            pass
    if _normalize_cfg().get("tie_floating_power_pins", True):
        try:
            _tie_floating_power_pins(ir)
        except Exception:
            pass
    if _normalize_cfg().get("tie_crystal_caps_gnd", True):
        try:
            _tie_crystal_caps_gnd(ir)
        except Exception:
            pass
    # Return a floating LED cathode to GND (CAN_LED_K / SD_LED_K NET_FLOATING on
    # the live CAN logger). Legacy per-component function -- DEFAULT OFF now that
    # the data-driven engine below covers LED via a single config rule. Kept as a
    # fallback; set tie_floating_led_cathode=true to use it instead of the engine.
    if _normalize_cfg().get("tie_floating_led_cathode", False):
        try:
            _tie_floating_led_cathode(ir)
        except Exception:
            pass

    # ONE data-driven engine: connect EVERY floating pin to its correct net from
    # the pin_completion.rules table -- ground pins, LED cathode, crystal caps,
    # supply pins, and any FUTURE part -- with NO per-component function. Adding a
    # component = one JSON rule. Runs last so rails/GND are consolidated.
    # Config-gated (pin_completion.enabled); never raises.
    try:
        from .pin_complete import complete_floating_pins
        complete_floating_pins(ir)
    except Exception:
        pass

    # Resolve PIN_IN_MULTIPLE_NETS on 2-terminal parts (R/C/L/ferrite/diode)
    # deterministically: move the functional net to the free pin, or drop a
    # redundant rail/short membership. Runs LAST, on the merged+deduped nets,
    # so it sees each pin's final net set. Config-gated; never raises.
    # See _resolve_multinet_passives + [feedback_non_breaking_changes].
    # Unified path: ONE classify->act resolver replacing the ground-fold +
    # shunt-fold + 2-terminal drop/move trio (all skipped above when on). Falls
    # back to the proven scattered resolver when off (default). Config-gated;
    # never raises. See _resolve_pin_multinet.
    if _unified_multinet:
        try:
            _resolve_pin_multinet(ir)
        except Exception:
            pass
    elif _normalize_cfg().get("resolve_multinet_passives", True):
        try:
            _resolve_multinet_passives(ir)
        except Exception:
            pass  # a repair miss must never block normalisation

    # Design-checklist completion -- the validator's _run_design_checklist
    # DETECTS missing SHUNT support parts (reset pull-up, LDO/crystal caps,
    # USB-C CC pull-downs) for any part via config patterns but only reports
    # them, so that class bounces to the architect and diverges on big boards.
    # This pass SYNTHESISES the missing part from the SAME config and wires it
    # from the offending pin's net to the named rail/GND -- deterministic,
    # part-agnostic, strictly additive (shunt-only, never creates a short).
    # Runs LAST so the rails it attaches to (built by _complete_power_tree +
    # the by-name merge above) already exist. Config-gated, default off until
    # live-verified; never raises. See intent/checklist_repair.py.
    if _normalize_cfg().get("complete_design_checklist", False):
        try:
            from .checklist_repair import complete_design_checklist
            complete_design_checklist(ir)
        except Exception:
            pass

    # Verified-block expansion (Phase 2) — deterministic instantiation of a
    # stored, known-good support sub-circuit anchored on a matched IC (e.g. an
    # INA240 current-sense front-end: shunt + input filter + Vs decoupling + the
    # internal wiring). General + config-driven (config/verified_blocks/*.json),
    # any anchor part can have a template, zero code. Internal nets are
    # namespaced by the anchor refdes so two instances can't short; support pins
    # join the anchor's EXISTING rails; the architect keeps the boundary
    # (OUT->ADC, REF->bias). Runs AFTER complete_design_checklist (so its skip_if
    # sees any shunt that pass added) and BEFORE _enforce_pin_ownership (so the
    # appended rail pins get the final ownership sweep). Config-gated, default
    # OFF; never raises. See intent/verified_blocks.py.
    if _normalize_cfg().get("expand_verified_blocks", False):
        try:
            from .verified_blocks import expand_verified_blocks
            expand_verified_blocks(ir)
        except Exception:
            pass

    # GLOBAL one-net-per-pin guarantee — runs DEAD LAST, after every merge/tie/
    # resolver above, so it enforces the invariant on the final net set. Merges
    # only provably-same-node nets (same name / 2+ shared pins / same rail);
    # leaves genuine mis-wires for the validator. Config-gated; never raises.
    if _normalize_cfg().get("global_pin_ownership", True):
        try:
            _enforce_pin_ownership(ir)
        except Exception:
            pass
    return warnings
