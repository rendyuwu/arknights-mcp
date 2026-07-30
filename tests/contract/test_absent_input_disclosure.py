"""§V118 absent-input contract (B165): a rule may not decline in SILENCE.

§V117 made every refusal arm declare its liveness, and the guard that enforces it reads
the arm sites out of each rule module's AST -- which means it can only see branches that
EMIT something. A branch that emits nothing has no site, so the registry read complete at
eleven arms while three refusals did not exist at all:

* ``def_res_skew`` skipped an enemy whose ``def`` AND ``res`` were both absent -- 64
  occurrences over 56 stages, one of them a boss -- while refusing out loud when only one
  of the two was missing. The enemy it knew least about was the one it said nothing about.
* ``ranged_arts`` folded an ABSENT ``damage_types`` in with the present-but-not-arts
  negative -- 227 occurrences over 181 stages.
* ``pressure_spike`` folded an absent ``total_count`` in with a genuinely low one.

And the refusals that did exist were dropped by the rules' own no-evidence gate, so a
stage where nothing else concluded showed the client nothing at all.

Three layers, mirroring the arm-liveness module:

* **static** -- every registered rule declares its gate fields, and they are real
  occurrence fields.
* **synthetic** -- a gate-absent occurrence really is named, including on a stage where
  the rule concludes nothing (the case the no-evidence gate used to swallow).
* **build sweep** -- over the promoted build, both directions: every gate-absent
  occurrence is named by one of the rule's gate arms, and every occurrence a gate arm
  names really is gate-absent (so a blanket "not judged" cannot satisfy this).

Figures are @promoted ``2026-07-30T092427Z-en-cn``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest

from arknights_mcp.analyzers.base import EnemyOccurrence, StageThreatContext
from arknights_mcp.analyzers.rules import (
    RULE_GATE_FIELDS,
    RULE_LIMITATION_ARMS,
    THREAT_RULES,
)
from arknights_mcp.analyzers.stage import SUBSTRATE_ABSENT_MARKER, analyze_stage
from arknights_mcp.services.stages import analyze_stage as analyze_stage_service

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"


def _active_build() -> Path | None:
    if not MANIFEST.is_file():
        return None
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    filename = manifest.get("database_filename")
    if not filename:
        return None
    path = REPO_ROOT / "data" / "builds" / str(filename)
    return path if path.is_file() else None


BUILD = _active_build()

requires_build = pytest.mark.skipif(
    BUILD is None,
    reason="needs a promoted build (data/current.json); run `arknights-mcp sync` first",
)

#: rule_id -> the declared arm names that refuse because a GATE field was absent. Every
#: other arm refuses about something the source DID state (an unrecognized token, a reach
#: read from a weaker field), so only these may name a gate-absent occurrence and only
#: these must.
_GATE_ARMS: dict[str, frozenset[str]] = {
    "threat.aerial": frozenset({"motion_missing"}),
    "threat.def_res_skew": frozenset({"def_missing", "res_missing", "def_and_res_missing"}),
    "threat.ranged_arts": frozenset({"damage_types_missing"}),
    "threat.pressure_spike": frozenset({"total_count_missing"}),
}


#: Gate field -> the attribute holding it on the SERVICE's occurrence row. The rule input
#: and the response row are two dataclasses over one datum, and the sweep reads the
#: response row, so the one field whose names differ (``defense`` / ``def_``, renamed
#: because ``def`` is a keyword) needs saying out loud rather than silently missing.
_FACT_ATTR = {
    "motion_type": "motion_type",
    "defense": "def_",
    "res": "res",
    "damage_types": "damage_types",
    "total_count": "total_count",
}


def _gate_markers(rule_id: str) -> list[str]:
    names = _GATE_ARMS.get(rule_id, frozenset())
    return [
        arm.marker for arm in RULE_LIMITATION_ARMS if arm.rule_id == rule_id and arm.name in names
    ]


# --- static: the declaration itself -------------------------------------------


def test_every_registered_rule_declares_its_gate() -> None:
    """A rule with no gate declaration is a rule whose silence nobody checks."""
    assert set(RULE_GATE_FIELDS) == {rule.rule_id for rule in THREAT_RULES}


def test_gate_fields_are_real_occurrence_fields() -> None:
    """A gate naming a field the occurrence does not carry can never be absent."""
    known = {field.name for field in dataclass_fields(EnemyOccurrence)}
    for rule_id, gate in RULE_GATE_FIELDS.items():
        assert gate <= known, f"{rule_id}: gate names non-fields {sorted(gate - known)}"
        assert gate <= set(_FACT_ATTR), f"{rule_id}: gate field with no response attribute"


def test_every_gate_arm_name_is_declared() -> None:
    """The gate map above and :data:`RULE_LIMITATION_ARMS` must agree.

    Renaming an arm without renaming it here would silently empty the marker list, and a
    sweep with no markers passes by finding nothing to check.
    """
    declared = {(arm.rule_id, arm.name) for arm in RULE_LIMITATION_ARMS}
    for rule_id, names in _GATE_ARMS.items():
        for name in names:
            assert (rule_id, name) in declared, f"{rule_id}/{name}: no such declared arm"
        assert _gate_markers(rule_id), f"{rule_id}: gate arms declared but no markers resolved"


def test_every_rule_with_a_gate_has_gate_arms() -> None:
    """A per-occurrence gate with no arm to refuse through is the B165 shape itself."""
    for rule_id, gate in RULE_GATE_FIELDS.items():
        arms = sorted(_GATE_ARMS.get(rule_id, frozenset()))
        assert bool(gate) == bool(arms), f"{rule_id}: gate fields {sorted(gate)} but arms {arms}"


# --- synthetic: the refusal exists, and it survives a rule that concludes nothing ---


def _occurrence(game_id: str, **fields: object) -> EnemyOccurrence:
    defaults: dict[str, object] = {
        "display_name": None,
        "motion_type": None,
        "damage_types": None,
        "total_count": None,
    }
    return EnemyOccurrence(game_id=game_id, **{**defaults, **fields})  # type: ignore[arg-type]


def _texts(ctx: StageThreatContext, rule_id: str) -> list[str]:
    """Every refusal the rule got out, through EITHER carrier (§V118 b).

    The channel is what B165 was half about: a limitation rides the observation when the
    rule concluded something and rides ``warnings`` when it did not, and a guard that
    only reads one of them re-creates the hole it is here to close. Warnings are
    stage-scoped, so they are matched by marker rather than attributed by rule.
    """
    result = analyze_stage(ctx)
    return [
        *(
            limitation
            for obs in result.observations
            if obs.rule_id == rule_id
            for limitation in obs.limitations
        ),
        *result.warnings,
    ]


@pytest.mark.parametrize("rule_id", sorted(rid for rid, gate in RULE_GATE_FIELDS.items() if gate))
def test_a_gate_absent_occurrence_is_named_even_when_nothing_concludes(rule_id: str) -> None:
    """The whole defect in one stage: one enemy, nothing known, nothing else to carry it.

    Before §T214 this produced an empty analysis for every rule -- no observation, so no
    limitations, so a client that asked about this stage was told nothing was wrong with
    it. Each rule's gate fields are left at their absent default here, so the occurrence
    trips every gate at once and each rule must still name it.
    """
    ctx = StageThreatContext(
        server="en",
        stage_game_id="main_04-04",
        stage_code="4-4",
        occurrences=(_occurrence("enemy_0000_unknown"),),
    )
    texts = _texts(ctx, rule_id)
    markers = _gate_markers(rule_id)
    assert any(
        marker in text and "enemy_0000_unknown" in text for text in texts for marker in markers
    ), f"{rule_id}: declined a gate-absent occurrence in silence (§V118 a) -- got {texts}"


def test_def_res_skew_names_the_both_missing_case() -> None:
    """B165's filed arm: 64 occurrences that used to hit a bare ``continue``."""
    ctx = StageThreatContext(
        server="en",
        stage_game_id="main_04-04",
        stage_code="4-4",
        occurrences=(
            _occurrence("enemy_1544_cledub", defense=None, res=None),
            _occurrence("enemy_0003_wall", defense=900, res=0),
        ),
    )
    texts = _texts(ctx, "threat.def_res_skew")
    assert any(
        "enemy_1544_cledub: def and res both missing; damage-type skew not assessed" in text
        for text in texts
    ), texts


