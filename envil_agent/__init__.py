"""Envil — KiCad schematic engineering agent on the Claude Agent SDK.

Replaces the legacy ``kicad_claude`` package. Architecture:
  envil_agent.agent     — ClaudeSDKClient + system prompt + tool registration
  envil_agent.kicad     — deterministic s-expression engine (read/write .kicad_sch)
  envil_agent.intent    — connectivity-first topology IR (LLM-as-architect)
  envil_agent.layout    — graph -> classify -> place -> route -> emit pipeline
  envil_agent.lint      — declarative rule engine (replaces rules.py)
  envil_agent.tools     — @tool functions exposed to the agent
  envil_agent.config    — JSON data (no Python config)

Public surface is intentionally tiny. External callers (server, tests) import
through this module rather than reaching into submodules.
"""
from .agent import build_client, run_turn

__all__ = ["build_client", "run_turn"]
