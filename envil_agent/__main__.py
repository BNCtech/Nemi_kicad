"""CLI: ``python -m envil_agent "your prompt" [--schematic path.kicad_sch]``.

Thin shim around ``agent.run_turn`` so the rebuild is exercisable from
the terminal before the FastAPI server is rewired. Streams text to
stdout and tool events to stderr, so you can ``> reply.txt`` and still
see what the agent did."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Windows ships a cp1252 console; Claude's reply often contains Ω, →, °,
# etc. and printing them crashes the process with UnicodeEncodeError.
# Force stdout/stderr to UTF-8 (with backslash-escape fallback) before
# anything writes — same defence the legacy server.py uses.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass

# Pull credentials out of ai_backend/.env. The historical project convention
# is CLAUDE_API_KEY / CLAUDE_MODEL_DEEP; the Claude Agent SDK expects the
# Anthropic-standard ANTHROPIC_API_KEY. Remap so existing .env files keep
# working without an edit.
try:
    from dotenv import load_dotenv
    _env = Path(__file__).resolve().parent.parent / ".env"
    if _env.exists():
        # override=True so .env wins over stale OS/inherited env vars
        # (e.g. a leftover LANGSMITH_API_KEY / LANGSMITH_PROJECT that
        # otherwise shadows .env and sends traces to the wrong org).
        load_dotenv(_env, override=True)
    _legacy_key = os.environ.get("CLAUDE_API_KEY")
    if _legacy_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = _legacy_key
except ImportError:
    pass

from .agent import run_turn


async def _drive(prompt: str, schematic: str | None, model: str | None) -> None:
    async for event in run_turn(prompt, schematic=schematic, model=model):
        if event.kind == "text":
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif event.kind == "tool_use":
            sys.stderr.write(
                f"\n[tool] {event.tool_name}({event.tool_input})\n"
            )
            sys.stderr.flush()
        elif event.kind == "end":
            sys.stdout.write("\n")
            sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="envil_agent")
    ap.add_argument("prompt", help="user request to send to the agent")
    ap.add_argument(
        "--schematic",
        help=".kicad_sch the agent should work with (announced in the prompt)",
        default=None,
    )
    ap.add_argument(
        "--model",
        help="override $ENVIL_MODEL (default: claude-sonnet-4-6)",
        default=None,
    )
    args = ap.parse_args(argv)
    asyncio.run(_drive(args.prompt, args.schematic, args.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
