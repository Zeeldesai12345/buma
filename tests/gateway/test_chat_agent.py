from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest
from sqlalchemy.exc import OperationalError

from buma.gateway.chat import agent as agent_module
from buma.gateway.chat.agent import FALLBACK_BETA, ChatAgent
from buma.gateway.chat.tools import TOOL_DEFINITIONS, ToolContext, ToolInputError

# ---------------------------------------------------------------------------
# Fakes for client.beta.messages.stream(...)
# ---------------------------------------------------------------------------


def _text(text: str):
    return SimpleNamespace(type="text", text=text)


def _tool_start(name: str):
    return SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="tool_use", name=name))


def _tool_use(name: str, tool_input: object, tool_id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def _final(stop_reason: str, *content):
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), stop_details=None)


class Turn:
    """One scripted model turn: stream `events`, then return `final` (or raise `error`)."""

    def __init__(self, events=(), final=None, error: Exception | None = None, raise_during_stream: bool = False):
        self.events = list(events)
        self.final = final
        self.error = error
        self.raise_during_stream = raise_during_stream


class FakeStream:
    def __init__(self, turn: Turn) -> None:
        self._turn = turn

    async def __aenter__(self):
        if self._turn.error is not None and not self._turn.raise_during_stream:
            raise self._turn.error
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for event in self._turn.events:
            yield event
        if self._turn.error is not None and self._turn.raise_during_stream:
            raise self._turn.error

    async def get_final_message(self):
        return self._turn.final


