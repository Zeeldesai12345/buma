from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from buma.db.models import IssueEmbedding

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
DEFAULT_SIMILARITY_THRESHOLD = 0.9


@dataclass(frozen=True)
class SimilarIssue:
    issue_number: int
    issue_state: str
    similarity: float  # cosine similarity, 1.0 = identical direction


class DuplicateDetector:
    """
    pgvector-backed storage and nearest-neighbour search over issue embeddings (T3 / DD-25).

    Every query is scoped to ONE repo and ONE model_version:
    - repo_id: without it, issues from other tenants' repositories would match (data leak).
    - model_version: vectors from different embedding models are not comparable.

    Methods take the caller's AsyncSession and never commit — the caller owns the transaction.
    Duplicates are only ever *flagged*; nothing here closes or modifies a GitHub issue.
    """

    def __init__(
        self,
        model_version: str,
        top_k: int = DEFAULT_TOP_K,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self.model_version = model_version
        self._top_k = top_k
        self._threshold = threshold

    async def upsert(
        self,
        session: AsyncSession,
        repo_id: int,
        issue_number: int,
        embedding: Sequence[float],
        issue_state: str = "open",
    ) -> None:
        """Insert or overwrite the single row for (repo_id, issue_number). Safe to repeat."""
        stmt = insert(IssueEmbedding).values(
            repo_id=repo_id,
            issue_number=issue_number,
            embedding=list(embedding),
            model_version=self.model_version,
            issue_state=issue_state,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[IssueEmbedding.repo_id, IssueEmbedding.issue_number],
            set_={
                "embedding": stmt.excluded.embedding,
                "model_version": stmt.excluded.model_version,
                "issue_state": stmt.excluded.issue_state,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)

    async def find_similar(
        self,
        session: AsyncSession,
        repo_id: int,
        issue_number: int,
        embedding: Sequence[float],
        k: int | None = None,
    ) -> list[SimilarIssue]:
        """Top-k most similar issues in the same repo (same model), excluding `issue_number` itself."""
        distance = IssueEmbedding.embedding.cosine_distance(list(embedding))
        result = await session.execute(
            select(
                IssueEmbedding.issue_number,
                IssueEmbedding.issue_state,
                (1 - distance).label("similarity"),
            )
            .where(
                IssueEmbedding.repo_id == repo_id,
                IssueEmbedding.model_version == self.model_version,
                IssueEmbedding.issue_number != issue_number,
            )
            .order_by(distance)
            .limit(k or self._top_k)
        )
        return [
            SimilarIssue(issue_number=row.issue_number, issue_state=row.issue_state, similarity=float(row.similarity))
            for row in result
        ]

    def filter_duplicates(self, matches: Sequence[SimilarIssue]) -> list[SimilarIssue]:
        """Keep only matches at or above the similarity threshold (order preserved)."""
        return [m for m in matches if m.similarity >= self._threshold]

    async def mark_closed(self, session: AsyncSession, repo_id: int, issue_number: int) -> bool:
        """Set issue_state='closed'. Returns False if the issue was never embedded."""
        result = await session.execute(
            update(IssueEmbedding)
            .where(IssueEmbedding.repo_id == repo_id, IssueEmbedding.issue_number == issue_number)
            .values(issue_state="closed", updated_at=func.now())
        )
        return result.rowcount > 0

    async def embedded_issue_numbers(self, session: AsyncSession, repo_id: int) -> set[int]:
        """Issue numbers in this repo that already have an embedding from the current model."""
        result = await session.execute(
            select(IssueEmbedding.issue_number).where(
                IssueEmbedding.repo_id == repo_id,
                IssueEmbedding.model_version == self.model_version,
            )
        )
        return set(result.scalars().all())
