"""Arm-liveness contract: the unit that must be counted is the ARM.

Every registered rule owns a deciding field a real build populates, and
``threat.aerial`` passed it comfortably -- 1030 observations over the promoted corpus.
It also emitted **zero** limitations, because both of its refusal arms were dead:
``motion_type`` is NULL on 0/3879 enemies (and the occurrence COALESCEs a variant over
that never-NULL base), and the "unrecognized motion_type" arm could not fire at all,
because the motion vocabulary declared ten tokens over a corpus that sends two. Those
eight guessed members were not inert padding: each one silently ANSWERED the case the
unrecognized arm exists to refuse, so the vocabulary disarmed the guard that would have
caught the vocabulary. A rule-level count cannot see any of this -- the rule fires.

So this module counts branches, not rules, in three layers:

* **static** -- every limitation-emitting site is resolved out of each rule module's own
  AST and matched against :data:`RULE_LIMITATION_ARMS`. It fails both ways: a new arm
  with no declaration, and a declaration whose text no longer exists in the code. Read
  from the module rather than from a hand-written list, because a hand list is exactly
  the thing that drifts once and then agrees with itself forever.
* **reachability** -- every ``dead_today`` arm is fired on purpose from a synthetic
  occurrence. ``dead_today`` is a statement about the corpus; an arm no input can reach
  is not dead, it is dishonest, and retiring it is what to do with it instead.
* **build sweep** -- every rule is run over every stage of the promoted build and each
  emitted refusal is mapped back to its declared arm. A ``live`` arm that fires zero
  times is the defect returning; a ``dead_today`` arm that fires is a declaration gone
  stale; a refusal that maps to no arm is an undeclared branch that the static layer
  somehow let through.

All three layers cover BOTH channels: a
rule that concludes nothing now sends its refusals out as ``warnings``, so a guard that
reads only limitations would report the arms that fix restored as dead again -- and the
two conflict warnings ``ranged_arts`` always emitted turn out to be refusals this
module never counted at all.

Counts in the declaration are @promoted ``2026-07-30T092427Z-en-cn``; the static and
reachability layers run offline, and only the sweep needs a build.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from collections import Counter
from itertools import product
from pathlib import Path

import pytest

from arknights_mcp.analyzers.base import EnemyOccurrence, StageThreatContext
from arknights_mcp.analyzers.rules import (
    ARM_STATUSES,
    RULE_LIMITATION_ARMS,
    THREAT_RULES,
    LimitationArm,
)
from arknights_mcp.analyzers.rules._common import FLY_MOTIONS, GROUND_MOTIONS
from arknights_mcp.analyzers.stage import SUBSTRATE_ABSENT_MARKER, analyze_stage
from arknights_mcp.services.stages import analyze_stage as analyze_stage_service

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "data" / "current.json"
_RULES_DIR = REPO_ROOT / "src" / "arknights_mcp" / "analyzers" / "rules"

#: Placeholder for an interpolation the resolver cannot evaluate (``{occ.game_id}``).
_HOLE = "\x00"


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

#: rule_id -> the module its arms live in. Every registered rule is a one-module rule,
#: and ``test_every_registered_rule_module_is_mapped`` keeps that true.
_RULE_MODULES = {
    "threat.aerial": "aerial.py",
    "threat.def_res_skew": "def_res_skew.py",
    "threat.ranged_arts": "ranged_arts.py",
    "threat.pressure_spike": "pressure_spike.py",
    "threat.lane_route": "lane_route.py",
    "threat.tiles_deploy": "tiles_deploy.py",
}


# --- static layer: resolve the arm sites out of the rule module ----------------


def _module_constants(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """Every ``NAME = <expr>`` binding in the module, module-level or local.

    Local ones matter as much as module constants: ``def_res_skew`` builds the stat name
    it refuses on with ``missing = "def" if d is None else "res"`` inside the loop, and a
    resolver that only reads the top level sees one arm where the code has two. A name
    bound more than once contributes every binding, since the site can carry any of them.
    """
    consts: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts.setdefault(target.id, []).append(node.value)
    return consts


def _module_returns(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """Every module-level helper -> the expressions it can return.

    ``ranged_arts`` words its "arts damage but no attack reach" refusal in a helper
    and appends the CALL, so the resolver saw a call whose arguments are three variables
    and produced nothing but holes. An arm composed one function away is still an arm, so
    the helper's own returns are resolved instead of its arguments.
    """
    returns: dict[str, list[ast.expr]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            returns[node.name] = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Return) and child.value is not None
            ]
    return returns


def _texts(
    node: ast.expr,
    consts: dict[str, list[ast.expr]],
    returns: dict[str, list[ast.expr]] | None = None,
) -> list[str]:
    """Every string an expression can evaluate to, with holes for what is unknown.

    Handles the shapes a limitation is actually written in: literals, implicit
    concatenation, f-strings, a module constant referenced by name, a tuple of them, and
    the ``"def" if d is None else "res"`` ternary -- which is the one that matters,
    because ``def_res_skew`` writes TWO arms through ONE ``limitations.append``. Reading
    that site as a single text would report both declarations as stale.

    A call (``fuller_view_note(...)``) is approximated by joining its string arguments,
    not by listing them separately: every candidate has to carry a declared marker, and
    the ``flag="include_map"`` argument riding beside the sentence is not an arm of its
    own, so splitting them would demand a declaration for a fragment nobody refuses with.
    """
    returns = returns if returns is not None else {}
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else [_HOLE]
    if isinstance(node, ast.JoinedStr):
        parts = [_texts(value, consts, returns) for value in node.values]
        return ["".join(combo) for combo in product(*parts)] if parts else [""]
    if isinstance(node, ast.FormattedValue):
        return _texts(node.value, consts, returns)
    if isinstance(node, ast.IfExp):
        return _texts(node.body, consts, returns) + _texts(node.orelse, consts, returns)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [
            left + right
            for left, right in product(
                _texts(node.left, consts, returns), _texts(node.right, consts, returns)
            )
        ]
    if isinstance(node, ast.Name):
        bindings = consts.get(node.id, [])
        return [text for binding in bindings for text in _texts(binding, consts, returns)] or [
            _HOLE
        ]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [text for element in node.elts for text in _texts(element, consts, returns)]
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in returns:
            # A helper defined in this module: its RETURNS are the arm's text. Only
            # module-local ones -- an imported helper is resolved by its arguments below,
            # which is how the shared ``fuller_view_note`` sentence stays readable.
            bodies = returns[node.func.id]
            return [text for body in bodies for text in _texts(body, consts, returns)] or [_HOLE]
        args: list[ast.expr] = [*node.args, *(kw.value for kw in node.keywords)]
        parts = [_texts(arg, consts, returns) for arg in args]
        return [" ".join(combo) for combo in product(*parts)] if parts else [_HOLE]
    return [_HOLE]


def _arm_sites(module: str) -> list[list[str]]:  # noqa: C901
    """Each limitation-emitting site in a rule module, as its candidate texts.

    A site is an ``append`` onto the local ``limitations`` or ``warnings`` list, or either
    handed over as a keyword -- the ways every rule in the package states a refusal.
    Anything new that emits one has to be one of these, or the sweep catches it.

    ``warnings`` joined the set: a rule that concludes nothing now
    routes its refusals there, and the two conflict warnings ``ranged_arts`` has
    always emitted were refusals this guard never saw, because it only ever read the
    ``limitations`` channel.

    A literal SEQUENCE of limitations is N sites, not one: reading
    ``limitations=(_TILE_LAYOUT_ROUTE, "...")`` as a single site lets one declared
    element vouch for an undeclared sibling, which is the same "a guard that cannot
    fail" shape this module exists to close.
    """
    tree = ast.parse((_RULES_DIR / module).read_text(encoding="utf-8"))
    consts = _module_constants(tree)
    returns = _module_returns(tree)
    sites: list[list[str]] = []

    def _emit(node: ast.expr) -> None:
        if isinstance(node, (ast.Tuple, ast.List)):
            for element in node.elts:
                _emit(element)
            return
        # Drop candidates that resolve to nothing but holes: ``limitations=tuple(
        # limitations)`` is the plumbing that hands the accumulated list to the
        # Observation, not an arm with text of its own. Every real arm in the package
        # carries a literal, and the build sweep covers anything that somehow does not.
        texts = [text for text in _texts(node, consts, returns) if text.replace(_HOLE, "").strip()]
        if texts:
            sites.append(texts)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"append", "extend"}
                and isinstance(func.value, ast.Name)
                and func.value.id in {"limitations", "warnings"}
            ):
                for arg in node.args:
                    _emit(arg)
            for keyword in node.keywords:
                if keyword.arg in {"limitations", "warnings"}:
                    _emit(keyword.value)
    return sites


def _arms_for(rule_id: str) -> tuple[LimitationArm, ...]:
    return tuple(arm for arm in RULE_LIMITATION_ARMS if arm.rule_id == rule_id)


def test_every_registered_rule_module_is_mapped() -> None:
    """A rule the map does not know is a rule whose arms are never read."""
    registered = {rule.rule_id for rule in THREAT_RULES}
    assert set(_RULE_MODULES) == registered
    for module in _RULE_MODULES.values():
        assert (_RULES_DIR / module).is_file(), module


def test_declaration_is_well_formed() -> None:
    """Status from the closed set, a counted witness on every arm, unique names."""
    assert RULE_LIMITATION_ARMS, "no arms declared at all"
    registered = {rule.rule_id for rule in THREAT_RULES}
    seen: set[tuple[str, str]] = set()
    for arm in RULE_LIMITATION_ARMS:
        assert arm.rule_id in registered, f"{arm.rule_id}: arm declared for an unregistered rule"
        assert arm.status in ARM_STATUSES, f"{arm.rule_id}/{arm.name}: bad status {arm.status!r}"
        assert arm.marker.strip(), f"{arm.rule_id}/{arm.name}: empty marker"
        assert arm.counted.strip(), f"{arm.rule_id}/{arm.name}: status asserted, not counted"
        key = (arm.rule_id, arm.name)
        assert key not in seen, f"duplicate arm {key}"
        seen.add(key)


@pytest.mark.parametrize("rule_id", sorted(_RULE_MODULES))
def test_every_arm_site_in_the_module_is_declared(rule_id: str) -> None:
    """An arm the code can emit and the declaration does not name.

    This is the direction that would have caught the defect before a build existed. The sites
    come out of the module's own AST, so adding a refusal without declaring its liveness
    fails here rather than four milestones later on a zero count.
    """
    sites = _arm_sites(_RULE_MODULES[rule_id])
    assert sites, f"{rule_id}: no limitation site found -- did the rule stop refusing?"
    markers = [arm.marker for arm in _arms_for(rule_id)]
    for candidates in sites:
        # EVERY text the site can emit, not merely one of them: ``def_res_skew`` writes
        # two arms through one f-string, so a site that only has to match once would let
        # the live half vouch for the dead one.
        for text in candidates:
            assert any(marker in text for marker in markers), (
                f"{rule_id}: undeclared refusal arm {text!r} -- declare it in "
                "RULE_LIMITATION_ARMS with the count that makes its status true"
            )


@pytest.mark.parametrize("rule_id", sorted(_RULE_MODULES))
def test_every_declared_arm_still_exists_in_the_module(rule_id: str) -> None:
    """The mirror: a declaration whose text the rule no longer emits.

    Without this, deleting an arm leaves a ``dead_today`` row behind that the sweep then
    confirms forever -- zero fires, exactly as declared, for a branch that is gone.
    """
    candidates = [text for site in _arm_sites(_RULE_MODULES[rule_id]) for text in site]
    for arm in _arms_for(rule_id):
        assert any(arm.marker in text for text in candidates), (
            f"{rule_id}/{arm.name}: declared marker {arm.marker!r} is in no limitation site"
        )


# --- reachability: dead_today is about the corpus, never about the code -------


def _stage(*occurrences: EnemyOccurrence) -> StageThreatContext:
    return StageThreatContext(
        server="en",
        stage_game_id="main_04-04",
        stage_code="4-4",
        occurrences=occurrences,
    )


def _limitations(ctx: StageThreatContext, rule_id: str) -> list[str]:
    """The rule's refusals through both carriers: observation, then warnings."""
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


