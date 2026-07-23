"""Image URL-reference derivation service (§T119; ADR 0008).

The single home (§V37) for turning an already-stored ``game_id`` into a query-time
image URL that points at the ``yuanyan3060/ArknightsGameResource`` raw-GitHub mirror.

Two invariants hold **by construction** here:

* **DERIVE, don't store (§V63).** Every function is a pure string builder over a
  ``game_id`` the database already holds (``operators.game_id`` → portrait/avatar/skin,
  ``enemies.game_id`` → enemy). No URL and no byte is ever persisted; takedown is a
  config flip with nothing to purge.
* **Never fetch (§V1/§V24).** The derived URL is an opaque emit string. This module
  performs no HEAD/GET/existence-check/validation -- not at import, not at query time.
  It imports no network library. A dead link is the client's to discover; the server
  never turns one into an error or a fallback download.

The emission gate lives in configuration, not here: :func:`arknights_mcp` config's
``AppConfig.image_refs_enabled`` is ON by default (§T124, founder 2026-07-22). As of ADR
0009 the gate carries no deployment-posture term -- "private" means access-controlled, not
loopback-only, since §V9 already fails startup closed on any anonymous non-loopback
surface, so an authenticated (OIDC/bearer) deployment may emit references (§C/D4). The
tool wiring (§T120) additionally requires the ``arknights_game_resource`` source to be
enabled in the registry (§V20 kill switch) before attaching any of these URLs. Deriving
a URL is free of side effects, so these functions are always safe to call; whether the
result is *emitted* is decided upstream.

Shape verified against the live repo tree (branch ``main``, 2026-07-22; §V63/ADR 0008):

* base   ``https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main/<folder>/<file>.png``
* portrait ``portrait/<game_id>_1.png`` (E0), ``portrait/<game_id>_2.png`` (E2)
* avatar   ``avatar/<game_id>.png`` (base), ``avatar/<game_id>_2.png`` (E2)
* skin     ``skin/<game_id>_1b.png`` (E0 full illustration), ``skin/<game_id>_2b.png`` (E2)
* enemy    ``enemy/<game_id>.png`` (base)

Base ids never contain ``#``/``+``, but skin-variant filenames can, so the derivation
percent-encodes ``#``→``%23`` and ``+``→``%2B`` **unconditionally** (§V63) -- one
encoder, applied the same way to every derived URL.

Each emitted ref also carries a ``variant`` label (§V78/B80/§T159) naming the art the
mirror's ``_1``/``_2``/``_1b``/``_2b`` suffix encodes -- ``_1``→``e0``, ``_2``→``e2``,
``_1b``/``_2b``→``skin``, no-suffix (avatar/enemy base)→``base`` -- so a client picks
E0-vs-E2 art from the typed field, not a filename-convention guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arknights_mcp.sources.registry import SourceRegistry

#: The registry ``source_id`` (§V27) for these references. Single home (§V37) for the
#: id the §T120 tool wiring stamps onto each emitted ``{category, url, source_id}`` entry
#: and checks for ``enabled`` before emitting.
SOURCE_ID = "arknights_game_resource"

#: The first-cut image categories (§V63). ``portrait``/``avatar``/``skin`` attach to an
#: operator, ``enemy`` to an enemy; a resolved banner featured-op carries ``portrait`` +
#: ``avatar`` (§V72: portrait alone lags newer ops, so the avatar rides alongside).
#: Single §T120 home for the category label stamped on each emitted ref.
CATEGORY_PORTRAIT = "portrait"
CATEGORY_AVATAR = "avatar"
CATEGORY_SKIN = "skin"
CATEGORY_ENEMY = "enemy"

#: The per-ref ``variant`` label (§V78/B80): the E0/E2/skin/base meaning of the mirror's
#: ``_1``/``_2``/``_1b``/``_2b`` filename suffix, stated on the wire where the client reads
#: it so picking E0-vs-E2 art needs no filename-convention knowledge. Single §T159 home for
#: the label stamped on each ref; ``_1``→E0, ``_2``→E2, ``_1b``/``_2b``→skin, no-suffix→base.
VARIANT_E0 = "e0"
VARIANT_E2 = "e2"
VARIANT_BASE = "base"
VARIANT_SKIN = "skin"

#: The variant sequence each ordered ``*_urls`` tuple carries, zipped onto the derived URLs
#: in :func:`_refs` (§V37 single home, no parallel-list drift -- ``zip(strict=True)`` guards
#: it): portrait = (E0, E2), avatar = (base, E2), skin = (E0-skin, E2-skin) both labelled
#: ``skin`` per §V78's ``_1b``/``_2b``→skin grouping.
_PORTRAIT_VARIANTS = (VARIANT_E0, VARIANT_E2)
_AVATAR_VARIANTS = (VARIANT_BASE, VARIANT_E2)
_SKIN_VARIANTS = (VARIANT_SKIN, VARIANT_SKIN)

#: Raw-content base for the mirror, pinned to ``main`` (§V63/ADR 0008). The folder and
#: ``<file>.png`` are appended by :func:`_png_url`. This literal has exactly ONE home in
#: the codebase (§V37); every derived URL is built from it.
_RAW_BASE = "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main"


def _encode(filename: str) -> str:
    """Percent-encode the ``#``/``+`` a skin-variant filename may carry (§V63).

    Applied unconditionally to every filename: base operator/enemy ids do not contain
    these characters, but the encoder is uniform so a skin id that does is always safe.
    Only these two characters are touched -- the ``game_id`` is otherwise URL-safe
    (``[a-z0-9_]``), so no general-purpose quoting is needed (and none is applied, which
    would wrongly escape the ``/`` path separators callers never pass in here anyway).
    """
    return filename.replace("#", "%23").replace("+", "%2B")


def _png_url(folder: str, filename: str) -> str:
    """Build one derived ``.png`` URL: ``<base>/<folder>/<encoded filename>.png`` (§V63).

    The single §V37 home for URL assembly + percent-encoding shared by every derive
    function below. Pure: it builds a string and touches no network (§V1/§V24).
    """
    return f"{_RAW_BASE}/{folder}/{_encode(filename)}.png"


def operator_portrait_urls(game_id: str) -> tuple[str, str]:
    """Derive an operator's portrait URLs (E0, then E2) from its ``game_id`` (§V63).

    ``game_id`` is a charId such as ``char_002_amiya``. Returns the E0 (``_1``) and E2
    (``_2``) portrait URLs, in that order. Pure derivation -- no network (§V1/§V24).
    """
    return (
        _png_url("portrait", f"{game_id}_1"),
        _png_url("portrait", f"{game_id}_2"),
    )


def operator_avatar_urls(game_id: str) -> tuple[str, str]:
    """Derive an operator's avatar URLs (base, then E2) from its ``game_id`` (§V63).

    Returns the base (``<game_id>``) and E2 (``<game_id>_2``) avatar URLs, in that order
    (the task's ``operator_avatar_url`` shorthand covers both the base and ``_2`` forms).
    Pure derivation -- no network (§V1/§V24).
    """
    return (
        _png_url("avatar", game_id),
        _png_url("avatar", f"{game_id}_2"),
    )


def operator_skin_urls(game_id: str) -> tuple[str, str]:
    """Derive an operator's base-skin URLs (E0, then E2) from its ``game_id`` (§V63).

    Returns the E0 full illustration (``_1b``) and E2 (``_2b``) skin URLs, in that order.
    Only the base skins are derived here; alternate/paid outfits need ``skin_table``
    (not imported) and stay deferred (§V63). Pure derivation -- no network (§V1/§V24).
    """
    return (
        _png_url("skin", f"{game_id}_1b"),
        _png_url("skin", f"{game_id}_2b"),
    )


def enemy_image_url(game_id: str) -> str:
    """Derive an enemy's sprite URL (base) from its ``game_id`` (§V63).

    ``game_id`` is an enemyId such as ``enemy_10001_trslim``. Returns the base
    (``<game_id>``) sprite URL; alternate forms (``_2`` …) are out of scope for the
    first cut. Pure derivation -- no network (§V1/§V24).
    """
    return _png_url("enemy", game_id)


@dataclass(frozen=True)
class ImageRef:
    """One derived image reference for the wire (§T120/§V63).

    A ``{category, url, variant, source_id}`` entry: ``url`` is a query-time DERIVED link
    (never stored, never fetched -- §V63); ``category`` is one of the :data:`CATEGORY_*`
    labels; ``variant`` is one of the :data:`VARIANT_*` labels naming the E0/E2/skin/base
    art the mirror filename suffix encodes (§V78/B80), stated on the wire so a client picks
    E0-vs-E2 without filename-convention knowledge; ``source_id`` is the §V27 registry
    attribution the wiring stamps on every ref.
    """

    category: str
    url: str
    variant: str
    source_id: str = SOURCE_ID


def image_ref_to_dict(ref: ImageRef) -> dict[str, object]:
    """One derived image reference for the wire (§T120/§V63): {category, url, variant, source_id}.

    The single §V37 home for the ``{category, url, variant, source_id}`` wire shape shared
    by every image-ref-bearing tool (get_operator/get_enemy/get_banners). The URL is a
    query-time DERIVED link (never stored, never fetched); ``variant`` names the E0/E2/skin/
    base art (§V78/B80); ``source_id`` is the §V27 registry attribution.
    """
    return {
        "category": ref.category,
        "url": ref.url,
        "variant": ref.variant,
        "source_id": ref.source_id,
    }


def _refs(category: str, urls: tuple[str, ...], variants: tuple[str, ...]) -> list[ImageRef]:
    """Stamp ``category`` + per-URL ``variant`` + :data:`SOURCE_ID` onto each derived URL.

    ``urls`` and ``variants`` are zipped in order (§T120/§V37/§V78); ``zip(strict=True)``
    fails closed on any length mismatch so the ordered ``*_urls`` tuple and its
    :data:`_PORTRAIT_VARIANTS`-style variant tuple can never silently drift apart.
    """
    return [
        ImageRef(category=category, url=url, variant=variant)
        for url, variant in zip(urls, variants, strict=True)
    ]


def operator_image_refs(game_id: str) -> tuple[ImageRef, ...]:
    """Derive an operator's portrait + avatar + skin refs from its ``game_id`` (§T120/§V63).

    A small, fixed set (portrait ``_1``/``_2``, avatar base/``_2``, skin ``_1b``/``_2b``)
    that attaches to the single already-fetched operator entity -- never a catalog list or
    enumeration (§V19). Pure derivation; whether it is *emitted* is decided by the wiring
    gate (:func:`refs_enabled`). No network (§V1/§V24).
    """
    refs = _refs(CATEGORY_PORTRAIT, operator_portrait_urls(game_id), _PORTRAIT_VARIANTS)
    refs += _refs(CATEGORY_AVATAR, operator_avatar_urls(game_id), _AVATAR_VARIANTS)
    refs += _refs(CATEGORY_SKIN, operator_skin_urls(game_id), _SKIN_VARIANTS)
    return tuple(refs)


def operator_banner_refs(game_id: str) -> tuple[ImageRef, ...]:
    """Derive a banner featured-op's portrait + avatar refs from its ``game_id`` (§T135/§V72/§V63).

    Attached when a banner's featured char id soft-resolved to a present operator (§V62):
    the resolved char id IS that operator's ``game_id``. Carries BOTH the portrait (E0/E2)
    AND the avatar (base/E2) categories, not portrait alone (§V72/B61): the mirror's
    portrait tree lags newer operators, so a portrait-only ref can be 100% dead while the
    avatar returns 200 one category over -- emitting the avatar alongside keeps a working
    reference. Pure derivation -- no network (§V1/§V24).
    """
    refs = _refs(CATEGORY_PORTRAIT, operator_portrait_urls(game_id), _PORTRAIT_VARIANTS)
    refs += _refs(CATEGORY_AVATAR, operator_avatar_urls(game_id), _AVATAR_VARIANTS)
    return tuple(refs)


def enemy_image_refs(game_id: str) -> tuple[ImageRef, ...]:
    """Derive an enemy's image ref (base sprite) from its ``game_id`` (§T120/§V63).

    A single ref attached to the one already-fetched enemy entity (§V19 -- no catalog).
    Pure derivation -- no network (§V1/§V24).
    """
    return (ImageRef(category=CATEGORY_ENEMY, url=enemy_image_url(game_id), variant=VARIANT_BASE),)


def refs_enabled(*, config_enabled: bool, registry: SourceRegistry) -> bool:
    """The combined §T120 emission gate -- single §V37 home (§V63/§C/§V27).

    An ``image_refs`` list is emitted ONLY when BOTH gates pass:

    * ``config_enabled`` -- the config posture
      (:attr:`~arknights_mcp.config.AppConfig.image_refs_enabled`): ON by default (§T124).
      Per ADR 0009 this is exactly ``[image_refs].enabled`` -- access-controlled, not
      loopback-only, since §V9 already fails startup closed on any anonymous non-loopback
      surface, so an authenticated deployment may emit when opted in (§C/D4);
    * the ``arknights_game_resource`` source is ``enabled`` in the machine registry (§V27)
      -- the takedown kill switch (§V20): flipping it off stops every ref with nothing to
      purge (§V63 store-nothing).

    Deriving a URL is side-effect-free, so the derive functions are always safe to call;
    this gate alone decides whether the wiring attaches the result.
    """
    if not config_enabled:
        return False
    entry = registry.get(SOURCE_ID)
    return entry is not None and entry.enabled
