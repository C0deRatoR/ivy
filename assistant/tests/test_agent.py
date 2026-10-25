import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from homebot.agent import MAX_STEPS, Agent
from homebot.config import Config
from homebot.store import Store
from homebot.tools import Toolbox, ToolError


def _cfg(**overrides: str) -> Config:
    base = dict(
        llm_api_key="k", llm_base_url="http://llm", llm_models=("m",), timezone="UTC", db_path=":memory:",
        telegram_token="t", telegram_users=frozenset({1}), discord_token="", discord_users=frozenset(),
        app_logins=frozenset(), app_port=8787,
        ha_url="http://ha", ha_token="tok", jellyfin_url="http://jf", jellyfin_key="",
        adguard_url="http://ag", adguard_user="", adguard_password="",
    )
    return Config(**{**base, **overrides})  # type: ignore[arg-type]


def _tool_call(name: str, args: dict[str, object] | str, id: str = "c1") -> SimpleNamespace:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return SimpleNamespace(id=id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _reply(content: str | None = None, tool_calls: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda **_: {"role": "assistant", "content": content},
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")])


class FakeLLM:
    """Returns scripted replies (or raises scripted errors) and records what it was sent."""

    def __init__(self, replies: list[SimpleNamespace | Exception]) -> None:
        self.replies = replies
        self.sent: list[list[dict[str, object]]] = []
        self.models: list[object] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: object) -> SimpleNamespace:
        self.models.append(kwargs["model"])
        self.sent.append(list(kwargs["messages"]))  # type: ignore[call-overload]
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _agent(
    replies: list[SimpleNamespace | Exception],
    handler: httpx.MockTransport | None = None,
    models: tuple[str, ...] = ("m",),
) -> tuple[Agent, FakeLLM, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": "light.hall", "state": "on", "attributes": {}}])
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=handler or httpx.MockTransport(record))
    llm = FakeLLM(replies)
    agent = Agent(llm, models, Toolbox(_cfg(adguard_user="u"), http, Store(":memory:")), "UTC")  # type: ignore[arg-type]
    return agent, llm, requests


def test_read_only_tool_runs_without_confirmation() -> None:
    agent, llm, requests = _agent([_reply(tool_calls=[_tool_call("ha_list_entities", {})]), _reply("Hall light is on.")])
    assert asyncio.run(agent.handle("telegram:1", "what's on?")) == "Hall light is on."
    assert [r.url.path for r in requests] == ["/api/states"]
    tool_msg = llm.sent[1][-1]
    assert tool_msg["role"] == "tool" and "light.hall" in str(tool_msg["content"])


def test_lights_act_at_once_with_colour_and_brightness() -> None:
    call = _tool_call("ha_control", {"entity_id": "light.hall", "action": "turn_on", "color": "Light Blue", "brightness_pct": 40})
    agent, llm, requests = _agent([_reply(tool_calls=[call]), _reply("Done, it's light blue.")])
    assert asyncio.run(agent.handle("telegram:1", "make the hall light blue")) == "Done, it's light blue."
    [request] = requests
    assert request.url.path == "/api/services/light/turn_on"
    assert json.loads(request.content) == {"entity_id": "light.hall", "brightness_pct": 40, "color_name": "lightblue"}


def test_risky_tool_waits_for_yes() -> None:
    call = _tool_call("adguard_pause", {"minutes": 10})
    agent, llm, requests = _agent([_reply(tool_calls=[call]), _reply("Paused.")])

    async def chat() -> tuple[str, str]:
        return await agent.handle("telegram:1", "pause ads"), await agent.handle("telegram:1", "yes")

    ask, done = asyncio.run(chat())
    assert ask == "Should I do this?\n• Pause ad blocking for 10 minutes\nReply yes to confirm."
    assert done == "Paused."
    assert [r.url.path for r in requests] == ["/control/protection"]


def test_risky_tool_not_run_when_user_declines() -> None:
    call = _tool_call("adguard_pause", {"minutes": 10})
    agent, llm, requests = _agent([_reply(tool_calls=[call]), _reply("Okay, left it on.")])

    async def chat() -> str:
        await agent.handle("telegram:1", "pause ads")
        return await agent.handle("telegram:1", "no wait")

    assert asyncio.run(chat()) == "Okay, left it on."
    assert requests == []
    tool_msg, user_msg = llm.sent[1][-2], llm.sent[1][-1]
    assert "did not confirm" in str(tool_msg["content"])
    assert str(user_msg["content"]).endswith("no wait")


def test_step_limit_stops_runaway_loop() -> None:
    loop = [_reply(tool_calls=[_tool_call("list_notes", {}, id=f"c{i}")]) for i in range(MAX_STEPS)]
    agent, llm, _ = _agent(loop)
    assert "too many steps" in asyncio.run(agent.handle("telegram:1", "hi"))
    assert len(llm.sent) == MAX_STEPS


def test_bad_tool_arguments_become_tool_errors() -> None:
    agent, llm, _ = _agent([_reply(tool_calls=[_tool_call("add_note", "{not json")]), _reply("Sorry.")])
    asyncio.run(agent.handle("telegram:1", "note"))
    assert "not valid JSON" in str(llm.sent[1][-1]["content"])


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("ha_control", {"entity_id": "lock.front_door", "action": "turn_off"}, "can't be controlled"),
        ("ha_control", {"entity_id": "light.hall; rm", "action": "turn_off"}, "not a valid entity_id"),
        ("ha_control", {"entity_id": "switch.fan", "action": "turn_on", "color": "red"}, "only work with turn_on on a light"),
        ("ha_control", {"entity_id": "light.hall", "action": "turn_off", "brightness_pct": 50}, "only work with turn_on"),
        ("ha_control", {"entity_id": "light.hall", "action": "turn_on", "brightness_pct": 0}, "between 1 and 100"),
        ("ha_control", {"entity_id": "light.hall", "action": "turn_on", "color": "red", "kelvin": 3000}, "either color or kelvin"),
        ("ha_control", {"entity_id": "light.hall", "action": "turn_on", "color": "red;rm"}, "not a colour name"),
        ("ha_control", {"entity_id": "light.hall", "action": "turn_on", "kelvin": 9000}, "between 2000 and 6500"),
        ("adguard_pause", {"minutes": True}, "must be a int"),
        ("add_reminder", {"text": "x", "at": "2001-01-01T09:00"}, "in the past"),
        ("add_reminder", {"text": "x", "at": "tomorrow"}, "not an ISO 8601"),
        ("jellyfin_search", {"query": "x"}, "Jellyfin is not set up"),
    ],
)
def test_toolbox_rejects_bad_input(name: str, args: dict[str, object], message: str) -> None:
    toolbox = Toolbox(_cfg(), httpx.AsyncClient(), Store(":memory:"))
    with pytest.raises(ToolError, match=message):
        asyncio.run(toolbox.run(name, args, "telegram:1"))


