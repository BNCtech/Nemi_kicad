"""Envil server — thin FastAPI shim over envil_agent.

Provides the four endpoints the frontend chat panel expects:
  GET  /                       — health/index JSON
  POST /api/upload             — temp-stash an uploaded file
  POST /api/open_in_eeschema   — broadcast an open_file IPC frame
  WS   /ws/chat                — bidirectional chat with the agent

Also runs the IPC listener on $IPC_PORT (pinned to 52344 by default) so
the eeschema plugin can connect once and stay connected across server
restarts. This file replaces the old kicad_claude/server.py (1300+ lines)
with a slim wrapper that delegates all model + tool work to envil_agent.

Run with:
    python -m uvicorn server:app --host 127.0.0.1 --port 8765
or just:
    python server.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import sys
import tempfile
import uuid as _uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# UTF-8 console — Windows cp1252 chokes on Ω, →, °, etc.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

# Load .env + remap legacy CLAUDE_API_KEY → ANTHROPIC_API_KEY for the SDK.
# override=True makes .env AUTHORITATIVE over inherited/OS env vars. Without
# it, a stale Windows env var (e.g. a leftover LANGSMITH_API_KEY pointing at
# the wrong LangSmith org, or LANGSMITH_PROJECT=KI_CAD) silently shadows this
# file — the langsmith SDK prefers LANGSMITH_* and load_dotenv's default does
# not overwrite an already-set var, so traces went to the wrong project even
# after editing .env. With override=True the server always uses .env. (2026-06-02)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
except ImportError:
    pass
if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_API_KEY"):
    os.environ["ANTHROPIC_API_KEY"] = os.environ["CLAUDE_API_KEY"]

# Avast MITM workaround — use Windows cert store
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from fastapi import (FastAPI, File, Form, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import JSONResponse, FileResponse

from envil_agent.agent import run_turn


# ---------------------------------------------------------------------------
# Upload / attachment storage
# ---------------------------------------------------------------------------

ATTACHMENT_DIR = Path(tempfile.gettempdir()) / "envil_uploads"
ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
ATTACHMENTS: Dict[str, Dict[str, Any]] = {}


def _classify(filename: str, content_type: Optional[str]) -> str:
    """Decide what to do with an uploaded attachment based on its name +
    MIME type. ERC reports get their own bucket so the chat AI can route
    them straight to erc_autofix without first trying to run kicad-cli."""
    ct = (content_type or "").lower()
    name_lc = filename.lower()
    if ct.startswith("image/"):
        return "image"
    if ct == "application/pdf" or name_lc.endswith(".pdf"):
        return "pdf"
    if name_lc.endswith(".kicad_sch"):
        return "schematic"
    # kicad-cli sch erc writes plain-text reports named *.rpt or
    # *.erc.txt by convention. Also accept any *.txt with "erc" in the
    # filename so an ERC log saved from eeschema (which uses .txt) gets
    # picked up.
    if (name_lc.endswith(".rpt")
            or name_lc.endswith(".erc.txt")
            or (name_lc.endswith(".txt") and "erc" in name_lc)):
        return "erc_report"
    return "other"


# ---------------------------------------------------------------------------
# IPC listener — eeschema plugin connects here
# ---------------------------------------------------------------------------

IPC_CLIENTS: Set[asyncio.StreamWriter] = set()
IPC_PENDING: deque = deque(maxlen=32)

# Sticky "open this project" state. open_project is a STATE-SYNC message, not a
# fire-once event: the SHELL must end up on the latest project no matter WHEN it
# connects. Two ordering hazards made the tree stay empty (observed in the live
# log as `ipc_clients=0` at broadcast time):
#   1. The broadcast fires while the shell is briefly disconnected (e.g. right
#      after a backend restart) -> it lands in IPC_PENDING.
#   2. IPC_PENDING is drained into the FIRST client to (re)connect and then
#      cleared -> that client is usually an eeschema editor, which IGNORES
#      open_project, so the shell (connecting a moment later) gets nothing.
# Fix: remember the most recent open_project and replay it to EVERY newly
# connected client. Editors discard the action; only the shell acts on it, and
# its handler skips a redundant reload when the project is already active.
IPC_LAST_OPEN_PROJECT: Optional[Dict[str, Any]] = None

# Per-session conversation history. Each entry is a list of
# {role: 'user'|'assistant', text: str} turns, oldest first. The agent
# replays this in every run_turn() call so the model has memory across
# WebSocket messages — without this, "Apply? (yes/no)" type flows fall
# apart because each turn spawns a fresh ClaudeSDKClient.
CHAT_HISTORY: Dict[str, list] = {}
HISTORY_MAX_TURNS = 20  # last 20 turns retained per session

# (Previously a module-level PAGE_SUMMARY_LAST dict was used to dedupe
# page_summary cards. That broke schematic chats: localStorage's
# session_id persists across panel closes, so reopening the panel
# matched the OLD dedupe key and never re-emitted the card. Per-
# connection dedupe lives inside ws_chat now — see `last_summary_key`
# in the handler. Each new WebSocket gets a fresh dedupe slate, which
# is exactly what users expect when they close + reopen the panel.)

# Cross-editor project path cache. The current pcbnew C++ doesn't
# pass `pcb=` in the chat URL (an unreleased rebuild fixes that), so
# the PCB chat hello arrives without a file path. Workaround: cache
# the schematic path whenever ANY chat connection tells us one, then
# derive the sibling .kicad_pcb when the PCB chat hello has no file.
# Lives module-level (not per-connection) so the eeschema panel's
# hello can inform a later-arriving pcbnew panel.
LAST_KNOWN_SCH_PATH: Optional[str] = None


_IPC_DRAIN_TIMEOUT_S = 2.0


async def _ipc_send(writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    writer.write(struct.pack(">I", len(body)) + body)
    # Bounded drain: eeschema's IPC plugin can stop reading when the
    # app is showing a modal dialog (unsaved-changes prompt, ERC
    # window, etc.) or when the user is mid-drag. Without a timeout
    # the chat turn would hang here forever and the whole KiCad UI
    # appears frozen. 2 s is generous for a normal write; clients
    # past it are considered hung and disconnected on the next round.
    try:
        await asyncio.wait_for(writer.drain(),
                                 timeout=_IPC_DRAIN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise


async def _ipc_broadcast(payload: Dict[str, Any]) -> None:
    # open_project handling: (1) absolutize the path, (2) remember it as sticky.
    if isinstance(payload, dict) and payload.get("action") == "open_project":
        # The SHELL resolves a relative path against ITS cwd (the install bin
        # folder), not the backend's — so a relative `name/name.kicad_pro` fails
        # wxFileExists() in the shell handler and LoadProject() never runs (the
        # "folder created on disk but tree stays empty" symptom). Resolve to an
        # absolute path here, at the single broadcast chokepoint, so every
        # open_project (mid-turn create_project AND end-of-turn auto-refresh)
        # sends a path the shell can actually open. resolve() is lexical-safe
        # even if the file does not exist yet and is a no-op when already absolute.
        try:
            _d = payload.get("data") or {}
            _p = _d.get("path") or ""
            if _p and not Path(_p).is_absolute():
                _abs = str(Path(_p).resolve()).replace("\\", "/")
                payload = {**payload, "data": {**_d, "path": _abs}}
        except Exception:
            pass  # never let path math break the broadcast
        # Remember the latest open_project so a client that connects (or
        # reconnects) AFTER this broadcast still gets it — see above.
        global IPC_LAST_OPEN_PROJECT
        IPC_LAST_OPEN_PROJECT = payload
    if not IPC_CLIENTS:
        IPC_PENDING.append(payload)
        return
    dead = []
    for w in list(IPC_CLIENTS):
        try:
            await _ipc_send(w, payload)
        except asyncio.TimeoutError:
            print(f"[IPC] drain timeout — dropping unresponsive client",
                   flush=True)
            dead.append(w)
        except Exception as exc:
            print(f"[IPC] send error: {type(exc).__name__}: {exc}",
                   flush=True)
            dead.append(w)
    for w in dead:
        IPC_CLIENTS.discard(w)
        try:
            w.close()
        except Exception:
            pass


async def _ipc_handle(reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
    addr = writer.get_extra_info("peername")
    print(f"[IPC] eeschema connected from {addr}", flush=True)
    IPC_CLIENTS.add(writer)
    # Replay queued messages
    if IPC_PENDING:
        backlog = list(IPC_PENDING)
        IPC_PENDING.clear()
        for q in backlog:
            try:
                await _ipc_send(writer, q)
            except Exception:
                break
    # Sticky state-sync: replay the latest open_project to THIS client too, so a
    # shell that connects after the broadcast (or reconnects after a backend
    # restart) still lands on the current project. Sent to every client; editors
    # ignore open_project, the shell loads it (and no-ops if already active).
    if IPC_LAST_OPEN_PROJECT is not None:
        try:
            await _ipc_send(writer, IPC_LAST_OPEN_PROJECT)
        except Exception:
            pass
    try:
        while True:
            length_bytes = await reader.readexactly(4)
            (length,) = struct.unpack(">I", length_bytes)
            if length > 1024 * 1024:
                break
            payload = await reader.readexactly(length)
            try:
                msg = json.loads(payload)
                print(f"[IPC] from eeschema: type={msg.get('type')}", flush=True)
            except json.JSONDecodeError:
                pass
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, OSError):
        # Windows Proactor raises ConnectionResetError (WinError 64) when
        # eeschema is killed / KiCad crashes / IPC socket is force-closed.
        # All four exception types here mean "the peer is gone" --- handle
        # them as a clean disconnect rather than letting them crash the
        # task and spew a 30-line stack trace into the server log.
        pass
    finally:
        IPC_CLIENTS.discard(writer)
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError, Exception):
            pass
        print(f"[IPC] eeschema disconnected: {addr}", flush=True)


def _user_state_dir() -> Path:
    """Per-user, install-location-independent state dir, IDENTICAL to the path the
    shell resolves first in TryConnectAiIpc() (kicad_manager_frame.cpp):
        Windows : %LOCALAPPDATA%\\orchestrator
        macOS   : ~/Library/Application Support/orchestrator
        Linux   : $XDG_STATE_HOME/orchestrator  (else ~/.local/state/orchestrator)
    The dev tree and the exe share a folder, so the old <src>/ipc_port.txt happened
    to be found on this machine — but on a shared/installed copy the exe lives
    elsewhere and never sees it, so the shell falls back to a dead port and the
    'open this project in the tree' command never arrives. Writing here too makes
    discovery work on every machine regardless of where the exe/backend live."""
    import sys
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip() or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local" / "state")
    return Path(base) / "orchestrator"


async def _start_ipc() -> int:
    pinned = os.environ.get("IPC_PORT", "52344").strip()
    try:
        port = int(pinned) if pinned else 52344
    except ValueError:
        port = 52344
    server = await asyncio.start_server(
        _ipc_handle, "127.0.0.1", port, reuse_address=True,
    )
    # Write the port for the eeschema plugin AND the shell to discover. The
    # per-user state dir is what the shell reads FIRST and is the only one that is
    # the same on a shared/installed copy as on the dev machine.
    _state_dir = _user_state_dir()
    try:
        _state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    port_files = [
        _state_dir / "ipc_port.txt",
        Path(tempfile.gettempdir()) / "envil_ipc_port.txt",
        Path(__file__).resolve().parent / "ipc_port.txt",
    ]
    for pf in port_files:
        try:
            pf.write_text(str(port), encoding="utf-8")
        except OSError:
            pass
    print(f"[IPC] listening on 127.0.0.1:{port}", flush=True)
    asyncio.create_task(server.serve_forever())
    return port


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    await _start_ipc()
    yield
    # No explicit cleanup — sockets close on process exit


app = FastAPI(lifespan=_lifespan)

# CORS: the chat panel is loaded via file:// inside eeschema's embedded
# webview but hits http://localhost:8765 for /api/preview, /api/upload,
# /ws/chat. Without allow_origins=["*"] the webview blocks the image
# response with a cross-origin error. The server is local-only so the
# wildcard is fine (don't expose this port to a public network).
try:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:
    pass  # very old fastapi w/o cors module — preview still works for same-origin clients


@app.get("/")
async def root():
    # Pull the live tool list from the agent's registered MCP tools so the
    # display stays accurate as we add verbs (apply_ops, etc.). Hardcoded
    # lists drift; this auto-syncs.
    try:
        from envil_agent.tools import ALL_TOOLS
        tool_names = [t.name for t in ALL_TOOLS]
    except Exception:
        tool_names = []
    return {
        "service": "envil",
        "agent": "envil_agent",
        "ipc_clients": len(IPC_CLIENTS),
        "tools": tool_names,
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...),
                  session_id: str = Form(default="")):
    blob = await file.read()
    aid = _uuid.uuid4().hex
    safe_name = os.path.basename(file.filename or "upload.bin")
    saved_path = ATTACHMENT_DIR / f"{aid}_{safe_name}"
    saved_path.write_bytes(blob)
    kind = _classify(safe_name, file.content_type)
    ATTACHMENTS[aid] = {
        "id": aid, "filename": safe_name, "path": str(saved_path),
        "size": len(blob), "content_type": file.content_type or "",
        "kind": kind, "session_id": session_id,
    }
    return JSONResponse({
        "attachment_id": aid, "filename": safe_name,
        "size": len(blob), "kind": kind,
    })


@app.get("/api/preview")
async def preview(path: str):
    """Phase 9. Stream an SVG preview file from disk so the chat frontend
    can render it inline via `<img src="/api/preview?path=...">`. Path
    validation: only .svg files are served, only when they exist. No
    directory traversal — accepts absolute paths but checks the suffix
    so non-image files can't leak."""
    if not path or not path.lower().endswith(".svg"):
        return JSONResponse({"ok": False, "error": "only .svg accepted"},
                              status_code=400)
    p = Path(path)
    if not p.exists() or not p.is_file():
        return JSONResponse({"ok": False, "error": f"not found: {path}"},
                              status_code=404)
    return FileResponse(str(p), media_type="image/svg+xml")


