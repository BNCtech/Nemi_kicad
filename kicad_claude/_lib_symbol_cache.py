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
are expanded against the live process environment. KiCad itself does NOT
push these vars into the OS environment — it stores them inside
'<config_dir>/kicad_common.json' under "environment.vars" and reads them
back at app startup. A Python backend that didn't inherit env from KiCad
therefore sees those vars as undefined and every standard library lookup
fails. To stay in sync with what KiCad actually resolves, we also read
kicad_common.json from the highest-version user config dir and use its
"environment.vars" as a fallback when the OS environment doesn't carry
the variable.

Two-stage resolution:
  1. lib_id_aliases.json (user-extensible) is consulted FIRST. Renames like
     KiCad 8/9 'Device:CP' -> KiCad 10 'Device:C_Polarized' live here, one
     line per pair. An alias hit short-circuits to exact-match resolution.
  2. When neither the original nor the aliased lib_id resolves exactly, we
     fall back to fuzzy symbol-name search across every registered library
     (case-insensitive, then substring). This handles common LLM lib_id
     mistakes (Timer:NE555 when the real symbol is Timer:NE555D or
     Timer:LM555xN) without baking in an alias table that needs maintaining
     per shop.

A symbol's drawing primitives can live in a parent it `(extends "Parent")` —
we resolve the chain recursively so the parent's def lands in (lib_symbols)
too, otherwise KiCad has nothing to draw.
"""

import json
import os
import re
from functools import lru_cache as _functools_lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import sexpdata

Sym = sexpdata.Symbol


# ---------------------------------------------------------------------------
# Auto-portable lib root
# ---------------------------------------------------------------------------
# `kicad-sym-lib/` ships alongside this code. Wherever the code is mounted
# (F:/ on the author's PC, Z:/ on a LAN-mounted colleague PC, C:/ if cloned
# locally, /opt/envil/ on Linux), the library folder sits one level up from
# the package. We surface that path as ENVIL_LIB_ROOT in the process env so
# every URI in the user's sym-lib-table can be written as
# `${ENVIL_LIB_ROOT}/Device.kicad_symdir` and resolve correctly per-machine.
#
# `setdefault` lets a user explicitly override with their own env var (e.g.
# pointing at a curated lib folder elsewhere) without us stomping on it.
_REPO_LIB_ROOT = Path(__file__).resolve().parents[2] / "kicad-sym-lib"
if _REPO_LIB_ROOT.is_dir():
    os.environ.setdefault(
        "ENVIL_LIB_ROOT",
        str(_REPO_LIB_ROOT).replace("\\", "/"),
    )


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


def _highest_user_sym_lib_table() -> Optional[Path]:
    """Like _discover_user_sym_lib_table but ignores project-local tables — we
    only ever auto-register into the user-level (global) table so the entry
    survives across every project on this machine."""
    return _discover_user_sym_lib_table(project_dir=None)


def ensure_envil_generated_registered() -> Optional[str]:
    """Idempotent bootstrap: make sure the running KiCad install can find
    envil_generated.kicad_sym (the auto-cloned symbol library) without the
    user having to add it manually via Preferences → Manage Symbol Libraries.

    Why this exists: when the orchestrator is shared over LAN (user's repo
    mounted on a colleague's PC as Z:), the colleague's eeschema can read
    every stock KiCad library but has no row pointing at our envil_generated
    file. Result: schematics that placed any envil-namespaced symbol render
    with wires + labels but no symbol bodies (KiCad silently drops the
    instance when its lib_id can't be resolved at draw time).

    Strategy:
      1. Locate envil_generated.kicad_sym next to this repo
         (`<repo>/kicad-sym-lib/envil_generated.kicad_sym`).
      2. Find the active user-level sym-lib-table (highest KiCad version).
      3. If envil_generated is already registered there, do nothing.
      4. Otherwise append a row pointing at the absolute on-disk path so the
         entry works no matter which project KiCad opens later.

    Returns a one-line status string for logging, or None if nothing useful
    could be done (no table found / file missing / table not writable). All
    failures are non-fatal — the orchestrator still starts."""
    repo_root = Path(__file__).resolve().parents[2]
    envil_path = repo_root / "kicad-sym-lib" / "envil_generated.kicad_sym"
    if not envil_path.is_file():
        return None  # no library to register; benign on fresh checkouts

    table = _highest_user_sym_lib_table()
    if table is None:
        return None  # KiCad has never been launched on this machine

    try:
        text = table.read_text(encoding="utf-8-sig")
    except OSError:
        return None

    # Fast path: already registered. Match the name token, not a substring of
    # an unrelated description, by anchoring on the standard '(lib (name "X")'
    # prefix that KiCad always emits.
    if '(name "envil_generated")' in text:
        return f"envil_generated already registered in {table}"

    # Build the new row. Use forward slashes to match KiCad's own writes (it
    # normalises path separators on save), and an absolute path so it survives
    # being read with any open project (KIPRJMOD would resolve to wherever the
    # opened project lives, which is not necessarily next to kicad-sym-lib).
    uri = str(envil_path).replace("\\", "/")
    new_row = (
        '\t(lib (name "envil_generated") (type "KiCad") '
        f'(uri "{uri}") (options "") '
        '(descr "Envil auto-generated symbols"))'
    )

    # Insert before the closing ')' of the sym_lib_table sexp. Walk from the
    # end so we don't accidentally match a ')' inside a (descr ...) field.
    idx = text.rfind(")")
    if idx < 0:
        return None  # malformed table; refuse to write
    patched = text[:idx].rstrip() + "\n" + new_row + "\n)\n"

    try:
        # Light backup so an unexpected re-registration can be undone manually.
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        table.with_suffix(table.suffix + f".bak-{ts}").write_text(text, encoding="utf-8")
        table.write_text(patched, encoding="utf-8")
    except OSError as exc:
        return f"could not write {table}: {exc}"

    return f"envil_generated registered in {table} -> {uri}"


# Matches any URI whose drive-prefixed absolute path points inside a
# kicad-sym-lib folder, with anything afterwards. Captures the tail
# (everything from kicad-sym-lib/ onwards) so we can splice it onto
# ${ENVIL_LIB_ROOT}/.
#
# Examples that match (capture in group 1):
#   F:/Ki_CAD/kicad-sym-lib/Device.kicad_symdir       -> "Device.kicad_symdir"
#   Z:\Ki_CAD\kicad-sym-lib\envil_generated.kicad_sym -> "envil_generated.kicad_sym"
#   E:/anywhere/kicad-sym-lib/Connector.kicad_symdir  -> "Connector.kicad_symdir"
#
# Deliberately permissive about what comes before kicad-sym-lib/ — only the
# tail matters, and we already require a drive-letter prefix so we don't
# accidentally rewrite KIPRJMOD-style or stock KiCad install URIs.
_ABS_DRIVE_LIB_PATH_RE = re.compile(
    r'(?:[A-Za-z]:[/\\])'         # drive letter + separator
    r'(?:[^\s"<>|]*?[/\\])?'       # any preceding folders (non-greedy)
    r'kicad-sym-lib[/\\]'          # the library folder name
    r'([^"<>|]+?)'                 # capture: <name>.kicad_symdir or .kicad_sym
    r'(?=["\s<>|]|$)',             # stop at quote/whitespace/EOL
)


def ensure_lib_paths_portable() -> Optional[str]:
    """Rewrite the user-level sym-lib-table to use ${ENVIL_LIB_ROOT} instead
    of hardcoded drive paths, so the SAME table works for every collaborator
    regardless of whether they mount the repo on F:, E:, Z:, or C:.

    Why this exists: when the orchestrator is shared across multiple PCs over
    LAN/Nextcloud, each colleague mounts the user's repo on a different drive
    letter. A user-level sym-lib-table written with `F:/Ki_CAD/kicad-sym-lib/
    Device.kicad_symdir` is broken on any machine without an F: drive. ENVIL_
    LIB_ROOT is set at module load (see _REPO_LIB_ROOT block at top of file)
    to a path computed from __file__, which always resolves to the local copy
    of the repo on every machine.

    The rewrite only touches URIs that:
      - Start with a drive letter (so we never disturb KIPRJMOD-style or
        stock-install KICAD<n>_SYMBOL_DIR URIs), AND
      - Contain '/kicad-sym-lib/' somewhere in the path (so we only repoint
        our own bundled library, not arbitrary user-managed libraries).

    Idempotent: a second run finds no matching URIs and returns 'already
    portable'. Backs up the table to .bak-<timestamp> before each write.

    Returns a one-line status string, or None if nothing useful could be done."""
    table = _highest_user_sym_lib_table()
    if table is None:
        return None

    try:
        text = table.read_text(encoding="utf-8-sig")
    except OSError:
        return None

    matches = list(_ABS_DRIVE_LIB_PATH_RE.finditer(text))
    if not matches:
        return f"sym-lib-table already portable ({table})"

    # Build the replacement using forward slashes (matches KiCad's own writes
    # and avoids backslash-escape headaches in the s-expression).
    def _repl(m: "re.Match[str]") -> str:
        tail = m.group(1).replace("\\", "/")
        return f"${{ENVIL_LIB_ROOT}}/{tail}"

    patched = _ABS_DRIVE_LIB_PATH_RE.sub(_repl, text)

    try:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        table.with_suffix(table.suffix + f".bak-{ts}").write_text(text, encoding="utf-8")
        table.write_text(patched, encoding="utf-8")
    except OSError as exc:
        return f"could not write {table}: {exc}"

    return f"rewrote {len(matches)} absolute paths -> ${{ENVIL_LIB_ROOT}} in {table}"


def ensure_kicad_common_has_envil_lib_root() -> Optional[str]:
    """Inject ENVIL_LIB_ROOT into KiCad's own path-variables dict so eeschema
    can expand ${ENVIL_LIB_ROOT} when it reads the sym-lib-table at startup.

    ensure_lib_paths_portable() makes the TABLE drive-agnostic, but eeschema
    itself doesn't inherit Python's os.environ — KiCad reads user-defined
    path vars from kicad_common.json (Preferences -> Configure Paths in the
    GUI). Without this step, eeschema sees an unresolved ${ENVIL_LIB_ROOT}
    and reports every stock library as 'library not found' even though the
    orchestrator's Python lookup still works.

    Writes only to the kicad_common.json sibling of the discovered sym-lib-
    table (same KiCad version). Skips if the variable is already set to the
    same path; rewrites only if missing or pointing somewhere else."""
    import json

    table = _highest_user_sym_lib_table()
    if table is None:
        return None
    common = table.parent / "kicad_common.json"
    if not common.is_file():
        return None

    desired = os.environ.get("ENVIL_LIB_ROOT")
    if not desired:
        return None  # module-level setdefault didn't fire; nothing to write

    try:
        text = common.read_text(encoding="utf-8-sig")
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        return f"could not read {common}: {exc}"

    env_block = data.get("environment") or {}
    vars_block = env_block.get("vars") or {}
    if vars_block.get("ENVIL_LIB_ROOT") == desired:
        return f"ENVIL_LIB_ROOT already set in {common.name}"

    vars_block["ENVIL_LIB_ROOT"] = desired
    env_block["vars"] = vars_block
    data["environment"] = env_block

    try:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        common.with_suffix(common.suffix + f".bak-{ts}").write_text(text, encoding="utf-8")
        common.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        return f"could not write {common}: {exc}"

    return f"ENVIL_LIB_ROOT={desired} written to {common}"


# ---------------------------------------------------------------------------
# sym-lib-table parsing + URI expansion
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# Module-level memoization caches. KiCad sym-lib-table changes rarely (only
# when the user edits libraries in Preferences); meanwhile every chat-apply
# op was re-parsing 222 libraries from disk, which dominated apply latency
# and caused WebSocket timeouts. These caches are keyed by (path, mtime) so
# any on-disk edit invalidates them automatically without explicit busting.
<<<<<<< Updated upstream
_TABLE_CACHE: Dict[Tuple[str, float, str], Dict[str, str]] = {}
_LIB_FILES_CACHE: Dict[Tuple[str, float], List[Path]] = {}
_LIB_INDEX_CACHE: Dict[Tuple[str, float], Dict[str, Tuple[Path, list]]] = {}
=======
_TABLE_CACHE: Dict[Tuple[str, float, str, str], Dict[str, str]] = {}
_LIB_FILES_CACHE: Dict[Tuple[str, float], List[Path]] = {}
_LIB_INDEX_CACHE: Dict[Tuple[str, float], Dict[str, Tuple[Path, list]]] = {}
_KICAD_ENV_CACHE: Dict[Tuple[str, float], Dict[str, str]] = {}
>>>>>>> Stashed changes


def _path_stat_key(p: Path) -> Optional[Tuple[str, float]]:
    """Cache key for a path: (str, mtime). None if the path doesn't exist."""
    try:
        return (str(p), p.stat().st_mtime)
    except OSError:
        return None


def _alias_lib_id(raw_lib_id: str) -> str:
    """Map a deprecated / renamed lib_id to its current canonical name via
    lib_id_aliases.json. Returns the input unchanged when no alias is set.
    The map handles KiCad 9 -> 10 short-name renames (CP -> C_Polarized
    etc.) and any shop-specific renames the user adds. Applied BEFORE the
    library lookup so the alias resolves as 'exact', not 'fuzzy'."""
    from ._config_loader import load as _load_config
    try:
        aliases = _load_config("lib_id_aliases").get("aliases", {})
    except (FileNotFoundError, ValueError):
        return raw_lib_id
    return aliases.get(raw_lib_id, raw_lib_id)


# Package-suffix patterns commonly used after the part number. Recognised so
# we can strip one suffix-level when the exact name isn't in the library —
# e.g. 'ATmega328P-AU' (TQFP-32) -> 'ATmega328P-A', or 'STM32F103C8T6' ->
# 'STM32F103C8' if needed. Kept conservative: only matches the END of the
# symbol name, only 1-2 trailing groups, only uppercase letters (so part
# numbers with hyphenated value suffixes like 'R-1k' aren't mangled — the
# 'k' isn't uppercase so it's safe).
import re as _re
_PKG_SUFFIX_RE = _re.compile(r"^(.+?)(-[A-Z]{1,3}(?:\d)?)$")


def _strip_package_suffix(sym_name: str) -> Optional[str]:
    """Strip ONE trailing package-suffix group and return the shorter name,
    or None if no recognised suffix is present.

      'ATmega328P-AU'  -> 'ATmega328P'    (AU → uppercase 2-char suffix)
      'ATmega328P-A'   -> 'ATmega328P'
      'STM32F103C8T6'  -> None            (no hyphen)
      'R-1k'           -> None            (lowercase 'k')
      'LM358-D'        -> 'LM358'

    The caller uses this to attempt a second exact-lookup before falling
    through to expensive fuzzy matching. We only strip the LAST hyphen
    group; deeper stripping is left to fuzzy."""
    m = _PKG_SUFFIX_RE.match(sym_name)
    if not m:
        return None
    base = m.group(1)
    # Avoid stripping when the base is implausibly short — protects against
    # things like 'C-1' where the '-1' is meaningful, not a package suffix.
    if len(base) < 3:
        return None
    return base


_PINHEADER_RE = _re.compile(
    r"^PinHeader_(\d+)x(\d+)_P([\d.]+)mm_(Vertical|Horizontal)$",
    _re.IGNORECASE,
)


def _try_family_rename(table: Dict[str, str], lib_id: str) -> Optional[Tuple[str, str, list]]:
    """Cross-library family rename for known KiCad library splits / renames
    that the simple suffix-strip can't bridge. Each rule is a pure regex
    rewrite of (lib, sym) → (target_lib, candidate_sym_pattern); if the
    candidate exists in `target_lib`, return it. Adding a new family is
    a one-time pattern entry — no part-number knowledge.

    Currently handles:
      - Connector_PinHeader_*:PinHeader_NxM_P2.54mm_Vertical
        → Connector_Generic:Conn_NxM_Odd_Even (KiCad 10 canonical name)
      - Same with M=1 → Connector_Generic:Conn_01xN_Male / Conn_01xN

    Universal — extending to other splits is a single regex + replacement."""
    if ":" not in lib_id:
        return None
    lib_name, sym_name = lib_id.split(":", 1)

    # RF_Module:* → search across all configured wireless / RF libraries
    # for the same symbol name. The chat agent often picks a non-canonical
    # library path (`RF_Module:` instead of the user's specific lib);
    # falling through to fuzzy then takes too long. Cross-lib direct lookup
    # by symbol name is fast and right when the part exists somewhere.
    if lib_name.upper() in {"RF_MODULE", "WIRELESS", "RF"}:
        for other_lib, other_uri in table.items():
            if other_lib.upper() in {"RF_MODULE", "WIRELESS", "RF"} or \
               "RF" in other_lib.upper() or "WIRELESS" in other_lib.upper():
                node = _find_symbol_in_library(other_uri, sym_name)
                if node is not None:
                    return (other_lib, sym_name, node)

    # PinHeader_NxM_P*.*mm_Orientation  →  Conn_NxM_*  in Connector_Generic
    m = _PINHEADER_RE.match(sym_name)
    if m:
        cols = int(m.group(1))
        pins_per_col = int(m.group(2))
        # Generic-lib candidates, ordered most-likely first
        candidates = []
        if cols == 1:
            # 1×N → Conn_01xNN or Conn_01xN_Male/_Pin etc.
            candidates.append(f"Conn_01x{pins_per_col:02d}")
            candidates.append(f"Conn_01x{pins_per_col:02d}_Male")
            candidates.append(f"Conn_01x{pins_per_col:02d}_Pin")
        else:
            # NxM → Conn_NNxMM_Odd_Even / _Counter_Clockwise
            candidates.append(f"Conn_{cols:02d}x{pins_per_col:02d}_Odd_Even")
            candidates.append(f"Conn_{cols:02d}x{pins_per_col:02d}_Counter_Clockwise")
            candidates.append(f"Conn_{cols:02d}x{pins_per_col:02d}")
        for target_lib in ("Connector_Generic", lib_name):
            uri = table.get(target_lib)
            if not uri:
                continue
            for cand in candidates:
                node = _find_symbol_in_library(uri, cand)
                if node is not None:
                    return (target_lib, cand, node)
    return None


def _try_normalize_lib_id(table: Dict[str, str], lib_id: str) -> Optional[Tuple[str, str, list]]:
    """Generic package-variant resolution. Returns (resolved_lib, resolved_sym,
    node) on hit, None on miss.

    Two-phase search after stripping the original part's package suffix:

      Phase A — try the stripped name AS-IS (exact match). Catches the case
        where the library carries the bare-family name (e.g. 'LM358').

      Phase B — prefix scan. Find every symbol in the same library (then
        any library) whose name STARTS WITH the stripped form. Pick the
        candidate whose suffix shares the longest prefix with the original
        suffix, falling back to the alphabetically-first candidate as a
        deterministic tie-break. This handles 'ATmega328P-AU' → 'ATmega328P-A'
        (suffix 'AU' shares 1 char with 'A' → wins over 'M' / 'P').

    Universal — no hardcoded part numbers. Adding a new package family
    works automatically as long as the lib carries the base or a sister
    variant."""
    if ":" not in lib_id:
        return None
    lib_name, sym_name = lib_id.split(":", 1)
    stripped = _strip_package_suffix(sym_name)
    if not stripped or stripped == sym_name:
        return None

    # Phase A: bare-family exact lookup (same library first, then any).
    uri = table.get(lib_name)
    if uri:
        node = _find_symbol_in_library(uri, stripped)
        if node is not None:
            return (lib_name, stripped, node)
    for other_lib, other_uri in table.items():
        if other_lib == lib_name:
            continue
        node = _find_symbol_in_library(other_uri, stripped)
        if node is not None:
            return (other_lib, stripped, node)

    # Phase B: prefix scan. Symbols named `<stripped>-<suffix>` are variants
    # of the same family — pick the one whose suffix is closest to the
    # original suffix the prompt asked for.
    orig_suffix = sym_name[len(stripped):]  # includes the leading '-'
    def _suffix_score(cand_sym: str) -> Tuple[int, str]:
        # higher prefix-match length wins; tie-break alphabetical.
        cand_suffix = cand_sym[len(stripped):]
        common = 0
        for a, b in zip(orig_suffix, cand_suffix):
            if a == b:
                common += 1
            else:
                break
        return (-common, cand_sym)  # negate so smaller=better in sort

    def _scan(lib: str, uri_str: str) -> Optional[Tuple[str, str, list]]:
        idx = _library_index(uri_str)
        prefix_lower = (stripped + "-").lower()
        matches: List[str] = [n for n in idx.keys()
                              if n.lower().startswith(prefix_lower)]
        if not matches:
            return None
        best = sorted(matches, key=_suffix_score)[0]
        return (lib, best, idx[best][1])

    if uri:
        hit = _scan(lib_name, uri)
        if hit is not None:
            return hit
    for other_lib, other_uri in table.items():
        if other_lib == lib_name:
            continue
        hit = _scan(other_lib, other_uri)
        if hit is not None:
            return hit
    return None


<<<<<<< Updated upstream
def _expand_uri(uri: str, kiprjmod: Optional[Path] = None) -> str:
    """Expand ${KICAD9_SYMBOL_DIR} etc. against os.environ. Unknown vars
    are left in place (KiCad does the same — the file simply won't resolve).
=======
def _load_kicad_env_from_json(config_dir: Path) -> Dict[str, str]:
    """Read 'environment.vars' from kicad_common.json in a KiCad config dir.

    Cached by (path, mtime) so an edit in KiCad's Preferences → Configure
    Paths is picked up automatically on the next call without restart.
    Returns {} when the file is missing, malformed, or has no env section.
    """
    p = config_dir / "kicad_common.json"
    stat = _path_stat_key(p)
    if stat is None:
        return {}
    cached = _KICAD_ENV_CACHE.get(stat)
    if cached is not None:
        return cached
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        _KICAD_ENV_CACHE[stat] = {}
        return {}
    env = data.get("environment", {}).get("vars", {}) if isinstance(data, dict) else {}
    result = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
    _KICAD_ENV_CACHE[stat] = result
    return result


def _discover_kicad_env_vars() -> Dict[str, str]:
    """Return env vars set inside KiCad's own Configure Paths (kicad_common.json).

    KiCad stores Configure Paths entries here and reads them at startup; they
    do NOT land in the OS process environment, so a sibling Python process
    that didn't fork from KiCad sees them as undefined. Picks the
    highest-version user config dir on this machine (same precedence as
    sym-lib-table discovery).
    """
    for root in _candidate_config_roots():
        if not root.is_dir():
            continue
        version_dirs = [p for p in root.iterdir() if p.is_dir() and re.match(r"^\d", p.name)]
        version_dirs.sort(key=lambda p: _version_sort_key(p.name), reverse=True)
        for vd in version_dirs:
            if (vd / "kicad_common.json").is_file():
                return _load_kicad_env_from_json(vd)
    return {}


def _expand_uri(uri: str, kiprjmod: Optional[Path] = None) -> str:
    """Expand ${KICAD9_SYMBOL_DIR} etc. against os.environ, with a fallback
    to KiCad's own kicad_common.json (Configure Paths). Unknown vars are
    left in place (KiCad does the same — the file simply won't resolve).

    Precedence: OS env first, then kicad_common.json. This lets a user who
    explicitly `setx`-ed a var override KiCad's stored value, while still
    making the common case work — where the var is ONLY set inside KiCad's
    Preferences → Configure Paths and never exported to the shell.
>>>>>>> Stashed changes

    KIPRJMOD is special: KiCad sets it to the open project's directory at
    file-open time, so it's NEVER in os.environ here. When the caller knows
    the project context (the directory containing the schematic being
    modified), it passes kiprjmod and we substitute it just like KiCad does.
    Without this, project-local tables that use ${KIPRJMOD}/... resolve to
    a literal '${KIPRJMOD}/...' path that doesn't exist, and every symbol
    looks "missing".
    """
<<<<<<< Updated upstream
=======
    kicad_env = _discover_kicad_env_vars()
>>>>>>> Stashed changes
    def repl(m):
        var = m.group(1)
        if var == "KIPRJMOD" and kiprjmod is not None:
            return str(kiprjmod)
<<<<<<< Updated upstream
        return os.environ.get(var, m.group(0))
=======
        val = os.environ.get(var)
        if val is not None:
            return val
        return kicad_env.get(var, m.group(0))
>>>>>>> Stashed changes
    return _ENV_RE.sub(repl, os.path.expanduser(uri))


def _parse_sym_lib_table(
    path: Path,
    seen: Optional[set] = None,
    kiprjmod: Optional[Path] = None,
) -> Dict[str, str]:
    """Return {library_name: expanded_uri}. Handles (type "Table") entries
    that reference ANOTHER sym-lib-table file by chasing them recursively.

    kiprjmod, when provided, is substituted for ${KIPRJMOD} during URI
    expansion (see _expand_uri). It is plumbed unchanged through nested-table
    recursion — KIPRJMOD always refers to the current project, never to the
    nested table's directory.

    Result is memoized per (path, mtime, kiprjmod) since sym-lib-table changes
    rarely. The kiprjmod component of the key prevents collisions when the
    same table is parsed from different project contexts.
    """
<<<<<<< Updated upstream
    cache_key: Optional[Tuple[str, float, str]] = None
    if seen is None:
        stat = _path_stat_key(path)
        if stat is not None:
            cache_key = (stat[0], stat[1], str(kiprjmod) if kiprjmod else "")
=======
    cache_key: Optional[Tuple[str, float, str, str]] = None
    if seen is None:
        stat = _path_stat_key(path)
        if stat is not None:
            # Fingerprint the discovered kicad_common.json env vars into the
            # key — when the user edits Configure Paths in KiCad, the parsed
            # table's expanded URIs change even though sym-lib-table itself
            # didn't, so the cache must invalidate.
            env_fp = repr(sorted(_discover_kicad_env_vars().items()))
            cache_key = (stat[0], stat[1], str(kiprjmod) if kiprjmod else "", env_fp)
>>>>>>> Stashed changes
            if cache_key in _TABLE_CACHE:
                return _TABLE_CACHE[cache_key]
    seen = seen or set()
    if path in seen:
        return {}
    seen.add(path)
    try:
        # utf-8-sig transparently strips a BOM if present. KiCad / Notepad
        # sometimes save sym-lib-table with a BOM, which sexpdata's lexer
        # otherwise rejects — silently returning {} hides the project-local
        # table and triggers wall-of-"missing-symbol" errors on every apply.
        text = path.read_text(encoding="utf-8-sig")
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
        expanded = _expand_uri(uri, kiprjmod=kiprjmod)
        if lib_type.lower() == "table":
            # Nested table — pull its entries in (without overwriting the
            # current table's entries; project / user tables shadow defaults).
            nested = _parse_sym_lib_table(Path(expanded), seen, kiprjmod=kiprjmod)
            for nk, nv in nested.items():
                out.setdefault(nk, nv)
        else:
            out[name] = expanded
<<<<<<< Updated upstream
    # Bundled-library fallback. The orchestrator ships `<repo>/kicad-sym-lib/`
    # with copies of every standard KiCad library it needs (Device, power,
    # Timer, Regulator_Linear, ...). Friend-PCs frequently have a sym-lib-
    # table that uses ${KICAD10_SYMBOL_DIR}/... URIs — that var is only set
    # inside KiCad's own process, NOT in our Python process, so `_expand_uri`
    # leaves the literal in place and every stock library looks "missing".
    # Fix: for each bundled lib, if the table either doesn't list it OR lists
    # a URI that fails to resolve on disk, substitute the bundled path. The
    # table-driven URI still wins when it actually resolves — we never
    # override a working entry.
    bundled = _bundled_libs()
    for lib_name, bundled_path in bundled.items():
        cur = out.get(lib_name)
        if cur and Path(cur).exists():
            continue  # user's table works; leave it alone
        out[lib_name] = bundled_path
=======
>>>>>>> Stashed changes
    # Only the top-level call (seen-set just initialized to this path) writes
    # to the module cache, so nested-table recursion doesn't pollute it.
    if seen == {path} and cache_key is not None:
        _TABLE_CACHE[cache_key] = out
<<<<<<< Updated upstream
    return out


@_functools_lru_cache(maxsize=1)
def _bundled_libs() -> Dict[str, str]:
    """Discover every .kicad_sym / .kicad_symdir inside this orchestrator's
    bundled `<repo>/kicad-sym-lib/` folder. Returns {lib_name: absolute_path}
    using forward slashes (matches how KiCad writes URIs).

    Cached after first call — the bundled folder doesn't change during a
    server run, so we scan once and reuse."""
    if not _REPO_LIB_ROOT.is_dir():
        return {}
    out: Dict[str, str] = {}
    for entry in _REPO_LIB_ROOT.iterdir():
        if entry.is_dir() and entry.suffix == ".kicad_symdir":
            out[entry.stem] = str(entry).replace("\\", "/")
        elif entry.is_file() and entry.suffix == ".kicad_sym":
            out[entry.stem] = str(entry).replace("\\", "/")
=======
>>>>>>> Stashed changes
    return out


# ---------------------------------------------------------------------------
# Symbol discovery within a library
# ---------------------------------------------------------------------------

def _library_symbol_files(library_uri: str) -> List[Path]:
    """KiCad libraries come in two shapes: a single .kicad_sym file (one big
    file holding every symbol) OR a .kicad_symdir directory (one file per
    symbol). Return every .kicad_sym we should scan.

    User's custom layout (envilcad fork): the sym-lib-table URI points at
    `<lib>.kicad_sym` BUT the on-disk artefact is `<lib>.kicad_symdir/`
    (one file per part). The URI never gets updated when the lib is
    converted to the directory form, so we transparently fall back:
    if the .kicad_sym path doesn't exist, look next to it for the
    .kicad_symdir sibling. Universal — no per-library configuration."""
    p = Path(library_uri)
    if p.is_dir():
        return sorted(p.glob("*.kicad_sym"))
    if p.is_file():
        return [p]
    # Try the .kicad_symdir sibling — the user's split-file lib layout.
    if p.suffix == ".kicad_sym":
        symdir = p.with_suffix(".kicad_symdir")
        if symdir.is_dir():
            return sorted(symdir.glob("*.kicad_sym"))
    return []


def _library_index(library_uri: str) -> Dict[str, Tuple[Path, list]]:
    """Build (or fetch from cache) a {symbol_name: (file, node)} index for the
    whole library. KEYED BY THE LIBRARY'S MTIME so on-disk edits invalidate
    automatically.

    Without this, every fuzzy lookup across 222 libraries re-read thousands
    of .kicad_sym files from disk — dominating apply latency.
    """
    p = Path(library_uri)
    key = _path_stat_key(p)
    if key is not None and key in _LIB_INDEX_CACHE:
        return _LIB_INDEX_CACHE[key]
    index: Dict[str, Tuple[Path, list]] = {}
    for f in _library_symbol_files(library_uri):
        for name, node in _read_symbols_in_file(f):
            index.setdefault(name, (f, node))
    if key is not None:
        _LIB_INDEX_CACHE[key] = index
    return index


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
    index = _library_index(library_uri)
    hit = index.get(symbol_name)
    if hit is not None:
        return hit[1]
    target = symbol_name.lower()
    for name, (_f, node) in index.items():
        if name.lower() == target:
            return node
    return None


def _fuzzy_find_anywhere(
    table: Dict[str, str], requested_lib_id: str
) -> Optional[Tuple[str, str, list, List[str]]]:
    """When an exact lib_id ("Timer:NE555") doesn't exist, search EVERY
    registered library for a symbol whose name plausibly matches the
    requested symbol part. Returns (resolved_lib_name, resolved_symbol_name,
    sexp) or None.

    Ranking (best first):
      1. Same library, name equal (case-insensitive)
      2. Same library, requested name is a prefix of an existing symbol
      3. Same library, package-code infix (target = "Family-Suffix" matches
         "FamilyXX-Suffix" where XX is a package code)
      4. ANY library, exact-name (case-insensitive)
      5. ANY library, requested name is a prefix
      6. ANY library, package-code infix
      7. ANY library, requested name is a substring
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

    # Package-code infix: real KiCad libraries name regulators / driver chips
    # as "Family<Pkg>-Suffix" (LM1117T-5.0, LM1117DT-5.0, LM7805_TO220,
    # LM317T-3.3). An LLM-suggested lib_id often omits the package code
    # ("LM1117-5.0"), so split target on the LAST hyphen and accept any
    # existing name that bridges them. Fully algorithmic: no part-specific
    # knowledge — works for any family with a "-Voltage" / "-ADJ" suffix.
    target_prefix, _, target_suffix = target.rpartition("-")

    def has_package_infix(existing_lower: str) -> bool:
        if not target_prefix or not target_suffix:
            return False
        if len(target_prefix) < 3:
            return False  # avoid degenerate one/two-letter prefixes
        if not (existing_lower.startswith(target_prefix)
                and existing_lower.endswith("-" + target_suffix)):
            return False
        # Reject the case where existing == target (handled by rank 1/4).
        if existing_lower == target:
            return False
        # Reject the case where there's nothing between — the names would
        # then be identical, caught above. Defence in depth:
        infix = existing_lower[len(target_prefix):-(len(target_suffix) + 1)]
        return len(infix) >= 1 and infix[0].isalnum()

    # Search the named library first if it exists.
    if same_lib_uri:
        for name, (_f, node) in _library_index(same_lib_uri).items():
            lname = name.lower()
            if lname == target:
                consider(1, req_lib, name, node)
            elif is_specific_prefix(lname, target):
                consider(2, req_lib, name, node)
            elif has_package_infix(lname):
                consider(3, req_lib, name, node)

    # Cross-library fallback.
    if not candidates:
        for lib_name, uri in table.items():
            if uri == same_lib_uri:
                continue  # already searched
            for name, (_f, node) in _library_index(uri).items():
                lname = name.lower()
                if lname == target:
                    consider(4, lib_name, name, node)
                elif is_specific_prefix(lname, target):
                    consider(5, lib_name, name, node)
                elif has_package_infix(lname):
                    consider(6, lib_name, name, node)
                elif len(target) >= 4 and target in lname:
                    consider(7, lib_name, name, node)

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], len(c[2])))  # rank ASC, shorter name first
    # Alternates = EVERY other plausible candidate, ranked best-first. The
    # chat-apply path uses this to show the user a "pick one" list when a
    # fuzzy substitution is happening — so they decide instead of us
    # guessing. Capped at 12 so a request like "Device:R" doesn't flood
    # the chat with hundreds of resistor variants; the cap is per-call so
    # the list always starts with the closest matches.
    chosen = (candidates[0][1], candidates[0][2])
    rank, lib, sym, node = candidates[0]
    # When the best candidate is an EXACT-NAME match (rank 1 same-lib or
    # rank 4 cross-lib), it IS the definitive answer — the other rank-2/3/5
    # entries are unrelated parts that just happen to share a prefix (e.g.
    # Device:SW_Push → Switch:SW_Push is exact; Switch:SW_Push_LED /
    # Switch:SW_Push_DPDT are *different parts*, not package variants).
    # Treat exact-name resolution as unambiguous: no alternates surfaced,
    # caller stays silent.
    if rank in (1, 4):
        return (lib, sym, node, [])
    alternates: List[str] = []
    seen: set = {chosen}
    for _r, alt_lib, alt_sym, _n in candidates[1:]:
        key = (alt_lib, alt_sym)
        if key in seen:
            continue
        seen.add(key)
        alternates.append(f"{alt_lib}:{alt_sym}")
        if len(alternates) >= 12:
            break
    return (lib, sym, node, alternates)
