"""Ad-hoc: list the most recent LangSmith root runs for Envil_CAD."""
import os, sys, io
from pathlib import Path
# Force UTF-8 so the Windows cp1252 console doesn't crash on arrows / unicode.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

# Avast MITM workaround — must run before any TLS client is built.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception as e:
    print("truststore not injected:", e)

# Load ai_backend/.env so LANGSMITH_* are set even outside the server.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from langsmith import Client

proj = os.environ.get("LANGSMITH_PROJECT") or "Envil_CAD"
c = Client()
print(f"project={proj} endpoint={os.environ.get('LANGCHAIN_ENDPOINT')}")

runs = list(c.list_runs(project_name=proj, is_root=True, limit=12))
runs.sort(key=lambda r: r.start_time, reverse=True)
print(f"\n{len(runs)} most-recent root runs:\n")
for r in runs:
    dur = ""
    if r.end_time and r.start_time:
        dur = f"{(r.end_time - r.start_time).total_seconds():6.1f}s"
    status = r.status or "?"
    err = (r.error or "").splitlines()[0][:80] if r.error else ""
    print(f"{r.start_time:%m-%d %H:%M:%S}  {status:8} {dur:>8}  {r.name[:34]:34}  {err}")

# Drill into the newest CIRCUIT DESIGN (build) run.
builds = [r for r in runs if "Circuit" in (r.name or "") or "Design" in (r.name or "")]
target = builds[0] if builds else (runs[0] if runs else None)
if target:
    print(f"\n=== newest build run: {target.name}  id={target.id}  status={target.status} ===")
    print("inputs:", str(target.inputs)[:400])
    print("outputs:", str(target.outputs)[:1500])
    kids = list(c.list_runs(project_name=proj, trace_id=target.trace_id, limit=100))
    kids.sort(key=lambda r: r.start_time)
    print(f"\n{len(kids)} child runs:")
    for k in kids:
        dur = ""
        if k.end_time and k.start_time:
            dur = f"{(k.end_time-k.start_time).total_seconds():.0f}s"
        flag = "ERR" if k.error else "   "
        print(f"  {flag} {k.name[:34]:34} {k.status or '':8} {dur:>6}")
    # Dump validate/check nodes (the REAL issue list) + architect IR (block
    # names + STM32 lib_id actually chosen).
    print("\n=== validate / check / pass-or-fix node outputs (REAL issues) ===")
    for k in kids:
        if any(t in (k.name or "").lower() for t in ("check", "pass or fix", "clean")):
            print(f"\n--- {k.name} ---\n{str(k.outputs or k.error or '')[:1800]}")
    print("\n=== lib_id / stm32 / block-name mentions in any node ===")
    import re as _re
    seen = set()
    for k in kids:
        out = str(k.outputs or "")
        for m in _re.findall(r'(STM32[A-Za-z0-9]*|lib_id["\s:=]+[^",}]+|block_type["\s:=]+[^",}]+|"name"\s*:\s*"[A-Za-z0-9_]+")', out):
            if m not in seen:
                seen.add(m)
    for s in sorted(seen)[:60]:
        print("  ", s[:80])
