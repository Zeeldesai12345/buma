from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

from buma.db.models import RepoConfig
from buma.worker.backfill_embeddings import backfill_repo
from buma.worker.services.duplicate_detector import DuplicateDetector

MODEL = "BAAI/bge-small-en-v1.5"


class InMemoryDetector(DuplicateDetector):
    """DuplicateDetector whose storage is a dict keyed like the real table: (repo_id, issue_number)."""

    def __init__(self, model_version: str = MODEL) -> None:
        super().__init__(model_version=model_version)
        self.rows: dict[tuple[int, int], dict] = {}
        self.upserts = 0

    async def upsert(self, session, repo_id, issue_number, embedding, issue_state="open") -> None:
        self.upserts += 1
        self.rows[(repo_id, issue_number)] = {
            "embedding": list(embedding),
            "model_version": self.model_version,
            "issue_state": issue_state,
        }

    async def embedded_issue_numbers(self, session, repo_id) -> set[int]:
        return {
            number
            for (rid, number), row in self.rows.items()
            if rid == repo_id and row["model_version"] == self.model_version
        }


class FakeEmbeddingService:
    def __init__(self) -> None:
        self.batches: list[list[tuple[str, str | None]]] = []

    async def embed_batch(self, issues: Sequence[tuple[str, str | None]]) -> list[list[float]]:
        self.batches.append(list(issues))
        return [[0.1] * 384 for _ in issues]


def _issue(number: int, state: str = "open", pr: bool = False) -> dict:
    item = {"number": number, "title": f"Issue {number}", "body": f"body {number}", "state": state}
    if pr:
        item["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
    return item


def _github(items: list[dict]) -> MagicMock:
    github = MagicMock()
    github.get_installation_token = AsyncMock(return_value="token")

    async def list_issues(token, owner, repo, state="all") -> AsyncIterator[dict]:
        assert (token, owner, repo, state) == ("token", "owner", "repo", "all")
        for item in items:
            yield item

    github.list_issues = list_issues
    return github


def _session_factory() -> MagicMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session)


def _repo_config() -> RepoConfig:
    return RepoConfig(repo_id=111, installation_id=12345, repo_full_name="owner/repo", config={})


ITEMS = [_issue(1), _issue(2, pr=True), _issue(3, state="closed"), _issue(4), _issue(5, pr=True), _issue(6)]


async def _run(detector: InMemoryDetector, embedder: FakeEmbeddingService, force: bool = False, batch_size: int = 64):
    return await backfill_repo(
        _repo_config(), _github(ITEMS), embedder, detector, _session_factory(), force=force, batch_size=batch_size
    )


async def test_backfill_skips_pull_requests() -> None:
    detector, embedder = InMemoryDetector(), FakeEmbeddingService()

    stats = await _run(detector, embedder)

    assert stats.fetched == 6
    assert stats.skipped_pull_requests == 2
    assert stats.embedded == 4
    assert sorted(n for _, n in detector.rows) == [1, 3, 4, 6]


async def test_backfill_stores_github_issue_state() -> None:
    detector = InMemoryDetector()
    await _run(detector, FakeEmbeddingService())
    assert detector.rows[(111, 3)]["issue_state"] == "closed"
    assert detector.rows[(111, 1)]["issue_state"] == "open"
    assert detector.rows[(111, 1)]["model_version"] == MODEL


async def test_backfill_embeds_in_batches() -> None:
    embedder = FakeEmbeddingService()
    await _run(InMemoryDetector(), embedder, batch_size=3)
    assert [len(b) for b in embedder.batches] == [3, 1]
    assert embedder.batches[0][0] == ("Issue 1", "body 1")


async def test_backfill_is_idempotent() -> None:
    detector, embedder = InMemoryDetector(), FakeEmbeddingService()

    first = await _run(detector, embedder)
    rows_after_first = dict(detector.rows)
    second = await _run(detector, embedder)

    assert first.embedded == 4
    assert second.embedded == 0
    assert second.skipped_existing == 4
    assert detector.rows == rows_after_first
    assert detector.upserts == 4


async def test_force_re_embeds_without_creating_duplicate_rows() -> None:
    detector, embedder = InMemoryDetector(), FakeEmbeddingService()

    await _run(detector, embedder)
    forced = await _run(detector, embedder, force=True)

    assert forced.embedded == 4
    assert forced.skipped_existing == 0
    assert len(detector.rows) == 4
    assert detector.upserts == 8


async def test_rows_from_another_model_are_re_embedded() -> None:
    detector = InMemoryDetector()
    detector.rows[(111, 1)] = {"embedding": [0.0] * 384, "model_version": "old-model", "issue_state": "open"}

    stats = await _run(detector, FakeEmbeddingService())

    assert stats.skipped_existing == 0
    assert detector.rows[(111, 1)]["model_version"] == MODEL


async def test_github_error_propagates_so_the_run_reports_failure() -> None:
    github = _github(ITEMS)
    github.get_installation_token = AsyncMock(side_effect=RuntimeError("401"))
    with pytest.raises(RuntimeError):
        await backfill_repo(_repo_config(), github, FakeEmbeddingService(), InMemoryDetector(), _session_factory())
