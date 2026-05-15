"""Shared JSON config loader. Every module that needs config goes through here
so configs are cached once per process and there is exactly one place to edit
when the on-disk layout changes.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


_PKG_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load(name: str) -> Dict[str, Any]:
    """Load a JSON config file from the package directory by basename (no .json)."""
    path = _PKG_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def reload_all() -> None:
    """Drop every cached config; the next load() reads fresh. For tests / hot edits."""
    load.cache_clear()
