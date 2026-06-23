"""Graph node functions. Each module exposes one or more pure functions
that take a State dict and return a State patch — LangGraph merges the
patch into the running state for the next node.

Nodes are thin: they delegate to envil_agent.tools.* or
envil_agent.intent.* for the actual work, so the graph layer never
duplicates business logic.
"""
