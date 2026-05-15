"""kicad-cli ERC wrapper.

Runs KiCad's official ERC engine on a .kicad_sch and normalizes the output
into the same issue shape that L1 (basic_checks) and L2 (validator) use, so
the diagnose / fix pipeline sees one merged list.

All knobs live in erc_config.json — binary location, severity flags,
type-ignore list, severity mapping. Nothing hardcoded here.

Public API:
  available()                 -> bool   (kicad-cli was found and is runnable)
  resolve_binary()            -> str | None
  run(schematic_path)         -> {status, found_cli, raw, issues, error?}
  to_text(report)             -> printable summary
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._config_loader import load as _load_config


def _cfg() -> Dict[str, Any]:
    return _load_config("erc_config")


def resolve_binary() -> Optional[str]:
    """Find kicad-cli using config order: explicit path → env var → autodetect → $PATH."""
    cfg = _cfg()["binary"]
    explicit = cfg.get("kicad_cli_path")
    if explicit and Path(explicit).is_file():
        return explicit
    env_var = cfg.get("env_var")
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val and Path(env_val).is_file():
            return env_val
    for candidate in cfg.get("autodetect_paths", []):
        if candidate and Path(candidate).is_file():
            return candidate
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    return None


def available() -> bool:
    return resolve_binary() is not None


def _normalize_violation(
    sheet_path: str,
    v: Dict[str, Any],
    severity_map: Dict[str, str],
) -> Dict[str, str]:
    sev_raw = v.get("severity", "warning")
    severity = severity_map.get(sev_raw, "medium")
    refs = []
    for it in v.get("items") or []:
        desc = it.get("description") or ""
        # KiCad item descriptions look like "Symbol R5 [R]" or "Pin 1 of U2"; trim the trailing [type] noise.
        refs.append(desc.split(" [")[0].strip())
    refs_str = ", ".join(r for r in refs if r) or sheet_path
    return {
        "layer": "ERC",
        "severity": severity,
        "check": v.get("type") or "ERC",
        "refs": refs_str,
        "message": (v.get("description") or "").strip(),
        "sheet": sheet_path,
    }


def run(schematic_path) -> Dict[str, Any]:
    """Invoke kicad-cli ERC on the schematic. Returns a structured report.

    The returned `issues` list uses the same shape as basic_checks and the
    fix-loop's diagnose, so it can be merged in directly. If kicad-cli is not
    available, status='SKIPPED' and issues=[]; the rest of the pipeline keeps
    working unchanged.
    """
    cfg = _cfg()
    binary = resolve_binary()
    if not binary:
        return {
            "status": "SKIPPED",
            "found_cli": None,
            "raw": None,
            "issues": [],
            "error": "kicad-cli not found (set erc_config.binary.kicad_cli_path or KICAD_CLI env var)",
        }

    inv = cfg["invoke"]
    out_path = Path(tempfile.gettempdir()) / "kicad_claude_erc.json"
    cmd = [
        binary, "sch", "erc",
        "--format", inv.get("format", "json"),
        "--units", inv.get("units", "mm"),
        "--output", str(out_path),
    ]
    if inv.get("severity_all", True):
        cmd.append("--severity-all")
    cmd.append(str(schematic_path))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=int(inv.get("timeout_sec", 60)),
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "ERROR",
            "found_cli": binary,
            "raw": None,
            "issues": [],
            "error": f"kicad-cli ERC timed out after {inv.get('timeout_sec', 60)}s",
        }
    except (OSError, subprocess.SubprocessError) as e:
        return {"status": "ERROR", "found_cli": binary, "raw": None, "issues": [],
                "error": f"kicad-cli failed to launch: {e}"}

    if not out_path.exists():
        return {
            "status": "ERROR",
            "found_cli": binary,
            "raw": None,
            "issues": [],
            "error": f"kicad-cli exited {proc.returncode} but produced no JSON. stdout={proc.stdout[:300]} stderr={proc.stderr[:300]}",
        }

    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"status": "ERROR", "found_cli": binary, "raw": None, "issues": [],
                "error": f"could not parse kicad-cli JSON output: {e}"}

    sev_map = cfg["ignore"]["severity_map"]
    ignored_types = set(cfg["ignore"].get("violation_types") or [])

    issues: List[Dict[str, str]] = []
    for sheet in data.get("sheets") or []:
        sheet_path = sheet.get("path") or "/"
        for v in sheet.get("violations") or []:
            if v.get("type") in ignored_types:
                continue
            issues.append(_normalize_violation(sheet_path, v, sev_map))

    return {
        "status": "OK",
        "found_cli": binary,
        "raw": data,
        "issues": issues,
        "error": None,
    }


def to_text(report: Dict[str, Any]) -> str:
    if report["status"] == "SKIPPED":
        return f"ERC SKIPPED — {report.get('error','')}"
    if report["status"] == "ERROR":
        return f"ERC ERROR — {report.get('error','')}"
    issues = report["issues"]
    counts: Dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for i in issues:
        counts[i["severity"]] = counts.get(i["severity"], 0) + 1
    lines = [
        f"KICAD-CLI ERC — {len(issues)} violation(s) (binary: {report['found_cli']})",
        f"  critical: {counts['critical']}, high: {counts['high']}, medium: {counts['medium']}, low: {counts['low']}",
    ]
    if issues:
        lines.append("")
        lines.append("VIOLATIONS:")
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for it in sorted(issues, key=lambda i: order.get(i["severity"], 9)):
            lines.append(f"  [{it['severity']:8s}] {it['check']:25s} {it['refs']}: {it['message']}")
    return "\n".join(lines)
