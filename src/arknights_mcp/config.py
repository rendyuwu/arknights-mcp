"""Configuration loading and startup safety checks (SPEC §I ``config.toml``).

Loads ``config.toml`` into typed Pydantic models mirroring PRD Section 19, and
enforces the §V9 startup rule: a non-loopback remote deployment without HTTPS
assumptions and valid OAuth/OIDC settings must fail startup.

Secrets are never read from TOML; non-secret OIDC descriptors (issuer,
audience, jwks_url, required_scopes) may be supplied via TOML and overlaid from
the environment.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Hosts that do not require HTTPS + OAuth for remote serving (§V9 loopback dev).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0::1"})

# Environment variables that overlay the non-secret OIDC descriptors.
ENV_OIDC_ISSUER = "ARKNIGHTS_MCP_OIDC_ISSUER"
ENV_OIDC_AUDIENCE = "ARKNIGHTS_MCP_OIDC_AUDIENCE"
ENV_OIDC_JWKS_URL = "ARKNIGHTS_MCP_OIDC_JWKS_URL"

# Scalar keys of the ``[sync]`` table; any other dict-valued key is a per-source
# subtable (``[sync.<source_id>]``). Kept at module scope so Pydantic does not
# capture it as a private model attribute.
_SYNC_SCALAR_KEYS = frozenset(
    {"enabled_sources", "allow_remote_download", "retain_versions", "max_total_download_mb"}
)


class ConfigError(ValueError):
    """Raised when configuration is invalid or unsafe to serve with."""


def _is_placeholder(value: str | None) -> bool:
    """A value is unset for validation purposes if empty or a ``<...>`` stub."""
    if value is None:
        return True
    stripped = value.strip()
    return not stripped or (stripped.startswith("<") and stripped.endswith(">"))


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DatabaseConfig(_Model):
    data_dir: str = "./data"
    current_manifest: str = "./data/current.json"
    read_only: bool = True


class SyncSourceConfig(_Model):
    base_url: str = ""
    servers: list[str] = Field(default_factory=lambda: ["en", "cn"])


class SyncConfig(_Model):
    enabled_sources: list[str] = Field(default_factory=lambda: ["arknights_assets_gamedata"])
    allow_remote_download: bool = True
    retain_versions: int = 3
    max_total_download_mb: int = 500
    # Per-source subtables (``[sync.<source_id>]``) folded here from the raw TOML.
    sources: dict[str, SyncSourceConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _fold_source_subtables(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        folded: dict[str, Any] = {}
        sources: dict[str, Any] = dict(data.get("sources", {}))
        for key, value in data.items():
            if key == "sources":
                continue
            if key not in _SYNC_SCALAR_KEYS and isinstance(value, dict):
                # A ``[sync.<source_id>]`` subtable.
                sources[key] = value
            else:
                folded[key] = value
        folded["sources"] = sources
        return folded


class SourceRegistryConfig(_Model):
    policy_file: str = "./DATA_SOURCES.md"
    machine_registry: str = "./config/data_sources.toml"


class AnalysisConfig(_Model):
    def_high: int = 800
    res_high: int = 50
    pressure_window_seconds: int = 15
    max_findings: int = 30


class McpLocalConfig(_Model):
    enabled: bool = True
    transport: str = "stdio"
    log_level: str = "INFO"


class McpRemoteConfig(_Model):
    enabled: bool = False
    transport: str = "streamable-http"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    path: str = "/mcp"
    public_base_url: str = "https://mcp.example.com"
    trust_proxy_headers: bool = True

    @property
    def is_loopback(self) -> bool:
        return self.bind_host in LOOPBACK_HOSTS

    @property
    def assumes_https(self) -> bool:
        """HTTPS termination is assumed when the public base URL is https://.

        A reverse proxy terminates TLS (``trust_proxy_headers``); the public URL
        being https is the operator's declaration that HTTPS is in front.
        """
        return self.public_base_url.strip().lower().startswith("https://")


class McpConfig(_Model):
    default_server: str = "en"
    max_tool_response_kb: int = 200
    schema_version: str = "1.0"
    local: McpLocalConfig = Field(default_factory=McpLocalConfig)
    remote: McpRemoteConfig = Field(default_factory=McpRemoteConfig)


class AuthConfig(_Model):
    mode: str = "oidc"
    issuer: str | None = None
    audience: str | None = None
    jwks_url: str | None = None
    required_scopes: list[str] = Field(default_factory=lambda: ["arknights:read"])

    @property
    def is_valid_oidc(self) -> bool:
        """True when OIDC descriptors are present and non-placeholder (§V9/§V10)."""
        return (
            self.mode == "oidc"
            and not _is_placeholder(self.issuer)
            and not _is_placeholder(self.audience)
            and not _is_placeholder(self.jwks_url)
            and len(self.required_scopes) > 0
        )

    def with_env_overrides(self, env: Mapping[str, str]) -> AuthConfig:
        """Overlay non-secret OIDC descriptors from the environment (env wins)."""
        return self.model_copy(
            update={
                "issuer": env.get(ENV_OIDC_ISSUER, self.issuer),
                "audience": env.get(ENV_OIDC_AUDIENCE, self.audience),
                "jwks_url": env.get(ENV_OIDC_JWKS_URL, self.jwks_url),
            }
        )


class LimitsConfig(_Model):
    requests_per_minute_per_principal: int = 60
    max_concurrent_requests_per_principal: int = 4
    request_timeout_seconds: int = 30
    max_page_size: int = 100


class PrivacyConfig(_Model):
    log_tool_arguments: bool = False
    log_tool_results: bool = False
    operational_log_retention_days: int = 14


class AppConfig(_Model):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    source_registry: SourceRegistryConfig = Field(default_factory=SourceRegistryConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)

    def assert_remote_startup_safe(self) -> None:
        """Enforce §V9: refuse non-loopback remote without HTTPS + valid OAuth.

        No-op for local-only serving and for loopback development. Raises
        :class:`ConfigError` otherwise.
        """
        remote = self.mcp.remote
        if not remote.enabled or remote.is_loopback:
            return
        problems: list[str] = []
        if not remote.assumes_https:
            problems.append(
                "public_base_url must be https:// (HTTPS termination) for non-loopback remote"
            )
        if not self.auth.is_valid_oidc:
            problems.append(
                "valid OAuth/OIDC settings required (mode=oidc, issuer, audience, "
                "jwks_url, required_scopes) for non-loopback remote"
            )
        if problems:
            raise ConfigError(
                "refusing to start non-loopback remote mode (§V9): " + "; ".join(problems)
            )


def load_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load ``config.toml`` into an :class:`AppConfig`.

    Missing file → all defaults (remote disabled). ``env`` overlays the
    non-secret OIDC descriptors; defaults to no overlay.
    """
    raw: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.is_file():
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
    config = AppConfig.model_validate(raw)
    if env is not None:
        config = config.model_copy(update={"auth": config.auth.with_env_overrides(env)})
    return config
