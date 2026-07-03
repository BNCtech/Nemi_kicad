"""KiCad symbol library lookup + per-pin geometry + bbox computation.

Resolves a `lib_id` like ``Timer:NE555`` to:
  - the raw ``(symbol ...)`` s-expression block to inline into a new
    schematic's ``(lib_symbols ...)`` (KiCad 9 requires this — eeschema
    does NOT look up sym-lib-table when loading);
  - a list of `PinGeom` (one per pin) with absolute position, rotation,
    side-of-body, electrical type, name and number;
  - the body bbox and the outer bbox (body + pin extents).

Library layout assumed: ``<root>/<libnick>.kicad_symdir/<part>.kicad_sym``
(the user's KiCad fork convention). Standard ``<libnick>.kicad_sym`` flat
files also work — if the symdir variant isn't found, falls back to
parsing a flat library file with the matching ``(symbol "<part>" ...)``.

No fuzzy matching — if a lib_id can't be resolved exactly, raises. This
is the defence against the NE555-fixture failure mode where a missing
symbol got auto-substituted with an unrelated regulator.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

import sexpdata


# Default symbol roots — user's kicad-sym-lib + the KiCad install (if present).
# Override with $KICAD_SYMBOL_DIR (colon-separated paths).
from ..settings import sym_lib_dir as _sym_lib_dir, envil_home as _envil_home

def _kicad_user_sym_dirs() -> list:
    """KiCad per-user symbol dirs (Documents/KiCad/<ver>/symbols) for versions 7-10."""
    from pathlib import Path as _P
    base = _P.home() / "Documents" / "KiCad"
    return [str(base / v / "symbols") for v in ("10.0", "9.0", "8.0", "7.0")]

DEFAULT_SYM_ROOTS = (
    [str(_sym_lib_dir())]
    + _kicad_user_sym_dirs()
    + [
        "C:/Program Files/KiCad/9.0/share/kicad/symbols",
        "C:/Program Files/KiCad/8.0/share/kicad/symbols",
    ]
)

# KiCad ships with version-specific path tokens in sym-lib-table URIs.
# These are not in os.environ or kicad_common.json, so we carry the defaults
# here. On non-standard installs the token expands to a non-existent path
# which _has_symbols() rejects cleanly — no harm done.
_KICAD_BUILTIN_TOKENS: dict = {
    "KICAD10_SYMBOL_DIR": "C:/Program Files/KiCad/10.0/share/kicad/symbols",
    "KICAD9_SYMBOL_DIR":  "C:/Program Files/KiCad/9.0/share/kicad/symbols",
    "KICAD8_SYMBOL_DIR":  "C:/Program Files/KiCad/8.0/share/kicad/symbols",
    "KICAD7_SYMBOL_DIR":  "C:/Program Files/KiCad/7.0/share/kicad/symbols",
    "KICAD_USER_DIR":     str(Path.home() / "Documents" / "KiCad"),
}


def _resolve_sym_lib_table(table: Path,
                            extra_env: Optional[dict] = None) -> List[str]:
    """Parse a KiCad sym-lib-table file and return the unique parent-directory
    root for every ``(lib ...)`` entry whose URI can be fully expanded.

    Token resolution order:
      1. ``extra_env``  — caller-supplied (from kicad_common.json vars)
      2. ``os.environ`` — process environment
      3. ``_KICAD_BUILTIN_TOKENS`` — known KiCad version-path defaults

    Entries whose URI still contains ``${…}`` after expansion are skipped
    (unresolvable token — not an error)."""
    import re
    if not table.is_file():
        return []
    try:
        txt = table.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    env: dict = dict(extra_env or {})

    def _sub(m: "re.Match") -> str:
        k = m.group(1)
        return (env.get(k) or os.environ.get(k)
                or _KICAD_BUILTIN_TOKENS.get(k) or m.group(0))

    roots: List[str] = []
    seen: set = set()
    for uri in re.findall(r'\(uri\s+"([^"]+)"', txt):
        expanded = re.sub(r"\$\{([^}]+)\}", _sub, uri).replace("\\", "/").strip()
        if "${" in expanded or not expanded:
            continue
        # The URI points to a .kicad_symdir or a flat .kicad_sym file;
        # the root is its parent directory.
        parent = expanded.rsplit("/", 1)[0] if "/" in expanded else expanded
        key = os.path.normcase(os.path.normpath(parent))
        if key not in seen:
            seen.add(key)
            roots.append(parent)
    return roots


# Project-level sym-lib-table roots — populated by inject_project_sym_roots()
# when a tool knows the active KiCad project path. Included in _candidate_roots()
# so _sym_roots() / load_symbol() / _all_symbols() all see project-local libs.
_project_sym_roots: List[str] = []


def inject_project_sym_roots(project_path: str) -> List[str]:
    """Read the sym-lib-table in `project_path`'s directory and register its
    library roots so the agent can find and edit project-local symbols.

    Call this from any tool that receives a ``project_path`` argument before
    the first ``load_symbol`` / ``_sym_roots`` call in that request.

    Returns the list of newly discovered roots (empty if already known or
    none found). Clears the symbol-resolution caches so the change takes
    effect immediately."""
    global _project_sym_roots
    p = Path(project_path).expanduser()
    if p.is_file():
        p = p.parent
    table = p / "sym-lib-table"
    new_roots = _resolve_sym_lib_table(table)
    changed = False
    for r in new_roots:
        key = os.path.normcase(os.path.normpath(r))
        already = any(os.path.normcase(os.path.normpath(e)) == key
                      for e in _project_sym_roots)
        if not already:
            _project_sym_roots.append(r)
            changed = True
    if changed:
        # Clear caches so next call to _sym_roots / load_symbol picks up
        # the new roots.
        _discover_config_roots.cache_clear()
        for fn_name in ("load_symbol", "_all_symbols", "resolve_lib_id_by_value"):
            fn = globals().get(fn_name)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
    return new_roots


def _has_symbols(root: Path) -> bool:
    """A root only counts if it actually holds symbols — either the fork's
    ``*.kicad_symdir`` folders or flat ``*.kicad_sym`` files. This is what
    stops a path that merely *exists* (e.g. an empty KiCad install dir, or a
    stale mount point) from being treated as a usable library."""
    try:
        if not root.is_dir():
            return False
        for _ in root.glob("*.kicad_symdir"):
            return True
        for _ in root.glob("*.kicad_sym"):
            return True
    except OSError:
        return False
    return False


def _kicad_config_dirs() -> List[Path]:
    """Per-version KiCad config dirs on this machine, newest first. Covers
    Windows (%APPDATA%/kicad/<ver>), Linux (~/.config/kicad/<ver>) and macOS
    (~/Library/Preferences/kicad/<ver>)."""
    bases: List[Path] = []
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        bases.append(Path(appdata) / "kicad")
    home = Path.home()
    bases.append(home / ".config" / "kicad")
    bases.append(home / "Library" / "Preferences" / "kicad")
    vers: List[Path] = []
    for base in bases:
        try:
            if base.is_dir():
                vers.extend(p for p in base.iterdir() if p.is_dir())
        except OSError:
            continue

    def _key(p: Path):
        try:
            return tuple(int(x) for x in p.name.split("."))
        except ValueError:
            return (-1,)

    return sorted(vers, key=_key, reverse=True)


@lru_cache(maxsize=1)
def _discover_config_roots() -> Tuple[str, ...]:
    """Auto-detect the symbol library the *running KiCad app* uses — no
    hardcoded drive. Reads each ``kicad_common.json`` for its declared
    environment vars and expands every ``sym-lib-table`` URI (using
    ``_resolve_sym_lib_table`` which falls back to ``_KICAD_BUILTIN_TOKENS``
    so version tokens like ``${KICAD10_SYMBOL_DIR}`` always resolve).
    Cached for the process; results are filtered for real symbol content by
    the caller."""
    import json

    found: List[str] = []
    seen: set = set()

    def _add(val: str) -> None:
        if not val:
            return
        key = os.path.normcase(os.path.normpath(val))
        if key not in seen:
            seen.add(key)
            found.append(val.replace("\\", "/").strip())

    for cfg_dir in _kicad_config_dirs():
        # Extract env vars from kicad_common.json (used for token expansion
        # in the sibling sym-lib-table).
        extra_env: dict = {}
        common = cfg_dir / "kicad_common.json"
        try:
            if common.is_file():
                data = json.loads(common.read_text(encoding="utf-8"))
                raw_env = data.get("environment") or {}
                env_vars = (raw_env.get("vars", raw_env)
                            if isinstance(raw_env, dict) else {})
                if isinstance(env_vars, dict):
                    extra_env = env_vars
                    for k, v in env_vars.items():
                        if isinstance(v, str) and (
                                "SYMBOL" in k.upper()
                                or "LIB_ROOT" in k.upper()
                                or "SYM" in k.upper()):
                            _add(v)
        except (OSError, ValueError):
            pass
        # Parse the global sym-lib-table for this KiCad version.  Every URI
        # whose parent directory is added here becomes a root that _sym_roots()
        # can discover — so user-added libs registered via KiCad's
        # "Preferences → Manage Symbol Libraries" are found automatically.
        for root in _resolve_sym_lib_table(cfg_dir / "sym-lib-table", extra_env):
            _add(root)

    return tuple(found)


def _candidate_roots() -> List[str]:
    """Ordered candidate roots (may include non-existent ones — useful for
    diagnostics).

    Priority:
      1. Explicit $KICAD_SYMBOL_DIR
      2. Project-local roots (inject_project_sym_roots)
      3. Auto-detected config roots (global sym-lib-table + kicad_common.json)
      4. Bundled Envil lib
      5. DEFAULT_SYM_ROOTS (hardcoded KiCad install + user dirs)
    """
    raw = os.environ.get("KICAD_SYMBOL_DIR", "")
    extras = [p for p in raw.split(os.pathsep) if p.strip()] if raw else []
    bundled = str(_envil_home() / "kicad-sym-lib")
    out: List[str] = []
    seen: set = set()
    for p in (extras + _project_sym_roots
              + list(_discover_config_roots())
              + [bundled] + DEFAULT_SYM_ROOTS):
        if not p:
            continue
        key = os.path.normcase(os.path.normpath(p))
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _sym_roots() -> List[Path]:
    """Symbol roots that actually contain symbols, in priority order. Dead
    paths (an unmounted drive, an empty install) are dropped, and the app's
    real library is auto-discovered — so a stale $KICAD_SYMBOL_DIR can no
    longer silently zero out the library and make every part look 'missing'."""
    roots = [Path(p) for p in _candidate_roots()]
    usable = [p for p in roots if _has_symbols(p)]
    if usable:
        return usable
    # Last resort: any path that at least exists, so the error message can
    # show what was inspected rather than nothing.
    return [p for p in roots if p.exists()]


def library_status() -> dict:
    """Pre-flight check: is a usable symbol library reachable on this system?
    Returns a structured verdict the build pipeline can act on instead of
    failing deep inside the architect loop and mis-reporting the cause."""
    roots = _sym_roots()
    probe: dict = {}
    for lid in ("Device:R", "Device:C", "Device:LED"):
        try:
            load_symbol(lid)
            probe[lid] = True
        except Exception:
            probe[lid] = False
    ok = bool(roots) and all(probe.values())
    return {
        "ok": ok,
        "roots": [str(r) for r in roots],
        "searched": _candidate_roots(),
        "probe": probe,
    }


def _head(node) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
    return None


def _find_children(node, name: str) -> List[list]:
    if not isinstance(node, list):
        return []
    return [c for c in node[1:] if isinstance(c, list) and _head(c) == name]


def _first_child(node, name: str) -> Optional[list]:
    cs = _find_children(node, name)
    return cs[0] if cs else None


def _atom(node) -> Optional[str]:
    if isinstance(node, sexpdata.Symbol):
        return node.value()
    if isinstance(node, str):
        return node
    return None


@dataclass
class PinGeom:
    number: str          # "1", "2", ...
    name: str            # "VCC", "TRG", or "~" if unnamed
    etype: str           # power_in / input / output / passive / ...
    shape: str           # line / inverted / clock / ...
    x_local: float       # local symbol-space (Y-up)
    y_local: float
    rot: float           # 0/90/180/270 (degrees, pin direction)
    length: float        # pin extent from anchor → tip
    unit: int = 0        # 0 = shared across all units, 1..N = unit-specific
    aliases: List[str] = field(default_factory=list)
    # KiCad `(alternate "NAME" ...)` sub-nodes — a pin's alternate-function
    # names (e.g. PD0 carries RCC_OSC_IN; PA9 carries USART1_TX). The library
    # is the source of truth for these; without parsing them, pin_by_name
    # could only match the primary GPIO name and the architect's functional
    # pin refs (OSC_IN, USART1_TX, I2C1_SCL) failed PIN_NOT_ON_SYMBOL.
    # Why: multi-unit symbols (LM358 = 2 op-amps + power) declare pins
    # under sub-blocks `<name>_<unit>_<style>`. When the engine emits an
    # instance with `(unit 1)`, only unit-1 pins render. NC markers must
    # filter by unit to avoid emitting markers for un-rendered unit-2/3
    # pins (which visually land overlapping unit-1's body).

    @property
    def tip_local(self) -> Tuple[float, float]:
        """Pin's connection point (the wire-attach end) in local coords.
        Per KiCad convention, the pin's ``(at x y)`` IS the wire-attach
        end — outside the body. The pin LINE extends from ``at`` in
        direction ``rot`` by ``length`` units, ending at the body edge.
        We want the wire-attach end, so just return (x, y)."""
        return (self.x_local, self.y_local)

    @property
    def body_end_local(self) -> Tuple[float, float]:
        """The OTHER end of the pin line — where the pin meets the body.
        Used only for body-extent bbox computation."""
        rad = math.radians(self.rot)
        return (self.x_local + self.length * math.cos(rad),
                self.y_local + self.length * math.sin(rad))


@dataclass
class PropertyOffset:
    """Where the symbol's designer placed Reference/Value text relative
    to body origin, in local Y-up coords. Used so placed instances put
    the text where the symbol library intended, not at a fixed offset."""
    x: float
    y: float
    rotation: float


@dataclass
class SymbolGeom:
    lib_id: str                  # "Timer:NE555"
    raw_symbol_sexpr: list       # the (symbol ...) block, for inlining
    pins: List[PinGeom] = field(default_factory=list)
    body_bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)  # local Y-up
    outer_bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    ref_offset: PropertyOffset = field(default_factory=lambda: PropertyOffset(0, 5.08, 0))
    val_offset: PropertyOffset = field(default_factory=lambda: PropertyOffset(0, -5.08, 0))
    # BOM metadata harvested from the library symbol's (property ...) fields.
    # Empty string when the symbol doesn't declare the field; parts_database.json
    # can fill the gap downstream without touching the library file.
    description: str = ""        # "Precision Timers, 555 compatible, PDIP-8"
    datasheet: str = ""          # URL or "~" — preserved verbatim from library
    keywords: str = ""           # "single timer 555" — used for search/UX
    mpn: str = ""                # manufacturer part number (rare in library)
    manufacturer: str = ""       # manufacturer name (rare in library)

    def pin_by_number(self, number: str) -> Optional[PinGeom]:
        for p in self.pins:
            if p.number == number:
                return p
        return None

    def pin_by_name(self, name: str) -> Optional[PinGeom]:
        """Match by full pin name or any of its alternate-function aliases.
        KiCad encodes MCU pins as e.g. ``~{RESET}/PB5`` — we want
        ``RESET``, ``~{RESET}``, ``PB5``, and the full string to all
        resolve. Both query and pin name are normalised: ``~{...}``
        markers are stripped, then we split on ``/`` and ``,``."""
        def _aliases(s: str) -> set:
            import re as _re
            cleaned = s.upper().replace("~{", "").replace("}", "")
            out = {cleaned}
            # Split on every separator KiCad / datasheets / CubeMX put
            # between a GPIO name and its alternate function: '/', ',',
            # '-' and whitespace. e.g. CubeMX "PC14-OSC32_IN" -> {PC14,
            # OSC32_IN}; "~{RESET}/PB5" -> {RESET, PB5}. The bare GPIO
            # token then intersects the symbol's short primary name.
            for chunk in _re.split(r"[/,\-\s]+", cleaned):
                chunk = chunk.strip()
                if chunk:
                    out.add(chunk)
            # Separator-insensitive variants so "VCAP1" == "VCAP_1" and
            # "OSC_IN" == "OSCIN": collapse all non-alphanumerics. Added
            # alongside (never replacing) the literal tokens, so matching
            # stays a superset of the previous behaviour.
            for tok in list(out):
                squashed = _re.sub(r"[^A-Z0-9]", "", tok)
                if squashed and squashed != tok:
                    out.add(squashed)
            return out

        # Peripheral-prefix tolerance: KiCad alternate names carry the
        # peripheral block prefix (RCC_OSC_IN, USART1_TX, I2C1_SCL). The
        # architect usually writes the bare function (OSC_IN, TX, SCL).
        # Strip ONE leading peripheral-looking prefix from ALTERNATE names
        # only (never the query, never recursively) so "OSC_IN" matches
        # "RCC_OSC_IN" without "IN" leaking in as a generic token.
        import re as _re
        def _strip_prefix(tok: str) -> Optional[str]:
            m = _re.match(r"^[A-Z]{2,6}\d*_(.+)$", tok)
            return m.group(1) if m else None

        wanted = _aliases(name)
        for p in self.pins:
            # Match against the primary name AND every alternate-function
            # name parsed from the library symbol's (alternate ...) nodes.
            pin_alias_set = _aliases(p.name)
            for a in p.aliases:
                a_aliases = _aliases(a)
                pin_alias_set |= a_aliases
                for tok in a_aliases:
                    stripped = _strip_prefix(tok)
                    if stripped:
                        pin_alias_set.add(stripped)
            if wanted & pin_alias_set:
                return p
        return None

    def resolve_pin(self, key: str) -> Optional[PinGeom]:
        """Accepts either a pin number ('8') or a pin name ('VCC', 'RESET',
        'PB5')."""
        if key.isdigit():
            return self.pin_by_number(key)
        return self.pin_by_name(key) or self.pin_by_number(key)

    def resolve_pin_with_score(self, key: str, min_score: float = 0.5
                               ) -> Tuple[Optional["PinGeom"], float, list]:
        """Dynamic 'did you mean?' ranker — used ONLY when resolve_pin()
        returns None, to suggest the closest real pins instead of dumping a
        truncated list. Ranks the requested name against each pin's OWN
        primary name + alternate-function aliases (parsed live from the
        library symbol's ``(alternate ...)`` nodes), reusing the exact
        token-overlap scorer that already powers resolve_lib_id_by_value.

        No synonym table: the symbol's own pin/alternate names ARE the
        vocabulary, derived at runtime. Works for any part. This is
        suggest-only — callers must NOT auto-rewrite the pin from it (a
        silent fuzzy rewrite would produce a wrong-but-passing circuit);
        it only enriches the error so the architect picks the real name.

        Returns ``(best_pin, best_score, candidates)`` where candidates is
        the top few ``(score, PinGeom)`` sorted best-first, or
        ``(None, 0.0, [])`` when nothing scores >= min_score. Note: purely
        semantic renames (e.g. datasheet 'Vref' -> symbol 'REGIN', unrelated
        strings) correctly score 0 here — those are covered by showing the
        FULL available-pin list, not by this string ranker."""
        if key.isdigit():
            p = self.pin_by_number(key)
            return (p, 1.0, [(1.0, p)]) if p else (None, 0.0, [])
        req = _tokenize_part(key)
        if not req:
            return (None, 0.0, [])
        scored: list = []
        for p in self.pins:
            cands = [p.name] + list(p.aliases)
            s = max((_score_candidate(req, c) for c in cands if c), default=0.0)
            if s >= min_score:
                scored.append((s, p))
        if not scored:
            return (None, 0.0, [])
        scored.sort(key=lambda t: (-t[0], t[1].number))
        return (scored[0][1], scored[0][0], scored[:3])


def _parse_pin(pin_node: list) -> PinGeom:
    # (pin <etype> <shape> (at x y rot) (length L) (name "..." ...) (number "..." ...))
    etype = _atom(pin_node[1]) or "passive"
    shape = _atom(pin_node[2]) or "line"
    at = _first_child(pin_node, "at")
    length_n = _first_child(pin_node, "length")
    name_n = _first_child(pin_node, "name")
    number_n = _first_child(pin_node, "number")
    x = float(at[1]) if at else 0.0
    y = float(at[2]) if at else 0.0
    rot = float(at[3]) if at and len(at) > 3 else 0.0
    length = float(length_n[1]) if length_n else 2.54
    name = str(name_n[1]) if name_n else "~"
    number = str(number_n[1]) if number_n else "?"
    # Alternate-function names: (alternate "RCC_OSC_IN" bidirectional line).
    # The alternate NAME is the first atom after the `alternate` head.
    aliases: List[str] = []
    for alt in _find_children(pin_node, "alternate"):
        if len(alt) > 1:
            alt_name = _atom(alt[1]) or (str(alt[1]) if alt[1] is not None else "")
            alt_name = str(alt_name).strip()
            if alt_name and alt_name not in ("~", "NC"):
                aliases.append(alt_name)
    return PinGeom(number=number, name=name, etype=etype, shape=shape,
                   x_local=x, y_local=y, rot=rot, length=length,
                   aliases=aliases)


def _unit_from_subblock_name(name: str) -> int:
    """Parse the unit number from a `<symname>_<unit>_<style>` subblock
    name (KiCad convention). `_0_<style>` = shared body / pins common
    to every unit; `_<N>_<style>` = unit-N specific. Returns 0 if the
    name doesn't match the convention (caller treats as shared)."""
    import re as _re
    m = _re.match(r".+_(\d+)_(\d+)$", name)
    if m:
        return int(m.group(1))
    return 0


def _walk_pins(symbol_node: list, current_unit: int = 0) -> List[PinGeom]:
    """Pins live either directly under (symbol ...) or under unit-subblocks
    (symbol "<name>_1_1" ...). Collect from both, tagging each pin with the
    unit it belongs to so NC emission can filter by the rendered unit."""
    pins = []
    for child in symbol_node[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)
        if h == "pin":
            pin = _parse_pin(child)
            pin.unit = current_unit
            pins.append(pin)
        elif h == "symbol":
            # Nested unit / body-style subblock. Parse the unit from
            # the subblock's name (e.g. `LM2904_2_1` -> unit 2). Pins
            # found inside inherit that unit number.
            child_name = ""
            if len(child) >= 2:
                first = child[1]
                child_name = first.value() if isinstance(first, sexpdata.Symbol) else str(first)
            child_unit = _unit_from_subblock_name(child_name)
            pins.extend(_walk_pins(child, current_unit=child_unit))
    return pins


def _expand_bbox(bbox, x, y):
    x1, y1, x2, y2 = bbox
    return (min(x1, x), min(y1, y), max(x2, x), max(y2, y))


def _walk_graphics(symbol_node: list, bbox):
    """Expand bbox to include rectangle/polyline/arc/circle in this node
    and any nested unit subblocks. Properties are NOT included (KLC S3.5)."""
    for child in symbol_node[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)
        if h == "rectangle":
            s = _first_child(child, "start")
            e = _first_child(child, "end")
            if s and e:
                bbox = _expand_bbox(bbox, float(s[1]), float(s[2]))
                bbox = _expand_bbox(bbox, float(e[1]), float(e[2]))
        elif h == "polyline":
            pts = _first_child(child, "pts")
            if pts:
                for pt in pts[1:]:
                    if _head(pt) == "xy":
                        bbox = _expand_bbox(bbox, float(pt[1]), float(pt[2]))
        elif h == "circle":
            c = _first_child(child, "center")
            r = _first_child(child, "radius")
            if c and r:
                cx, cy, rv = float(c[1]), float(c[2]), float(r[1])
                bbox = _expand_bbox(bbox, cx - rv, cy - rv)
                bbox = _expand_bbox(bbox, cx + rv, cy + rv)
        elif h == "arc":
            for k in ("start", "mid", "end"):
                p = _first_child(child, k)
                if p:
                    bbox = _expand_bbox(bbox, float(p[1]), float(p[2]))
        elif h == "symbol":  # nested unit
            bbox = _walk_graphics(child, bbox)
    return bbox


def _bbox_with_pins(symbol_node: list) -> Tuple[Tuple, Tuple]:
    body = _walk_graphics(symbol_node, (1e9, 1e9, -1e9, -1e9))
    if body[0] > body[2]:
        body = (0, 0, 0, 0)
    outer = body
    # Outer bbox extends to include the wire-attach pin tips (which are
    # OUTSIDE the body per KLC S3.5). We also include the body-end of
    # each pin line — for symbols with no graphic body (some
    # connectors), this ensures we still get a meaningful bbox.
    for p in _walk_pins(symbol_node):
        tx, ty = p.tip_local
        outer = _expand_bbox(outer, tx, ty)
        bx, by = p.body_end_local
        outer = _expand_bbox(outer, bx, by)
    return body, outer


def _find_symbol_in_lib(libnick: str, part: str) -> Optional[list]:
    """Locate a (symbol "<part>" ...) block by walking the candidate
    library files. Returns the raw s-expr list or None."""
    for root in _sym_roots():
        candidates = [
            root / f"{libnick}.kicad_symdir" / f"{part}.kicad_sym",
            root / f"{libnick}.kicad_sym",
        ]
        for path in candidates:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            tree = sexpdata.loads(text)
            if not isinstance(tree, list):
                continue
            if _head(tree) == "kicad_symbol_lib":
                for sym in _find_children(tree, "symbol"):
                    if str(sym[1]) == part:
                        return sym
            elif _head(tree) == "symbol":
                if str(tree[1]) == part:
                    return tree
    return None


def _rename_subunits(node: list, old_prefix: str, new_prefix: str) -> list:
    """Walk a (symbol ...) tree and rename every nested (symbol "<old>_X_Y" ...)
    block so its prefix matches `new_prefix`. eeschema rejects subunit names
    whose prefix doesn't equal the outer symbol name (KiCad 9 strict parser:
    'Invalid symbol unit name prefix' error)."""
    if not isinstance(node, list):
        return node
    out = list(node)
    if _head(out) == "symbol" and len(out) > 1 and isinstance(out[1], str):
        name = out[1]
        if name.startswith(old_prefix + "_"):
            out[1] = new_prefix + name[len(old_prefix):]
    # Recurse into children
    for i, child in enumerate(out):
        if isinstance(child, list):
            out[i] = _rename_subunits(child, old_prefix, new_prefix)
    return out


def _resolve_extends(sym: list, libnick: str, _seen=None) -> list:
    """If `sym` is `(symbol "X" (extends "Y") ...)`, replace it with a
    full clone of Y's body renamed to X. Recursively resolves multi-level
    inheritance. The returned node has no `extends` directive and carries
    the parent's pins/graphics/subunits inlined. Subunit blocks are
    re-prefixed so their names match the new outer name (eeschema's
    'Invalid symbol unit name prefix' check)."""
    if _seen is None:
        _seen = set()
    extends_n = _first_child(sym, "extends")
    if extends_n is None:
        return sym
    parent_name = str(extends_n[1])
    if parent_name in _seen:
        return sym  # cycle guard
    _seen.add(parent_name)
    parent = _find_symbol_in_lib(libnick, parent_name)
    if parent is None:
        return sym  # parent not found — caller will see 0 pins
    parent = _resolve_extends(parent, libnick, _seen)
    child_name = sym[1]
    # Clone parent body, rename outer + every subunit "<parent_name>_X_Y"
    merged = [parent[0], child_name]
    for child in parent[2:]:
        if isinstance(child, list) and _head(child) == "extends":
            continue
        merged.append(_rename_subunits(child, parent_name, child_name))
    return merged


@lru_cache(maxsize=1)
def _load_aliases() -> dict:
    """Read envil_agent/config/lib_id_aliases.json — maps deprecated/short
    lib_ids (e.g. 'Timer:NE555') to canonical ones present in the user's
    library ('Timer:NE555P'). Returns {} on any error."""
    import json
    here = Path(__file__).resolve().parent.parent
    candidates = [here / "config" / "lib_id_aliases.json"]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return dict(data.get("aliases", {}))
            except Exception:
                return {}
    return {}


def _norm_token(tok: str) -> str:
    """Normalise a single token for fuzzy comparison: lowercase + strip
    non-alphanumeric. Voltage forms like `3.3` -> `33`, `1.8` -> `18`
    so they line up with KiCad's `MCP1700x-330xxTO` / `MCP1700x-180xxTO`
    naming. KiCad uses 100x-mV in many regulator part names; we
    normalise to that form by stripping the decimal point."""
    import re as _re
    t = "".join(ch for ch in tok if ch.isalnum() or ch == ".").lower()
    # Voltage normalisation: 3.3 -> 33, 1.8 -> 18, 12.0 -> 120
    m = _re.fullmatch(r"(\d+)\.(\d+)", t)
    if m:
        return m.group(1) + m.group(2)
    return t.replace(".", "")


def _tokenize_part(part: str) -> list:
    import re as _re
    raw = _re.split(r"[-_./\s]+", part)
    return [_norm_token(t) for t in raw if t]


def _score_candidate(req_tokens: list, cand_part: str) -> float:
    """Fuzzy score in [0, 1]. For each requested token, find the
    longest PREFIX of it that occurs in the candidate name (case-
    insensitive). Weights by character length so `MCP1700` (7) >>
    `TO` (2), and partial matches like `3302e` -> `330` still count
    (3 chars matched out of 5). Universal — covers KiCad's truncated
    naming convention (MCP1700x-330xxTO vs full MCP1700-3302E_TO92)
    without per-part hardcoding."""
    cand_norm = _norm_token(cand_part)
    if not req_tokens:
        return 0.0
    total = sum(len(t) for t in req_tokens) or 1
    matched = 0
    for t in req_tokens:
        if not t:
            continue
        best = 0
        # Walk shortening prefixes until one is a substring of cand
        for k in range(len(t), 1, -1):
            if t[:k] in cand_norm:
                best = k
                break
        matched += best
    return matched / total


def _fuzzy_resolve(libnick: str, part: str) -> Optional[str]:
    """Best-effort dynamic lookup when the exact part name doesn't exist
    in `libnick`. Walks the lib's symdir, tokenises each filename,
    scores by token-overlap with the requested name, returns the best
    match if its score is above 0.5. Universal — works for any KiCad
    part family without hardcoded aliases."""
    req_tokens = _tokenize_part(part)
    if not req_tokens:
        return None
    candidates: list = []
    for root in _sym_roots():
        symdir = root / f"{libnick}.kicad_symdir"
        if symdir.exists():
            for p in symdir.glob("*.kicad_sym"):
                cand_part = p.stem
                score = _score_candidate(req_tokens, cand_part)
                if score >= 0.5:
                    candidates.append((score, cand_part))
        # Also a flat libname.kicad_sym would be searched but skipping
        # — for v1 the symdir layout covers our case.
    if not candidates:
        return None
    # Sort by score DESCENDING, then by name ASCENDING. The ascending
    # tiebreaker keeps the result predictable and prefers shorter /
    # alphabetically-earlier variants (e.g. Conn_01x02_Pin beats
    # Conn_01x02_Socket — a pin header is the typical default for a
    # generic Conn_01x02 request).
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1]