def _occurrence(game_id: str, **fields: object) -> EnemyOccurrence:
    """An occurrence with the four always-required inputs defaulted to "absent".

    ``display_name``/``damage_types``/``total_count`` carry no meaning for the arms
    proven below, and ``None`` is what the repository hands a rule when the source said
    nothing -- so defaulting them keeps each test naming only the field its own arm
    turns on.
    """
    defaults: dict[str, object] = {
        "display_name": None,
        "motion_type": None,
        "damage_types": None,
        "total_count": None,
    }
    return EnemyOccurrence(game_id=game_id, **{**defaults, **fields})  # type: ignore[arg-type]


def _flyer() -> EnemyOccurrence:
    """A confirmed flyer, because the aerial rule drops its limitations without one.

    ``if not evidence: return RuleResult()`` -- a stage where no enemy is judged aerial
    emits no observation, and the refusal goes with it. That makes the arm narrower than
    its branch condition reads, so a reachability proof that omits the flyer proves the
    wrong thing.
    """
    return _occurrence("enemy_0001_flyer", display_name="Drone", motion_type="FLY")


def test_aerial_motion_missing_arm_is_reachable() -> None:
    """NULL motion really can arrive -- the column is nullable, not empty."""
    limitations = _limitations(
        _stage(_flyer(), _occurrence("enemy_0002_x", motion_type=None)),
        "threat.aerial",
    )
    assert any("motion_type missing; not judged as aerial" in text for text in limitations)


