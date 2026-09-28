"""Account roster test support (ADR 0020): the synthetic syncData fixture, a shared
read-only fixture store, and a fake Yostar transport.

The fixture store is written once per session into a temp dir and never mutated: a
test that needs a modified database copies :func:`account_fixture_path` first.
"""

from __future__ import annotations

import atexit
import functools
import json
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arknights_mcp.db.account import AccountStore
from arknights_mcp.importers.account import parse_sync_data

ACCOUNT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "account" / "sync_data_en.json"
FIXTURE_SYNCED_AT = datetime(2026, 9, 29, tzinfo=UTC)


def fixture_user() -> dict[str, Any]:
    user: dict[str, Any] = json.loads(ACCOUNT_FIXTURE.read_text(encoding="utf-8"))
    return user


@functools.cache
def account_fixture_path() -> Path:
    tmp = tempfile.mkdtemp(prefix="arknights-account-fixture-")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    path = Path(tmp) / "account.sqlite"
    AccountStore(f"sqlite:///{path}").write_roster(
        parse_sync_data(fixture_user()), synced_at=FIXTURE_SYNCED_AT
    )
    return path


@functools.cache
def account_fixture_store() -> AccountStore:
    return AccountStore(f"sqlite:///{account_fixture_path()}")


_NETWORK_CONFIG = {
    "funcVer": "V1",
    "configs": {
        "V1": {
            "network": {
                "gs": "https://gs.arknights.global:8443",
                "u8": "https://as.arknights.global/u8",
                "hv": "https://ark-us-static-online.yo-star.com/assetbundle/official/{0}/version",
            }
        }
    },
}


class FakeYostar:
    """A :data:`~arknights_mcp.sources.yostar.YostarSend` that answers by path suffix
    and records every ``(method, url)``."""

    def __init__(self, *, reject_token: bool = False) -> None:
        self.reject_token = reject_token
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> bytes:
        self.calls.append((method, url))
        return json.dumps(self._answer(url)).encode()

    def _answer(self, url: str) -> object:
        if url.endswith("/network_config"):
            return {"content": json.dumps(_NETWORK_CONFIG)}
        if url.endswith("/yostar/send-code"):
            return {"Code": 200, "Data": {}}
        if url.endswith("/yostar/get-auth"):
            return {"Code": 200, "Data": {"Token": "email-token"}}
        if url.endswith("/user/login"):
            return {
                "Code": 200,
                "Data": {"UserInfo": {"ID": "yostar-uid-1", "Token": "yostar-token-1"}},
            }
        if url.endswith("/user/v1/getToken"):
            if self.reject_token:
                return {"result": 1}
            return {"result": 0, "uid": "game-uid-1", "token": "u8-token-1"}
        if url.endswith("/version"):
            return {"resVersion": "26-09-01", "clientVersion": "2.6.41"}
        if url.endswith("/account/login"):
            return {"result": 0, "secret": "secret-1"}
        if url.endswith("/account/syncData"):
            return {"result": 0, "user": fixture_user()}
        raise AssertionError(f"unexpected request: {url}")
