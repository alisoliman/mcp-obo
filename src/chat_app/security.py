"""Response headers, stamped once for every route including the SSE stream."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS = {
    # style-src has to allow inline styles: Radix's dropdown pulls in
    # react-remove-scroll, which injects a <style> element at runtime to lock
    # body scroll, and a static bundle has no way to carry a per-response nonce.
    # Everything else is closed. img-src is the one that blocks the prompt
    # injection exfiltration channel — table COMMENT text from Unity Catalog
    # reaches the model, and a remote <img> in the model's markdown would be a
    # silent GET carrying whatever it was told to encode.
    #
    # frame-ancestors is load-bearing rather than routine. The session cookie
    # must stay SameSite=none for Entra's cross-site form POST, so it rides
    # along inside a hostile iframe; without frame-ancestors an attacker page
    # can frame the signed-in UI, with its CSRF token already fetched, and
    # drive a real turn by redressing the send button.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "object-src 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware:
    """Stamps the response headers on every route, including the SSE stream.

    Raw ASGI rather than BaseHTTPMiddleware on purpose: BaseHTTPMiddleware pumps
    every body chunk through an anyio stream, which would put a queue between
    the agent's token deltas and the browser. This only rewrites the
    `http.response.start` message and never touches the body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
