"""LangGraph orchestration layer.

Graphs assemble the existing tools + intent modules into typed state
machines. Nothing in this package owns business logic — every node is
a thin wrapper that calls into envil_agent.tools.* or envil_agent.intent.*
so the engine, validators, and tool surface stay the single source of
truth.

Entry points:
  router_graph — top-level single entry point: classify_intent ->
                 build subgraph. The user-facing tool (build_circuit)
                 invokes this so LangSmith shows the routing decision.
  build_graph  — architect → validate → render → preview pipeline.
                 Kept exported for tests + internal callers that don't
                 need the classifier.
"""
from .build_graph import build_graph
from .router_graph import router_graph

__all__ = ["build_graph", "router_graph"]
