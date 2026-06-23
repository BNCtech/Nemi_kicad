import asyncio
import copy
import json
import os
import re as _re_pro
import shutil
import socket
import struct
import sys
import tempfile
import uuid as _uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Windows ships a cp1252 console; printing Claude's reply text (which often
# contains '→', '—', '°' etc.) crashes with UnicodeEncodeError and the
# exception bubbles up into the WebSocket handler as a "server error".
# Force stdout/stderr to UTF-8 so diagnostic prints can never break a request.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ._config_loader import load as _load_config
<<<<<<< Updated upstream
from .chat import (
    CHAT_SYSTEM_PROMPT,
    _count_component_add_ops,
    _count_connectivity_ops,
    _extract_json,
    run_connectivity_retry,
)
=======
from .chat import CHAT_SYSTEM_PROMPT, _extract_json
>>>>>>> Stashed changes
from .claude_client import ClaudeClient
from .schematic_extractor import SchematicExtractor, format_dump_with_context
from .schematic_modifier import SchematicDocument, apply_operation


<<<<<<< Updated upstream
def _schematic_edge_count(schematic_path: str) -> Tuple[int, int]:
    """Build the layout connectivity graph and return (edge_count, node_count).
    Pre-flight gate for the layout post-step: if a schematic has placed
    components but no edges, the layout pipeline produces a dead .kicad_sch
    (no wires, no labels). Fail fast instead of pretending success.

    Returns (-1, -1) if the graph build itself raises — caller should treat
    that as "skip the gate, let layout try anyway"."""
    try:
        from kicad_claude.layout.connectivity_graph import build_graph
        g = build_graph(schematic_path)
        return (g.number_of_edges(), g.number_of_nodes())
    except Exception as exc:
        print(f"[chat] connectivity gate: build_graph raised "
              f"({type(exc).__name__}: {exc})", flush=True)
        return (-1, -1)


=======
>>>>>>> Stashed changes
HISTORY_TURNS = int(_load_config("conventions").get("chat", {}).get("history_turns", 12))


# Map each basic_checks check_id to (human-readable title, category).
# Keeps the chat panel free of raw [CHECK_ID] tokens.
_CHECK_INFO: Dict[str, Tuple[str, str]] = {
    # Grid / placement
    "GRID_COMPONENT":          ("Component off grid", "Grid alignment"),
    "GRID_LABEL":              ("Label off grid", "Grid alignment"),
    "GRID_WIRE":               ("Wire endpoint off grid", "Grid alignment"),
    "GEOM_DIAGONAL":           ("Diagonal wire segment", "Routing"),
    "GEOM_WIRE_OVERLAP":       ("Overlapping wires", "Routing"),
    "GEOM_WIRE_THRU_BODY":     ("Wire crosses through symbol body", "Routing"),
    "GEOM_LABEL_CONFLICT":     ("Conflicting labels on same net", "Labels"),
    "GEOM_LABEL_REDUNDANT":    ("Redundant label", "Labels"),
    "GEOM_LABEL_OVER_WIRE":    ("Label sits on a wire mid-segment", "Labels"),
    "GEOM_SYMBOL_PROXIMITY":   ("Symbols too close (placement clash)", "Placement"),
    "GEOM_SYMBOL_OVERLAP":     ("Symbol bodies overlap", "Placement"),
    # References / values
    "REF_MISSING":             ("Reference designator missing", "References"),
    "REF_UNANNOTATED":         ("Component not annotated (e.g. R?)", "References"),
    "REF_INVALID_FORMAT":      ("Reference format invalid", "References"),
    "REF_DUPLICATE":           ("Duplicate reference designator", "References"),
    "VALUE_MISSING":           ("Component value missing", "BOM / Values"),
    "VALUE_INVALID":           ("Component value invalid", "BOM / Values"),
    "LABEL_FORMAT":            ("Label name format invalid", "Labels"),
    # Net integrity / ERC-ish
    "NET_DANGLING_LABEL":      ("Label is dangling (no net)", "Connectivity"),
    "NET_JUNCTION_MISSING":    ("Junction missing at wire crossing", "Connectivity"),
    "MISSING_DECOUPLING":      ("Missing decoupling capacitor on power pin", "Power integrity"),
}


_PASSIVE_REF_PREFIXES = ("R", "C", "L", "D", "Q", "TP", "FB", "MH", "F", "Y", "X")


def _identify_circuit(components: List[Dict[str, Any]]) -> str:
    """Return a concise one-line summary of the active ICs on the sheet —
    e.g. "NE555 + LM2596" — so the user instantly sees what circuit is on
    the page without paying for a Claude call.

    Skips passives (R/C/L/D/Q/TP/F/Y/X), power-port symbols, mounting holes,
    and PWR_FLAGs. Dedupes by value, preserves first-seen order, and adds
    a "Nx" prefix when the same active part appears more than once.
    """
    seen: Dict[str, int] = {}
    order: List[str] = []
    for c in components:
        lib = (c.get("lib_id") or "")
        ref = (c.get("reference") or "")
        val = ((c.get("value") or "") or "").strip()
        if lib.startswith("power:") or ref.startswith("#"):
            continue
        # passive refdes like R1, C12, D3, Q4 — first char is a passive
        # letter AND the rest starts with a digit (avoids skipping U1/IC1).
        if ref and ref[0] in _PASSIVE_REF_PREFIXES and ref[1:2].isdigit():
            continue
        if not val or val.lower() in {"~", "?", "x"}:
            val = lib.split(":")[-1] if lib else ""
        if not val:
            continue
        if val not in seen:
            order.append(val)
            seen[val] = 0
        seen[val] += 1
    if not order:
        return ""
    return " + ".join((f"{seen[v]}× {v}" if seen[v] > 1 else v) for v in order)


def _humanize_issue(it: Dict[str, Any]) -> Dict[str, Any]:
    """Return a chat-friendly dict for one basic_checks issue."""
    cid = it.get("check") or ""
    title, category = _CHECK_INFO.get(cid, (cid.replace("_", " ").title(), "Other"))
    return {
        "id":       cid,
        "severity": it.get("severity") or "info",
        "title":    title,
        "category": category,
        "refs":     it.get("refs") or "",
        "message":  it.get("message") or "",
    }