<<<<<<< Updated upstream


# ---------------------------------------------------------------------------
# Phase 3-5: capability / pin-compat / generic-placeholder resolver
# ---------------------------------------------------------------------------
#
# These run AFTER the existing name-based fuzzy resolver fails. The goal is
# to NEVER return `missing` when there's any plausible symbol the LLM could
# have meant — keep the topology alive even when the exact part doesn't
# exist in the user's libraries.
#
# Strategy ladder (descending confidence):
#   Phase 3 (token match)   — overlap of name tokens + library namespace,
#                             optionally weighted by description / keywords.
#   Phase 4 (pin-compat)    — find a symbol with the same pin count and the
#                             same coarse role (IC vs passive vs connector).
#   Phase 5 (generic)       — synthesize a Generic_<N>pin placeholder into
#                             the envil_generated library so add_component
#                             succeeds and the labels around it have pins
#                             to anchor to.

_PACKAGE_RE = __import__("re").compile(
    r"(?:^|[-_])("
    r"QFN\d+|TQFP\d+|LQFP\d+|SOIC\d+|TSSOP\d+|MSOP\d+|DIP\d+|SOP\d+|"
    r"SOT\d+|TO\d+|BGA\d+|QFP\d+|VQFN\d+|UQFN\d+|WQFN\d+|GQFN\d+|"
    r"SSOP\d+|TQFN\d+|DFN\d+"
    r")(?:[-_]|$)",
    __import__("re").IGNORECASE,
)
_PIN_NUM_RE = __import__("re").compile(r"(\d+)$")
_TOKEN_SPLIT_RE = __import__("re").compile(r"[\s_\-:.]+")
# Stopwords that show up in every other symbol name and add noise to the
# capability score — dropped before scoring.
_TOKEN_STOPWORDS = frozenset({
    "ic", "chip", "module", "device", "generic", "interface",
    "package", "smd", "tht", "th", "lib", "kicad",
})


