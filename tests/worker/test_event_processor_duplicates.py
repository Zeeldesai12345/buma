"""
EventProcessorService ↔ semantic duplicate detection (T3 / DD-25).

Everything here is mocked — the real pgvector queries are covered by
tests/integration/test_issue_embeddings_pg.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from buma.db.models import IssueSnapshot, RepoConfig, TriageDecision
from buma.schemas.normalized_event import IssueRef, NormalizedEvent, RepoRef
from buma.worker.services.assignee_selector import AssigneeSelector
from buma.worker.services.duplicate_detector import DuplicateDetector, SimilarIssue
from buma.worker.services.event_processor import EventProcessorService
from buma.worker.services.github_client import GitHubClient
from buma.worker.services.triage_engine import TriageResult

RECEIVED_AT = datetime(2024, 1, 1, tzinfo=UTC)
VECTOR = [0.1] * 384


def _event(action: str = "opened", number: int = 42) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=f"delivery-{action}-{number}",
        delivery_id=f"delivery-{action}-{number}",
        event_name="issues",
        action=action,
        received_at=RECEIVED_AT,
        installation_id=12345,
        repo=RepoRef(id=111, full_name="owner/repo", private=False),
        issue=IssueRef(
            number=number,
            id=999,
            node_id="I_node",
            url=f"https://api.github.com/repos/owner/repo/issues/{number}",
            html_url=f"https://github.com/owner/repo/issues/{number}",
            title="App crashes on login",
            body="NullPointerException when clicking sign in",
            labels=[],
            author_login="octocat",
            created_at=RECEIVED_AT,
            updated_at=RECEIVED_AT,
        ),
    )


def _result(category: str = "bug") -> TriageResult:
    return TriageResult(category=category, priority="P2", confidence=0.9, engine_version="rules-v1")


def _session(*execute_results: object) -> AsyncMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.add = MagicMock()
    if execute_results:
        session.execute = AsyncMock(side_effect=list(execute_results))
    else:
        repo_config = MagicMock(spec=RepoConfig)
        repo_config.config = {}
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=repo_config)))
    return session


def _detector(similar: list[SimilarIssue] | None = None, threshold: float = 0.8) -> DuplicateDetector:
    detector = DuplicateDetector(model_version="BAAI/bge-small-en-v1.5", threshold=threshold)
    detector.find_similar = AsyncMock(return_value=similar or [])
    detector.upsert = AsyncMock()
    detector.mark_closed = AsyncMock(return_value=True)
    return detector


def _processor(
    *,
    category: str = "bug",
    detector: DuplicateDetector | None = None,
    embed: AsyncMock | None = None,
    comment_enabled: bool = False,
    session: AsyncMock | None = None,
) -> tuple[EventProcessorService, AsyncMock, AsyncMock]:
    """Returns (processor, shared_session, github_client)."""
    session = session or _session()
    engine = MagicMock()
    engine.classify_with_fallback = AsyncMock(return_value=_result(category))
    selector = MagicMock(spec=AssigneeSelector)
    selector.select = AsyncMock(return_value="alice")
    github = AsyncMock(spec=GitHubClient)
    github.get_installation_token.return_value = "token"
    embedding_service = MagicMock()
    embedding_service.embed = embed or AsyncMock(return_value=VECTOR)
    processor = EventProcessorService(
        session_factory=MagicMock(return_value=session),
        triage_engine=engine,
        assignee_selector=selector,
        github_client=github,
        embedding_service=embedding_service,
        duplicate_detector=detector if detector is not None else _detector(),
        duplicate_comment_enabled=comment_enabled,
    )
    return processor, session, github


def _added(session: AsyncMock, model: type) -> list:
    return [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], model)]


MATCH_HIGH = SimilarIssue(issue_number=7, issue_state="open", similarity=0.93)
MATCH_CLOSED = SimilarIssue(issue_number=3, issue_state="closed", similarity=0.85)
MATCH_LOW = SimilarIssue(issue_number=9, issue_state="open", similarity=0.55)


# ---------------------------------------------------------------------------
# Phase 2b — indexing every opened issue
# ---------------------------------------------------------------------------


async def test_opened_issue_is_embedded_searched_and_upserted() -> None:
    detector = _detector([MATCH_HIGH])
    processor, session, _ = _processor(detector=detector)

    await processor.process(_event(number=42))

    processor._embedding_service.embed.assert_awaited_once_with(
        "App crashes on login", "NullPointerException when clicking sign in"
    )
    detector.find_similar.assert_awaited_once_with(session, 111, 42, VECTOR)
    detector.upsert.assert_awaited_once_with(session, 111, 42, VECTOR)


async def test_non_bug_is_embedded_but_gets_no_comment() -> None:
    detector = _detector([MATCH_HIGH])
    processor, session, github = _processor(category="question", detector=detector, comment_enabled=True)

    await processor.process(_event())

    detector.upsert.assert_awaited_once()
    github.post_comment.assert_not_called()
    assert _added(session, TriageDecision) == []


async def test_unenrolled_repo_is_not_embedded() -> None:
    no_repo = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    detector = _detector()
    processor, _, _ = _processor(detector=detector, session=_session(no_repo))

    await processor.process(_event())

    processor._embedding_service.embed.assert_not_called()
    detector.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# Failures never block triage
# ---------------------------------------------------------------------------


async def test_embedding_failure_does_not_stop_triage() -> None:
    detector = _detector([MATCH_HIGH])
    processor, session, github = _processor(
        detector=detector, embed=AsyncMock(side_effect=RuntimeError("onnxruntime exploded")), comment_enabled=True
    )

    await processor.process(_event())

    detector.upsert.assert_not_called()
    assert len(_added(session, IssueSnapshot)) == 1
    assert len(_added(session, TriageDecision)) == 1
    github.post_comment.assert_awaited_once()
    assert "Possible duplicate" not in github.post_comment.call_args.args[4]


async def test_vector_db_failure_does_not_stop_triage() -> None:
    detector = _detector()
    detector.find_similar = AsyncMock(side_effect=RuntimeError('extension "vector" is not available'))
    processor, session, github = _processor(detector=detector, comment_enabled=True)

    await processor.process(_event())

    assert len(_added(session, TriageDecision)) == 1
    github.patch_issue.assert_awaited_once()
    github.post_comment.assert_awaited_once()


async def test_upsert_failure_does_not_stop_triage() -> None:
    detector = _detector([MATCH_HIGH])
    detector.upsert = AsyncMock(side_effect=RuntimeError("deadlock"))
    processor, session, github = _processor(detector=detector, comment_enabled=True)

    await processor.process(_event())

    assert len(_added(session, TriageDecision)) == 1
    # The lookup result is discarded on failure: no half-indexed duplicate info is posted.
    assert "Possible duplicate" not in github.post_comment.call_args.args[4]


# ---------------------------------------------------------------------------
# Duplicate line in the ONE existing comment
# ---------------------------------------------------------------------------


async def test_duplicates_are_added_to_the_single_existing_comment() -> None:
    processor, session, github = _processor(detector=_detector([MATCH_HIGH, MATCH_CLOSED]), comment_enabled=True)

    await processor.process(_event())

    github.post_comment.assert_awaited_once()
    body = github.post_comment.call_args.args[4]
    assert body.startswith("🤖 **buma triage**")
    assert "- **Possible duplicate of:** #7 (open, similarity 0.93), #3 (closed, similarity 0.85)" in body
    assert "flagged only, not closed" in body
    [decision] = _added(session, TriageDecision)
    assert decision.explanation == body


async def test_matches_below_threshold_are_not_mentioned() -> None:
    processor, _, github = _processor(detector=_detector([MATCH_LOW], threshold=0.8), comment_enabled=True)

    await processor.process(_event())

    assert "Possible duplicate" not in github.post_comment.call_args.args[4]


async def test_feature_flag_off_keeps_comment_unchanged_but_still_indexes() -> None:
    detector = _detector([MATCH_HIGH])
    processor, _, github = _processor(detector=detector, comment_enabled=False)

    await processor.process(_event())

    detector.upsert.assert_awaited_once()
    github.post_comment.assert_awaited_once()
    assert "Possible duplicate" not in github.post_comment.call_args.args[4]


async def test_duplicate_detection_never_closes_or_edits_the_issue_state() -> None:
    processor, _, github = _processor(detector=_detector([MATCH_HIGH]), comment_enabled=True)

    await processor.process(_event())

    labels = github.patch_issue.call_args.args[4]
    assert "duplicate" not in labels
    assert "state" not in str(github.patch_issue.call_args)


async def test_comment_contains_no_text_from_the_matched_issue() -> None:
    # Only numbers/states/scores are posted — another issue's title/body is attacker-controlled.
    processor, _, github = _processor(detector=_detector([MATCH_HIGH]), comment_enabled=True)
    await processor.process(_event())
    duplicate_line = next(line for line in github.post_comment.call_args.args[4].splitlines() if "duplicate" in line)
    assert duplicate_line == "- **Possible duplicate of:** #7 (open, similarity 0.93) — flagged only, not closed"


# ---------------------------------------------------------------------------
# Closed issues
# ---------------------------------------------------------------------------


def _closed_session(decision: object | None) -> AsyncMock:
    repo_config = MagicMock(spec=RepoConfig)
    return _session(
        MagicMock(scalar_one_or_none=MagicMock(return_value=repo_config)),  # enrollment
        MagicMock(scalar_one_or_none=MagicMock(return_value=decision)),  # latest TriageDecision
    )


async def test_closed_issue_marks_embedding_closed_even_without_triage_decision() -> None:
    detector = _detector()
    processor, session, _ = _processor(detector=detector, session=_closed_session(decision=None))

    await processor.process(_event(action="closed", number=42))

    detector.mark_closed.assert_awaited_once_with(session, 111, 42)
    session.commit.assert_awaited()


async def test_mark_closed_failure_does_not_break_closed_handling() -> None:
    decision = MagicMock(spec=TriageDecision)
    decision.selected_assignee_login = None
    detector = _detector()
    detector.mark_closed = AsyncMock(side_effect=RuntimeError("db down"))
    processor, _, _ = _processor(detector=detector, session=_closed_session(decision=decision))

    await processor.process(_event(action="closed"))

    assert decision.closed_at is not None  # existing closed-path behaviour still ran


async def test_processor_without_duplicate_detection_is_unchanged() -> None:
    session = _session()
    processor = EventProcessorService(session_factory=MagicMock(return_value=session))
    processor._engine.classify_with_fallback = AsyncMock(return_value=_result())
    processor._selector.select = AsyncMock(return_value=None)

    await processor.process(_event())

    assert len(_added(session, TriageDecision)) == 1
