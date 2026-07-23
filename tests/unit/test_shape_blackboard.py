"""T138 (§V67/§V37/B63): the shared blackboard-shaping helper drops always-null
``valueStr`` keys at emit.

:func:`~arknights_mcp.services.operators.shape_blackboard` is the single §V37 home the
``get_operator`` and ``compare_operator_modules`` read services route their decoded
blackboard structures through before shaping them onto the wire. §V67 omits an
always-null optional scalar (``valueStr`` is ``null`` for ~60 numeric params on a full
operator) rather than emit ``null`` so a client is not forced to decide "none vs
unknown"; a real (non-null) ``valueStr`` string param is kept, and every other key/shape
is preserved (additive/backward-compatible, §V21).
"""

from __future__ import annotations

from arknights_mcp.services.operators import hoist_uniform_template, shape_blackboard


def test_null_valuestr_key_is_dropped() -> None:
    assert shape_blackboard([{"key": "atk", "value": 34, "valueStr": None}]) == [
        {"key": "atk", "value": 34}
    ]


def test_non_null_valuestr_is_kept() -> None:
    # A genuine string param survives -- only the always-null key is omitted.
    assert shape_blackboard([{"key": "tag", "value": 0, "valueStr": "sluggish"}]) == [
        {"key": "tag", "value": 0, "valueStr": "sluggish"}
    ]


def test_nested_blackboard_inside_change_bundle_is_cleaned() -> None:
    # A trait/talent-change bundle nests its own ``blackboard`` list; the recursion
    # reaches it so the null ``valueStr`` there is dropped too, while the surrounding
    # keys (description template, unlock condition) are preserved untouched.
    bundle = [
        {
            "blackboard": [{"key": "atk_scale", "value": 1.1, "valueStr": None}],
            "description": "Increases ATK to {atk_scale:0%}.",
            "unlockCondition": {"level": 1, "phase": "PHASE_2"},
        }
    ]
    assert shape_blackboard(bundle) == [
        {
            "blackboard": [{"key": "atk_scale", "value": 1.1}],
            "description": "Increases ATK to {atk_scale:0%}.",
            "unlockCondition": {"level": 1, "phase": "PHASE_2"},
        }
    ]


def test_non_blackboard_shapes_pass_through() -> None:
    # A None (source carried no bundle) and a non-list/non-dict scalar are returned
    # unchanged; the helper never fabricates structure.
    assert shape_blackboard(None) is None
    assert shape_blackboard(42) == 42
    # An upgrade-cost list has no ``valueStr`` -- it is left exactly as stored.
    assert shape_blackboard([{"id": "mat_1", "count": 8, "type": "MATERIAL"}]) == [
        {"id": "mat_1", "count": 8, "type": "MATERIAL"}
    ]


def test_valuestr_only_dropped_when_null_not_when_falsey() -> None:
    # ``valueStr`` is dropped only when it is exactly ``None``; an empty string is a
    # (degenerate but present) string value and is kept -- the guard tests ``is None``.
    assert shape_blackboard([{"key": "k", "value": 1, "valueStr": ""}]) == [
        {"key": "k", "value": 1, "valueStr": ""}
    ]


# --- T146 (§V66.3/§V37): the shared per-level template hoist helper -----------


def test_hoist_returns_the_single_shared_template() -> None:
    # Every level carries the identical template -> hoist it once to the parent.
    assert hoist_uniform_template(["X", "X", "X"]) == "X"
    # A single level is trivially uniform.
    assert hoist_uniform_template(["X"]) == "X"


def test_hoist_declines_when_templates_differ() -> None:
    # Differing templates -> None so the caller keeps the per-row copies (lossless).
    assert hoist_uniform_template(["X", "Y"]) is None
    # A present template mixed with a null level is NOT uniform -> keep per-row.
    assert hoist_uniform_template(["X", None]) is None


def test_hoist_returns_none_when_nothing_to_hoist() -> None:
    # No values and all-null both yield None (there is no template to hoist).
    assert hoist_uniform_template([]) is None
    assert hoist_uniform_template([None, None]) is None
