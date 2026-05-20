"""circuit_profile.py — Dynamic circuit-size scaling for rules.py.

Single source of truth for every threshold, active-rule set, severity
override, and fixer default value. Every detector and fixer reads from
a CircuitProfile instance instead of from hardcoded module-level constants.

Usage (in rules.py):
    from .circuit_profile import CircuitProfile

    ctx = build_context(path)
    profile = CircuitProfile.from_context(ctx)          # auto-detect
    # or with override:
    profile = CircuitProfile.from_context(ctx, override_tier="large")

    # In a detector:
    radius = profile.threshold("proximity_per_pin_radius_mm")
    if profile.rule_active("POWER_012"):
        ...

    # In a fixer:
    cap_val = profile.fix_default("cap_decoupling")
    sev     = profile.severity("LAY_007", base_severity="high")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ._config_loader import load as _load_config


# ---------------------------------------------------------------------------
# Config loader (cached — same lru_cache pattern as all other configs)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _scale_cfg() -> Dict[str, Any]:
    return _load_config("circuit_scale_config")


@lru_cache(maxsize=1)
def _pin_cat() -> Dict[str, Any]:
    return _load_config("pin_catalogs")


@lru_cache(maxsize=1)
def _part_fam() -> Dict[str, Any]:
    return _load_config("part_families")


# ---------------------------------------------------------------------------
# Tier ordering (used for comparisons)
# ---------------------------------------------------------------------------

TIER_ORDER = ["nano", "small", "medium", "large", "xlarge"]


# ---------------------------------------------------------------------------
# CircuitMetrics — raw numbers extracted from the schematic
# ---------------------------------------------------------------------------

@dataclass
class CircuitMetrics:
    component_count: int = 0
    pin_count: int = 0
    net_count: int = 0
    sheet_count: int = 1
    max_ic_pin_count: int = 0   # pins on the largest single IC

    @property
    def is_multi_sheet(self) -> bool:
        return self.sheet_count > 1

    @classmethod
    def from_context(cls, ctx) -> "CircuitMetrics":
        """Extract metrics from a RuleContext (rules.py dataclass)."""
        component_count = len(ctx.components)
        pin_count = sum(
            len(pins)
            for pins in ctx.component_pins_by_ref.values()
        )
        net_count = len(ctx.nets)
        max_ic = max(
            (len(pins) for ref, pins in ctx.component_pins_by_ref.items()
             if ref.startswith(("U", "IC"))),
            default=0,
        )
        return cls(
            component_count=component_count,
            pin_count=pin_count,
            net_count=net_count,
            max_ic_pin_count=max_ic,
        )


# ---------------------------------------------------------------------------
# CircuitProfile — the public API
# ---------------------------------------------------------------------------

@dataclass
class CircuitProfile:
    tier: str
    metrics: CircuitMetrics
    _scale: Dict[str, Any] = field(repr=False, default_factory=_scale_cfg)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_context(cls, ctx, override_tier: Optional[str] = None) -> "CircuitProfile":
        """Build a profile from a RuleContext, auto-detecting tier.

        override_tier: if provided, skips auto-detection and uses this tier.
        Also checks conventions.json -> circuit.size_tier for a persistent
        project-level override (override_tier arg takes priority over JSON).
        """
        metrics = CircuitMetrics.from_context(ctx)
        tier = override_tier or _project_tier_override() or _auto_tier(metrics)
        return cls(tier=tier, metrics=metrics)

    @classmethod
    def from_metrics(cls, metrics: CircuitMetrics,
                     override_tier: Optional[str] = None) -> "CircuitProfile":
        """Build from pre-computed metrics (useful for tests)."""
        tier = override_tier or _project_tier_override() or _auto_tier(metrics)
        return cls(tier=tier, metrics=metrics)

    # ------------------------------------------------------------------
    # Threshold access
    # ------------------------------------------------------------------

    def threshold(self, key: str) -> Any:
        """Get a scaled numeric threshold for the current tier.

        Falls back to 'medium' if the key is missing for this tier,
        then to the raw value if it's not tier-keyed at all.
        """
        tbl = self._scale["thresholds"].get(key)
        if tbl is None:
            raise KeyError(f"Unknown threshold key: {key!r}")
        if isinstance(tbl, dict):
            return tbl.get(self.tier, tbl.get("medium"))
        return tbl  # scalar (not tier-keyed) — same for all tiers

    def threshold_int(self, key: str) -> int:
        return int(self.threshold(key))

    def threshold_float(self, key: str) -> float:
        return float(self.threshold(key))

    # ------------------------------------------------------------------
    # Active-rule gating
    # ------------------------------------------------------------------

    def rule_active(self, rule_id: str) -> bool:
        """True if this rule should run at the current tier."""
        disabled: List[str] = self._scale["active_rules"].get(self.tier, [])
        return rule_id not in disabled

    def active_rule_ids(self, all_ids: List[str]) -> List[str]:
        """Filter a full list of rule IDs to those active at this tier."""
        return [r for r in all_ids if self.rule_active(r)]

    # ------------------------------------------------------------------
    # Severity scaling
    # ------------------------------------------------------------------

    def severity(self, rule_id: str, base_severity: str) -> str:
        """Return the effective severity, applying any tier-level override.

        CRITICAL is never downgraded regardless of tier or override.
        """
        if base_severity.lower() == "critical":
            return "critical"
        overrides: Dict[str, str] = self._scale["severity_overrides"].get(self.tier, {})
        return overrides.get(rule_id, base_severity)

    # ------------------------------------------------------------------
    # Fix defaults
    # ------------------------------------------------------------------

    def fix_default(self, key: str) -> Any:
        """Get a scaled fixer default value (cap value, resistor value, etc.)."""
        tbl = self._scale["fix_defaults"].get(key)
        if tbl is None:
            raise KeyError(f"Unknown fix_default key: {key!r}")
        if isinstance(tbl, dict):
            return tbl.get(self.tier, tbl.get("medium"))
        return tbl

    # ------------------------------------------------------------------
    # Fixer run-limits (prevent flooding small boards)
    # ------------------------------------------------------------------

    def cap_budget(self) -> int:
        return int(self.threshold("decoupling_cap_max_per_fixer_run"))

    def pullup_budget(self) -> int:
        return int(self.threshold("pullup_max_per_fixer_run"))

    # ------------------------------------------------------------------
    # AVDD filter caps (separate catalog so the datasheet citations
    # stay co-located with the values)
    # ------------------------------------------------------------------

    def avdd_cap(self, role: str) -> str:
        """Tier-appropriate cap/inductor value for an AVDD network role.

        role: key in avdd_filter.json -> _tier_cap_values (e.g. 'vdda_bulk').
        Falls back to '100n' if the role is unknown or the file is missing,
        so callers never have to guard around this.
        """
        try:
            avdd = _load_config("avdd_filter")
            tbl = avdd.get("_tier_cap_values", {}).get(role)
            if isinstance(tbl, dict):
                return tbl.get(self.tier, tbl.get("medium", "100n"))
        except Exception:
            pass
        return "100n"

    # ------------------------------------------------------------------
    # Convenience: tier comparisons
    # ------------------------------------------------------------------

    def at_least(self, tier: str) -> bool:
        return TIER_ORDER.index(self.tier) >= TIER_ORDER.index(tier)

    def at_most(self, tier: str) -> bool:
        return TIER_ORDER.index(self.tier) <= TIER_ORDER.index(tier)

    # ------------------------------------------------------------------
    # String repr for logging
    # ------------------------------------------------------------------

    def summary(self) -> str:
        m = self.metrics
        return (
            f"CircuitProfile(tier={self.tier!r}, "
            f"components={m.component_count}, "
            f"pins={m.pin_count}, "
            f"nets={m.net_count}, "
            f"sheets={m.sheet_count})"
        )


# ---------------------------------------------------------------------------
# Pin / net catalog accessors (replace inline tuples in rules.py)
# ---------------------------------------------------------------------------

def pin_names(key: str) -> tuple:
    """Return a tuple of lowercase pin-name strings for the given catalog key.

    Dot-notation for nested keys: pin_names("i2c.scl_names")
    """
    cat = _pin_cat()
    parts = key.split(".")
    node = cat
    for p in parts:
        node = node[p]
    return tuple(node)


def rail_hints(polarity: str) -> tuple:
    """polarity: 'positive' or 'negative'"""
    cat = _pin_cat()
    k = "positive_rail_hints" if polarity == "positive" else "negative_rail_hints"
    return tuple(cat[k])


def gnd_family_rails() -> Set[str]:
    return set(_pin_cat()["gnd_family_rails"])


# ---------------------------------------------------------------------------
# Part-family recognition (replace inline lib_hint tuples + regex lists)
# ---------------------------------------------------------------------------

def _compile_patterns(patterns: List[str]) -> List[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


@lru_cache(maxsize=None)
def _compiled_part_patterns(family: str):
    fam = _part_fam().get(family)
    if fam is None:
        raise KeyError(f"Unknown part family: {family!r}")
    lib_hints = tuple(fam.get("lib_id_substrings", []))
    val_pats   = tuple(_compile_patterns(fam.get("value_patterns", [])))
    ref_pfx    = tuple(fam.get("reference_prefixes", []))
    return lib_hints, val_pats, ref_pfx


def is_part_family(comp: Dict[str, Any], family: str) -> bool:
    """Return True if comp matches the given part-family catalog entry.

    Checks lib_id substrings, value regex patterns, and reference prefixes.
    All three checks are attempted; any match returns True.
    """
    lib_hints, val_pats, ref_pfx = _compiled_part_patterns(family)
    lib_id = (comp.get("lib_id") or "").lower()
    value  = comp.get("value") or ""
    ref    = comp.get("reference") or ""

    if any(h in lib_id for h in lib_hints):
        return True
    if any(p.search(value) for p in val_pats):
        return True
    if any(ref.startswith(pfx) for pfx in ref_pfx):
        pass  # reference_prefixes alone are too broad to be authoritative
              # (e.g. "D" matches ALL diodes not just TVS); skip solo match.
    return False


def lib_hints(family: str) -> tuple:
    """Raw lib_id substrings for a family (for use in membership checks)."""
    return _compiled_part_patterns(family)[0]


def value_patterns(family: str) -> List[re.Pattern]:
    """Compiled regex patterns for a family's value strings."""
    return list(_compiled_part_patterns(family)[1])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auto_tier(m: CircuitMetrics) -> str:
    """Pick the smallest tier whose maximums the circuit fits within."""
    tiers = _scale_cfg()["tiers"]
    for tier_name in TIER_ORDER:
        t = tiers[tier_name]
        if (m.component_count <= t["component_count_max"]
                and m.pin_count     <= t["pin_count_max"]
                and m.net_count     <= t["net_count_max"]):
            return tier_name
    return "xlarge"


def _project_tier_override() -> Optional[str]:
    """Check conventions.json for a project-level size tier override.

    conventions.json -> circuit.size_tier  (optional key)
    Returns None if not set or if the value is not a valid tier name.
    """
    try:
        conv = _load_config("conventions")
        tier = conv.get("circuit", {}).get("size_tier")
        if tier and tier in TIER_ORDER:
            return tier
    except Exception:
        pass
    return None
