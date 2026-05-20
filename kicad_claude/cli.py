import argparse
import json
import sys
from pathlib import Path

from .basic_checks import run_all as basic_check_run, to_text as basic_check_to_text
from .bom import (
    apply_quantity_breaks as bom_apply_qb,
    enrich_with_ai as bom_enrich_ai,
    extract_rows as bom_extract_rows,
    health_scorecard as bom_health,
    to_csv as bom_to_csv,
    to_html as bom_to_html,
    to_json as bom_to_json,
    to_text as bom_to_text,
    to_text_report as bom_to_text_report,
    to_xml as bom_to_xml,
    validate_rows as bom_validate_rows,
)
from . import bomdoc as bom_doc
from . import bom_backfill as bom_bf
from .chat import repl as chat_repl
from .rules import apply_all as rules_apply_all, detect_all as rules_detect_all, to_text as rules_to_text
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
    # Layer 1: schematic + library enrichment.
    doc = bom_doc.load(args.path) if args.bomdoc or args.enrich else None
    rows = bom_extract_rows(
        args.path,
        enrich_from_lib=not args.no_lib_enrich,
        bomdoc_overlay=doc,
        variant=args.variant,
    )

    # Layer 2: AI procurement enrichment (Claude → MPN/Mfr/Price/Lifecycle).
    if args.enrich:
        if doc is None:
            doc = bom_doc.load(args.path)

        def _progress(i, total, r):
            print(f"  [{i+1}/{total}] resolving {r.get('value','?')} {r.get('footprint','')}",
                  file=sys.stderr)

        rows = bom_enrich_ai(
            rows, args.path, doc,
            force_refresh=args.refresh,
            progress=_progress,
        )
        bom_doc.save(args.path, doc)
        print(f"BomDoc saved: {bom_doc.doc_path_for(args.path)}", file=sys.stderr)

    # Layer 3: quantity breaks → ext_price.
    rows = bom_apply_qb(rows, boards=max(1, int(args.boards)))

    issues = bom_validate_rows(rows, strict_mpn=args.strict_mpn)
    scorecard = bom_health(rows)

    if args.json:
        print(bom_to_json(rows, issues, scorecard))
    else:
        print(bom_to_text_report(rows, issues, scorecard, bom_type=args.bom_type))

    if args.csv:
        out = bom_to_csv(rows, args.csv, bom_type=args.bom_type)
        print(f"\nCSV written to {out}", file=sys.stderr)

    if args.xlsx:
        try:
            from .xlsx_writer import write as xlsx_write
            out = xlsx_write(rows, args.xlsx, issues, scorecard, bom_type=args.bom_type)
            print(f"XLSX written to {out}", file=sys.stderr)
        except ImportError as e:
            print(f"XLSX skipped: {e}", file=sys.stderr)

    if args.html:
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            bom_to_html(rows, issues, scorecard, bom_type=args.bom_type),
            encoding="utf-8",
        )
        print(f"HTML written to {out}", file=sys.stderr)

    if args.xml:
        out = Path(args.xml)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(bom_to_xml(rows, bom_type=args.bom_type), encoding="utf-8")
        print(f"XML written to {out}", file=sys.stderr)

    critical = [i for i in issues if i["severity"] == "critical"]
    if critical:
        sys.exit(1)


