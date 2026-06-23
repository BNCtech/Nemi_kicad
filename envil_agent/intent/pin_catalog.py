"""Pin catalog — turn a list of lib_ids into a compact text block the
architect prompt can inject so the LLM never has to guess pin names.

The catalog is built from the LIVE `.kicad_sym` library (via
``kicad.symbol_geom.load_symbol``). One line per lib_id; each line lists
the pin names (and optionally numbers + electrical types) up to the
configured cap. Bad lib_ids degrade gracefully — they show up as
``(SYMBOL NOT FOUND IN LIBRARY)`` so the LLM knows to pick a different
part instead of hallucinating pins on a missing symbol.

Per published research (PCBSchemaGen, CircuitLM) this is the single
biggest fix for pin-number hallucinations in LLM-driven schematic
generation. Every config value lives in
``config/pin_catalog_seeds.json`` — no hardcoded part data in Python.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pin_catalog_seeds.json"
_PART_KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "config" / "part_keywords.json"


@lru_cache(maxsize=1)
def _load_config() -> Dict:
    """Read pin_catalog_seeds.json once per process. Restart the server
    to pick up edits — same convention as every other config file in
    this project."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def _load_part_keywords() -> Dict:
    """Read part_keywords.json once per process (same restart convention
    as _load_config). Returns {} on any read/parse error so the resolver
    degrades to no-op."""
    try:
        return json.loads(_PART_KEYWORDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_lib_ids_from_prompt(prompt: str) -> List[str]:
    """Front-load helper: scan the user prompt for known part keywords and
    return the lib_ids whose REAL pin maps should be injected on attempt 1
    — so the architect sees exact pin names BEFORE its first guess, not
    only on the retry path (which mines the failed attempt's lib_ids).

    Rules live in ``config/part_keywords.json`` (keyword substring -> lib_ids).
    Returns [] when disabled, the prompt is empty, the config is missing,
    or no keyword matches — in which case the architect prompt stays
    byte-identical to the pre-front-load behaviour (cache prefix intact).
    """
    if not prompt:
        return []
    cfg = _load_part_keywords()
    if not cfg.get("enabled", True):
        return []
    lower = prompt.lower()
    out: List[str] = []
    seen: set = set()
    for rule in cfg.get("rules", []) or []:
        keywords = rule.get("keywords") or []
        if not any(kw in lower for kw in keywords):
            continue
        for lid in rule.get("lib_ids") or []:
            if lid and lid not in seen:
                seen.add(lid)
                out.append(lid)

    # DYNAMIC MPN resolution — the keyword table above only covers
    # hand-curated parts; this resolves ANY part-number-like token in the
    # prompt to its real lib_id via the cross-library value resolver, so the
    # architect sees real pin names for parts that AREN'T in the table
    # (BQ76952 -> Battery_Management:BQ7695201PFBR's VC0..VC15, LM5164 ->
    # LM5164DDA's VIN/SW/EN-UVLO, INA240 -> INA240A1D's +/-/REF). Without
    # this the architect invents 'Pin_1'/'OUT'/'~{ON}/OFF' on the correct
    # symbol and every one fails PIN_NOT_ON_SYMBOL. Per-circuit-agnostic:
    # purely token + library driven, no part list. Gated by
    # part_keywords.json:dynamic_mpn_resolve (default true).
    if cfg.get("dynamic_mpn_resolve", True):
        try:
            import re as _re
            from ..kicad.symbol_geom import resolve_lib_id_by_value
            min_s = float(cfg.get("dynamic_mpn_min_score", 0.75))
            cap = int(cfg.get("dynamic_mpn_max_parts", 16))
            for tok in _re.findall(r"[A-Za-z][A-Za-z0-9\-./]{3,}", prompt):
                if len(out) >= cap:
                    break
                t = tok.strip("-./")
                # MPN-like only: >= 2 letters AND >= 1 digit AND len >= 5
                # (filters '48V', '13S', '3V3', plain words).
                if (len(t) < 5
                        or sum(c.isalpha() for c in t) < 2
                        or not any(c.isdigit() for c in t)):
                    continue
                best, score, _c = resolve_lib_id_by_value(t)
                if best and score >= min_s and best not in seen:
                    seen.add(best)
                    out.append(best)
        except Exception:
            pass
    return out


def seed_lib_ids() -> List[str]:
    """The author-curated seed list — every lib_id here gets its pin
    map injected into the architect prompt on every call."""
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return []
    return list(cfg.get("lib_ids", []) or [])


def _format_pin(pin, include_etype: bool, skip_aliases: set) -> Optional[str]:
    """Format one pin entry. Returns None when the pin should be
    suppressed (e.g. unnamed ``~`` placeholder)."""
    name = (pin.name or "").strip()
    if not name or name in skip_aliases:
        return None
    if include_etype:
        return f"{name}({pin.number},{pin.etype})"
    return f"{name}({pin.number})"


def _format_entry(lib_id: str,
                   max_pins: int,
                   include_etype: bool,
                   skip_aliases: set) -> str:
    """Build one catalog line for a single lib_id. Never throws — a
    missing symbol returns a sentinel string so the architect retry
    loop sees the right error class."""
    try:
        from ..kicad.symbol_geom import load_symbol
        geom = load_symbol(lib_id)
    except Exception as exc:
        return f"- {lib_id}: (SYMBOL NOT FOUND: {type(exc).__name__})"

    pins = list(geom.pins or [])
    if not pins:
        return f"- {lib_id}: (no pins detected)"
    entries: List[str] = []
    for p in pins[:max_pins]:
        formatted = _format_pin(p, include_etype, skip_aliases)
        if formatted:
            entries.append(formatted)
    overflow = len(pins) - max_pins
    suffix = f", +{overflow} more pins" if overflow > 0 else ""
    return f"- {lib_id}: {', '.join(entries)}{suffix}"


def build_pin_catalog_text(extra_lib_ids: Optional[Iterable[str]] = None) -> str:
    """Build the full catalog text block — seed list plus any extras.

    `extra_lib_ids` is the per-call extension point. On retry, the
    caller passes the lib_ids the architect picked on the failed
    attempt; that way the retry prompt includes pin maps for the parts
    it actually tried (not just the seed list).

    Returns a multi-line string ready to inject into the system prompt
    under an XML-tagged section. Empty string when the catalog is
    disabled in config (lets the prompt stay byte-identical so prompt
    caching keeps working)."""
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return ""

    max_pins = int(cfg.get("max_pins_per_part", 32))
    include_etype = bool(cfg.get("include_etype", True))
    skip_aliases = set(cfg.get("skip_aliases", ["~", "NC"]) or [])

    # Order: seeds in config order first, then extras de-duped
    seen: set = set()
    lib_ids_in_order: List[str] = []
    for lid in seed_lib_ids():
        if lid not in seen:
            seen.add(lid)
            lib_ids_in_order.append(lid)
    for lid in (extra_lib_ids or []):
        if lid and lid not in seen:
            seen.add(lid)
            lib_ids_in_order.append(lid)

    if not lib_ids_in_order:
        return ""

    lines = [_format_entry(lid, max_pins, include_etype, skip_aliases)
             for lid in lib_ids_in_order]
    return "\n".join(lines)


def extract_lib_ids_from_ir_json(raw_json: str) -> List[str]:
    """Quick helper for the retry path: pull every lib_id mentioned in
    a (possibly malformed) IR JSON string. Returns [] on parse failure.

    Why this lives here, not in build_circuit: it's the symmetric
    inverse of build_pin_catalog_text. Co-located so the retry author
    can read the whole pin-catalog story in one file."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    out: List[str] = []
    seen: set = set()
    for c in data.get("components", []) or []:
        if isinstance(c, dict):
            lid = c.get("lib_id")
            if isinstance(lid, str) and lid and lid not in seen:
                seen.add(lid)
                out.append(lid)
    return out
