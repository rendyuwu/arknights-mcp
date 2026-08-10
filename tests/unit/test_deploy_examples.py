"""T55/T215: the systemd + nginx + docker deploy examples exist and encode the
safe posture for both transports.

These are reference deployments for the Streamable HTTP transport (§I.api) and,
since T215, for local ``stdio`` out of the same image (§V14). The assertions pin
the load-bearing guardrails so an example can't silently drift into an unsafe or
non-working shape:

* §V9/§V40 — loopback bind fronted by a TLS proxy, with ``behind_proxy = true``
  the way the app forces the HTTPS + OIDC gate on a 127.0.0.1 bind.
* §V12/§I.env — OIDC descriptors + secrets are env-only, supplied through the
  three ``ARKNIGHTS_MCP_OIDC_*`` variables via ``.env`` templates that carry
  placeholders only (no real secret committed).
* §V16 — the Docker image is code-only: no data/DB baked in, ``.dockerignore``
  bars data + snapshots, the build is a read-only mounted volume.
* §V11 — pre-auth flood protection lives at the nginx proxy (``limit_req``).
* §V13/§V14 (T215) — the transport is in ``CMD``, never welded into
  ``ENTRYPOINT``, so one image serves both; and the stdio service keeps stdin open
  with no TTY, because a TTY folds stderr into the stdout the JSON-RPC frames own.
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


def _dockerfile_directive(name: str) -> str:
    """The single ``ENTRYPOINT``/``CMD`` line, ignoring the comment prose above it."""
    matches = [ln for ln in _read(DOCKERFILE).splitlines() if ln.startswith(f"{name} ")]
    assert len(matches) == 1, f"expected exactly one {name} in the Dockerfile, got {matches}"
    return matches[0]


def _compose_service(name: str) -> str:
    """One service's own block from the compose file.

    PyYAML is not a dependency, so this slices by indentation rather than parsing:
    services sit at two spaces, their keys deeper. Slicing matters because the
    absence assertions below (no ``ports:``, no ``env_file``) are about *this*
    service -- the file as a whole legitimately contains both. Comment lines are
    dropped for the same reason: a comment explaining *why* a key is absent must
    not read as the key being present.
    """
    lines = _read(COMPOSE).splitlines()
    starts = [i for i, ln in enumerate(lines) if ln == f"  {name}:"]
    assert len(starts) == 1, f"expected exactly one `{name}:` service, got {len(starts)}"
    block: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        if line.lstrip().startswith("#"):
            continue
        block.append(line)
    return "\n".join(block)


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


def test_dockerfile_keeps_the_transport_out_of_entrypoint() -> None:
    # T215/§V14: one image, both transports. The transport must stay in CMD so
    # `docker run ... serve --transport stdio` overrides it; welding it into
    # ENTRYPOINT would make stdio need a second image.
    entrypoint = _dockerfile_directive("ENTRYPOINT")
    assert "--transport" not in entrypoint, f"transport welded into ENTRYPOINT: {entrypoint}"
    assert "streamable-http" not in entrypoint and "stdio" not in entrypoint
    # ENTRYPOINT still pins the config path, so an argument override can't quietly
    # swap in a different config.
    assert "/app/config.toml" in entrypoint
    # ...and the remote transport stays the default, in CMD.
    cmd = _dockerfile_directive("CMD")
    assert "serve" in cmd and "--transport" in cmd and "streamable-http" in cmd


def test_dockerfile_is_unbuffered_so_stdio_replies_are_not_held_in_a_pipe() -> None:
    # §V13: stdout carries the JSON-RPC frames. A block-buffered stdout parks a
    # reply until the buffer fills, which reads to the client as a hung server.
    assert "PYTHONUNBUFFERED=1" in _read(DOCKERFILE)


def test_compose_stdio_service_is_profile_gated_and_pipe_shaped() -> None:
    stdio = _compose_service("mcp-stdio")
    # Behind a profile: `up` must not start it. A stdio server owns a pipe and
    # exits at EOF -- it is not a listener to bring up beside nginx.
    assert "profiles:" in stdio
    assert '"stdio"' in stdio.split("profiles:")[1].splitlines()[0]
    # The other services must NOT be profile-gated -- `up` still brings the remote
    # stack up, which is what this file was for before T215.
    for name in ("mcp", "nginx"):
        assert "profiles:" not in _compose_service(name)
    # The transport override is the whole point of the service.
    assert '"--transport", "stdio"' in stdio
    # stdin open, no TTY (§V13: a TTY folds stderr into stdout and rewrites
    # newlines, corrupting the framing).
    assert "stdin_open: true" in stdio
    assert "tty: false" in stdio


def test_compose_stdio_service_has_no_listener_and_no_oidc_env() -> None:
    stdio = _compose_service("mcp-stdio")
    # No bind at all: a local pipe publishes nothing.
    assert "ports:" not in stdio and "expose:" not in stdio
    # No env_file/OIDC: there is no §V9 gate on a pipe, so requiring the operator's
    # OIDC template here would block stdio on a file it has no use for.
    assert "env_file" not in stdio
    for var in OIDC_ENV_VARS:
        assert var not in stdio
    # Exiting at EOF is a normal end of session, not something to restart.
    assert "restart:" not in stdio


def test_compose_remote_env_file_is_optional_so_stdio_validates() -> None:
    # Compose validates EVERY service in the file, so a hard-required env_file made
    # `compose run mcp-stdio` fail on a missing OIDC file. Optional here is safe: an
    # OIDC-less remote start still fails closed in the app (§V9/§V40).
    mcp = _compose_service("mcp")
    assert "env_file" in mcp
    assert "required: false" in mcp


def test_compose_shares_one_image_between_both_transports() -> None:
    text = _read(COMPOSE)
    # §V37: one definition of build+image+mounts, so a change can't land on one
    # transport and miss the other.
    assert "&mcp-app" in text
    assert text.count("<<: *mcp-app") == 2
    for name in ("mcp", "mcp-stdio"):
        assert "<<: *mcp-app" in _compose_service(name)


def test_compose_runs_non_root_with_an_operator_overridable_uid() -> None:
    # The container reads the mounted build through host permissions; `import`
    # writes data/current.json mode 600 owned by the operator, so a fixed uid 999
    # yields PermissionError and every tool answers internal_error. Default stays
    # the image's non-root user (§V1/§V2); the override is read-only either way.
    text = _read(COMPOSE)
    assert 'user: "${ARKNIGHTS_MCP_UID:-999}:${ARKNIGHTS_MCP_GID:-999}"' in text
    assert "user: root" not in text and "user: 0" not in text


def test_deploy_readme_documents_the_stdio_docker_path() -> None:
    text = _read(DEPLOY_README)
    assert "--transport stdio" in text
    assert "mcp-stdio" in text
    # The three ways to break it silently, each named.
    assert "-i" in text
    assert "-t" in text and "TTY" in text
    assert "--user" in text and "PermissionError" in text


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