def _build_auto_summary(room: "ChatRoom") -> Optional[Dict[str, Any]]:
    """Build a compact summary of the currently attached schematic so the chat
    panel can show what's in the file the moment the user opens it, before
    they type anything. Returns None if no schematic is attached or the read
    fails — callers should skip the send in that case so the panel doesn't
    show an empty summary card.
    """
    if not room.schematic_path:
        return None
    try:
        from . import basic_checks, hierarchy
    except Exception:
        return None
    try:
        components = hierarchy.aggregate_components(room.schematic_path)
        sheets = hierarchy.sheet_count(room.schematic_path)
    except Exception:
        return None

    # Power rails detected from power-port symbols (universal across libs).
    rails = sorted({
        c.get("value", "")
        for c in components
        if (c.get("lib_id", "") or "").startswith("power:") and c.get("value")
    })

    # Real physical part count (dedupe multi-unit ICs by refdes).
    physical_refs = {
        c.get("reference") for c in components
        if c.get("reference") and not (c.get("lib_id", "") or "").startswith("power:")
        and not (c.get("reference", "") or "").startswith("#")
    }

    # Production-grade pass — surface critical / high / medium so the user sees
    # everything that matters before sign-off, not just the worst.
    issues: List[Dict[str, Any]] = []
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    try:
        report = basic_checks.run_all(room.schematic_path)
        for it in report.get("issues", []):
            sev = it.get("severity") or "info"
            counts[sev] = counts.get(sev, 0) + 1
            if sev in ("critical", "high", "medium"):
                issues.append(_humanize_issue(it))
    except Exception:
        pass

    # Production-readiness verdict — drives the colored status badge in the UI.
    if counts.get("critical", 0) > 0:
        readiness = "not_ready"
    elif counts.get("high", 0) > 0 or counts.get("medium", 0) > 3:
        readiness = "needs_review"
    elif counts.get("medium", 0) > 0:
        readiness = "minor_polish"
    else:
        readiness = "ready"

    name = Path(room.schematic_path).name
    return {
        "file":        name,
        "sheets":      sheets,
        "parts":       len(physical_refs),
        "power_rails": rails,
        "circuit":     _identify_circuit(components),
        "issues":      issues,
        "counts":      counts,
        "readiness":   readiness,
    }


class ChatRoom:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.history: List[Dict[str, str]] = []
        self._client: Optional[ClaudeClient] = None
        self.project_path: Optional[str] = None
        self.schematic_path: Optional[str] = None
        self.doc: Optional[SchematicDocument] = None
        self.pending_ops: Optional[List[Dict[str, Any]]] = None
        self.pending_message: str = ""

    @property
    def client(self) -> ClaudeClient:
        if self._client is None:
            self._client = ClaudeClient()
        return self._client

    def attach_schematic(self, schematic_file: str) -> None:
        if not schematic_file or schematic_file == self.schematic_path:
            return
        if not Path(schematic_file).exists():
            return
        self.schematic_path = schematic_file
        try:
            self.doc = SchematicDocument(schematic_file)
        except Exception:
            self.doc = None

    def current_dump(self) -> str:
        if not self.schematic_path:
            return "(no schematic loaded)"
        # Delegated to schematic_extractor.format_dump_with_context so the
        # server and the chat REPL stay in lockstep on what context the AI
        # receives (pin endpoints + detected defects).
        from .schematic_extractor import format_dump_with_context
        return format_dump_with_context(self.schematic_path)


SESSIONS: Dict[str, ChatRoom] = {}
IPC_CLIENTS: Set[asyncio.StreamWriter] = set()
# When _ipc_broadcast fires before eeschema has reconnected (common if the
# server restarted while eeschema kept running), payloads are queued here
# and replayed to the next client that connects. Bounded so a long-offline
# eeschema can't grow this without limit.
IPC_PENDING: deque = deque(maxlen=64)


def _get_room(session_id: Optional[str]) -> ChatRoom:
    if session_id and session_id in SESSIONS:
        return SESSIONS[session_id]
    sid = session_id or _uuid.uuid4().hex
    SESSIONS[sid] = ChatRoom(sid)
    return SESSIONS[sid]