def test_a_present_gate_is_not_refused() -> None:
    """The mirror: a fully typed enemy earns no "not assessed" line from any rule.

    Without this the forward check is satisfiable by refusing on everything, which would
    be honest about nothing and would bury the 64 real cases in 28302 rows of noise.
    """
    ctx = StageThreatContext(
        server="en",
        stage_game_id="main_04-04",
        stage_code="4-4",
        occurrences=(
            _occurrence(
                "enemy_0005_full",
                display_name="Typed",
                motion_type="WALK",
                damage_types=("PHYSIC",),
                total_count=3,
                defense=300,
                res=10,
                attack_range=0.0,
                targeting="MELEE",
                first_spawn_time=1.0,
                last_spawn_time=2.0,
            ),
        ),
        route_count=1,
        tiles=None,
    )
    for rule_id in RULE_GATE_FIELDS:
        for marker in _gate_markers(rule_id):
            assert not any(
                "enemy_0005_full" in text and marker in text for text in _texts(ctx, rule_id)
            )


def test_substrate_absence_is_stated_once() -> None:
    """§V118 (c): an unimported stage says so, and says it one time (§V66)."""
    empty = StageThreatContext(
        server="en", stage_game_id="act10d5_01", stage_code="SV-1", occurrences=()
    )
    warnings = analyze_stage(empty).warnings
    said = [text for text in warnings if SUBSTRATE_ABSENT_MARKER in text]
    assert len(said) == 1, warnings
    assert "enemy, tile or route" in said[0], said[0]