def test_adguard_pause_range_checked() -> None:
    toolbox = Toolbox(_cfg(adguard_user="u"), httpx.AsyncClient(), Store(":memory:"))
    with pytest.raises(ToolError, match="between 1 and"):
        asyncio.run(toolbox.run("adguard_pause", {"minutes": 500}, "telegram:1"))


def test_reminders_are_due_once_marked_sent() -> None:
    store = Store(":memory:")
    store.add_reminder("telegram:1", "stretch", time.time() - 1)
    [(rid, chat_key, text)] = store.due_reminders(time.time())
    assert (chat_key, text) == ("telegram:1", "stretch")
    store.mark_sent(rid)
    assert store.due_reminders(time.time()) == []


def _overloaded() -> openai.InternalServerError:
    response = httpx.Response(503, request=httpx.Request("POST", "http://llm/chat/completions"))
    return openai.InternalServerError("model overloaded", response=response, body=None)


def test_overloaded_model_falls_back_to_the_next_one() -> None:
    agent, llm, _ = _agent([_overloaded(), _reply("Hi from the backup.")], models=("busy", "backup"))
    assert asyncio.run(agent.handle("telegram:1", "hi")) == "Hi from the backup."
    assert llm.models == ["busy", "backup"]


def test_all_models_overloaded_gives_a_clear_error() -> None:
    agent, llm, _ = _agent([_overloaded(), _overloaded()], models=("busy", "also-busy"))
    assert "AI provider returned an error (503)" in asyncio.run(agent.handle("telegram:1", "hi"))
    assert llm.models == ["busy", "also-busy"]


def test_bad_request_is_not_retried_on_other_models() -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "http://llm/chat/completions"))
    agent, llm, _ = _agent([openai.BadRequestError("bad", response=response, body=None)], models=("a", "b"))
    asyncio.run(agent.handle("telegram:1", "hi"))
    assert llm.models == ["a"]
