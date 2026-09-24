from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from buma.db.models import DLQRecord, IssueSnapshot, RepoConfig, TriageDecision
from buma.schemas.normalized_event import IssueRef, NormalizedEvent, RepoRef
from buma.worker.services.assignee_selector import AssigneeSelector
from buma.worker.services.event_processor import EventProcessorService, _build_explanation, _build_labels
from buma.worker.services.github_client import GitHubClient
from buma.worker.services.triage_engine import TriageResult

RECEIVED_AT = datetime(2024, 1, 1, tzinfo=UTC)


def _make_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_id="delivery-abc",
        delivery_id="delivery-abc",
        event_name="issues",
        action="opened",
        received_at=RECEIVED_AT,
        installation_id=12345,
        repo=RepoRef(id=111, full_name="owner/repo", private=False),
        issue=IssueRef(
            number=1,
            id=999,
            node_id="I_node",
            url="https://api.github.com/repos/owner/repo/issues/1",
            html_url="https://github.com/owner/repo/issues/1",
            title="Bug",
            body=None,
            labels=[],
            author_login="octocat",
            created_at=RECEIVED_AT,
            updated_at=RECEIVED_AT,
        ),
    )


def _make_session(repo_config: RepoConfig | None) -> AsyncMock:
    """Build a single mock AsyncSession that handles a RepoConfig query."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=repo_config)))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    # session.add is synchronous in SQLAlchemy; use MagicMock to avoid coroutine warnings
    session.add = MagicMock()
    return session


def _make_processor(repo_config: RepoConfig | None) -> EventProcessorService:
    """Build an EventProcessorService with a mocked session_factory (single shared session)."""
    mock_session = _make_session(repo_config)
    mock_factory = MagicMock(return_value=mock_session)
    return EventProcessorService(session_factory=mock_factory)


def _make_processor_phase5(
    assignee_login: str | None = "alice",
    raise_integrity_error: bool = False,
) -> tuple[EventProcessorService, AsyncMock]:
    """
    Build a processor wired for Phase 5 testing.
    Returns (processor, write_session) so tests can inspect session.add calls.

    Uses two separate mock sessions: read_session (Phase 2) and write_session (Phase 4+5).
    Injects a mock AssigneeSelector so candidate queries are bypassed.
    """
    # Phase 2 — read-only session
    repo_config = MagicMock(spec=RepoConfig)
    repo_config.config = {}
    read_session = _make_session(repo_config)

    # Phase 4+5 — write session
    write_session = AsyncMock()
    write_session.__aenter__ = AsyncMock(return_value=write_session)
    write_session.__aexit__ = AsyncMock(return_value=False)
    # session.add is synchronous in SQLAlchemy; use MagicMock to avoid coroutine warnings
    write_session.add = MagicMock()
    if raise_integrity_error:
        write_session.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("unique violation")))
        write_session.rollback = AsyncMock()

    # Factory returns read_session first, then write_session
    mock_factory = MagicMock(side_effect=[read_session, write_session])

    mock_selector = MagicMock(spec=AssigneeSelector)
    mock_selector.select = AsyncMock(return_value=assignee_login)

    processor = EventProcessorService(
        session_factory=mock_factory,
        assignee_selector=mock_selector,
    )
    return processor, write_session


# ---------------------------------------------------------------------------
# Existing pipeline gate tests
# ---------------------------------------------------------------------------


async def test_process_skips_when_repo_not_enrolled():
    processor = _make_processor(repo_config=None)
    # Should return early without raising
    await processor.process(_make_event())


async def test_process_continues_when_repo_enrolled():
    config = MagicMock(spec=RepoConfig)
    processor = _make_processor(repo_config=config)
    await processor.process(_make_event())


async def test_process_accepts_event_with_labels():
    config = MagicMock(spec=RepoConfig)
    processor = _make_processor(repo_config=config)
    event = _make_event()
    object.__setattr__(event.issue, "labels", ["bug", "p1"])
    await processor.process(event)


# ---------------------------------------------------------------------------
# Phase 5 — IssueSnapshot persistence
# ---------------------------------------------------------------------------


async def test_phase5_adds_issue_snapshot() -> None:
    processor, write_session = _make_processor_phase5()
    await processor.process(_make_event())

    added_types = [type(call.args[0]) for call in write_session.add.call_args_list]
    assert IssueSnapshot in added_types


async def test_phase5_issue_snapshot_fields() -> None:
    processor, write_session = _make_processor_phase5()
    event = _make_event()
    await processor.process(event)

    snapshots = [call.args[0] for call in write_session.add.call_args_list if isinstance(call.args[0], IssueSnapshot)]
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.event_id == event.event_id
    assert snap.delivery_id == event.delivery_id
    assert snap.repo_id == event.repo.id
    assert snap.issue_number == event.issue.number
    assert snap.issue_id == event.issue.id
    assert snap.issue_node_id == event.issue.node_id
    assert snap.title == event.issue.title
    assert snap.body == event.issue.body
    assert snap.labels == event.issue.labels
    assert snap.author_login == event.issue.author_login
    assert snap.issue_created_at == event.issue.created_at
    assert snap.issue_updated_at == event.issue.updated_at


# ---------------------------------------------------------------------------
# Phase 5 — TriageDecision persistence
# ---------------------------------------------------------------------------


async def test_phase5_adds_triage_decision() -> None:
    processor, write_session = _make_processor_phase5()
    await processor.process(_make_event())

    added_types = [type(call.args[0]) for call in write_session.add.call_args_list]
    assert TriageDecision in added_types


async def test_phase5_triage_decision_fields() -> None:
    processor, write_session = _make_processor_phase5(assignee_login="alice")
    event = _make_event()
    await processor.process(event)

    decisions = [call.args[0] for call in write_session.add.call_args_list if isinstance(call.args[0], TriageDecision)]
    assert len(decisions) == 1
    dec = decisions[0]
    assert dec.event_id == event.event_id
    assert dec.delivery_id == event.delivery_id
    assert dec.repo_id == event.repo.id
    assert dec.issue_number == event.issue.number
    assert dec.predicted_category == "bug"
    assert dec.predicted_priority is not None
    assert dec.confidence is not None
    assert dec.selected_assignee_login == "alice"
    assert dec.explanation is not None


async def test_phase5_triage_decision_no_assignee() -> None:
    processor, write_session = _make_processor_phase5(assignee_login=None)
    await processor.process(_make_event())

    decisions = [call.args[0] for call in write_session.add.call_args_list if isinstance(call.args[0], TriageDecision)]
    assert decisions[0].selected_assignee_login is None


async def test_phase5_commit_is_called() -> None:
    processor, write_session = _make_processor_phase5()
    await processor.process(_make_event())
    write_session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Phase 5 — explanation string
# ---------------------------------------------------------------------------


async def test_phase5_explanation_contains_assignee() -> None:
    processor, write_session = _make_processor_phase5(assignee_login="alice")
    await processor.process(_make_event())

    decisions = [call.args[0] for call in write_session.add.call_args_list if isinstance(call.args[0], TriageDecision)]
    assert "@alice" in decisions[0].explanation


async def test_phase5_explanation_no_assignee_text() -> None:
    processor, write_session = _make_processor_phase5(assignee_login=None)
    await processor.process(_make_event())

    decisions = [call.args[0] for call in write_session.add.call_args_list if isinstance(call.args[0], TriageDecision)]
    assert "no assignee found" in decisions[0].explanation


# ---------------------------------------------------------------------------
# Phase 5 — idempotency (duplicate event_id)
# ---------------------------------------------------------------------------


async def test_phase5_duplicate_event_is_silently_skipped() -> None:
    processor, write_session = _make_processor_phase5(raise_integrity_error=True)
    # Must not raise — duplicate is treated as a no-op
    await processor.process(_make_event())
    write_session.rollback.assert_awaited_once()


# ---------------------------------------------------------------------------
# _build_explanation unit tests
# ---------------------------------------------------------------------------


def _make_result(**kwargs) -> TriageResult:
    defaults = {
        "category": "bug",
        "priority": "P1",
        "confidence": 0.9,
        "engine_version": "rules-v1",
    }
    return TriageResult(**{**defaults, **kwargs})


def test_build_explanation_with_assignee() -> None:
    result = _make_result()
    text = _build_explanation(result, "alice")
    assert "@alice" in text
    assert "bug" in text
    assert "P1" in text
    assert "90%" in text
    assert "rules-v1" in text


def test_build_explanation_without_assignee() -> None:
    result = _make_result()
    text = _build_explanation(result, None)
    assert "no assignee found" in text
    assert "@" not in text


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (1.0, "100%"),
        (0.9, "90%"),
        (0.7, "70%"),
        (0.0, "0%"),
    ],
)
def test_build_explanation_confidence_formatting(confidence: float, expected: str) -> None:
    result = _make_result(confidence=confidence)
    text = _build_explanation(result, "alice")
    assert expected in text


def test_build_explanation_includes_note_when_set() -> None:
    note = "Priority capped at P1 — AI-suggested P0 needs a human to confirm."
    text = _build_explanation(_make_result(engine_version="claude-hybrid-v1", note=note), "alice")
    assert f"- **Note:** {note}" in text


def test_build_explanation_omits_note_when_unset() -> None:
    assert "Note" not in _build_explanation(_make_result(), "alice")


# ---------------------------------------------------------------------------
# Phase 3 — hybrid (Claude) classification wiring
# ---------------------------------------------------------------------------


def _make_processor_with_engine(result: TriageResult) -> tuple[EventProcessorService, MagicMock, AsyncMock]:
    """Processor whose TriageEngine is mocked to return `result`. Returns (processor, factory, engine)."""
    repo_config = MagicMock(spec=RepoConfig)
    repo_config.config = {}
    mock_factory = MagicMock(return_value=_make_session(repo_config))
    mock_engine = MagicMock()
    mock_engine.classify_with_fallback = AsyncMock(return_value=result)
    mock_selector = MagicMock(spec=AssigneeSelector)
    mock_selector.select = AsyncMock(return_value="alice")
    processor = EventProcessorService(
        session_factory=mock_factory, triage_engine=mock_engine, assignee_selector=mock_selector
    )
    return processor, mock_factory, mock_engine


async def test_classify_receives_repo_id_and_event_id() -> None:
    processor, _, mock_engine = _make_processor_with_engine(_make_result())
    event = _make_event()
    await processor.process(event)

    kwargs = mock_engine.classify_with_fallback.call_args.kwargs
    assert kwargs == {"repo_id": event.repo.id, "event_id": event.event_id}


@pytest.mark.parametrize("category", ["security", "docs", "feature", "question"])
async def test_claude_non_bug_category_skips_persistence(category: str) -> None:
    claude_result = _make_result(category=category, priority="P1", engine_version="claude-hybrid-v1")
    processor, mock_factory, _ = _make_processor_with_engine(claude_result)

    await processor.process(_make_event())

    # Only the Phase 2 read session is opened — no Phase 4/5 write session, nothing persisted.
    assert mock_factory.call_count == 1
    mock_factory.return_value.add.assert_not_called()
    mock_factory.return_value.commit.assert_not_called()


# ---------------------------------------------------------------------------
# _build_labels unit tests
# ---------------------------------------------------------------------------


def test_build_labels_appends_buma_labels() -> None:
    assert _build_labels([], "bug", "P1") == ["bug", "P1"]


def test_build_labels_preserves_existing() -> None:
    result = _build_labels(["frontend", "needs-triage"], "bug", "P1")
    assert result[:2] == ["frontend", "needs-triage"]
    assert "bug" in result
    assert "P1" in result


def test_build_labels_deduplicates() -> None:
    result = _build_labels(["bug", "P1"], "bug", "P1")
    assert result.count("bug") == 1
    assert result.count("P1") == 1


# ---------------------------------------------------------------------------
# Phase 6 helper
# ---------------------------------------------------------------------------


def _make_processor_phase6(
    assignee_login: str | None = "alice",
    patch_error: httpx.HTTPStatusError | None = None,
) -> tuple[EventProcessorService, AsyncMock, AsyncMock]:
    """
    Build a processor wired for Phase 6 testing.
    Returns (processor, write_session, patch_session) where:
      - write_session  = the Phase 4+5 commit session
      - patch_session  = the Phase 6 DB update session (APPLIED / FAILED_RETRY)
    Injects mock AssigneeSelector and GitHubClient.
    """
    # Phase 2 — read-only session
    repo_config = MagicMock(spec=RepoConfig)
    repo_config.config = {}
    read_session = _make_session(repo_config)

    # Phase 4+5 — write session
    write_session = AsyncMock()
    write_session.__aenter__ = AsyncMock(return_value=write_session)
    write_session.__aexit__ = AsyncMock(return_value=False)
    write_session.add = MagicMock()

    # Phase 6 — patch-state update session
    patch_session = AsyncMock()
    patch_session.__aenter__ = AsyncMock(return_value=patch_session)
    patch_session.__aexit__ = AsyncMock(return_value=False)
    patch_session.add = MagicMock()

    mock_factory = MagicMock(side_effect=[read_session, write_session, patch_session])

    mock_selector = MagicMock(spec=AssigneeSelector)
    mock_selector.select = AsyncMock(return_value=assignee_login)

    mock_github = MagicMock(spec=GitHubClient)
    mock_github.get_installation_token = AsyncMock(return_value="ghs_test_token")
    if patch_error:
        mock_github.patch_issue = AsyncMock(side_effect=patch_error)
    else:
        mock_github.patch_issue = AsyncMock()
    mock_github.post_comment = AsyncMock()

    processor = EventProcessorService(
        session_factory=mock_factory,
        assignee_selector=mock_selector,
        github_client=mock_github,
    )
    return processor, write_session, patch_session


def _make_http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "https://api.github.com/repos/o/r/issues/1")
    response = httpx.Response(status_code, request=request, text="error")
    return httpx.HTTPStatusError("error", request=request, response=response)


# ---------------------------------------------------------------------------
# Phase 6 — happy path
# ---------------------------------------------------------------------------


async def test_phase6_calls_get_installation_token() -> None:
    processor, _, _ = _make_processor_phase6()
    event = _make_event()
    await processor.process(event)
    processor._github_client.get_installation_token.assert_awaited_once_with(event.installation_id)


async def test_phase6_calls_patch_issue_with_labels_and_assignee() -> None:
    processor, _, _ = _make_processor_phase6(assignee_login="alice")
    event = _make_event()
    await processor.process(event)
    processor._github_client.patch_issue.assert_awaited_once()
    _, _, _, _, labels, assignee = processor._github_client.patch_issue.call_args.args
    assert "bug" in labels
    assert assignee == "alice"


async def test_phase6_calls_post_comment() -> None:
    processor, _, _ = _make_processor_phase6()
    await processor.process(_make_event())
    processor._github_client.post_comment.assert_awaited_once()


async def test_phase6_marks_patch_applied() -> None:
    processor, _, patch_session = _make_processor_phase6()
    await processor.process(_make_event())
    patch_session.execute.assert_awaited_once()
    patch_session.commit.assert_awaited_once()


async def test_phase6_skipped_when_github_client_is_none() -> None:
    processor, _, _ = _make_processor_phase6()
    processor._github_client = None
    # Should complete without raising
    await processor.process(_make_event())


# ---------------------------------------------------------------------------
# Phase 6 — error handling
# ---------------------------------------------------------------------------


async def test_phase6_transient_error_sets_failed_retry() -> None:
    error = _make_http_error(429)
    processor, _, patch_session = _make_processor_phase6(patch_error=error)
    await processor.process(_make_event())
    # patch_session used for FAILED_RETRY update
    patch_session.execute.assert_awaited_once()
    patch_session.commit.assert_awaited_once()


async def test_phase6_transient_error_does_not_write_dlq() -> None:
    error = _make_http_error(503)
    processor, _, patch_session = _make_processor_phase6(patch_error=error)
    await processor.process(_make_event())
    added_types = [type(call.args[0]) for call in patch_session.add.call_args_list]
    assert DLQRecord not in added_types


async def test_phase6_non_retryable_error_writes_dlq() -> None:
    error = _make_http_error(422)
    processor, _, patch_session = _make_processor_phase6(patch_error=error)
    await processor.process(_make_event())
    added_types = [type(call.args[0]) for call in patch_session.add.call_args_list]
    assert DLQRecord in added_types


async def test_phase6_server_error_does_not_write_dlq() -> None:
    error = _make_http_error(500)
    processor, _, patch_session = _make_processor_phase6(patch_error=error)
    await processor.process(_make_event())
    added_types = [type(call.args[0]) for call in patch_session.add.call_args_list]
    assert DLQRecord not in added_types


# ---------------------------------------------------------------------------
# _handle_closed helpers
# ---------------------------------------------------------------------------


def _make_closed_event() -> NormalizedEvent:
    return NormalizedEvent(
        event_id="delivery-close-xyz",
        delivery_id="delivery-close-xyz",
        event_name="issues",
        action="closed",
        received_at=RECEIVED_AT,
        installation_id=12345,
        repo=RepoRef(id=111, full_name="owner/repo", private=False),
        issue=IssueRef(
            number=1,
            id=999,
            node_id="I_node",
            url="https://api.github.com/repos/owner/repo/issues/1",
            html_url="https://github.com/owner/repo/issues/1",
            title="Bug",
            body=None,
            labels=[],
            author_login="octocat",
            created_at=RECEIVED_AT,
            updated_at=RECEIVED_AT,
        ),
    )


def _make_processor_handle_closed(
    repo_config_result: RepoConfig | None,
    decision_result: TriageDecision | None,
) -> tuple[EventProcessorService, AsyncMock]:
    """
    Build a processor for _handle_closed tests.
    Session 1 handles the RepoConfig lookup.
    Session 2 handles the TriageDecision select + optional UPDATE + commit.
    Returns (processor, session2) so tests can assert on session2.
    """
    session1 = AsyncMock()
    session1.__aenter__ = AsyncMock(return_value=session1)
    session1.__aexit__ = AsyncMock(return_value=False)
    session1.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=repo_config_result)))

    session2 = AsyncMock()
    session2.__aenter__ = AsyncMock(return_value=session2)
    session2.__aexit__ = AsyncMock(return_value=False)
    # First execute: TriageDecision select. Second execute: UPDATE (may not be called).
    decision_result_mock = MagicMock(scalar_one_or_none=MagicMock(return_value=decision_result))
    session2.execute = AsyncMock(side_effect=[decision_result_mock, AsyncMock()])

    mock_factory = MagicMock(side_effect=[session1, session2])
    processor = EventProcessorService(session_factory=mock_factory)
    return processor, session2


# ---------------------------------------------------------------------------
# _handle_closed tests
# ---------------------------------------------------------------------------


async def test_handle_closed_skips_unenrolled_repo() -> None:
    processor, session2 = _make_processor_handle_closed(repo_config_result=None, decision_result=None)
    # Must not raise; session2 should never be opened
    await processor.process(_make_closed_event())
    session2.__aenter__.assert_not_called()


async def test_handle_closed_skips_unknown_issue() -> None:
    config = MagicMock(spec=RepoConfig)
    processor, session2 = _make_processor_handle_closed(repo_config_result=config, decision_result=None)
    await processor.process(_make_closed_event())
    # Session opened but commit must NOT be called — nothing to update
    session2.commit.assert_not_awaited()


async def test_handle_closed_sets_closed_at() -> None:
    config = MagicMock(spec=RepoConfig)
    decision = MagicMock(spec=TriageDecision)
    decision.selected_assignee_login = "alice"
    decision.closed_at = None

    processor, _ = _make_processor_handle_closed(repo_config_result=config, decision_result=decision)

    fixed_now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
    with patch("buma.worker.services.event_processor.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_now
        await processor.process(_make_closed_event())

    assert decision.closed_at == fixed_now


async def test_handle_closed_executes_update_for_assignee() -> None:
    config = MagicMock(spec=RepoConfig)
    decision = MagicMock(spec=TriageDecision)
    decision.selected_assignee_login = "alice"
    decision.closed_at = None

    processor, session2 = _make_processor_handle_closed(repo_config_result=config, decision_result=decision)
    await processor.process(_make_closed_event())

    # Two execute calls: decision SELECT + open_assignments UPDATE
    assert session2.execute.await_count == 2


async def test_handle_closed_skips_update_when_no_assignee() -> None:
    config = MagicMock(spec=RepoConfig)
    decision = MagicMock(spec=TriageDecision)
    decision.selected_assignee_login = None
    decision.closed_at = None

    processor, session2 = _make_processor_handle_closed(repo_config_result=config, decision_result=decision)
    await processor.process(_make_closed_event())

    # Only one execute call: decision SELECT; no UPDATE when no assignee
    assert session2.execute.await_count == 1


async def test_handle_closed_commits_when_decision_found() -> None:
    config = MagicMock(spec=RepoConfig)
    decision = MagicMock(spec=TriageDecision)
    decision.selected_assignee_login = "alice"
    decision.closed_at = None

    processor, session2 = _make_processor_handle_closed(repo_config_result=config, decision_result=decision)
    await processor.process(_make_closed_event())

    session2.commit.assert_awaited_once()
