"""T36: server instructions (PRD §13.1). The first 512 characters must carry the
facts/observations/recommendations distinction + the never-invent rule, and the
text must respect §V6 (observations = evidence-backed inference) and §V7
(recommendations = optional/capability-based, never mandatory).
"""

from __future__ import annotations

from arknights_mcp.instructions import (
    BLACKBOARD_KEY_GLOSSARY,
    FIRST_SEGMENT_CHARS,
    SERVER_INSTRUCTIONS,
    core_segment,
    server_instructions,
)


def test_server_instructions_is_stable_nonempty_text() -> None:
    assert isinstance(SERVER_INSTRUCTIONS, str)
    assert SERVER_INSTRUCTIONS.strip()
    # Deterministic: both transports get the exact same text (§V14).
    assert server_instructions() == SERVER_INSTRUCTIONS
    assert server_instructions() == server_instructions()


def test_first_512_chars_carry_the_core_distinction() -> None:
    # PRD §13.1: a truncating client sees only the leading segment, so it must
    # stand alone with the three-tier distinction + the never-invent rule.
    head = core_segment()
    assert head == SERVER_INSTRUCTIONS[:FIRST_SEGMENT_CHARS]
    assert len(head) <= FIRST_SEGMENT_CHARS
    lowered = head.lower()
    for tier in ("facts", "observations", "recommendations"):
        assert tier in lowered, f"{tier!r} missing from first {FIRST_SEGMENT_CHARS} chars"
    # Prohibition against inventing missing data, in the truncation-safe segment.
    assert "never invent" in lowered


def test_observations_framed_as_deterministic_evidence_backed() -> None:
    # §V6: observations = deterministic inference carrying rule_id + evidence +
    # confidence + limitations; the host guidance must frame them as system
    # inference, not source-backed facts.
    lowered = SERVER_INSTRUCTIONS.lower()
    assert "deterministic" in lowered
    for token in ("rule_id", "evidence", "confidence", "limitations"):
        assert token in lowered, f"observation guidance omits {token!r}"


def test_recommendations_capability_based_never_mandatory() -> None:
    # §V7: recommendations are optional + capability-based; the instructions must
    # never present one as mandatory or a universal best.
    lowered = SERVER_INSTRUCTIONS.lower()
    assert "capability" in lowered
    assert "optional" in lowered
    # The only mention of mandatory/best is the prohibition itself.
    assert "never mandatory or best-in-slot" in lowered


def test_blackboard_glossary_folded_in_after_core_segment() -> None:
    # §V84/§T169 (B89): the common-key glossary lives once here (was duplicated into two
    # tool descriptions). It elaborates only, so it follows the truncation-safe core
    # segment -- a truncating client still keeps the three-tier distinction intact.
    assert BLACKBOARD_KEY_GLOSSARY in SERVER_INSTRUCTIONS
    assert BLACKBOARD_KEY_GLOSSARY not in core_segment()
    for key in ("atk_scale", "attack@times", "stun", "prob", "max_hp"):
        assert key in SERVER_INSTRUCTIONS, key


def test_covers_remaining_prd_13_1_host_directives() -> None:
    # The rest of the PRD §13.1 directives the server must give the host model.
    lowered = SERVER_INSTRUCTIONS.lower()
    for token in (
        "region",
        "get_data_status",
        "get_data_sources",
        "community consensus",
        "limitations",
    ):
        assert token in lowered, f"PRD §13.1 directive missing: {token!r}"
