"""Deterministic evidence-backed analyzers (§V6, §V26).

Every observation carries ``rule_id`` + evidence + confidence + limitations +
``analyzer_version`` (§V6); rules match on typed fields only, never on prose
(§V26). Public entry point: :func:`analyze_stage`.
"""

from __future__ import annotations

from arknights_mcp.analyzers.base import (
    ANALYZER_VERSION,
    EnemyOccurrence,
    EvidenceItem,
    Observation,
    RuleResult,
    StageThreatContext,
    StageTiles,
    ThreatRule,
)
from arknights_mcp.analyzers.farming import (
    DropFact,
    FarmingAnalysis,
    FarmingContext,
    analyze_farming,
)
from arknights_mcp.analyzers.module import (
    ModuleAnalysis,
    ModuleAnalysisContext,
    ModuleInput,
    ModuleLevelInput,
    ModuleStat,
    ModuleTalentChange,
    analyze_modules,
)
from arknights_mcp.analyzers.rules import THREAT_RULES
from arknights_mcp.analyzers.stage import StageAnalysis, analyze_stage

__all__ = [
    "ANALYZER_VERSION",
    "THREAT_RULES",
    "DropFact",
    "EnemyOccurrence",
    "EvidenceItem",
    "FarmingAnalysis",
    "FarmingContext",
    "ModuleAnalysis",
    "ModuleAnalysisContext",
    "ModuleInput",
    "ModuleLevelInput",
    "ModuleStat",
    "ModuleTalentChange",
    "Observation",
    "RuleResult",
    "StageAnalysis",
    "StageThreatContext",
    "StageTiles",
    "ThreatRule",
    "analyze_farming",
    "analyze_modules",
    "analyze_stage",
]
