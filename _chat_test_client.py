"""Ad-hoc Envil chat tester: connects to the live FastAPI WS like the
KiCad chat panel does, sends one circuit prompt, and prints the streamed
frames + final reply + generated path. Throwaway diagnostic script."""
import asyncio, json, sys, time
import websockets
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

URL = "ws://127.0.0.1:8765/ws/chat"
PROMPT = sys.argv[1] if len(sys.argv) > 1 else (
    "Design a 555 timer astable LED blinker at about 1 Hz, "
    "single 9V supply. Standard NE555 with the timing RC network and "
    "an LED with current-limiting resistor on the output."
)
SESSION = "chat-test-" + str(int(time.time()))

async def main():
    print(f"[client] connecting {URL}")
    async with websockets.connect(URL, max_size=None, open_timeout=15) as ws:
        await ws.send(json.dumps({"kind": "hello", "session_id": SESSION,
                                  "app": "schematic"}))
        await ws.send(json.dumps({"kind": "message", "session_id": SESSION,
                                  "app": "schematic", "text": PROMPT}))
        print(f"[client] sent prompt: {PROMPT[:70]}...")
        gen_path = None
        child_paths = []
        confirmed = False
        t0 = time.time()
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=600)
            except asyncio.TimeoutError:
                print("[client] TIMEOUT waiting for frame"); break
            try:
                m = json.loads(raw)
            except Exception:
                print("[raw]", raw[:200]); continue
            k = m.get("kind")
            el = int(time.time() - t0)
            if k == "status":
                print(f"  [{el}s] status: {m.get('text','')[:90]}")
            elif k == "tool_use":
                print(f"  [{el}s] tool_use: {m.get('tool_name')} "
                      f"input={json.dumps(m.get('tool_input',{}))[:120]}")
            elif k == "tool_result":
                r = m.get("result") or {}
                p = (r or {}).get("path") if isinstance(r, dict) else None
                if p: gen_path = p
                cps = (r or {}).get("child_paths") if isinstance(r, dict) else None
                if cps: child_paths = cps
                rs = json.dumps(r)[:160] if isinstance(r, (dict, list)) else str(r)[:160]
                print(f"  [{el}s] tool_result: {m.get('tool_name')} -> {rs}")
            elif k == "message_chunk":
                pass  # accumulates into final reply
            elif k == "page_summary":
                print(f"  [{el}s] page_summary: {m.get('state')} {m.get('title','')}")
            elif k == "reply":
                txt = m.get('text', '')
                print(f"\n[client] REPLY ({el}s):\n{txt}\n")
                low = txt.lower()
                # If the agent gated with a confirmation question and we
                # haven't yet confirmed, answer 'yes' and keep streaming.
                if (not confirmed and gen_path is None
                        and ("?" in txt or "want me" in low
                             or "shall i" in low or "build it" in low)):
                    confirmed = True
                    print("[client] -> auto-confirming: 'yes, build it'")
                    await ws.send(json.dumps({
                        "kind": "message", "session_id": SESSION,
                        "app": "schematic", "text": "Yes, build it."}))
                    t0 = time.time()
                    continue
                break
            elif k == "error":
                print(f"  [{el}s] ERROR: {m.get('text')}"); break
            elif k == "ready":
                print(f"  [{el}s] ready (app={m.get('app')})")
            else:
                print(f"  [{el}s] {k}: {json.dumps(m)[:120]}")
        print("[client] generated_path:", gen_path)
        if child_paths:
            print("[client] child_paths:", child_paths)
        return gen_path

if __name__ == "__main__":
    asyncio.run(main())
