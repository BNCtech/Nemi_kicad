"""Tools for the "where is your KiCad library" conversation.

Envil auto-discovers a working symbol/footprint library on every machine
(bundled copy, the installed KiCad's own config, standard install paths —
see kicad.symbol_geom). This file covers the two things that stay outside
that automatic chain, because only the USER can answer them:

  check_kicad_library  — read-only: what did auto-discovery actually find on
                          THIS system, so the AI can tell the user plainly
                          instead of guessing.
  set_kicad_library_path — the user has told the AI where their KiCad
                          symbols/footprints live (or where new parts should
                          go); remember it (settings.user_library_override,
                          keyed per-user) so it works immediately and on every
                          future build without editing any file by hand.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool


def _txt(obj: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2, ensure_ascii=False)}]}


@tool(
    name="check_kicad_library",
    description=(
        "Check whether a usable KiCad symbol and footprint library was found "
        "on THIS user's system. Call this before telling a user their library "
        "is missing, and whenever a build/create fails on a library error. "
        "Returns which folders were searched, which ones actually have parts, "
        "and a short verdict. If nothing usable was found, ask the user in "
        "plain language where KiCad is installed on their computer (e.g. "
        "\"the folder where you installed KiCad, or where your custom parts "
        "are\") — never mention .env, environment variables, or file paths "
        "internal to this app. Once they answer, call set_kicad_library_path."
    ),
    input_schema={},
)
async def check_kicad_library(args: dict[str, Any]) -> dict[str, Any]:
    from ..kicad.symbol_geom import library_status
    from ..tools.create_symbol import _fp_roots

    sym = library_status()
    try:
        fp_roots = [str(p) for p in _fp_roots()]
    except Exception:
        fp_roots = []

    return _txt({
        "symbols_ok": sym.get("ok", False),
        "symbol_roots_found": sym.get("roots", []),
        "symbol_roots_searched": sym.get("searched", []),
        "footprint_roots_found": fp_roots,
        "verdict": (
            "A usable library was found — builds and part creation can proceed."
            if sym.get("ok") else
            "No usable symbol library found anywhere on this system. Ask the "
            "user where their KiCad symbols/footprints are, in plain language."
        ),
    })


@tool(
    name="set_kicad_library_path",
    description=(
        "Remember where THIS user's KiCad symbol or footprint library lives, "
        "after they've told you in plain language (e.g. \"my KiCad is in "
        "D:/KiCad\" or \"put new parts in C:/MyParts\"). Fields: kind "
        "('symbol' or 'footprint'), path (a folder — for 'symbol' it should "
        "contain .kicad_sym files or .kicad_symdir folders; for 'footprint' "
        "it should contain .pretty folders; a fresh empty folder is fine too, "
        "it will be created and used for new parts). Takes effect immediately "
        "— no restart needed. Call check_kicad_library first if unsure "
        "whether this is even necessary."
    ),
    input_schema={"kind": str, "path": str},
)
async def set_kicad_library_path(args: dict[str, Any]) -> dict[str, Any]:
    kind = str(args.get("kind", "") or "").strip().lower()
    path = str(args.get("path", "") or "").strip()
    if kind not in ("symbol", "footprint"):
        return _txt({"ok": False, "error": "kind must be 'symbol' or 'footprint'"})
    if not path:
        return _txt({"ok": False, "error": "path is required"})

    from .. import settings

    p = Path(path).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _txt({"ok": False, "error": f"can't create/access '{path}': {e}"})

    cfg_key = f"{kind}_dir"
    saved = settings.set_user_library_override(cfg_key, str(p))

    # Clear the symbol resolver's caches so this takes effect on the very
    # next tool call, matching create_symbol._refresh_caches().
    try:
        from ..kicad import symbol_geom as sg
        for fn in ("load_symbol", "_all_symbols", "resolve_lib_id_by_value",
                   "_discover_config_roots"):
            obj = getattr(sg, fn, None)
            if obj is not None and hasattr(obj, "cache_clear"):
                obj.cache_clear()
    except Exception:
        pass

    has_content = any(p.glob("*.kicad_sym")) or any(p.glob("*.kicad_symdir")) \
        if kind == "symbol" else any(p.glob("*.pretty"))

    return _txt({
        "ok": True,
        "kind": kind,
        "path": str(p),
        "remembered_at": str(saved),
        "existing_parts_found": bool(has_content),
        "note": (
            "This folder already has parts — they'll now show up to the AI."
            if has_content else
            "This folder is empty — new parts you create will be written "
            "here from now on."
        ),
    })