def test_aerial_unrecognized_motion_arm_is_reachable() -> None:
    """The arm the defect was about: an unseen token must REFUSE, not resolve.

    ``SWIM`` is one of the eight tokens the vocabulary used to declare, so before the fix
    this input produced a silent ground classification -- no limitation, no evidence, and
    an enemy quietly reported as not aerial on the strength of a guess.
    """
    limitations = _limitations(
        _stage(_flyer(), _occurrence("enemy_0002_x", motion_type="SWIM")),
        "threat.aerial",
    )
    assert any("unrecognized motion_type='SWIM'" in text for text in limitations)


def test_motion_vocabulary_carries_no_token_the_corpus_never_sends() -> None:
    """The classifier's own vocabulary: two sets, two tokens, no overlap.

    The offline half of the vocabulary check -- the build half compares these against the
    counted domain. A token added here without a count is the defect returning.
    """
    assert frozenset({"FLY"}) == FLY_MOTIONS
    assert frozenset({"WALK"}) == GROUND_MOTIONS
    assert not (FLY_MOTIONS & GROUND_MOTIONS)


def test_def_res_skew_def_missing_arm_is_reachable() -> None:
    """def-only-NULL is 0/28302 on the build, but both columns are independently NULLable.

    The skewed enemy is not decoration: like the aerial rule, this one returns an empty
    result when nothing was concluded, so the refusal only reaches a client when some
    OTHER enemy in the same stage produced a conclusion to carry it.
    """
    limitations = _limitations(
        _stage(
            _occurrence("enemy_0003_x", defense=None, res=30),
            _occurrence("enemy_0003_wall", defense=900, res=0),
        ),
        "threat.def_res_skew",
    )
    assert any("def missing; damage-type skew not assessed" in text for text in limitations)


