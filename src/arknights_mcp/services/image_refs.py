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
``AppConfig.image_refs_enabled`` is OFF by default and private-only (§C/D4), and the
tool wiring (§T120) additionally requires the ``arknights_game_resource`` source to be
enabled in the registry before attaching any of these URLs. Deriving a URL is free of
side effects, so these functions are always safe to call; whether the result is
*emitted* is decided upstream.

Shape verified against the live repo tree (branch ``main``, 2026-07-22; §V63/ADR 0008):

* base   ``https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main/<folder>/<file>.png``
* portrait ``portrait/<game_id>_1.png`` (E0), ``portrait/<game_id>_2.png`` (E2)
* avatar   ``avatar/<game_id>.png`` (base), ``avatar/<game_id>_2.png`` (E2)
* skin     ``skin/<game_id>_1b.png`` (E0 full illustration), ``skin/<game_id>_2b.png`` (E2)
* enemy    ``enemy/<game_id>.png`` (base)

Base ids never contain ``#``/``+``, but skin-variant filenames can, so the derivation
percent-encodes ``#``→``%23`` and ``+``→``%2B`` **unconditionally** (§V63) -- one
encoder, applied the same way to every derived URL.
"""

from __future__ import annotations

#: The registry ``source_id`` (§V27) for these references. Single home (§V37) for the
#: id the §T120 tool wiring stamps onto each emitted ``{category, url, source_id}`` entry
#: and checks for ``enabled`` before emitting.
SOURCE_ID = "arknights_game_resource"

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
