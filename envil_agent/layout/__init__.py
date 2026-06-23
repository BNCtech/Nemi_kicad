"""Layout pipeline: graph -> classify -> place -> route -> labels -> emit.

Not yet implemented in the rebuild. Reference implementation lives in
git commit 9191899 at kicad_claude/layout/ (pipeline.py, placer.py,
router.py, label_placer.py, emitter.py, quality.py, hierarchical.py).
Migration plan: port one stage at a time, expose each as its own @tool,
so the agent can run the pipeline incrementally and inspect intermediate
artifacts (graph.json, classified.json, placement.json, routed.json).
"""