async def _ipc_send(writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    writer.write(struct.pack(">I", len(body)) + body)
    await writer.drain()


async def _ipc_broadcast(payload: Dict[str, Any]) -> None:
    if not IPC_CLIENTS:
        # eeschema isn't connected (typically because it was launched before
        # this server instance and is still pointing at a stale ipc_port.txt).
        # Queue the payload so the next connecting client replays it — the
        # user gets the reload without having to manually File > Revert.
        IPC_PENDING.append(payload)
        print(
            f"[IPC] queued action={payload.get('action')!r} — 0 eeschema "
            f"clients connected; will replay on next connect "
            f"(queue {len(IPC_PENDING)}/{IPC_PENDING.maxlen}).",
            flush=True,
        )
        return
    dead = []
    for w in list(IPC_CLIENTS):
        try:
            await _ipc_send(w, payload)
        except Exception:
            dead.append(w)
    for w in dead:
        IPC_CLIENTS.discard(w)


async def _ipc_handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    addr = writer.get_extra_info("peername")
    print(f"[IPC] eeschema connected from {addr}", flush=True)
    IPC_CLIENTS.add(writer)
    if IPC_PENDING:
        backlog = list(IPC_PENDING)
        IPC_PENDING.clear()
        delivered = 0
        for queued in backlog:
            try:
                await _ipc_send(writer, queued)
                delivered += 1
            except Exception:
                # Writer died mid-replay. Re-queue the unsent tail (including
                # the failed item) so the NEXT client connecting gets them.
                IPC_PENDING.extendleft(reversed(backlog[delivered:]))
                break
        print(f"[IPC] replayed {delivered}/{len(backlog)} queued messages to {addr}", flush=True)
    try:
        while True:
            length_bytes = await reader.readexactly(4)
            (length,) = struct.unpack(">I", length_bytes)
            if length > 1024 * 1024:
                print(f"[IPC] oversized message ({length} bytes), dropping connection", flush=True)
                break
            payload = await reader.readexactly(length)
            try:
                msg = json.loads(payload)
                print(f"[IPC] from eeschema: type={msg.get('type')}", flush=True)
            except json.JSONDecodeError:
                print("[IPC] received non-JSON payload, ignoring", flush=True)
    except asyncio.IncompleteReadError:
        pass
    finally:
        IPC_CLIENTS.discard(writer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        print(f"[IPC] eeschema disconnected: {addr}", flush=True)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _start_ipc(port_files: List[Path]) -> int:
    # Honour an explicit IPC_PORT pin so server restarts reuse the same port
    # and the running eeschema's IPC link survives. eeschema reads ipc_port.txt
    # only at startup, so an ephemeral port forces a manual eeschema restart on
    # every server restart. Fall back to ephemeral when IPC_PORT is unset / 0.
    pinned = os.environ.get("IPC_PORT", "").strip()
    try:
        port = int(pinned) if pinned else 0
    except ValueError:
        port = 0
    if port <= 0:
        port = _free_port()
    try:
        # reuse_address=True lets the new server bind through a TIME_WAIT
        # left by the previous server — without it, restarts within ~4 min
        # of a clean shutdown collide and fall back to ephemeral, defeating
        # the entire purpose of pinning.
        server = await asyncio.start_server(_ipc_handle, "127.0.0.1", port,
                                            reuse_address=True)
    except OSError as e:
        # Pinned port held by a genuinely live process. Fall back to
        # ephemeral so the server still starts; eeschema will need a one-
        # time restart this time.
        print(f"[IPC] WARNING: pinned port {port} unavailable ({e}); "
              f"falling back to ephemeral", flush=True)
        port = _free_port()
        server = await asyncio.start_server(_ipc_handle, "127.0.0.1", port,
                                            reuse_address=True)
    for p in port_files:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(port), encoding="utf-8")
            print(f"[IPC] wrote port {port} -> {p}", flush=True)
        except Exception as e:
            print(f"[IPC] could not write {p}: {e}", flush=True)
    print(f"[IPC] listening on 127.0.0.1:{port}", flush=True)
    asyncio.create_task(server.serve_forever())
    return port


_ipc_port_files: List[Path] = []


@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        from ._lib_symbol_cache import (
            ensure_envil_generated_registered,
            ensure_kicad_common_has_envil_lib_root,
            ensure_lib_paths_portable,
        )
        for fn in (
            ensure_lib_paths_portable,
            ensure_kicad_common_has_envil_lib_root,
            ensure_envil_generated_registered,
        ):
            status = fn()
            if status:
                print(f"[bootstrap] {status}", flush=True)
    except Exception as exc:
        # Bootstrap is best-effort; never block server start.
        print(f"[bootstrap] sym-lib-table bootstrap skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)
    await _start_ipc(_ipc_port_files)
    yield


app = FastAPI(lifespan=_lifespan)


@app.get("/")
async def root():
    return {
        "service": "kicad-claude",
        "sessions": list(SESSIONS.keys()),
        "ipc_clients": len(IPC_CLIENTS),
    }


ATTACHMENT_DIR = Path(tempfile.gettempdir()) / "envil_uploads"
ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
ATTACHMENTS: Dict[str, Dict[str, Any]] = {}


def _classify(filename: str, content_type: Optional[str]) -> str:
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "image"
    if ct == "application/pdf" or filename.lower().endswith(".pdf"):
        return "pdf"
    return "other"


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), session_id: str = Form(default="")):
    blob = await file.read()
    aid = _uuid.uuid4().hex
    safe_name = os.path.basename(file.filename or "upload.bin")
    saved_path = ATTACHMENT_DIR / f"{aid}_{safe_name}"
    saved_path.write_bytes(blob)
    kind = _classify(safe_name, file.content_type)
    ATTACHMENTS[aid] = {
        "id": aid,
        "filename": safe_name,
        "path": str(saved_path),
        "size": len(blob),
        "content_type": file.content_type or "",
        "kind": kind,
        "session_id": session_id,
    }
    return JSONResponse({
        "attachment_id": aid,
        "filename": safe_name,
        "size": len(blob),
        "kind": kind,
    })


@app.post("/api/open_in_eeschema")
async def open_in_eeschema(payload: Dict[str, Any]):
    """Broadcast open_file + revert to any connected eeschema client so an
    externally-generated .kicad_sch (e.g. produced by the kicad_layout
    pipeline) loads without the user reaching for File > Open. Mirrors the
    sequence chat-apply uses (open_file first to attach a new file, revert
    after to force-reload an already-open one — exactly one no-ops)."""
    path = str((payload or {}).get("path") or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "path required"}, status_code=400)
    p = Path(path)
    # Reject anything that isn't a .kicad_sch — without this an external
    # caller (or a careless probe) can broadcast any file path to eeschema
    # and it will pop a "not a KiCad schematic file" error dialog.
    if p.suffix.lower() != ".kicad_sch":
        return JSONResponse(
            {"ok": False, "error": f"only .kicad_sch accepted; got: {p.suffix}"},
            status_code=400,
        )
    if not p.exists():
        return JSONResponse({"ok": False, "error": f"file not found: {path}"},
                             status_code=404)
    await _ipc_broadcast({"action": "open_file", "data": {"path": path}})
    await _ipc_broadcast({"action": "revert",    "data": {"path": path}})
    return JSONResponse({"ok": True, "path": path,
                          "ipc_clients": len(IPC_CLIENTS)})


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"kind": "error", "text": "bad JSON"})
                continue

            kind = msg.get("kind")
            room = _get_room(msg.get("session_id"))

            if kind == "ping":
                await ws.send_json({"kind": "pong", "session_id": room.session_id})
                continue

            if kind == "hello":
                pp = msg.get("project_path") or ""
                sf = msg.get("schematic_file") or ""
                if pp:
                    room.project_path = pp
                if sf:
                    room.attach_schematic(sf)
                await ws.send_json({"kind": "ready", "session_id": room.session_id})
                # Auto-summary: as soon as the panel connects to a real schematic,
                # send back a snapshot so the AI panel can show what's in the file
                # WITHOUT requiring the user to type anything first.
                summary = _build_auto_summary(room)
                if summary:
                    await ws.send_json({"kind": "schematic_summary",
                                        "session_id": room.session_id,
                                        **summary})
                continue

            if kind == "reset":
                room.history.clear()
                room.pending_ops = None
                await ws.send_json({"kind": "status", "text": "Conversation reset.", "session_id": room.session_id})
                continue

            if kind == "approve":
                print(f"[chat] approve received; pending_ops={len(room.pending_ops or [])}", flush=True)
                await _apply_pending(ws, room)
                continue

            if kind == "reject":
                room.pending_ops = None
                room.pending_message = ""
                await ws.send_json({"kind": "status", "text": "Rejected.", "session_id": room.session_id})
                continue

            if kind == "message":
                sf = msg.get("schematic_file") or ""
                pp = msg.get("project_path") or ""
                if pp:
                    room.project_path = pp
                if sf:
                    room.attach_schematic(sf)
                attachments = msg.get("attachments") or []
                user_text = msg.get("text", "")
                print(f"[chat] message: text={user_text[:80]!r}{'...' if len(user_text)>80 else ''} "
                      f"schem={sf!r}", flush=True)
                await _stream_turn(ws, room, user_text, attachments)
                continue

            await ws.send_json({"kind": "error", "text": f"unknown kind: {kind}", "session_id": room.session_id})

    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"kind": "error", "text": f"server error: {e}"})
        except Exception:
            pass


