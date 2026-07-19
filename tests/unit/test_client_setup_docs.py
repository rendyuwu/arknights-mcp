"""T48: client-setup docs exist and carry the *current* official MCP config
formats plus the read-only / CLI-only guardrail notes.

`docs/clients/claude-code.md` and `docs/clients/codex.md` are the setup guides
the README promises. This guard keeps them from drifting away from the actual
`serve --transport stdio` invocation, the current Claude Code / Codex config
formats, and the guardrails they must not contradict: data is built via the
admin CLI (§V28) and the server never fetches at query time (§V1), it is
read-only (§V2), and stdio keeps the protocol on stdout / logs on stderr
(§V13).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENTS_DIR = REPO_ROOT / "docs" / "clients"


def _norm(name: str) -> str:
    text = (CLIENTS_DIR / name).read_text(encoding="utf-8")
    return " ".join(text.split()).lower()


def test_client_docs_exist() -> None:
    for name in ("claude-code.md", "codex.md"):
        assert (CLIENTS_DIR / name).is_file(), f"missing docs/clients/{name}"


def test_both_docs_use_the_stdio_serve_invocation() -> None:
    # The one supported v0.1 transport; docs must show the real command.
    for name in ("claude-code.md", "codex.md"):
        assert "arknights-mcp serve --transport stdio" in _norm(name)


def test_claude_code_doc_uses_current_format() -> None:
    text = _norm("claude-code.md")
    # Current official Claude Code MCP surface.
    assert "claude mcp add" in text
    assert "--transport stdio" in text
    assert ".mcp.json" in text
    assert '"mcpservers"' in text  # project-scope JSON shape
    assert "--scope" in text


def test_codex_doc_uses_current_format() -> None:
    text = _norm("codex.md")
    # Current official Codex MCP surface.
    assert "codex mcp add" in text
    assert "[mcp_servers.arknights]" in text
    assert "startup_timeout_sec" in text


def test_both_docs_carry_guardrail_notes() -> None:
    for name in ("claude-code.md", "codex.md"):
        text = _norm(name)
        # §V28: building data is an admin-CLI step, not an MCP tool.
        assert "arknights-mcp import" in text or "arknights-mcp sync" in text
        assert "cli" in text
        # §V1: no query-time fetch.
        assert "query time" in text or "query-time" in text
        # §V2: read-only.
        assert "read-only" in text
        # §V13: stdout = protocol, stderr = logs.
        assert "stdout" in text and "stderr" in text


def test_readme_links_both_client_docs() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/clients/claude-code.md" in text
    assert "docs/clients/codex.md" in text
