"""CLI ``account`` group (ADR 0020): login/sync over a fake Yostar transport.

Proves the account flow touches only the allowlisted endpoints, stores only the
allowlisted roster, keeps the session file private, and sends nothing when the
account database is missing or unreachable, the run is unattended, or the owner
cancels at the close-the-game prompt. No config.toml is written: the account
commands need none.
"""

from __future__ import annotations

import contextlib
import re
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.account import FakeYostar

import arknights_mcp.cli.account as account_cli
import arknights_mcp.sources.yostar as yostar
from arknights_mcp.cli import main
from arknights_mcp.config import ENV_ACCOUNT_DB_URL
from arknights_mcp.db.account import AccountStore
from arknights_mcp.importers.account import (
    OWNED_CHAR_ALLOWLIST,
    OWNED_EQUIP_ALLOWLIST,
    OWNED_SKILL_ALLOWLIST,
    AccountRoster,
    OwnedModule,
    parse_sync_data,
)
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.yostar import YostarSession, check_allowed

EMAIL = "owner@example.com"

_EXPECTED_PATHS = (
    "/yostar/send-code",
    "/yostar/get-auth",
    "/user/login",
    "/config/prod/official/network_config",
    "/user/v1/getToken",
    "/official/Android/version",
    "/account/login",
    "/account/syncData",
)
_SECRETS = ("email-token", "yostar-uid-1", "yostar-token-1", "u8-token-1", "secret-1", EMAIL)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("real network")

    monkeypatch.setattr(yostar, "urllib_send", refuse)


@pytest.fixture
def url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    db_url = f"sqlite:///{tmp_path}/account.sqlite"
    monkeypatch.setenv(ENV_ACCOUNT_DB_URL, db_url)
    monkeypatch.setattr(account_cli, "_stdin_is_tty", lambda: True)
    return db_url