@app.post("/api/open_in_eeschema")
async def open_in_eeschema(payload: Dict[str, Any]):
    """Broadcast open_file + revert to any connected eeschema client."""
    path = str((payload or {}).get("path") or "").strip()
    if not path:
        return JSONResponse({"ok": False, "error": "path required"},
                              status_code=400)
    p = Path(path)
    if p.suffix.lower() != ".kicad_sch":
        return JSONResponse(
            {"ok": False, "error": f"only .kicad_sch accepted; got: {p.suffix}"},
            status_code=400,
        )
    if not p.exists():
        return JSONResponse({"ok": False, "error": f"file not found: {path}"},
                              status_code=404)
    await _ipc_broadcast({"action": "open_file", "data": {"path": path}})
    await _ipc_broadcast({"action": "revert", "data": {"path": path}})
    return JSONResponse({"ok": True, "path": path,
                          "ipc_clients": len(IPC_CLIENTS)})


# ---------------------------------------------------------------------------
# WebSocket chat
# ---------------------------------------------------------------------------

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    session_id = ""
    schematic_path: Optional[str] = None
    pcb_path: Optional[str] = None
    app_ctx: str = ""           # "" | "schematic" | "pcb"
    # Per-connection dedupe for page_summary. KiCad fires hello twice
    # in a row on panel open (initial WS connect + envilSetSchematic/
    # envilSetPcb refresh). We re-emit only when (app, sch, pcb)
    # changes within THIS connection. A panel close + reopen creates a
    # new WebSocket → fresh dedupe state → fresh card. localStorage
    # session_id is unaffected.
    last_summary_key: tuple = ()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"kind": "error", "text": "bad JSON"})
                continue

            kind = msg.get("kind")
            session_id = msg.get("session_id") or session_id or _uuid.uuid4().hex

            if kind == "ping":
                await ws.send_json({"kind": "pong", "session_id": session_id})
                continue

            if kind == "hello":
                global LAST_KNOWN_SCH_PATH
                pp = msg.get("project_path") or ""
                sf = msg.get("schematic_file") or ""
                pf = msg.get("pcb_file") or ""
                # Cache any schematic path we see so a later PCB hello
                # with no path can still derive a working .kicad_pcb.
                if sf and Path(sf).exists():
                    LAST_KNOWN_SCH_PATH = sf
                # Frontend tells us which editor it's running inside.
                # Normalised lowercase; aliases handled by run_turn /
                # tools_for_app so we can stay forgiving here.
                ax = (msg.get("app") or "").strip().lower()
                if sf:
                    schematic_path = sf
                if pf:
                    pcb_path = pf
                if ax in ("schematic", "sch", "eeschema",
                          "pcb", "pcbnew", "board"):
                    app_ctx = ax
                # Derive the sibling file when only one was provided.
                # KiCad projects keep the same basename for .kicad_sch
                # and .kicad_pcb; we use that convention as fallback
                # (same convention ship_design.py uses).
                if schematic_path and not pcb_path:
                    sib = Path(schematic_path).with_suffix(".kicad_pcb")
                    if sib.exists():
                        pcb_path = str(sib)
                if pcb_path and not schematic_path:
                    sib = Path(pcb_path).with_suffix(".kicad_sch")
                    if sib.exists():
                        schematic_path = str(sib)
                # PCB hello with NO path AND no schematic in URL: fall
                # back to the cross-editor cache. Most common case is
                # the user has eeschema's chat open (which DOES pass
                # the schematic path) and then opens pcbnew's chat on
                # the same project. The current pcbnew C++ ships an
                # empty chat URL; an upcoming C++ rebuild will pass
                # ?pcb=<path> directly and this fallback becomes a no-op.
                if (app_ctx in ("pcb", "pcbnew", "board")
                        and not pcb_path and not schematic_path
                        and LAST_KNOWN_SCH_PATH):
                    cached = Path(LAST_KNOWN_SCH_PATH)
                    if cached.exists():
                        schematic_path = str(cached)
                        sib = cached.with_suffix(".kicad_pcb")
                        if sib.exists():
                            pcb_path = str(sib)
                await ws.send_json({"kind": "ready",
                                      "session_id": session_id,
                                      "app": app_ctx})
                # Emit a deterministic page-summary card so the chat
                # opens with a useful "what's on this page" message
                # instead of a blank panel. NO LLM call here — just
                # reads the file and formats per layout_config.json
                # templates. Disable by setting page_summary.enabled=false.
                # Dedupe: only emit if (app, sch, pcb) actually changed
                # since the last hello on THIS connection. Per-
                # connection so closing + reopening the panel always
                # produces a fresh card.
                ctx_key = (app_ctx, schematic_path or "", pcb_path or "")
                if last_summary_key != ctx_key:
                    last_summary_key = ctx_key
                    await _emit_page_summary(ws, app_ctx, schematic_path,
                                              pcb_path, session_id)
                continue

            if kind == "reset":
                # Clear this session's history so the next turn starts
                # fresh — otherwise the agent would still see the prior
                # preview and act on it. Also clear the per-connection
                # page_summary dedupe so a fresh card is emitted on the
                # next hello (typically the chat panel re-hellos after
                # /reset to re-anchor).
                CHAT_HISTORY.pop(session_id, None)
                last_summary_key = ()
                await ws.send_json({"kind": "status",
                                      "text": "Conversation reset.",
                                      "session_id": session_id})
                continue

            if kind == "open_pcb":
                # Chat "Open PCB" button: show the generated board. The .kicad_pcb is
                # written at build time (pcb_gen), so we just ask the running app to
                # open it. eeschema's IPC client handles "open_pcb" -> OnOpenPcbnew(),
                # which opens the project board and docks pcbnew as a tab. Also nudge
                # the shell to load the project so the board is in the tree.
                pf2 = (msg.get("pcb_file") or pcb_path or "").strip()
                if not pf2 and schematic_path and schematic_path.endswith(".kicad_sch"):
                    pf2 = schematic_path[:-len(".kicad_sch")] + ".kicad_pcb"
                if pf2 and Path(pf2).exists():
                    await _ipc_broadcast({"action": "open_pcb",
                                            "data": {"path": pf2}})
                    print(f"[chat] open_pcb -> {pf2}", flush=True)
                    await ws.send_json({"kind": "status",
                                          "text": "Opening PCB…",
                                          "session_id": session_id})
                else:
                    await ws.send_json({"kind": "status",
                                          "text": "No PCB file found yet — build a "
                                                  "circuit first.",
                                          "session_id": session_id})
                continue

            if kind == "export_gerbers":
                # One-click "Download Gerbers" button. Runs DRC (honest gate)
                # then export_pcb (gerbers + drill + pick&place + zip) WITHOUT
                # the LLM — deterministic, instant, zero tokens. Emits
                # `gerbers_ready` so the chat shows an "Open Folder" button.
                # Universal: works on any .kicad_pcb, all flags from
                # layout_config.json:pcb_export — no per-circuit logic here.
                pf2 = (msg.get("pcb_file") or pcb_path or "").strip()
                if not pf2 and schematic_path and schematic_path.endswith(".kicad_sch"):
                    pf2 = schematic_path[:-len(".kicad_sch")] + ".kicad_pcb"
                if not (pf2 and Path(pf2).exists()):
                    await ws.send_json({"kind": "error",
                                          "text": "No PCB file found yet — build a "
                                                  "circuit first, then I can make the "
                                                  "Gerber files.",
                                          "session_id": session_id})
                    continue
                await ws.send_json({"kind": "status",
                                      "text": "Making the Gerber files…",
                                      "session_id": session_id})
                try:
                    from envil_agent.tools._pcb_sexpr import (
                        dispatch_tool as _dispatch_gerber)
                    # Honest DRC gate — never present the board as ready to
                    # order if it still has rule violations. -1 = couldn't run.
                    _drc_errors = -1
                    try:
                        _drc = await _dispatch_gerber("drc_check",
                                                       {"pcb_path": pf2})
                        if not _drc.get("is_error"):
                            _drc_errors = int(_drc.get("error_count", -1))
                    except Exception:
                        _drc_errors = -1
                    _exp = await _dispatch_gerber("export_pcb",
                                                   {"pcb_path": pf2, "zip": True})
                    if _exp.get("is_error") or not _exp.get("ok"):
                        await ws.send_json({"kind": "error",
                                              "text": "Could not make the Gerber "
                                                      "files automatically. Open the "
                                                      "board in KiCad and use "
                                                      "File → Fabrication Outputs "
                                                      "→ Gerbers.",
                                              "session_id": session_id})
                        continue
                    await ws.send_json({
                        "kind": "gerbers_ready",
                        "zip_path": _exp.get("zip_path", ""),
                        "output_dir": _exp.get("output_dir", ""),
                        "files": _exp.get("files", []),
                        "drc_errors": _drc_errors,
                        "drc_clean": (_drc_errors == 0),
                        "session_id": session_id,
                    })
                    print(f"[export_gerbers] {pf2} -> "
                          f"{_exp.get('zip_path','')} drc_errors={_drc_errors}",
                          flush=True)
                except Exception as _ge:
                    print(f"[export_gerbers] failed: {_ge}", flush=True)
                    await ws.send_json({"kind": "error",
                                          "text": "Could not make the Gerber files.",
                                          "session_id": session_id})
                continue

            if kind == "open_path":
                # "Open Folder" button: reveal the gerbers folder in the OS
                # file manager so the user can drag the zip to the fab house.
                # Local desktop app — the path is a folder the backend itself
                # just wrote next to the user's project.
                _op = (msg.get("path") or "").strip()
                try:
                    if _op and Path(_op).exists():
                        try:
                            os.startfile(_op)            # Windows: open folder
                        except AttributeError:
                            import subprocess as _sp
                            _opener = ("open" if sys.platform == "darwin"
                                       else "xdg-open")
                            _sp.Popen([_opener, _op])
                        await ws.send_json({"kind": "status",
                                              "text": "Opened the folder.",
                                              "session_id": session_id})
                    else:
                        await ws.send_json({"kind": "error",
                                              "text": "That folder no longer exists.",
                                              "session_id": session_id})
                except Exception as _oe:
                    print(f"[open_path] failed: {_oe}", flush=True)
                continue

            if kind == "message":
                sf = msg.get("schematic_file") or ""
                pf = msg.get("pcb_file") or ""
                ax = (msg.get("app") or "").strip().lower()
                if sf:
                    schematic_path = sf
                if pf:
                    pcb_path = pf
                if ax in ("schematic", "sch", "eeschema",
                          "pcb", "pcbnew", "board"):
                    app_ctx = ax
                user_text = msg.get("text", "")
                # Attachment expansion. Frontend POSTs files to
                # /api/upload (returns attachment_id + kind) then
                # sends the chat message with `attachments: [...]`.
                # Server expands known kinds into the user prompt so
                # the agent sees them in-context — without this, the
                # agent's 'fix ERC errors' path silently failed even
                # though the .rpt was uploaded (bug 2026-05-27: user
                # uploaded ERC.rpt + said 'fix the ERC errors' and the
                # agent replied 'I need the ERC report').
                #
                # Supported kinds today:
                #   - erc_report : inline the report text verbatim;
                #                  agent's SOURCE-CHECK rule B treats
                #                  pasted ERC text as an authoritative
                #                  source for erc_autofix.
                #   - image / pdf / schematic / other : pass through a
                #                  short metadata line so the agent
                #                  knows a file was attached even if
                #                  it doesn't need to read the bytes.
                # Accept several frontend-field-name variants — the
                # KiCad C++ chat panel uses different keys depending
                # on build (attachments / attachment_ids / files / file_ids).
                # Falling back through them keeps the server forward-
                # compatible without requiring a panel rebuild.
                attachments = (msg.get("attachments")
                               or msg.get("attachment_ids")
                               or msg.get("files")
                               or msg.get("file_ids")
                               or [])
                # Diagnostic — when a message arrives with NO attachments
                # but the text suggests one was expected, print the raw
                # msg keys so we can see what the frontend actually sent.
                if not attachments and any(k in (msg.get("text") or "").lower()
                                            for k in ("erc", "report",
                                                       "attach", "rpt", "fix")):
                    print(f"[chat][diag] attachment-bearing intent but no "
                          f"attachments field; raw msg keys = {list(msg.keys())}",
                           flush=True)
                attachment_blocks: list = []
                for att in attachments:
                    # Frontend may send several shapes:
                    #   1. {"attachment_id": "..."}  (matches /api/upload response)
                    #   2. {"id": "..."}              (canonical)
                    #   3. "raw-string-id"            (just the id)
                    #   4. {"file_id": "..."} / {"aid": "..."}
                    # Try every common key before falling through.
                    if isinstance(att, dict):
                        aid = (att.get("attachment_id")
                               or att.get("id")
                               or att.get("file_id")
                               or att.get("aid")
                               or att.get("uuid"))
                    else:
                        aid = att
                    if not aid:
                        print(f"[chat][diag] attachment entry has no "
                              f"recognizable id field: keys={list(att.keys()) if isinstance(att, dict) else type(att).__name__}",
                               flush=True)
                        continue
                    meta = ATTACHMENTS.get(aid)
                    if meta is None:
                        # In-memory dict is per-process; the frontend
                        # may cache attachment_ids from BEFORE the last
                        # server restart. Fall back to scanning the
                        # upload dir on disk for `{aid}_*`. Reclassify
                        # by filename so we still route .rpt to the
                        # erc_report bucket.
                        matches = list(ATTACHMENT_DIR.glob(f"{aid}_*"))
                        if not matches:
                            print(f"[chat][diag] attachment id {aid!r} not "
                                  f"in dict and no file matches on disk",
                                   flush=True)
                            continue
                        disk_file = matches[0]
                        fname_disk = disk_file.name.split("_", 1)[-1]
                        meta = {
                            "id": aid, "filename": fname_disk,
                            "path": str(disk_file),
                            "size": disk_file.stat().st_size,
                            "content_type": "",
                            "kind": _classify(fname_disk, None),
                            "session_id": session_id,
                        }
                        # Re-cache so subsequent turns in this same
                        # session don't have to disk-scan again.
                        ATTACHMENTS[aid] = meta
                        print(f"[chat][diag] recovered attachment {aid!r} "
                              f"from disk: {fname_disk} (kind={meta['kind']})",
                               flush=True)
                    kind_a = meta.get("kind", "other")
                    fname = meta.get("filename", "attachment")
                    fpath = meta.get("path", "")
                    if kind_a == "erc_report" and fpath:
                        try:
                            rpt_text = Path(fpath).read_text(
                                encoding="utf-8", errors="replace")
                            attachment_blocks.append(
                                f"Attached ERC report `{fname}` "
                                f"(kind=erc_report) — verbatim content:\n"
                                f"```\n{rpt_text}\n```"
                            )
                        except Exception as exc:
                            attachment_blocks.append(
                                f"(could not read attachment {fname}: "
                                f"{type(exc).__name__}: {exc})"
                            )
                    else:
                        attachment_blocks.append(
                            f"Attached file `{fname}` "
                            f"(kind={kind_a}, path={fpath})"
                        )
                if attachment_blocks:
                    user_text = ("\n\n".join(attachment_blocks)
                                 + "\n\n" + user_text).strip()
                print(f"[chat] message: app={app_ctx!r} "
                      f"text={user_text[:60]!r} "
                      f"attachments={len(attachments)} "
                      f"sch={schematic_path!r} pcb={pcb_path!r}",
                       flush=True)
                await _stream_agent_turn(ws, user_text, schematic_path,
                                          pcb_path, app_ctx, session_id)
                continue

            await ws.send_json({"kind": "error",
                                  "text": f"unknown kind: {kind}",
                                  "session_id": session_id})

    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"kind": "error",
                                  "text": f"server error: {type(e).__name__}: {e}"})
        except Exception:
            pass


