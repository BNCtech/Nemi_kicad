"""Tell the chat server to auto-display a freshly-generated .kicad_sch in
eeschema. Best-effort: if the server isn't running or the request fails,
the pipeline still succeeds — the user just has to open the file manually.

Discovery order for the chat server's HTTP port:
  1. $ENVIL_HTTP_PORT (explicit override)
  2. default 8765 (matches kicad_claude.server.run default)

The IPC port (eeschema link) is read by the server from ipc_port.txt and
is unrelated to the HTTP port — we never talk to the IPC socket directly
from here because eeschema is the IPC client and only the server can
broadcast TO it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def _http_url() -> str:
    port = os.environ.get("ENVIL_HTTP_PORT", "").strip() or "8765"
    host = os.environ.get("ENVIL_HTTP_HOST", "").strip() or "127.0.0.1"
    return f"http://{host}:{port}/api/open_in_eeschema"


def notify_eeschema(path, timeout_sec: float = 2.0) -> dict:
    """POST {"path": <abs>} to the chat server's open_in_eeschema endpoint.
    Returns {"ok": bool, "reason": str?, "response": dict?}. Never raises —
    callers want a best-effort signal, not a failure.

    Auto-display is OFF when ENVIL_NO_AUTO_DISPLAY=1 (e.g. CI runs)."""
    if os.environ.get("ENVIL_NO_AUTO_DISPLAY", "").strip() in {"1", "true", "yes"}:
        return {"ok": False, "reason": "disabled via ENVIL_NO_AUTO_DISPLAY"}
    abs_path = str(Path(path).resolve())
    if not Path(abs_path).exists():
        return {"ok": False, "reason": f"file does not exist: {abs_path}"}
    body = json.dumps({"path": abs_path}).encode("utf-8")
    req = urllib.request.Request(
        _http_url(), data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return {"ok": True, "response": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        # Server is up but rejected us. 404 = the user is still running an
        # old build of the chat server (the /api/open_in_eeschema endpoint
        # was added 2026-05-18) — surface that distinct from a connection
        # refused so they know to restart the server, not start it.
        if e.code == 404:
            reason = "endpoint missing — restart the chat server to pick up /api/open_in_eeschema"
        else:
            reason = f"server returned HTTP {e.code} {e.reason}"
        return {"ok": False, "reason": reason}
    except urllib.error.URLError as e:
        # ConnectionRefusedError most often — server not running. Don't spam
        # the pipeline output with a traceback; just report the cause.
        return {"ok": False, "reason": f"server unreachable: {e.reason}"}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
