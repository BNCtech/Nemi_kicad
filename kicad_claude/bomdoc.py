"""Persistent BOM document — the Altium-equivalent of `.BomDoc`.

Lives next to the .kicad_sch as `<stem>.bomdoc.json`. Carries:

  - Per-line user overrides     : "this 100nF is locked to Murata GRM188R61A104"
  - Per-line approved-MPN list  : ordered, preferred-first
  - Per-line AI-resolution cache: MPN, Mfr, Price, Lifecycle, alternates
  - Per-line manual notes
  - Variant overrides           : "exclude R7 from EU build"

The schematic itself is never mutated. The bomdoc is a sidecar that survives
re-renders by the AI bridge — the bridge wipes and rewrites the .kicad_sch,
the bomdoc stays. Lookup key is the same group key bom.extract_rows uses
(`MPN || value|footprint`), so a re-render that produces the same parts
re-attaches its previously resolved metadata automatically.

NO YAML. NO hardcoded values — paths, schema version, and field defaults all
flow from bomdoc_config.json.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._config_loader import load as _load_config


def _cfg() -> Dict[str, Any]:
    return _load_config("bomdoc_config")


def doc_path_for(schematic_path) -> Path:
    """Return the bomdoc.json path that pairs with this .kicad_sch."""
    p = Path(schematic_path)
    pattern = _cfg()["bomdoc"]["filename_pattern"]
    return p.parent / pattern.format(stem=p.stem)


def group_key(mpn: str, value: str, footprint: str) -> str:
    """Stable bomdoc line key.

    Always derived from value+footprint when both are known. The MPN is
    deliberately NOT part of the key — otherwise the key changes the moment
    --enrich resolves an MPN, breaking the round-trip with --refresh,
    bom-lock, and bom-backfill. Two distinct parts with identical
    value+footprint live on the same line by default; user can split via
    bom-lock if they need separate procurement entries.

    Falls back to mpn-only when value AND footprint are both empty (rare —
    only when the schematic doesn't carry either field).
    """
    if value or footprint:
        return f"vf::{value}|{footprint}"
    return f"mpn::{mpn}" if mpn else "vf::|"


def _empty_doc() -> Dict[str, Any]:
    return {
        "schema_version": _cfg()["bomdoc"]["schema_version"],
        "created_utc": int(time.time()),
        "updated_utc": int(time.time()),
        "lines": {},
        "variants_seen": [],
    }


def load(schematic_path) -> Dict[str, Any]:
    """Read the bomdoc next to schematic_path. Returns an empty doc if absent."""
    path = doc_path_for(schematic_path)
    if not path.is_file():
        return _empty_doc()
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_doc()
    if "lines" not in doc:
        doc["lines"] = {}
    return doc


def save(schematic_path, doc: Dict[str, Any]) -> Path:
    path = doc_path_for(schematic_path)
    doc["updated_utc"] = int(time.time())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    return path


def line(doc: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Return the line dict for `key`, creating an empty one on first access."""
    lines = doc.setdefault("lines", {})
    return lines.setdefault(
        key,
        {
            "user_locked": False,
            "approved_mpns": [],     # ordered, preferred first
            "preferred_distributor": "",
            "supplier_choices": [],  # [{distributor, sku, stock, unit_price, currency, fetched_utc}]
            "lifecycle": "Unknown",
            "unit_price": None,
            "currency": _cfg()["distributors"]["currency"],
            "alternates": [],
            "notes": "",
            "ai_resolved": False,
            "ai_resolved_utc": 0,
            "ai_confidence": "",
        },
    )


def merge_into_row(doc: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay bomdoc data onto a BOM row in-place. Returns the row.

    Bomdoc beats schematic on procurement fields (MPN, Mfr, distributor,
    price, lifecycle) ONLY when the schematic instance left them blank OR
    the user has set `user_locked: true`. The lock makes the bomdoc
    authoritative even if the schematic disagrees — useful when the AI
    bridge keeps re-emitting the wrong MPN.
    """
    key = group_key(row.get("mpn", ""), row.get("value", ""), row.get("footprint", ""))
    ld = doc.get("lines", {}).get(key)
    if not ld:
        return row

    locked = bool(ld.get("user_locked"))

    def take(field: str, src_value: Any) -> Any:
        if locked and src_value not in (None, ""):
            return src_value
        cur = row.get(field)
        if cur in (None, "", [], {}):
            return src_value
        return cur

    if ld.get("approved_mpns"):
        preferred = ld["approved_mpns"][0]
        row["mpn"] = take("mpn", preferred)
        row["alternates"] = take("alternates", ld["approved_mpns"][1:])

    row["lifecycle"] = take("lifecycle", ld.get("lifecycle", "Unknown"))
    row["unit_price"] = take("unit_price", ld.get("unit_price"))
    row["currency"] = take("currency", ld.get("currency", ""))
    row["preferred_distributor"] = take(
        "preferred_distributor", ld.get("preferred_distributor", "")
    )
    if ld.get("notes"):
        row["notes"] = take("notes", ld["notes"])

    row["_bomdoc_locked"] = locked
    row["_bomdoc_ai_resolved"] = bool(ld.get("ai_resolved"))
    return row


def write_resolved(
    doc: Dict[str, Any],
    key: str,
    resolved: Dict[str, Any],
) -> None:
    """Persist a resolver result into the bomdoc line. Skips fields the user
    has locked (their choice wins forever)."""
    ld = line(doc, key)
    if ld.get("user_locked"):
        return

    mpn = (resolved.get("mpn") or "").strip()
    alts = [a for a in (resolved.get("alternates") or []) if a]
    if mpn:
        # Promote MPN to first position; keep any prior approved entries below.
        existing = [m for m in ld.get("approved_mpns", []) if m != mpn]
        ld["approved_mpns"] = [mpn] + existing[:9]
        for a in alts:
            if a not in ld["approved_mpns"]:
                ld["approved_mpns"].append(a)
        ld["alternates"] = alts

    if resolved.get("manufacturer"):
        ld["manufacturer"] = resolved["manufacturer"]
    if resolved.get("preferred_distributor"):
        ld["preferred_distributor"] = resolved["preferred_distributor"]
    if resolved.get("lifecycle"):
        ld["lifecycle"] = resolved["lifecycle"]
    price = resolved.get("unit_price_usd")
    if price is not None:
        ld["unit_price"] = price
        ld["currency"] = _cfg()["distributors"]["currency"]
    if resolved.get("confidence"):
        ld["ai_confidence"] = resolved["confidence"]
    if resolved.get("notes"):
        ld["notes"] = resolved["notes"]

    ld["ai_resolved"] = True
    ld["ai_resolved_utc"] = int(time.time())


def lock(doc: Dict[str, Any], key: str, locked: bool = True) -> None:
    line(doc, key)["user_locked"] = locked


def set_approved_mpns(doc: Dict[str, Any], key: str, mpns: List[str]) -> None:
    """Programmatic approved-MPN list edit. Preserves order, dedupes."""
    seen: set = set()
    clean: List[str] = []
    for m in mpns:
        m = (m or "").strip()
        if m and m not in seen:
            clean.append(m)
            seen.add(m)
    line(doc, key)["approved_mpns"] = clean


def keys_in_doc(doc: Dict[str, Any]) -> List[str]:
    return sorted(doc.get("lines", {}).keys())


def find_keys_for_ref(
    schematic_path,
    refs: List[str],
) -> Dict[str, str]:
    """Map each reference designator to the bomdoc group key it belongs to.
    Computed by re-running the same value+footprint extraction the BOM uses,
    so the keys round-trip exactly. Refs not present in the schematic map
    to an empty string."""
    from . import bom as _bom  # local import: bom imports bomdoc, avoid cycle
    rows = _bom.extract_rows(schematic_path, enrich_from_lib=True)
    out: Dict[str, str] = {ref: "" for ref in refs}
    for r in rows:
        key = group_key(r.get("mpn", ""), r.get("value", ""), r.get("footprint", ""))
        for ref in r["references"]:
            if ref in out:
                out[ref] = key
    return out


def apply_lock(
    doc: Dict[str, Any],
    key: str,
    *,
    mpn: Optional[str] = None,
    manufacturer: Optional[str] = None,
    distributor: Optional[str] = None,
    unit_price: Optional[float] = None,
    lifecycle: Optional[str] = None,
    alternates: Optional[List[str]] = None,
    notes: Optional[str] = None,
    locked: bool = True,
) -> Dict[str, Any]:
    """Set user-curated procurement data on a bomdoc line and (by default)
    flag it locked so future --enrich runs cannot overwrite it. Returns the
    line dict for display."""
    ld = line(doc, key)
    if mpn:
        existing_alts = [m for m in ld.get("approved_mpns", []) if m != mpn]
        ld["approved_mpns"] = [mpn] + existing_alts
    if alternates is not None:
        for a in alternates:
            if a and a not in ld["approved_mpns"]:
                ld["approved_mpns"].append(a)
        ld["alternates"] = list(alternates)
    if manufacturer is not None:
        ld["manufacturer"] = manufacturer
    if distributor is not None:
        ld["preferred_distributor"] = distributor
    if unit_price is not None:
        ld["unit_price"] = float(unit_price)
    if lifecycle is not None:
        ld["lifecycle"] = lifecycle
    if notes is not None:
        ld["notes"] = notes
    ld["user_locked"] = bool(locked)
    return ld