def _ensure_kicad_pro(chat_path: Path) -> Optional[str]:
    """Ensure a valid .kicad_pro sits alongside the schematic. If the sibling
    project file is missing OR is a stub (≤10 bytes — typically just '{}'),
    write a minimal valid project linking the schematic's root UUID.

    Universal — works for any project. Without this, envilcad's project
    manager sees an empty .kicad_pro and asks the user to create
    `untitled.kicad_sch`, ignoring the real schematic.

    Returns the path written (str) or None if no action was needed."""
    pro_path = chat_path.with_suffix(".kicad_pro")
    try:
        size = pro_path.stat().st_size if pro_path.exists() else 0
    except OSError:
        size = 0
    if size > 10:
        return None  # already populated, don't touch

    # Pull the root UUID from the .kicad_sch — that's what links the
    # project file to the schematic in eeschema's project manager.
    try:
        text = chat_path.read_text(encoding="utf-8")
        m = _re_pro.search(r'\(uuid\s+"?([0-9a-f-]+)"?\)', text)
        root_uuid = m.group(1) if m else None
    except Exception:
        root_uuid = None
    if not root_uuid:
        return None  # can't construct a valid link without root UUID

    project = {
        "meta": {"filename": pro_path.name, "version": 1},
        "schematic": {
            "legacy_lib_dir": "",
            "legacy_lib_list": [],
            "sheets": [[root_uuid, ""]],
        },
        "version": 20260306,
    }
    try:
        pro_path.write_text(json.dumps(project, indent=2), encoding="utf-8")
        return str(pro_path)
    except OSError:
        return None


