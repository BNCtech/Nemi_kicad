"""Per-build progress transport — a thread-safe bridge so the SYNC build
(running in a worker thread via build_circuit's gated asyncio.to_thread) can push
live "drew block 3/8" status frames to the chat WebSocket WITHOUT touching the
loop/ws directly.

Why this exists: the build graph nodes are synchronous and the architect uses the
sync anthropic client, so the build is offloaded to a worker thread to keep the
asyncio loop free (Phase 0). A worker thread must never call ws.send_json or
await directly. Instead the server registers the running loop + the SAME asyncio
queue its heartbeat consumer drains (`begin_turn`), and the worker calls `emit()`,
which schedules a queue put on the loop via call_soon_threadsafe — so EVERY frame
still goes out on the single consumer task (no concurrent-send races).

Everything is best-effort and gated by the caller: `emit()` is a silent no-op
unless a turn was registered, so importing/calling this module is harmless when
the progressive-status feature is off. Single-user localhost tool → one active
turn at a time; a module-level slot is sufficient and simplest.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

_lock = threading.Lock()
_turn: dict = {"loop": None, "queue": None, "session_id": None}


def begin_turn(loop: Any, queue: Any, session_id: Optional[str]) -> None:
    """Register the loop + heartbeat queue for the current chat turn so worker
    threads can stream status. Called by the server BEFORE the agent runs."""
    with _lock:
        _turn["loop"] = loop
        _turn["queue"] = queue
        _turn["session_id"] = session_id


def end_turn() -> None:
    """Clear the registration when the turn finishes (server `finally`)."""
    with _lock:
        _turn["loop"] = None
        _turn["queue"] = None
        _turn["session_id"] = None


def emit(text: str) -> None:
    """Push a `{kind:"status"}` frame to the chat from ANY thread. Best-effort:
    a no-op when no turn is registered (feature off / between turns), and never
    raises — a flaky sink must never break a build."""
    with _lock:
        loop = _turn["loop"]
        queue = _turn["queue"]
        sid = _turn["session_id"]
    if loop is None or queue is None:
        return
    frame = {"kind": "status", "text": text, "progress": True}
    if sid is not None:
        frame["session_id"] = sid
    try:
        loop.call_soon_threadsafe(queue.put_nowait, frame)
    except Exception:                       # noqa: BLE001 - never break the build
        pass
