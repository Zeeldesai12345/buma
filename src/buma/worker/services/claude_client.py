from __future__ import annotations

import logging

import anthropic

from buma.schemas.api.repo_config import VALID_CATEGORIES, VALID_PRIORITIES
from buma.schemas.normalized_event import IssueRef
from buma.worker.services.triage_engine import CLAUDE_ENGINE_VERSION, TriageResult

logger = logging.getLogger(__name__)

_TOOL_NAME = "classify_issue"

_SYSTEM_PROMPT = (
    "You triage GitHub issues for a software team. Read the issue and call the "
    f"{_TOOL_NAME} tool exactly once with your classification. Do not output any text "
    "outside the tool call."
)

_TOOL_SCHEMA: dict = {
    "name": _TOOL_NAME,
    "description": "Classify a GitHub issue into a category and a priority.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": sorted(VALID_CATEGORIES),
                "description": "The single best-fitting category for this issue.",
            },
            "priority": {
                "type": "string",
                "enum": sorted(VALID_PRIORITIES),
                "description": "P0 = critical/production-impacting, P3 = trivial/cosmetic.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Your confidence in this classification, from 0.0 to 1.0.",
            },
        },
        "required": ["category", "priority", "confidence"],
    },
}


class ClaudeClassifier:
    """
    Async fallback classifier used by TriageEngine.classify_with_fallback() when the
    rule-based result's confidence is below the configured threshold.

    Contract: classify() NEVER raises. Any network error, timeout, malformed response, or
    invalid category/priority/confidence value is logged and results in a `None` return, so
    callers can safely fall back to the rule-based result.
    """

    def __init__(self, api_key: str, model: str, timeout_seconds: float) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)
        self._model = model

    async def classify(self, issue: IssueRef) -> TriageResult | None:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._build_prompt(issue)}],
                tools=[_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
            )
        except anthropic.AnthropicError as exc:
            logger.warning("Claude classification request failed: %s", exc)
            return None
        except Exception:
            logger.exception("Unexpected error calling Claude — treating as classification failure")
            return None

        return self._parse_response(response)

    def _parse_response(self, response: anthropic.types.Message) -> TriageResult | None:
        tool_use = next((block for block in response.content if block.type == "tool_use"), None)
        if tool_use is None:
            logger.warning("Claude response contained no tool_use block")
            return None

        data = tool_use.input
        category = data.get("category")
        priority = data.get("priority")
        confidence = data.get("confidence")

        if category not in VALID_CATEGORIES:
            logger.warning("Claude returned invalid/unknown category: %r", category)
            return None
        if priority not in VALID_PRIORITIES:
            logger.warning("Claude returned invalid/unknown priority: %r", priority)
            return None
        if not isinstance(confidence, int | float) or isinstance(confidence, bool) or not (0.0 <= confidence <= 1.0):
            logger.warning("Claude returned invalid confidence: %r", confidence)
            return None

        return TriageResult(
            category=category,
            priority=priority,
            confidence=float(confidence),
            engine_version=CLAUDE_ENGINE_VERSION,
        )

    @staticmethod
    def _build_prompt(issue: IssueRef) -> str:
        labels = ", ".join(issue.labels) if issue.labels else "(none)"
        body = issue.body or "(no description provided)"
        return f"Title: {issue.title}\nLabels: {labels}\nBody:\n{body}"
