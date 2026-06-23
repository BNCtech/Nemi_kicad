"""Connectivity-first topology IR (P19 architecture).

Not yet implemented in the rebuild. Will host:
  ir.py               — TopologyIR / ConnectionIntent dataclasses
  architect_prompt.py — system prompt for the architect role
  engine.py           — TopologyIR -> SchematicDocument (deterministic)
  validate.py         — required-net-coverage check after build

Reference implementation lives in git commit 9191899 at:
  kicad_claude/layout/topology_to_schematic.py (referenced but missing)
  kicad_claude/layout/connectivity_graph.py    (the upstream pieces)
"""
