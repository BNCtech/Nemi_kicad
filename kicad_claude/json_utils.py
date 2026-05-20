"""Shared JSON extraction helper. Pulls the first parseable JSON object out of
a Claude reply that may be wrapped in ```json fences, padded with prose, or
truncated mid-token. Returns None on total failure so callers can decide how
to recover instead of crashing the pipeline.
"""

import json
import re
from typing import Any, Dict, Optional


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    candidates = []
    m = _JSON_FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    candidates.append(text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None
