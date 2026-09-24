from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from buma.schemas.normalized_event import IssueRef
from buma.worker.services.triage_engine import (
    CLAUDE_ENGINE_VERSION,
    ENGINE_VERSION,
    FALLBACK_ENGINE_VERSION,
    TriageEngine,
    TriageResult,
)

RECEIVED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _issue(
    title: str = "",
    body: str | None = None,
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


@pytest.fixture
def engine() -> TriageEngine:
    return TriageEngine()


# ---------------------------------------------------------------------------
# Category — label matching
# ---------------------------------------------------------------------------


def test_label_match_bug(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="some issue", labels=["bug"]), config={})
    assert result.category == "bug"
    assert result.confidence == 1.0


def test_label_match_defect_maps_to_bug(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="some issue", labels=["defect"]), config={})
    assert result.category == "bug"
    assert result.confidence == 1.0


def test_label_match_feature(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="some issue", labels=["enhancement"]), config={})
    assert result.category == "feature"


def test_label_match_is_case_insensitive(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="some issue", labels=["BUG"]), config={})
    assert result.category == "bug"
    assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# Category — keyword matching
# ---------------------------------------------------------------------------


def test_strong_keyword_in_title(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="app crashes on submit"), config={})
    assert result.category == "bug"
    assert result.confidence == 0.9


def test_strong_keyword_in_body(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="issue report", body="getting a traceback"), config={})
    assert result.category == "bug"
    assert result.confidence == 0.9


def test_medium_keyword_category(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="response is slow under load"), config={})
    assert result.category == "bug"
    assert result.confidence == 0.7


def test_keyword_category_feature(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="add dark mode support"), config={})
    assert result.category == "feature"
    assert result.confidence == 0.7


def test_keyword_category_question(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="how do I reset my password"), config={})
    assert result.category == "question"


def test_keyword_category_security(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="SQL injection vulnerability found"), config={})
    assert result.category == "security"


def test_keyword_category_docs(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="typo in readme"), config={})
    assert result.category == "docs"


# ---------------------------------------------------------------------------
# Category — fallback
# ---------------------------------------------------------------------------


def test_fallback_category_default(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="something happened"), config={})
    assert result.category == "bug"
    assert result.confidence == 0.0


def test_fallback_category_from_repo_config(engine: TriageEngine) -> None:
    config = {"defaults": {"category": "question"}}
    result = engine.classify(_issue(title="something happened"), config=config)
    assert result.category == "question"
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Priority — label matching
# ---------------------------------------------------------------------------


def test_label_match_priority_p0(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="issue", labels=["critical"]), config={})
    assert result.priority == "P0"
    assert result.confidence == 1.0


def test_label_match_priority_p3(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="issue", labels=["trivial"]), config={})
    assert result.priority == "P3"


def test_label_match_priority_case_insensitive(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="issue", labels=["HIGH"]), config={})
    assert result.priority == "P1"


# ---------------------------------------------------------------------------
# Priority — keyword matching
# ---------------------------------------------------------------------------


def test_priority_keyword_p0(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="production down, users cannot login"), config={})
    assert result.priority == "P0"
    assert result.confidence == 0.9


def test_priority_keyword_p1(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="login page crashes on submit"), config={})
    assert result.priority == "P1"
    assert result.confidence == 0.9


def test_priority_keyword_p2(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="dashboard is slow and intermittent"), config={})
    assert result.priority == "P2"
    assert result.confidence == 0.7


def test_priority_keyword_p3(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="cosmetic issue with button alignment"), config={})
    assert result.priority == "P3"
    assert result.confidence == 0.7


def test_priority_highest_wins(engine: TriageEngine) -> None:
    # Title contains both P1 ("crash") and P2 ("slow") signals
    result = engine.classify(_issue(title="app crashes and is also slow"), config={})
    assert result.priority == "P1"


def test_priority_p0_beats_p2(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="intermittent outage causing data loss"), config={})
    assert result.priority == "P0"


# ---------------------------------------------------------------------------
# Priority — fallback
# ---------------------------------------------------------------------------


def test_fallback_priority_default(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="something happened"), config={})
    assert result.priority == "P2"
    assert result.confidence == 0.0


def test_fallback_priority_from_repo_config(engine: TriageEngine) -> None:
    config = {"defaults": {"priority": "P3"}}
    result = engine.classify(_issue(title="something happened"), config=config)
    assert result.priority == "P3"
    assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Per-repo config overrides
# ---------------------------------------------------------------------------


def test_repo_config_category_label_override(engine: TriageEngine) -> None:
    config = {"label_map": {"categories": {"type: defect": "bug"}}}
    result = engine.classify(_issue(title="issue", labels=["type: defect"]), config=config)
    assert result.category == "bug"
    assert result.confidence == 1.0


def test_repo_config_priority_label_override(engine: TriageEngine) -> None:
    config = {"label_map": {"priorities": {"blocker": "P0"}}}
    result = engine.classify(_issue(title="issue", labels=["blocker"]), config=config)
    assert result.priority == "P0"
    assert result.confidence == 1.0


