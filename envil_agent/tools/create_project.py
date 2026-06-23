"""Tool: create_project — scaffold an EMPTY KiCad project (folder + files).

Cursor / Claude Code model: the WORKSPACE exists first. This tool creates the
project container — a folder plus a valid EMPTY ``.kicad_sch`` / ``.kicad_pro`` /
``.kicad_pcb`` — as a SEPARATE, EARLIER step than ``build_circuit``. It ALWAYS
succeeds (pure file writes, no architect call, no validation), so the user keeps
the project even if the later circuit generation has doubts or errors. The agent
then calls ``build_circuit`` with ``out_path`` set to the returned ``.kicad_sch``
so the circuit fills THIS project in place (same folder + name).

Because the result JSON carries a ``path`` ending in ``.kicad_sch``, the server's
existing auto-refresh path picks it up and broadcasts ``open_project`` — loading
the new (empty) project into the shell's Project Files tree, exactly like opening
a folder in Cursor.
"""
from __future__ import annotations

import json
import re
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict

from claude_agent_sdk import tool


_DEFAULT_OUT_DIR = "F:/Ki_CAD/_envil_out"


def _safe_name(name: str) -> str:
    """Same slug rule the render path uses (graphs/nodes/render.py) so the
    folder/file names match what build_circuit would pick."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "circuit"


def _write_empty_project(sch_path: Path, title: str) -> str:
    """Write a valid EMPTY ``.kicad_sch`` + sibling ``.kicad_pro`` (+ an empty
    ``.kicad_pcb``, emitted by ``_write_kicad_pro``). Reuses the engine's own
    emitters so the empty file matches what a real render produces (same header
    version, lib_symbols, sheet_instances). Returns the ``.kicad_pro`` path."""
    from ..intent.engine import (
        _emit_header,
        _emit_sheet_instances,
        _write_kicad_pro,
    )
    file_uuid = str(_uuid.uuid4())
    body = _emit_header(file_uuid, title=title, paper="A4")
    body += "\t(lib_symbols)\n"
    body += _emit_sheet_instances()
    body += ")\n"
    sch_path.parent.mkdir(parents=True, exist_ok=True)
    sch_path.write_text(body, encoding="utf-8")
    pro_path = _write_kicad_pro(sch_path, root_uuid=file_uuid)
    return str(pro_path)


@tool(
    name="create_project",
    description=(
        "Create an EMPTY KiCad project: the folder plus an empty .kicad_sch, "
        ".kicad_pro and .kicad_pcb. Call this FIRST — right after the user "
        "confirms a project name and BEFORE designing the circuit. The project "
        "is the workspace and must exist before its contents (Cursor / coding-"
        "agent style). It ALWAYS succeeds (no circuit logic), and the new "
        "project loads into the Project Files tree. AFTER it returns, ask the "
        "design questions, then call build_circuit with out_path set to the "
        ".kicad_sch this returns so the circuit fills THIS project.\n\n"
        "Args:\n"
        "  project_name: the project / folder name (required; a short slug, no "
        "spaces or extension, e.g. 'ne555_blinker')\n"
        "  out_dir: base directory (default F:/Ki_CAD/_envil_out). The project "
        "is created at <out_dir>/<project_name>/<project_name>.kicad_sch.\n\n"
        "Returns JSON: {status:'created'|'exists', path:<.kicad_sch>, "
        "project:<.kicad_pro>, dir:<folder>}. Pass `path` as build_circuit's "
        "out_path on the build step that follows."
    ),
    input_schema={"project_name": str, "out_dir": str},
)
async def create_project(args: Dict[str, Any]) -> Dict[str, Any]:
    name = (args.get("project_name") or "").strip()
    if not name:
        return {"content": [{"type": "text", "text": "ERROR: project_name required"}],
                "is_error": True}

    base = (args.get("out_dir") or "").strip() or _DEFAULT_OUT_DIR
    safe = _safe_name(name)
    proj_dir = Path(base) / safe
    sch_path = proj_dir / f"{safe}.kicad_sch"

    # Idempotent: NEVER clobber an existing project — it may already hold a
    # built circuit. Re-running just returns the existing paths so the flow is
    # safe to repeat (and build_circuit's out_path then fills it).
    if sch_path.exists():
        pro = sch_path.with_suffix(".kicad_pro")
        result = {
            "status": "exists",
            "path": str(sch_path).replace("\\", "/"),
            "project": str(pro).replace("\\", "/"),
            "dir": str(proj_dir).replace("\\", "/"),
            "note": f"Project '{safe}' already exists; kept as-is.",
        }
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    try:
        pro_path = _write_empty_project(sch_path, title=safe)
    except Exception as exc:  # noqa: BLE001 - report cleanly, never crash the turn
        return {"content": [{"type": "text",
                             "text": (f"ERROR: create_project failed: "
                                      f"{type(exc).__name__}: {exc}")}],
                "is_error": True}

    result = {
        "status": "created",
        "path": str(sch_path).replace("\\", "/"),
        "project": str(pro_path).replace("\\", "/"),
        "dir": str(proj_dir).replace("\\", "/"),
        "note": (f"Created empty project '{safe}'. Now ask the design questions, "
                 f"then call build_circuit with out_path={str(sch_path).replace(chr(92), '/')} "
                 f"to fill the circuit into this project."),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
