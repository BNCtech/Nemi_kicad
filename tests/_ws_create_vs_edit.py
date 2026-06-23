"""LIVE Envil-chat test of the CREATE-vs-EDIT logic, driven over the real
WebSocket the KiCad panel uses.

- CREATE: connect with NO project open -> a build prompt should call
  build_circuit and produce a new project file.
- EDIT: reconnect WITH that file as the open project (schematic_file) ->
  an edit prompt should call apply_ops and must NOT rebuild from scratch.

Run (server must be up on :8765):  python tests/_ws_create_vs_edit.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "ws://127.0.0.1:8765/ws/chat"


async def _drain(ws, tag, timeout=300):
    tools, paths, reply, status = [], [], "", "done"
    t0 = time.time()
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"[{tag}] TIMEOUT")
            return tools, paths, reply, "timeout"
        m = json.loads(raw)
        k = m.get("kind")
        if k == "tool_use":
            tn = str(m.get("tool_name", "")).split("__")[-1]
            tools.append(tn)
            print(f"[{tag}] tool_use: {tn} {json.dumps(m.get('tool_input', {}))[:90]}")
        elif k == "tool_result":
            r = m.get("result") or {}
            p = r.get("path") if isinstance(r, dict) else None
            if p:
                paths.append(p)
            body = json.dumps(r)[:110] if isinstance(r, (dict, list)) else str(r)[:110]
            print(f"[{tag}] tool_result[{m.get('tool_name')}] -> {body}")
        elif k == "reply":
            reply = m.get("text", "")
            print(f"[{tag}] reply: {reply[:180]}")
        elif k == "status":
            print(f"[{tag}] status: {str(m.get('text', ''))[:80]}")
        elif k == "error":
            print(f"[{tag}] ERROR: {m.get('text')}")
            return tools, paths, reply, "error"
        elif k == "done":
            print(f"[{tag}] <done> ({int(time.time() - t0)}s)")
            return tools, paths, reply, "done"
        # ignore: message_chunk, ready, page_summary, confirm_buttons, previews


async def _hello(ws, sess, app, schematic_file=None):
    h = {"kind": "hello", "session_id": sess, "app": app}
    if schematic_file:
        h["schematic_file"] = schematic_file
    await ws.send(json.dumps(h))
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=6)
            if json.loads(raw).get("kind") == "ready":
                break
    except asyncio.TimeoutError:
        pass


async def main() -> int:
    created_path = None

    # ===== CREATE: no project open =====
    sess = "cve-create-" + str(int(time.time()))
    print("\n===== CREATE (no project open) =====")
    async with websockets.connect(URL, max_size=None, open_timeout=15) as ws:
        await _hello(ws, sess, "schematic")
        await ws.send(json.dumps({"kind": "reset", "session_id": sess}))
        try:
            await asyncio.wait_for(ws.recv(), timeout=3)
        except asyncio.TimeoutError:
            pass
        await ws.send(json.dumps({"kind": "message", "session_id": sess, "app": "schematic",
            "text": "Build a simple 5V red LED indicator: one red LED in series with a "
                    "330 ohm resistor between +5V and GND."}))
        tools, paths, reply, st = await _drain(ws, "CREATE")
        tries = 0
        while (st == "done" and not any("build_circuit" in t for t in tools) and tries < 2
               and ("?" in reply or "build it" in reply.lower() or "want me" in reply.lower())):
            tries += 1
            print("[CREATE] -> auto-confirm 'yes, build it'")
            await ws.send(json.dumps({"kind": "message", "session_id": sess, "app": "schematic",
                "text": "Yes, build it with sensible defaults."}))
            t2, p2, reply, st = await _drain(ws, f"CREATE+{tries}")
            tools += t2
            paths += p2
        created_path = paths[0] if paths else None

    create_ok = bool(any("build_circuit" in t for t in tools) and created_path)
    print(f"\n[CREATE] build_circuit called={any('build_circuit' in t for t in tools)} "
          f"path={created_path}")

    # ===== EDIT: project open =====
    edit_ok = False
    if created_path:
        sess2 = "cve-edit-" + str(int(time.time()))
        print(f"\n===== EDIT (project open: {Path(created_path).name}) =====")
        async with websockets.connect(URL, max_size=None, open_timeout=15) as ws:
            await _hello(ws, sess2, "schematic", schematic_file=created_path)
            await ws.send(json.dumps({"kind": "message", "session_id": sess2, "app": "schematic",
                "schematic_file": created_path,
                "text": "Change the 330 ohm resistor to 1k."}))
            tools, paths, reply, st = await _drain(ws, "EDIT")
        edited = any(t == "apply_ops" for t in tools)
        rebuilt = any("build_circuit" in t for t in tools)
        edit_ok = edited and not rebuilt
        print(f"\n[EDIT] apply_ops called={edited}  build_circuit(rebuild) called={rebuilt}")
    else:
        print("[EDIT] skipped — no created_path from CREATE step")

    print("\n==== VERDICT ====")
    print(("PASS" if create_ok else "FAIL"), "CREATE (no project) -> build_circuit made a new project")
    print(("PASS" if edit_ok else "FAIL"), "EDIT (project open) -> apply_ops edited, no rebuild")
    ok = create_ok and edit_ok
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