# --- build sweep: both directions over the promoted corpus ---------------------


@pytest.fixture(scope="module")
def sweep() -> tuple[Counter[str], Counter[str], int, int]:
    """Named/unnamed tallies per rule, plus the substrate stage counts.

    One pass over every stage of the build, because the arms this module exists for fire
    on 64 and 227 of 28302 occurrences -- a sample that misses them reports the defect as
    fixed either way.
    """
    assert BUILD is not None
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    named: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    substrate_stages = 0
    substrate_said = 0
    try:
        for server, game_id in conn.execute("SELECT server, game_id FROM stages"):
            result = analyze_stage_service(conn, server=server, game_id=game_id)
            texts = [
                *(
                    (obs.rule_id, limitation)
                    for obs in result.observations
                    for limitation in obs.limitations
                ),
                *((None, warning) for warning in result.warnings),
            ]
            if not result.occurrences:
                substrate_stages += 1
                if any(SUBSTRATE_ABSENT_MARKER in text for _, text in texts):
                    substrate_said += 1
            for rule_id, gate in RULE_GATE_FIELDS.items():
                if not gate:
                    continue
                markers = _gate_markers(rule_id)
                refused = {
                    occ.game_id
                    for _, text in texts
                    for occ in result.occurrences
                    if occ.game_id in text and any(marker in text for marker in markers)
                }
                # Grouped by game_id, not per row: one enemy at several level variants is
                # one enemy (§V35), and its stats come off the variant, so one variant may
                # carry a stat the next one lacks. The rule refuses per occurrence and the
                # client reads the enemy, so the question is whether the ENEMY was named.
                absent_ids = {
                    occ.game_id
                    for occ in result.occurrences
                    if any(getattr(occ, _FACT_ATTR[field]) is None for field in gate)
                }
                for game_id in sorted({occ.game_id for occ in result.occurrences}):
                    if game_id in absent_ids:
                        if game_id in refused:
                            named[rule_id] += 1
                        else:
                            missed[rule_id] += 1
                    elif game_id in refused:
                        missed[f"{rule_id}:false-refusal"] += 1
    finally:
        conn.close()
    return named, missed, substrate_stages, substrate_said


@requires_build
def test_no_gate_absent_occurrence_is_declined_in_silence(
    sweep: tuple[Counter[str], Counter[str], int, int],
) -> None:
    """§V118 (a), forward: the direction that fails on the code B165 was filed against."""
    _, missed, _, _ = sweep
    assert not missed, (
        f"occurrences declined without a word: {dict(missed)} -- a rule that cannot judge an "
        "enemy must say so; absence is not a negative (§V118 a)"
    )


@requires_build
def test_the_counted_gate_refusals_are_still_there(
    sweep: tuple[Counter[str], Counter[str], int, int],
) -> None:
    """The figures B165 was filed on, kept as figures rather than as memory.

    ``def_res_skew`` names every enemy missing either stat -- 198 stage-enemy pairs over
    64 both-missing + 136 res-only-missing occurrences -- and ``ranged_arts`` names the
    223 pairs behind its 227. If upstream starts filling these the numbers move, which is
    the prompt to re-count the arms, not to loosen the guard.
    """
    named, _, _, _ = sweep
    assert named["threat.def_res_skew"] == 198, named
    assert named["threat.ranged_arts"] == 223, named
    assert named["threat.aerial"] == 0, named
    assert named["threat.pressure_spike"] == 0, named


@requires_build
def test_every_unimported_stage_says_so(
    sweep: tuple[Counter[str], Counter[str], int, int],
) -> None:
    """§V118 (c): 1749 stages returned ``ok`` + nothing at all before §T214."""
    _, _, stages, said = sweep
    assert stages == said, f"{stages - said} stage(s) with no enemy data stayed silent (§V118 c)"
    assert stages == 1765, stages