def _session_file(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "arknights-mcp" / "yostar_session.json"


def _answer(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    prompts: list[str] = []
    replies: Iterator[str] = iter(answers)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def _login_and_sync(monkeypatch: pytest.MonkeyPatch, fake: FakeYostar) -> None:
    _answer(monkeypatch, "", "123456")  # Enter at the game-closed prompt, then the code
    assert main(["account", "login", "--email", EMAIL], yostar_send=fake) == 0
    _answer(monkeypatch, "")
    assert main(["account", "sync"], yostar_send=fake) == 0


def test_login_then_sync_stores_only_the_allowlisted_roster(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeYostar()
    _login_and_sync(monkeypatch, fake)

    session = _session_file(tmp_path)
    assert stat.S_IMODE(session.stat().st_mode) == 0o600
    assert EMAIL not in session.read_text(encoding="utf-8")

    paths = [u for _, u in fake.calls]
    assert len(paths) == len(_EXPECTED_PATHS)
    assert all(u.endswith(p) for u, p in zip(paths, _EXPECTED_PATHS, strict=True)), paths
    assert not any("/assets/" in u for u in paths)

    stored = AccountStore(url).load("en")
    assert stored is not None and stored.roster is not None
    assert re.fullmatch(r"yostar-en-\d{8}T\d{6}Z", stored.snapshot_id)
    roster = stored.roster
    assert [op.char_id for op in roster.operators] == ["char_002_amiya", "char_9999_synthetic"]
    amiya, synthetic = roster.operators
    assert amiya.potential == 6
    assert [s.form_id for s in amiya.skills] == [
        "char_002_amiya",
        "char_002_amiya",
        "char_1001_amiya2",
    ]
    # uniequip_001_amiya is the INITIAL slot, uniequip_002_synthetic is locked.
    assert amiya.modules == (OwnedModule("char_002_amiya", "uniequip_002_amiya", 3),)
    assert synthetic.modules == ()
    assert amiya.equipped_module_id == "uniequip_002_amiya"
    assert len(roster.skins) == 4
    assert len(roster.inventory) == 3

    db_bytes = (tmp_path / "account.sqlite").read_bytes()
    assert b"SENTINEL_NICK" not in db_bytes
    assert b"SENTINEL_UID" not in db_bytes

    out = capsys.readouterr()
    for secret in _SECRETS:
        assert secret not in out.out and secret not in out.err


def test_a_rejected_token_keeps_the_previous_roster(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _login_and_sync(monkeypatch, FakeYostar())
    before = AccountStore(url).load("en")
    assert before is not None
    capsys.readouterr()

    _answer(monkeypatch, "")
    assert main(["account", "sync"], yostar_send=FakeYostar(reject_token=True)) == 1
    assert "account login" in capsys.readouterr().err
    after = AccountStore(url).load("en")
    assert after is not None
    assert (after.snapshot_id, after.content_hash) == (before.snapshot_id, before.content_hash)


def test_sync_without_a_session_says_not_logged_in(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["account", "sync"], yostar_send=FakeYostar()) == 1
    assert "not logged in" in capsys.readouterr().err


def test_logout_keeps_the_roster_and_purge_removes_both(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login_and_sync(monkeypatch, FakeYostar())
    assert main(["account", "logout"]) == 0
    assert not _session_file(tmp_path).exists()
    assert AccountStore(url).load("en") is not None

    _answer(monkeypatch, "", "123456")
    assert main(["account", "login", "--email", EMAIL], yostar_send=FakeYostar()) == 0
    assert main(["account", "purge"]) == 0
    assert AccountStore(url).load("en") is None
    assert not _session_file(tmp_path).exists()


@pytest.mark.parametrize(
    "blocked",
    [
        "https://ark-us-static-online.yo-star.com/assetbundle/official/Android/assets/26-09/hot_update_list.json",
        "http://gs.arknights.global/account/syncData",
        "https://evil.example/account/syncData",
    ],
)
def test_the_allowlist_blocks_assets_plain_http_and_foreign_hosts(blocked: str) -> None:
    with pytest.raises(SourceAdapterError, match="allowlist"):
        check_allowed(blocked)


_KEYS = st.sampled_from(
    sorted(OWNED_CHAR_ALLOWLIST | OWNED_SKILL_ALLOWLIST | OWNED_EQUIP_ALLOWLIST)
) | st.text(max_size=8)
_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=16),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(_KEYS, children, max_size=4),
    max_leaves=30,
)
_USERS = _JSON | st.fixed_dictionaries(
    {
        "status": _JSON | st.fixed_dictionaries({"gold": st.integers()}),
        "troop": _JSON
        | st.fixed_dictionaries({"chars": st.dictionaries(st.text(max_size=3), _JSON)}),
        "skin": _JSON,
        "inventory": _JSON,
    }
)


@settings(max_examples=300, deadline=None)
@given(_USERS)
def test_parse_sync_data_returns_a_roster_or_a_typed_error(user: object) -> None:
    with contextlib.suppress(SourceAdapterError):
        assert isinstance(parse_sync_data(user), AccountRoster)


@pytest.mark.parametrize(
    ("argv", "tweak", "code", "fragments"),
    [
        (["account", "sync"], "no_tty", 1, ("refusing to run unattended",)),
        (["account", "login", "--email", EMAIL], "no_tty", 1, ("refusing to run unattended",)),
        (["account", "sync"], "eof", 1, ("cancelled; nothing was sent",)),
        (
            ["account", "login", "--email", EMAIL],
            "no_url",
            1,
            (f"{ENV_ACCOUNT_DB_URL} is not set",),
        ),
        (["account", "sync"], "unreachable", 1, ("unreachable or read-only", "nothing was sent")),
        (["account", "status"], None, 0, ()),
        (["account", "logout"], None, 0, ()),
        (["account", "purge"], None, 0, ()),
    ],
)
def test_nothing_is_sent(
    argv: list[str],
    tweak: str | None,
    code: int,
    fragments: tuple[str, ...],
    url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account_cli._write_session(YostarSession("uid", "token"))
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        if tweak == "eof":
            raise EOFError
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    if tweak == "no_tty":
        monkeypatch.setattr(account_cli, "_stdin_is_tty", lambda: False)
    elif tweak == "no_url":
        monkeypatch.delenv(ENV_ACCOUNT_DB_URL)
    elif tweak == "unreachable":
        monkeypatch.setenv(ENV_ACCOUNT_DB_URL, f"sqlite:///{tmp_path}/no-such-dir/account.sqlite")

    fake = FakeYostar()
    assert main(argv, yostar_send=fake) == code
    err = capsys.readouterr().err
    assert fake.calls == []
    for fragment in fragments:
        assert fragment in err
    if tweak in ("no_url", "unreachable"):
        assert prompts == []
    if tweak == "no_tty" and argv[1] == "sync":
        assert AccountStore(url).load("en") is None