def test_pressure_spike_spawn_timing_missing_arm_is_reachable() -> None:
    """A spike whose spawn bounds are absent: 0 rows on the build, still reachable."""
    limitations = _limitations(
        _stage(
            _occurrence("enemy_0004_x", total_count=12, first_spawn_time=None, last_spawn_time=None)
        ),
        "threat.pressure_spike",
    )
    assert any("spawn timing missing; burst window unconfirmed" in text for text in limitations)


def test_pressure_spike_total_count_missing_arm_is_reachable() -> None:
    """An unknown arrival count, which used to read as a small one.

    NULL on 0/28302 rows today, and nullable, so it is declared rather than dropped. The
    stage fields nothing else, which is the point: the refusal has to reach a client with
    no observation to ride on.
    """
    limitations = _limitations(_stage(_occurrence("enemy_0006_x")), "threat.pressure_spike")
    assert any("total_count missing; spawn pressure not assessed" in text for text in limitations)


def test_every_dead_arm_has_a_reachability_proof() -> None:
    """Accounting: a ``dead_today`` arm with no test above is an undischarged claim.

    Named explicitly so adding a fifth dead arm without proving it can fire fails here,
    rather than resting on the sweep -- which confirms zero fires either way and cannot
    tell "the corpus never triggers it" from "no input ever could".
    """
    proven = {
        ("threat.aerial", "motion_missing"),
        ("threat.aerial", "motion_unrecognized"),
        ("threat.def_res_skew", "def_missing"),
        ("threat.pressure_spike", "spawn_timing_missing"),
        ("threat.pressure_spike", "total_count_missing"),
    }
    declared_dead = {
        (arm.rule_id, arm.name) for arm in RULE_LIMITATION_ARMS if arm.status == "dead_today"
    }
    assert declared_dead == proven, (
        "a dead_today arm without a reachability proof above -- prove it can fire, or "
        "retire it the way the arms nothing could reach were retired"
    )


# --- build sweep: the only witness that a branch fires on the real corpus ------


