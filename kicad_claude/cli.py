import argparse
import json
import sys
from pathlib import Path

from .basic_checks import run_all as basic_check_run, to_text as basic_check_to_text
from .bom import extract_rows as bom_extract_rows, to_csv as bom_to_csv, to_text as bom_to_text, validate_rows as bom_validate_rows
from .chat import repl as chat_repl
from .circuit_rules import apply_all as rules_apply_all, detect_all as rules_detect_all, to_text as rules_to_text
from .erc import run as erc_run, to_text as erc_to_text
from .fixer import diagnose as fix_diagnose, fix as fix_run, to_text as fix_to_text
from .schematic_extractor import SchematicExtractor
from .validator import validate


def cmd_extract(args):
    extractor = SchematicExtractor(args.path)
    if args.json:
        print(json.dumps(extractor.summary(), indent=2, default=str))
    else:
        print(extractor.format_for_claude())


def cmd_validate(args):
    # L1 fail-fast: deterministic structural checks first. If the schematic has
    # critical structural defects (missing/duplicate refdes, blank values,
    # off-grid pins) the LLM cannot reason about it cleanly, so don't burn
    # tokens. --skip-l1 lets you force the LLM call anyway.
    if not args.skip_l1:
        l1 = basic_check_run(args.path)
        if l1["status"] != "PASS":
            if args.json:
                print(json.dumps({"l1_failed": True, "l1": l1}, indent=2, default=str))
            else:
                print(basic_check_to_text(l1))
                print()
                print("Aborted before Claude call: fix the L1 critical issues above, "
                      "or pass --skip-l1 to validate anyway.", file=sys.stderr)
            sys.exit(1)
        elif not args.json:
            print(basic_check_to_text(l1))
            print()

    from . import hierarchy as _hier
    text = _hier.format_for_claude(args.path)
    print(f"Sending to Claude ({args.model or 'default'}) ...", file=sys.stderr)
    result = validate(text, model=args.model)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    score = result.get("score", "?")
    status = result.get("status", "?")
    print()
    print(f"Score: {score}/100   Status: {status}")
    for tier in ("critical", "high", "medium"):
        items = result.get(tier) or []
        if items:
            print(f"\n[{tier.upper()}]")
            for it in items:
                print(f"  - {it}")
    recs = result.get("recommendations") or []
    if recs:
        print("\n[RECOMMENDATIONS]")
        for r in recs:
            print(f"  - {r}")
    fails = [c for c in (result.get("checks") or []) if c.get("result") == "fail"]
    if fails:
        print("\n[FAILED CHECKS]")
        for c in fails:
            print(f"  {c['id']}: {c.get('evidence','')}")
            if c.get("fix"):
                print(f"     fix: {c['fix']}")


def cmd_check(args):
    report = basic_check_run(args.path)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(basic_check_to_text(report))
    if report["status"] != "PASS":
        sys.exit(1)


def cmd_bom(args):
    rows = bom_extract_rows(args.path)
    issues = bom_validate_rows(rows, strict_mpn=args.strict_mpn)

    if args.json:
        print(json.dumps({"rows": rows, "issues": issues}, indent=2, default=str))
    else:
        print(bom_to_text(rows, issues))

    if args.csv:
        out = bom_to_csv(rows, args.csv)
        print(f"\nCSV written to {out}", file=sys.stderr)

    critical = [i for i in issues if i["severity"] == "critical"]
    if critical:
        sys.exit(1)


def cmd_fix(args):
    if args.dry_run:
        # Default: L1 only (free, local). --include-l2 adds the LLM validator pass.
        report = fix_diagnose(args.path, include_l2=args.include_l2)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
            return
        l2_state = (report["l2"] or {}).get("status") if report["l2"] else "(skipped — pass --include-l2 to run, costs tokens)"
        erc_state = (report.get("erc") or {}).get("status", "?")
        print(f"DIAGNOSE - {args.path}")
        print(f"L1: {report['l1']['status']}, ERC: {erc_state}, L2: {l2_state}")
        print(f"Total issues that would be sent to fix loop: {len(report['issues'])}")
        for i in report["issues"][:30]:
            print(f"  [{i['layer']}/{i['severity']}] {i.get('check','')} {i['refs']}: {i['message']}")
        return

    result = fix_run(args.path, model=args.model)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(fix_to_text(result))
    if result["status"] != "PASS":
        sys.exit(1)


def cmd_erc(args):
    report = erc_run(args.path)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(erc_to_text(report))
    if report["status"] == "OK":
        critical = sum(1 for i in report["issues"] if i["severity"] == "critical")
        if critical:
            sys.exit(1)
    elif report["status"] == "ERROR":
        sys.exit(2)


