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

import os
import shutil
from functools import lru_cache
from pathlib import Path


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
    """The bundled symbol library. Override with ``$KICAD_SYMBOL_DIR``."""
    override = os.environ.get("KICAD_SYMBOL_DIR", "").strip()
    if override:
        # $KICAD_SYMBOL_DIR may be os.pathsep-separated; take the first entry.
        first = next((p for p in override.split(os.pathsep) if p.strip()), "")
        if first:
            return Path(first).expanduser().resolve()
    return envil_home() / "kicad-sym-lib"


def fp_lib_dir() -> Path:
    """The bundled footprint library root."""
    return envil_home() / "kicad-fp-lib"


def fp_lib_table() -> Path:
    """The fp-lib-table that maps footprint nicknames to .pretty dirs."""
    return envil_home() / "fp-lib-table"


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
    for ver in ("9.0", "8.0", "7.0"):
        cand = Path(f"C:/Program Files/KiCad/{ver}/bin/kicad-cli.exe")
        if cand.exists():
            return str(cand)
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