def _sweep() -> Counter[tuple[str, str]]:
    """Every rule over every stage of the promoted build, tallied per declared arm.

    Whole corpus, not a sample: an arm that fires on 56 of 28302 occurrences is exactly
    the kind a sample reports as dead, and mistaking a live arm for a dead one is how a
    real refusal gets deleted.
    """
    assert BUILD is not None
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    try:
        fires: Counter[tuple[str, str]] = Counter()
        unmapped: list[tuple[str, str]] = []
        for server, game_id in conn.execute("SELECT server, game_id FROM stages"):
            result = analyze_stage_service(conn, server=server, game_id=game_id)
            for obs in result.observations:
                arms = _arms_for(obs.rule_id)
                for limitation in obs.limitations:
                    matched = [arm for arm in arms if arm.marker in limitation]
                    if not matched:
                        unmapped.append((obs.rule_id, limitation))
                        continue
                    for arm in matched:
                        fires[(arm.rule_id, arm.name)] += 1
            # A rule that concludes nothing sends its refusals out as
            # warnings, so counting only the observation channel would report the arms
            # this project just fixed as dead again -- and would let a genuinely new
            # undeclared refusal ride out unmapped. Warnings are stage-scoped, so the
            # marker attributes them; the stage's own substrate disclosure is the one
            # warning that belongs to no rule.
            for warning in result.warnings:
                if SUBSTRATE_ABSENT_MARKER in warning:
                    continue
                matched = [arm for arm in RULE_LIMITATION_ARMS if arm.marker in warning]
                if not matched:
                    unmapped.append(("<warnings>", warning))
                    continue
                for arm in matched:
                    fires[(arm.rule_id, arm.name)] += 1
    finally:
        conn.close()
    assert not unmapped, (
        f"{len(unmapped)} limitation(s) map to no declared arm, e.g. {unmapped[:2]} -- an "
        "undeclared refusal branch"
    )
    return fires


@pytest.fixture(scope="module")
def sweep() -> Counter[tuple[str, str]]:
    return _sweep()


@requires_build
@pytest.mark.parametrize(
    "arm", RULE_LIMITATION_ARMS, ids=[f"{a.rule_id}:{a.name}" for a in RULE_LIMITATION_ARMS]
)
def test_declared_status_matches_the_build(
    arm: LimitationArm, sweep: Counter[tuple[str, str]]
) -> None:
    """Both ways: ``live`` must fire, ``dead_today`` must not.

    The half that would have caught the defect on a promoted corpus, and the half that keeps
    the declaration from rotting once upstream starts sending a value it did not before
    -- a ``dead_today`` arm that begins firing is not a failure of the rule, it is the
    prompt to re-count it and move the row.
    """
    fires = sweep[(arm.rule_id, arm.name)]
    if arm.status == "live":
        assert fires > 0, (
            f"{arm.rule_id}/{arm.name} is declared live and fired 0 times on the build -- "
            "this is the defect's shape returning; re-count it and declare it dead_today, or fix "
            "the branch that can no longer be reached"
        )
    else:
        assert fires == 0, (
            f"{arm.rule_id}/{arm.name} is declared dead_today and fired {fires} times -- the "
            "corpus changed; re-count the arm and move the row to live"
        )


@requires_build
def test_aerial_still_emits_no_limitations_and_says_so(sweep: Counter[tuple[str, str]]) -> None:
    """The headline figure, kept as a figure rather than a memory.

    1030 observations, zero limitations. The point is not that zero is wrong -- the
    corpus really does type every motion -- but that it must be DECLARED, so nobody
    reads the observation count as evidence that the rule's refusals work.
    """
    assert sweep[("threat.aerial", "motion_missing")] == 0
    assert sweep[("threat.aerial", "motion_unrecognized")] == 0
    dead = [arm for arm in _arms_for("threat.aerial") if arm.status == "dead_today"]
    assert len(dead) == len(_arms_for("threat.aerial")), (
        "every aerial arm is dead on this corpus; if one went live, say so in the declaration"
    )


@requires_build
def test_the_vocabulary_equals_the_counted_motion_domain() -> None:
    """The classifier's sets are the corpus's tokens, no more, no less.

    Fails both ways on purpose. A token added to the code that the build never sends is
    the defect exactly. A token the build starts sending that the code does not classify is the
    prompt to ground it in a source and count it -- and until that happens the
    unrecognized arm carries it, which is the whole reason that arm was worth saving.
    """
    assert BUILD is not None
    conn = sqlite3.connect(f"file:{BUILD}?mode=ro", uri=True)
    try:
        stored = {
            (row[0] or "").upper()
            for row in conn.execute(
                "SELECT DISTINCT COALESCE(v.motion_type, e.motion_type) "
                "FROM stage_enemies se "
                "JOIN enemies e ON e.enemy_pk = se.enemy_pk "
                "LEFT JOIN stage_enemy_variants v ON v.variant_pk = se.variant_pk"
            )
        }
    finally:
        conn.close()
    assert stored == FLY_MOTIONS | GROUND_MOTIONS, (
        f"motion vocabulary {sorted(FLY_MOTIONS | GROUND_MOTIONS)} != counted domain "
        f"{sorted(stored)}"
    )
