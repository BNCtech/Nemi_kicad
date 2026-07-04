"""Central path resolution for Envil — the ONE place that knows where things are.

Rule: NO module hardcodes an absolute path. Everything derives from the project
root, which is auto-detected from THIS file's location, so the code runs on any
machine / any drive without edits. Every path can still be overridden by an env
var for special setups (CI, cloud, a teammate's custom layout).

Assumed layout (all cloned under one root, as on the dev machine):

    <ENVIL_HOME>/
        ai_backend/envil_agent/settings.py   <- this file
        kicad-sym-lib/
        kicad-fp-lib/
        fp-lib-table
        kicad-source-mirror/   (optional; provides kicad-cli.exe when built)

On the original dev machine ENVIL_HOME auto-resolves to ``F:/Ki_CAD`` (because
that is literally where this file lives), so behavior is identical to the old
hardcoded paths — this change is non-breaking here and portable everywhere else.
"""
from __future__ import annotations

import json
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Optional


@lru_cache(maxsize=1)
def envil_home() -> Path:
    """The project root. ``$ENVIL_HOME`` if set, else auto-detected as the folder
    that contains ``ai_backend`` (this file is <root>/ai_backend/envil_agent/...)."""
    override = os.environ.get("ENVIL_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    # settings.py -> envil_agent -> ai_backend -> <root>
    return Path(__file__).resolve().parents[2]


def out_dir() -> Path:
    """Where generated projects are written.

    Per-user by default: ``<home>/Documents/Anvil`` (e.g.
    ``C:/Users/<name>/Documents/Anvil`` on Windows). This is the ONE place a
    project should land — NOT inside the app/backend install tree. The old
    default (``<install>/_envil_out``) put every user's projects inside the
    shared program folder, which breaks the moment two people use the same
    install (and pollutes the source tree). Documents is writable, per-account,
    and the same convention KiCad itself uses for new projects.

    Override priority:
      1. ``$ENVIL_OUT_DIR``      — explicit, wins over everything.
      2. ``<home>/Documents/Anvil`` — the per-user default.
      3. ``<home>/Anvil``        — fallback if Documents doesn't exist
                                    (rare; some headless / non-standard homes).
    """
    override = os.environ.get("ENVIL_OUT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    docs = home / "Documents"
    base = docs if docs.is_dir() else home
    return (base / "Anvil").resolve()


def sym_lib_dir() -> Path:
    """Where newly-created symbols are written. Priority: ``$KICAD_SYMBOL_DIR``,
    then a folder this user previously told the AI about, then the bundled lib."""
    override = os.environ.get("KICAD_SYMBOL_DIR", "").strip()
    if override:
        # $KICAD_SYMBOL_DIR may be os.pathsep-separated; take the first entry.
        first = next((p for p in override.split(os.pathsep) if p.strip()), "")
        if first:
            return Path(first).expanduser().resolve()
    told = user_library_override("symbol_dir")
    if told:
        return told
    return envil_home() / "kicad-sym-lib"


def fp_lib_dir() -> Path:
    """Where newly-created footprints are written. Priority:
    ``$KICAD_FOOTPRINT_DIR``, then a folder this user previously told the AI
    about, then the bundled lib — mirrors ``sym_lib_dir()``."""
    override = os.environ.get("KICAD_FOOTPRINT_DIR", "").strip()
    if override:
        first = next((p for p in override.split(os.pathsep) if p.strip()), "")
        if first:
            return Path(first).expanduser().resolve()
    told = user_library_override("footprint_dir")
    if told:
        return told
    return envil_home() / "kicad-fp-lib"


def fp_lib_table() -> Path:
    """The fp-lib-table that maps footprint nicknames to .pretty dirs."""
    return envil_home() / "fp-lib-table"


def user_library_config_path() -> Path:
    """Where a user's own answer to 'where is your KiCad library' is
    remembered, once the AI has asked and they've told it. Per-user (keyed by
    user_id, mirrors intent/user_rules.py's overlay convention) and separate
    from .env: .env ships with the app and is the same for everyone, this
    file is written at runtime and is specific to one person's machine."""
    return Path(__file__).resolve().parent / "config" / "user_library_paths.json"


def _load_user_library_config() -> dict:
    try:
        return json.loads(user_library_config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def user_library_override(kind: str, user_id: str = "default") -> Optional[Path]:
    """``kind`` is 'symbol_dir' or 'footprint_dir'. Returns the path the user
    told the AI about, if any, else None (falls through to auto-discovery)."""
    entry = _load_user_library_config().get(user_id) or {}
    p = entry.get(kind, "")
    return Path(p).expanduser().resolve() if p else None


def set_user_library_override(kind: str, path: str, user_id: str = "default") -> Path:
    """Persist the user's answer so it takes effect immediately and survives
    a restart, without touching the shared .env."""
    cfg_path = user_library_config_path()
    data = _load_user_library_config()
    data.setdefault(user_id, {})[kind] = str(Path(path).expanduser().resolve())
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return cfg_path


@lru_cache(maxsize=1)
def kicad_cli() -> str:
    """Locate kicad-cli.exe without hardcoding: env override, then the built fork,
    then PATH, then a system KiCad install, finally the bare name."""
    override = os.environ.get("KICAD_CLI", "").strip()
    if override and Path(override).exists():
        return override
    built = (envil_home() / "kicad-source-mirror" / "build" / "install"
             / "msvc-win64-release" / "bin" / "kicad-cli.exe")
    if built.exists():
        return str(built)
    found = shutil.which("kicad-cli")
    if found:
        return found
    for ver in ("10.0", "9.0", "8.0", "7.0"):
        cand = Path(f"C:/Program Files/KiCad/{ver}/bin/kicad-cli.exe")
        if cand.exists():
            return str(cand)
    # Per-user, no-admin installs (envil_user.nsi — the VS Code/Chrome
    # convention) land under %LocalAppData%\Programs\<any name>\bin, never
    # under Program Files. Glob by shape, not by a fixed app name, so any
    # such install (Envil CAD or stock KiCad) is found without an edit here.
    local = os.environ.get("LocalAppData", "").strip()
    if local:
        try:
            for cand in Path(local).glob("Programs/*/bin/kicad-cli.exe"):
                if cand.is_file():
                    return str(cand)
        except OSError:
            pass
    return "kicad-cli"


def resolve_tokens(value):
    """Rebase config values onto the real root, recursively.

    Replaces the ``${ENVIL_HOME}`` token AND the legacy literal ``F:/Ki_CAD`` with
    the actual ``envil_home()``. On the dev machine this is a no-op (same path);
    elsewhere it makes config/layout_config.json portable without editing it."""
    home = str(envil_home()).replace("\\", "/")
    if isinstance(value, str):
        return value.replace("${ENVIL_HOME}", home).replace("F:/Ki_CAD", home)
    if isinstance(value, dict):
        return {k: resolve_tokens(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_tokens(v) for v in value]
    return value
