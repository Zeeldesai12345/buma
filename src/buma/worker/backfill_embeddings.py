"""
Backfill issue embeddings for enrolled repositories from the GitHub API (T3 / DD-25).

    python -m buma.worker.backfill_embeddings                    # every enrolled repo
    python -m buma.worker.backfill_embeddings --repo owner/name  # one repo
    python -m buma.worker.backfill_embeddings --force            # re-embed everything

Safe to re-run: issues that already have an embedding from the current model are skipped
(unless --force), and every write is an upsert on (repo_id, issue_number), so a repeated or
interrupted run never creates duplicate rows. Pull requests are skipped.

Inside Docker: `docker compose run --rm worker uv run --no-sync python -m buma.worker.backfill_embeddings`
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from buma.core.config import get_settings
from buma.db.models import RepoConfig
from buma.worker.services.duplicate_detector import DuplicateDetector
from buma.worker.services.embedding_service import EmbeddingService
from buma.worker.services.github_client import GitHubClient

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 64


@dataclass
class BackfillStats:
    fetched: int = 0
    skipped_pull_requests: int = 0
    skipped_existing: int = 0
    embedded: int = 0


async def backfill_repo(
    repo_config: RepoConfig,
    github_client: GitHubClient,
    embedding_service: EmbeddingService,
    detector: DuplicateDetector,
    session_factory: async_sessionmaker[AsyncSession],
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> BackfillStats:
    stats = BackfillStats()
    owner, repo_name = repo_config.repo_full_name.split("/", 1)

    existing: set[int] = set()
    if not force:
        async with session_factory() as session:
            existing = await detector.embedded_issue_numbers(session, repo_config.repo_id)

    token = await github_client.get_installation_token(repo_config.installation_id)
    batch: list[dict] = []

    async def flush() -> None:
        vectors = await embedding_service.embed_batch([(i["title"], i.get("body")) for i in batch])
        async with session_factory() as session:
            for issue, vector in zip(batch, vectors, strict=True):
                state = "closed" if issue.get("state") == "closed" else "open"
                await detector.upsert(session, repo_config.repo_id, issue["number"], vector, issue_state=state)
            await session.commit()
        stats.embedded += len(batch)
        logger.info("%s — embedded %d issues so far", repo_config.repo_full_name, stats.embedded)
        batch.clear()

    async for issue in github_client.list_issues(token, owner, repo_name, state="all"):
        stats.fetched += 1
        if "pull_request" in issue:
            stats.skipped_pull_requests += 1
            continue
        if issue["number"] in existing:
            stats.skipped_existing += 1
            continue
        batch.append(issue)
        if len(batch) >= batch_size:
            await flush()

    if batch:
        await flush()
    return stats


async def run(repo: str | None, force: bool, batch_size: int) -> int:
    settings = get_settings()
    if not (settings.github_app_id and settings.github_app_private_key):
        logger.error("GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY are required for the backfill")
        return 2

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            query = select(RepoConfig).order_by(RepoConfig.repo_full_name)
            if repo:
                query = query.where(RepoConfig.repo_full_name == repo)
            repo_configs = list((await session.execute(query)).scalars().all())

        if not repo_configs:
            logger.error("No enrolled repository matches %s", repo or "(any)")
            return 1

        embedding_service = await asyncio.to_thread(
            EmbeddingService.load,
            settings.embedding_model,
            cache_dir=settings.embedding_cache_dir,
            max_chars=settings.embedding_max_chars,
        )
        detector = DuplicateDetector(model_version=embedding_service.model_version)
        github_client = GitHubClient(app_id=settings.github_app_id, private_key_pem=settings.github_app_private_key)

        failures = 0
        for repo_config in repo_configs:
            try:
                stats = await backfill_repo(
                    repo_config, github_client, embedding_service, detector, session_factory, force, batch_size
                )
            except Exception:
                failures += 1
                logger.exception("%s — backfill failed (re-run to resume)", repo_config.repo_full_name)
                continue
            logger.info(
                "%s — done: fetched=%d embedded=%d skipped_existing=%d skipped_pull_requests=%d",
                repo_config.repo_full_name,
                stats.fetched,
                stats.embedded,
                stats.skipped_existing,
                stats.skipped_pull_requests,
            )
        return 1 if failures else 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill issue embeddings from the GitHub API.")
    parser.add_argument("--repo", help="Only backfill this enrolled repository (owner/name)")
    parser.add_argument("--force", action="store_true", help="Re-embed issues already embedded with the current model")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    return asyncio.run(run(args.repo, args.force, args.batch_size))


if __name__ == "__main__":
    sys.exit(main())
