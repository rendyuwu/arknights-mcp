"""``arknights-mcp account`` -- personal Yostar (en) account roster (ADR 0020).

CLI-only, run on the owner's own machine so every Yostar and game-server
request leaves from the owner's usual IP. ``login`` asks for the email and a
one-time code once and saves the resulting Yostar token pair in a mode-600
session file; ``sync`` reuses it, pulls ``account/syncData`` and replaces the
allowlisted roster in the account database (``ARKNIGHTS_MCP_ACCOUNT_DB_URL``).
``status``, ``logout`` and ``purge`` make no Yostar or game-server calls.

``login`` and ``sync`` first prove the account database is reachable and
writable, then refuse to run unattended and wait for the owner to confirm the
game is closed: a sync is a new game session, which can sign out a running app.
No config.toml is read. Nothing here prints the email, code, token, or uid.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from arknights_mcp.cli._shared import CliContext, _out
from arknights_mcp.config import ENV_ACCOUNT_DB_URL
from arknights_mcp.db.account import ACCOUNT_SERVER, AccountStore, AccountStoreError
from arknights_mcp.importers.account import parse_sync_data
from arknights_mcp.sources.base import SourceAdapterError
from arknights_mcp.sources.yostar import YostarClient, YostarSession
from arknights_mcp.util.atomic import atomic_write_text

LOGIN_NOTICE = (
    "This signs in to your Yostar account as a new device. Close Arknights first so it "
    "does not have to sign in again mid-operation."
)
SYNC_NOTICE = (
    "This signs in to the Arknights game server as a new session. An Arknights app that "
    "is still open or in the background on this account can be signed out and lose an "
    "unfinished operation."
)
GAME_CLOSED_PROMPT = (
    "Fully close Arknights on every device using this account (swipe it away, not just "
    "switch apps), then press Enter to continue or Ctrl+C to cancel: "
)
GAME_CLOSED_REFUSAL = (
    "refusing to run unattended: account commands sign in to your Arknights account and "
    "can sign out a running game; run this from a terminal after closing Arknights"
)

_NOT_LOGGED_IN = "not logged in: run `arknights-mcp account login` first"
_UNREADABLE_SESSION = (
    "the saved Yostar session is unreadable; run `arknights-mcp account login` again"
)


def _store() -> AccountStore:
    store = AccountStore.from_env(os.environ)
    if store is None:
        raise AccountStoreError(
            f"{ENV_ACCOUNT_DB_URL} is not set; put the writer URL in .env (see .env.example) "
            "and run the command through `uv run --env-file .env`"
        )
    return store


def _session_path(env: Mapping[str, str]) -> Path:
    base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "arknights-mcp" / "yostar_session.json"


def _write_session(session: YostarSession) -> None:
    path = _session_path(os.environ)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkstemp creates the file 0600.
    atomic_write_text(
        path,
        json.dumps(
            {
                "server": ACCOUNT_SERVER,
                "yostar_uid": session.yostar_uid,
                "yostar_token": session.yostar_token,
            }
        ),
    )


def _read_session() -> YostarSession:
    path = _session_path(os.environ)
    if not path.is_file():
        raise FileNotFoundError(_NOT_LOGGED_IN)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        uid, token = raw["yostar_uid"], raw["yostar_token"]
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(_UNREADABLE_SESSION) from None
    if not (isinstance(uid, str) and uid and isinstance(token, str) and token):
        raise ValueError(_UNREADABLE_SESSION)
    return YostarSession(uid, token)


def _stdin_is_tty() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise ValueError("cancelled; nothing was sent") from None


def _confirm_game_closed(notice: str) -> None:
    """Kick prevention: no bypass flag; non-TTY runs are refused."""
    if not _stdin_is_tty():
        raise ValueError(GAME_CLOSED_REFUSAL)
    _out(notice)
    _ask(GAME_CLOSED_PROMPT)


def _cmd_account_login(args: argparse.Namespace, ctx: CliContext) -> int:
    _store().check_writable()
    _confirm_game_closed(LOGIN_NOTICE)
    email = (args.email or _ask("Yostar account email: ")).strip()
    if not email:
        raise ValueError("an email address is required")
    client = YostarClient(send=ctx.yostar_send)
    client.send_email_code(email)
    _out("A verification code was sent to that address.")
    code = _ask("Verification code: ").strip()
    _write_session(client.session_from_email_code(email, code))
    _out(
        "Logged in. The session is saved (mode 600). You can open Arknights again; run "
        "`arknights-mcp account sync` later, with the game closed, to pull your roster."
    )
    return 0


def _cmd_account_sync(args: argparse.Namespace, ctx: CliContext) -> int:
    session = _read_session()
    store = _store()
    store.check_writable()
    _confirm_game_closed(SYNC_NOTICE)
    try:
        user = YostarClient(send=ctx.yostar_send).fetch_sync_data(session)
        roster = parse_sync_data(user)
        store.write_roster(roster, synced_at=datetime.now(UTC))
    except SourceAdapterError as exc:
        raise SourceAdapterError(f"{exc}; you can open Arknights again") from None
    except AccountStoreError as exc:
        raise AccountStoreError(f"{exc}; you can open Arknights again") from None
    _out(
        f"Synced the en account: {len(roster.operators)} operators, {len(roster.skins)} "
        f"skins, {len(roster.inventory)} item stacks ({roster.skipped} unexpected entries "
        "skipped)."
    )
    _out("This sync's game session is discarded; you can open Arknights again.")
    return 0


def _cmd_account_status(args: argparse.Namespace, ctx: CliContext) -> int:
    if _session_path(os.environ).is_file():
        _out("session: saved")
    else:
        _out("session: none (run `arknights-mcp account login`)")
    store = AccountStore.from_env(os.environ)
    if store is None:
        _out(f"roster: unknown ({ENV_ACCOUNT_DB_URL} is not set)")
        return 0
    stored = store.load(ACCOUNT_SERVER)
    if stored is None:
        _out("roster: none (run `arknights-mcp account sync`)")
    elif stored.roster is None:
        _out(
            f"roster: synced_at={stored.synced_at}, written by account schema "
            f"{stored.schema_version} (run `arknights-mcp account sync` to rewrite it)"
        )
    else:
        _out(f"roster: synced_at={stored.synced_at}, {len(stored.roster.operators)} operators")
    return 0


def _remove_session() -> None:
    path = _session_path(os.environ)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    _out("session removed" if existed else "no saved session")


def _cmd_account_logout(args: argparse.Namespace, ctx: CliContext) -> int:
    _remove_session()
    return 0


def _cmd_account_purge(args: argparse.Namespace, ctx: CliContext) -> int:
    # The database first: an unset or unreachable one fails before anything is deleted.
    _out("roster removed" if _store().purge() else "no synced roster")
    _remove_session()
    return 0