@lru_cache(maxsize=1)
def _all_symbols() -> tuple:
    """Flat index of every symbol across all roots as (libnick, part).
    Cached for the process — symbol libraries don't change at runtime.
    Used by resolve_lib_id_by_value for CROSS-library resolution (unlike
    _fuzzy_resolve, which only searches inside one libnick)."""
    out: list = []
    for root in _sym_roots():
        for symdir in root.glob("*.kicad_symdir"):
            name = symdir.name
            if not name.endswith(".kicad_symdir"):
                continue
            libnick = name[: -len(".kicad_symdir")]
            for p in symdir.glob("*.kicad_sym"):
                out.append((libnick, p.stem))
    return tuple(out)


@lru_cache(maxsize=2048)
def resolve_lib_id_by_value(value: str, current_lib_id: str = "",
                            min_score: float = 0.62) -> tuple:
    """CROSS-library symbol resolution keyed on the part VALUE (the MPN).

    The architect reliably puts the intended part number in ``value``
    ('BQ76952', 'INA240', 'STM32G474VET6') but often emits a wrong/fake
    ``lib_id``: a wrong libnick ('Sensor_Battery:BQ76952' when the real
    symbol lives in 'Battery_Management'), or a different REAL part
    entirely ('Amplifier_Operational:TL072' standing in for an INA240).
    Neither is recoverable downstream — _fuzzy_resolve only searches the
    guessed libnick, and a wrong-but-valid symbol loads with no error and
    only fails later as PIN_NOT_ON_SYMBOL once real pins are wired onto it.
    This scans EVERY library for the symbol whose name best matches
    ``value`` (same token-overlap scorer as _fuzzy_resolve) and returns
    ``(best_lib_id, best_score, candidates)`` — candidates being the sorted
    near-ties, so a caller can auto-fix on a confident win or ASK THE USER
    when several variants tie. Returns ``(None, 0.0, [])`` below min_score.
    Universal — no per-part alias table."""
    req = _tokenize_part(value or "")
    if not req:
        return None, 0.0, []
    scored: list = []
    for libnick, part in _all_symbols():
        s = _score_candidate(req, part)
        if s >= min_score:
            scored.append((s, f"{libnick}:{part}"))
    if not scored:
        return None, 0.0, []
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, best = scored[0]
    candidates = [lid for _, lid in scored[:6]]
    return best, best_score, candidates


