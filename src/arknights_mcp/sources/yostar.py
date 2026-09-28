"""Yostar (en) account client for CLI ``account login|sync`` (ADR 0020).

Stdlib only. Implements the email + one-time-code login and the syncData pull
that ArkPRTS documents; no ArkPRTS code is used, only the protocol constants as
interoperability facts. CLI-only: no MCP process imports this module (§V1).

Every request passes :func:`check_allowed`, a host+path allowlist checked as
pairs, so no asset or game-data endpoint is reachable: the only asset-host path is
the small ``…/official/Android/version`` JSON that ``account/login`` needs.
Redirects are refused. Nothing here logs, and no message carries the email, code,
or a token (§V12/§V15). ``fetch_sync_data`` keeps the u8 token and the
game-session secret in local variables only, never retries, and every connection
is closed (urllib sends ``Connection: close``), so no game-server state outlives
the call.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import ssl
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit

from arknights_mcp.sources.base import SourceAdapterError, json_within_limits

#: (method, url, body, headers) -> response bytes. Test seam.
YostarSend = Callable[[str, str, bytes | None, Mapping[str, str]], bytes]

NETWORK_CONFIG_URL = "https://ak-conf.arknights.global/config/prod/official/network_config"
YOSTAR_SDK = "https://en-sdk-api.yostarplat.com"

_U8_SIGN_KEY = b"91240f70c09a08a6bc72af1a5c8d4670"
_YOSTARPLAT_SALT = "886c085e4a8d30a703367b120dd8353948405ec2"
_GAME_HEADERS = {
    "Content-Type": "application/json",
    "X-Unity-Version": "2017.4.39f1",
    "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; KB2000 Build/RP1A.201005.001)",
}

_TIMEOUT_S = 30
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: (host suffix, allowed path suffixes): a URL passes only when both match the
#: same pair.
_ALLOWED: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        ".arknights.global",
        (
            "/config/prod/official/network_config",
            "/user/v1/getToken",
            "/account/login",
            "/account/syncData",
        ),
    ),
    (".yostarplat.com", ("/yostar/send-code", "/yostar/get-auth", "/user/login")),
    (".yo-star.com", ("/official/Android/version",)),
)


@dataclass(frozen=True)
class YostarSession:
    yostar_uid: str
    yostar_token: str


def check_allowed(url: str) -> None:
    """Raise unless ``url`` is https and its host+path match one allowlisted pair."""
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path
    if parts.scheme == "https" and any(
        host.endswith(suffix) and path.endswith(paths) for suffix, paths in _ALLOWED
    ):
        return
    raise SourceAdapterError(f"blocked a request outside the account-sync allowlist: {host}{path}")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _read_capped(response: Any, host: str) -> bytes:
    data: bytes = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(data) > _MAX_RESPONSE_BYTES:
        raise SourceAdapterError(f"response too large from {host}")
    return data


def urllib_send(method: str, url: str, body: bytes | None, headers: Mapping[str, str]) -> bytes:
    host = urlsplit(url).hostname or ""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(
        _RefuseRedirects, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=_TIMEOUT_S) as response:
            return _read_capped(response, host)
    except HTTPError as exc:
        with exc:
            if not 400 <= exc.code < 600:
                raise SourceAdapterError(f"redirect refused from {host}") from None
            data = _read_capped(exc, host)
        if data.startswith(b"{"):
            return data
        raise SourceAdapterError(f"HTTP {exc.code} from {host}") from None
    except (URLError, OSError, TimeoutError, http.client.HTTPException):
        raise SourceAdapterError(f"network error talking to {host}") from None


def _dumps(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _str(data: Mapping[str, Any], key: str, step: str) -> str:
    value = data.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value:
        raise SourceAdapterError(f"unexpected {step} response")
    return value


def _dict(value: Any, step: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceAdapterError(f"unexpected {step} response")
    return value


def _check_game(data: Mapping[str, Any], step: str, hint: str = "") -> None:
    result = data.get("result")
    is_int = isinstance(result, int) and not isinstance(result, bool)
    # u8 rejects a stale token as {"message": "verify fail"}, with no result.
    if data.get("error") or (is_int and result != 0) or (result is None and "message" in data):
        shown = result if is_int else "?"
        # Like ArkPRTS: a captcha key only matters on a rejected response.
        if "captcha" in data:
            raise SourceAdapterError(
                f"the game server asked for a captcha on {step} (result {shown}); "
                "log in once in the game client, then retry"
            )
        raise SourceAdapterError(f"the game server rejected {step} (result {shown}){hint}")


class YostarClient:
    def __init__(self, send: YostarSend | None = None) -> None:
        # The default is resolved per call so tests can patch ``urllib_send``.
        self._send = send
        self._device_id = uuid.uuid4().hex
        self._device_id2 = "86" + "".join(secrets.choice("0123456789") for _ in range(13))
        self._device_id3 = uuid.uuid4().hex

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        check_allowed(url)
        raw = (self._send or urllib_send)(method, url, body, {**_GAME_HEADERS, **(headers or {})})
        host = urlsplit(url).hostname or ""
        try:
            parsed = json.loads(raw)
        except (ValueError, RecursionError):
            raise SourceAdapterError(f"unexpected response from {host}") from None
        json_within_limits(parsed)
        if not isinstance(parsed, dict):
            raise SourceAdapterError(f"unexpected response from {host}")
        return parsed

    def _yostarplat(self, endpoint: str, data: object, step: str) -> dict[str, Any]:
        head = {
            "PID": "US-ARKNIGHTS",
            "Channel": "googleplay",
            "Platform": "android",
            "Version": "4.10.0",
            "GVersionNo": "2000112",
            "GBuildNo": "",
            "Lang": "en",
            "DeviceID": str(uuid.uuid4()),
            "DeviceModel": "F9",
            "UID": "",
            "Token": "",
            "Time": int(time.time()),
        }
        body = _dumps(data)
        sign = hashlib.md5((_dumps(head) + body + _YOSTARPLAT_SALT).encode()).hexdigest().upper()
        response = self._request(
            "POST",
            f"{YOSTAR_SDK}/{endpoint}",
            body.encode(),
            {"Authorization": _dumps({"Head": head, "Sign": sign})},
        )
        code = response.get("Code")
        if code != 200:
            shown = code if isinstance(code, int) and not isinstance(code, bool) else "?"
            raise SourceAdapterError(f"Yostar rejected {step} (code {shown})")
        return response

    def send_email_code(self, email: str) -> None:
        self._yostarplat(
            "yostar/send-code",
            {"Account": email, "Randstr": "", "Ticket": ""},
            "the code request",
        )

    def session_from_email_code(self, email: str, code: str) -> YostarSession:
        auth = self._yostarplat(
            "yostar/get-auth", {"Account": email, "Code": code}, "the verification code"
        )
        email_token = _str(_dict(auth.get("Data"), "get-auth"), "Token", "get-auth")
        login = self._yostarplat(
            "user/login",
            {
                "CheckAccount": 0,
                "Geetest": {
                    "CaptchaID": None,
                    "CaptchaOutput": None,
                    "GenTime": None,
                    "LotNumber": None,
                    "PassToken": None,
                },
                "OpenID": email,
                "Secret": "",
                "Token": email_token,
                "Type": "yostar",
                "UserName": email,
            },
            "the login",
        )
        info = _dict(_dict(login.get("Data"), "login").get("UserInfo"), "login")
        return YostarSession(_str(info, "ID", "login"), _str(info, "Token", "login"))

    def fetch_sync_data(self, session: YostarSession) -> dict[str, Any]:
        """Five requests, no retries: network config, u8 token, version, login, syncData."""
        config = self._request("GET", NETWORK_CONFIG_URL)
        try:
            content = json.loads(config["content"])
            json_within_limits(content)
            net = content["configs"][content["funcVer"]]["network"]
            gs, u8, hv = net["gs"], net["u8"], net["hv"]
        except (KeyError, TypeError, ValueError, RecursionError):
            raise SourceAdapterError("unexpected network config response") from None
        if not all(isinstance(value, str) for value in (gs, u8, hv)):
            raise SourceAdapterError("unexpected network config response")

        body: dict[str, Any] = {
            "appId": "1",
            "platform": 1,
            "channelId": "3",
            "subChannel": "3",
            "extension": _dumps(
                {"type": 1, "uid": session.yostar_uid, "token": session.yostar_token}
            ),
            "worldId": "3",
            "deviceId": self._device_id,
            "deviceId2": self._device_id2,
            "deviceId3": self._device_id3,
        }
        body["sign"] = (
            hmac.new(_U8_SIGN_KEY, urlencode(sorted(body.items())).encode(), "sha1")
            .hexdigest()
            .lower()
        )
        u8_data = self._request("POST", f"{u8}/user/v1/getToken", _dumps(body).encode())
        _check_game(u8_data, "the saved session", "; run `arknights-mcp account login` again")
        uid, u8_token = _str(u8_data, "uid", "getToken"), _str(u8_data, "token", "getToken")

        # str.replace, not str.format: the template is untrusted server data.
        version = self._request("GET", hv.replace("{0}", "Android"))
        res_version = _str(version, "resVersion", "version")
        client_version = _str(version, "clientVersion", "version")

        login = self._request(
            "POST",
            f"{gs}/account/login",
            _dumps(
                {
                    "platform": 1,
                    "networkVersion": "1",
                    "assetsVersion": res_version,
                    "clientVersion": client_version,
                    "token": u8_token,
                    "uid": uid,
                    "deviceId": self._device_id,
                    "deviceId2": self._device_id2,
                    "deviceId3": self._device_id3,
                }
            ).encode(),
            {"secret": "", "seqnum": "1", "uid": uid},
        )
        _check_game(login, "the game login")
        secret = _str(login, "secret", "account/login")

        synced = self._request(
            "POST",
            f"{gs}/account/syncData",
            _dumps({"platform": 1}).encode(),
            {"secret": secret, "seqnum": "2", "uid": uid},
        )
        _check_game(synced, "the roster request")
        return _dict(synced.get("user"), "syncData")
