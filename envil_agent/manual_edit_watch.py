"""Detect the user's MANUAL KiCad edits between AI turns (Cursor-parity).

Why this exists
---------------
The AI reads the real ``.kicad_sch`` / ``.kicad_pcb`` files from disk each turn.
A user's manual edits in KiCad normally live only in the editor's memory until an
explicit Ctrl+S, so the AI never sees them. With the fork's ``EnvilAutoSaveRealFile``
autosave those manual edits are flushed to the real file on a short timer — but the
AI still only *re-reads* a file when it decides to. This module closes the loop the
way Cursor does: it notices that a project file changed **since the AI last acted**
and tells the AI, so the AI re-reads before doing anything.

How it stays correct (user edits vs the AI's own writes)
--------------------------------------------------------
Between two consecutive AI turns the ONLY actor touching the files is the user — the
AI writes files only *during* a turn. So:

  * at the END of each turn we ``snapshot`` the files (this captures the AI's own
    writes as the new "AI-known" baseline), and
  * at the START of the next turn we ``detect`` — any file whose content differs from
    that baseline was changed by the user.

This is the exact analogue of VSCode's ``TextDocumentChangeReason.userInput`` (telling
user edits apart from programmatic ones): our baseline *is* the AI's last-known state,
so a mismatch is definitionally a human edit.

Pure read. No writes, no network. Gated by ``unified_chat.manual_edit_watch`` (default
True); disabled -> ``detect`` always returns "" and behaviour is unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# session_id -> { absolute_path : file_text_at_last_turn_end }
_SNAPSHOTS: Dict[str, Dict[str, str]] = {}

# Files whose manual change is a real design decision the AI must notice:
#   .kicad_sch/.kicad_pcb — the schematic / board (part moves, adds, deletes)
#   .kicad_dru            — user-authored custom design rules (Board Setup)
#   .kicad_pro            — board setup + net classes (clearances, track/via sizes)
#   sym-lib-table / fp-lib-table — project-local library tables (libs added/removed)
# .kicad_prl is intentionally EXCLUDED: it is window/selection state, not design.
_DESIGN_SUFFIXES = (".kicad_sch", ".kicad_pcb", ".kicad_dru", ".kicad_pro")
_DESIGN_NAMES = ("sym-lib-table", "fp-lib-table")


def _enabled() -> bool:
    try:
        from .intent.engine import _load_layout_config as _lc
        cfg = (_lc().get("unified_chat", {}) or {})
        return bool(cfg.get("manual_edit_watch", True))
    except Exception:
        return True


def _design_files(paths: List[Optional[str]]) -> List[Path]:
    """Every real design file in the project folder(s) implied by the active
    paths. Derived from the parent dir so an edit to ANY project file is seen,
    not just the one currently open."""
    out: Dict[str, Path] = {}
    for p in paths:
        if not p:
            continue
        try:
            parent = Path(p).parent
        except Exception:
            continue
        if not parent.exists():
            continue
        try:
            for entry in parent.iterdir():
                if not entry.is_file():
                    continue
                name = entry.name
                if (name.startswith("_autosave") or name.endswith("-bak")
                        or name.endswith(".lck") or "~" in name):
                    continue
                if entry.suffix.lower() in _DESIGN_SUFFIXES or name in _DESIGN_NAMES:
                    out[str(entry)] = entry
        except OSError:
            continue
    return list(out.values())


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


# ---- part extraction (best-effort) --------------------------------------

_FP_BLOCK = re.compile(r"\(footprint\b", re.IGNORECASE)
_SYM_BLOCK = re.compile(r"\(symbol\b", re.IGNORECASE)
_AT = re.compile(r"\(at\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")
_REF_PROP = re.compile(r'\(property\s+"Reference"\s+"([^"]+)"', re.IGNORECASE)


def _split_blocks(text: str, opener: re.Pattern) -> List[str]:
    """Return the substring of each top-level ``(footprint|symbol ...)`` block by
    walking parentheses from each opener. Best-effort; never raises."""
    blocks: List[str] = []
    for m in opener.finditer(text):
        i = m.start()
        depth = 0
        j = i
        n = len(text)
        while j < n:
            c = text[j]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(text[i:j + 1])
                    break
            j += 1
    return blocks


def _extract_parts(text: str, suffix: str) -> Dict[str, Tuple[float, float]]:
    """{reference: (x, y)} for each placed part. The block's FIRST ``(at ...)``
    is the part placement; the Reference property names it. Unref'd parts are
    keyed by index so a positional change is still detected."""
    opener = _FP_BLOCK if suffix == ".kicad_pcb" else _SYM_BLOCK
    parts: Dict[str, Tuple[float, float]] = {}
    try:
        for idx, blk in enumerate(_split_blocks(text, opener)):
            at = _AT.search(blk)
            if not at:
                continue
            pos = (round(float(at.group(1)), 3), round(float(at.group(2)), 3))
            rm = _REF_PROP.search(blk)
            ref = rm.group(1) if rm else f"#{idx}"
            parts[ref] = pos
    except Exception:
        return {}
    return parts


_RULE_NAME = re.compile(r'\(rule\s+"([^"]+)"', re.IGNORECASE)
_LIB_NAME = re.compile(r'\(lib\s+\(name\s+"?([^")\s]+)"?', re.IGNORECASE)


def _set_diff(old_items, new_items, noun: str) -> str:
    """added/removed/modified phrasing for a set of named things."""
    o, n = set(old_items), set(new_items)
    frags: List[str] = []
    if n - o:
        frags.append(f"added {noun} " + ", ".join(sorted(n - o)[:8]))
    if o - n:
        frags.append(f"removed {noun} " + ", ".join(sorted(o - n)[:8]))
    return "; ".join(frags)


def _diff_parts(old: str, new: str, suffix: str) -> str:
    op = _extract_parts(old, suffix)
    np_ = _extract_parts(new, suffix)
    added = [r for r in np_ if r not in op and not r.startswith("#")]
    removed = [r for r in op if r not in np_ and not r.startswith("#")]
    moved = [r for r in np_ if r in op and np_[r] != op[r] and not r.startswith("#")]
    frags: List[str] = []
    if added:
        frags.append("added " + ", ".join(sorted(added)[:8]))
    if removed:
        frags.append("removed " + ", ".join(sorted(removed)[:8]))
    if moved:
        ex = moved[0]
        frags.append(f"moved {len(moved)} part(s) (e.g. {ex}: "
                     f"{op[ex][0]},{op[ex][1]} -> {np_[ex][0]},{np_[ex][1]})")
    return "; ".join(frags)


def _diff_dru(old: str, new: str) -> str:
    """Custom design rules (Board Setup). Report which named rules the user
    added/removed; if the set is identical but the text differs, they tuned an
    existing rule."""
    s = _set_diff(_RULE_NAME.findall(old), _RULE_NAME.findall(new), "design rule")
    if s:
        return s
    return "edited an existing design rule (constraints changed) — re-read the .kicad_dru"


def _diff_pro(old: str, new: str) -> str:
    """Project settings JSON: net classes + board setup live here. Report which
    top-level sections changed (net_settings = net classes/clearances, board =
    DRC/track/via defaults) rather than a noisy full diff."""
    try:
        import json
        oj, nj = json.loads(old), json.loads(new)
    except Exception:
        return "board setup / project settings changed — re-read the .kicad_pro"
    watch = {"net_settings": "net classes / clearances",
             "board": "board setup (DRC, track/via defaults)",
             "libraries": "project library list"}
    changed = [label for key, label in watch.items() if oj.get(key) != nj.get(key)]
    if changed:
        return "changed " + "; ".join(changed)
    return "project settings changed — re-read the .kicad_pro"


def _diff_libtable(old: str, new: str) -> str:
    """sym-lib-table / fp-lib-table: report libraries added/removed."""
    s = _set_diff(_LIB_NAME.findall(old), _LIB_NAME.findall(new), "library")
    return s or "a library entry changed (path/options) — re-read the lib table"


def _diff_summary(old: str, new: str, name: str) -> str:
    """A short, human description of what the user changed, dispatched by file
    type. Falls back to a coarse size delta if a specific parser yields nothing."""
    low = name.lower()
    try:
        if low.endswith((".kicad_sch", ".kicad_pcb")):
            r = _diff_parts(old, new, ".kicad_pcb" if low.endswith(".kicad_pcb")
                            else ".kicad_sch")
        elif low.endswith(".kicad_dru"):
            r = _diff_dru(old, new)
        elif low.endswith(".kicad_pro"):
            r = _diff_pro(old, new)
        elif name in _DESIGN_NAMES:
            r = _diff_libtable(old, new)
        else:
            r = ""
    except Exception:
        r = ""
    if r:
        return r
    d = abs(len(new) - len(old))
    return f"content changed (~{d} chars) — re-read the file to see what"


# ---- public API ---------------------------------------------------------

def snapshot(session_id: str, paths: List[Optional[str]]) -> None:
    """Record the current on-disk content of every project design file as the
    AI-known baseline for this session. Call at the END of a turn (after the AI
    has written whatever it was going to write)."""
    if not _enabled() or not session_id:
        return
    store: Dict[str, str] = {}
    for f in _design_files(paths):
        txt = _read(f)
        if txt is not None:
            store[str(f)] = txt
    if store:
        _SNAPSHOTS[session_id] = store


def detect(session_id: str, paths: List[Optional[str]]) -> str:
    """Compare the current on-disk files to this session's baseline. Returns a
    ready-to-inject context note describing the user's manual edits since the AI
    last acted, or "" if nothing changed (or on the first turn, or disabled).

    Does NOT update the baseline — the caller re-snapshots at turn end."""
    if not _enabled() or not session_id:
        return ""
    base = _SNAPSHOTS.get(session_id)
    if not base:
        return ""   # first turn this session — nothing to compare against yet

    changes: List[str] = []
    for f in _design_files(paths):
        key = str(f)
        new = _read(f)
        if new is None:
            continue
        old = base.get(key)
        if old is None:
            changes.append(f"- `{f.name}`: new file appeared since your last turn")
        elif old != new:
            changes.append(f"- `{f.name}`: {_diff_summary(old, new, f.name)}")

    if not changes:
        return ""

    return (
        "[MANUAL EDIT DETECTED] The user changed the project in KiCad by hand "
        "since your last turn (this is not your own edit). Treat the file on disk "
        "as the source of truth: RE-READ the affected file(s) with your tools "
        "before acting, and take these changes into account:\n"
        + "\n".join(changes)
        + "\n"
    )


def forget(session_id: str) -> None:
    """Drop a session's baseline (e.g. on New Chat / session rotation)."""
    _SNAPSHOTS.pop(session_id, None)
