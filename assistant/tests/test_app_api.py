import asyncio
from collections.abc import Awaitable, Callable

from aiohttp.test_utils import TestClient, TestServer

from homebot.app_api import LOGIN_HEADER, AppChannel
from homebot.store import Store

ME = {LOGIN_HEADER: "me@example.com"}


class FakeAgent:
    def __init__(self) -> None:
        self.pending = False
        self.seen: list[tuple[str, str]] = []

    async def handle(self, chat_key: str, text: str) -> str:
        self.seen.append((chat_key, text))
        return f"echo {text}"

    def awaiting_confirmation(self, chat_key: str) -> bool:
        return self.pending


def _run(test: Callable[[TestClient, FakeAgent, AppChannel], Awaitable[None]]) -> None:
    agent = FakeAgent()
    channel = AppChannel(frozenset({"me@example.com"}), agent, Store(":memory:"), 0)  # type: ignore[arg-type]

    async def go() -> None:
        async with TestClient(TestServer(channel.app)) as client:
            await test(client, agent, channel)

    asyncio.run(go())


def test_requires_tailscale_login() -> None:
    async def test(client: TestClient, agent: FakeAgent, channel: AppChannel) -> None:
        assert (await client.get("/api/me")).status == 401
        assert (await client.get("/api/me", headers={LOGIN_HEADER: "stranger@example.com"})).status == 403
        r = await client.get("/api/me", headers=ME)
        assert r.status == 200 and (await r.json()) == {"login": "me@example.com"}

    _run(test)


def test_chat_validates_and_reports_confirmation() -> None:
    async def test(client: TestClient, agent: FakeAgent, channel: AppChannel) -> None:
        assert (await client.post("/api/chat", headers=ME, data="nope")).status == 400
        assert (await client.post("/api/chat", headers=ME, json={"text": "  "})).status == 400
        assert (await client.post("/api/chat", headers=ME, json={"text": "x" * 5000})).status == 400
        agent.pending = True
        r = await client.post("/api/chat", headers=ME, json={"text": " lights off "})
        assert (await r.json()) == {"reply": "echo lights off", "needs_confirmation": True}
        assert agent.seen == [("app:me@example.com", "lights off")]

    _run(test)


def test_inbox_returns_only_new_messages_for_this_user() -> None:
    async def test(client: TestClient, agent: FakeAgent, channel: AppChannel) -> None:
        await channel.send("me@example.com", "first")
        await channel.send("other@example.com", "not mine")
        await channel.send("me@example.com", "second")
        all_mine = (await (await client.get("/api/inbox", headers=ME)).json())["messages"]
        assert [m["text"] for m in all_mine] == ["first", "second"]
        newer = await (await client.get(f"/api/inbox?after={all_mine[0]['id']}", headers=ME)).json()
        assert [m["text"] for m in newer["messages"]] == ["second"]
        assert (await client.get("/api/inbox?after=-1", headers=ME)).status == 400

    _run(test)