def _tokenize_lib_id(lib_id: str) -> List[str]:
    """Split a lib_id into searchable tokens. Lower-cased, stopworded,
    de-duplicated, package-suffix preserved AS its own token (so QFN28
    matches QFN28 across families). The library namespace contributes
    its own tokens (Interface_USB → ['interface', 'usb']).

    Pure data — no part-number knowledge required. Works for any lib_id."""
    raw = lib_id.replace(":", "_")
    parts = _TOKEN_SPLIT_RE.split(raw)
    out: List[str] = []
    seen: set = set()
    import re as _re_cc
    for p in parts:
        if not p:
            continue
        # Single-letter parts between separators are KEPT as their own token
        # (USB_C → ['usb', 'c'], USB_A → ['usb', 'a']) — otherwise USB_C and
        # USB_A become indistinguishable, breaking USB-receptacle resolution.
        if len(p) == 1 and p.isalpha():
            tok = p.lower()
            if tok not in seen and tok not in _TOKEN_STOPWORDS:
                seen.add(tok)
                out.append(tok)
            continue
        # Break CamelCase + digit runs (USBHostIC → ['USB', 'Host', 'IC']).
        for sub in _re_cc.findall(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z][a-z])|[A-Z]+|[a-z]+|\d+", p):
            tok = sub.lower()
            if len(tok) < 2:
                continue
            if tok in _TOKEN_STOPWORDS:
                continue
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
    return out


