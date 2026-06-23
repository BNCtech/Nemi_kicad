"""Tool: run kicad-cli sch erc on the open schematic and surface the
result in chat. Read-only — does NOT attempt auto-fix in v1. Auto-fix
(unconnected pins, missing PWR_FLAG, ground loops) is a follow-up
phase.

Config: layout_config.json:erc_check controls kicad-cli command name,
severity filter, and timeout. The kicad_cli_command may be a single
path or a list of fallback paths — the first one that exists wins.
Falls back to common Windows install paths if the configured path
is missing. No part-number / circuit-name hardcoding.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_agent_sdk import tool


def _load_erc_config() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("erc_check", {})
    except Exception:
        return {}


def _resolve_kicad_cli(cfg: Dict[str, Any]) -> str:
    """Find a working kicad-cli binary. Tries (in order):
      1. cfg['kicad_cli_command'] if it's a real file
      2. cfg['kicad_cli_fallbacks'] list, each entry checked for existence
      3. Common Windows install paths (KiCad 10/9/8/7/6 in Program Files)
      4. `kicad-cli` resolved via PATH (shutil.which)
    Returns the first hit; raises FileNotFoundError if nothing works."""
    candidates: List[str] = []
    raw = cfg.get("kicad_cli_command")
    if isinstance(raw, list):
        candidates.extend(str(p) for p in raw)
    elif isinstance(raw, str) and raw:
        candidates.append(raw)
    for p in cfg.get("kicad_cli_fallbacks", []) or []:
        candidates.append(str(p))
    candidates.extend([
        r"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe",
        r"C:/Program Files/KiCad/9.0/bin/kicad-cli.exe",
        r"C:/Program Files/KiCad/8.0/bin/kicad-cli.exe",
        r"C:/Program Files/KiCad/7.0/bin/kicad-cli.exe",
    ])
    for c in candidates:
        if c and Path(c).is_file():
            return c
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    raise FileNotFoundError(
        "No working kicad-cli binary found. Tried: "
        + ", ".join(candidates) + " (and PATH lookup).")


@tool(
    name="erc_check",
    description=(
        "Run KiCad's Electrical Rule Check on the open schematic and "
        "return the issues found (errors + warnings). Read-only — no "
        "auto-fix in v1. Use when the user asks: 'check ERC', 'are "
        "there any errors?', 'run a design check', 'verify the "
        "schematic', etc.\n"
        "Input: {\"path\": \"<path to .kicad_sch>\"}\n"
        "Output: {error_count, warning_count, issues: [{severity, "
        "type, message, location}, ...], raw_output}"
    ),
    input_schema={"path": str},
)
async def erc_check(args: Dict[str, Any]) -> Dict[str, Any]:
    raw_path = args.get("path", "")
    p = Path(raw_path).expanduser()
    if not p.exists():
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: file not found: {p}"}],
            "is_error": True,
        }
    cfg = _load_erc_config()
    if not cfg.get("enabled", True):
        return {
            "content": [{"type": "text",
                          "text": "ERC checking disabled in layout_config.json"}],
            "is_error": True,
        }
    try:
        cmd = _resolve_kicad_cli(cfg)
    except FileNotFoundError as exc:
        return {
            "content": [{"type": "text", "text": f"ERROR: {exc}"}],
            "is_error": True,
        }
    severity = cfg.get("severity", "all")
    timeout = float(cfg.get("timeout_seconds", 60))
    run_on_copy = bool(cfg.get("run_on_copy", True))

    # The schematic kicad-cli actually reads. When the project is OPEN in
    # KiCad, KiCad holds a `~<name>.kicad_sch.lck` and kicad-cli HANGS
    # reading the locked file (measured 2026-06-08: the locked original
    # times out at 30s while an unlocked COPY of the SAME file finishes in
    # 10s — so this is a LOCK, not slowness; a bigger timeout would not
    # help). By default we copy the whole project (every .kicad_sch + the
    # .kicad_pro + any local *-lib-table, preserving the relative sheet
    # filenames a hierarchy needs) into a throwaway temp dir and run ERC
    # THERE, which works WHILE the file is open in KiCad. The report is
    # then persisted next to the original as <name>.erc.txt so callers get
    # a stable path. Gated by `run_on_copy` (default true); false runs on
    # the original (byte-stable legacy, but times out on a locked file).
    final_report_file = p.with_suffix(".erc.txt")
    erc_target = p
    out_file = final_report_file
    tmpdir: Optional[Path] = None
    if run_on_copy:
        try:
            tmpdir = Path(tempfile.mkdtemp(prefix="envil_erc_"))
            for pat in ("*.kicad_sch", "*.kicad_pro"):
                for f in p.parent.glob(pat):
                    shutil.copy2(f, tmpdir / f.name)
            for nm in ("sym-lib-table", "fp-lib-table"):
                src = p.parent / nm
                if src.exists():
                    shutil.copy2(src, tmpdir / nm)
            erc_target = tmpdir / p.name
            out_file = tmpdir / (p.stem + ".erc.txt")
        except OSError:
            tmpdir = None
            erc_target = p
            out_file = final_report_file

    sev_flag = f"--severity-{severity}" if severity in ("all", "error", "warning") else "--severity-all"

    def _cleanup_tmp() -> None:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, "sch", "erc", sev_flag, str(erc_target),
            "--output", str(out_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                       timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            _cleanup_tmp()
            return {
                "content": [{"type": "text",
                              "text": (f"ERC timed out after {timeout}s — "
                                        f"is `{cmd}` reachable?")}],
                "is_error": True,
            }
    except FileNotFoundError:
        _cleanup_tmp()
        return {
            "content": [{"type": "text",
                          "text": (f"ERROR: `{cmd}` not found on PATH. "
                                    "Install KiCad / point erc_check."
                                    "kicad_cli_command to the binary.")}],
            "is_error": True,
        }

    raw_stdout = (stdout or b"").decode("utf-8", errors="replace")
    raw_stderr = (stderr or b"").decode("utf-8", errors="replace")
    report = ""
    if out_file.exists():
        try:
            report = out_file.read_text(encoding="utf-8")
        except OSError:
            report = ""
    # Persist the report next to the ORIGINAL so the returned path stays
    # valid after the temp dir is removed.
    if tmpdir is not None:
        if report:
            try:
                final_report_file.write_text(report, encoding="utf-8")
            except OSError:
                final_report_file, tmpdir = out_file, None  # keep temp
        else:
            final_report_file, tmpdir = out_file, None       # keep temp
    _cleanup_tmp()

    # Parse the kicad-cli text report. The real format (KiCad 7/8/9+) is:
    #   [violation_type]: Human readable message
    #       ; error            <- severity is on its OWN line, prefixed ;
    #       @(x mm, y mm): description
    # The `[...]` bracket holds the violation TYPE (e.g. power_pin_not_
    # driven), NOT the severity. The previous code read the bracket as the
    # severity, so `error`/`warning` were never matched and every report
    # counted 0 errors / 0 warnings. Now: each `; <sev>` line is one
    # violation; pair it with the preceding `[type]:` line for the issue.
    issues = []
    error_count = warning_count = 0
    if report:
        cur_type = ""
        cur_msg = ""
        for line in report.splitlines():
            ls = line.strip()
            if ls.startswith("[") and "]" in ls:
                cur_type = ls.split("]", 1)[0].strip("[]")
                cur_msg = ls.split("]", 1)[1].lstrip(": ").strip()
            elif ls.startswith(";"):
                sev = ls.lstrip(";").strip().lower()
                if "error" in sev:
                    error_count += 1
                    issues.append({"severity": "error", "type": cur_type,
                                    "message": cur_msg})
                elif "warning" in sev:
                    warning_count += 1
                    issues.append({"severity": "warning", "type": cur_type,
                                    "message": cur_msg})

    summary = {
        "path": str(p),
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues[:50],
        "report_file": (str(final_report_file)
                         if final_report_file.exists() else ""),
        "exit_code": proc.returncode if proc else -1,
    }
    return {
        "content": [{"type": "text",
                      "text": json.dumps(summary, indent=2)}],
    }
