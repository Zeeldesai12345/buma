"""
Unit tests for DuplicateDetector: threshold filtering and the SHAPE of the SQL it sends.
Real pgvector behaviour (ordering, scoping, upsert) is in tests/integration/test_issue_embeddings_pg.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from buma.worker.services.duplicate_detector import DuplicateDetector, SimilarIssue

MODEL = "BAAI/bge-small-en-v1.5"


def _sql(session: AsyncMock) -> str:
    statement = session.execute.call_args.args[0]
    return str(statement.compile(dialect=postgresql.dialect()))


def _session(rows: list | None = None) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=rows or [])
    return session


def test_filter_duplicates_keeps_matches_at_or_above_threshold() -> None:
    detector = DuplicateDetector(model_version=MODEL, threshold=0.85)
    matches = [
        SimilarIssue(1, "open", 0.95),
        SimilarIssue(2, "closed", 0.85),
        SimilarIssue(3, "open", 0.849),
    ]
    assert [m.issue_number for m in detector.filter_duplicates(matches)] == [1, 2]


async def test_find_similar_is_scoped_to_repo_model_and_excludes_self() -> None:
    session = _session([MagicMock(issue_number=7, issue_state="open", similarity=0.9)])
    detector = DuplicateDetector(model_version=MODEL, top_k=5)

    result = await detector.find_similar(session, repo_id=111, issue_number=42, embedding=[0.1] * 384)

    sql = _sql(session)
    assert "issue_embeddings.repo_id = %(repo_id_1)s" in sql
    assert "issue_embeddings.model_version = %(model_version_1)s" in sql
    assert "issue_embeddings.issue_number != %(issue_number_1)s" in sql
    assert "<=>" in sql  # pgvector cosine distance operator
    assert "ORDER BY issue_embeddings.embedding <=> " in sql
    assert "LIMIT" in sql
    params = session.execute.call_args.args[0].compile(dialect=postgresql.dialect()).params
    assert params["repo_id_1"] == 111
    assert params["model_version_1"] == MODEL
    assert params["issue_number_1"] == 42
    assert params["param_2"] == 5
    assert result == [SimilarIssue(7, "open", 0.9)]


async def test_find_similar_k_override() -> None:
    session = _session()
    await DuplicateDetector(model_version=MODEL, top_k=5).find_similar(session, 1, 2, [0.0] * 384, k=10)
    assert session.execute.call_args.args[0].compile(dialect=postgresql.dialect()).params["param_2"] == 10


async def test_upsert_is_insert_on_conflict_update() -> None:
    session = _session()
    await DuplicateDetector(model_version=MODEL).upsert(session, 111, 42, [0.1] * 384)
    sql = _sql(session)
    assert sql.startswith("INSERT INTO issue_embeddings")
    assert "ON CONFLICT (repo_id, issue_number) DO UPDATE" in sql
    assert "model_version = excluded.model_version" in sql
    assert "updated_at = now()" in sql


async def test_mark_closed_updates_only_that_issue() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    assert await DuplicateDetector(model_version=MODEL).mark_closed(session, 111, 42) is True
    sql = _sql(session)
    assert sql.startswith("UPDATE issue_embeddings SET issue_state=")
    assert "issue_embeddings.repo_id = " in sql and "issue_embeddings.issue_number = " in sql


async def test_mark_closed_reports_missing_row() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    assert await DuplicateDetector(model_version=MODEL).mark_closed(session, 111, 42) is False
