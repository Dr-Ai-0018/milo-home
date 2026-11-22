from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
            },
        },
        status_code=status,
    )


class HttpSecurity:
    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ]
                )
            await send(message)

        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            origins = [
                value.decode("latin1") for name, value in scope["headers"] if name == b"origin"
            ]
            if len(origins) != 1 or origins[0] not in self.settings.origins:
                await error_response(403, "origin_rejected", "请求来源不被允许。")(
                    scope, receive, secure_send
                )
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.settings.body_limit:
                    await error_response(413, "body_too_large", "请求内容太长。")(
                        scope, receive, secure_send
                    )
                    return
                if not message.get("more_body", False):
                    break
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            await self.app(scope, buffered_receive, secure_send)
            return
        await self.app(scope, receive, secure_send)
