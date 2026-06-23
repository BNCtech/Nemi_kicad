"""Deterministic KiCad s-expression engine.

Pure read/write of ``.kicad_sch`` and ``.kicad_sym`` files. No LLM calls, no
heuristics — given the same input, always produces the same output. The agent
layer (envil_agent.agent) is the only thing allowed to be non-deterministic.
"""
from .document import SchematicSummary, read_summary
from .pcb_document import PCBSummary, read_pcb_summary
from .project_summary import ProjectSummary, read_project_summary

__all__ = ["SchematicSummary", "read_summary",
           "PCBSummary", "read_pcb_summary",
           "ProjectSummary", "read_project_summary"]
