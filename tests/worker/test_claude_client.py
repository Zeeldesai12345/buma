from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest

from buma.schemas.normalized_event import IssueRef
from buma.worker.services.claude_client import ClaudeClassifier
from buma.worker.services.triage_engine import CLAUDE_ENGINE_VERSION

RECEIVED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _issue(
    title: str = "app crashes",
    body: str | None = "traceback attached",
    labels: list[str] | None = None,
) -> IssueRef:
    return IssueRef(
        number=1,
        id=1,
        node_id="I_node",
        url="https://api.github.com/repos/owner/repo/issues/1",
        html_url="https://github.com/owner/repo/issues/1",
        title=title,
        body=body,
        labels=labels or [],
        author_login="octocat",
        created_at=RECEIVED_AT,
        updated_at=RECEIVED_AT,
    )


def _tool_response(input_data: dict) -> SimpleNamespace:
    """Build a fake anthropic.types.Message with a single tool_use content block."""
    block = SimpleNamespace(type="tool_use", input=input_data)
    return SimpleNamespace(content=[block])


@pytest.fixture
def classifier() -> ClaudeClassifier:
    return ClaudeClassifier(api_key="sk-test-key", model="claude-haiku-4-5-20251001", timeout_seconds=5.0)


# ---------------------------------------------------------------------------
# Successful classification
# ---------------------------------------------------------------------------


async def test_successful_response_returns_triage_result(classifier: ClaudeClassifier) -> None:
    classifier._client.messages.create = AsyncMock(
        return_value=_tool_response({"category": "bug", "priority": "P1", "confidence": 0.85})
    )

    result = await classifier.classify(_issue())

    assert result is not None
    assert result.category == "bug"
    assert result.priority == "P1"
    assert result.confidence == pytest.approx(0.85)
    assert result.engine_version == CLAUDE_ENGINE_VERSION


async def test_request_forces_classify_issue_tool(classifier: ClaudeClassifier) -> None:
    mock_create = AsyncMock(return_value=_tool_response({"category": "docs", "priority": "P3", "confidence": 0.6}))
    classifier._client.messages.create = mock_create

    await classifier.classify(_issue())

    _, kwargs = mock_create.call_args
    assert kwargs["model"] == "claude-haiku-4-5-20251001"
    assert kwargs["tool_choice"] == {"type": "tool", "name": "classify_issue"}
    assert kwargs["tools"][0]["name"] == "classify_issue"


# ---------------------------------------------------------------------------
# Invalid Claude response — must be rejected, never raise
# ---------------------------------------------------------------------------


async def test_invalid_category_returns_none(classifier: ClaudeClassifier) -> None:
    classifier._client.messages.create = AsyncMock(
        return_value=_tool_response({"category": "not-a-real-category", "priority": "P1", "confidence": 0.9})
    )
    assert await classifier.classify(_issue()) is None


async def test_invalid_priority_returns_none(classifier: ClaudeClassifier) -> None:
    classifier._client.messages.create = AsyncMock(
        return_value=_tool_response({"category": "bug", "priority": "P9", "confidence": 0.9})
    )
    assert await classifier.classify(_issue()) is None


@pytest.mark.parametrize("bad_confidence", [-0.1, 1.5, "high", None, True])
async def test_invalid_confidence_returns_none(classifier: ClaudeClassifier, bad_confidence: object) -> None:
    classifier._client.messages.create = AsyncMock(
        return_value=_tool_response({"category": "bug", "priority": "P1", "confidence": bad_confidence})
    )
    assert await classifier.classify(_issue()) is None


async def test_missing_tool_use_block_returns_none(classifier: ClaudeClassifier) -> None:
    classifier._client.messages.create = AsyncMock(return_value=SimpleNamespace(content=[]))
    assert await classifier.classify(_issue()) is None


# ---------------------------------------------------------------------------
# Claude API failure — timeouts, connection errors, unexpected exceptions
# ---------------------------------------------------------------------------


async def test_api_connection_error_returns_none(classifier: ClaudeClassifier) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    classifier._client.messages.create = AsyncMock(side_effect=anthropic.APIConnectionError(request=request))
    assert await classifier.classify(_issue()) is None


async def test_api_timeout_error_returns_none(classifier: ClaudeClassifier) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    classifier._client.messages.create = AsyncMock(side_effect=anthropic.APITimeoutError(request=request))
    assert await classifier.classify(_issue()) is None


async def test_unexpected_exception_returns_none(classifier: ClaudeClassifier) -> None:
    classifier._client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
    assert await classifier.classify(_issue()) is None