@lru_cache(maxsize=None)
def load_symbol(lib_id: str) -> SymbolGeom:
    """Resolve and parse a lib_id into a SymbolGeom. Raises ValueError if
    not found AND fuzzy fallback finds nothing.

    Resolution order:
      1. Exact lib_id
      2. Alias map (lib_id_aliases.json) — for genuine renames
      3. Fuzzy match within the same library — token-overlap scoring,
         no per-part hardcoding. Picks the closest existing symbol.
    Handles (extends ...) inheritance by inlining the parent's body."""
    if ":" not in lib_id:
        raise ValueError(f"lib_id must be 'libnick:part': {lib_id!r}")
    aliases = _load_aliases()
    original_lib_id = lib_id
    if lib_id in aliases:
        lib_id = aliases[lib_id]
    libnick, part = lib_id.split(":", 1)
    sym = _find_symbol_in_lib(libnick, part)
    # FUZZY FALLBACK — no per-circuit hardcoding per the user rule.
    # If exact match misses, scan the library for the closest existing
    # symbol by token overlap. The RESOLVED lib_id (with its actual
    # part name) is propagated through the geom — we never rename the
    # symbol's inner subunits, because KiCad enforces outer-symbol-name
    # == subunit-name-prefix and a partial rename leaves the inner
    # subunits dangling, which kicad-cli rejects with 'Failed to load
    # schematic'. Trade-off: the schematic shows the RESOLVED lib_id
    # (Connector:Conn_01x02_Pin) instead of the user-typed alias
    # (Connector:Conn_01x02). That's the correct kicad behavior.
    fuzzy_resolved = False
    if sym is None:
        guess = _fuzzy_resolve(libnick, part)
        if guess is not None:
            sym = _find_symbol_in_lib(libnick, guess)
            if sym is not None:
                lib_id = f"{libnick}:{guess}"
                part = guess
                fuzzy_resolved = True
    if sym is None:
        roots = "\n  ".join(str(r) for r in _sym_roots())
        raise ValueError(f"symbol {original_lib_id!r} not found. Searched roots:\n  {roots}")
    sym = _resolve_extends(sym, libnick)
    # ALIAS rename (NOT fuzzy): when user has an explicit alias entry,
    # they expect the schematic to reference the alias name not the
    # canonical. We rename outer + subunits consistently.
    if not fuzzy_resolved and original_lib_id != lib_id:
        original_part = original_lib_id.split(":", 1)[1]
        sym = list(sym)
        sym[1] = original_part
        sym = _rename_subunits(sym, part, original_part)
        return _build_geom(original_lib_id, sym)
    return _build_geom(lib_id, sym)