def _apply_ops_blocking(doc: SchematicDocument, ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Synchronous apply + save. Runs in a worker thread via asyncio.to_thread
    so the event loop stays responsive to WebSocket pings during large applies.

    Atomicity: if any op raises, the doc is rolled back to its pre-batch state
    so the next apply doesn't see half-mutated in-memory data. ok=False return
    values (graceful per-op failures) do NOT trigger rollback — only raised
    exceptions do.
    """
    pre_tree = copy.deepcopy(doc.tree)
    pre_history = list(doc._history)
    results: List[Dict[str, Any]] = []
    try:
        for op in ops:
            r = apply_operation(doc, op)
            results.append({"op": op.get("op") or op.get("type"), **r})
        if any(r.get("ok") for r in results):
            doc.save()
        return results
    except Exception as exc:
        doc.tree = pre_tree
        doc._history = pre_history
        results.append({
            "op": "<rollback>",
            "ok": False,
            "error": f"batch aborted after {len(results)} ops: {exc}",
        })
        return results


async def _apply_pending(ws: WebSocket, room: ChatRoom) -> None:
    if not room.pending_ops:
        await ws.send_json({"kind": "error", "text": "no pending action", "session_id": room.session_id})
        return
    if not room.doc:
        await ws.send_json(
            {"kind": "error", "text": "no schematic attached; cannot apply", "session_id": room.session_id}
        )
        room.pending_ops = None
        return

    ops = room.pending_ops
    # Hand the synchronous apply + file-write to a worker thread; otherwise a
    # 50+ op batch (each touching the library cache + s-expr writer) can block
    # the event loop for seconds and the WebSocket times out before the user
    # sees an "applied" message.
    results = await asyncio.to_thread(_apply_ops_blocking, room.doc, ops)
    applied = sum(1 for r in results if r.get("ok"))
    chat_path = Path(str(room.doc.path))
    display_path = chat_path  # default: show whatever chat wrote
<<<<<<< Updated upstream

    # Two-phase recovery for components-placed-but-disconnected sheets.
    # Phase 1: deterministic geometric snap — moves off-pin wire endpoints
    # onto the nearest pin tip (rotation-mismatch class).
    # Phase 2: connectivity-only Claude retry — fires ONLY when the snap
    # left the graph with zero edges (no wires existed, or too-far-to-snap).
    # Both phases are hard-capped at one pass per turn.
    components_first = _count_component_add_ops(ops)
    snap_stats: Optional[Dict[str, Any]] = None
    retry_attempted = False
    retry_applied = 0
    retry_total = 0
    edges_after_snap = -1
    if (applied and components_first >= 3
            and not os.environ.get("ENVIL_DISABLE_ORPHAN_RETRY", "").strip()):
        # --- Phase 1: snap -------------------------------------------------
        try:
            from kicad_claude.connectivity_normalize import (
                normalize_wire_endpoints,
            )
            snap_stats = await asyncio.to_thread(
                normalize_wire_endpoints, room.doc,
            )
            edges_after_snap = snap_stats.get("edges_after", -1)
            print(f"[chat] snap: snapped {snap_stats.get('endpoints_snapped',0)}"
                  f"/{snap_stats.get('endpoints_total',0)} endpoints; "
                  f"edges {snap_stats.get('edges_before','?')}→"
                  f"{edges_after_snap}", flush=True)
        except Exception as exc:
            print(f"[chat] snap: raised ({type(exc).__name__}: {exc})",
                  flush=True)

        # --- Phase 2: retry if still disconnected --------------------------
        if edges_after_snap == 0:
            retry_attempted = True
            await ws.send_json({
                "kind": "status",
                "session_id": room.session_id,
                "text": (
                    f"Placed {components_first} component(s); snap recovered "
                    f"0 edges — re-asking Claude for connectivity only..."
                ),
            })
            try:
                retry_ops = await asyncio.to_thread(
                    run_connectivity_retry, room.client, str(chat_path),
                )
            except Exception as exc:
                print(f"[chat] orphan-retry: helper raised "
                      f"({type(exc).__name__}: {exc})", flush=True)
                retry_ops = None
            if retry_ops:
                retry_results = await asyncio.to_thread(
                    _apply_ops_blocking, room.doc, retry_ops,
                )
                retry_applied = sum(1 for r in retry_results if r.get("ok"))
                retry_total = len(retry_results)
                results.extend(retry_results)
                applied += retry_applied
                print(f"[chat] orphan-retry: applied "
                      f"{retry_applied}/{retry_total} connectivity ops",
                      flush=True)
                # Snap one more time — retry-emitted wires may need it too.
                try:
                    snap_stats_after = await asyncio.to_thread(
                        normalize_wire_endpoints, room.doc,
                    )
                    print(f"[chat] snap-after-retry: snapped "
                          f"{snap_stats_after.get('endpoints_snapped',0)} "
                          f"endpoints; edges={snap_stats_after.get('edges_after','?')}",
                          flush=True)
                except Exception as exc:
                    print(f"[chat] snap-after-retry: raised "
                          f"({type(exc).__name__}: {exc})", flush=True)
            else:
                print("[chat] orphan-retry: no usable ops returned", flush=True)

    if applied:
        # Heal a stub / missing .kicad_pro before anything else. Without
        # this, envilcad's project manager can't find the schematic and
        # prompts "untitled.kicad_sch does not exist; create it?". The
        # function is a no-op if the project file is already populated.
        try:
            healed_pro = _ensure_kicad_pro(chat_path)
            if healed_pro:
                print(f"[chat] healed stub .kicad_pro → {Path(healed_pro).name}",
                      flush=True)
        except Exception as _pro_exc:
            print(f"[chat] kicad_pro heal skipped ({type(_pro_exc).__name__}: "
                  f"{_pro_exc})", flush=True)
        # Step 2: post-process through the kicad_layout pipeline so the new
        # placer / router / labels / dynamic zone titles / pin-number layout
        # take effect. Writes a sibling `<name>_layout.kicad_sch` next to the
        # chat-edited file; the original is left untouched as a fallback.
        # The pipeline is best-effort — any exception or quality.error count
        # falls back to opening the chat's own output (zero regression risk).
        # Disable via ENVIL_LAYOUT_POST=0 if needed.
        layout_post_enabled = os.environ.get(
            "ENVIL_LAYOUT_POST", "1").strip() not in {"0", "false", "no", ""}
        # Fail-fast gate: if the schematic on disk has no connectivity edges,
        # the layout pipeline will produce a placed-but-electrically-dead
        # sheet (every net is a single floating pin). Skip the post-step
        # entirely and tell the user, instead of pretending success.
        if layout_post_enabled:
            edge_count, node_count = await asyncio.to_thread(
                _schematic_edge_count, str(chat_path),
            )
            if edge_count == 0 and node_count >= 3:
                layout_post_enabled = False
                msg = (
                    f"Layout pipeline skipped: schematic has {node_count} "
                    f"components but no electrical connectivity (0 edges "
                    f"in net graph). The chat output is on disk but cannot "
                    f"be auto-routed. Re-prompt with explicit wires / labels."
                )
                print(f"[chat] layout GATE ABORTED: {msg}", flush=True)
                await ws.send_json({
                    "kind": "warning",
                    "session_id": room.session_id,
                    "text": msg,
                })
        if layout_post_enabled:
            try:
                from kicad_claude.layout.api import run_layout as _run_layout
                from kicad_claude.layout import load_config as _layout_load_config
                # Respect hierarchical_split.enabled in layout_config.json.
                # Falls back to skip=True (single-sheet) only when the config
                # entry is missing or explicitly disabled — never hardcoded.
                try:
                    _h_cfg = _layout_load_config("layout_config").get(
                                "hierarchical_split") or {}
                    skip_h = not bool(_h_cfg.get("enabled", False))
                except Exception:
                    skip_h = True
                layout_out_dir = chat_path.parent / "_layout"
                layout_result = await asyncio.to_thread(
                    _run_layout, str(chat_path), str(layout_out_dir),
                    skip_hierarchical=skip_h,
                    skip_quality=False,
                    auto_display=False,       # we own the broadcast below
                )
                if not layout_result.has_errors:
                    # Overwrite the chat-written sheet with the FLAT laid-out
                    # version so subsequent chat-apply passes operate on a
                    # consistent base sheet (the chat agent edits chat_path
                    # in place — incremental edits need it to be the latest
                    # full schematic).
                    layout_p = Path(layout_result.schematic)
                    import stat as _stat
                    shutil.copy(layout_p, chat_path)
                    try:
                        chat_path.chmod(chat_path.stat().st_mode
                                         | _stat.S_IWUSR | _stat.S_IWGRP
                                         | _stat.S_IWOTH)
                    except OSError:
                        pass
                    # If hierarchy fired, point envilcad at the PARENT sheet
                    # instead of the flat one — the user prompted a circuit
                    # that crossed the hierarchical threshold, they should
                    # SEE the multi-sheet view. chat_path stays as the flat
                    # base for back-compat with future chat edits.
                    h_info = (layout_result.hierarchical or {})
                    h_parent = h_info.get("parent_path")
                    if h_parent and Path(h_parent).exists():
                        display_path = Path(h_parent)
                        try:
                            display_path.chmod(display_path.stat().st_mode
                                                | _stat.S_IWUSR | _stat.S_IWGRP
                                                | _stat.S_IWOTH)
                        except OSError:
                            pass
                        print(f"[chat] hierarchical view → {display_path.name} "
                              f"(children={h_info.get('stats', {}).get('children', '?')})",
                              flush=True)
                    else:
                        print(f"[chat] layout post-step ok → overwrote "
                              f"{chat_path.name} from {layout_p.name} "
                              f"(warnings={layout_result.quality_totals.get('warning', 0)})",
                              flush=True)
                else:
                    print(f"[chat] layout post-step had errors; "
                          f"falling back to chat output {chat_path.name}",
                          flush=True)
            except Exception as _layout_exc:
                # Never let the layout step kill chat-apply. Surface the
                # cause to the server log so we can investigate, then fall
                # through to the chat-only display path.
                print(f"[chat] layout post-step failed "
                      f"({type(_layout_exc).__name__}: {_layout_exc}); "
                      f"falling back to {chat_path.name}", flush=True)

=======
    if applied:
        # Heal a stub / missing .kicad_pro before anything else. Without
        # this, envilcad's project manager can't find the schematic and
        # prompts "untitled.kicad_sch does not exist; create it?". The
        # function is a no-op if the project file is already populated.
        try:
            healed_pro = _ensure_kicad_pro(chat_path)
            if healed_pro:
                print(f"[chat] healed stub .kicad_pro → {Path(healed_pro).name}",
                      flush=True)
        except Exception as _pro_exc:
            print(f"[chat] kicad_pro heal skipped ({type(_pro_exc).__name__}: "
                  f"{_pro_exc})", flush=True)
        # Step 2: post-process through the kicad_layout pipeline so the new
        # placer / router / labels / dynamic zone titles / pin-number layout
        # take effect. Writes a sibling `<name>_layout.kicad_sch` next to the
        # chat-edited file; the original is left untouched as a fallback.
        # The pipeline is best-effort — any exception or quality.error count
        # falls back to opening the chat's own output (zero regression risk).
        # Disable via ENVIL_LAYOUT_POST=0 if needed.
        layout_post_enabled = os.environ.get(
            "ENVIL_LAYOUT_POST", "1").strip() not in {"0", "false", "no", ""}
        if layout_post_enabled:
            try:
                from kicad_claude.layout.api import run_layout as _run_layout
                from kicad_claude.layout import load_config as _layout_load_config
                # Respect hierarchical_split.enabled in layout_config.json.
                # Falls back to skip=True (single-sheet) only when the config
                # entry is missing or explicitly disabled — never hardcoded.
                try:
                    _h_cfg = _layout_load_config("layout_config").get(
                                "hierarchical_split") or {}
                    skip_h = not bool(_h_cfg.get("enabled", False))
                except Exception:
                    skip_h = True
                layout_out_dir = chat_path.parent / "_layout"
                layout_result = await asyncio.to_thread(
                    _run_layout, str(chat_path), str(layout_out_dir),
                    skip_hierarchical=skip_h,
                    skip_quality=False,
                    auto_display=False,       # we own the broadcast below
                )
                if not layout_result.has_errors:
                    # Overwrite the chat-written sheet with the FLAT laid-out
                    # version so subsequent chat-apply passes operate on a
                    # consistent base sheet (the chat agent edits chat_path
                    # in place — incremental edits need it to be the latest
                    # full schematic).
                    layout_p = Path(layout_result.schematic)
                    import stat as _stat
                    shutil.copy(layout_p, chat_path)
                    try:
                        chat_path.chmod(chat_path.stat().st_mode
                                         | _stat.S_IWUSR | _stat.S_IWGRP
                                         | _stat.S_IWOTH)
                    except OSError:
                        pass
                    # If hierarchy fired, point envilcad at the PARENT sheet
                    # instead of the flat one — the user prompted a circuit
                    # that crossed the hierarchical threshold, they should
                    # SEE the multi-sheet view. chat_path stays as the flat
                    # base for back-compat with future chat edits.
                    h_info = (layout_result.hierarchical or {})
                    h_parent = h_info.get("parent_path")
                    if h_parent and Path(h_parent).exists():
                        display_path = Path(h_parent)
                        try:
                            display_path.chmod(display_path.stat().st_mode
                                                | _stat.S_IWUSR | _stat.S_IWGRP
                                                | _stat.S_IWOTH)
                        except OSError:
                            pass
                        print(f"[chat] hierarchical view → {display_path.name} "
                              f"(children={h_info.get('stats', {}).get('children', '?')})",
                              flush=True)
                    else:
                        print(f"[chat] layout post-step ok → overwrote "
                              f"{chat_path.name} from {layout_p.name} "
                              f"(warnings={layout_result.quality_totals.get('warning', 0)})",
                              flush=True)
                else:
                    print(f"[chat] layout post-step had errors; "
                          f"falling back to chat output {chat_path.name}",
                          flush=True)
            except Exception as _layout_exc:
                # Never let the layout step kill chat-apply. Surface the
                # cause to the server log so we can investigate, then fall
                # through to the chat-only display path.
                print(f"[chat] layout post-step failed "
                      f"({type(_layout_exc).__name__}: {_layout_exc}); "
                      f"falling back to {chat_path.name}", flush=True)

>>>>>>> Stashed changes
        # Send open_file first — if eeschema has no file open (or a different
        # file open), this loads the just-written one. THEN send revert — if
        # eeschema already has this file open, OpenProjectFiles short-circuits
        # and we'd see the stale in-memory view forever; revert force-reloads
        # the on-disk content. Whichever case we're in, exactly one of these
        # two messages does the right thing and the other is a no-op.
        path_str = str(display_path)
        await _ipc_broadcast({"action": "open_file", "data": {"path": path_str}})
        await _ipc_broadcast({"action": "revert", "data": {"path": path_str}})

    print(f"[chat] applied: {applied}/{len(results)} ops on {room.doc.path.name}", flush=True)
    # `file_path` reports the file actually being shown — if the kicad_layout
    # post-step succeeded, that's the layout output; otherwise the chat's
    # own output. `source_file_path` is always the chat's source so the
    # panel can distinguish the two when needed.
<<<<<<< Updated upstream
    applied_payload: Dict[str, Any] = {
=======
    await ws.send_json({
>>>>>>> Stashed changes
        "kind": "applied",
        "session_id": room.session_id,
        "count": applied,
        "total": len(results),
        "file_path": str(display_path),
        "source_file_path": str(room.doc.path),
        "results": results,
    }
    if snap_stats is not None:
        applied_payload["snap"] = snap_stats
    if retry_attempted:
        applied_payload["orphan_retry"] = {
            "applied": retry_applied,
            "total": retry_total,
            "components_first_pass": components_first,
        }
    await ws.send_json(applied_payload)
    room.pending_ops = None
    room.pending_message = ""


def _build_message_blocks(text: str, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for a in attachments or []:
        aid = a.get("attachment_id")
        rec = ATTACHMENTS.get(aid) if aid else None
        if not rec:
            continue
        if rec["kind"] == "image":
            try:
                import base64
                data = Path(rec["path"]).read_bytes()
                b64 = base64.b64encode(data).decode("ascii")
                media_type = rec["content_type"] or "image/png"
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            except Exception:
                pass
        elif rec["kind"] == "pdf":
            blocks.append({
                "type": "text",
                "text": f"[Attached PDF: {rec['filename']} ({rec['size']} bytes) at {rec['path']}]",
            })
    if text:
        blocks.append({"type": "text", "text": text})
    if not blocks:
        blocks.append({"type": "text", "text": "(no content)"})
    return blocks


_IR_PIPELINE_ENABLED = os.environ.get("ENVIL_IR_PIPELINE", "1") != "0"
# Explicit build verbs. Catches "make me a relay driver", "build an LM324
# comparator", "design a regulator", etc.
_IR_BUILD_VERBS_RE = _re_pro.compile(
    r"\b(make|build|create|generate|design|draw|render|give\s+me|"
    r"new\s+circuit|need\s+a|want\s+a|wireup|wire\s+up|sketch|"
    r"add\s+a\s+circuit|implement)\b",
    _re_pro.IGNORECASE,
)
# Part-list pattern — "LM324 + 10k + Relay_SPDT", "NE555 + 2x R + 2x C",
# "STM32F103 + USB + LED". When the user just lists parts they're
# implicitly asking for a circuit. Two or more `+`-joined tokens =
# build request.
_IR_PARTS_LIST_RE = _re_pro.compile(
    r"\S+\s*\+\s*\S+",
    _re_pro.IGNORECASE,
)
# Circuit-archetype keywords — even without a verb, these are always
# synthesis requests. "Relay driver" / "voltage divider" / "low-pass
# filter" / "555 timer astable" etc.
_IR_ARCHETYPE_RE = _re_pro.compile(
    r"\b("
    r"relay\s+driver|voltage\s+divider|level\s+shifter|"
    r"low[\-\s]?pass|high[\-\s]?pass|band[\-\s]?pass|"
    r"astable|monostable|bistable|"
    r"buck|boost|buck[\-\s]boost|inverting|non[\-\s]?inverting|"
    r"comparator|amplifier|integrator|differentiator|"
    r"regulator|rectifier|oscillator|"
    r"h[\-\s]?bridge|half[\-\s]?bridge|"
    r"timer\s+circuit|driver\s+circuit"
    r")\b",
    _re_pro.IGNORECASE,
)


def _detect_circuit_generation_intent(user_text: str, room: "ChatRoom") -> bool:
    """Cheap rule-based intent classifier — no LLM round-trip just to
    decide whether to take one. True when the user is asking for a
    NEW circuit on an empty (or component-less) sheet.

    Three trigger families (any one fires the IR pipeline):
      * Build verb ("make / build / create / generate ...").
      * Part-list shape ("LM324 + 10k + Relay_SPDT") — two or more
        `+`-joined tokens implies "synthesise a circuit from these
        parts" without needing an explicit verb.
      * Circuit archetype keyword ("relay driver", "low-pass filter",
        "astable", "voltage divider"...).

    All three are gated by an empty-or-componentless schematic — once
    the user starts editing a populated sheet, incremental edits use
    the legacy path (lower-risk, preserves existing topology). Per
    [[claude-architect-engine-composer]] this branch is what migrates
    production off the "Claude draws .kicad_sch" failure mode."""
    if not _IR_PIPELINE_ENABLED or not user_text:
        return False

    has_signal = bool(
        _IR_BUILD_VERBS_RE.search(user_text)
        or _IR_PARTS_LIST_RE.search(user_text)
        or _IR_ARCHETYPE_RE.search(user_text)
    )
    if not has_signal:
        return False

    # Empty / componentless sheet → route to IR. Populated sheet →
    # legacy incremental edits.
    if room.doc is not None:
        try:
            existing = room.doc.list_components()
        except Exception:
            existing = []
        if len(existing) > 0:
            return False
    return True


async def _run_ir_pipeline(
    ws: WebSocket, room: ChatRoom, user_text: str,
) -> None:
    """Route the user's "build me a circuit" turn through the
    connectivity-first synthesizer. Architect prompt → TopologyIR →
    deterministic engine → strict validate → up to 3 attempts with
    targeted repair re-prompts on unfulfilled nets."""
    from .layout.topology_to_schematic import run_with_retry
    await ws.send_json({"kind": "status", "session_id": room.session_id,
                         "text": "Analyzing circuit topology..."})

    def _architect_call(prompt: str) -> str:
        # The chat client returns an Anthropic Message. Pull the first
        # text block — the architect prompt instructs Claude to return
        # ONLY JSON, so there's no tool wrapping to unpack.
        resp = room.client.client.messages.create(
            model=room.client.model,
            max_tokens=room.client.cfg.max_tokens,
            messages=[{"role": "user", "content": prompt}],
            system="You are a circuit architect. Return only valid JSON.",
        )
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", "")
        return ""

    out_dir = Path(room.schematic_path or "").parent / (
        f"_ir_{room.session_id[:8]}"
    ) if room.schematic_path else Path("/tmp") / f"_ir_{room.session_id[:8]}"

    try:
        report = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: run_with_retry(
                user_text, out_dir,
                max_attempts=3, strict=True,
                architect_call=_architect_call,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        await ws.send_json({"kind": "error", "session_id": room.session_id,
                             "text": f"IR pipeline failed: {exc}"})
        return

    if report.status == "ok":
        await ws.send_json({"kind": "message", "session_id": room.session_id,
                             "text": (
                                 f"Built schematic via topology IR. "
                                 f"Components: "
                                 f"{report.placement.get('stats', {}).get('components_placed', 0)} | "
                                 f"Validator: 100% required nets connected."
                             )})
        # Auto-attach so the legacy edit-path can pick up follow-ups.
        if report.schematic_path:
            room.attach_schematic(report.schematic_path)
            await ws.send_json({"kind": "open_schematic",
                                 "session_id": room.session_id,
                                 "path": report.schematic_path})
    else:
        failed = report.failed_intents or []
        fail_lines = "\n".join(
            f"  - {f.get('name')} ({f.get('signal_type')}): "
            f"unreachable={f.get('unreachable_pins')}"
            for f in failed[:5]
        )
        await ws.send_json({"kind": "message", "session_id": room.session_id,
                             "text": (
                                 f"Build did not converge after 3 attempts: "
                                 f"{report.error or 'unknown'}\n"
                                 f"Failing nets:\n{fail_lines}"
                             )})


async def _stream_turn(
    ws: WebSocket, room: ChatRoom, user_text: str, attachments: List[Dict[str, Any]]
) -> None:
    # Route circuit-generation turns through the IR pipeline; legacy
    # edit-path handles everything else (mutations on an existing
    # schematic, questions, summaries, fixes).
    if _detect_circuit_generation_intent(user_text, room):
        await _run_ir_pipeline(ws, room, user_text)
        return

    dump = room.current_dump()
    framed_prefix = f"=== CURRENT SCHEMATIC ===\n{dump}\n\n=== USER ===\n"
    blocks = _build_message_blocks(framed_prefix + user_text, attachments)
    room.history.append({"role": "user", "content": blocks})
    await ws.send_json({"kind": "status", "text": "Thinking...", "session_id": room.session_id})

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    cfg = room.client.cfg
    sys_arg = (
        [{"type": "text", "text": CHAT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
        if cfg.enable_cache
        else CHAT_SYSTEM_PROMPT
    )

    # Force structured output via tool use. The model MUST call `emit_reply`
    # with a {message, ops} payload — it cannot emit free-form prose, so the
    # chain-of-thought-as-message failure mode is eliminated. Newer Claude
    # models (Sonnet 4.6+, Opus 4.x) do not support assistant prefill, so
    # tool_choice is the supported way to constrain output shape.
    emit_reply_tool = {
        "name": "emit_reply",
        "description": (
            "Emit the schematic edit reply. Always call this tool — never reply "
            "with prose. `message` is a 1-3 sentence summary for the user; `ops` "
            "is the list of schematic operations (empty for pure-answer turns)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "1-3 sentence user-facing summary. No chain-of-thought.",
                },
                "ops": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Schematic edit operations in the order they should apply.",
                },
            },
            "required": ["message", "ops"],
        },
    }

    def _worker():
        # The Anthropic SDK rejects non-streaming calls when max_tokens is high
        # enough that the request could exceed the 10-minute non-stream limit
        # (CLAUDE_MAX_TOKENS defaults to 16000, well past the threshold). Use
        # the streaming context manager and pull the final accumulated Message
        # so the rest of this function keeps treating the response as a single
        # object with .content / .stop_reason / .usage.
        try:
            with room.client.client.messages.stream(
                model=room.client.model,
                max_tokens=cfg.max_tokens,
                system=sys_arg,
                tools=[emit_reply_tool],
                tool_choice={"type": "tool", "name": "emit_reply"},
                messages=room.history[-HISTORY_TURNS:],
            ) as stream:
                resp = stream.get_final_message()
            loop.call_soon_threadsafe(queue.put_nowait, resp)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, RuntimeError(str(e)))
        loop.call_soon_threadsafe(queue.put_nowait, DONE)

    loop.run_in_executor(None, _worker)

    resp = None
    while True:
        item = await queue.get()
        if item is DONE:
            break
        if isinstance(item, Exception):
            await ws.send_json(
                {"kind": "error", "text": f"Claude error: {item}", "session_id": room.session_id}
            )
            return
        resp = item

    parsed: Optional[Dict[str, Any]] = None
    truncated = False
    if resp is not None:
        stop_reason = getattr(resp, "stop_reason", None)
        usage = getattr(resp, "usage", None)
        out_tok = getattr(usage, "output_tokens", "?") if usage else "?"
        in_tok = getattr(usage, "input_tokens", "?") if usage else "?"
        print(f"[chat] stop_reason={stop_reason} usage in={in_tok} out={out_tok} "
              f"limit={cfg.max_tokens}", flush=True)
        if stop_reason == "max_tokens":
            truncated = True
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "emit_reply":
                tool_in = block.input
                if isinstance(tool_in, dict):
                    parsed = {
                        "message": tool_in.get("message", "") or "",
                        "ops": tool_in.get("ops", []) or [],
                    }
                break

    # If the model promised a build but emitted zero ops AND we hit max_tokens,
    # surface a clear error instead of letting the user see an empty apply card.
    if truncated and parsed and not parsed.get("ops"):
        await ws.send_json({
            "kind": "error",
            "session_id": room.session_id,
            "text": (
                f"Reply was truncated at max_tokens={cfg.max_tokens}. The model "
                f"started planning the circuit but ran out of budget before "
                f"emitting any ops. Increase CLAUDE_MAX_TOKENS in .env (try 32000) "
                f"or break the request into smaller steps."
            ),
        })
        return

    # Store the structured reply in history as a JSON string so future turns
    # see a clean, parseable assistant message (the model treats prior turns
    # as text-content even when the current turn is tool-constrained).
    full_text = json.dumps(parsed) if parsed else ""
    room.history.append({"role": "assistant", "content": full_text or "{}"})
    reply: Dict[str, Any] = {"kind": "reply", "session_id": room.session_id, "text": full_text}
    if parsed and parsed.get("ops"):
        room.pending_ops = parsed["ops"]
        room.pending_message = parsed.get("message", "")
        reply["text"] = parsed.get("message") or full_text
        reply["action_id"] = _uuid.uuid4().hex
        reply["ops"] = parsed["ops"]
        print(f"[chat] reply ready: ops={len(parsed['ops'])} action_id={reply['action_id']} "
              f"msg={parsed.get('message','')!r}", flush=True)
    elif parsed and parsed.get("message"):
        reply["text"] = parsed["message"]
        print(f"[chat] reply ready: ops=0 (text-only) msg={parsed.get('message','')!r}", flush=True)
    else:
        print(f"[chat] reply ready: parse failed; full_text_len={len(full_text)}", flush=True)
        try:
            dump_path = Path(tempfile.gettempdir()) / f"kicad_claude_reply_{room.session_id[:8]}.txt"
            dump_path.write_text(full_text, encoding="utf-8")
            print(f"[chat] full reply dumped to: {dump_path}", flush=True)
        except Exception as e:
            print(f"[chat] could not dump reply: {e}", flush=True)
    await ws.send_json(reply)


def _user_state_dir() -> Path:
    """Machine-portable per-user state dir. Same root as chat history storage.
    Windows: %LOCALAPPDATA%/orchestrator
    macOS:   ~/Library/Application Support/orchestrator
    Linux:   $XDG_STATE_HOME/orchestrator (or ~/.local/state/orchestrator)
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "orchestrator"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "orchestrator"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "orchestrator"


def _default_port_files() -> List[Path]:
    """Where to publish ipc_port.txt. The user-state-dir entry is the one
    eeschema reads first — it's the same across drives and machines, so the
    IPC handshake works without any hardcoded paths.

    Project-tree entries are kept as a courtesy for developer setups that
    run eeschema from the build dir; they're optional, not required.

    Additional roots can be supplied via the env var KICAD_EXTRA_BUILD_ROOTS
    (semicolon-separated, e.g. "C:\\Ki_CAD\\kicad-source-mirror") so a user
    who copied the build off the LAN share to a local disk still gets the
    port file written next to their local eeschema.exe.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    candidates: List[Path] = [
        _user_state_dir() / "ipc_port.txt",
        project_root / "ai_backend" / "ipc_port.txt",
    ]
    roots: List[Path] = [project_root / "kicad-source-mirror"]
    extra = os.environ.get("KICAD_EXTRA_BUILD_ROOTS", "").strip()
    if extra:
        for raw in extra.split(";"):
            raw = raw.strip().strip('"')
            if raw:
                roots.append(Path(raw))
    for src_mirror in roots:
        if not src_mirror.exists():
            continue
        candidates.append(src_mirror / "ai_backend" / "ipc_port.txt")
        for build_root in src_mirror.glob("build/*/eeschema"):
            for exe_dir in build_root.glob("*"):
                if exe_dir.is_dir() and (
                    (exe_dir / "_eeschema.kiface").exists()
                    or (exe_dir / "_eeschema.dll").exists()
                ):
                    candidates.append(exe_dir / "ai_backend" / "ipc_port.txt")
    return candidates


def run(host: str = "127.0.0.1", port: int = 8765, ipc_port_files: Optional[List[Path]] = None) -> None:
    import uvicorn

    global _ipc_port_files
    _ipc_port_files = ipc_port_files if ipc_port_files is not None else _default_port_files()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
