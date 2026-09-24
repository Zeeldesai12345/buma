"""
Integration tests for DuplicateDetector against a REAL Postgres + pgvector database.

Opt-in: skipped unless BUMA_TEST_DATABASE_URL is set, e.g. against the Docker db service:

    BUMA_TEST_DATABASE_URL=postgresql+psycopg://buma:buma@localhost:5433/buma \
        uv run pytest -m integration tests/integration

Each test creates a throwaway schema (tables created from the ORM models) and drops it afterwards,
so it never touches the application's own tables.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from buma.db.models import IssueEmbedding, RepoConfig
from buma.worker.services.duplicate_detector import DuplicateDetector

DATABASE_URL = os.getenv("BUMA_TEST_DATABASE_URL")
MODEL = "BAAI/bge-small-en-v1.5"
REPO_A, REPO_B = 1001, 1002

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="BUMA_TEST_DATABASE_URL not set (needs Postgres with pgvector)"),
]


def _vec(angle_deg: float) -> list[float]:
    """384-dim unit vector at `angle_deg` from the x-axis in the (x, y) plane: cosine similarity is easy to predict."""
    v = [0.0] * 384
    v[0] = math.cos(math.radians(angle_deg))
    v[1] = math.sin(math.radians(angle_deg))
    return v


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    schema = f"t3_test_{uuid.uuid4().hex[:10]}"
    admin = create_async_engine(DATABASE_URL)
    async with admin.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    # Unqualified table names resolve to the test schema; the vector type still resolves from public.
    engine = create_async_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema},public"})
    try:
        async with engine.begin() as conn:
            # checkfirst=False is essential: the app's own tables in `public` are visible through the
            # search_path, so the default existence check would skip creating the test copies and
            # the tests would then write into the real application tables.
            await conn.run_sync(
                IssueEmbedding.metadata.create_all,
                tables=[RepoConfig.__table__, IssueEmbedding.__table__],
                checkfirst=False,
            )
            tables = await conn.execute(
                text("SELECT table_schema FROM information_schema.tables WHERE table_name = 'issue_embeddings'")
            )
            assert schema in set(tables.scalars()), "test tables were not created in the throwaway schema"
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            for repo_id in (REPO_A, REPO_B):
                session.add(RepoConfig(repo_id=repo_id, installation_id=1, repo_full_name=f"o/r{repo_id}", config={}))
            await session.commit()
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def _seed(factory, detector: DuplicateDetector, rows: list[tuple[int, int, list[float]]]) -> None:
    async with factory() as session:
        for repo_id, number, vector in rows:
            await detector.upsert(session, repo_id, number, vector)
        await session.commit()


async def _find(factory, detector, repo_id, number, vector, k=5):
    async with factory() as session:
        return await detector.find_similar(session, repo_id, number, vector, k=k)


async def test_search_is_scoped_to_the_same_repository(session_factory) -> None:
    detector = DuplicateDetector(model_version=MODEL)
    # Repo B holds an IDENTICAL vector; repo A only a distant one.
    await _seed(session_factory, detector, [(REPO_B, 7, _vec(0)), (REPO_A, 8, _vec(60))])

    results = await _find(session_factory, detector, REPO_A, 99, _vec(0))

    assert [r.issue_number for r in results] == [8]


async def test_search_excludes_the_issue_itself(session_factory) -> None:
    detector = DuplicateDetector(model_version=MODEL)
    await _seed(session_factory, detector, [(REPO_A, 42, _vec(0)), (REPO_A, 43, _vec(30))])

    results = await _find(session_factory, detector, REPO_A, 42, _vec(0))

    assert [r.issue_number for r in results] == [43]


async def test_results_are_ordered_by_cosine_similarity(session_factory) -> None:
    detector = DuplicateDetector(model_version=MODEL)
    await _seed(
        session_factory,
        detector,
        [(REPO_A, 1, _vec(80)), (REPO_A, 2, _vec(10)), (REPO_A, 3, _vec(45)), (REPO_A, 4, _vec(170))],
    )

    results = await _find(session_factory, detector, REPO_A, 99, _vec(0), k=3)

    assert [r.issue_number for r in results] == [2, 3, 1]  # top-k: #4 (170°) is cut off
    for result, angle in zip(results, (10, 45, 80), strict=True):
        assert result.similarity == pytest.approx(math.cos(math.radians(angle)), abs=1e-5)


async def test_cosine_ignores_vector_length(session_factory) -> None:
    detector = DuplicateDetector(model_version=MODEL)
    await _seed(session_factory, detector, [(REPO_A, 1, [x * 5 for x in _vec(0)]), (REPO_A, 2, _vec(20))])

    [best, _] = await _find(session_factory, detector, REPO_A, 99, _vec(0))

    assert best.issue_number == 1
    assert best.similarity == pytest.approx(1.0, abs=1e-5)


async def test_other_model_versions_are_ignored(session_factory) -> None:
    current = DuplicateDetector(model_version=MODEL)
    old = DuplicateDetector(model_version="sentence-transformers/all-MiniLM-L6-v2")
    await _seed(session_factory, old, [(REPO_A, 1, _vec(0))])
    await _seed(session_factory, current, [(REPO_A, 2, _vec(40))])

    results = await _find(session_factory, current, REPO_A, 99, _vec(0))

    assert [r.issue_number for r in results] == [2]
    async with session_factory() as session:
        assert await current.embedded_issue_numbers(session, REPO_A) == {2}


async def test_upsert_is_idempotent_and_overwrites(session_factory) -> None:
    detector = DuplicateDetector(model_version=MODEL)
    await _seed(session_factory, detector, [(REPO_A, 5, _vec(0))])
    await _seed(session_factory, detector, [(REPO_A, 5, _vec(90))])
    async with session_factory() as session:
        await detector.upsert(session, REPO_A, 5, _vec(90), issue_state="closed")
        await session.commit()

    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(IssueEmbedding))
        row = (await session.execute(select(IssueEmbedding))).scalar_one()

    assert count == 1
    assert row.issue_state == "closed"
    assert row.model_version == MODEL
    assert list(row.embedding[:2]) == pytest.approx([0.0, 1.0], abs=1e-6)


async def test_mark_closed_keeps_the_row_searchable(session_factory) -> None:
    detector = DuplicateDetector(model_version=MODEL)
    await _seed(session_factory, detector, [(REPO_A, 5, _vec(0))])

    async with session_factory() as session:
        assert await detector.mark_closed(session, REPO_A, 5) is True
        assert await detector.mark_closed(session, REPO_A, 404) is False
        await session.commit()

    [result] = await _find(session_factory, detector, REPO_A, 99, _vec(0))
    assert (result.issue_number, result.issue_state) == (5, "closed")


async def test_deleting_a_repo_cascades_to_its_embeddings(session_factory) -> None:
    detector = DuplicateDetector(model_version=MODEL)
    await _seed(session_factory, detector, [(REPO_A, 1, _vec(0)), (REPO_B, 1, _vec(0))])

    async with session_factory() as session:
        await session.delete(await session.get(RepoConfig, REPO_A))
        await session.commit()
        remaining = (await session.execute(select(IssueEmbedding.repo_id))).scalars().all()

    assert remaining == [REPO_B]


async def test_hnsw_cosine_index_exists(session_factory) -> None:
    async with session_factory() as session:
        indexdef = await session.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_issue_embeddings_embedding_hnsw'")
        )
    assert "USING hnsw (embedding vector_cosine_ops)" in indexdef
