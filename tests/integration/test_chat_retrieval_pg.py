"""
Integration tests for the chat assistant's retrieval queries (DD-27) against a REAL Postgres + pgvector.

Opt-in: skipped unless BUMA_TEST_DATABASE_URL is set (see test_issue_embeddings_pg.py). Seeds a
throwaway schema, then reads it through the same READ-ONLY engine factory the chat runtime uses.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from buma.db.models import EMBEDDING_DIM, IssueEmbedding, IssueSnapshot, RepoConfig, TriageDecision
from buma.gateway.chat.tools import ToolContext, execute_tool
from buma.gateway.services import observability_queries as queries
from buma.mcp_server.db import create_readonly_engine, create_readonly_session_factory

DATABASE_URL = os.getenv("BUMA_TEST_DATABASE_URL")
REPO_A, REPO_B = 3001, 3002
MODEL = "BAAI/bge-small-en-v1.5"
T0 = datetime(2026, 9, 1, tzinfo=UTC)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="BUMA_TEST_DATABASE_URL not set (needs Postgres with pgvector)"),
]

TABLES = [RepoConfig.__table__, IssueSnapshot.__table__, TriageDecision.__table__, IssueEmbedding.__table__]


def _vec(angle_deg: float) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[0], v[1] = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return v


def _snapshot(repo_id: int, event_id: str, number: int, title: str, body: str, minutes: int) -> IssueSnapshot:
    return IssueSnapshot(
        event_id=event_id,
        delivery_id=event_id,
        repo_id=repo_id,
        issue_number=number,
        issue_id=number,
        issue_node_id=f"I_{number}",
        title=title,
        body=body,
        labels=["bug"],
        author_login="someone",
        issue_created_at=T0,
        issue_updated_at=T0,
        snapshot_at=T0 + timedelta(minutes=minutes),
    )


def _decision(repo_id: int, event_id: str, number: int, minutes: int, assignee: str) -> TriageDecision:
    return TriageDecision(
        event_id=event_id,
        delivery_id=event_id,
        repo_id=repo_id,
        issue_number=number,
        decided_at=T0 + timedelta(minutes=minutes),
        predicted_category="bug",
        predicted_priority="P2",
        confidence=0.7,
        selected_assignee_login=assignee,
        patch_state="APPLIED",
    )


@pytest.fixture
async def readonly_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = f"chat_test_{uuid.uuid4().hex[:10]}"
    search_path = f"-csearch_path={schema},public"
    admin = create_async_engine(DATABASE_URL)
    async with admin.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    writable = create_async_engine(DATABASE_URL, connect_args={"options": search_path})
    readonly = create_readonly_engine(DATABASE_URL, extra_options=search_path)
    try:
        async with writable.begin() as conn:
            await conn.run_sync(RepoConfig.metadata.create_all, tables=TABLES, checkfirst=False)

        seed = async_sessionmaker(writable, expire_on_commit=False)
        async with seed() as s:
            s.add_all(
                [
                    RepoConfig(repo_id=REPO_A, installation_id=1, repo_full_name="acme/alpha", config={}),
                    RepoConfig(repo_id=REPO_B, installation_id=2, repo_full_name="acme/beta", config={}),
                ]
            )
            await s.flush()
            s.add_all(
                [
                    # Issue 1 was edited: two snapshots, the later one must win.
                    _snapshot(REPO_A, "a-1-old", 1, "Old title", "old body", minutes=1),
                    _snapshot(REPO_A, "a-1-new", 1, "Login page crashes on Safari", "Stack trace attached", 5),
                    _snapshot(REPO_A, "a-2", 2, "Export to CSV is slow", "Takes minutes for big files", 2),
                    _snapshot(REPO_B, "b-1", 1, "Login crash in beta", "Other tenant", 3),
                    # Issue 1 re-triaged: the later decision must win.
                    _decision(REPO_A, "a-1-old", 1, minutes=1, assignee="alice"),
                    _decision(REPO_A, "a-1-new", 1, minutes=6, assignee="bob"),
                    _decision(REPO_B, "b-1", 1, minutes=3, assignee="carol"),
                    IssueEmbedding(repo_id=REPO_A, issue_number=1, embedding=_vec(0), model_version=MODEL),
                    IssueEmbedding(repo_id=REPO_A, issue_number=2, embedding=_vec(80), model_version=MODEL),
                    IssueEmbedding(repo_id=REPO_A, issue_number=3, embedding=_vec(1), model_version="other-model"),
                    IssueEmbedding(repo_id=REPO_B, issue_number=1, embedding=_vec(0), model_version=MODEL),
                ]
            )
            await s.commit()

        yield create_readonly_session_factory(readonly)
    finally:
        await readonly.dispose()
        await writable.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_embedding_search_is_repo_and_model_scoped(readonly_factory) -> None:
    async with readonly_factory() as db:
        hits = await queries.search_issues_by_embedding(db, REPO_A, MODEL, _vec(5), limit=10)

    assert [number for number, _, _ in hits] == [1, 2]  # no repo B rows, no other-model row
    assert hits[0][2] > hits[1][2]
    assert hits[0][2] == pytest.approx(math.cos(math.radians(5)), abs=1e-4)


async def test_keyword_search_matches_any_term_and_ranks(readonly_factory) -> None:
    async with readonly_factory() as db:
        both = await queries.search_issues_by_keyword(db, REPO_A, "why does the login page crash?", limit=10)
        none = await queries.search_issues_by_keyword(db, REPO_A, "!!! ???", limit=10)
        odd = await queries.search_issues_by_keyword(db, REPO_A, 'csv" OR 1=1 --', limit=10)

    assert both == [1]  # repo B's "Login crash in beta" never leaks in
    assert none == []
    assert odd == [2]


async def test_latest_snapshot_and_decision_per_issue(readonly_factory) -> None:
    async with readonly_factory() as db:
        snapshots = await queries.get_latest_snapshots(db, REPO_A, [1, 2, 99])
        decisions = await queries.get_latest_decisions(db, REPO_A, [1, 2])

    assert snapshots[1].title == "Login page crashes on Safari"
    assert set(snapshots) == {1, 2}
    assert decisions[1].selected_assignee_login == "bob"
    assert set(decisions) == {1}


async def test_tools_run_end_to_end_on_the_readonly_engine(readonly_factory) -> None:
    async with readonly_factory() as db:
        ctx = ToolContext(db=db, repo_id=REPO_A, embedder=None)
        result = await execute_tool("search_issues", {"query": "safari crash"}, ctx)
        issue = await execute_tool("get_issue", {"issue_number": 1}, ctx)

    assert '"search_mode": "keyword"' in result
    assert "Login page crashes on Safari" in result
    assert '"assignee": "bob"' in issue
    assert ctx.seen_issues == {1: "Login page crashes on Safari"}