def _property_offset(sym: list, name: str, default_y: float) -> PropertyOffset:
    """Read a (property "name" ... (at x y rot) ...) from the symbol's
    direct children. Returns default if not found or hidden."""
    for child in sym[1:]:
        if not isinstance(child, list) or _head(child) != "property":
            continue
        if len(child) < 2 or str(child[1]) != name:
            continue
        at = _first_child(child, "at")
        if at is None:
            continue
        x = float(at[1]) if len(at) > 1 else 0.0
        y = float(at[2]) if len(at) > 2 else default_y
        rot = float(at[3]) if len(at) > 3 else 0.0
        return PropertyOffset(x=x, y=y, rotation=rot)
    return PropertyOffset(x=0.0, y=default_y, rotation=0.0)


def _property_string(sym: list, name: str) -> str:
    """Return the verbatim string value of `(property "<name>" "..." ...)`
    or "" if absent. Used to pull Description/Datasheet/ki_keywords/MPN/
    Manufacturer from the library symbol for BOM emission."""
    for child in sym[1:]:
        if not isinstance(child, list) or _head(child) != "property":
            continue
        if len(child) < 3 or str(child[1]) != name:
            continue
        val = child[2]
        if isinstance(val, str):
            return val
        if isinstance(val, sexpdata.Symbol):
            return val.value()
        return str(val)
    return ""


