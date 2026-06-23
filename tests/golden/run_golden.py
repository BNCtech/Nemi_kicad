"""Golden regression harness — deterministic electrical-correctness gate.

For every fixture in ``fixtures/*.json`` (pure IR data) this runs the real
pipeline — normalize -> validate -> render — then fingerprints the result and
fails if anything regresses against the committed baseline.

DESIGN (no hardcoding — see [[project_config_no_hardcode]]):
  - Fixtures are discovered by glob; there is NO per-circuit code here.
  - Every threshold / selector param / canonicalization rule / strip pattern
    lives in ``golden_config.json``; this module reads them, never embeds them.
  - Render mode is the engine's own auto-decision (block-count driven) — the
    harness never forces flat/hierarchy.
  - The pass/fail gate is a two-tier connectivity hash plus universal
    invariants (0 errors, 0 R2 body-crossings, 0 R16 cross-block wires) that a
    known-good circuit must satisfy. A fixture may relax these via its own
    optional ``expected`` block (for intentional-defect fixtures).

USAGE
  python run_golden.py                 # gate: exit 1 on any regression
  python run_golden.py --update        # (re)seed baselines from current output
  python run_golden.py --only ne555    # run one fixture (substring match)
  python run_golden.py --determinism   # 0.0 probe: same fixture must hash the
                                        #   same twice in-proc AND across procs
  python run_golden.py --emit-snapshots # print {name: snapshot} JSON, no gate
                                        #   (used by --determinism cross-proc)
  python run_golden.py --no-tier2      # skip the kicad-cli netlist hash
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_AI_BACKEND = _HERE.parent.parent          # f:/Ki_CAD/ai_backend
sys.path.insert(0, str(_AI_BACKEND))

from envil_agent.intent.ir import TopologyIR            # noqa: E402
from envil_agent.intent import engine                    # noqa: E402
from envil_agent.intent.normalize import normalize_ir    # noqa: E402
from envil_agent.intent.validate import (                # noqa: E402
    validate_ir, dedupe_issues,
)
from envil_agent.lint.context import build_context       # noqa: E402
from envil_agent.lint.selectors import (                 # noqa: E402
    find_wires_piercing_bodies, find_crossblock_wires,
)

_FIXTURES_DIR = _HERE / "fixtures"
_BASELINES_DIR = _HERE / "baselines"
_CONFIG_PATH = _HERE / "golden_config.json"


# ---------------------------------------------------------------------------
# Config — single source of every threshold. No literals leak into the logic.
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------
def _sha(blob: str) -> str:
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _canonical_stats(stats: dict, cfg: dict) -> dict:
    """Recursively drop absolute-path / per-run keys so the stats snapshot is
    pure connectivity-relevant counts. Rules come from config (drop_keys +
    abs_path_regex), nothing hardcoded."""
    canon = cfg["canonicalize"]
    drop = set(canon.get("drop_keys", []))
    path_re = re.compile(canon["abs_path_regex"]) if canon.get("abs_path_regex") else None

    def _walk(v):
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in sorted(v.items())
                    if k not in drop}
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, str) and path_re is not None and path_re.search(v):
            return "<path>"
        return v

    return _walk(stats)


def _tier1_netlist_hash(ir: TopologyIR) -> str:
    """Connectivity fingerprint from the POST-normalize IR: the partition of
    pins into named nets. Deterministic, pure-python, no external tool."""
    nets = sorted(
        ({"name": n.name, "is_power": bool(n.is_power),
          "pins": sorted(n.pins)} for n in ir.nets),
        key=lambda d: (d["name"], d["pins"]),
    )
    return _sha(json.dumps(nets, sort_keys=True, ensure_ascii=False))


def _resolve_kicad_cli(cfg: dict):
    """Locate kicad-cli WITHOUT hardcoding a path (see
    [[project_config_no_hardcode]]):
      1. an explicit override in golden_config.json:netlist_hash.kicad_cli_command
      2. the project's OWN resolver — any `kicad_cli_command` in layout_config.json
         (the same value erc_check / export_preview_svg / drc_check already use)
      3. PATH (shutil.which)
    Returns an existing executable path or None."""
    nh = cfg.get("netlist_hash", {})
    cand = nh.get("kicad_cli_command")
    if cand and Path(cand).exists():
        return cand
    try:
        from envil_agent.intent.engine import _load_layout_config
        for section in _load_layout_config().values():
            if isinstance(section, dict):
                c = section.get("kicad_cli_command")
                if c and Path(c).exists():
                    return c
    except Exception:                              # noqa: BLE001
        pass
    return shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")


def _tier2_netlist_hash(parent_sheet: Path, cfg: dict, tmp: Path):
    """Ground-truth fingerprint: `kicad-cli sch export netlist` of the rendered
    file (exactly what KiCad's connectivity engine sees), canonicalized by the
    config strip patterns. Returns None when kicad-cli is unavailable/disabled
    — Tier 1 still gates."""
    nh = cfg.get("netlist_hash", {})
    if not nh.get("tier2_kicad_cli_enabled", True):
        return None
    exe = _resolve_kicad_cli(cfg)
    if not exe:
        return None
    out = tmp / "golden.net"
    try:
        subprocess.run(
            [exe, "sch", "export", "netlist", "--output", str(out),
             str(parent_sheet)],
            capture_output=True, timeout=120, check=True,
        )
        text = out.read_text(encoding="utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return None
    for pat in nh.get("tier2_strip_patterns", []):
        text = re.sub(pat, "", text)
    lines = sorted(ln.strip() for ln in text.splitlines() if ln.strip())
    return _sha("\n".join(lines))


def _geometry(sheets, cfg: dict) -> dict:
    """Sum R2 (body-pierce) + R16 (cross-block wire) violations across every
    rendered sheet (parent + children). Selector params from config."""
    margin = float(cfg.get("selector_params", {}).get("r2_margin_mm", 0.5))
    r2 = r16 = 0
    errors = []
    for sh in sheets:
        try:
            ctx = build_context(Path(sh))
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"{Path(sh).name}: {type(exc).__name__}: {exc}")
            continue
        r2 += len(find_wires_piercing_bodies(
            ctx["wires"], ctx["bboxes"], ctx["pin_positions"], margin_mm=margin))
        r16 += len(find_crossblock_wires(ctx["wires"], ctx["blocks"]))
    out = {"R2_body_crossings": r2, "R16_cross_block_wires": r16}
    if errors:
        out["parse_errors"] = errors
    return out


def _build_snapshot(fixture: dict, cfg: dict, no_tier2: bool) -> dict:
    """Run the pipeline on ONE fixture and return its deterministic snapshot."""
    ir = TopologyIR.from_dict(fixture["ir"])
    normalize_ir(ir)                                   # mirror production order
    issues = dedupe_issues(validate_ir(ir))
    err = sum(1 for i in issues if i.get("severity") == "error")
    warn = sum(1 for i in issues if i.get("severity") == "warning")
    codes = sorted({i.get("code", "") for i in issues if i.get("code")})

    tmp = Path(tempfile.mkdtemp(prefix="golden_"))
    try:
        stats = engine.render(ir, tmp)                 # engine auto-decides mode
        sheets = [stats["path"]]
        sheets += [c.get("path") for c in stats.get("children", []) if c.get("path")]
        sheets = [s for s in sheets if s and Path(s).exists()]
        geom = _geometry(sheets, cfg)
        tier1 = _tier1_netlist_hash(ir) if cfg["netlist_hash"].get("tier1_enabled", True) else None
        tier2 = None if no_tier2 else _tier2_netlist_hash(Path(stats["path"]), cfg, tmp)
        snapshot = {
            "validation": {"error_count": err, "warning_count": warn, "codes": codes},
            "render": _canonical_stats(stats, cfg),
            "geometry": geom,
            "netlist_tier1": tier1,
            "netlist_tier2": tier2,
        }
        return snapshot
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Invariant evaluation
# ---------------------------------------------------------------------------
def _check_invariants(snapshot: dict, fixture: dict, cfg: dict):
    """Return a list of human-readable failure strings (empty == pass).
    Universal invariants from config, overridable per-fixture via `expected`."""
    inv = dict(cfg.get("universal_invariants", {}))
    inv.update(fixture.get("expected", {}) or {})      # per-fixture override
    val = snapshot["validation"]
    geom = snapshot["geometry"]
    fails = []
    if val["error_count"] > int(inv.get("max_errors", 0)):
        fails.append(f"error_count {val['error_count']} > max {inv.get('max_errors', 0)}")
    if geom["R2_body_crossings"] > int(inv.get("max_r2_body_crossings", 0)):
        fails.append(f"R2 body-crossings {geom['R2_body_crossings']} > "
                     f"max {inv.get('max_r2_body_crossings', 0)}")
    if geom["R16_cross_block_wires"] > int(inv.get("max_r16_cross_block_wires", 0)):
        fails.append(f"R16 cross-block-wires {geom['R16_cross_block_wires']} > "
                     f"max {inv.get('max_r16_cross_block_wires', 0)}")
    if geom.get("parse_errors"):
        fails.append(f"sheet parse errors: {geom['parse_errors']}")
    for code in inv.get("codes_absent", []):
        if code in val["codes"]:
            fails.append(f"forbidden code present: {code}")
    for code in inv.get("require_codes", []):
        if code not in val["codes"]:
            fails.append(f"required code absent: {code}")
    return fails


# ---------------------------------------------------------------------------
# Baseline IO
# ---------------------------------------------------------------------------
def _baseline_path(name: str) -> Path:
    return _BASELINES_DIR / f"{name}.baseline.json"


def _load_baseline(name: str):
    p = _baseline_path(name)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _write_baseline(name: str, snapshot: dict) -> None:
    _BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    _baseline_path(name).write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")


def _diff(base: dict, cur: dict) -> list:
    """Flat key-path diff between two snapshots."""
    out = []

    def _walk(prefix, a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                _walk(f"{prefix}.{k}" if prefix else k, a.get(k), b.get(k))
        elif a != b:
            out.append(f"  {prefix}: baseline={a!r}  current={b!r}")

    _walk("", base, cur)
    return out


# ---------------------------------------------------------------------------
# Fixture discovery + drivers
# ---------------------------------------------------------------------------
def _discover(only: str):
    fixtures = []
    for p in sorted(_FIXTURES_DIR.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        name = d.get("name") or p.stem
        if only and only.lower() not in name.lower():
            continue
        fixtures.append((name, d))
    return fixtures


def _run_gate(only: str, update: bool, no_tier2: bool, verbose: bool, cfg: dict) -> int:
    fixtures = _discover(only)
    if not fixtures:
        print("no fixtures matched", flush=True)
        return 1
    rc = 0
    print(f"{'fixture':24s} {'err':>3s} {'warn':>4s} {'R2':>3s} {'R16':>4s} "
          f"{'tier1':>8s} {'tier2':>8s}  result", flush=True)
    for name, d in fixtures:
        try:
            snap = _build_snapshot(d, cfg, no_tier2)
        except Exception as exc:                       # noqa: BLE001
            print(f"{name:24s}  BUILD ERROR: {type(exc).__name__}: {exc}", flush=True)
            rc = 1
            continue
        v, g = snap["validation"], snap["geometry"]
        t1 = (snap["netlist_tier1"] or "-")[:8]
        t2 = (snap["netlist_tier2"] or "-")[:8]
        fails = _check_invariants(snap, d, cfg)
        strict = cfg.get("gate", {}).get("mode", "regression") == "strict"
        status = "ok"
        base = _load_baseline(name)
        if update:
            _write_baseline(name, snap)
            status = "UPDATED"
        else:
            if base is None:
                status = "NO-BASELINE"
                rc = 1
            elif base != snap:
                status = "DRIFT"
                rc = 1
            elif strict and fails:
                status = "INVARIANT-FAIL"
                rc = 1
        print(f"{name:24s} {v['error_count']:3d} {v['warning_count']:4d} "
              f"{g['R2_body_crossings']:3d} {g['R16_cross_block_wires']:4d} "
              f"{t1:>8s} {t2:>8s}  {status}", flush=True)
        # Quality issues: loud at seed time (never silently bless a defect) and
        # shown at gate time as KNOWN issues (non-gating unless gate.mode=strict).
        if fails:
            tag = "QUALITY (blessed)" if update else ("INVARIANT" if strict else "KNOWN")
            for f in fails:
                print(f"    {tag}: {f}", flush=True)
        if status == "DRIFT":
            for line in _diff(base, snap):
                print(line, flush=True)
        if verbose:
            print(json.dumps(snap, indent=2, sort_keys=True), flush=True)
    return rc


def _run_determinism(only: str, no_tier2: bool, cfg: dict) -> int:
    """Phase 0.0 probe: a fixture must hash identically twice in one process
    AND across two processes with different PYTHONHASHSEED. Any difference
    means hidden set/dict-ordering nondeterminism that would make baselines
    flap — fix it before seeding baselines."""
    fixtures = _discover(only)
    rc = 0
    # In-process double render.
    for name, d in fixtures:
        a = _build_snapshot(d, cfg, no_tier2)
        b = _build_snapshot(d, cfg, no_tier2)
        if a != b:
            rc = 1
            print(f"{name:24s} IN-PROC NONDETERMINISTIC", flush=True)
            for line in _diff(a, b):
                print(line, flush=True)
        else:
            print(f"{name:24s} in-proc stable", flush=True)
    # Cross-process: re-run self with two different hash seeds and diff.
    snaps = {}
    for seed in ("0", "1"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__)), "--emit-snapshots"]
            + (["--only", only] if only else [])
            + (["--no-tier2"] if no_tier2 else []),
            capture_output=True, text=True, env=env, timeout=600)
        try:
            snaps[seed] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            print(f"cross-proc seed={seed} produced no JSON:\n{proc.stdout}\n{proc.stderr}",
                  flush=True)
            return 1
    if snaps["0"] != snaps["1"]:
        rc = 1
        print("CROSS-PROC NONDETERMINISTIC", flush=True)
        for line in _diff(snaps["0"], snaps["1"]):
            print(line, flush=True)
    else:
        print("cross-proc stable (PYTHONHASHSEED 0 vs 1)", flush=True)
    print("DETERMINISM OK" if rc == 0 else "DETERMINISM FAILED", flush=True)
    return rc


def _emit_snapshots(only: str, no_tier2: bool, cfg: dict) -> int:
    """Print {name: snapshot} JSON to stdout, no gating. Used by --determinism
    cross-process check."""
    out = {}
    for name, d in _discover(only):
        out[name] = _build_snapshot(d, cfg, no_tier2)
    print(json.dumps(out, sort_keys=True, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Golden regression harness")
    ap.add_argument("--update", action="store_true", help="(re)seed baselines")
    ap.add_argument("--only", default="", help="run fixtures whose name contains this")
    ap.add_argument("--determinism", action="store_true", help="0.0 probe")
    ap.add_argument("--emit-snapshots", action="store_true", help="print snapshots JSON")
    ap.add_argument("--no-tier2", action="store_true", help="skip kicad-cli netlist hash")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    cfg = _load_config()
    if args.emit_snapshots:
        return _emit_snapshots(args.only, args.no_tier2, cfg)
    if args.determinism:
        return _run_determinism(args.only, args.no_tier2, cfg)
    return _run_gate(args.only, args.update, args.no_tier2, args.verbose, cfg)


if __name__ == "__main__":
    sys.exit(main())
