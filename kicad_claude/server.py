import asyncio
import json
import os
import socket
import struct
import tempfile
import uuid as _uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .chat import CHAT_SYSTEM_PROMPT, _extract_json
from .claude_client import ClaudeClient
from .schematic_extractor import SchematicExtractor
from .schematic_modifier import SchematicDocument, apply_operation


HISTORY_TURNS = 12


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
        try:
            base = SchematicExtractor(self.schematic_path).format_for_claude()
        except Exception as e:
            return f"(unable to read schematic: {e})"
        parts: list = [base]

        # Inject per-component pin tip coordinates so the AI can emit wires
        # that LAND on real pin endpoints. Without this the AI sees only the
        # component anchor and guesses pin positions — usually wrong by 2-5 mm,
        # which produces "wire-near-pin-but-not-on-it" defects (e.g.
        # FUNC_NO_DECOUPLING despite a placed cap).
        try:
            from . import nets as _nets
            from .schematic_extractor import SchematicExtractor as _SE
            ext = _SE(self.schematic_path)
            lib_pins = ext.lib_symbol_pins()
            pin_lines = []
            for c in ext.components():
                lib_id = c.get("lib_id", "")
                ref = c.get("reference", "")
                if not ref or (lib_id or "").startswith("power:"):
                    continue
                by_unit = lib_pins.get(lib_id) or {}
                if not by_unit:
                    continue
                inst_unit = int(c.get("unit", 1))
                pin_defs = list(by_unit.get(0, [])) + list(by_unit.get(inst_unit, []))
                if not pin_defs:
                    continue
                for ep in _nets.placed_pin_endpoints(c, pin_defs):
                    name = ep.get("name", "") or "~"
                    num = ep.get("pin_number", "?")
                    etype = ep.get("electrical_type", "")
                    pin_lines.append(
                        f"  {ref}.{num}({name:>8s}) [{etype:>10s}] @ ({ep['x']:.2f}, {ep['y']:.2f})"
                    )
            if pin_lines:
                parts.append("")
                parts.append("=== PIN ENDPOINTS (wire to these exact coordinates) ===")
                parts.extend(pin_lines)
        except Exception:
            pass

        # Append the L1 defect list so the AI sees what's broken on every turn.
        try:
            from . import basic_checks
            report = basic_checks.run_all(self.schematic_path)
            issues = [
                i for i in report.get("issues", [])
                if i.get("severity") in ("critical", "high", "medium")
            ]
            if issues:
                parts.append("")
                parts.append("=== DETECTED DEFECTS (fix these unless the user says otherwise) ===")
                for i in issues[:40]:
                    parts.append(
                        f"  [{i.get('severity','?')}/{i.get('check','?')}] "
                        f"{i.get('refs','')}: {i.get('message','')}"
                    )
                if len(issues) > 40:
                    parts.append(f"  ... +{len(issues)-40} more")
        except Exception:
            pass

        return "\n".join(parts)


SESSIONS: Dict[str, ChatRoom] = {}
IPC_CLIENTS: Set[asyncio.StreamWriter] = set()


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
    port = _free_port()
    server = await asyncio.start_server(_ipc_handle, "127.0.0.1", port)
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
                await _stream_turn(ws, room, msg.get("text", ""), attachments)
                continue

            await ws.send_json({"kind": "error", "text": f"unknown kind: {kind}", "session_id": room.session_id})

    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"kind": "error", "text": f"server error: {e}"})
        except Exception:
            pass


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

    results = [
        {"op": op.get("op") or op.get("type"), **apply_operation(room.doc, op)} for op in room.pending_ops
    ]
    applied = sum(1 for r in results if r.get("ok"))
    if applied:
        room.doc.save()
        await _ipc_broadcast({"action": "open_file", "data": {"path": str(room.doc.path)}})

    await ws.send_json({
        "kind": "applied",
        "session_id": room.session_id,
        "count": applied,
        "total": len(results),
        "file_path": str(room.doc.path),
        "results": results,
    })
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


async def _stream_turn(
    ws: WebSocket, room: ChatRoom, user_text: str, attachments: List[Dict[str, Any]]
) -> None:
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

    def _worker():
        try:
            with room.client.client.messages.stream(
                model=room.client.model,
                max_tokens=cfg.max_tokens,
                system=sys_arg,
                messages=room.history[-HISTORY_TURNS:],
            ) as stream:
                for chunk in stream.text_stream:
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, RuntimeError(str(e)))
        loop.call_soon_threadsafe(queue.put_nowait, DONE)

    loop.run_in_executor(None, _worker)

    full_text = ""
    while True:
        item = await queue.get()
        if item is DONE:
            break
        if isinstance(item, Exception):
            await ws.send_json(
                {"kind": "error", "text": f"Claude error: {item}", "session_id": room.session_id}
            )
            return
        full_text += item
        await ws.send_json({"kind": "chunk", "text": item, "session_id": room.session_id})

    room.history.append({"role": "assistant", "content": full_text})

    parsed = _extract_json(full_text)
    reply: Dict[str, Any] = {"kind": "reply", "session_id": room.session_id, "text": full_text}
    if parsed and parsed.get("ops"):
        room.pending_ops = parsed["ops"]
        room.pending_message = parsed.get("message", "")
        reply["text"] = parsed.get("message") or full_text
        reply["action_id"] = _uuid.uuid4().hex
        reply["ops"] = parsed["ops"]
    elif parsed and parsed.get("message"):
        reply["text"] = parsed["message"]
    await ws.send_json(reply)


def _default_port_files() -> List[Path]:
    project_root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        project_root / "ai_backend" / "ipc_port.txt",
        project_root / "kicad-source-mirror" / "ai_backend" / "ipc_port.txt",
    ]
    src_mirror = project_root / "kicad-source-mirror"
    if src_mirror.exists():
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
