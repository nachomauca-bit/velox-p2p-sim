"""HTTP basic auth in front of the whole app: one shared user name and password (brief section 17).

Only for showing the Cloud Run URL safely; it is not a user or role system. Registered in main.py when
config.APP_PASSWORD is set (empty = off, the local default):

    app.add_middleware(BasicAuthMiddleware, username=config.APP_USERNAME, password=config.APP_PASSWORD)

Pure ASGI (no BaseHTTPMiddleware): it answers 401 before the app sees the request, so it also covers the
static files and the intake webhook. The credentials are compared in constant time. /health stays open for
uptime checks.
"""
from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any, Optional

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REALM = "Velox"
UNAUTHORIZED_BODY = b"Authentication required.\n"


def parse_basic(header: Optional[bytes]) -> Optional[tuple[bytes, bytes]]:
    """(user, password) from an 'Authorization: Basic <base64>' header value; None if absent or malformed."""
    if not header:
        return None
    scheme, _, token = header.strip().partition(b" ")
    if scheme.lower() != b"basic" or not token.strip():
        return None
    try:
        decoded = base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    user, sep, password = decoded.partition(b":")
    return (user, password) if sep else None


class BasicAuthMiddleware:
    """Require the shared credentials on every HTTP request except the exempt paths."""

    def __init__(self, app: ASGIApp, username: str, password: str, *, realm: str = REALM,
                 exempt_paths: Iterable[str] = ("/health",)) -> None:
        if not password:
            raise ValueError("BasicAuthMiddleware needs a password (leave it unregistered to switch auth off)")
        self.app = app
        self.username = username.encode("utf-8")
        self.password = password.encode("utf-8")
        self.realm = realm
        self.exempt_paths = frozenset(exempt_paths)

    def authorized(self, header: Optional[bytes]) -> bool:
        """Constant-time check of both parts (no early exit that would tell which part was wrong)."""
        credentials = parse_basic(header)
        if credentials is None:
            return False
        user_ok = hmac.compare_digest(credentials[0], self.username)
        password_ok = hmac.compare_digest(credentials[1], self.password)
        return user_ok & password_ok

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        header = dict(scope.get("headers") or []).get(b"authorization")
        if self.authorized(header):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":  # the app has none; refuse the handshake anyway
            await send({"type": "websocket.close", "code": 1008})
            return
        await self._unauthorized(send)

    async def _unauthorized(self, send: Send) -> None:
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"www-authenticate", f'Basic realm="{self.realm}", charset="UTF-8"'.encode("latin-1")),
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(UNAUTHORIZED_BODY)).encode("latin-1")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})
