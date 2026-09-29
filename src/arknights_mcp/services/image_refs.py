"""Image URL-reference derivation service (ADR 0008).

The single home for turning an already-stored ``game_id`` into a query-time
image URL that points at the ``yuanyan3060/ArknightsGameResource`` raw-GitHub mirror.

Two invariants hold **by construction** here:

* **DERIVE, don't store.** Every function is a pure string builder over a
  ``game_id`` the database already holds (``operators.game_id`` → portrait/avatar/skin,
  ``enemies.game_id`` → enemy). No URL and no byte is ever persisted; takedown is a
  config flip with nothing to purge.
* **Never fetch.** The derived URL is an opaque emit string. This module
  performs no HEAD/GET/existence-check/validation -- not at import, not at query time.
  It imports no network library. A dead link is the client's to discover; the server
  never turns one into an error or a fallback download.

The emission gate lives in configuration, not here: :func:`arknights_mcp` config's
``AppConfig.image_refs_enabled`` is ON by default (founder 2026-07-22). As of ADR
0009 the gate carries no deployment-posture term -- "private" means access-controlled, not
loopback-only, since startup already fails closed on any anonymous non-loopback
surface, so an authenticated (OIDC/bearer) deployment may emit references (D4). The
tool wiring additionally requires the ``arknights_game_resource`` source to be
enabled in the registry (the kill switch) before attaching any of these URLs. Deriving
a URL is free of side effects, so these functions are always safe to call; whether the
result is *emitted* is decided upstream.

Shape verified against the live repo tree (branch ``main``, 2026-07-22; ADR 0008):

* base   ``https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main/<folder>/<file>.png``
* portrait ``portrait/<game_id>_1.png`` (E0), ``portrait/<game_id>_2.png`` (E2)
* avatar   ``avatar/<game_id>.png`` (base), ``avatar/<game_id>_2.png`` (E2)
* skin     ``skin/<game_id>_1b.png`` (E0 full illustration), ``skin/<game_id>_2b.png`` (E2)
* enemy    ``enemy/<game_id>.png`` (base)

On the wire the shared base is hoisted (ADR 0014): each ref carries the
RELATIVE ``path`` (``<folder>/<file>.png``) and the response emits
:data:`IMAGE_REFS_BASE_URL` once; the client joins ``base_url + "/" + path`` for the
full URL. The derivation functions therefore build paths, not absolute URLs -- the base
never repeats per ref. ADR 0019 hoists the ref family's other response-wide constant the
same way: :data:`SOURCE_ID` rides the response once as ``image_refs_source_id`` instead of
once per ref. It was a byte-identical 23-char copy on every entry -- 576 of them on one
``get_banners`` page, 21.4% of that whole result frame -- and the hoist is what lets that
page keep its references under the response cap at all. The invariance is not assumed:
:func:`image_ref_to_dict` fails closed on a ref whose ``source_id`` is not the hoisted
constant.

Base ids never contain ``%``/``#``/``+``, but skin-variant filenames can, so the
derivation percent-encodes ``%``→``%25`` (first, so encoding stays injective for
externally-imported ``portrait_id`` stems), ``#``→``%23`` and ``+``→``%2B``
**unconditionally** -- one encoder, applied the same way to every derived path.

Each emitted ref also carries a ``variant`` label naming the art the mirror's
``_1``/``_2``/``_1b``/``_2b`` suffix encodes -- ``_1``→``e0``, ``_2``→``e2``,
``_1b``/``_2b``→``skin``, no-suffix (avatar/enemy base)→``base`` -- so a client picks
E0-vs-E2 art from the typed field, not a filename-convention guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arknights_mcp.services.operators import OperatorSkinFacts
    from arknights_mcp.sources.registry import SourceRegistry

#: The registry ``source_id`` for these references. Single home for the
#: id the tool wiring checks for ``enabled`` before emitting, and for the attribution the
#: response carries ONCE as ``image_refs_source_id`` rather than on every ref (ADR 0019).
#: The hoist home is
#: :func:`~arknights_mcp.mcp.tools._shared.image_ref_hoisted_fields`; what makes it legal
#: is that this is a module constant no derivation overrides, which
#: :func:`image_ref_to_dict` enforces rather than trusts.
SOURCE_ID = "arknights_game_resource"

#: The first-cut image categories. ``portrait``/``avatar``/``skin`` attach to an
#: operator, ``enemy`` to an enemy; a resolved banner featured-op carries ``portrait`` +
#: ``avatar`` (portrait alone lags newer ops, so the avatar rides alongside).
#: Single home for the category label stamped on each emitted ref.
CATEGORY_PORTRAIT = "portrait"
CATEGORY_AVATAR = "avatar"
CATEGORY_SKIN = "skin"
CATEGORY_ENEMY = "enemy"

#: The per-ref ``variant`` label: the E0/E2/skin/base meaning of the mirror's
#: ``_1``/``_2``/``_1b``/``_2b`` filename suffix, stated on the wire where the client reads
#: it so picking E0-vs-E2 art needs no filename-convention knowledge. Single home for
#: the label stamped on each ref; ``_1``→E0, ``_2``→E2, ``_1b``/``_2b``→skin, no-suffix→base.
VARIANT_E0 = "e0"
VARIANT_E1 = "e1"
VARIANT_E2 = "e2"
VARIANT_BASE = "base"
VARIANT_SKIN = "skin"

#: A named-gallery skin ref's variant derives from the imported ``skin_group_id``:
#: the ``ILLUST_0/1/2`` groups are the operator's default E0/E1/E2 art,
#: anything else is a named outfit series and stays labelled ``skin``.
_SKIN_GROUP_VARIANTS = {"ILLUST_0": VARIANT_E0, "ILLUST_1": VARIANT_E1, "ILLUST_2": VARIANT_E2}

#: The variant sequence each ordered ``*_paths`` tuple carries, zipped onto the derived paths
#: in :func:`_refs` (single home, no parallel-list drift -- ``zip(strict=True)`` guards
#: it): portrait = (E0, E2), avatar = (base, E2), skin = (E0-skin, E2-skin) both labelled
#: ``skin`` per the ``_1b``/``_2b``→skin grouping.
_PORTRAIT_VARIANTS = (VARIANT_E0, VARIANT_E2)
_AVATAR_VARIANTS = (VARIANT_BASE, VARIANT_E2)
_SKIN_VARIANTS = (VARIANT_SKIN, VARIANT_SKIN)

#: The RESPONSE-SIDE legend for the two ref enums, hoisted once
#: beside ``image_refs_base_url`` on any response that emits refs. These meanings used to
#: live in the ``get_operator`` tool description, where they were half of the longest
#: description on the server -- long enough that a client tool-listing truncated it
#: mid-sentence, diluting the pre-call facts a caller actually needs. A legend belongs
#: with the values it decodes (the same choice the tile-grid and rendered-map legends
#: make). Keyed on the emitted ``category`` / ``variant`` labels above so the legend and
#: the stamped labels cannot drift (single home). Client-facing text, so no internal
#: cites/jargon.
IMAGE_REFS_LEGEND: dict[str, dict[str, str]] = {
    "category": {
        CATEGORY_PORTRAIT: "full operator art",
        CATEGORY_AVATAR: "square operator icon",
        CATEGORY_SKIN: "outfit art",
        CATEGORY_ENEMY: "enemy sprite",
    },
    "variant": {
        VARIANT_E0: "elite-0 art",
        VARIANT_E1: "elite-1 art",
        VARIANT_E2: "elite-2 art",
        VARIANT_BASE: "default art",
        VARIANT_SKIN: "outfit art",
    },
}

#: Raw-content base for the mirror, pinned to ``main`` (ADR 0008). Emitted ONCE per
#: ref-carrying response as ``image_refs_base_url`` (hoisted); each ref carries
#: only its relative ``path`` and the client joins ``base_url + "/" + path``. This
#: literal has exactly ONE home in the codebase; every derived path resolves
#: against it.
IMAGE_REFS_BASE_URL = "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main"


class ImageRefSourceError(ValueError):
    """A ref whose ``source_id`` is not the hoisted constant.

    The fail-closed half of the hoist. Once the attribution rides the response ONCE, a ref
    carrying some other source is attributed to :data:`SOURCE_ID` by the response-level key
    -- silently, on every row, which is the attribution error the per-ref copy could
    not make. The hoist requires the invariance be COUNTED rather than assumed and the
    shaper fail closed when it does not hold, so this raises instead of emitting a
    mis-attributed page.

    Counted on the promoted build: ONE distinct value over every emitted ref, invariant by
    construction because no derive function passes ``source_id`` at all. So this arm fires
    on no live shape and its reachability is proven synthetically instead -- the
    same call the ``map_image`` shed step got.

    Loud rather than lenient, for the reason
    :class:`~arknights_mcp.mcp.cap_pressure.CapPressureError` is: a second mirror added
    later must stop the response, not be folded into the first mirror's name.
    """


def _encode(filename: str) -> str:
    """Percent-encode the ``%``/``#``/``+`` a skin-variant filename may carry.

    Applied unconditionally to every filename: base operator/enemy ids do not contain
    these characters, but the encoder is uniform so a skin id that does is always safe.
    ``%`` is escaped FIRST so the encoding is injective (a literal ``%23`` in an
    imported ``portrait_id`` -- external data -- can never collide with an
    encoded ``#``). Only these three characters are touched -- the stems are otherwise
    URL-safe (``[a-z0-9_@]``), so no general-purpose quoting is needed (and none is
    applied, which would wrongly escape the ``/`` path separators callers never pass
    in here anyway).
    """
    return filename.replace("%", "%25").replace("#", "%23").replace("+", "%2B")


def _png_path(folder: str, filename: str) -> str:
    """Build one derived ``.png`` path: ``<folder>/<encoded filename>.png``.

    The single home for path assembly + percent-encoding shared by every derive
    function below. Relative to :data:`IMAGE_REFS_BASE_URL` (the response base hoist).
    Pure: it builds a string and touches no network.
    """
    return f"{folder}/{_encode(filename)}.png"


def operator_portrait_paths(game_id: str) -> tuple[str, str]:
    """Derive an operator's portrait paths (E0, then E2) from its ``game_id``.

    ``game_id`` is a charId such as ``char_002_amiya``. Returns the E0 (``_1``) and E2
    (``_2``) portrait paths, in that order. Pure derivation -- no network.
    """
    return (
        _png_path("portrait", f"{game_id}_1"),
        _png_path("portrait", f"{game_id}_2"),
    )


def operator_avatar_paths(game_id: str) -> tuple[str, str]:
    """Derive an operator's avatar paths (base, then E2) from its ``game_id``.

    Returns the base (``<game_id>``) and E2 (``<game_id>_2``) avatar paths, in that
    order. Pure derivation -- no network.
    """
    return (
        _png_path("avatar", game_id),
        _png_path("avatar", f"{game_id}_2"),
    )


def operator_skin_paths(game_id: str) -> tuple[str, str]:
    """Derive an operator's base-skin paths (E0, then E2) from its ``game_id``.

    Returns the E0 full illustration (``_1b``) and E2 (``_2b``) skin paths, in that
    order. Only the base skins are derived here; the NAMED gallery replaces this
    fallback on builds carrying the imported skin domain. Pure derivation
    -- no network.
    """
    return (
        _png_path("skin", f"{game_id}_1b"),
        _png_path("skin", f"{game_id}_2b"),
    )


def skin_image_path(portrait_id: str) -> str:
    """Derive one named-gallery skin path from an imported ``portrait_id``.

    The mirror stores every skin illustration -- default E0/E1/E2 art, paid/event
    outfits, and alt-form (Amiya-family) art alike -- as ``skin/<portraitId>b.png``
    (rule verified against the live mirror 2026-07-26, ADR 0015). ``portrait_id``
    comes from the imported ``operator_skins`` row; the path itself stays query-time
    DERIVED, never stored. ``#``/``+`` in outfit stems (``…_epoque#4``,
    ``…_1+``) ride the shared unconditional encoder. Pure derivation -- no network.
    """
    return _png_path("skin", f"{portrait_id}b")


def named_skin_ref_to_dict(
    *,
    skin_id: str,
    portrait_id: str,
    skin_name: str | None = None,
    skin_group_id: str | None = None,
    skin_group_name: str | None = None,
    alt_form: bool = False,
    paid: bool = False,
) -> dict[str, object]:
    """One named-gallery skin ref for the wire: the single home.

    Extends the ``{category, path, variant}`` shape with additive fields:
    ``skin_id`` always; ``skin_name``/``skin_group`` only when imported
    (absent = default art with no outfit name, omit-discipline); ``alt_form``/
    ``paid`` only when true (an absent flag is the default, never ``null``).
    ``variant`` derives from the imported ``skin_group_id``: default ``ILLUST_0/1/2``
    art maps to ``e0``/``e1``/``e2``, a named outfit series stays ``skin``.
    On an ``alt_form`` ref that variant names the ALTERNATE form's elite art, not the
    base operator's -- clients must read the flag beside the variant.
    ``path`` is relative to the response's hoisted ``image_refs_base_url``.
    The base three keys route through :class:`ImageRef` + :func:`image_ref_to_dict` so
    the shared wire shape keeps exactly one constructor -- which is also why the
    ``source_id`` hoist reached this surface without a second edit; only the additive
    named-gallery fields are assembled here.
    """
    ref = image_ref_to_dict(
        ImageRef(
            category=CATEGORY_SKIN,
            path=skin_image_path(portrait_id),
            variant=_SKIN_GROUP_VARIANTS.get(skin_group_id or "", VARIANT_SKIN),
        )
    )
    ref["skin_id"] = skin_id
    if skin_name:
        ref["skin_name"] = skin_name
    if skin_group_name:
        ref["skin_group"] = skin_group_name
    if alt_form:
        ref["alt_form"] = True
    if paid:
        ref["paid"] = True
    return ref


def operator_ref_dicts(game_id: str, skins: Sequence[OperatorSkinFacts]) -> list[dict[str, object]]:
    """An operator's full ``image_refs`` list: NAMED gallery, else the derived fallback.

    The single home for the named-vs-fallback choice, which had been living in the tool
    layer: with imported ``operator_skins`` rows the
    complete NAMED gallery replaces the derived base-outfit pair (identity refs + one
    :func:`named_skin_ref_to_dict` per row -- named, alt-form-labeled); without them
    (a pre-0014 build, or a combat-only snapshot) the operator keeps the derived
    ``_1b``/``_2b`` fallback of :func:`operator_image_refs` plus the partial-gallery
    limitation the wiring attaches (stale-active-db degrade).

    ``is_buy_skin`` is tri-state: only an explicit source ``True`` emits the
    ``paid`` flag -- ``None`` (the source did not state it) stays absent exactly like
    ``False``, so an unstated field is never emitted as a fabricated not-paid claim.

    Pure derivation over already-stored ids -- no URL stored, no fetch.
    Whether the result is emitted at all is the wiring's gate (:func:`refs_enabled`).
    """
    if not skins:
        return [image_ref_to_dict(ref) for ref in operator_image_refs(game_id)]
    refs = [image_ref_to_dict(ref) for ref in operator_identity_refs(game_id)]
    refs += [
        named_skin_ref_to_dict(
            skin_id=skin.skin_id,
            portrait_id=skin.portrait_id,
            skin_name=skin.display_name,
            skin_group_id=skin.skin_group_id,
            skin_group_name=skin.skin_group_name,
            alt_form=skin.is_alt_form,
            paid=skin.is_buy_skin is True,
        )
        for skin in skins
    ]
    return refs


def enemy_image_path(game_id: str) -> str:
    """Derive an enemy's sprite path (base) from its ``game_id``.

    ``game_id`` is an enemyId such as ``enemy_10001_trslim``. Returns the base
    (``<game_id>``) sprite path; alternate forms (``_2`` …) are out of scope for the
    first cut. Pure derivation -- no network.
    """
    return _png_path("enemy", game_id)


@dataclass(frozen=True)
class ImageRef:
    """One derived image reference for the wire.

    A ``{category, path, variant}`` entry: ``path`` is a query-time DERIVED
    link, relative to the response's hoisted ``image_refs_base_url`` (never
    stored, never fetched); ``category`` is one of the :data:`CATEGORY_*` labels;
    ``variant`` is one of the :data:`VARIANT_*` labels naming the E0/E2/skin/base art the
    mirror filename suffix encodes, stated on the wire so a client picks
    E0-vs-E2 without filename-convention knowledge.

    ``source_id`` is the registry attribution, and it does NOT reach the
    wire per ref: the response carries it once as ``image_refs_source_id`` (ADR 0019).
    The field stays on this dataclass because it is what
    :func:`image_ref_to_dict` checks the hoist against -- dropping it would leave the
    hoisted key claiming an attribution nothing can contradict, which the hoist forbids.
    """

    category: str
    path: str
    variant: str
    source_id: str = SOURCE_ID


def image_ref_to_dict(ref: ImageRef) -> dict[str, object]:
    """One derived image ref for the wire: ``{category, path, variant}``.

    The single home for the ``{category, path, variant}`` wire shape shared by every
    image-ref-bearing tool (get_operator/get_enemy/get_banners). ``path`` is a query-time
    DERIVED link relative to the hoisted ``image_refs_base_url`` (never stored,
    never fetched); ``variant`` names the E0/E2/skin/base art.

    ``source_id`` is NOT emitted here (ADR 0019). It was byte-identical on
    every ref of every response -- 576 copies of one 23-char constant on a single
    ``get_banners`` page -- while the response already hoisted the *varying* half of the
    same family (``image_refs_base_url``). The attribution now rides the response once,
    beside that base, from
    :func:`~arknights_mcp.mcp.tools._shared.image_ref_hoisted_fields`.

    Because the hoisted key speaks for every ref in the response, a ref disagreeing with
    it would be mis-attributed silently on every row. So the invariance is enforced at this
    one gate rather than assumed: a ``source_id`` that is not :data:`SOURCE_ID` raises
    :class:`ImageRefSourceError` (fail-closed). The check is on the VALUE, not
    on "two distinct values in one response" -- a response uniformly carrying some other
    mirror is equally mis-attributed by the hoisted constant, and equally has to stop.
    """
    if ref.source_id != SOURCE_ID:
        raise ImageRefSourceError(
            f"image ref {ref.path!r} carries source_id {ref.source_id!r}, but the response "
            f"hoists {SOURCE_ID!r} for every ref. A second mirror needs a "
            "per-ref attribution again, or a grouped reshape -- it cannot ride this hoist"
        )
    return {
        "category": ref.category,
        "path": ref.path,
        "variant": ref.variant,
    }


def _refs(category: str, paths: tuple[str, ...], variants: tuple[str, ...]) -> list[ImageRef]:
    """Stamp ``category`` + per-path ``variant`` onto each derived path.

    ``paths`` and ``variants`` are zipped in order; ``zip(strict=True)``
    fails closed on any length mismatch so the ordered ``*_paths`` tuple and its
    :data:`_PORTRAIT_VARIANTS`-style variant tuple can never silently drift apart.

    ``source_id`` is deliberately NOT passed: it takes :class:`ImageRef`'s
    :data:`SOURCE_ID` default at every construction site in this module, which is what
    makes the response-level hoist invariant BY CONSTRUCTION rather than by
    coincidence of today's corpus.
    """
    return [
        ImageRef(category=category, path=path, variant=variant)
        for path, variant in zip(paths, variants, strict=True)
    ]


def operator_identity_refs(game_id: str) -> tuple[ImageRef, ...]:
    """Derive an operator's portrait + avatar refs from its ``game_id``.

    The shared home for the two identity categories every operator-ref surface
    carries (portrait ``_1``/``_2``, avatar base/``_2``);
    :func:`operator_image_refs` and :func:`operator_banner_refs` both build on it.
    Pure derivation -- no network.
    """
    refs = _refs(CATEGORY_PORTRAIT, operator_portrait_paths(game_id), _PORTRAIT_VARIANTS)
    refs += _refs(CATEGORY_AVATAR, operator_avatar_paths(game_id), _AVATAR_VARIANTS)
    return tuple(refs)


def operator_image_refs(game_id: str) -> tuple[ImageRef, ...]:
    """Derive an operator's portrait + avatar + FALLBACK skin refs.

    A small, fixed set (portrait ``_1``/``_2``, avatar base/``_2``, skin ``_1b``/``_2b``)
    that attaches to the single already-fetched operator entity -- never a catalog list or
    enumeration. The two skin refs here are the derived BASE-outfit fallback: on a
    build carrying the imported skin domain the wiring emits the NAMED gallery instead
    (:func:`operator_identity_refs` + one :func:`named_skin_ref_to_dict` per row).
    Pure derivation; whether it is *emitted* is decided by the wiring gate
    (:func:`refs_enabled`). No network.
    """
    refs = list(operator_identity_refs(game_id))
    refs += _refs(CATEGORY_SKIN, operator_skin_paths(game_id), _SKIN_VARIANTS)
    return tuple(refs)


def operator_banner_refs(game_id: str) -> tuple[ImageRef, ...]:
    """Derive a banner featured-op's portrait + avatar refs from its ``game_id``.

    Attached when a banner's featured char id soft-resolved to a present operator:
    the resolved char id IS that operator's ``game_id``. Carries BOTH the portrait (E0/E2)
    AND the avatar (base/E2) categories, not portrait alone: the mirror's
    portrait tree lags newer operators, so a portrait-only ref can be 100% dead while the
    avatar returns 200 one category over -- emitting the avatar alongside keeps a working
    reference. Pure derivation -- no network.
    """
    return operator_identity_refs(game_id)


def enemy_image_refs(game_id: str) -> tuple[ImageRef, ...]:
    """Derive an enemy's image ref (base sprite) from its ``game_id``.

    A single ref attached to the one already-fetched enemy entity (no catalog).
    Pure derivation -- no network.
    """
    return (
        ImageRef(category=CATEGORY_ENEMY, path=enemy_image_path(game_id), variant=VARIANT_BASE),
    )


def refs_enabled(*, config_enabled: bool, registry: SourceRegistry) -> bool:
    """The combined emission gate -- single home.

    An ``image_refs`` list is emitted ONLY when BOTH gates pass:

    * ``config_enabled`` -- the config posture
      (:attr:`~arknights_mcp.config.AppConfig.image_refs_enabled`): ON by default.
      Per ADR 0009 this is exactly ``[image_refs].enabled`` -- access-controlled, not
      loopback-only, since startup already fails closed on any anonymous non-loopback
      surface, so an authenticated deployment may emit when opted in (D4);
    * the ``arknights_game_resource`` source is ``enabled`` in the machine registry
      -- the takedown kill switch: flipping it off stops every ref with nothing to
      purge (store-nothing).

    Deriving a URL is side-effect-free, so the derive functions are always safe to call;
    this gate alone decides whether the wiring attaches the result.
    """
    if not config_enabled:
        return False
    entry = registry.get(SOURCE_ID)
    return entry is not None and entry.enabled