def cmd_bom_lock(args):
    """Lock a corrected MPN/Mfr/etc. for one or more refs (Altium-style
    manual approval). Subsequent --enrich --refresh keeps user choices."""
    refs = [r.strip() for r in args.ref.split(",") if r.strip()]
    if not refs:
        print("error: --ref required (comma-separated)", file=sys.stderr)
        sys.exit(2)

    doc = bom_doc.load(args.path)
    key_map = bom_doc.find_keys_for_ref(args.path, refs)

    missing = [r for r, k in key_map.items() if not k]
    if missing:
        print(f"error: refs not found in schematic: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    # Group refs that share the same line so we lock once per line.
    seen_keys: set = set()
    locked_summary = []
    for ref in refs:
        key = key_map[ref]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ld = bom_doc.apply_lock(
            doc,
            key,
            mpn=args.mpn,
            manufacturer=args.mfr,
            distributor=args.distributor,
            unit_price=args.unit_price,
            lifecycle=args.lifecycle,
            alternates=[a.strip() for a in args.alternates.split(",")] if args.alternates else None,
            notes=args.notes,
            locked=not args.unlock,
        )
        locked_summary.append((ref, key, ld))

    out = bom_doc.save(args.path, doc)
    if args.json:
        print(json.dumps({"saved": str(out), "locked": [
            {"ref": r, "key": k, "line": ld} for r, k, ld in locked_summary
        ]}, indent=2, default=str))
    else:
        print(f"BomDoc saved: {out}")
        print(f"{'Unlocked' if args.unlock else 'Locked'} {len(locked_summary)} line(s):")
        for ref, key, ld in locked_summary:
            mpn = (ld.get('approved_mpns') or [''])[0]
            print(f"  {ref:6s}  {key}")
            print(f"          MPN={mpn!r}  Mfr={ld.get('manufacturer','')!r}  "
                  f"Dist={ld.get('preferred_distributor','')!r}  "
                  f"Price={ld.get('unit_price')}  Lifecycle={ld.get('lifecycle','')!r}")


def cmd_bom_backfill(args):
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    report = bom_bf.back_fill(
        args.path,
        use_bomdoc=not args.no_bomdoc,
        fields=fields,
        overwrite_existing=args.overwrite,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(bom_bf.to_text(report))


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

    pb = sub.add_parser("bom",
                        help="extract a Bill of Materials from a .kicad_sch (Altium-class: lib enrichment, AI MPN/price resolution, variants, quantity breaks, multi-format export)")
    pb.add_argument("path", type=Path)
    pb.add_argument("--csv", type=Path, default=None, help="also write a CSV to this path")
    pb.add_argument("--xlsx", type=Path, default=None, help="also write an Excel .xlsx to this path (needs openpyxl)")
    pb.add_argument("--html", type=Path, default=None, help="also write a self-contained HTML report")
    pb.add_argument("--xml",  type=Path, default=None, help="also write an ERP-friendly XML")
    pb.add_argument("--json", action="store_true", help="emit JSON instead of a text table")
    pb.add_argument("--strict-mpn", action="store_true",
                    help="treat ICs without an MPN property as critical issues")
    pb.add_argument("--bom-type", default="procurement",
                    choices=["schematic", "procurement", "assembly", "test", "full"],
                    help="column subset to render (defaults to procurement)")
    pb.add_argument("--variant", default="default",
                    help="assembly variant name (see bomdoc_config.json:variants)")
    pb.add_argument("--boards", type=int, default=1,
                    help="number of boards being built (multiplies qty for quantity-break discounts)")
    pb.add_argument("--bomdoc", action="store_true",
                    help="overlay user overrides + cached AI from <project>.bomdoc.json")
    pb.add_argument("--enrich", action="store_true",
                    help="run Claude-backed MPN/Mfr/Price/Lifecycle resolver, cache into bomdoc")
    pb.add_argument("--refresh", action="store_true",
                    help="force re-resolution; ignore the bomdoc cache (use after schematic edits)")
    pb.add_argument("--no-lib-enrich", action="store_true",
                    help="skip pulling Description/Datasheet/Footprint from the .kicad_sym lib")
    pb.set_defaults(func=cmd_bom)

    pbl = sub.add_parser("bom-lock",
                         help="lock a curated MPN/Mfr/Distributor/Price/Lifecycle for one or more refs (Altium-style manual approval). Future --enrich --refresh respects locks.")
    pbl.add_argument("path", type=Path)
    pbl.add_argument("--ref", required=True,
                     help="reference designator(s), comma-separated (e.g. C1,C2). Locking once per shared line is automatic.")
    pbl.add_argument("--mpn", default=None, help="approved MPN (becomes preferred)")
    pbl.add_argument("--mfr", default=None, help="manufacturer")
    pbl.add_argument("--distributor", default=None, help="preferred distributor (LCSC/DigiKey/Mouser/...)")
    pbl.add_argument("--unit-price", type=float, default=None, help="unit price (single qty, currency from bomdoc_config)")
    pbl.add_argument("--lifecycle", default=None,
                     choices=["Active", "NRND", "EOL", "Obsolete", "Unknown"])
    pbl.add_argument("--alternates", default=None, help="comma-separated approved alternate MPNs")
    pbl.add_argument("--notes", default=None, help="free-text procurement notes")
    pbl.add_argument("--unlock", action="store_true", help="clear the lock flag (keep stored values)")
    pbl.add_argument("--json", action="store_true")
    pbl.set_defaults(func=cmd_bom_lock)

    pbf = sub.add_parser("bom-backfill",
                         help="write Description/Datasheet/MPN/Manufacturer onto each placed symbol so KiCad's GUI BOM tool also exports them")
    pbf.add_argument("path", type=Path)
    pbf.add_argument("--fields", default="Description,Datasheet,MPN,Manufacturer",
                     help="comma-separated list of property names to back-fill")
    pbf.add_argument("--overwrite", action="store_true",
                     help="replace non-blank existing values too (default: only fill blanks/sentinels)")
    pbf.add_argument("--no-bomdoc", action="store_true",
                     help="ignore <project>.bomdoc.json (lib-only back-fill: Description+Datasheet only)")
    pbf.add_argument("--dry-run", action="store_true",
                     help="report what would change; do not modify the file")
    pbf.add_argument("--json", action="store_true")
    pbf.set_defaults(func=cmd_bom_backfill)

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
