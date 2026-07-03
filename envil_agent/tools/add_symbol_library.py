"""Tool: add_symbol_library — register an existing .kicad_symdir (or a folder
containing one) into KiCad's sym-lib-table so it appears in the symbol chooser.

This is the AI equivalent of:
  Edit → Preferences → Manage Symbol Libraries → Add folder

No new symbols are created. The folder must already exist on disk.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool


# ---------------------------------------------------------------------------
# Helpers — reuse the same logic as create_symbol (don't duplicate)
# ---------------------------------------------------------------------------

def _find_sym_lib_tables() -> List[Path]:
    """KiCad global sym-lib-table files (one per installed version)."""
    out: List[Path] = []
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        base = Path(appdata) / "kicad"
        if base.exists():
            for ver in sorted(base.iterdir()):
                t = ver / "sym-lib-table"
                if t.is_file():
                    out.append(t)
    cfg = os.environ.get("KICAD_CONFIG_HOME")
    if cfg:
        t = Path(cfg) / "sym-lib-table"
        if t.is_file() and t not in out:
            out.append(t)
    return out


def _register_in_lib_tables(library: str, symdir: Path) -> List[str]:
    notes: List[str] = []
    tables = _find_sym_lib_tables()
    if not tables:
        return ["no KiCad sym-lib-table found — folder exists but not "
                "registered in the GUI library list"]
    for tbl in tables:
        try:
            text = tbl.read_text(encoding="utf-8")
        except OSError as exc:
            notes.append(f"could not read {tbl.parent.name}/sym-lib-table: {exc}")
            continue
        if re.search(r'\(lib\s+\(name\s+"' + re.escape(library) + r'"', text):
            notes.append(f"already registered in {tbl.parent.name}")
            continue
        # Try to reuse the path token already in the table (e.g. ${KICAD10_SYMBOL_DIR})
        m = re.search(r'\(uri\s+"([^"]*?)/[^"/]+\.kicad_symdir"', text)
        if m:
            uri = f"{m.group(1)}/{library}.kicad_symdir"
        else:
            uri = str(symdir).replace("\\", "/")
        entry = (f'\t(lib (name "{library}") (type "KiCad") '
                 f'(uri "{uri}") (options "") '
                 f'(descr ""))\n')
        idx = text.rstrip().rfind(")")
        if idx == -1:
            notes.append(f"malformed table {tbl.parent.name} — skipped")
            continue
        new_text = text[:idx] + entry + text[idx:]
        try:
            backup = tbl.with_name(tbl.name + ".envil-bak")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            tbl.write_text(new_text, encoding="utf-8")
            notes.append(f"registered in {tbl.parent.name}")
        except OSError as exc:
            notes.append(f"could not update {tbl.parent.name}: {exc}")
    return notes


def _register_in_project_lib_table(library: str, symdir: Path,
                                    project_path: str) -> List[str]:
    notes: List[str] = []
    p = Path(project_path).expanduser()
    if p.is_file():
        p = p.parent
    if not p.is_dir():
        return [f"project directory not found for {project_path!r}"]
    tbl = p / "sym-lib-table"
    if tbl.exists():
        try:
            text = tbl.read_text(encoding="utf-8")
        except OSError as exc:
            return [f"could not read project sym-lib-table: {exc}"]
    else:
        text = "(sym_lib_table\n)\n"
    if re.search(r'\(lib\s+\(name\s+"' + re.escape(library) + r'"', text):
        notes.append("already registered in project sym-lib-table")
        return notes
    uri = str(symdir).replace("\\", "/")
    entry = (f'\t(lib (name "{library}") (type "KiCad") '
             f'(uri "{uri}") (options "") '
             f'(descr ""))\n')
    idx = text.rstrip().rfind(")")
    if idx == -1:
        notes.append("malformed project table — skipped")
        return notes
    new_text = text[:idx] + entry + text[idx:]
    try:
        if tbl.exists():
            backup = tbl.with_name(tbl.name + ".envil-bak")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
        tbl.write_text(new_text, encoding="utf-8")
        notes.append(f"registered in project sym-lib-table ({tbl})")
    except OSError as exc:
        notes.append(f"could not update project sym-lib-table: {exc}")
    return notes


def _refresh_caches() -> None:
    try:
        from ..kicad import symbol_geom as sg
        for fn in ("load_symbol", "_all_symbols", "resolve_lib_id_by_value",
                   "_discover_config_roots"):
            obj = getattr(sg, fn, None)
            if obj is not None and hasattr(obj, "cache_clear"):
                obj.cache_clear()
    except Exception:
        pass


def _inject_root(root: Path) -> None:
    """Make the new library root visible to this session's resolver immediately,
    without needing a KiCad restart."""
    try:
        from ..kicad import symbol_geom as sg
        key = os.path.normcase(os.path.normpath(str(root)))
        already = any(os.path.normcase(os.path.normpath(e)) == key
                      for e in sg._project_sym_roots)
        if not already:
            sg._project_sym_roots.append(str(root))
        _refresh_caches()
    except Exception:
        pass


def _count_symbols(symdir: Path) -> int:
    try:
        return sum(1 for _ in symdir.glob("*.kicad_sym"))
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Tool entry
# ---------------------------------------------------------------------------

@tool(
    name="add_symbol_library",
    description=(
        "Register an EXISTING symbol library folder on disk into KiCad's "
        "sym-lib-table so it appears in the eeschema symbol chooser — "
        "the AI equivalent of Edit → Preferences → Manage Symbol Libraries → Add.\n\n"
        "No new symbols are created; the folder must already exist.\n\n"
        "IMPORTANT — ALWAYS ask the user BEFORE calling this tool:\n"
        "  'Do you want to add this library globally (visible to ALL KiCad "
        "projects on this machine) or only for a specific project?'\n"
        "Do NOT assume a default. Wait for the user's answer, then call this "
        "tool with scope='global' or scope='project'. If the user says "
        "'project', also ask for the project path if not already known.\n\n"
        "Args:\n"
        "  path: path to the library. Accepts:\n"
        "    • A .kicad_symdir folder  (e.g. C:/libs/MyParts.kicad_symdir)\n"
        "    • A parent folder that CONTAINS .kicad_symdir(s) — registers ALL of them\n"
        "    • A single .kicad_sym file — registers its parent .kicad_symdir\n"
        "  scope: REQUIRED — 'global' to register in KiCad's user-level "
        "sym-lib-table (all projects on this machine) or 'project' to register "
        "in one KiCad project's local table only. Must be provided explicitly — "
        "the tool will error if omitted.\n"
        "  library: nickname to use in the sym-lib-table "
        "(default: derived from the folder name).\n"
        "  project_path: path to the .kicad_pro file or project folder. "
        "Required when scope='project'.\n\n"
        "Returns: {registered: [...], already_registered: [...], errors: [...], "
        "symbol_counts: {...}}. KiCad must be restarted (or Preferences → "
        "Manage Symbol Libraries refreshed) for the new library to appear in "
        "the GUI chooser."
    ),
    input_schema={
        "path": str,
        "library": str,
        "scope": str,
        "project_path": str,
    },
)
async def add_symbol_library(args: Dict[str, Any]) -> Dict[str, Any]:
    def _err(msg: str) -> Dict[str, Any]:
        return {"content": [{"type": "text", "text": f"ERROR: {msg}"}],
                "is_error": True}

    raw_path = (args.get("path") or "").strip()
    if not raw_path:
        return _err("path is required")

    # Validate scope and project_path BEFORE touching the filesystem so the
    # error message is about the missing arg, not the path.
    scope = (args.get("scope") or "").strip().lower()
    if not scope:
        return _err(
            "scope is required — must be 'global' or 'project'. "
            "Ask the user: 'Should this library be added globally (available to "
            "all KiCad projects on this machine) or only for a specific project?'"
        )
    if scope not in ("global", "project"):
        return _err("scope must be 'global' or 'project'")
    project_path = (args.get("project_path") or "").strip()
    if scope == "project" and not project_path:
        return _err("scope='project' requires project_path")

    given = Path(raw_path).expanduser().resolve()
    if not given.exists():
        return _err(f"path does not exist: {given}")

    # --- Resolve which .kicad_symdir folders to register ---
    targets: List[Tuple[str, Path]] = []   # (nickname, symdir_path)

    user_nick = (args.get("library") or "").strip()
    user_nick = re.sub(r"[^A-Za-z0-9_+-]+", "_", user_nick).strip("_") if user_nick else ""

    if given.is_file() and given.suffix == ".kicad_sym":
        # Single file → register its parent .kicad_symdir
        symdir = given.parent
        nick = user_nick or (symdir.name[:-len(".kicad_symdir")]
                             if symdir.name.endswith(".kicad_symdir")
                             else symdir.name)
        targets.append((nick, symdir))

    elif given.name.endswith(".kicad_symdir"):
        # Explicit .kicad_symdir folder
        nick = user_nick or given.name[:-len(".kicad_symdir")]
        targets.append((nick, given))

    else:
        # Parent folder — look for .kicad_symdir sub-directories
        symdirs = sorted(given.glob("*.kicad_symdir"))
        if symdirs:
            for sd in symdirs:
                nick = sd.name[:-len(".kicad_symdir")]
                targets.append((nick, sd))
        else:
            # No .kicad_symdir found — treat the folder itself as the root
            nick = user_nick or given.name
            targets.append((nick, given))

    # Override nickname when registering a single library and user specified one
    if len(targets) == 1 and user_nick:
        targets = [(user_nick, targets[0][1])]

    registered: List[str] = []
    already: List[str] = []
    errors: List[str] = []
    sym_counts: Dict[str, int] = {}

    for nick, symdir in targets:
        if scope == "project":
            notes = _register_in_project_lib_table(nick, symdir, project_path)
        else:
            notes = _register_in_lib_tables(nick, symdir)

        cnt = _count_symbols(symdir)
        sym_counts[nick] = cnt

        for n in notes:
            if n.startswith("already"):
                already.append(f"{nick}: {n}")
            elif "could not" in n or "malformed" in n or "not found" in n:
                errors.append(f"{nick}: {n}")
            else:
                registered.append(f"{nick}: {n}")

        # Inject into this session's resolver immediately
        _inject_root(symdir.parent if symdir.name.endswith(".kicad_symdir")
                     else symdir)

    gui_registered = bool(registered)
    if registered:
        restart_note = ("RESTART KiCad (or Preferences → Manage Symbol Libraries → "
                        "Refresh) to see the new librar(ies) in the chooser.")
    elif already:
        restart_note = "All libraries were already registered — no changes made."
    else:
        restart_note = "Registration failed — check errors."

    result = {
        "registered": registered,
        "already_registered": already,
        "errors": errors,
        "symbol_counts": sym_counts,
        "scope": scope,
        "library_registered": gui_registered,
        "note": restart_note,
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