def _estimate_pin_count(lib_id: str) -> Optional[int]:
    """Try to read a pin count from package-style suffixes in a lib_id —
    QFN28, DIP14, SOIC8, GQFN20, etc. Returns None when nothing matches;
    callers fall through to Phase 5's pin-count-from-context path."""
    m = _PACKAGE_RE.search(lib_id)
    if not m:
        return None
    pkg = m.group(1)
    n_match = _PIN_NUM_RE.search(pkg)
    if not n_match:
        return None
    try:
        n = int(n_match.group(1))
    except ValueError:
        return None
    if 2 <= n <= 256:
        return n
    return None


def _count_pins_in_symbol(node: list) -> int:
    """Count (pin ...) entries inside a (symbol ...) node, including
    sub-units. Multi-unit ICs nest sub-units like (symbol "U1_1_0" ...)
    which carry the actual pin entries."""
    n = 0
    for child in node[1:] if isinstance(node, list) else []:
        if not isinstance(child, list):
            continue
        head = _to_str(child[0])
        if head == "pin":
            n += 1
        elif head == "symbol":
            for sub in child[1:]:
                if isinstance(sub, list) and _to_str(sub[0]) == "pin":
                    n += 1
    return n


def _symbol_property(node: list, prop_name: str) -> str:
    """Read a (property "Name" "Value" ...) string off the symbol node.
    Returns '' when the property is absent."""
    target = prop_name.lower()
    for child in node[1:] if isinstance(node, list) else []:
        if not (isinstance(child, list) and _to_str(child[0]) == "property"
                and len(child) >= 3):
            continue
        name = _to_str(child[1])
        if name.lower() == target:
            return _to_str(child[2])
    return ""


