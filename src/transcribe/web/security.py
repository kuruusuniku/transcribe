"""Web UI のアクセス制御（ASGI ミドルウェア）。

- Origin チェック: 他サイトのページから localhost の API を叩かれる（CSRF / DNS rebinding 系）のを防ぐ。
  ブラウザは cross-origin の POST / WebSocket に Origin を付けるため、Host と一致しないものを拒否する。
- トークン認証: app.state.auth_token が設定されている場合のみ有効。
  初回は `/?token=xxx` でアクセスすると HttpOnly Cookie が発行され、以降の API / WebSocket は Cookie で認証する。
"""

from __future__ import annotations

import hmac
import logging
import re
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "transcribe_token"

_TOKEN_IN_URL_RE = re.compile(r"token=[^\s\"&]+")
_REDACTED = "token=***"


class RedactTokenFilter(logging.Filter):
    """アクセスログに残る ?token=... を伏せる（履歴やログからのトークン流出を防ぐ）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_URL_RE.sub(_REDACTED, record.msg)
        if record.args:
            record.args = tuple(
                _TOKEN_IN_URL_RE.sub(_REDACTED, a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


def install_log_redaction() -> None:
    """uvicorn のアクセスログなどからトークンを伏せる。"""
    log_filter = RedactTokenFilter()
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokenFilter) for f in logger.filters):
            logger.addFilter(log_filter)


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _origin_allowed(headers: dict[str, str]) -> bool:
    origin = headers.get("origin")
    if not origin:
        # 同一オリジンの GET や curl 等は Origin を付けない
        return True
    host = headers.get("host", "")
    return urlparse(origin).netloc == host


def _cookie_token(headers: dict[str, str]) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel else None


def _bearer_token(headers: dict[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _match(expected: str, given: str | None) -> bool:
    return given is not None and hmac.compare_digest(expected.encode(), given.encode())


class AccessControlMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = _headers(scope)
        if not _origin_allowed(headers):
            await self._deny(scope, receive, send, 403, "Forbidden origin")
            return

        token: str | None = getattr(scope["app"].state, "auth_token", None)
        if not token:
            await self.app(scope, receive, send)
            return

        if _match(token, _cookie_token(headers)) or _match(token, _bearer_token(headers)):
            await self.app(scope, receive, send)
            return

        # ?token=xxx でのアクセスは Cookie を発行してクエリなしの URL にリダイレクト
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if scope["type"] == "http" and _match(token, (query.get("token") or [None])[0]):
            resp = RedirectResponse(scope.get("path", "/"), status_code=303)
            resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict")
            await resp(scope, receive, send)
            return

        await self._deny(scope, receive, send, 401, "Unauthorized: /?token=<token> でアクセスしてください")

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send, status: int, message: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await PlainTextResponse(message, status_code=status)(scope, receive, send)
