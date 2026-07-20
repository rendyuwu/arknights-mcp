"""T55: the systemd + nginx + docker deploy examples exist and encode the
safe private-remote posture.

These are reference deployments for the Streamable HTTP transport (§I.api). The
assertions pin the load-bearing guardrails so an example can't silently drift
into an unsafe shape:

* §V9/§V40 — loopback bind fronted by a TLS proxy, with ``behind_proxy = true``
  the way the app forces the HTTPS + OIDC gate on a 127.0.0.1 bind.
* §V12/§I.env — OIDC descriptors + secrets are env-only, supplied through the
  three ``ARKNIGHTS_MCP_OIDC_*`` variables via ``.env`` templates that carry
  placeholders only (no real secret committed).
* §V16 — the Docker image is code-only: no data/DB baked in, ``.dockerignore``
  bars data + snapshots, the build is a read-only mounted volume.
* §V11 — pre-auth flood protection lives at the nginx proxy (``limit_req``).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"

SYSTEMD_UNIT = DEPLOY / "systemd" / "arknights-mcp.service"
SYSTEMD_ENV = DEPLOY / "systemd" / "arknights-mcp.env.example"
NGINX_CONF = DEPLOY / "nginx" / "arknights-mcp.conf"
DOCKERFILE = DEPLOY / "docker" / "Dockerfile"
DOCKERIGNORE = DEPLOY / "docker" / ".dockerignore"
COMPOSE = DEPLOY / "docker" / "docker-compose.yml"
DOCKER_ENV = DEPLOY / "docker" / "arknights-mcp.env.example"
DEPLOY_README = DEPLOY / "README.md"

#: The three non-secret OIDC descriptors the server reads from the environment
#: (§I.env; config.ENV_OIDC_*). Every env template must reference all three.
OIDC_ENV_VARS = (
    "ARKNIGHTS_MCP_OIDC_ISSUER",
    "ARKNIGHTS_MCP_OIDC_AUDIENCE",
    "ARKNIGHTS_MCP_OIDC_JWKS_URL",
)

ALL_EXAMPLES = (
    SYSTEMD_UNIT,
    SYSTEMD_ENV,
    NGINX_CONF,
    DOCKERFILE,
    DOCKERIGNORE,
    COMPOSE,
    DOCKER_ENV,
    DEPLOY_README,
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing deploy example: {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def test_all_examples_present() -> None:
    for path in ALL_EXAMPLES:
        assert path.is_file(), f"missing deploy example: {path.relative_to(REPO_ROOT)}"


def test_no_stale_gitkeep_placeholders() -> None:
    # The three subdirs carried .gitkeep placeholders before T55; real content
    # replaces them.
    for sub in ("systemd", "nginx", "docker"):
        assert not (DEPLOY / sub / ".gitkeep").exists(), f"stale .gitkeep in deploy/{sub}"


def test_systemd_unit_serves_streamable_http_via_console_script() -> None:
    text = _read(SYSTEMD_UNIT)
    # Runs the console script for the remote transport (§I.api).
    assert "serve" in text and "--transport streamable-http" in text
    assert "arknights-mcp" in text


def test_systemd_unit_is_hardened_and_non_secret() -> None:
    text = _read(SYSTEMD_UNIT)
    # Secrets come from an EnvironmentFile, never inline in the unit (§V12/§I.env).
    assert "EnvironmentFile=" in text
    for var in OIDC_ENV_VARS:
        assert f"{var}=" not in text, f"{var} value must not be baked into the unit (§V12)"
    # A few load-bearing hardening directives for a read-only server (§V1/§V2).
    for directive in ("NoNewPrivileges=true", "ProtectSystem=strict"):
        assert directive in text, f"systemd unit missing hardening: {directive}"


def test_nginx_terminates_tls_and_proxies_loopback() -> None:
    text = _read(NGINX_CONF)
    # TLS termination in front of the loopback bind (§V9).
    assert "listen 443 ssl" in text
    assert "ssl_certificate" in text
    # 80 -> 443 redirect: no cleartext MCP.
    assert "listen 80" in text
    assert "https://$host$request_uri" in text
    # The single MCP endpoint proxied to the loopback Streamable HTTP bind (§I.api).
    assert "location /mcp" in text
    assert "127.0.0.1:8000" in text
    # SSE-safe: never buffer the long-lived stream.
    assert "proxy_buffering off" in text
    assert "proxy_http_version 1.1" in text


def test_nginx_forwards_oauth_discovery_unauthenticated() -> None:
    text = _read(NGINX_CONF)
    # §V45: RFC 9728 protected-resource metadata must reach the app WITHOUT a bearer
    # so `claude mcp login` can bootstrap; a proxy forwarding only /mcp would 404 it.
    assert "location /.well-known/oauth-protected-resource" in text


def test_nginx_carries_pre_auth_flood_protection() -> None:
    text = _read(NGINX_CONF)
    # §V11 pre-auth ingress caps are the proxy's job (wrap_remote_app pins this
    # to the §T55 nginx example).
    assert "limit_req_zone" in text
    assert "limit_req " in text
    assert "limit_conn" in text


def test_dockerfile_is_code_only_and_non_root() -> None:
    text = _read(DOCKERFILE)
    # Locked, reproducible dependency install (§V25/§C).
    assert "uv sync --frozen" in text
    # Non-root runtime (defense in depth, §V1/§V2).
    assert "useradd" in text
    assert "USER arknights" in text
    # Code-only (§V16): the data dir is a mounted volume, never COPY'd in.
    assert 'VOLUME ["/app/data"]' in text
    assert "COPY data" not in text and "COPY ./data" not in text
    # Serves the remote transport (§I.api).
    assert "--transport" in text and "streamable-http" in text


def test_dockerignore_excludes_data_and_secrets() -> None:
    text = _read(DOCKERIGNORE)
    # §V16: no data / DB / snapshots in the build context.
    assert "data/" in text
    assert "*.sqlite" in text
    assert "snapshot" in text
    # §V12: local env secrets stay out of image layers.
    assert ".env" in text


def test_compose_mounts_data_read_only_and_no_direct_app_port() -> None:
    text = _read(COMPOSE)
    # The promoted build is mounted read-only (§V2/§V16).
    assert "/app/data:ro" in text
    # env-only secrets (§V12/§I.env).
    assert "env_file" in text
    # nginx is the sole public ingress; the app service publishes no host port.
    assert "443:443" in text


def test_env_templates_are_placeholders_only() -> None:
    for path in (SYSTEMD_ENV, DOCKER_ENV):
        text = _read(path)
        for var in OIDC_ENV_VARS:
            assert f"{var}=" in text, f"{path.name} missing {var}"
        # Placeholder host only -- no real issuer/tenant committed (§V12).
        assert "example" in text.lower()


def test_deploy_readme_documents_posture() -> None:
    text = _read(DEPLOY_README).lower()
    # The behind_proxy / §V9 / §V40 posture is spelled out.
    assert "behind_proxy" in text
    assert "§v9" in text or "v9" in text
    # All three fronts are covered.
    for front in ("systemd", "nginx", "docker"):
        assert front in text
