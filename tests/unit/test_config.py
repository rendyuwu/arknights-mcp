"""T8: config loading + the §V9 startup safety rule.

Verifies the example config loads with the PRD Section 19 defaults, per-source
sync subtables fold correctly, and a non-loopback remote deployment without
HTTPS + valid OAuth/OIDC refuses startup while loopback dev is allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arknights_mcp.config import (
    ENV_OIDC_ISSUER,
    AppConfig,
    ConfigError,
    SyncSourceConfig,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config.example.toml"


def test_example_config_loads_with_defaults() -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.database.read_only is True
    assert cfg.mcp.default_server == "en"
    assert cfg.mcp.max_tool_response_kb == 200
    assert cfg.limits.max_page_size == 100
    assert cfg.privacy.operational_log_retention_days == 14
    assert cfg.analysis.def_high == 800
    assert cfg.sync.enabled_sources == ["arknights_assets_gamedata"]
    # Remote disabled in the example (PRD 19).
    assert cfg.mcp.remote.enabled is False


def test_sync_source_subtable_folds() -> None:
    cfg = load_config(EXAMPLE)
    assert "arknights_assets_gamedata" in cfg.sync.sources
    src = cfg.sync.sources["arknights_assets_gamedata"]
    assert src.servers == ["en", "cn"]


def test_base_url_for_substitutes_server_token() -> None:
    src = SyncSourceConfig(base_url="https://repo.test/{server}/data")
    assert src.base_url_for("en") == "https://repo.test/en/data"
    assert src.base_url_for("cn") == "https://repo.test/cn/data"
    # Distinct per region → the §V5 same-URL guard never trips.
    assert src.base_url_for("en") != src.base_url_for("cn")


def test_base_urls_override_takes_precedence() -> None:
    src = SyncSourceConfig(
        base_url="https://fallback.test",
        base_urls={"cn": "https://cn.repo.test/data"},
    )
    assert src.base_url_for("cn") == "https://cn.repo.test/data"
    assert src.base_url_for("en") == "https://fallback.test"


def test_missing_file_yields_defaults() -> None:
    cfg = load_config(REPO_ROOT / "does-not-exist.toml")
    assert isinstance(cfg, AppConfig)
    assert cfg.mcp.remote.enabled is False
    # Defaults are safe: startup check is a no-op when remote is disabled.
    cfg.assert_remote_startup_safe()


def test_example_config_is_startup_safe() -> None:
    # Remote disabled → safe regardless of placeholder auth values.
    load_config(EXAMPLE).assert_remote_startup_safe()


def test_loopback_remote_allowed_without_https_or_oauth() -> None:
    cfg = AppConfig.model_validate({"mcp": {"remote": {"enabled": True, "bind_host": "127.0.0.1"}}})
    # Loopback dev is the explicit §V9 exception.
    cfg.assert_remote_startup_safe()


def test_nonloopback_remote_without_https_refuses() -> None:
    cfg = AppConfig.model_validate(
        {
            "mcp": {
                "remote": {
                    "enabled": True,
                    "bind_host": "0.0.0.0",
                    "public_base_url": "http://mcp.example.com",
                }
            },
            "auth": {
                "mode": "oidc",
                "issuer": "https://issuer.example.com",
                "audience": "arknights-mcp",
                "jwks_url": "https://issuer.example.com/jwks",
                "required_scopes": ["arknights:read"],
            },
        }
    )
    with pytest.raises(ConfigError, match="V9"):
        cfg.assert_remote_startup_safe()


def test_nonloopback_remote_without_valid_oauth_refuses() -> None:
    cfg = AppConfig.model_validate(
        {
            "mcp": {
                "remote": {
                    "enabled": True,
                    "bind_host": "0.0.0.0",
                    "public_base_url": "https://mcp.example.com",
                }
            },
            # Placeholder OIDC descriptors → not valid.
            "auth": {
                "mode": "oidc",
                "issuer": "<OIDC issuer>",
                "audience": "<MCP audience>",
                "jwks_url": "<JWKS endpoint>",
            },
        }
    )
    with pytest.raises(ConfigError, match="OAuth/OIDC"):
        cfg.assert_remote_startup_safe()


def test_nonloopback_remote_with_https_and_oauth_ok() -> None:
    cfg = AppConfig.model_validate(
        {
            "mcp": {
                "remote": {
                    "enabled": True,
                    "bind_host": "0.0.0.0",
                    "public_base_url": "https://mcp.example.com",
                }
            },
            "auth": {
                "mode": "oidc",
                "issuer": "https://issuer.example.com",
                "audience": "arknights-mcp",
                "jwks_url": "https://issuer.example.com/jwks",
                "required_scopes": ["arknights:read"],
            },
        }
    )
    cfg.assert_remote_startup_safe()  # must not raise


def test_env_overlays_oidc_issuer() -> None:
    cfg = load_config(EXAMPLE, env={ENV_OIDC_ISSUER: "https://issuer.example.com"})
    assert cfg.auth.issuer == "https://issuer.example.com"