def _symbol_fingerprint_tokens(lib_name: str, sym_name: str, node: list) -> List[str]:
    """Token cloud for a symbol — name + description + ki_keywords + library
    namespace. Used by Phase 3 capability matching. Cached at the index
    level so re-tokenising every symbol on every resolution is avoided."""
    name_toks = _tokenize_lib_id(f"{lib_name}:{sym_name}")
    desc = _symbol_property(node, "Description")
    kws = _symbol_property(node, "ki_keywords")
    extra = _tokenize_lib_id(f"{desc} {kws}")
    out: List[str] = list(name_toks)
    seen: set = set(name_toks)
    for tok in extra:
        if tok not in seen:
            out.append(tok)
            seen.add(tok)
    return out


def _capability_find_anywhere(
    table: Dict[str, str], requested_lib_id: str,
    target_pin_count: Optional[int],
) -> Optional[Tuple[str, str, list, float, List[str]]]:
    """Phase 3: token-overlap search across all libraries.

    Score = jaccard(query, fingerprint), boosted when pin count matches
    the target. Returns the best candidate above MIN_SCORE — caller decides
    whether to use it based on confidence.

    Universal: no part-specific knowledge, no per-IC rules. Works for any
    failed lib_id as long as ANY symbol in any registered library shares
    enough name / description tokens. CP2102 → Interface_UART:CP2102 (or
    a sibling like FT232RL) because both have tokens {usb, uart, bridge}
    in description/keywords."""
    MIN_SCORE = 0.15
    PIN_MATCH_BOOST = 0.20
    SAME_LIB_BOOST = 0.10  # candidate's library namespace overlaps with the
                            # requested one (Connector_USB → Connector:* gets
                            # a small bump over Interface:* with similar tokens)
    SYMBOL_NAME_SUBSTR_BOOST = 0.15  # candidate's symbol name is a prefix /
                                       # substring of the requested name, or
                                       # vice versa (USB_C_Receptacle is a
                                       # prefix of USB_C_Receptacle_USB2.0_...)

    query_tokens = set(_tokenize_lib_id(requested_lib_id))
    if not query_tokens:
        return None

    # Library-name tokens for the requested lib_id — used by SAME_LIB_BOOST.
    req_lib_name = (requested_lib_id.split(":", 1)[0]
                    if ":" in requested_lib_id else "")
    req_sym_name = (requested_lib_id.split(":", 1)[1]
                    if ":" in requested_lib_id else requested_lib_id)
    req_lib_tokens = set(_tokenize_lib_id(req_lib_name))
    req_sym_lower = req_sym_name.lower()

    best: Optional[Tuple[float, str, str, list]] = None
    alt_top: List[Tuple[float, str, str]] = []

    for lib_name, uri in table.items():
        try:
            index = _library_index(uri)
        except Exception:
            continue
        lib_tokens = set(_tokenize_lib_id(lib_name))
        lib_overlap = bool(req_lib_tokens & lib_tokens)
        for sym_name, (_f, node) in index.items():
            fp = set(_symbol_fingerprint_tokens(lib_name, sym_name, node))
            if not fp:
                continue
            inter = len(query_tokens & fp)
            if inter == 0:
                continue
            union_size = len(query_tokens | fp)
            score = inter / union_size  # jaccard
            if target_pin_count is not None:
                pin_n = _count_pins_in_symbol(node)
                if pin_n and abs(pin_n - target_pin_count) <= max(2, target_pin_count // 8):
                    score += PIN_MATCH_BOOST
            if lib_overlap:
                score += SAME_LIB_BOOST
            sym_lower = sym_name.lower()
            if sym_lower and (sym_lower in req_sym_lower
                               or req_sym_lower.startswith(sym_lower)):
                score += SYMBOL_NAME_SUBSTR_BOOST
            if score < MIN_SCORE:
                continue
            if best is None or score > best[0]:
                if best is not None:
                    alt_top.append((best[0], best[1], best[2]))
                best = (score, lib_name, sym_name, node)
            else:
                alt_top.append((score, lib_name, sym_name))

    if best is None:
        return None

    alt_top.sort(key=lambda x: -x[0])
    alternates = [f"{lib}:{sym}" for _s, lib, sym in alt_top[:8]]
    score, lib, sym, node = best
    return (lib, sym, node, score, alternates)


def _pin_compat_find_anywhere(
    table: Dict[str, str], target_pin_count: int,
) -> Optional[Tuple[str, str, list]]:
    """Phase 4: when token matching fails, fall back to a symbol with the
    same pin count and a generic-IC-looking name (no specific part-number
    semantics). Prefers `Logic`/`MCU`/`Driver` library namespaces over
    `Connector` because connectors have different pin geometry that breaks
    the placer's IC zone-fits. Returns None when nothing in the user's
    libraries comes close."""
    if target_pin_count < 4:
        return None  # don't fallback for resistors / 2-pin parts

    GENERIC_LIB_TOKENS = {"logic", "mcu", "driver", "interface", "amplifier",
                          "memory", "device"}

    best_node: Optional[list] = None
    best_lib = best_sym = ""
    best_score = -1
    for lib_name, uri in table.items():
        try:
            index = _library_index(uri)
        except Exception:
            continue
        lib_tok_set = set(_tokenize_lib_id(lib_name))
        lib_boost = 1 if (lib_tok_set & GENERIC_LIB_TOKENS) else 0
        for sym_name, (_f, node) in index.items():
            pin_n = _count_pins_in_symbol(node)
            if pin_n != target_pin_count:
                continue
            # Prefer compact names — short tend to be generic / family parts.
            score = lib_boost * 100 - len(sym_name)
            if score > best_score:
                best_score = score
                best_node = node
                best_lib = lib_name
                best_sym = sym_name
    if best_node is None:
        return None
    return (best_lib, best_sym, best_node)


def _build_generic_placeholder_node(pin_count: int) -> list:
    """Synthesize a generic IC symbol with `pin_count` pins on a single
    rectangular body. Pin layout: half on the left side, half on the right,
    numbered 1..N (1..N/2 down the left, N..N/2+1 up the right, KLC
    convention). No power-pin marking — pins are generic 'passive'
    electrical type so ERC doesn't flag them as expecting drivers.

    The body is sized to match the pin count: 7.62 mm wide, height = pin
    count * 2.54 / 2 mm. Lives in the envil_generated.kicad_sym library
    (auto-created and auto-registered by ensure_envil_generated_registered)."""
    pin_count = max(2, int(pin_count))
    half = (pin_count + 1) // 2
    body_h_half = (half + 1) * 1.27  # half-height in mm
    body_w_half = 3.81               # 7.62 mm wide

    sym_name = f"Generic_{pin_count}Pin"
    out: list = [
        Sym("symbol"), sym_name,
        [Sym("pin_numbers"), [Sym("hide"), Sym("yes")]],
        [Sym("pin_names"), [Sym("offset"), 0.508]],
        [Sym("in_bom"), Sym("yes")],
        [Sym("on_board"), Sym("yes")],
        [Sym("property"), "Reference", "U",
         [Sym("at"), 0.0, body_h_half + 1.27, 0.0],
         [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]]]],
        [Sym("property"), "Value", sym_name,
         [Sym("at"), 0.0, -body_h_half - 1.27, 0.0],
         [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]]]],
        [Sym("property"), "Footprint", "",
         [Sym("at"), 0.0, 0.0, 0.0],
         [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]],
          [Sym("hide"), Sym("yes")]]],
        [Sym("property"), "Datasheet", "~",
         [Sym("at"), 0.0, 0.0, 0.0],
         [Sym("effects"), [Sym("font"), [Sym("size"), 1.27, 1.27]],
          [Sym("hide"), Sym("yes")]]],
    ]
    # The body rectangle lives inside a sub-unit (KLC convention for any
    # symbol with pins). Use _0_1 for the body (graphic) and _1_1 for the
    # pins (electrical).
    body_sub = [
        Sym("symbol"), f"{sym_name}_0_1",
        [Sym("rectangle"),
         [Sym("start"), -body_w_half, body_h_half],
         [Sym("end"), body_w_half, -body_h_half],
         [Sym("stroke"), [Sym("width"), 0.254], [Sym("type"), Sym("default")]],
         [Sym("fill"), [Sym("type"), Sym("background")]]],
    ]
    pin_sub: list = [Sym("symbol"), f"{sym_name}_1_1"]
    pin_x_left = -body_w_half - 2.54
    pin_x_right = body_w_half + 2.54
    # Left pins, top to bottom: 1, 2, ..., half
    for i in range(1, half + 1):
        py = body_h_half - i * 2.54
        pin_sub.append([
            Sym("pin"), Sym("passive"), Sym("line"),
            [Sym("at"), pin_x_left, py, 0.0],
            [Sym("length"), 2.54],
            [Sym("name"), f"~", [Sym("effects"),
                                  [Sym("font"), [Sym("size"), 1.27, 1.27]]]],
            [Sym("number"), str(i), [Sym("effects"),
                                       [Sym("font"), [Sym("size"), 1.27, 1.27]]]],
        ])
    # Right pins, bottom to top: half+1, half+2, ..., pin_count
    right_count = pin_count - half
    for j in range(right_count):
        pin_no = half + j + 1
        py = -body_h_half + (j + 1) * 2.54
        pin_sub.append([
            Sym("pin"), Sym("passive"), Sym("line"),
            [Sym("at"), pin_x_right, py, 180.0],
            [Sym("length"), 2.54],
            [Sym("name"), f"~", [Sym("effects"),
                                  [Sym("font"), [Sym("size"), 1.27, 1.27]]]],
            [Sym("number"), str(pin_no), [Sym("effects"),
                                            [Sym("font"), [Sym("size"), 1.27, 1.27]]]],
        ])
    out.append(body_sub)
    out.append(pin_sub)
    return out


