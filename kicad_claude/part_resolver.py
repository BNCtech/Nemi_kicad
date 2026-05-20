"""Resolve procurement metadata for a BOM line — manufacturer, exact MPN,
unit price, lifecycle, alternates — using:

  1. Datasheet URL host → manufacturer table (free, deterministic).
  2. The schematic + library description, footprint, value, keywords.
  3. Claude as a fallback for the fields the rules can't fill (cached forever
     in the bomdoc, so this runs once per unique part per project).

NO external supplier API. NO YAML. Everything tunable lives in
bomdoc_config.json. The Claude prompt and JSON schema live in config too,
not hardcoded here.

The class signature is `PartResolver(model=None, project_dir=None)`. Calling
`.resolve(row, lib_props)` returns a dict matching the schema declared in
config; `.resolve_and_cache(doc, row, lib_props)` does the same and writes
the result into the bomdoc so the next run is free.
"""

import json
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from . import bomdoc as _bomdoc
from ._config_loader import load as _load_config
from .claude_client import ClaudeClient


def _cfg() -> Dict[str, Any]:
    return _load_config("bomdoc_config")


def manufacturer_from_datasheet(url: str) -> str:
    """Match the host against the manufacturer_map. Longest host suffix wins so
    `datasheet.lcsc.com` is preferred over `lcsc.com` for the same URL."""
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]

    table = _cfg()["manufacturer_map"]
    candidates = [h for h in table.keys() if not h.startswith("_")]
    matches = [h for h in candidates if host == h or host.endswith("." + h)]
    if not matches:
        return ""
    matches.sort(key=len, reverse=True)
    name = table[matches[0]]
    # Aggregator hosts are flagged as "(aggregator — manufacturer unknown)"
    if name.startswith("("):
        return ""
    return name


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Tolerant parser. Claude is told to return raw JSON but sometimes wraps
    it in fences or adds a trailing sentence. Pull the largest top-level
    object out either way."""
    if not text:
        return None
    # 1. Direct parse.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 2. Fenced.
    matches = _JSON_FENCE.findall(text)
    if matches:
        for m in sorted(matches, key=len, reverse=True):
            try:
                return json.loads(m)
            except json.JSONDecodeError:
                continue
    # 3. Greedy {...}.
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


class PartResolver:
    """Wraps ClaudeClient with the bomdoc-config-driven prompt and schema."""

    def __init__(self, model: Optional[str] = None, project_dir: Optional[str] = None):
        self.cfg = _cfg()
        self.project_dir = project_dir
        self._client: Optional[ClaudeClient] = None
        self._model = model

    def _claude(self) -> ClaudeClient:
        if self._client is None:
            self._client = ClaudeClient(model=self._model)
        return self._client

    def _format_user(self, row: Dict[str, Any], lib_props: Dict[str, str]) -> str:
        return self.cfg["ai_resolver"]["user_template"].format(
            lib_id=row.get("lib_id", "") or lib_props.get("ki_lib_id", ""),
            value=row.get("value", "") or lib_props.get("Value", ""),
            footprint=row.get("footprint", "") or lib_props.get("Footprint", ""),
            description=row.get("description", "") or lib_props.get("Description", ""),
            datasheet=row.get("datasheet", "") or lib_props.get("Datasheet", ""),
            keywords=lib_props.get("ki_keywords", ""),
        )

    def _format_system(self) -> str:
        currency = self.cfg["distributors"]["currency"]
        return self.cfg["ai_resolver"]["system"].format(currency=currency)

    def resolve(self, row: Dict[str, Any], lib_props: Dict[str, str]) -> Dict[str, Any]:
        """Run the full resolution pipeline. Returns a dict shaped per the
        schema in bomdoc_config.json. Falls back to a partial result if the
        AI call fails or returns garbage — never raises."""
        # 1. Pre-fill manufacturer deterministically from datasheet URL.
        ds = row.get("datasheet", "") or lib_props.get("Datasheet", "")
        mfr_from_url = manufacturer_from_datasheet(ds)

        result: Dict[str, Any] = {
            "mpn": row.get("mpn", "") or "",
            "manufacturer": row.get("manufacturer", "") or mfr_from_url,
            "unit_price_usd": None,
            "lifecycle": "Unknown",
            "alternates": [],
            "preferred_distributor": self.cfg["distributors"]["ranked"][0],
            "confidence": "low",
            "notes": "",
        }

        if not self.cfg["ai_resolver"].get("enabled", True):
            return result

        # 2. Skip the AI call only if EVERYTHING the AI would fill is already
        # known — MPN + manufacturer + lifecycle + price. Otherwise call.
        already_complete = bool(
            result["mpn"]
            and result["manufacturer"]
            and result["unit_price_usd"] is not None
            and result["lifecycle"] != "Unknown"
        )
        if already_complete:
            result["confidence"] = "high"
            return result

        try:
            text = self._claude().ask(
                system=self._format_system(),
                user=self._format_user(row, lib_props),
                max_tokens=self.cfg["ai_resolver"].get("max_tokens", 1500),
            )
        except Exception as e:
            result["notes"] = f"AI resolver failed: {e}"
            return result

        parsed = _extract_json_object(text)
        if not parsed:
            result["notes"] = "AI returned non-JSON; kept rule-based defaults"
            return result

        # Merge — never trust the AI to overwrite something the user/library
        # already knows.
        for field in (
            "mpn", "manufacturer", "lifecycle", "preferred_distributor",
            "confidence", "notes",
        ):
            v = parsed.get(field)
            if v and not result.get(field):
                result[field] = v
            elif v and field in ("lifecycle", "confidence", "preferred_distributor"):
                # AI is allowed to set these even if a placeholder was there.
                result[field] = v

        if isinstance(parsed.get("unit_price_usd"), (int, float)):
            result["unit_price_usd"] = float(parsed["unit_price_usd"])

        alts = parsed.get("alternates")
        if isinstance(alts, list):
            result["alternates"] = [str(a) for a in alts if a][:5]

        # If the manufacturer host map already gave us an answer, never let
        # the AI override it — the URL is more reliable than text guessing.
        if mfr_from_url:
            result["manufacturer"] = mfr_from_url

        # Validate lifecycle against allowed states.
        allowed = set(self.cfg["lifecycle"]["states"])
        if result["lifecycle"] not in allowed:
            result["lifecycle"] = "Unknown"

        return result

    def resolve_and_cache(
        self,
        doc: Dict[str, Any],
        row: Dict[str, Any],
        lib_props: Dict[str, str],
        force: bool = False,
    ) -> Dict[str, Any]:
        """Run resolve() and persist into the bomdoc. If the line is already
        AI-resolved and `force` is False, return the cached resolution
        without spending tokens."""
        key = _bomdoc.group_key(
            row.get("mpn", ""), row.get("value", ""), row.get("footprint", "")
        )
        ld = _bomdoc.line(doc, key)

        if not force and ld.get("ai_resolved") and ld.get("approved_mpns"):
            cached = {
                "mpn": ld["approved_mpns"][0],
                "manufacturer": ld.get("manufacturer", ""),
                "unit_price_usd": ld.get("unit_price"),
                "lifecycle": ld.get("lifecycle", "Unknown"),
                "alternates": ld["approved_mpns"][1:],
                "preferred_distributor": ld.get("preferred_distributor", ""),
                "confidence": ld.get("ai_confidence", "high"),
                "notes": ld.get("notes", ""),
            }
            return cached

        resolved = self.resolve(row, lib_props)
        if self.cfg["ai_resolver"].get("cache_in_bomdoc", True):
            _bomdoc.write_resolved(doc, key, resolved)
        return resolved