def test_repo_config_empty_uses_global_defaults(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="app crashes"), config={})
    assert result.category == "bug"
    assert result.priority == "P1"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_none_body_does_not_raise(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="app crashes", body=None), config={})
    assert isinstance(result, TriageResult)
    assert result.category == "bug"


def test_empty_labels_and_title_uses_fallback(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="", labels=[]), config={})
    assert result.category == "bug"
    assert result.priority == "P2"
    assert result.confidence == 0.0


def test_overall_confidence_is_min_of_fired_signals(engine: TriageEngine) -> None:
    # Label match on category (1.0) + keyword match on priority P2 (0.7) → overall min = 0.7
    result = engine.classify(_issue(title="slow response", labels=["bug"]), config={})
    assert result.confidence == pytest.approx(0.7)


def test_overall_confidence_ignores_fallback_zero(engine: TriageEngine) -> None:
    # Label match on category (1.0) + no priority signal (fallback 0.0) → overall 1.0
    result = engine.classify(_issue(title="some issue", labels=["bug"]), config={})
    assert result.confidence == pytest.approx(1.0)


def test_engine_version_in_result(engine: TriageEngine) -> None:
    result = engine.classify(_issue(title="crash"), config={})
    assert result.engine_version == ENGINE_VERSION


# ---------------------------------------------------------------------------
# Hybrid Claude fallback — classify_with_fallback()
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_claude() -> AsyncMock:
    return AsyncMock()


def _claude_result(confidence: float = 0.8) -> TriageResult:
    return TriageResult(category="security", priority="P0", confidence=confidence, engine_version=CLAUDE_ENGINE_VERSION)


async def test_no_classifier_configured_behaves_like_rules_only() -> None:
    engine = TriageEngine(claude_classifier=None)
    result = await engine.classify_with_fallback(_issue(title="something happened"), config={})
    assert result.engine_version == ENGINE_VERSION
    assert result.confidence == 0.0


async def test_high_confidence_skips_claude(mock_claude: AsyncMock) -> None:
    engine = TriageEngine(claude_classifier=mock_claude, confidence_threshold=0.5)
    result = await engine.classify_with_fallback(_issue(title="issue", labels=["bug"]), config={})

    mock_claude.classify.assert_not_called()
    assert result.engine_version == ENGINE_VERSION
    assert result.category == "bug"
    assert result.confidence == 1.0


async def test_low_confidence_triggers_claude_call(mock_claude: AsyncMock) -> None:
    mock_claude.classify.return_value = _claude_result()
    engine = TriageEngine(claude_classifier=mock_claude, confidence_threshold=0.5)

    issue = _issue(title="something happened")  # confidence 0.0 -> below threshold
    await engine.classify_with_fallback(issue, config={})

    mock_claude.classify.assert_called_once_with(issue)


async def test_successful_claude_response_is_used(mock_claude: AsyncMock) -> None:
    mock_claude.classify.return_value = _claude_result(confidence=0.8)
    engine = TriageEngine(claude_classifier=mock_claude, confidence_threshold=0.5)

    result = await engine.classify_with_fallback(_issue(title="something happened"), config={})

    assert result.category == "security"
    assert result.priority == "P0"
    assert result.confidence == 0.8
    assert result.engine_version == CLAUDE_ENGINE_VERSION


async def test_invalid_claude_response_falls_back_to_rules(mock_claude: AsyncMock) -> None:
    mock_claude.classify.return_value = None  # ClaudeClassifier's own contract on invalid data
    engine = TriageEngine(claude_classifier=mock_claude, confidence_threshold=0.5)

    result = await engine.classify_with_fallback(_issue(title="something happened"), config={})

    assert result.category == "bug"  # rule fallback default
    assert result.priority == "P2"
    assert result.engine_version == FALLBACK_ENGINE_VERSION


async def test_claude_api_failure_falls_back_to_rules(mock_claude: AsyncMock) -> None:
    mock_claude.classify.side_effect = RuntimeError("Claude API unavailable")
    engine = TriageEngine(claude_classifier=mock_claude, confidence_threshold=0.5)

    result = await engine.classify_with_fallback(_issue(title="something happened"), config={})

    assert result.category == "bug"
    assert result.engine_version == FALLBACK_ENGINE_VERSION


async def test_claude_fallback_preserves_rule_confidence_on_failure(mock_claude: AsyncMock) -> None:
    mock_claude.classify.return_value = None
    engine = TriageEngine(claude_classifier=mock_claude, confidence_threshold=0.9)

    # Medium-confidence keyword match (0.7) is still below a 0.9 threshold.
    result = await engine.classify_with_fallback(_issue(title="response is slow under load"), config={})

    assert result.confidence == pytest.approx(0.7)
    assert result.engine_version == FALLBACK_ENGINE_VERSION