def _ensure_generic_placeholder_in_envil_lib(pin_count: int) -> Optional[Tuple[str, str, list]]:
    """Ensure envil_generated.kicad_sym contains a Generic_<N>pin symbol
    for the requested pin count. Returns (lib_name, sym_name, node) or
    None when the envil_generated library isn't writable.

    The envil_generated library is registered by
    `ensure_envil_generated_registered` at server startup, so by the
    time chat-apply calls us the URI is in the sym-lib-table."""
    table_path = _highest_user_sym_lib_table()
    if table_path is None:
        return None
    try:
        table = _parse_sym_lib_table(table_path)
    except Exception:
        return None
    uri = table.get("envil_generated")
    if not uri:
        return None
    expanded = _expand_uri(uri)
    lib_path = Path(expanded)
    if not lib_path.exists():
        # The lib file may live under a .kicad_symdir/ split layout.
        if lib_path.suffix == ".kicad_sym":
            symdir = lib_path.with_suffix(".kicad_symdir")
            if symdir.is_dir():
                target_file = symdir / f"Generic_{pin_count}Pin.kicad_sym"
                if not target_file.exists():
                    _write_split_envil_symbol(target_file, pin_count)
                # Re-read to get a parsed node.
                for f in [target_file]:
                    for name, node in _read_symbols_in_file(f):
                        if name == f"Generic_{pin_count}Pin":
                            # Bust the library cache so the new symbol is visible.
                            _LIB_INDEX_CACHE.pop(_path_stat_key(symdir), None)
                            return ("envil_generated", name, node)
                return None
        return None
    # Single-file lib: append to it.
    sym_name = f"Generic_{pin_count}Pin"
    # Check if already present.
    for name, node in _read_symbols_in_file(lib_path):
        if name == sym_name:
            return ("envil_generated", name, node)
    # Append.
    try:
        text = lib_path.read_text(encoding="utf-8")
        tree = sexpdata.loads(text)
        if not (isinstance(tree, list)
                and _to_str(tree[0]) == "kicad_symbol_lib"):
            return None
        new_node = _build_generic_placeholder_node(pin_count)
        tree.append(new_node)
        lib_path.write_text(sexpdata.dumps(tree), encoding="utf-8")
        _LIB_INDEX_CACHE.pop(_path_stat_key(lib_path), None)
        return ("envil_generated", sym_name, new_node)
    except Exception:
        return None


