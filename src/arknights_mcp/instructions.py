"""Server instructions surfaced to the MCP host model (§T36; PRD §13.1; §V6/§V7).

The MCP ``initialize`` response carries an ``instructions`` string that tells the
host model how to treat this server's output. Both transports pass the same text
(§V14). PRD §13.1 is binding: because some clients truncate long instructions,
the first :data:`FIRST_SEGMENT_CHARS` characters must stand alone -- they carry
the core facts/observations/recommendations distinction plus the prohibition
against inventing missing data.

The three tiers map to invariants:

* **facts** -- source-backed data; every fact carries region + provenance (§V5).
* **observations** -- deterministic system inference; every observation carries
  ``rule_id`` + evidence + confidence + limitations + ``analyzer_version`` (§V6).
* **recommendations** -- optional, capability-based suggestions; never labelled
  mandatory or universal-best (§V7).

This text is static project prose, authored here -- never assembled from imported
source strings (§V18/§V31). Imported data is only ever returned as structured
facts, never concatenated into these instructions.
"""

from __future__ import annotations

#: PRD §13.1 truncation budget. Clients may truncate long instructions, so the
#: leading segment (this many characters) must stand alone: it carries the
#: facts/observations/recommendations distinction + the never-invent rule. This
#: is a distinct concept from ``util.text.DEFAULT_MAX_TEXT_LENGTH`` (max length of
#: an imported string) -- do not couple them despite the coincident value.
FIRST_SEGMENT_CHARS = 512

#: Leading segment -- kept under FIRST_SEGMENT_CHARS so it survives client
#: truncation with the three-tier distinction + never-invent rule intact (§13.1).
#: Built from short source lines (E501) then newline-joined into 5 logical lines.
_LEAD = "\n".join(
    (
        "Arknights Intelligence MCP: read-only, provenance-backed Arknights data. "
        "Keep three response tiers distinct:",
        "- facts: source-backed game data; each carries region + provenance.",
        "- observations: deterministic inference; "
        "each carries rule_id, evidence, confidence, limitations.",
        "- recommendations: optional, capability-based; never mandatory or best-in-slot.",
        "Never invent missing abilities, waves, module effects, or release availability; "
        "if a field is absent, say so.",
    )
)

#: Remaining host-model guidance (PRD §13.1). A client may truncate this, so it
#: only elaborates -- nothing here is required for the core distinction above. One
#: logical paragraph, assembled from short source pieces (E501).
_DETAIL = (
    "Identify the region (en/cn) and data version when relevant, and never silently mix "
    "regions. Prefer capability recommendations (anti-air, physical burst, crowd control, "
    "lane holding) before naming specific operators. Call get_data_status when freshness is "
    "disputed, and get_data_sources when provenance, attribution, or source policy is "
    "relevant. Do not claim this server includes community consensus or wiki prose. Disclose "
    "limitations whenever a source field is unavailable or low-confidence."
)

#: §V65 grounding FLOOR path (c) + §V84/§T169: the ONE home for the common
#: blackboard-key glossary. It used to be folded into BOTH the ``get_operator`` and
#: ``compare_operator_modules`` tool descriptions, so a client paid the ~1.5KB text
#: twice every session (B89). The glossary is static host-model guidance, so it lives
#: here once (server instructions are sent a single time on ``initialize``) and each
#: emitting tool description carries only a one-line pointer to it (§V37 one home; §V84
#: no >=500-char block duplicated across descriptions). These are common interpretations
#: only -- the exact meaning of a key is set by the specific effect, hence the standing
#: per-emit limitation still rides every response that carries blackboard data (§V65 b).
#: Client-facing text, so no internal cites/jargon (§V71 b); the cites live in this
#: comment, never the emitted string.
BLACKBOARD_KEY_GLOSSARY = (
    "Common blackboard keys (interpretation depends on the specific effect): "
    "atk / atk_scale = ATK modifier or multiplier; def / def_scale = DEF modifier; "
    "max_hp = max HP modifier; magic_resistance = RES modifier; "
    "attack@atk_scale = ATK multiplier for that hit; attack@times / times = hit count; "
    "damage = flat damage; damage_scale = damage-taken multiplier; "
    "heal_scale = healing multiplier; sp / sp_cost = skill point cost; "
    "sp_recovery_per_sec = SP gained per second; duration = effect length in seconds; "
    "interval = interval in seconds; prob = trigger chance (0 to 1); stun = stun seconds; "
    "sleep = sleep seconds; freeze = freeze seconds; "
    "attack_speed = attack-speed (ASPD) modifier; "
    "base_attack_time = attack interval in seconds; move_speed = move-speed modifier; "
    "cost = deploy cost modifier; respawn_time = redeploy seconds; "
    "max_target = maximum targets hit; block_cnt = block-count modifier; "
    "range_extend = added range; charge = charge or stack state; "
    "hp_ratio = HP as a fraction; value = generic magnitude."
)

#: The full server ``instructions`` string (§I). Lead first so a truncating
#: client keeps the core contract; detail then the glossary follow (both only
#: elaborate, so a truncating client still keeps the core distinction).
SERVER_INSTRUCTIONS = f"{_LEAD}\n\n{_DETAIL}\n\n{BLACKBOARD_KEY_GLOSSARY}"


def server_instructions() -> str:
    """Return the server ``instructions`` string for the MCP ``initialize`` reply.

    Identical for both transports (§V14); deterministic + side-effect free.
    """
    return SERVER_INSTRUCTIONS


def core_segment() -> str:
    """The leading :data:`FIRST_SEGMENT_CHARS` characters (PRD §13.1).

    This is what a truncating client is guaranteed to see; it must stand alone
    with the facts/observations/recommendations distinction + never-invent rule.
    """
    return SERVER_INSTRUCTIONS[:FIRST_SEGMENT_CHARS]