class FakeClient:
    def __init__(self, turns: list[Turn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        # Snapshot messages: the agent keeps appending to the same list after the call.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self._turns.pop(0))


def _agent(client: FakeClient, max_tool_rounds: int = 6) -> ChatAgent:
    return ChatAgent(
        client=client, model="claude-test", effort="medium", max_tokens=1000, max_tool_rounds=max_tool_rounds
    )


def _ctx(seen: dict[int, str] | None = None) -> ToolContext:
    db = AsyncMock()
    return ToolContext(db=db, repo_id=42, embedder=None, seen_issues=dict(seen or {}))


async def _collect(agent: ChatAgent, ctx: ToolContext, question: str = "q", history=()) -> list[dict]:
    return [event async for event in agent.run(ctx, "acme/widgets", list(history), question)]


@pytest.fixture
def fake_tool(monkeypatch):
    """Replace execute_tool; records calls and marks #7 as seen (as a real search would)."""
    calls = []

    async def fake_execute(name, raw_input, ctx):
        calls.append((name, raw_input))
        ctx.seen_issues[7] = "Login crash"
        return '{"results": []}'

    monkeypatch.setattr(agent_module, "execute_tool", fake_execute)
    return calls


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_tool_round_then_answer_streams_events_in_order(fake_tool):
    client = FakeClient(
        [
            Turn(
                events=[_tool_start("search_issues")],
                final=_final("tool_use", _tool_use("search_issues", {"query": "login"})),
            ),
            Turn(events=[_text("See #7"), _text(" and #99.")], final=_final("end_turn")),
        ]
    )
    ctx = _ctx()
    events = await _collect(_agent(client), ctx)

    assert events == [
        {"type": "tool", "name": "search_issues"},
        {"type": "text", "text": "See #7"},
        {"type": "text", "text": " and #99."},
        # #99 was never returned by a tool, so it is not offered as a source.
        {"type": "sources", "issues": [{"issue_number": 7, "title": "Login crash"}]},
        {"type": "done"},
    ]
    assert fake_tool == [("search_issues", {"query": "login"})]


async def test_tool_results_are_sent_back_in_one_user_message(fake_tool):
    uses = [_tool_use("search_issues", {"query": "a"}, "tu_a"), _tool_use("get_workload", {}, "tu_b")]
    client = FakeClient([Turn(final=_final("tool_use", *uses)), Turn(final=_final("end_turn"))])
    await _collect(_agent(client), _ctx())

    second = client.calls[1]["messages"]
    assert second[-2] == {"role": "assistant", "content": uses}
    assert second[-1]["role"] == "user"
    assert [r["tool_use_id"] for r in second[-1]["content"]] == ["tu_a", "tu_b"]
    assert all(r["type"] == "tool_result" and "is_error" not in r for r in second[-1]["content"])


async def test_request_carries_tools_system_effort_and_refusal_fallback():
    client = FakeClient([Turn(final=_final("end_turn"))])
    await _collect(_agent(client), _ctx(), question="who is busiest?")

    call = client.calls[0]
    assert call["model"] == "claude-test"
    assert call["tools"] == TOOL_DEFINITIONS
    assert "acme/widgets" in call["system"]
    assert "untrusted_" in call["system"]
    assert call["output_config"] == {"effort": "medium"}
    assert call["betas"] == [FALLBACK_BETA]
    assert call["fallbacks"] == "default"
    assert call["messages"] == [{"role": "user", "content": "who is busiest?"}]


async def test_history_is_prepended_and_leading_assistant_turns_dropped():
    client = FakeClient([Turn(final=_final("end_turn"))])
    history = [
        {"role": "assistant", "content": "Hi! Ask me anything."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
    ]
    await _collect(_agent(client), _ctx(), question="second", history=history)

    assert client.calls[0]["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]


# ---------------------------------------------------------------------------
# Bounds and failures
# ---------------------------------------------------------------------------


async def test_round_limit_stops_the_loop(fake_tool):
    looping = [Turn(final=_final("tool_use", _tool_use("get_workload", {}))) for _ in range(5)]
    client = FakeClient(looping)
    events = await _collect(_agent(client, max_tool_rounds=2), _ctx())

    assert len(client.calls) == 2
    assert events[-2]["type"] == "error" and events[-2]["code"] == "round_limit"
    assert events[-1] == {"type": "done"}


async def test_api_error_yields_llm_unavailable():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient([Turn(error=anthropic.APIConnectionError(request=request))])
    events = await _collect(_agent(client), _ctx())

    assert events == [
        {"type": "error", "code": "llm_unavailable", "message": events[0]["message"]},
        {"type": "done"},
    ]


async def test_refusal_yields_refused_without_running_tools(fake_tool):
    client = FakeClient([Turn(final=_final("refusal", _tool_use("get_workload", {})))])
    events = await _collect(_agent(client), _ctx())

    assert events[0]["code"] == "refused"
    assert fake_tool == []


async def test_truncated_tool_input_is_never_executed(fake_tool):
    client = FakeClient([Turn(final=_final("max_tokens", _tool_use("search_issues", {"query": "lo"})))])
    events = await _collect(_agent(client), _ctx())

    assert events[0]["code"] == "truncated"
    assert fake_tool == []
    assert len(client.calls) == 1


async def test_unparseable_tool_json_is_retried_once():
    client = FakeClient(
        [
            Turn(error=ValueError("bad json"), raise_during_stream=True),
            Turn(events=[_text("ok")], final=_final("end_turn")),
        ]
    )
    events = await _collect(_agent(client, max_tool_rounds=1), _ctx())

    assert len(client.calls) == 2
    assert events == [{"type": "text", "text": "ok"}, {"type": "done"}]


async def test_invalid_tool_input_is_returned_to_the_model_as_error(monkeypatch):
    async def reject(name, raw_input, ctx):
        raise ToolInputError("Invalid input for search_issues: limit: too big")

    monkeypatch.setattr(agent_module, "execute_tool", reject)
    client = FakeClient(
        [
            Turn(final=_final("tool_use", _tool_use("search_issues", {"query": "x", "limit": 99}))),
            Turn(final=_final("end_turn")),
        ]
    )
    await _collect(_agent(client), _ctx())

    [result] = client.calls[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "limit" in result["content"]


async def test_database_error_is_reported_and_transaction_rolled_back(monkeypatch):
    async def db_down(name, raw_input, ctx):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(agent_module, "execute_tool", db_down)
    client = FakeClient([Turn(final=_final("tool_use", _tool_use("get_workload", {}))), Turn(final=_final("end_turn"))])
    ctx = _ctx()
    await _collect(_agent(client), ctx)

    [result] = client.calls[1]["messages"][-1]["content"]
    assert result["is_error"] is True
    assert "connection refused" not in result["content"]  # no internals leak to the model
    ctx.db.rollback.assert_awaited_once()
