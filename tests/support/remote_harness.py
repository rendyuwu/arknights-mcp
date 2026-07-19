"""One home for the offline authenticated-remote server the wire tests need (§V37).

Both §T56 (happy-path shared-core parity) and §T57 (adversarial security/privacy
matrix) drive the *full* auth-requiring remote stack over a real loopback socket:
the shared core promoted from the pinned 4-4 fixture, wrapped in
:func:`~arknights_mcp.transports.streamable_http.wrap_remote_app` with a real
:class:`~arknights_mcp.auth.oidc.OidcTokenVerifier` whose JWKS key is resolved from a
local issuer (no network, §V1). Rather than each suite re-implementing the config
write + fixture import + uvicorn thread lifecycle, that scaffolding lives here once
(§V37); the suites supply only their assertions.

Offline + deterministic: the active build is promoted via the real ``import`` CLI
(no network, §V1); the OIDC keypair + JWKS are local (no provider reached, §V10).
TLS is the reverse proxy's job (§I.api); the process speaks plain HTTP on loopback,
with ``behind_proxy`` auth semantics enforced in the app layer (§V40).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from arknights_mcp.app import build_application
from arknights_mcp.auth.oidc import OidcTokenVerifier
from arknights_mcp.cli import main
from arknights_mcp.config import load_config
from arknights_mcp.transports.streamable_http import build_asgi_app, wrap_remote_app
from tests.support.oidc_issuer import LocalOidcIssuer

#: Repo root (this file is ``<root>/tests/support/remote_harness.py``).
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "stage_4_4"
REGISTRY = REPO_ROOT / "config" / "data_sources.toml"

#: The read-only tool set the shared registry exposes (§V14) -- the authenticated
#: remote server must enumerate exactly this over the wire, identical to stdio. One
#: home (§V37) shared by the parity test and the isolation test's post-probe check.
EXPECTED_TOOLS = frozenset(
    {
        "search_entities",
        "search_stages",
        "get_stage",
        "get_enemy",
        "get_operator",
        "compare_operator_modules",
        "analyze_stage",
    }
)


def _write_config(tmp_path: Path, *, limits: dict[str, int] | None) -> Path:
    """Write a minimal loopback config; optionally override ``[limits]`` (§V11)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "[database]\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        f'current_manifest = "{(data_dir / "current.json").as_posix()}"\n'
        "\n[source_registry]\n"
        f'machine_registry = "{REGISTRY.as_posix()}"\n'
    )
    if limits:
        text += "\n[limits]\n" + "".join(f"{k} = {v}\n" for k, v in limits.items())
    config = tmp_path / "config.toml"
    config.write_text(text, encoding="utf-8")
    return config


def _promote_fixture_build(config: Path) -> None:
    rc = main(
        ["--config", str(config), "import", "--server", "en", "--source-path", str(FIXTURE_ROOT)]
    )
    assert rc == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def remote_server(
    tmp_path: Path, *, limits: dict[str, int] | None = None
) -> Iterator[tuple[str, LocalOidcIssuer]]:
    """Serve the fixture build behind the full auth-requiring remote stack.

    Wraps the shared ASGI app in :func:`wrap_remote_app` with a real
    :class:`OidcTokenVerifier` whose JWKS key is resolved from a local issuer, then
    binds uvicorn on an ephemeral loopback port. Yields the ``/mcp`` URL and the
    issuer (so a caller mints a bearer the verifier will accept, or shapes an attack
    token on the same trusted keypair). ``limits`` overrides ``[limits]`` so the §V11
    rate-limit test can drive a low per-principal cap without a bespoke harness (§V37).
    """
    config_path = _write_config(tmp_path, limits=limits)
    _promote_fixture_build(config_path)
    config = load_config(config_path)
    core = build_application(config)

    issuer = LocalOidcIssuer()
    verifier = OidcTokenVerifier(issuer.settings, jwks_client=issuer.jwks_resolver)
    app = build_asgi_app(core, path="/mcp")
    # The full §T54 stack: redacted logging → bearer (§V10) → rate/concurrency →
    # request limits (§V11) → session manager. Real verifier, local JWKS.
    wrapped = wrap_remote_app(app, config, verifier, issuer.settings)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(wrapped, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{port}/mcp", issuer
    finally:
        server.should_exit = True
        thread.join(timeout=15)


__all__ = ["EXPECTED_TOOLS", "REPO_ROOT", "remote_server"]
