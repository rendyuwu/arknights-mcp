"""T119: image URL-reference derivation service + private-only config gate.

One test per invariant the task cites (§V63/§V1/§V24/§V37/§C):

* **§V63 derive shape** -- each pure function derives the exact mirror URL from a
  ``game_id`` (portrait ``_1``/``_2``, avatar base/``_2``, skin ``_1b``/``_2b``, enemy
  base) off the pinned raw-GitHub base.
* **§V63 percent-encode** -- ``#``/``+`` are encoded to ``%23``/``%2B`` unconditionally.
* **§V1 / §V24 no network** -- the module imports no network library and derives URLs
  with a socket-open guard tripped, proving it never fetches/HEADs/validates a link.
* **§V37 single home** -- every derived URL is built from the one ``_RAW_BASE`` constant.
* **§C private-only gate** -- ``[image_refs].enabled`` is OFF by default and stays
  suppressed on a public-facing (non-loopback / behind-proxy) deployment, so a single
  flag can never expose art references publicly (D4).
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from arknights_mcp.config import AppConfig, load_config
from arknights_mcp.services import image_refs
from arknights_mcp.services.image_refs import (
    SOURCE_ID,
    enemy_image_url,
    operator_avatar_urls,
    operator_portrait_urls,
    operator_skin_urls,
)

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


def test_operator_portrait_urls_derive_e0_and_e2() -> None:
    assert operator_portrait_urls(OPERATOR_ID) == (
        f"{BASE}/portrait/{OPERATOR_ID}_1.png",
        f"{BASE}/portrait/{OPERATOR_ID}_2.png",
    )


def test_operator_avatar_urls_derive_base_and_e2() -> None:
    assert operator_avatar_urls(OPERATOR_ID) == (
        f"{BASE}/avatar/{OPERATOR_ID}.png",
        f"{BASE}/avatar/{OPERATOR_ID}_2.png",
    )


def test_operator_skin_urls_derive_e0_and_e2() -> None:
    assert operator_skin_urls(OPERATOR_ID) == (
        f"{BASE}/skin/{OPERATOR_ID}_1b.png",
        f"{BASE}/skin/{OPERATOR_ID}_2b.png",
    )


def test_enemy_image_url_derives_base() -> None:
    assert enemy_image_url(ENEMY_ID) == f"{BASE}/enemy/{ENEMY_ID}.png"


# --- §V63: unconditional percent-encode -------------------------------------------


def test_percent_encode_hash_and_plus_unconditionally() -> None:
    # Skin-variant filenames can carry ``#``/``+``; the encoder is applied uniformly
    # so any derived URL is safe. A synthetic id proves the encoding on every function.
    dirty = "char_x_epoque#1+alt"
    urls = [
        *operator_portrait_urls(dirty),
        *operator_avatar_urls(dirty),
        *operator_skin_urls(dirty),
        enemy_image_url(dirty),
    ]
    for url in urls:
        # Only the filename segment is checked for the raw characters; the base has none.
        assert "#" not in url
        assert "+" not in url
        assert "%23" in url
        assert "%2B" in url


def test_clean_ids_are_left_intact() -> None:
    # A base id has no ``#``/``+`` so encoding is a no-op -- the URL is exactly the id.
    assert enemy_image_url(ENEMY_ID) == f"{BASE}/enemy/{ENEMY_ID}.png"
    assert "%" not in enemy_image_url(ENEMY_ID)


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
    assert operator_portrait_urls(OPERATOR_ID)[0].startswith(BASE)
    assert operator_avatar_urls(OPERATOR_ID)[0].startswith(BASE)
    assert operator_skin_urls(OPERATOR_ID)[0].startswith(BASE)
    assert enemy_image_url(ENEMY_ID).startswith(BASE)


# --- §V37: single home ------------------------------------------------------------


def test_all_urls_derive_from_single_base_constant() -> None:
    # DRY (§V37): every function routes through the one _RAW_BASE constant -- there is no
    # divergent hardcoded base. Pinning the constant also fixes the §V63 base value.
    assert image_refs._RAW_BASE == BASE
    urls = [
        *operator_portrait_urls(OPERATOR_ID),
        *operator_avatar_urls(OPERATOR_ID),
        *operator_skin_urls(OPERATOR_ID),
        enemy_image_url(ENEMY_ID),
    ]
    for url in urls:
        assert url.startswith(image_refs._RAW_BASE + "/")


# --- §C: private-only config gate --------------------------------------------------


def test_image_refs_off_by_default() -> None:
    # Default AppConfig and the shipped example both leave the surface OFF (§C/§V63).
    assert AppConfig().image_refs.enabled is False
    assert AppConfig().image_refs_enabled is False
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.image_refs.enabled is False
    assert cfg.image_refs_enabled is False


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


def test_gate_suppressed_on_nonloopback_remote() -> None:
    # §C/D4: a non-loopback (public) bind is public-facing → the single flag cannot
    # expose references, even when enabled.
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
        }
    )
    assert cfg.mcp.remote.requires_auth is True
    assert cfg.image_refs_enabled is False


def test_gate_suppressed_behind_proxy() -> None:
    # §V40/§C: a loopback bind declared behind_proxy serves the public internet → gate off.
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
        }
    )
    assert cfg.mcp.remote.requires_auth is True
    assert cfg.image_refs_enabled is False


def test_active_config_never_exposes_refs() -> None:
    # The shipped active config is behind_proxy=true (public-facing) AND enabled=false,
    # so the surface is doubly suppressed -- a regression guard on the real deployment.
    cfg = load_config(ACTIVE_CONFIG)
    assert cfg.image_refs_enabled is False
