"""T119: image URL-reference derivation service + private-only config gate.

One test per invariant the task cites (§V63/§V1/§V24/§V37/§C):

* **§V63 derive shape** -- each pure function derives the exact mirror path from a
  ``game_id`` (portrait ``_1``/``_2``, avatar base/``_2``, skin ``_1b``/``_2b``, enemy
  base). §T183/§V66: the derivation builds RELATIVE paths; the pinned raw-GitHub base
  (:data:`IMAGE_REFS_BASE_URL`) is hoisted once per response by the tool shapers, never
  repeated per ref.
* **§V63 percent-encode** -- ``#``/``+`` are encoded to ``%23``/``%2B`` unconditionally.
* **§V1 / §V24 no network** -- the module imports no network library and derives paths
  with a socket-open guard tripped, proving it never fetches/HEADs/validates a link.
* **§V37 single home** -- the base constant has one home and every derived path is
  relative (scheme-free), so the base can never fork per ref.
* **§V63 access-controlled gate (ADR 0009 / §T124)** -- ``[image_refs].enabled`` is ON by
  default and carries NO deployment-posture term: it emits on any *startable* posture (loopback dev
  OR an authenticated non-loopback / behind-proxy remote), because §V9 already fails startup
  closed on any anonymous non-loopback surface. "Private" means access-controlled, not
  loopback-only (D4 refined).
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from arknights_mcp.config import AppConfig, ImageRefsConfig, load_config
from arknights_mcp.mcp.tools import operator as operator_tool
from arknights_mcp.services import image_refs
from arknights_mcp.services.image_refs import (
    IMAGE_REFS_BASE_URL,
    SOURCE_ID,
    enemy_image_path,
    enemy_image_refs,
    image_ref_to_dict,
    named_skin_ref_to_dict,
    operator_avatar_paths,
    operator_banner_refs,
    operator_identity_refs,
    operator_image_refs,
    operator_portrait_paths,
    operator_ref_dicts,
    operator_skin_paths,
    skin_image_path,
)
from arknights_mcp.services.operators import OperatorSkinFacts

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.toml"
ACTIVE_CONFIG = REPO_ROOT / "config.toml"

BASE = "https://raw.githubusercontent.com/yuanyan3060/ArknightsGameResource/main"
OPERATOR_ID = "char_002_amiya"
ENEMY_ID = "enemy_10001_trslim"


# --- §V63: derive shape ------------------------------------------------------------


def test_source_id_matches_registry() -> None:
    # The service's SOURCE_ID is the single home for the §V27 registry id the §T120
    # wiring stamps + gates on (registered by T118).
    assert SOURCE_ID == "arknights_game_resource"


def test_operator_portrait_paths_derive_e0_and_e2() -> None:
    assert operator_portrait_paths(OPERATOR_ID) == (
        f"portrait/{OPERATOR_ID}_1.png",
        f"portrait/{OPERATOR_ID}_2.png",
    )


def test_operator_avatar_paths_derive_base_and_e2() -> None:
    assert operator_avatar_paths(OPERATOR_ID) == (
        f"avatar/{OPERATOR_ID}.png",
        f"avatar/{OPERATOR_ID}_2.png",
    )


def test_operator_skin_paths_derive_e0_and_e2() -> None:
    assert operator_skin_paths(OPERATOR_ID) == (
        f"skin/{OPERATOR_ID}_1b.png",
        f"skin/{OPERATOR_ID}_2b.png",
    )


def test_enemy_image_path_derives_base() -> None:
    assert enemy_image_path(ENEMY_ID) == f"enemy/{ENEMY_ID}.png"


def test_operator_banner_refs_derive_portrait_and_avatar() -> None:
    # §V72 (§T135, B61): a banner featured-op ref carries portrait (E0/E2) + avatar
    # (base/E2), each stamped with the source_id -- the avatar rides ALONGSIDE the
    # portrait so the mirror's lagging portrait tree never leaves a portrait-only
    # (possibly 100%-dead) ref while a working avatar exists one category over.
    refs = operator_banner_refs(OPERATOR_ID)
    assert [(r.category, r.path) for r in refs] == [
        ("portrait", f"portrait/{OPERATOR_ID}_1.png"),
        ("portrait", f"portrait/{OPERATOR_ID}_2.png"),
        ("avatar", f"avatar/{OPERATOR_ID}.png"),
        ("avatar", f"avatar/{OPERATOR_ID}_2.png"),
    ]
    assert all(r.source_id == SOURCE_ID for r in refs)
    # §V78/B80: portrait = E0/E2, avatar = base/E2.
    assert [r.variant for r in refs] == ["e0", "e2", "base", "e2"]


# --- §V78 (B80): each ref carries a variant label the client reads directly --------


def test_v78_operator_refs_carry_variant_labels() -> None:
    # §V78/B80/§T159: the E0/E2/skin/base meaning of the mirror's _1/_2/_1b/_2b filename
    # suffix is stated on the wire per ref so the client never guesses from the filename.
    refs = operator_image_refs(OPERATOR_ID)
    assert [(r.category, r.variant) for r in refs] == [
        ("portrait", "e0"),
        ("portrait", "e2"),
        ("avatar", "base"),
        ("avatar", "e2"),
        ("skin", "skin"),
        ("skin", "skin"),
    ]


def test_v78_enemy_ref_variant_is_base() -> None:
    # §V78: the enemy sprite carries no elite suffix -> variant base.
    (ref,) = enemy_image_refs(ENEMY_ID)
    assert ref.variant == "base"


def test_v78_wire_dict_carries_variant() -> None:
    # §V21/§V78: the {category, path, variant, source_id} wire shape includes variant.
    (ref,) = enemy_image_refs(ENEMY_ID)
    assert image_ref_to_dict(ref) == {
        "category": "enemy",
        "path": f"enemy/{ENEMY_ID}.png",
        "variant": "base",
        "source_id": SOURCE_ID,
    }


# --- §V63: unconditional percent-encode -------------------------------------------


def test_percent_encode_hash_and_plus_unconditionally() -> None:
    # Skin-variant filenames can carry ``#``/``+``; the encoder is applied uniformly
    # so any derived path is safe. A synthetic id proves the encoding on every function.
    dirty = "char_x_epoque#1+alt"
    paths = [
        *operator_portrait_paths(dirty),
        *operator_avatar_paths(dirty),
        *operator_skin_paths(dirty),
        enemy_image_path(dirty),
    ]
    for path in paths:
        assert "#" not in path
        assert "+" not in path
        assert "%23" in path
        assert "%2B" in path


def test_clean_ids_are_left_intact() -> None:
    # A base id has no ``#``/``+`` so encoding is a no-op -- the path is exactly the id.
    assert enemy_image_path(ENEMY_ID) == f"enemy/{ENEMY_ID}.png"
    assert "%" not in enemy_image_path(ENEMY_ID)


def test_percent_itself_is_escaped_first_so_encoding_is_injective() -> None:
    # ``portrait_id`` is imported external data (§T182): a stem carrying a literal
    # ``%`` must not collide with an encoded ``#`` -- ``%`` is escaped FIRST.
    assert skin_image_path("char_x_50%off") == "skin/char_x_50%25offb.png"
    assert skin_image_path("char_x_%23") != skin_image_path("char_x_#")
    assert skin_image_path("char_x_%23") == "skin/char_x_%2523b.png"


# --- §T182/§V88: named skin gallery derivation -------------------------------------


def test_skin_image_path_derives_from_portrait_id() -> None:
    # ADR 0015 verified mirror rule: skin/<portraitId>b.png, shared encoder applied
    # (# -> %23, + -> %2B covers outfit and E1 stems alike).
    assert skin_image_path("char_002_amiya_1") == "skin/char_002_amiya_1b.png"
    assert skin_image_path("char_002_amiya_epoque#4") == "skin/char_002_amiya_epoque%234b.png"
    assert skin_image_path("char_002_amiya_1+") == "skin/char_002_amiya_1%2Bb.png"


def test_named_skin_ref_variant_maps_illust_groups() -> None:
    # §V78: default ILLUST_0/1/2 art -> e0/e1/e2; a named outfit series stays "skin".
    for group, variant in (("ILLUST_0", "e0"), ("ILLUST_1", "e1"), ("ILLUST_2", "e2")):
        ref = named_skin_ref_to_dict(skin_id="s", portrait_id="p", skin_group_id=group)
        assert ref["variant"] == variant
    outfit = named_skin_ref_to_dict(skin_id="s", portrait_id="p", skin_group_id="2020#sale")
    assert outfit["variant"] == "skin"
    unknown = named_skin_ref_to_dict(skin_id="s", portrait_id="p", skin_group_id=None)
    assert unknown["variant"] == "skin"


def test_named_skin_ref_omit_discipline() -> None:
    # §V67: skin_name/skin_group only when imported; alt_form/paid only when TRUE --
    # an absent key is the default, never a null the client must decode.
    bare = named_skin_ref_to_dict(skin_id="char_002_amiya#1", portrait_id="char_002_amiya_1")
    assert bare["category"] == "skin"
    assert bare["skin_id"] == "char_002_amiya#1"
    assert bare["source_id"] == SOURCE_ID
    for absent in ("skin_name", "skin_group", "alt_form", "paid"):
        assert absent not in bare

    full = named_skin_ref_to_dict(
        skin_id="char_1001_amiya2@sale#16",
        portrait_id="char_1001_amiya2_sale#16",
        skin_name="Name",
        skin_group_id="2020#sale",
        skin_group_name="Series",
        alt_form=True,
        paid=True,
    )
    assert full["skin_name"] == "Name"
    assert full["skin_group"] == "Series"
    assert full["alt_form"] is True
    assert full["paid"] is True
    assert full["path"] == "skin/char_1001_amiya2_sale%2316b.png"


def test_identity_refs_are_portrait_plus_avatar_only() -> None:
    # §V37 shared home: identity refs = portrait E0/E2 + avatar base/E2 (no skin);
    # operator_image_refs = identity + the derived base-skin FALLBACK pair;
    # operator_banner_refs = exactly the identity refs.
    identity = operator_identity_refs(OPERATOR_ID)
    assert [r.category for r in identity] == ["portrait", "portrait", "avatar", "avatar"]
    assert operator_banner_refs(OPERATOR_ID) == identity
    assert operator_image_refs(OPERATOR_ID)[: len(identity)] == identity
    assert [r.category for r in operator_image_refs(OPERATOR_ID)[len(identity) :]] == [
        "skin",
        "skin",
    ]


# --- §V37: the named-vs-fallback branch has ONE home (T189/B125) --------------------


def _skin(
    skin_id: str,
    portrait_id: str,
    *,
    display_name: str | None = None,
    skin_group_id: str | None = None,
    skin_group_name: str | None = None,
    is_buy_skin: bool | None = None,
    is_alt_form: bool = False,
) -> OperatorSkinFacts:
    return OperatorSkinFacts(
        skin_id=skin_id,
        portrait_id=portrait_id,
        display_name=display_name,
        skin_group_id=skin_group_id,
        skin_group_name=skin_group_name,
        is_buy_skin=is_buy_skin,
        is_alt_form=is_alt_form,
    )


def test_operator_ref_dicts_falls_back_without_skin_rows() -> None:
    # §V88/§V21: a build with no imported skin domain keeps the derived _1b/_2b pair.
    refs = operator_ref_dicts(OPERATOR_ID, [])
    assert refs == [image_ref_to_dict(r) for r in operator_image_refs(OPERATOR_ID)]
    assert [r["category"] for r in refs][-2:] == ["skin", "skin"]


def test_operator_ref_dicts_named_gallery_replaces_fallback() -> None:
    # §T182/§V88: with imported rows the NAMED gallery replaces the derived pair --
    # identity refs (portrait+avatar) then one ref per skin row, never both galleries.
    skins = [
        _skin("char_002_amiya#1", "char_002_amiya_1", skin_group_id="ILLUST_0"),
        _skin(
            "char_002_amiya@epoque#4",
            "char_002_amiya_epoque#4",
            display_name="Epoque",
            skin_group_name="Epoque series",
            is_buy_skin=True,
        ),
    ]
    refs = operator_ref_dicts(OPERATOR_ID, skins)
    identity = [image_ref_to_dict(r) for r in operator_identity_refs(OPERATOR_ID)]
    assert refs[: len(identity)] == identity
    gallery = refs[len(identity) :]
    assert [r["skin_id"] for r in gallery] == [s.skin_id for s in skins]
    assert gallery[1]["path"] == "skin/char_002_amiya_epoque%234b.png"
    # the derived fallback pair is GONE (no unnamed skin ref rides along).
    assert all("skin_id" in r for r in gallery)
    assert len(refs) == len(identity) + len(skins)


def test_operator_ref_dicts_paid_is_tri_state() -> None:
    # §V67: only an explicit source True emits `paid`; None (not stated) stays absent
    # exactly like False -- never a fabricated not-paid claim.
    unstated = operator_ref_dicts(OPERATOR_ID, [_skin("s", "p", is_buy_skin=None)])[-1]
    explicit_false = operator_ref_dicts(OPERATOR_ID, [_skin("s", "p", is_buy_skin=False)])[-1]
    explicit_true = operator_ref_dicts(OPERATOR_ID, [_skin("s", "p", is_buy_skin=True)])[-1]
    assert "paid" not in unstated
    assert "paid" not in explicit_false
    assert explicit_true["paid"] is True


def test_named_vs_fallback_branch_has_single_home() -> None:
    # §V37/B125: the choice lives in the service; the tool layer decides only WHETHER
    # to attach refs (the config+registry gate), never WHICH gallery to build.
    tool_src = Path(operator_tool.__file__).read_text(encoding="utf-8")
    assert "operator_ref_dicts" in tool_src
    for gone in ("named_skin_ref_to_dict", "operator_identity_refs", "operator_image_refs"):
        assert gone not in tool_src, gone
    assert "if operator.skins" not in tool_src


# --- §V1 / §V24: no network -------------------------------------------------------


def test_module_imports_no_network_library() -> None:
    # Static guard: the module must not import any network/socket/async library, so it
    # cannot fetch/HEAD/validate a derived link (§V1/§V24). Parsing the AST is robust to
    # the docstring mentioning "fetch"/"network" in prose.
    source = Path(image_refs.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "socket",
        "ssl",
        "urllib",
        "http",
        "httpx",
        "requests",
        "aiohttp",
        "asyncio",
        "ftplib",
    }
    leak = imported & forbidden
    assert not leak, f"image_refs must not import network libs: {leak}"


def test_derivation_opens_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    # Behavioral guard: with socket creation booby-trapped, deriving every category still
    # succeeds -- proving the derivation is pure string-building, never a fetch (§V1/§V24).
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("image_refs derivation must not open a socket (§V1/§V24)")

    monkeypatch.setattr(socket, "socket", _boom)
    assert operator_portrait_paths(OPERATOR_ID)[0].startswith("portrait/")
    assert operator_avatar_paths(OPERATOR_ID)[0].startswith("avatar/")
    assert operator_skin_paths(OPERATOR_ID)[0].startswith("skin/")
    assert enemy_image_path(ENEMY_ID).startswith("enemy/")
    # §T135: the banner-ref builder (portrait+avatar) is pure string derivation too --
    # §V63 never-fetch is UNCHANGED, so it derives with the socket booby-trap tripped.
    assert operator_banner_refs(OPERATOR_ID)[0].path.startswith("portrait/")


# --- §V37 / §T183: single base home, paths relative --------------------------------


def test_base_constant_pinned_and_paths_are_relative() -> None:
    # DRY (§V37): the base literal has ONE home -- the pinned public constant the tool
    # shapers hoist onto the wire (§T183/§V66). Every derived path is RELATIVE
    # (scheme-free, no leading slash), so a ref can never re-embed a divergent base, and
    # joining base + "/" + path reconstructs the §V63-verified absolute URL.
    assert IMAGE_REFS_BASE_URL == BASE
    paths = [
        *operator_portrait_paths(OPERATOR_ID),
        *operator_avatar_paths(OPERATOR_ID),
        *operator_skin_paths(OPERATOR_ID),
        enemy_image_path(ENEMY_ID),
    ]
    for path in paths:
        assert "://" not in path
        assert not path.startswith("/")
    assert (
        f"{IMAGE_REFS_BASE_URL}/{operator_portrait_paths(OPERATOR_ID)[0]}"
        == f"{BASE}/portrait/{OPERATOR_ID}_1.png"
    )


# --- §V63: access-controlled config gate (ADR 0009) -------------------------------

#: A valid, non-placeholder OIDC block so an auth-requiring (``requires_auth``) remote is
#: startup-safe (§V9): behind_proxy / non-loopback binds below pair it with an https
#: ``public_base_url`` so ``assert_remote_startup_safe`` does not raise.
_AUTH_OIDC = {
    "mode": "oidc",
    "issuer": "https://issuer.example.com/",
    "audience": "https://mcp.example.com/mcp",
    "jwks_url": "https://issuer.example.com/.well-known/jwks.json",
    "required_scopes": ["arknights:read"],
}


def test_image_refs_on_by_default() -> None:
    # §T124 (founder 2026-07-22): the surface is ON by default -- both the default AppConfig
    # and the shipped example config leave the config half of the gate enabled (§C/§V63).
    assert AppConfig().image_refs.enabled is True
    assert AppConfig().image_refs_enabled is True
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.image_refs.enabled is True
    assert cfg.image_refs_enabled is True


def test_gate_active_on_local_deployment() -> None:
    # Local (no remote) → not public-facing → the flag takes effect.
    cfg = AppConfig.model_validate({"image_refs": {"enabled": True}})
    assert cfg.mcp.remote.requires_auth is False
    assert cfg.image_refs_enabled is True


def test_gate_active_on_loopback_dev_remote() -> None:
    # A genuine loopback dev remote (not behind a proxy) is private → the flag takes effect.
    cfg = AppConfig.model_validate(
        {
            "image_refs": {"enabled": True},
            "mcp": {"remote": {"enabled": True, "bind_host": "127.0.0.1"}},
        }
    )
    assert cfg.mcp.remote.requires_auth is False
    assert cfg.image_refs_enabled is True


def test_gate_emits_on_authenticated_nonloopback() -> None:
    # §V63/ADR 0009: a non-loopback bind under valid OIDC is an AUTHENTICATED, startable
    # surface -- the gate no longer carries a posture term, so the flag takes effect.
    cfg = AppConfig.model_validate(
        {
            "image_refs": {"enabled": True},
            "mcp": {
                "remote": {
                    "enabled": True,
                    "bind_host": "0.0.0.0",
                    "public_base_url": "https://mcp.example.com",
                }
            },
            "auth": _AUTH_OIDC,
        }
    )
    assert cfg.mcp.remote.requires_auth is True
    cfg.assert_remote_startup_safe()  # §V9: startable (HTTPS + valid OIDC), does not raise
    assert cfg.image_refs_enabled is True


def test_gate_emits_behind_proxy_authenticated() -> None:
    # §V63/ADR 0009: the shipped Cloudflare-tunnel posture (loopback bind, behind_proxy,
    # Auth0 OIDC) is authenticated ∴ access-controlled ∴ the flag emits when enabled.
    cfg = AppConfig.model_validate(
        {
            "image_refs": {"enabled": True},
            "mcp": {
                "remote": {
                    "enabled": True,
                    "bind_host": "127.0.0.1",
                    "behind_proxy": True,
                    "public_base_url": "https://mcp.example.com",
                }
            },
            "auth": _AUTH_OIDC,
        }
    )
    assert cfg.mcp.remote.requires_auth is True
    cfg.assert_remote_startup_safe()  # §V9: startable, does not raise
    assert cfg.image_refs_enabled is True


def test_gate_off_when_flag_false_even_authenticated() -> None:
    # §V63: the flag alone is the config gate now -- enabled=false suppresses regardless of
    # an authenticated behind_proxy posture (accept: [image_refs].enabled=false → absent).
    cfg = AppConfig.model_validate(
        {
            "image_refs": {"enabled": False},
            "mcp": {
                "remote": {
                    "enabled": True,
                    "bind_host": "127.0.0.1",
                    "behind_proxy": True,
                    "public_base_url": "https://mcp.example.com",
                }
            },
            "auth": _AUTH_OIDC,
        }
    )
    assert cfg.mcp.remote.requires_auth is True
    assert cfg.image_refs_enabled is False


def test_active_config_authenticated_emits_by_default() -> None:
    # The shipped active config is behind_proxy=true + Auth0 OIDC (authenticated). §T124
    # (founder 2026-07-22) flipped image_refs ON by default and config.toml sets no
    # [image_refs] override, so the shipped config emits references on this authenticated
    # deployment with no further opt-in -- the ADR 0009 accept case on the real config.
    # Setting [image_refs].enabled=false is the §V20 kill switch.
    cfg = load_config(ACTIVE_CONFIG)
    assert cfg.mcp.remote.requires_auth is True  # behind_proxy Cloudflare-tunnel posture
    assert cfg.image_refs_enabled is True  # shipped ON by default (§T124)
    disabled = cfg.model_copy(update={"image_refs": ImageRefsConfig(enabled=False)})
    assert disabled.image_refs_enabled is False  # §V20 kill switch: flag off suppresses
