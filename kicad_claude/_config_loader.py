"""Shared JSON config loader. Every module that needs config goes through here
so configs are cached once per process and there is exactly one place to edit
when the on-disk layout changes.

Schema validation: if config_schema/{name}.schema.json exists, the loaded
config is validated against it on first load. A typo or missing block fails
fast with a path-of-error (`required: 'foo' in /loop`) instead of crashing
mid-run with a KeyError. Configs without a schema load unvalidated — schemas
can be added incrementally without breaking anything.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import jsonschema
from jsonschema import Draft202012Validator


_PKG_DIR = Path(__file__).parent
_PROMPTS_DIR = _PKG_DIR / "prompts"
_SCHEMA_DIR = _PKG_DIR / "config_schema"


class ConfigError(ValueError):
    """Raised when a config file fails schema validation. The message lists
    every violation as `JSON-pointer: reason` so the operator can fix the
    config without spelunking through Python tracebacks."""


def _validate(name: str, data: Dict[str, Any]) -> None:
    schema_path = _SCHEMA_DIR / f"{name}.schema.json"
    if not schema_path.exists():
        return
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    lines = [f"{name}.json failed schema validation ({schema_path.name}):"]
    for e in errors:
        pointer = "/" + "/".join(str(p) for p in e.absolute_path) if e.absolute_path else "/"
        lines.append(f"  {pointer}: {e.message}")
    raise ConfigError("\n".join(lines))


@lru_cache(maxsize=None)
def load(name: str) -> Dict[str, Any]:
    """Load a JSON config file from the package directory by basename (no .json).
    Validates against config_schema/{name}.schema.json when present."""
    path = _PKG_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _validate(name, data)
    return data


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Load a prompt template from prompts/{name}.md by basename (no .md).

    Templates use {{PLACEHOLDER}} sentinels resolved by the caller via
    plain str.replace — chosen over str.format because prompts contain
    literal JSON braces that would otherwise need escaping.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def reload_all() -> None:
    """Drop every cached config + prompt; the next load reads fresh.
    For tests and hot edits to JSON / prompts without restart."""
    load.cache_clear()
    load_prompt.cache_clear()
