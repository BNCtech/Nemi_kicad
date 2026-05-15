"""Inline lib_symbol definitions into a .kicad_sch file — fully dynamic.

Whatever KiCad symbol libraries the running user has configured, this module
reads them by discovering the user's KiCad config at runtime. NOTHING is
hardcoded: no library paths, no symbol-name aliases, no environment-specific
assumptions. The same code works on any machine where KiCad has been
configured at least once.

Discovery chain (first hit wins, in this order):
  1. Per-project sym-lib-table (next to the .kicad_sch / .kicad_pro)
  2. User-level sym-lib-table from highest KiCad config version found at
     %APPDATA%/kicad/<v>/ (Windows) or ~/.config/kicad/<v>/ (Linux/macOS)
  3. KICAD_CONFIG_HOME / KICAD<n>_CONFIG_DIR env-var pointing dirs
  4. The "KiCad" Table entry (default install template) referenced from inside
     a user table — chased through recursively

URIs that use env-var substitution like '${KICAD9_SYMBOL_DIR}/Device.kicad_sym'
are expanded against the live process environment.

When a placed (symbol)'s lib_id can't be found exactly, we fall back to fuzzy
symbol-name search across every registered library (case-insensitive, then
substring). This handles common LLM lib_id mistakes (Timer:NE555 when the
real symbol is Timer:NE555D or Timer:LM555xN) without baking in an alias
table that needs maintaining per shop.

A symbol's drawing primitives can live in a parent it `(extends "Parent")` —
we resolve the chain recursively so the parent's def lands in (lib_symbols)
too, otherwise KiCad has nothing to draw.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sexpdata

Sym = sexpdata.Symbol


def _to_str(t) -> str:
    return t.value() if isinstance(t, Sym) else str(t)


# ---------------------------------------------------------------------------
# KiCad config discovery — no hardcoded paths
# ---------------------------------------------------------------------------

def _candidate_config_roots() -> List[Path]:
    """Every place KiCad might keep its user config on this machine. Order is
    indicative; existence is checked by the caller."""
    out: List[Path] = []
    # Explicit env-var override wins
    for var in ("KICAD_CONFIG_HOME", "KICAD9_CONFIG_DIR", "KICAD10_CONFIG_DIR"):
        v = os.environ.get(var)
        if v:
            out.append(Path(v))
    # OS-standard locations
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "kicad")
    home = Path.home()
    out.extend([
        home / ".config" / "kicad",
        home / "Library" / "Preferences" / "kicad",
    ])
    return out


def _version_sort_key(name: str) -> Tuple[int, int, str]:
    """Sort '10.99' > '10.0' > '9.0' numerically, falling back to string."""
    m = re.match(r"^(\d+)(?:\.(\d+))?$", name)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2) or 0)
        return (major, minor, name)
    return (-1, -1, name)


def _discover_user_sym_lib_table(project_dir: Optional[Path] = None) -> Optional[Path]:
    """Find the live sym-lib-table this KiCad install uses.

    Project-local table wins (KiCad behavior). Otherwise pick the
    highest-version user config that has one.
    """
    if project_dir:
        local = project_dir / "sym-lib-table"
        if local.is_file():
            return local

    for root in _candidate_config_roots():
        if not root.is_dir():
            continue
        version_dirs = [p for p in root.iterdir() if p.is_dir() and re.match(r"^\d", p.name)]
        version_dirs.sort(key=lambda p: _version_sort_key(p.name), reverse=True)
        for vd in version_dirs:
            candidate = vd / "sym-lib-table"
            if candidate.is_file():
                return candidate
    return None


# ---------------------------------------------------------------------------
# sym-lib-table parsing + URI expansion
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_uri(uri: str) -> str:
    """Expand ${KICAD9_SYMBOL_DIR} etc. against os.environ. Unknown vars
    are left in place (KiCad does the same — the file simply won't resolve)."""
    def repl(m):
        return os.environ.get(m.group(1), m.group(0))
    return _ENV_RE.sub(repl, os.path.expanduser(uri))


def _parse_sym_lib_table(path: Path, seen: Optional[set] = None) -> Dict[str, str]:
    """Return {library_name: expanded_uri}. Handles (type "Table") entries
    that reference ANOTHER sym-lib-table file by chasing them recursively."""
    seen = seen or set()
    if path in seen:
        return {}
    seen.add(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        tree = sexpdata.loads(text)
    except Exception:
        return {}
    if not isinstance(tree, list) or _to_str(tree[0]) != "sym_lib_table":
        return {}
    out: Dict[str, str] = {}
    for entry in tree[1:]:
        if not (isinstance(entry, list) and _to_str(entry[0]) == "lib"):
            continue
        name = uri = lib_type = ""
        for sub in entry[1:]:
            if not isinstance(sub, list):
                continue
            tag = _to_str(sub[0])
            if tag == "name" and len(sub) > 1:
                name = _to_str(sub[1])
            elif tag == "uri" and len(sub) > 1:
                uri = _to_str(sub[1])
            elif tag == "type" and len(sub) > 1:
                lib_type = _to_str(sub[1])
        if not name or not uri:
            continue
        expanded = _expand_uri(uri)
        if lib_type.lower() == "table":
            # Nested table — pull its entries in (without overwriting the
            # current table's entries; project / user tables shadow defaults).
            nested = _parse_sym_lib_table(Path(expanded), seen)
            for nk, nv in nested.items():
                out.setdefault(nk, nv)
        else:
            out[name] = expanded
    return out


# ---------------------------------------------------------------------------
# Symbol discovery within a library
# ---------------------------------------------------------------------------

def _library_symbol_files(library_uri: str) -> List[Path]:
    """KiCad libraries come in two shapes: a single .kicad_sym file (one big
    file holding every symbol) OR a .kicad_symdir directory (one file per
    symbol). Return every .kicad_sym we should scan."""
    p = Path(library_uri)
    if p.is_dir():
        return sorted(p.glob("*.kicad_sym"))
    if p.is_file():
        return [p]
    return []


def _read_symbols_in_file(path: Path) -> Iterable[Tuple[str, list]]:
    """Yield (symbol_name, sexp) for every (symbol "name" ...) at the
    kicad_symbol_lib top level. Sub-units like 'name_0_0' are NOT yielded
    here — they live inside their parent symbol."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    try:
        tree = sexpdata.loads(text)
    except Exception:
        return
    if not isinstance(tree, list):
        return
    for child in tree[1:] if _to_str(tree[0]) == "kicad_symbol_lib" else tree:
        if not (isinstance(child, list) and _to_str(child[0]) == "symbol"):
            continue
        if len(child) < 2:
            continue
        yield (_to_str(child[1]), child)


def _find_symbol_in_library(library_uri: str, symbol_name: str) -> Optional[list]:
    """Return the (symbol ...) sexp for symbol_name from the given library.
    Tries exact match first, then case-insensitive."""
    files = _library_symbol_files(library_uri)
    # First pass: exact name (covers both single-file and per-file libs)
    for f in files:
        for name, node in _read_symbols_in_file(f):
            if name == symbol_name:
                return node
    # Second pass: case-insensitive
    target = symbol_name.lower()
    for f in files:
        for name, node in _read_symbols_in_file(f):
            if name.lower() == target:
                return node
    return None


def _fuzzy_find_anywhere(
    table: Dict[str, str], requested_lib_id: str
) -> Optional[Tuple[str, str, list]]:
    """When an exact lib_id ("Timer:NE555") doesn't exist, search EVERY
    registered library for a symbol whose name plausibly matches the
    requested symbol part. Returns (resolved_lib_name, resolved_symbol_name,
    sexp) or None.

    Ranking (best first):
      1. Same library, name equal (case-insensitive)
      2. Same library, requested name is a prefix of an existing symbol
      3. ANY library, exact-name (case-insensitive)
      4. ANY library, requested name is a prefix
      5. ANY library, requested name is a substring
    """
    if ":" not in requested_lib_id:
        return None
    req_lib, req_sym = requested_lib_id.split(":", 1)
    target = req_sym.lower()

    same_lib_uri = table.get(req_lib)
    candidates: List[Tuple[int, str, str, list]] = []

    def consider(rank: int, lib_name: str, sym_name: str, node: list):
        candidates.append((rank, lib_name, sym_name, node))

    # Prefix match runs ONE direction only: existing name starts with the
    # requested name, meaning the existing is a MORE SPECIFIC variant
    # (R_POT → R_Potentiometer, NE555 → NE555D, LM358 → LM358N).
    # The reverse direction (existing name is a prefix of requested) is wrong:
    # it would pick Device:R for Device:R_POT because "R" prefixes "R_POT",
    # but they're different parts.
    def is_specific_prefix(existing_lower: str, target_lower: str) -> bool:
        if not existing_lower.startswith(target_lower):
            return False
        if existing_lower == target_lower:
            return False
        # The next char must be a separator/qualifier — _ . - or alphanumeric.
        # Always true since startswith requires more chars; just ensure
        # we don't match "R" → "Resistor" (too generic — different family).
        # Minimum target length 3 keeps single-letter false positives out.
        return len(target_lower) >= 3

    # Search the named library first if it exists.
    if same_lib_uri:
        for f in _library_symbol_files(same_lib_uri):
            for name, node in _read_symbols_in_file(f):
                lname = name.lower()
                if lname == target:
                    consider(1, req_lib, name, node)
                elif is_specific_prefix(lname, target):
                    consider(2, req_lib, name, node)

    # Cross-library fallback.
    if not candidates:
        for lib_name, uri in table.items():
            if uri == same_lib_uri:
                continue  # already searched
            for f in _library_symbol_files(uri):
                for name, node in _read_symbols_in_file(f):
                    lname = name.lower()
                    if lname == target:
                        consider(3, lib_name, name, node)
                    elif is_specific_prefix(lname, target):
                        consider(4, lib_name, name, node)
                    elif len(target) >= 4 and target in lname:
                        consider(5, lib_name, name, node)

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], len(c[2])))  # rank ASC, shorter name first
    rank, lib, sym, node = candidates[0]
    return (lib, sym, node)


# ---------------------------------------------------------------------------
# Inheritance: (extends "Parent")
# ---------------------------------------------------------------------------

def _symbol_extends(sym_node: list) -> Optional[str]:
    """Return the parent symbol name if this symbol's def says (extends X)."""
    for sub in sym_node[1:] if isinstance(sym_node, list) else []:
        if isinstance(sub, list) and _to_str(sub[0]) == "extends" and len(sub) > 1:
            return _to_str(sub[1])
    return None


def _rename_symbol(sym_node: list, full_name: str) -> list:
    """Return a shallow copy with the symbol-name string set to full_name."""
    new = list(sym_node)
    if len(new) > 1:
        new[1] = full_name
    return new


# ---------------------------------------------------------------------------
# Public: ensure_lib_symbols_for_doc
# ---------------------------------------------------------------------------

def ensure_lib_symbols_for_doc(
    tree: list,
    lib_ids: List[str],
    project_dir: Optional[Path] = None,
) -> Dict[str, Dict[str, str]]:
    """For each lib_id, ensure its drawing definition (and any parent it
    extends) is inlined into the schematic's (lib_symbols ...) block.

    Returns {original_lib_id: {"resolved": <lib_id>, "status": <status>}} where
    status is one of:
      - "exact"   the lib_id exists verbatim in the user's libraries
      - "fuzzy"   no exact match; resolver picked the closest available symbol
                  (caller should warn the user — they may want to create the
                  real symbol or revert the substitution)
      - "missing" no acceptable match anywhere; nothing was added to lib_symbols
                  and the caller should abort the op with a clear error

    For "fuzzy" and "exact" the schematic's (lib_symbols ...) block is
    populated; for "missing" it is left untouched.
    """
    table_path = _discover_user_sym_lib_table(project_dir)
    table = _parse_sym_lib_table(table_path) if table_path else {}

    # Find or create the (lib_symbols ...) block.
    lib_block: Optional[list] = None
    for child in tree[1:] if isinstance(tree, list) else []:
        if isinstance(child, list) and _to_str(child[0]) == "lib_symbols":
            lib_block = child
            break
    if lib_block is None:
        lib_block = [Sym("lib_symbols")]
        tree.insert(1, lib_block)

    already_cached: set = set()
    for child in lib_block[1:]:
        if isinstance(child, list) and _to_str(child[0]) == "symbol" and len(child) > 1:
            already_cached.add(_to_str(child[1]))

    def fetch_into_cache(full_name: str, node: list) -> None:
        """Insert node (renamed to full_name) into lib_block, and recursively
        fetch any (extends Parent) chain so KiCad has full drawing data."""
        if full_name in already_cached:
            return
        lib_block.append(_rename_symbol(node, full_name))
        already_cached.add(full_name)
        parent = _symbol_extends(node)
        if parent and ":" in full_name:
            parent_lib = full_name.split(":", 1)[0]
            parent_full = f"{parent_lib}:{parent}"
            if parent_full in already_cached:
                return
            parent_uri = table.get(parent_lib)
            parent_node = (
                _find_symbol_in_library(parent_uri, parent) if parent_uri else None
            )
            if parent_node is None:
                fuzzy = _fuzzy_find_anywhere(table, parent_full)
                if fuzzy:
                    _, _, parent_node = fuzzy
            if parent_node is not None:
                fetch_into_cache(parent_full, parent_node)

    resolution: Dict[str, Dict[str, str]] = {}
    for raw_lib_id in lib_ids:
        if raw_lib_id in resolution:
            continue
        if ":" not in raw_lib_id:
            resolution[raw_lib_id] = {"resolved": raw_lib_id, "status": "missing"}
            continue
        lib_name, sym_name = raw_lib_id.split(":", 1)

        # 1. Try exact lookup in the requested library.
        node: Optional[list] = None
        status = "missing"
        resolved_lib = lib_name
        resolved_sym = sym_name
        uri = table.get(lib_name)
        if uri:
            node = _find_symbol_in_library(uri, sym_name)
            if node is not None:
                status = "exact"

        # 2. Fall back to fuzzy lookup across all libraries.
        if node is None:
            fuzzy = _fuzzy_find_anywhere(table, raw_lib_id)
            if fuzzy:
                resolved_lib, resolved_sym, node = fuzzy
                status = "fuzzy"

        resolved = f"{resolved_lib}:{resolved_sym}"
        resolution[raw_lib_id] = {"resolved": resolved, "status": status}

        if node is not None:
            fetch_into_cache(resolved, node)

    return resolution


def rewrite_placed_lib_ids(tree: list, resolution: Dict[str, Dict[str, str]]) -> int:
    """Walk every placed (symbol) at the schematic root and rewrite its
    (lib_id "...") child according to the resolution map. Returns count
    rewritten."""
    if not isinstance(tree, list):
        return 0
    n = 0
    for child in tree[1:]:
        if not (isinstance(child, list) and _to_str(child[0]) == "symbol"):
            continue
        for i, sub in enumerate(child[1:], start=1):
            if isinstance(sub, list) and _to_str(sub[0]) == "lib_id" and len(sub) > 1:
                old = _to_str(sub[1])
                info = resolution.get(old)
                if not info:
                    continue
                new = info.get("resolved", old)
                if new != old and info.get("status") != "missing":
                    child[i] = [Sym("lib_id"), new]
                    n += 1
    return n
