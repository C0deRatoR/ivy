import asyncio

from aiohttp import web
from aiohttp.typedefs import Handler

from .agent import Agent
from .store import Store

# Set by `tailscale serve` on every proxied request from a tailnet user. The API listens on
# 127.0.0.1 only, so requests can't reach it without passing through Tailscale.
LOGIN_HEADER = "Tailscale-User-Login"
MAX_TEXT = 4000
CHAT_KEY = web.RequestKey("chat_key", str)


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


class AppChannel:
    """JSON API for the Flutter app. Sign-in is your Tailscale identity."""

    def __init__(self, allowed: frozenset[str], agent: Agent, store: Store, port: int) -> None:
        self.allowed = allowed
        self.agent = agent
        self.store = store
        self.port = port
        self.app = web.Application(middlewares=[self._auth])
        self.app.add_routes(
            [
                web.get("/api/me", self._me),
                web.post("/api/chat", self._chat),
                web.get("/api/inbox", self._inbox),
            ]
        )

    @web.middleware
    async def _auth(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        login = request.headers.get(LOGIN_HEADER, "")
        if not login:
            return _error(401, "Open the app through Tailscale (tailscale serve); no Tailscale login was sent.")
        if login not in self.allowed:
            return _error(403, f"{login} is not in APP_ALLOWED_LOGINS on the server.")
        request[CHAT_KEY] = f"app:{login}"
        return await handler(request)

    async def _me(self, request: web.Request) -> web.Response:
        return web.json_response({"login": request[CHAT_KEY].removeprefix("app:")})

    async def _chat(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "Body must be JSON.")
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return _error(400, "'text' must be a non-empty string.")
        if len(text) > MAX_TEXT:
            return _error(400, f"Message is longer than {MAX_TEXT} characters.")
        chat_key = request[CHAT_KEY]
        reply = await self.agent.handle(chat_key, text.strip())
        return web.json_response({"reply": reply, "needs_confirmation": self.agent.awaiting_confirmation(chat_key)})

    async def _inbox(self, request: web.Request) -> web.Response:
        after = request.query.get("after", "0")
        if not after.isdigit():
            return _error(400, "'after' must be a message id.")
        rows = self.store.inbox_after(request[CHAT_KEY], int(after))
        return web.json_response({"messages": [{"id": i, "text": t, "at": at} for i, t, at in rows]})

    async def send(self, login: str, text: str) -> None:
        self.store.add_inbox(f"app:{login}", text)

    async def run(self) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.port).start()
        await asyncio.Event().wait()