def _build_geom(lib_id: str, sym: list) -> SymbolGeom:
    pins = _walk_pins(sym)
    body, outer = _bbox_with_pins(sym)
    ref_off = _property_offset(sym, "Reference", default_y=5.08)
    val_off = _property_offset(sym, "Value", default_y=-5.08)
    description = _property_string(sym, "Description")
    datasheet   = _property_string(sym, "Datasheet")
    keywords    = _property_string(sym, "ki_keywords")
    # MPN / Manufacturer are non-standard but some KiCad libraries (e.g.
    # Espressif, ST) ship them; if present we preserve them, otherwise
    # parts_database.json fills the gap at emit time.
    mpn         = _property_string(sym, "MPN") or _property_string(sym, "Mpn")
    manufacturer = (_property_string(sym, "Manufacturer")
                     or _property_string(sym, "MFG")
                     or _property_string(sym, "Mfg"))
    return SymbolGeom(
        lib_id=lib_id,
        raw_symbol_sexpr=sym,
        pins=pins,
        body_bbox=body,
        outer_bbox=outer,
        ref_offset=ref_off,
        val_offset=val_off,
        description=description,
        datasheet="" if datasheet in ("~", "") else datasheet,
        keywords=keywords,
        mpn=mpn,
        manufacturer=manufacturer,
    )