def _write_split_envil_symbol(target_file: Path, pin_count: int) -> None:
    """Write a single-symbol .kicad_sym file under the split-lib layout
    used by the user's KiCad fork (envil_generated.kicad_symdir/ contains
    one file per part)."""
    sym_name = f"Generic_{pin_count}Pin"
    new_node = _build_generic_placeholder_node(pin_count)
    tree = [Sym("kicad_symbol_lib"),
            [Sym("version"), 20211014],
            [Sym("generator"), "envil_kicad_claude"],
            new_node]
    target_file.write_text(sexpdata.dumps(tree), encoding="utf-8")
=======
>>>>>>> Stashed changes


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
    """Return a shallow copy with the symbol-name string set to full_name.
    No body manipulation — used only for ROOT (non-derived) symbols.
    Derived symbols must go through _flatten_into_self instead.
    """
    new = list(sym_node)
    if len(new) > 1:
        new[1] = full_name
    return new


def _walk_to_root(
    start_node: list,
    table: Dict[str, str],
    start_lib_name: str,
) -> Tuple[list, List[list]]:
    """Walk the (extends ...) chain from start_node up to the topmost
    ancestor that has no extends clause.

    Returns (root_node, override_chain). override_chain lists the
    intermediate symbols in walking order (start_node first, root excluded),
    so callers can apply property overrides bottom-up: the child wins on
    conflict (closest-to-leaf overrides win).

    If a parent name can't be found in the library, the chain breaks early
    and we return whatever we have — the caller flattens what's available.
    """
    # Cycle protection: dual-key. id() catches re-entering the same cached
    # Python object; bare-name catches the case where fuzzy fallback returns
    # a freshly-parsed copy of an already-walked symbol. Max depth caps
    # pathological-but-acyclic chains (no real KiCad library has >32 levels).
    chain: List[list] = []
    node = start_node
    visited_ids: set = set()
    visited_names: set = set()
    MAX_EXTENDS_DEPTH = 32
    for _ in range(MAX_EXTENDS_DEPTH):
        ident = id(node)
        bare = _to_str(node[1]) if isinstance(node, list) and len(node) > 1 else ""
        if ident in visited_ids or (bare and bare in visited_names):
            return (node, chain)  # cycle detected — caller flattens what we have
        visited_ids.add(ident)
        if bare:
            visited_names.add(bare)
        parent_bare = _symbol_extends(node)
        if not parent_bare:
            return (node, chain)
        parent_uri = table.get(start_lib_name)
        parent_node = (
            _find_symbol_in_library(parent_uri, parent_bare) if parent_uri else None
        )
        if parent_node is None:
            fuzzy = _fuzzy_find_anywhere(table, f"{start_lib_name}:{parent_bare}")
            if fuzzy:
                _, _, parent_node, _ = fuzzy
        if parent_node is None:
            return (node, chain)  # broken chain — best effort
        chain.append(node)
        node = parent_node
    return (node, chain)  # depth-capped — degenerate library, give up


