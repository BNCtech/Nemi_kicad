"""End-to-end WS smoke test: drives the RUNNING server at ws://127.0.0.1:8765
the exact way the chat panel does — hello -> build prompt -> (preview) ->
confirm -> build_circuit -> done. Prints every frame and the final path.

Run:  python tests/_ws_smoke.py
"""
import asyncio
import json
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

import websockets

URL = "ws://127.0.0.1:8765/ws/chat"
SESSION = sys.argv[2] if len(sys.argv) > 2 else "smoke-test-001"
PROMPT = sys.argv[1] if len(sys.argv) > 1 else (
    "Build a simple 5V LED indicator circuit: a single red LED in series "
    "with a 330 ohm current-limiting resistor, connected between a +5V "
    "rail and ground.")

CONFIRM = "yes, build it with sensible defaults"

# Per-frame receive timeout (s). A flat 2-3 part build is usually < 90s.
RECV_TIMEOUT = 240


def short(v, n=400):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"... <+{len(s)-n} chars>"


async def drain_turn(ws, tag):
    """Read frames until this turn's `done`. Returns dict of collected info."""
    info = {"reply": "", "paths": [], "child_paths": [], "tools": [],
            "errors": [], "tool_results": []}
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[{tag}] TIMEOUT waiting for next frame")
            info["errors"].append("timeout")
            return info
        msg = json.loads(raw)
        kind = msg.get("kind")
        if kind in ("message_chunk",):
            continue  # streamed text; final 'reply' has the whole thing
        if kind == "tool_use":
            tn = msg.get("tool_name")
            info["tools"].append(tn)
            print(f"[{tag}] tool_use: {tn}  input={short(msg.get('tool_input'), 200)}")
        elif kind == "tool_result":
            res = msg.get("result") or {}
            info["tool_results"].append({msg.get("tool_name"): res})
            p = (res.get("path") or "") if isinstance(res, dict) else ""
            if p:
                info["paths"].append(p)
            cps = res.get("child_paths") if isinstance(res, dict) else None
            if isinstance(cps, list):
                info["child_paths"].extend(cps)
            print(f"[{tag}] tool_result[{msg.get('tool_name')}]: {short(res, 500)}")
        elif kind == "status":
            print(f"[{tag}] status: {msg.get('text')}")
        elif kind == "reply":
            info["reply"] = msg.get("text", "")
            print(f"[{tag}] reply: {short(info['reply'], 700)}")
        elif kind == "open_schematic":
            print(f"[{tag}] open_schematic: {msg.get('path')}")
            if msg.get("path"):
                info["paths"].append(msg["path"])
        elif kind == "error":
            info["errors"].append(msg.get("text"))
            print(f"[{tag}] ERROR: {msg.get('text')}")
        elif kind == "done":
            print(f"[{tag}] <done>")
            return info
        elif kind in ("ready", "pong", "confirm_buttons", "previews",
                      "page_summary", "message"):
            print(f"[{tag}] {kind}: {short(msg, 200)}")
        else:
            print(f"[{tag}] {kind}: {short(msg, 200)}")


async def main():
    print(f"connecting -> {URL}")
    async with websockets.connect(URL, max_size=None) as ws:
        # 1) hello
        await ws.send(json.dumps({"kind": "hello", "session_id": SESSION,
                                  "app": "schematic"}))
        # drain hello responses (ready + maybe page_summary) briefly
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                m = json.loads(raw)
                print(f"[hello] {m.get('kind')}: {short(m, 160)}")
                if m.get("kind") == "ready":
                    # page_summary may follow; grab one more non-blocking
                    try:
                        raw2 = await asyncio.wait_for(ws.recv(), timeout=4)
                        print(f"[hello] {json.loads(raw2).get('kind')}")
                    except asyncio.TimeoutError:
                        pass
                    break
        except asyncio.TimeoutError:
            pass

        # clear any prior history on this session so the preview->confirm
        # flow runs from scratch (not short-circuited by a stale preview)
        await ws.send(json.dumps({"kind": "reset", "session_id": SESSION}))
        try:
            await asyncio.wait_for(ws.recv(), timeout=4)
        except asyncio.TimeoutError:
            pass

        # 2) build prompt
        print("\n=== TURN 1: build request ===")
        await ws.send(json.dumps({"kind": "message", "session_id": SESSION,
                                  "app": "schematic", "text": PROMPT}))
        info = await drain_turn(ws, "T1")

        final = info
        # 3) confirm/clarify loop (max 3 follow-ups)
        for i in range(3):
            built = any("build_circuit" in t for t in final["tools"]) and final["paths"]
            if built:
                break
            # if it asked something / showed a preview, push it forward
            r = final["reply"].lower()
            needs_push = ("?" in r) or ("want me to build" in r) or (not final["tools"])
            if not needs_push:
                break
            print(f"\n=== TURN {i+2}: confirm -> '{CONFIRM}' ===")
            await ws.send(json.dumps({"kind": "message", "session_id": SESSION,
                                      "app": "schematic", "text": CONFIRM}))
            final = await drain_turn(ws, f"T{i+2}")
            # accumulate paths/tools
            info["tools"] += final["tools"]
            info["paths"] += final["paths"]
            info["child_paths"] += final["child_paths"]
            info["errors"] += final["errors"]

        print("\n" + "=" * 60)
        built = any("build_circuit" in t for t in info["tools"])
        paths = sorted(set(info["paths"]))
        print("build_circuit called :", built)
        print("schematic paths      :", paths or "(none)")
        print("child paths          :", sorted(set(info["child_paths"])) or "(none)")
        print("errors               :", info["errors"] or "(none)")
        ok = built and paths and not info["errors"]
        print("RESULT               :", "PASS" if ok else "CHECK")
        # emit machine-readable last line for the harness
        print("JSON_RESULT=" + json.dumps({"built": built, "paths": paths,
              "child_paths": sorted(set(info["child_paths"])),
              "errors": info["errors"], "ok": bool(ok)}))


if __name__ == "__main__":
    asyncio.run(main())