def place_pin(pin: PinGeom, comp_x: float, comp_y: float,
              comp_rot: float = 0.0,
              mirror: Optional[str] = None) -> Tuple[float, float, float]:
    """Convert a pin's local (Y-up) position into absolute schematic (Y-down).

    Returns the pin TIP (wire-attach point) in absolute schematic coordinates
    plus the pin's absolute rotation. Mirror is applied before rotation, per
    eeschema's SCH_SYMBOL::GetPinPhysicalPosition."""
    tx, ty = pin.tip_local         # local Y-up
    x, y = tx, -ty                 # Y-flip to schematic Y-down
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    rad = math.radians(comp_rot)
    xr = x * math.cos(rad) - y * math.sin(rad)
    yr = x * math.sin(rad) + y * math.cos(rad)
    abs_rot = (pin.rot + comp_rot) % 360
    return (comp_x + xr, comp_y + yr, abs_rot)


def pin_side_from_rot(abs_rot: float) -> str:
    """Pin's absolute rotation → which side of the placed body it sits on.
    Drives the label-justify rule AND the satellite-outward direction.

    KiCad convention: the pin LINE points from `at` toward the body in
    direction `rot`. So a pin with rot=0 (line going right INTO body) is
    on the LEFT side of the body — the wire-attach end is to the LEFT.
    After Y-flip into schematic coords (Y-down), local-up ↔ schematic-top:
      rot=0   → line points right toward body → pin on LEFT side
      rot=90  → line points up   toward body → pin on BOTTOM (Y-down)
      rot=180 → line points left toward body → pin on RIGHT side
      rot=270 → line points down toward body → pin on TOP (Y-down)"""
    r = int(round(abs_rot)) % 360
    return {0: "left", 90: "bottom", 180: "right", 270: "top"}.get(r, "left")


def label_justify_for_side(side: str) -> str:
    """KLC convention: label text reads OUTWARD from the body."""
    return {"left": "right", "right": "left",
            "top": "bottom", "bottom": "top"}.get(side, "left")
