"""
schematic_analyzer.py

Deep schematic analysis engine.
- Extracts power topology, signal paths, MCU connections, current budgets
- Detects designer skill level from their prompt style
- Returns analysis + skill-appropriate feedback prompt for Claude

Usage:
    analyzer = SchematicAnalyzer(claude_client, schematic_extractor, nets)
    report   = analyzer.full_analysis(schematic_dump, user_prompt)
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


CONFIG_PATH = Path(__file__).parent / "analyzer_config.json"


SKILL_PARSE_SYSTEM = """
You are an electronics design skill level classifier.
Given a user's prompt about a schematic/PCB, classify their skill level as:
- "beginner": hobbyist, Arduino-level, vague language, unsure of terminology
- "intermediate": knows common ICs, standard interfaces, basic power design
- "expert": talks about SI, PDN, EMC, uses precise engineering language

Also extract their PRIMARY GOAL in one sentence.

Reply ONLY with JSON: {"skill": "beginner|intermediate|expert", "goal": "..."}
"""

ANALYSIS_SYSTEM = """
You are a senior electronics engineer performing a deep schematic review.
Analyze the schematic dump and produce a structured JSON report covering:

1. power_rails: list each rail {name, voltage, source_ic, load_ics, estimated_current_ma, issues[]}
2. microcontrollers: for each MCU {refdes, part, clock_mhz, used_peripherals[], unused_pins_count, power_pins_ok, issues[]}
3. signal_integrity: {differential_pairs[], single_ended_critical[], pullup_missing[], debounce_missing[]}
4. protection: {reverse_polarity: bool, overvoltage: bool, esd_on_io: bool, fuse_present: bool, issues[]}
5. bom_summary: {total_components, passives_count, ics_count, connectors_count, estimated_cost_usd_rough}
6. critical_issues: [] list of show-stopper problems
7. warnings: [] list of non-critical but important items
8. good_practices: [] list of things the designer did well

