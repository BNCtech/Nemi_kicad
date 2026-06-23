"""Verify the Cursor-style project-naming path (2026-06-21).

Deterministic, no LLM: loads the known-good ne555 golden fixture IR
(ir.name == "ne555") and drives the REAL render_node three ways:

  1. project_name set            -> folder + .kicad_sch basename follow it
  2. no project_name (control)   -> unchanged, still "ne555" (byte-stable)
  3. out_path + project_name     -> project_name IGNORED, file keeps out_path
                                    name (overwriting an open file wins)

Run:  python tests/_verify_project_naming.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))            # ai_backend on sys.path

from envil_agent.intent.ir import TopologyIR          # noqa: E402
from envil_agent.graphs.nodes.render import render_node  # noqa: E402

_FIXTURE = _HERE.parents[1] / "tests" / "golden" / "fixtures" / "ne555_blinker.json"


def _load_ir() -> TopologyIR:
    d = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return TopologyIR.from_dict(d["ir"])


def _basename(stats: dict) -> str:
    return Path(stats["path"]).name


def main() -> int:
    passed, failed = 0, 0

    def check(label: str, cond: bool, got: str) -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {label}  ({got})")
        else:
            failed += 1
            print(f"  FAIL  {label}  -> got {got!r}")

    # 1. project_name set -> file named after it
    with tempfile.TemporaryDirectory() as td:
        r = render_node({"ir": _load_ir(), "out_dir": td,
                         "project_name": "my_custom_blinker"})
        stats = r.get("stats") or {}
        bn = _basename(stats) if stats.get("path") else f"<error: {r.get('error')}>"
        check("project_name -> basename", bn == "my_custom_blinker.kicad_sch", bn)

    # 2. control: no project_name -> unchanged (ir.name 'ne555')
    with tempfile.TemporaryDirectory() as td:
        r = render_node({"ir": _load_ir(), "out_dir": td})
        stats = r.get("stats") or {}
        bn = _basename(stats) if stats.get("path") else f"<error: {r.get('error')}>"
        check("no project_name -> unchanged 'ne555'", bn == "ne555.kicad_sch", bn)

    # 3. out_path wins: project_name must be ignored when overwriting a file
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "open_file.kicad_sch"
        r = render_node({"ir": _load_ir(), "out_dir": td,
                         "out_path": str(target),
                         "project_name": "should_be_ignored"})
        stats = r.get("stats") or {}
        bn = _basename(stats) if stats.get("path") else f"<error: {r.get('error')}>"
        check("out_path beats project_name", bn == "open_file.kicad_sch", bn)

    print(f"\n{passed}/{passed + failed} checks passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