async def _emit_page_summary(ws: WebSocket, app_ctx: str,
                              schematic_path: Optional[str],
                              pcb_path: Optional[str],
                              session_id: str) -> None:
    """Send a `page_summary` WS event describing what is currently on
    the page the chat just opened on. Pure deterministic read of the
    file (no LLM, no Anthropic API call); wording comes from
    `page_summary` in layout_config.json so it can be re-toned without
    a code edit. Per-page behaviour:

      schematic page + no file        — empty greeting + starter prompts
      schematic page + file w/ parts  — counts + top-N parts
      pcb page + no .kicad_pcb        — "run F8 first" message
      pcb page + empty .kicad_pcb     — placement/outline starter prompts
      pcb page + populated .kicad_pcb — counts + outline status + layers

    Safe by construction: any exception falls back to a no-summary
    state and a debug log; the chat panel still opens.
    """
    try:
        from envil_agent.intent.engine import _load_layout_config as _lc
        cfg = _lc().get("page_summary", {}) or {}
    except Exception:
        cfg = {}
    if not cfg.get("enabled", True):
        return

    a = (app_ctx or "").strip().lower()
    payload: Dict[str, Any] = {"kind": "page_summary",
                                "session_id": session_id,
                                "app": a}

    try:
        if a in ("pcb", "pcbnew", "board"):
            tpl = cfg.get("pcb", {}) or {}
            if not pcb_path or not Path(pcb_path).exists():
                t = tpl.get("empty_no_file", {}) or {}
                payload.update({
                    "state": "empty_no_file",
                    "title": t.get("title", "No PCB"),
                    "body": t.get("body", ""),
                    "suggestions": list(t.get("suggestions", [])),
                })
            else:
                from envil_agent.kicad import read_pcb_summary
                s = read_pcb_summary(pcb_path)
                if s.footprint_count == 0:
                    t = tpl.get("empty_pcb", {}) or {}
                    payload.update({
                        "state": "empty_pcb",
                        "title": t.get("title", "Empty board"),
                        "body": t.get("body", ""),
                        "suggestions": list(t.get("suggestions", [])),
                    })
                else:
                    t = tpl.get("populated", {}) or {}
                    outline = (t.get("outline_yes", "yes")
                               if s.has_edge_cuts
                               else t.get("outline_no", "missing"))
                    layers = (", ".join(s.layers_used)
                              if s.layers_used
                              else t.get("layers_none", "none"))
                    body = (t.get("body_template",
                                  "{footprints} footprints, {tracks} tracks, "
                                  "{vias} vias, {zones} zones. Outline: {outline}. "
                                  "Layers: {layers}.")
                            .format(footprints=s.footprint_count,
                                    tracks=s.track_count,
                                    vias=s.via_count,
                                    zones=s.zone_count,
                                    outline=outline,
                                    layers=layers))
                    payload.update({
                        "state": "populated",
                        "title": t.get("title", "Current PCB"),
                        "body": body,
                        "suggestions": list(t.get("suggestions", [])),
                        "stats": s.to_dict()["totals"],
                    })
        else:
            # Default (and explicit schematic) path.
            tpl = cfg.get("schematic", {}) or {}
            if not schematic_path or not Path(schematic_path).exists():
                t = tpl.get("empty", {}) or {}
                payload.update({
                    "state": "empty",
                    "title": t.get("title", "New schematic"),
                    "body": t.get("body", ""),
                    "suggestions": list(t.get("suggestions", [])),
                })
            else:
                from envil_agent.kicad import (read_summary,
                                                 read_project_summary)
                # Walk the WHOLE hierarchy — root .kicad_sch usually
                # contains only sheet stubs, not the real components.
                # Without this, hierarchical projects look "empty" on
                # the page-summary card even when 80 parts live in
                # child sheets.
                deep = None
                try:
                    deep = read_project_summary(
                        schematic_path, cfg.get("detection", {}) or {})
                except Exception as exc:
                    print(f"[page_summary] deep read failed: "
                          f"{type(exc).__name__}: {exc}", flush=True)
                # Decide empty vs populated using the AGGREGATE count.
                total = deep.total_components if deep else 0
                if total == 0:
                    # Fall back to flat read to double-check (handles
                    # edge cases where the deep walker bailed early).
                    try:
                        s = read_summary(schematic_path)
                        total = s.component_count
                    except Exception:
                        s = None
                    if total == 0:
                        t = tpl.get("empty", {}) or {}
                        payload.update({
                            "state": "empty",
                            "title": t.get("title", "New schematic"),
                            "body": t.get("body", ""),
                            "suggestions": list(t.get("suggestions", [])),
                        })
                    else:
                        # Flat-only fallback (no hierarchy detected)
                        t = tpl.get("populated", {}) or {}
                        body = (t.get("body_template",
                                      "{components} components")
                                .format(components=s.component_count,
                                        wires=s.wire_count,
                                        labels=(s.label_count
                                                 + s.global_label_count
                                                 + s.hierarchical_label_count),
                                        top_parts="—"))
                        payload.update({
                            "state": "populated",
                            "title": t.get("title", "Current schematic"),
                            "body": body,
                            "suggestions": list(t.get("suggestions", [])),
                        })
                else:
                    # Populated path — terse "type + main components"
                    # summary per user request. Raw counts + per-sheet
                    # breakdown live in payload.deep so the AI has them
                    # when the user asks; the visible card stays short.
                    t = tpl.get("populated", {}) or {}
                    # Build the power_line conditionally so we don't
                    # surface "Input not detected" noise on circuits
                    # without an input rail.
                    has_in  = bool(deep.input_voltage)
                    has_out = bool(deep.output_voltages)
                    if has_in and has_out:
                        power_line = (t.get("power_line_both",
                                            "{input_voltage} → {output_voltages}. ")
                                       .format(
                            input_voltage=deep.input_voltage,
                            output_voltages=", ".join(deep.output_voltages)))
                    elif has_in:
                        power_line = (t.get("power_line_input",
                                            "Input {input_voltage}. ")
                                       .format(input_voltage=deep.input_voltage))
                    elif has_out:
                        power_line = (t.get("power_line_output",
                                            "Output {output_voltages}. ")
                                       .format(output_voltages=", ".join(
                                                deep.output_voltages)))
                    else:
                        power_line = t.get("power_line_none", "")

                    # Key parts: just the value (no refdes), regulator
                    # output voltage appended. Order: MCU first (the
                    # "main" component), then regulators, then
                    # connectors, then crystals.
                    limit = max(1, int(t.get("key_parts_limit", 5)))
                    key_parts_bits: list = []
                    for ref, val in deep.mcus:
                        if len(key_parts_bits) >= limit:
                            break
                        key_parts_bits.append(val)
                    for ref, val, vout in deep.regulators:
                        if len(key_parts_bits) >= limit:
                            break
                        # vout already in power_line; keep value clean
                        key_parts_bits.append(val)
                    for ref, val in deep.connectors:
                        if len(key_parts_bits) >= limit:
                            break
                        key_parts_bits.append(val)
                    for ref, val in deep.crystals:
                        if len(key_parts_bits) >= limit:
                            break
                        key_parts_bits.append(val)
                    key_parts_text = ", ".join(key_parts_bits) or "—"

                    body = (t.get("deep_body_template",
                                  "**{circuit_type}**. {power_line}"
                                  "Main parts: {key_parts}.")
                            .format(
                        circuit_type=deep.circuit_type or "Circuit",
                        power_line=power_line,
                        key_parts=key_parts_text,
                    ))
                    payload.update({
                        "state": "populated",
                        "title": t.get("title", "Current schematic"),
                        "body": body,
                        "suggestions": list(t.get("suggestions", [])),
                        # Stats + deep stay on the payload so the AI
                        # has them when the user asks ("how many
                        # components?" / "what's in the MCU sheet?"),
                        # they just aren't printed on the card.
                        "stats": {
                            "total_components": deep.total_components,
                            "sheet_count": deep.sheet_count,
                            "regulators": len(deep.regulators),
                            "mcus": len(deep.mcus),
                            "connectors": len(deep.connectors),
                        },
                        "deep": deep.to_dict(),
                    })
    except Exception as exc:
        # Never let a summary failure break the chat open — log + skip.
        print(f"[page_summary] failed: {type(exc).__name__}: {exc}",
              flush=True)
        return

    await ws.send_json(payload)


