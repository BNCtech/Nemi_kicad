import asyncio
import json
import sys

import websockets


async def main(url: str = "ws://127.0.0.1:8765/ws/chat") -> int:
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "ping", "session_id": None}))
        reply = json.loads(await ws.recv())
        print("ping ->", reply)
        assert reply.get("type") == "pong", f"expected pong, got {reply}"
        sid = reply.get("session_id")

        await ws.send(json.dumps({"type": "set_project", "path": "F:/Ki_CAD/test", "session_id": sid}))
        reply = json.loads(await ws.recv())
        print("set_project ->", reply)
        assert reply.get("type") == "project_ack"

        await ws.send(json.dumps({"type": "clear_circuit", "session_id": sid}))
        reply = json.loads(await ws.recv())
        print("clear_circuit ->", reply)
        assert reply.get("type") == "status"

        await ws.send(json.dumps({"type": "garbage", "session_id": sid}))
        reply = json.loads(await ws.recv())
        print("garbage ->", reply)
        assert reply.get("type") == "error"

    print("OK — WebSocket dispatch verified")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
