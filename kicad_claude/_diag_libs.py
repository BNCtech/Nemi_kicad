"""One-shot diagnostic: why is the orchestrator saying 'lib_id not found' for
stock KiCad libraries on this machine?

Usage (from the orchestrator's project root, same shell that launches server.py):

    python -m ai_backend.kicad_claude._diag_libs <path-to-test_circuit.kicad_sch>

If no path is given, it diagnoses without project context (user-level table only).
Prints, in order:

  1. Which sym-lib-table the discovery picks up
  2. Whether that table has a row for Device / power / Regulator_Linear
  3. For each of those rows, the expanded URI and whether the path exists on disk
  4. For Device:R specifically, whether `R` resolves end-to-end

This is read-only — it touches no files."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from ai_backend.kicad_claude._lib_symbol_cache import (
    _discover_user_sym_lib_table,
    _parse_sym_lib_table,
    _expand_uri,
)


def _hr(title: str) -> None:
    print(f"\n=== {title} ===")


def main(argv: list[str]) -> int:
    sch_arg: Optional[Path] = Path(argv[1]).resolve() if len(argv) > 1 else None
    project_dir = sch_arg.parent if sch_arg else None

    _hr("Inputs")
    print(f"schematic arg : {sch_arg}")
    print(f"project_dir   : {project_dir}")
    print(f"cwd           : {Path.cwd()}")
    print(f"APPDATA       : {os.environ.get('APPDATA')}")
    for var in ("KICAD9_SYMBOL_DIR", "KICAD10_SYMBOL_DIR", "KICAD_USER_SYMBOL_DIR"):
        print(f"{var:14}: {os.environ.get(var, '(unset)')}")

    _hr("Discovery")
    table_path = _discover_user_sym_lib_table(project_dir)
    print(f"discovered    : {table_path}")
    if table_path is None:
        print("FATAL: no sym-lib-table discovered. KiCad has never been run on this PC,")
        print("       or APPDATA points somewhere unexpected.")
        return 2

    # Also report what the project-local file would look like even if discovery
    # took the global one — helps catch the "I forgot there's a stray
    # sym-lib-table next to my schematic" trap.
    if project_dir:
        local = project_dir / "sym-lib-table"
        print(f"project-local : {local} (exists: {local.is_file()})")

    _hr("Parsed entries (subset)")
    table = _parse_sym_lib_table(table_path, kiprjmod=project_dir)
    print(f"total libs    : {len(table)}")
    interesting = ["Device", "power", "Regulator_Linear", "envil_generated"]
    for name in interesting:
        uri = table.get(name)
        if uri is None:
            print(f"  {name:20} : NOT IN TABLE")
            continue
        expanded = _expand_uri(uri, kiprjmod=project_dir)
        exists = Path(expanded).exists()
        print(f"  {name:20} : {uri}")
        print(f"  {'':20}   expanded -> {expanded}   exists={exists}")

    _hr("End-to-end resolution for Device:R")
    try:
        from ai_backend.kicad_claude._lib_symbol_cache import resolve_lib_id
        result = resolve_lib_id("Device:R", project_dir=project_dir)
        print(f"resolve_lib_id('Device:R') -> {result}")
    except ImportError:
        # The resolve helper has a different name in some versions; fall back to
        # the same call ensure_lib_symbols_for_doc uses.
        from ai_backend.kicad_claude._lib_symbol_cache import ensure_lib_symbols_for_doc
        # Build a minimal sexp doc with a (lib_symbols) block so the call works.
        import sexpdata
        doc = sexpdata.loads('(kicad_sch (lib_symbols))')
        res = ensure_lib_symbols_for_doc(doc, ["Device:R"], project_dir=project_dir)
        print(f"ensure_lib_symbols_for_doc -> {res}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