Reply ONLY with valid JSON. Be specific — cite refdes, net names, pin numbers.
"""


@dataclass
class SchematicReport:
    power_rails: list = field(default_factory=list)
    microcontrollers: list = field(default_factory=list)
    signal_integrity: dict = field(default_factory=dict)
    protection: dict = field(default_factory=dict)
    bom_summary: dict = field(default_factory=dict)
    critical_issues: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    good_practices: list = field(default_factory=list)


@dataclass
class DesignerProfile:
    skill: str          # "beginner" | "intermediate" | "expert"
    goal: str
    confidence: float   # 0-1 how confident we are in the skill level


@dataclass
class FullAnalysisResult:
    report: SchematicReport
    profile: DesignerProfile
    feedback: str       # AI-generated, skill-appropriate feedback text
    raw_analysis: dict  # full JSON from analysis step


class SchematicAnalyzer:
    """
    Deep analysis engine. Plugs into the existing ClaudeClient
    (ai_backend.kicad_claude.claude_client.ClaudeClient), schematic_extractor,
    and nets modules.
    """

    def __init__(self, claude_client, schematic_extractor=None, nets=None,
                 config_path: Optional[Path] = None):
        self.client = claude_client
        self.extractor = schematic_extractor
        self.nets = nets
        self.config = json.loads((config_path or CONFIG_PATH).read_text(encoding="utf-8"))

    def full_analysis(
        self, schematic_dump: dict, user_prompt: str
    ) -> FullAnalysisResult:
        """
        Main entry point.
        schematic_dump: output of schematic_extractor.extract()
        user_prompt:    the user's raw message
        """
        profile = self._detect_skill(user_prompt)
        enriched = self._enrich_dump(schematic_dump)
        raw_analysis = self._analyze_schematic(enriched)
        report = self._parse_report(raw_analysis)
        feedback = self._generate_feedback(report, profile, user_prompt)

        return FullAnalysisResult(
            report=report,
            profile=profile,
            feedback=feedback,
            raw_analysis=raw_analysis,
        )

    def _detect_skill(self, prompt: str) -> DesignerProfile:
        """Rule-based + AI skill detection from user prompt."""
        prompt_lower = prompt.lower()
        signals_by_level = self.config.get("skill_signals", {})
        thresholds = self.config.get("skill_detection", {})
        min_conf = thresholds.get("min_confidence_for_rule_winner", 0.6)
        min_total = thresholds.get("min_total_signals_for_rule_winner", 2)

        scores = {"beginner": 0, "intermediate": 0, "expert": 0}
        for level, patterns in signals_by_level.items():
            for pat in patterns:
                if re.search(pat, prompt_lower):
                    scores[level] = scores.get(level, 0) + 1

        rule_winner = max(scores, key=scores.get) if scores else "intermediate"
        total = sum(scores.values())
        rule_confidence = scores[rule_winner] / max(total, 1)

        if rule_confidence >= min_conf and total >= min_total:
            return DesignerProfile(
                skill=rule_winner,
                goal=self._extract_goal_simple(prompt),
                confidence=rule_confidence,
            )

        raw = self.client.ask(
            system=SKILL_PARSE_SYSTEM,
            user=prompt,
            max_tokens=100,
        )
        try:
            data = json.loads(_strip_json(raw))
            return DesignerProfile(
                skill=data.get("skill", "intermediate"),
                goal=data.get("goal", prompt[:100]),
                confidence=0.85,
            )
        except (json.JSONDecodeError, KeyError):
            return DesignerProfile(
                skill="intermediate",
                goal=prompt[:100],
                confidence=0.4,
            )

    def _enrich_dump(self, dump: dict) -> dict:
        """
        Add derived data to schematic dump:
        - Power rail mapping (VCC, GND, named rails)
        - Component counts by type
        - MCU detection
        """
        enriched = dict(dump)

        power_nets = {}
        for net_name, net_data in dump.get("nets", {}).items():
            if any(kw in net_name.upper() for kw in
                   ["VCC", "VDD", "V3", "V5", "V1", "VBUS", "VBAT",
                    "GND", "AGND", "DGND", "PGND"]):
                power_nets[net_name] = {
                    "members": net_data.get("members", []),
                    "driver": net_data.get("driver"),
                    "member_count": len(net_data.get("members", [])),
                }
        enriched["power_nets"] = power_nets

        comps = dump.get("components", [])
        enriched["component_summary"] = {
            "total": len(comps),
            "by_prefix": self._count_by_prefix(comps),
            "ics": [c for c in comps if c.get("refdes", "").startswith(("U", "IC"))],
            "passives": [c for c in comps if c.get("refdes", "").startswith(("R", "C", "L"))],
            "connectors": [c for c in comps if c.get("refdes", "").startswith(("J", "P", "CN"))],
        }
        enriched["mcus"] = self._detect_mcus(comps)
        return enriched

    def _count_by_prefix(self, components: list) -> dict:
        counts = {}
        for c in components:
            prefix = re.match(r"[A-Z]+", c.get("refdes", ""))
            if prefix:
                p = prefix.group(0)
                counts[p] = counts.get(p, 0) + 1
        return counts

    def _detect_mcus(self, components: list) -> list:
        """Identify MCU/MPU components by lib_id / value patterns from config."""
        families = self.config.get("mcu_families", {})
        patterns = [re.escape(name) for name in families.keys()]
        patterns += [r"ATTINY", r"ESP8266", r"PIC\d+", r"LPC\d+",
                     r"NRF5\d", r"SAMD", r"MSP430", r"IMX\d+"]
        mcus = []
        for c in components:
            lib_id = (c.get("lib_id", "") + c.get("value", "")).upper()
            if any(re.search(p, lib_id) for p in patterns):
                mcus.append(c)
        return mcus

    def _analyze_schematic(self, enriched: dict) -> dict:
        """Send enriched dump to Claude for deep structured analysis."""
        analysis_input = {
            "power_nets": enriched.get("power_nets", {}),
            "component_summary": enriched.get("component_summary", {}),
            "mcus": enriched.get("mcus", []),
            "nets_sample": dict(list(enriched.get("nets", {}).items())[:50]),
            "labels": enriched.get("labels", []),
            "no_connect_count": enriched.get("no_connect_count", 0),
        }

        raw = self.client.ask(
            system=ANALYSIS_SYSTEM,
            user=json.dumps(analysis_input, indent=2),
            max_tokens=2000,
        )
        try:
            return json.loads(_strip_json(raw))
        except json.JSONDecodeError:
            return {"parse_error": raw}

    def _parse_report(self, raw: dict) -> SchematicReport:
        return SchematicReport(
            power_rails=raw.get("power_rails", []),
            microcontrollers=raw.get("microcontrollers", []),
            signal_integrity=raw.get("signal_integrity", {}),
            protection=raw.get("protection", {}),
            bom_summary=raw.get("bom_summary", {}),
            critical_issues=raw.get("critical_issues", []),
            warnings=raw.get("warnings", []),
            good_practices=raw.get("good_practices", []),
        )

    def _generate_feedback(
        self,
        report: SchematicReport,
        profile: DesignerProfile,
        user_prompt: str,
    ) -> str:
        """Generate skill-appropriate human-readable feedback using config-driven template."""
        skill_cfg = self.config.get("skill_levels", {}).get(profile.skill, {})
        severity = self.config.get("severity_labels", {}).get(profile.skill, {})
        max_issues = skill_cfg.get("max_issues_shown", 999)

        critical = report.critical_issues[:max_issues]
        warnings = report.warnings[:max_issues]

        template = (
            f"You are giving design-review feedback to a {profile.skill} designer.\n"
            f"Tone: {skill_cfg.get('feedback_tone', 'collegial, direct')}.\n"
            f"Sections to show: {skill_cfg.get('show_sections', ['all'])}.\n"
            f"Sections to hide: {skill_cfg.get('hide_sections', [])}.\n"
            f"Explain technical terms: {skill_cfg.get('explain_terms', False)}.\n"
            f"Issue format: {skill_cfg.get('issue_format', 'severity | location | problem | fix')}.\n"
            f"Cap visible issues at {max_issues}.\n"
            f"Severity labels — critical: {severity.get('critical', 'CRITICAL')}, "
            f"warning: {severity.get('warning', 'WARNING')}, info: {severity.get('info', 'NOTE')}.\n"
            f"Never pad. No trailing summary."
        )

        context = (
            f"Designer goal: {profile.goal}\n"
            f"Skill level: {profile.skill}\n\n"
            f"Critical issues: {json.dumps(critical, indent=2)}\n"
            f"Warnings: {json.dumps(warnings, indent=2)}\n"
            f"Good practices: {json.dumps(report.good_practices, indent=2)}\n"
            f"Power rails: {json.dumps(report.power_rails, indent=2)}\n"
            f"MCUs: {json.dumps(report.microcontrollers, indent=2)}\n"
            f"Protection: {json.dumps(report.protection, indent=2)}\n"
            f"BOM: {json.dumps(report.bom_summary, indent=2)}\n"
        )

        feedback = self.client.ask(
            system=template,
            user=context,
            max_tokens=1500,
        )
        return feedback.strip()

    def _extract_goal_simple(self, prompt: str) -> str:
        sentences = re.split(r"[.!?]", prompt)
        return sentences[0].strip()[:150] if sentences else prompt[:150]


def _strip_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()
