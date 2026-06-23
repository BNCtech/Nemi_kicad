"""Tool: run Design Rules Check on a .kicad_pcb via kicad-cli.

Universal — works on any board, no per-circuit logic. Parallels
erc_check (which validates the schematic). Together they're the
two-stage validation before sending Gerbers to a fab.

Output: a `<basename>-drc.json` report next to the input PCB plus
a parsed summary returned to chat. All flags + timeout come from
`layout_config.json:drc_check` (per feedback_no_hardcode_json_config).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("drc_check", {}) or {}
    except Exception:
        return {}


def _kicad_cli(cfg: Dict[str, Any]) -> str:
    if cfg.get("kicad_cli_command"):
        return str(cfg["kicad_cli_command"])
    try:
        from ..intent.engine import _load_layout_config
        return str(_load_layout_config().get("erc_check", {}).get(
            "kicad_cli_command", "kicad-cli"))
    except Exception:
        return "kicad-cli"


def _summarise_report(report_path: Path) -> Dict[str, Any]:
    """Parse the JSON DRC report into a compact summary the agent can
    relay back. kicad-cli writes a structured `violations` array.

    Returns a `parse_error` field with the underlying reason whenever
    the file exists but can't be parsed (truncated write, locked file,
    schema drift across kicad-cli versions). Callers MUST check that
    field before trusting error_count / warning_count — those carry
    the sentinel value -1 only as a defensive default for backwards
    compatibility, real callers should branch on parse_error first."""
    try:
        with report_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as exc:
        return {"error_count": -1, "warning_count": -1, "issues": [],
                "parse_error": f"report missing: {exc}"}
    except json.JSONDecodeError as exc:
        return {"error_count": -1, "warning_count": -1, "issues": [],
                "parse_error": f"malformed JSON at line {exc.lineno} "
                                 f"col {exc.colno}: {exc.msg}"}
    except OSError as exc:
        return {"error_count": -1, "warning_count": -1, "issues": [],
                "parse_error": f"read failed: {exc}"}
    issues: List[Dict[str, Any]] = []
    error_count = 0
    warning_count = 0
    for v in data.get("violations", []) or []:
        sev = (v.get("severity") or "").lower()
        if sev == "error":
            error_count += 1
        elif sev == "warning":
            warning_count += 1
        issues.append({
            "type": v.get("type", ""),
            "severity": sev,
            "description": v.get("description", ""),
            "items": [
                f"{it.get('description', '')} @ "
                f"({it.get('pos', {}).get('x', '?')}, "
                f"{it.get('pos', {}).get('y', '?')})"
                for it in (v.get("items") or [])
            ],
        })
    # Also surface unconnected items as they're a separate top-level key
    for u in data.get("unconnected_items", []) or []:
        warning_count += 1
        issues.append({
            "type": "unconnected_item",
            "severity": "warning",
            "description": u.get("description", "unconnected"),
            "items": [],
        })
    return {
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues[:50],  # cap for chat context
        "total_issues": len(issues),
    }


@tool(
    name="drc_check",
    description=(
        "Run Design Rules Check on a .kicad_pcb. Catches clearance "
        "violations, unrouted nets, holes too close to edges, etc. — "
        "the must-pass gate before sending Gerbers to a fab. Pairs "
        "with erc_check (schematic ERC).\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}                  # required\n'
        '  {"pcb_path": "...", "schematic_parity": true}          # cross-check vs schematic\n'
        '  {"pcb_path": "...", "all_track_errors": true}          # verbose track issues\n'
        "Returns error/warning counts + per-issue summary. Severity "
        "thresholds + units in layout_config.json:drc_check."
    ),
    input_schema={"pcb_path": str},
)
async def drc_check(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: .kicad_pcb not found: {pcb_path}"}],
            "is_error": True,
        }
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: expected .kicad_pcb, got {pcb_path.suffix}"}],
            "is_error": True,
        }

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {
            "content": [{"type": "text",
                          "text": "drc_check disabled in layout_config.json"}],
            "is_error": True,
        }

    cli = _kicad_cli(cfg)
    base_timeout = int(cfg.get("timeout_seconds", 60))
    # P3.1: scale timeout with footprint count. kicad-cli's DRC runs in
    # roughly linear time over footprints + tracks for a typical board,
    # so a fixed 60 s budget times out on anything beyond ~100 footprints.
    # Scale is JSON-gated (timeout_per_footprint_ms, default 200 ms) so
    # large boards get the time they need without dragging tiny ones.
    per_fp_ms = int(cfg.get("timeout_per_footprint_ms", 200))
    max_timeout = int(cfg.get("timeout_max_seconds", 600))
    try:
        fp_count = sum(1 for _ln in pcb_path.read_text(
            encoding="utf-8", errors="ignore").splitlines()
                        if _ln.lstrip().startswith("(footprint "))
    except OSError:
        fp_count = 0
    timeout = min(max_timeout, max(base_timeout, base_timeout + fp_count * per_fp_ms // 1000))
    units = str(cfg.get("units", "mm"))

    report_path = pcb_path.with_name(pcb_path.stem + "-drc.json")
    # Defensive: a leftover garbage report from a previous interrupted
    # run (or from a copied-in sandbox) would pass `report_path.exists()`
    # below but break the JSON parse. Delete first so kicad-cli writes a
    # clean file from scratch; the post-run existence check then ensures
    # we surface a clean "no report produced" error if kicad-cli itself
    # failed silently.
    try:
        if report_path.exists():
            report_path.unlink()
    except OSError:
        # Best-effort — if we can't delete, we still try; the parse
        # path catches downstream issues.
        pass
    cmd = [
        cli, "pcb", "drc",
        "--output", str(report_path),
        "--format", "json",
        "--units", units,
        "--severity-all",
    ]
    if bool(args.get("all_track_errors", cfg.get("all_track_errors", False))):
        cmd.append("--all-track-errors")
    if bool(args.get("schematic_parity", cfg.get("schematic_parity", False))):
        cmd.append("--schematic-parity")
    cmd.append(str(pcb_path))

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": "ERROR: DRC timed out"}],
                 "is_error": True}
    except FileNotFoundError:
        return {"content": [{"type": "text",
                              "text": f"ERROR: kicad-cli not found: {cli}"}],
                 "is_error": True}

    if not report_path.exists():
        err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "no report produced"
        return {"content": [{"type": "text",
                              "text": f"ERROR: DRC failed (exit={r.returncode}): {err}"}],
                 "is_error": True}

    summary = _summarise_report(report_path)
    # Parse failure path — never surface the -1 sentinel to chat. Return
    # is_error so callers (pcb_verify, ship_design, the agent itself)
    # know the DRC stage did not produce a usable result; show the real
    # reason instead of pretending "everything passed".
    if summary.get("parse_error"):
        cli_stderr = (r.stderr or "").strip()
        snippet = ""
        try:
            snippet = report_path.read_text(encoding="utf-8")[:200]
        except OSError:
            pass
        return {
            "content": [{"type": "text",
                          "text": (f"ERROR: DRC report could not be parsed: "
                                    f"{summary['parse_error']}\n"
                                    f"  kicad-cli exit={r.returncode}\n"
                                    + (f"  stderr (last): "
                                        f"{cli_stderr.splitlines()[-1]}\n"
                                        if cli_stderr else "")
                                    + (f"  report head: {snippet!r}"
                                        if snippet else ""))}],
            "is_error": True,
            "parse_error": summary["parse_error"],
            "report_path": str(report_path),
        }
    err_n = summary["error_count"]
    warn_n = summary["warning_count"]
    total = summary.get("total_issues", 0)

    head = (f"DRC: {err_n} errors, {warn_n} warnings"
             f" ({total} total issues)"
             if total else "DRC: 0 errors, 0 warnings — clean")

    detail = ""
    if summary["issues"]:
        bullets = []
        for it in summary["issues"][:5]:
            short = f"  - [{it['severity']}] {it['type']}: {it['description'][:80]}"
            bullets.append(short)
        detail = "\n" + "\n".join(bullets)
        if len(summary["issues"]) > 5:
            detail += f"\n  - ... ({len(summary['issues']) - 5} more in {report_path.name})"

    return {
        "content": [{"type": "text",
                      "text": head + detail + f"\n\nfull report: {report_path}"}],
        "ok": err_n == 0,
        "error_count": err_n,
        "warning_count": warn_n,
        "issues": summary["issues"],
        "report_path": str(report_path),
    }