async def _stream_agent_turn(ws: WebSocket, user_text: str,
                              schematic_path: Optional[str],
                              pcb_path: Optional[str],
                              app_ctx: str,
                              session_id: str) -> None:
    """Drive one agent turn and forward events to the WS client.

    Old frontend expects a single ``{kind:'reply', text:...}`` at the end —
    we emit that for compatibility, plus optional ``message_chunk`` /
    ``tool_use`` events for clients that want streaming. After build_circuit
    runs, if the agent's reply mentions a generated path, we auto-broadcast
    an open_file IPC so eeschema reloads."""
    # Fresh-shell build escape hatch. When NO project is open (no schematic AND
    # no pcb file), the shell's common AI panel may still report a stale page
    # scope (commonly "pcb" — see the "PCB Editor — No PCB yet" card). In page-
    # scoped mode that scope strips build_circuit AND the build prompt, so the
    # user cannot start a circuit from an empty shell — the agent just says
    # "switch to the Schematic Editor" ("not working properly"). With nothing to
    # scope to, treat the turn as the build-capable shell context ("") so
    # build_circuit + the intake / project-naming flow are available. An actually
    # OPEN editor (a schematic or pcb file is present) keeps its page scope, so
    # the per-editor page-scoped behaviour is untouched.
    if not (schematic_path or "").strip() and not (pcb_path or "").strip():
        if (app_ctx or "").strip().lower() != "":
            print(f"[chat] no project open — overriding app scope "
                  f"{app_ctx!r} -> '' (build-capable shell)", flush=True)
        app_ctx = ""
    # Status text is JSON-driven (`chat_ui.status_messages.initial`)
    # so the wording can change without a code edit. Default uses
    # professional EE/PCB vocabulary (replacing the generic "Thinking...").
    try:
        from envil_agent.intent.engine import _load_layout_config as _lc
        _initial_status = (
            _lc().get("chat_ui", {})
                 .get("status_messages", {})
                 .get("initial", "Analyzing circuit design..."))
    except Exception:
        _initial_status = "Analyzing circuit design..."
    await ws.send_json({"kind": "status", "text": _initial_status,
                          "session_id": session_id})
    try:
        full_reply = []
        generated_path: Optional[str] = None
        generated_pcb_path: Optional[str] = None
        child_paths: list = []
        preview_svgs: list = []
        # Pull this session's history so the agent sees prior turns and
        # can act on confirmations like "yes" / "allow" / "cancel".
        history = CHAT_HISTORY.get(session_id, [])

        # Heartbeat config — see layout_config.json -> chat_ui.heartbeat.
        # Keeps the socket warm during the long idle gap between
        # build_circuit's tool_use and tool_result. enabled=false falls
        # back to the original plain async-for consumer (byte-stable).
        try:
            from envil_agent.intent.engine import _load_layout_config as _lc_hb
            _hb = (_lc_hb().get("chat_ui", {}) or {}).get("heartbeat", {}) or {}
        except Exception:
            _hb = {}
        _hb_on = bool(_hb.get("enabled", True))
        _hb_int = float(_hb.get("interval_seconds", 10) or 10)
        _hb_text = _hb.get(
            "text", "Still working — generating the schematic ({elapsed}s elapsed)…")

        async def _handle_event(event) -> None:
            nonlocal generated_path
            if event.kind == "text":
                full_reply.append(event.text)
                await ws.send_json({"kind": "message_chunk",
                                      "text": event.text,
                                      "session_id": session_id})
            elif event.kind == "tool_use":
                await ws.send_json({"kind": "tool_use",
                                      "tool_name": event.tool_name,
                                      "tool_input": event.tool_input,
                                      "session_id": session_id})
                if event.tool_name == "build_circuit":
                    try:
                        from envil_agent.intent.engine import _load_layout_config as _lc2
                        _build_status = (
                            _lc2().get("chat_ui", {})
                                  .get("status_messages", {})
                                  .get("build_circuit",
                                        "Drafting schematic & routing nets..."))
                    except Exception:
                        _build_status = "Drafting schematic & routing nets..."
                    await ws.send_json({"kind": "status",
                                          "text": _build_status,
                                          "session_id": session_id})
            elif event.kind == "tool_result":
                # Authoritative source for the generated path — direct
                # from the tool's JSON return, no regex guessing. Also
                # surface child sheet paths so hierarchical builds get
                # all their .kicad_sch files refreshed in eeschema, not
                # just the parent.
                if event.tool_result:
                    p = event.tool_result.get("path") or ""
                    if p and p.lower().endswith(".kicad_sch"):
                        generated_path = p
                    # PCB tools (generate_pcb, auto_layout_pcb, route_pcb_simple,
                    # auto_zones_pcb, drc_autofix, …) return a .kicad_pcb path.
                    # Capture the LAST one so the open PCB editor gets a live
                    # reload at turn end, the same way eeschema does for .kicad_sch.
                    elif p and p.lower().endswith(".kicad_pcb"):
                        generated_pcb_path = p
                    # Some PCB tools (pcb_improve / "Fix all", pcb_quality) return
                    # the board under "pcb_path", NOT "path" — so the capture above
                    # missed them and the open PCB editor never got a reload after
                    # a fix. Capture "pcb_path" too so EVERY board-touching tool
                    # triggers the turn-end pcbnew revert.
                    pp = event.tool_result.get("pcb_path") or ""
                    if pp and pp.lower().endswith(".kicad_pcb"):
                        generated_pcb_path = pp
                    # Fab bundle: when export_pcb / ship_design produce a
                    # Gerber zip, surface the same "Open Folder" card the
                    # one-click button uses — so the natural-language path
                    # ("download the gerbers") gets the folder shortcut too.
                    _zp = event.tool_result.get("zip_path") or ""
                    if _zp and _zp.lower().endswith(".zip"):
                        await ws.send_json({
                            "kind": "gerbers_ready",
                            "zip_path": _zp,
                            "output_dir": event.tool_result.get("output_dir", ""),
                            "files": event.tool_result.get("files", []),
                            "drc_errors": -1,
                            "drc_clean": False,
                            "session_id": session_id,
                        })
                    cps = event.tool_result.get("child_paths") or []
                    if isinstance(cps, list):
                        for cp in cps:
                            if (isinstance(cp, str) and cp
                                    and cp.lower().endswith(".kicad_sch")
                                    and cp not in child_paths):
                                child_paths.append(cp)
                    # Phase 9: SVG preview paths produced by build_circuit.
                    # Universal — works for any schematic, no per-circuit
                    # branching here.
                    pvs = event.tool_result.get("preview_svgs") or []
                    if isinstance(pvs, list):
                        for pv in pvs:
                            if (isinstance(pv, str) and pv
                                    and pv.lower().endswith(".svg")
                                    and pv not in preview_svgs):
                                preview_svgs.append(pv)
                await ws.send_json({"kind": "tool_result",
                                      "tool_name": event.tool_name,
                                      "result": event.tool_result,
                                      "session_id": session_id})
                # Folder-first UX: when create_project finishes, load the EMPTY
                # project into the shell's Project Files tree IMMEDIATELY —
                # mid-turn, before the design questions stream. The generic
                # open_project broadcast below only runs at turn-END, so without
                # this the user sees the design questions appear before the empty
                # file shows up (the exact "create the file first, THEN go to
                # design" complaint). Gated auto_refresh.open_project_in_shell
                # (same flag as the end-of-turn path); never breaks the turn.
                # tool_name is the SDK's MCP-prefixed form (e.g.
                # "mcp__envil__create_project") — match on the bare suffix.
                _bare_tool = str(event.tool_name or "").split("__")[-1]
                if _bare_tool == "create_project" and event.tool_result:
                    _cp_sch = event.tool_result.get("path") or ""
                    if _cp_sch.lower().endswith(".kicad_sch"):
                        try:
                            from envil_agent.intent.engine import (
                                _load_layout_config as _lc_cp)
                            _cp_on = bool((_lc_cp().get("auto_refresh", {}) or {})
                                          .get("open_project_in_shell", True))
                        except Exception:
                            _cp_on = True
                        _cp_pro = _cp_sch[:-len(".kicad_sch")] + ".kicad_pro"
                        try:
                            if _cp_on and Path(_cp_pro).exists():
                                await _ipc_broadcast({"action": "open_project",
                                                        "data": {"path": _cp_pro}})
                                print(f"[create_project] open_project -> shell: "
                                      f"{_cp_pro}", flush=True)
                        except Exception as _cp_exc:
                            print(f"[create_project] open_project skipped: "
                                  f"{_cp_exc}", flush=True)
            elif event.kind == "end":
                # 'end' is run_turn's final event. Do NOT abandon the
                # generator early — draining it to StopAsyncIteration is
                # what lets run_turn close cleanly (otherwise a spurious
                # GeneratorExit is painted on every build in LangSmith).
                return

        if not _hb_on:
            # Original path — unchanged behaviour when heartbeat disabled.
            async for event in run_turn(user_text,
                                           schematic=schematic_path,
                                           pcb=pcb_path,
                                           app=app_ctx,
                                           history=history,
                                           session_id=session_id):
                await _handle_event(event)
        else:
            # Heartbeat path. A pump task drains run_turn into a queue;
            # this single consumer reads with a timeout and, on each idle
            # gap, sends a real frame so the socket never goes quiet for
            # more than _hb_int seconds. All ws sends happen from THIS
            # task (the pump only touches the queue) → no concurrent-send
            # hazard, and we never cancel the in-flight SDK read (only the
            # queue.get() is cancelled on timeout, which is loss-safe).
            _SENTINEL = object()
            _q: asyncio.Queue = asyncio.Queue()
            _pump_err: list = []

            # Gated (build_graph.progressive_status): register this turn's loop +
            # queue so the build worker thread (Phase 0 asyncio.to_thread) can
            # stream live "drew block 3/8" status through build_progress.emit().
            # Those frames ride the SAME single consumer below, so there is no
            # concurrent-send hazard. Off -> begin_turn is never called, emit() is
            # a no-op, and the consumer never sees a dict frame (byte-stable).
            _progressive_on = False
            try:
                from envil_agent.intent.engine import _load_layout_config as _lc_pg
                _progressive_on = bool((_lc_pg().get("build_graph", {}) or {})
                                       .get("progressive_status", False))
            except Exception:
                _progressive_on = False
            if _progressive_on:
                from envil_agent import build_progress as _bprog
                _bprog.begin_turn(asyncio.get_running_loop(), _q, session_id)

            async def _pump() -> None:
                try:
                    async for ev in run_turn(user_text,
                                               schematic=schematic_path,
                                               pcb=pcb_path,
                                               app=app_ctx,
                                               history=history,
                                               session_id=session_id):
                        await _q.put(ev)
                except Exception as _e:  # surface to the consumer below
                    _pump_err.append(_e)
                finally:
                    await _q.put(_SENTINEL)

            _pump_task = asyncio.create_task(_pump())
            _t0 = asyncio.get_running_loop().time()
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(_q.get(), timeout=_hb_int)
                    except asyncio.TimeoutError:
                        elapsed = int(asyncio.get_running_loop().time() - _t0)
                        # If the client has gone this send raises, ending
                        # the loop within one interval instead of stalling
                        # for the whole build. The .kicad_sch is already
                        # written by the render node, so nothing is lost.
                        await ws.send_json({"kind": "status",
                                              "text": _hb_text.format(elapsed=elapsed),
                                              "heartbeat": True,
                                              "elapsed": elapsed,
                                              "session_id": session_id})
                        continue
                    if event is _SENTINEL:
                        break
                    if isinstance(event, dict):
                        # Live per-block progress frame injected by the build
                        # worker via build_progress.emit() — forward as-is. This
                        # is the single sender, so it can't race _handle_event.
                        await ws.send_json(event)
                        continue
                    await _handle_event(event)
            finally:
                if _progressive_on:
                    try:
                        from envil_agent import build_progress as _bprog2
                        _bprog2.end_turn()
                    except Exception:
                        pass
                if not _pump_task.done():
                    _pump_task.cancel()
                try:
                    await _pump_task
                except BaseException:
                    pass
            if _pump_err:
                raise _pump_err[0]

        text = "".join(full_reply).strip() or "(no reply)"

        # Persist this turn to session history so the NEXT turn sees
        # what the user asked + what the agent said. Required for
        # multi-turn confirmation flows ("Allow / Cancel" follow-ups).
        sess = CHAT_HISTORY.setdefault(session_id, [])
        sess.append({"role": "user", "text": user_text})
        sess.append({"role": "assistant", "text": text})
        if len(sess) > HISTORY_MAX_TURNS * 2:  # *2 since each round is user + assistant
            del sess[: len(sess) - HISTORY_MAX_TURNS * 2]

        # Old-format reply event — what the frontend's chat panel binds to
        await ws.send_json({"kind": "reply",
                              "text": text,
                              "session_id": session_id})

        # Phase 10 polish: detect the confirmation-prompt pattern in the
        # assistant text and emit a side-channel `confirm_buttons` event so
        # the chat panel can render Allow / Cancel / Ask buttons instead of
        # forcing the user to type the response. Pattern is intentionally
        # forgiving: case-insensitive, and triggered by any line containing
        # both "allow" and "cancel". Edit the trigger list in
        # layout_config.json:confirm_buttons if a new wording shows up.
        try:
            import re as _re
            from envil_agent.intent.engine import _load_layout_config as _lc
            cb_cfg = _lc().get("confirm_buttons", {})
            if cb_cfg.get("enabled", True):
                triggers = cb_cfg.get("triggers", [
                    "allow / cancel",
                    "allow/cancel",
                    "reply: allow",
                    "allow or cancel",
                ])
                # When the chat AI emits a preview, we ALSO trigger on
                # generic preview cues so the user doesn't have to read
                # ritual wording — the buttons appear after any tool-use
                # confirmation-style reply. JSON-tunable.
                low = text.lower()
                fired = any(t.lower() in low for t in triggers)
                if fired:
                    await ws.send_json({
                        "kind": "confirm_buttons",
                        # Two-button default (no "Ask" — duplicated the
                        # text the AI already provides). Override in
                        # layout_config.json -> confirm_buttons.options
                        # to restore a third button if needed.
                        "options": cb_cfg.get("options", [
                            {"label": "Allow",  "send": "allow",  "primary": True},
                            {"label": "Cancel", "send": "cancel", "primary": False},
                        ]),
                        "session_id": session_id,
                    })
        except Exception:
            pass  # never break the chat reply because of a UX add-on

        await ws.send_json({"kind": "done", "session_id": session_id})

        # Auto-broadcast open_file so eeschema reloads the new schematic.
        # The "first time works, second time doesn't" symptom comes from
        # eeschema's plugin debouncing or de-duplicating reverts that
        # arrive too quickly after each other, OR not noticing the file
        # actually changed when mtime is identical. Mitigations stacked:
        #   1. Touch mtime so the file-watcher sees a new timestamp.
        #   2. open_file first, brief sleep, then revert.
        #   3. Send revert TWICE with a 200ms gap — second one is the
        #      safety net when the first gets debounced.
        if generated_path and Path(generated_path).exists():
            import os as _os
            import asyncio as _asyncio
            # Refresh scope. Default `active_only` (added 2026-06-01 per
            # user feedback "auto refresh only refresh that schematic
            # page only not whole pro folder"): refresh ONLY the
            # parent / target sheet, leave hierarchical child sheets
            # alone so eeschema does not jump pages or flicker through
            # every block. `all_sheets` restores the legacy behaviour
            # where every produced .kicad_sch gets a revert.
            try:
                from envil_agent.intent.engine import _load_layout_config as _lc_ar
                _ar_cfg = _lc_ar().get("auto_refresh", {}) or {}
            except Exception:
                _ar_cfg = {}
            refresh_scope = str(_ar_cfg.get("scope", "active_only")).lower()
            if refresh_scope == "all_sheets":
                all_paths = [generated_path] + [p for p in child_paths
                                                  if Path(p).exists()]
            else:
                all_paths = [generated_path]
            n_clients = len(IPC_CLIENTS)
            print(f"[auto-refresh] scope={refresh_scope} "
                  f"parent={generated_path} "
                  f"refreshing={len(all_paths)} of {1 + len(child_paths)} sheet(s) "
                  f"ipc_clients={n_clients}",
                  flush=True)
            # Work out UP FRONT whether the target sheet is the one
            # eeschema already has open. This must happen BEFORE we touch
            # any mtime: bumping the timestamp of a file eeschema is
            # actively showing wakes its OWN "file changed on disk -
            # reload?" watcher, which then races the in-place `revert` we
            # send below. When the user closes the page mid-race the
            # pending external-change dialog surfaces behind the close and
            # eeschema goes Not Responding -- the close+restart hang.
            # build_circuit already wrote the file, so its mtime is fresh
            # anyway; touching the active sheet again is pure churn.
            def _same_file(a: str, b: str) -> bool:
                if not a or not b:
                    return False
                try:
                    return (_os.path.normcase(_os.path.realpath(a))
                            == _os.path.normcase(_os.path.realpath(b)))
                except OSError:
                    return (_os.path.normcase(_os.path.normpath(a))
                            == _os.path.normcase(_os.path.normpath(b)))

            skip_open_when_active = bool(
                _ar_cfg.get("skip_open_when_active", True))
            # A schematic eeschema has open must count as "active" even when
            # the message arrived from the PCB editor (both windows open, PCB
            # focused). The old check below required app_ctx to BE the
            # schematic, so a PCB-context edit fell through to touch+open_file
            # the background schematic -- arming its "file changed on disk -
            # reload?" modal, which then surfaces behind the close and hangs
            # the whole project. `schematic_path` already points at the file
            # eeschema has open in the both-open case, so "this is the open
            # schematic" is the real signal, not app_ctx.
            # Gate: auto_refresh.active_ignores_app_ctx (default true) keeps
            # the fix; set false to restore the app_ctx-bound behaviour.
            active_ignores_app_ctx = bool(
                _ar_cfg.get("active_ignores_app_ctx", True))
            sch_is_open = _same_file(generated_path, schematic_path or "")
            if active_ignores_app_ctx:
                already_open = sch_is_open
            else:
                already_open = (
                    app_ctx in ("schematic", "sch", "eeschema")
                    and sch_is_open)
            send_open_file = not (skip_open_when_active and already_open)
            # mtime touch is the file-watcher fallback for when a revert
            # gets debounced. It is harmless on a sheet eeschema does NOT
            # have open, but on the active sheet it is what arms the
            # competing reload dialog above. Gate it: skip the touch on the
            # active sheet unless a project explicitly opts back in via
            # auto_refresh.touch_mtime_when_active.
            touch_mtime_when_active = bool(
                _ar_cfg.get("touch_mtime_when_active", False))
            for p in all_paths:
                if (already_open and not touch_mtime_when_active
                        and _same_file(p, schematic_path or "")):
                    print(f"[auto-refresh] skip mtime touch (active sheet) {p}",
                          flush=True)
                    continue
                try:
                    _os.utime(p, None)
                except OSError as _exc:
                    print(f"[auto-refresh] mtime touch failed for {p}: {_exc}",
                          flush=True)
            # `open_file` makes eeschema run a FULL OpenProjectFiles() (the
            # heavy "Load Schematic" path). When the file we just wrote is
            # ALREADY open (the normal case after an edit) that full re-open
            # is redundant and hangs on a hidden "save changes?" modal -- so
            # `send_open_file` (computed above) is False there and we send
            # only an in-place `revert`.
            #   - already open       -> revert only (no open_file, no touch)
            #   - new / nothing open -> touch + open_file, then revert
            # Config gate `auto_refresh.skip_open_when_active` (default true)
            # lets a project fall back to the legacy always-open_file path.
            print(f"[auto-refresh] already_open={already_open} "
                  f"send_open_file={send_open_file} "
                  f"open_sch={schematic_path}",
                  flush=True)
            # open_file the parent so eeschema loads the project root.
            # Skipped when the file is already open (see above) — revert
            # alone refreshes it without the full-reload hang.
            if send_open_file:
                await _ipc_broadcast({"action": "open_file",
                                        "data": {"path": generated_path}})
                await _asyncio.sleep(0.1)
            # revert each sheet in scope. In `active_only` mode this is
            # just the parent; in `all_sheets` it walks parent + every
            # child so child views stay fresh.
            for p in all_paths:
                await _ipc_broadcast({"action": "revert",
                                        "data": {"path": p}})
                await _asyncio.sleep(0.05)
            # Safety net second pass — picks up cases where the first revert
            # was debounced by the plugin.
            await _asyncio.sleep(0.15)
            for p in all_paths:
                await _ipc_broadcast({"action": "revert",
                                        "data": {"path": p}})
            # KiCad Next (Cursor-style): tell the SHELL to load this project so the
            # newly-built files appear in the "Project Files" tree (and update live
            # afterwards via the tree's file watcher). The editors ignore "open_project";
            # only the shell's IPC client handles it -> LoadProject(). Without this the
            # files land in F:/Ki_CAD/_envil_out/<name>/ but no open project is watching
            # that folder, so the tree stays empty. The .kicad_pro sits next to the parent
            # .kicad_sch with the same stem; derive it from the string so the path-separator
            # style matches `generated_path`. Gate auto_refresh.open_project_in_shell
            # (default true); set false to restore the editor-only refresh.
            if (bool(_ar_cfg.get("open_project_in_shell", True))
                    and generated_path.endswith(".kicad_sch")):
                _pro = generated_path[:-len(".kicad_sch")] + ".kicad_pro"
                try:
                    if Path(_pro).exists():
                        await _ipc_broadcast({"action": "open_project",
                                                "data": {"path": _pro}})
                        print(f"[auto-refresh] open_project -> shell: {_pro}",
                              flush=True)
                except Exception as _exc:
                    print(f"[auto-refresh] open_project skipped: {_exc}", flush=True)

            # KiCad Next (Cursor-style, fully automatic): after the schematic is
            # loaded/reverted, tell eeschema to push the netlist to the PCB (the F8
            # "Update PCB from Schematic"). eeschema's AI IPC client handles
            # `update_pcb_from_schematic` -> OnUpdatePCB(true), which opens/docks
            # pcbnew and applies the update SILENTLY (no modal dialog) thanks to the
            # "auto" payload. This is what turns "build schematic" into footprints
            # landing on the board with no manual step. The brief sleep lets the
            # revert above settle so the schematic is annotated/current before the
            # netlist is fetched. Gate `auto_refresh.update_pcb_after_build`
            # (default true); set false to keep F8 manual.
            if (bool(_ar_cfg.get("update_pcb_after_build", True))
                    and generated_path.endswith(".kicad_sch")):
                try:
                    await _asyncio.sleep(0.3)
                    await _ipc_broadcast({"action": "update_pcb_from_schematic",
                                            "data": {"path": generated_path}})
                    print(f"[auto-refresh] update_pcb_from_schematic -> eeschema: "
                          f"{generated_path}", flush=True)
                except Exception as _exc:
                    print(f"[auto-refresh] update_pcb skipped: {_exc}", flush=True)

            await ws.send_json({"kind": "open_schematic",
                                  "path": generated_path,
                                  "child_paths": child_paths,
                                  "session_id": session_id})

            # Phase 9: send SVG previews to chat for inline display.
            # Frontend renders these as <img src="/api/preview?path=..."/>
            # (the /api/preview endpoint streams the file from disk so
            # paths outside the web root are reachable safely).
            if preview_svgs:
                await ws.send_json({"kind": "previews",
                                      "svgs": preview_svgs,
                                      "session_id": session_id})

        # PCB editor live-refresh (Cursor-style): when a PCB tool rewrote the
        # board (generate_pcb / auto_layout_pcb / route / zones / drc_autofix),
        # tell the open pcbnew to silently reload it from disk. pcbnew's AI IPC
        # client handles `revert` (OpenProjectFiles + KICTL_REVERT, no "discard
        # changes?" dialog) and is GUARDED on its side to act only when the path
        # IS the board it has open — so this is safe to broadcast even when the
        # PCB editor isn't open (no client acts) or only eeschema is. No mtime
        # touch: KICTL_REVERT reloads unconditionally, which also avoids racing
        # KiCad's native "file changed on disk - reload?" watcher. Sent twice with
        # a gap as a debounce safety net (same pattern as the schematic revert).
        # Gate auto_refresh.refresh_pcb_editor (default true).
        if generated_pcb_path and Path(generated_pcb_path).exists():
            import asyncio as _asyncio_pcb
            try:
                from envil_agent.intent.engine import _load_layout_config as _lc_pcb
                _pcb_ar = _lc_pcb().get("auto_refresh", {}) or {}
            except Exception:
                _pcb_ar = {}
            if bool(_pcb_ar.get("refresh_pcb_editor", True)):
                try:
                    await _ipc_broadcast({"action": "revert",
                                            "data": {"path": generated_pcb_path}})
                    await _asyncio_pcb.sleep(0.15)
                    await _ipc_broadcast({"action": "revert",
                                            "data": {"path": generated_pcb_path}})
                    print(f"[auto-refresh] PCB revert -> pcbnew: "
                          f"{generated_pcb_path} ipc_clients={len(IPC_CLIENTS)}",
                          flush=True)
                except Exception as _exc:
                    print(f"[auto-refresh] PCB revert skipped: {_exc}", flush=True)

    except Exception as e:
        try:
            await ws.send_json({"kind": "error",
                                  "text": f"agent error: {type(e).__name__}: {e}",
                                  "session_id": session_id})
        except Exception:
            pass
    except BaseException:
        # CancelledError / GeneratorExit / timeout are BaseException, NOT
        # Exception, so the handler above misses them. Without a terminal frame
        # here the client never learns the build died and the "Designing…"
        # spinner cycles forever (the stuck-spinner bug). Send a best-effort
        # error frame to CLEAR the spinner, then re-raise so cancellation still
        # propagates (asyncio must see the CancelledError). If the socket is
        # already gone the send fails harmlessly and the client's onclose
        # handler clears the spinner instead.
        try:
            await ws.send_json({"kind": "error",
                                  "text": "Build was interrupted (it ran too long "
                                          "and timed out, or was cancelled). Please "
                                          "try again.",
                                  "session_id": session_id})
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Run directly
# ---------------------------------------------------------------------------

def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    # Long circuit builds (multi-minute, multi-block) leave the chat
    # WebSocket idle between the build_circuit tool_use and its
    # tool_result. uvicorn's default keepalive (ping every 20s, drop the
    # socket if no pong within 20s) then tears the connection down on any
    # brief event-loop stall — the build finishes server-side but the
    # reply + eeschema-reload are lost (the "GeneratorExit after ~500s"
    # failures seen in LangSmith). On a trusted localhost link a generous
    # ping timeout is safe; combined with the app-level heartbeat in
    # _stream_agent_turn the socket survives the whole build. All three
    # are env-overridable so they can be retuned without a code edit.
    ws_ping_interval = float(os.environ.get("ENVIL_WS_PING_INTERVAL", "20"))
    ws_ping_timeout = float(os.environ.get("ENVIL_WS_PING_TIMEOUT", "120"))
    keep_alive = int(os.environ.get("ENVIL_WS_KEEP_ALIVE", "75"))
    uvicorn.run(app, host=host, port=port, log_level="info",
                ws_ping_interval=ws_ping_interval,
                ws_ping_timeout=ws_ping_timeout,
                timeout_keep_alive=keep_alive)


if __name__ == "__main__":
    run()