def cmd_apply_rules(args):
    """Deterministic rule applier (POWER_001 / POWER_005 / OSC_001 / RST_001 /
    PUL_001 / LED_001). No Claude call — pure local. Mutates the schematic."""
    rules = args.only.split(",") if args.only else None
    if args.dry_run:
        report = rules_detect_all(args.path)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(f"CIRCUIT RULES (DRY RUN) - {report['path']}")
            print(f"  total findings: {len(report['findings'])}")
            for rid, n in sorted(report["by_rule"].items()):
                print(f"    {rid}: {n}")
            for f in report["findings"]:
                print(f"  [{f['severity']:8s}] {f['rule_id']:10s} "
                      f"{', '.join(f['refs'])}: {f['message']}")
        return
    report = rules_apply_all(args.path, rules=rules)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(rules_to_text(report))


def cmd_serve(args):
    from .server import run as serve_run
    ipc_files = [Path(p) for p in (args.ipc_port_file or [])] or None
    serve_run(host=args.host, port=args.port, ipc_port_files=ipc_files)


def main(argv=None):
    p = argparse.ArgumentParser(prog="kicad-claude")
    sub = p.add_subparsers(required=True, dest="cmd")

    pe = sub.add_parser("extract", help="parse a .kicad_sch file and print summary")
    pe.add_argument("path", type=Path)
    pe.add_argument("--json", action="store_true")
    pe.set_defaults(func=cmd_extract)

    pv = sub.add_parser("validate", help="run Claude validation on a .kicad_sch")
    pv.add_argument("path", type=Path)
    pv.add_argument("--model", default=None,
                    help="override model (default from CLAUDE_MODEL_DEEP env)")
    pv.add_argument("--json", action="store_true")
    pv.add_argument("--skip-l1", action="store_true",
                    help="skip the L1 deterministic pre-pass (debug only — wastes tokens on broken schematics)")
    pv.set_defaults(func=cmd_validate)

    pck = sub.add_parser("check",
                         help="L1 deterministic checks (refdes, values, label format, orphans)")
    pck.add_argument("path", type=Path)
    pck.add_argument("--json", action="store_true")
    pck.set_defaults(func=cmd_check)

    perc = sub.add_parser("erc",
                          help="run kicad-cli ERC and report violations (uses official KiCad engine)")
    perc.add_argument("path", type=Path)
    perc.add_argument("--json", action="store_true")
    perc.set_defaults(func=cmd_erc)

    pb = sub.add_parser("bom", help="extract a Bill of Materials from a .kicad_sch")
    pb.add_argument("path", type=Path)
    pb.add_argument("--csv", type=Path, default=None, help="also write a CSV to this path")
    pb.add_argument("--json", action="store_true", help="emit JSON instead of a text table")
    pb.add_argument("--strict-mpn", action="store_true",
                    help="treat ICs without an MPN property as critical issues")
    pb.set_defaults(func=cmd_bom)

    pf = sub.add_parser("fix",
                        help="run validate->repair loop (L1 + L2 + Claude-driven ops, mutates the schematic)")
    pf.add_argument("path", type=Path)
    pf.add_argument("--model", default=None,
                    help="override model (default = fixer_config.json claude.model or .env CLAUDE_MODEL_DEEP)")
    pf.add_argument("--dry-run", action="store_true",
                    help="diagnose only — list issues; no edits, no repair calls. L1 only by default (free).")
    pf.add_argument("--include-l2", action="store_true",
                    help="with --dry-run, also run the LLM validator (read-only but COSTS TOKENS)")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_fix)

    pc = sub.add_parser("chat", help="start a chat session that can edit a .kicad_sch")
    pc.add_argument("path", type=str)
    pc.add_argument("--model", default=None)
    pc.set_defaults(func=lambda a: chat_repl(a.path, model=a.model))

    par = sub.add_parser("apply-rules",
                         help="deterministic rule applier: auto-add decoupling caps, "
                              "pull-ups, load caps, PWR_FLAGs, current-limit R for LEDs. "
                              "Pure local — no Claude call. Mutates the schematic.")
    par.add_argument("path", type=Path)
    par.add_argument("--dry-run", action="store_true",
                     help="list findings only; do not edit the schematic")
    par.add_argument("--only", default=None,
                     help="comma-separated rule IDs to apply "
                          "(POWER_001,POWER_002,POWER_005,OSC_001,RST_001,PUL_001,LED_001)")
    par.add_argument("--json", action="store_true")
    par.set_defaults(func=cmd_apply_rules)

    ps = sub.add_parser("serve", help="run the WebSocket+IPC backend for the eeschema AI chat panel")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8765)
    ps.add_argument("--ipc-port-file", action="append", default=None,
                    help="path to write the TCP IPC port; can be passed multiple times")
    ps.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
