"""Resolve a Footprint property string for a (lib_id, value) pair.

Called from schematic_modifier.add_component when the caller passes no
explicit footprint. Resolution order:

  1. Skip — power-port / virtual lib_id prefixes get "".
  2. Exact by_lib_id match in footprint_aliases.json.
  3. Class fallback — substring on lib_id_lower against by_class.
  4. Empty — user assigns post-render (no worse than today's behaviour).

The aliases file is reloaded via _config_loader (lru_cached), so server
restart is required after edits.
"""
from typing import Optional

from ._config_loader import load as _load_config


def resolve_footprint(lib_id: str, value: str = "", reference: str = "") -> str:
    """Return the best-known footprint string for this lib_id (+ value/ref hints).

    Empty string means "no opinion" — the symbol's Footprint property stays
    blank and KiCad's Assign Footprints dialog handles it. Never raises.
    """
    if not lib_id:
        return ""

    cfg = _load_config("footprint_aliases")
    lib_id_lower = lib_id.lower()

    for prefix in cfg.get("skip_lib_id_prefixes", []):
        if lib_id.startswith(prefix):
            return ""

    fp = cfg.get("by_lib_id", {}).get(lib_id)
    if fp:
        return fp

    for needle, footprint in cfg.get("by_class", {}).items():
        if needle.lower() in lib_id_lower:
            return footprint

    return ""