def _flatten_into_self(
    root_node: list,
    overrides: List[list],
    target_bare_name: str,
) -> list:
    """Build a self-contained symbol node from root_node's body plus
    property overrides from overrides (closest-to-leaf wins).

    This is the Python equivalent of LIB_SYMBOL::Flatten() in eeschema.
    KiCad's .kicad_sch lib_symbols block does NOT resolve (extends "...") —
    every cached entry must carry its own body. Caching a derived child
    with (extends) leaves m_parent unlinked and the canvas shows only the
    placeholder. Flattening at write time avoids that whole class.

    target_bare_name is the bare (no library prefix) name the result will
    be addressed by. Sub-unit child symbols (e.g. "AP1117-15_0_1") are
    renamed to start with target_bare_name so KiCad's parse-time prefix
    check at sch_io_kicad_sexpr_parser.cpp:501 passes.
    """
    import copy

    out = copy.deepcopy(root_node)
    if not isinstance(out, list) or len(out) < 2:
        return out

    # Remember the root's bare name so we can rewrite sub-unit prefixes.
    root_bare = _to_str(out[1])
    if ":" in root_bare:
        root_bare = root_bare.split(":", 1)[1]

    # Set the outer symbol's name to target_bare_name. The caller wraps
    # this with the library prefix when inserting into lib_block.
    out[1] = target_bare_name

    # Strip any (extends) clause the root happens to carry (defence — a
    # true root has none).
    out = [
        item for i, item in enumerate(out)
        if not (i > 0 and isinstance(item, list) and len(item) > 0
                and _to_str(item[0]) == "extends")
    ]

    # Rename sub-unit child symbols so their name prefix matches the new
    # outer name (KiCad's parser enforces this).
    new_out: List[Any] = []
    for item in out:
        if (isinstance(item, list) and len(item) > 1
                and _to_str(item[0]) == "symbol"):
            subname = _to_str(item[1])
            if subname.startswith(root_bare + "_"):
                renamed = list(item)
                renamed[1] = target_bare_name + subname[len(root_bare):]
                new_out.append(renamed)
                continue
        new_out.append(item)
    out = new_out

    # Apply property overrides from each level of the extends chain,
    # closest-to-root first so that the leaf (overrides[0]) wins on
    # conflict.
    for override in reversed(overrides):
        if not isinstance(override, list):
            continue
        override_props: Dict[str, list] = {}
        for item in override[1:]:
            if (isinstance(item, list) and len(item) > 1
                    and _to_str(item[0]) == "property"):
                override_props[_to_str(item[1])] = item

        replaced: List[Any] = []
        last_prop_idx = -1
        for item in out:
            if (isinstance(item, list) and len(item) > 1
                    and _to_str(item[0]) == "property"):
                pname = _to_str(item[1])
                if pname in override_props:
                    replaced.append(override_props.pop(pname))
                else:
                    replaced.append(item)
                last_prop_idx = len(replaced) - 1
            else:
                replaced.append(item)

        # Append any properties the leaf had that the root didn't define.
        insert_at = last_prop_idx + 1 if last_prop_idx >= 0 else len(replaced)
        for prop in override_props.values():
            replaced.insert(insert_at, prop)
            insert_at += 1
        out = replaced

    return out


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
    # KIPRJMOD in KiCad always means the open project's directory; mirror that
    # so project-local tables (and user-level tables that also use ${KIPRJMOD})
    # resolve the same way KiCad would.
    table = _parse_sym_lib_table(table_path, kiprjmod=project_dir) if table_path else {}

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
        """Insert node into lib_block under full_name (Lib:Bare form).

        Derived symbols (those with `(extends "Parent")`) are FLATTENED:
        the parent chain is walked to its root, the root's body is copied
        into a fresh entry, and the chain's property overrides are layered
        on (closest-to-leaf wins). The result has no extends clause and is
        fully self-contained — required because KiCad's .kicad_sch parser
        does NOT resolve extends at load time (see
        sch_io_kicad_sexpr_parser.cpp:2858 comment "No derived symbols are
        allowed in the library cache").

        Root symbols (no extends) are appended as-is with their outer name
        rewritten to the prefixed form.
        """
        if full_name in already_cached:
            return
        if ":" not in full_name:
            lib_block.append(_rename_symbol(node, full_name))
            already_cached.add(full_name)
            return
        lib_prefix, bare = full_name.split(":", 1)
        parent_bare_name = _symbol_extends(node)
        if not parent_bare_name:
            lib_block.append(_rename_symbol(node, full_name))
            already_cached.add(full_name)
            return
        root_node, override_chain = _walk_to_root(node, table, lib_prefix)
        if _symbol_extends(root_node):
            # Chain broken: parent name didn't resolve in the library.
            # Best-effort: emit derived-as-is so the user at least sees the
            # placeholder + warning, instead of a silent empty schematic.
            lib_block.append(_rename_symbol(node, full_name))
            already_cached.add(full_name)
            return
        flat = _flatten_into_self(root_node, override_chain, bare)
        flat[1] = full_name  # outer name = "Lib:Bare", matching .kicad_sch convention
        lib_block.append(flat)
        already_cached.add(full_name)

    resolution: Dict[str, Dict[str, str]] = {}
    for raw_lib_id in lib_ids:
        if raw_lib_id in resolution:
            continue
        if ":" not in raw_lib_id:
            resolution[raw_lib_id] = {"resolved": raw_lib_id, "status": "missing"}
            continue
        # Consult the rename map FIRST. K8/K9 short names (Device:CP) become
        # their K10 canonical (Device:C_Polarized); aliased hits then resolve
        # as 'exact', not 'fuzzy'. Unknown lib_ids pass through untouched.
        effective_lib_id = _alias_lib_id(raw_lib_id)
        lib_name, sym_name = effective_lib_id.split(":", 1)

        # 1. Try exact lookup in the requested library.
        node: Optional[list] = None
        status = "missing"
        resolved_lib = lib_name
        resolved_sym = sym_name
        alternates: List[str] = []
        uri = table.get(lib_name)
        if uri:
            node = _find_symbol_in_library(uri, sym_name)
            if node is not None:
                status = "exact"

        # 2. Try stripping a trailing package suffix and look up the shorter
        # name. Covers 'ATmega328P-AU' -> 'ATmega328P-A' / 'ATmega328P' style
        # mismatches where the prompt names a specific package variant the
        # standard lib doesn't carry by that exact suffix.
        if node is None:
            normalized = _try_normalize_lib_id(table, effective_lib_id)
            if normalized is not None:
                resolved_lib, resolved_sym, node = normalized
                status = "normalized"

        # 2b. Try cross-library family rename (e.g. Connector_PinHeader_2.54mm:
        # PinHeader_2x03_* → Connector_Generic:Conn_02x03_Odd_Even). Pure
        # regex rewrite — no part numbers, no manual aliases.
        if node is None:
            renamed = _try_family_rename(table, effective_lib_id)
            if renamed is not None:
                resolved_lib, resolved_sym, node = renamed
                status = "family_renamed"

        # 3. Fall back to fuzzy lookup across all libraries. Pass the aliased
        # lib_id so any rename (CP -> C_Polarized) is what fuzzy ranks against.
        if node is None:
            fuzzy = _fuzzy_find_anywhere(table, effective_lib_id)
            if fuzzy:
                resolved_lib, resolved_sym, node, alternates = fuzzy
                status = "fuzzy"

        # 4. Capability/token match — same library namespace, same package
        # class, same description tokens. Catches "CP2102N-A01-GQFN28" →
        # "Interface_UART:CP2102N" or similar across-library kin when the
        # name-only fuzzy missed.
        confidence = None
        if node is None:
            target_pin_n = _estimate_pin_count(effective_lib_id)
            cap = _capability_find_anywhere(table, effective_lib_id, target_pin_n)
            if cap is not None:
                resolved_lib, resolved_sym, node, score, cap_alts = cap
                status = "capability"
                confidence = round(float(score), 3)
                if cap_alts:
                    alternates = cap_alts

        # 5. Pin-compatible fallback — any symbol with the same pin count
        # and a generic-IC-looking library namespace. Lower confidence,
        # but keeps the topology alive.
        if node is None:
            target_pin_n = _estimate_pin_count(effective_lib_id)
            if target_pin_n:
                compat = _pin_compat_find_anywhere(table, target_pin_n)
                if compat is not None:
                    resolved_lib, resolved_sym, node = compat
                    status = "pin_compat"
                    confidence = 0.45

        # 6. Generic placeholder — synthesize Generic_<N>pin in
        # envil_generated. Last resort; the symbol body is a plain
        # rectangle but it has the right pin count so labels around it
        # have anchors.
        if node is None:
            target_pin_n = _estimate_pin_count(effective_lib_id)
            if target_pin_n:
                gen = _ensure_generic_placeholder_in_envil_lib(target_pin_n)
                if gen is not None:
                    resolved_lib, resolved_sym, node = gen
                    status = "generic_placeholder"
                    confidence = 0.20

        resolved = f"{resolved_lib}:{resolved_sym}"
        entry: Dict[str, Any] = {"resolved": resolved, "status": status}
<<<<<<< Updated upstream
        if confidence is not None:
            entry["confidence"] = confidence
=======
>>>>>>> Stashed changes
        if alternates:
            # Surface tied candidates so the caller can ask the user to pick.
            # Listed exactly as "Library:Symbol" matching the input format.
            entry["alternates"] = alternates
        resolution[raw_lib_id] = entry

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
