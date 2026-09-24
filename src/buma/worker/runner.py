from __future__ import annotations

import asyncio
import logging
import signal

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from buma.core.config import Settings, get_settings
from buma.worker.consumer import QueueConsumer
from buma.worker.services.claude_client import ClaudeClassifier
from buma.worker.services.duplicate_detector import DuplicateDetector
from buma.worker.services.embedding_service import EmbeddingService
from buma.worker.services.event_processor import EventProcessorService
from buma.worker.services.github_client import GitHubClient
from buma.worker.services.llm_budget import LLMBudget
from buma.worker.services.triage_engine import TriageEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def load_duplicate_detection(settings: Settings) -> tuple[EmbeddingService | None, DuplicateDetector | None]:
    """
    Load the embedding model ONCE for the worker's lifetime (T3 / DD-25).

    Loading is slow and blocking, so it runs in a thread. Any failure (missing model files, no
    network for the first download, onnxruntime error) disables semantic duplicate detection
    instead of stopping the worker — triage must keep running without it.
    """
    if not settings.embedding_enabled:
        logger.info("EMBEDDING_ENABLED=false — semantic duplicate detection disabled")
        return None, None

    try:
        embedding_service = await asyncio.to_thread(
            EmbeddingService.load,
            settings.embedding_model,
            cache_dir=settings.embedding_cache_dir,
            max_chars=settings.embedding_max_chars,
        )
    except Exception:
        logger.exception(
            "Failed to load embedding model %s — semantic duplicate detection disabled, triage continues",
            settings.embedding_model,
        )
        return None, None

    duplicate_detector = DuplicateDetector(
        model_version=embedding_service.model_version,
        top_k=settings.duplicate_top_k,
        threshold=settings.duplicate_similarity_threshold,
    )
    logger.info(
        "Semantic duplicate detection enabled (model=%s top_k=%d comment_enabled=%s threshold=%.2f)",
        embedding_service.model_version,
        settings.duplicate_top_k,
        settings.duplicate_comment_enabled,
        settings.duplicate_similarity_threshold,
    )
    return embedding_service, duplicate_detector


async def main() -> None:
    settings = get_settings()
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    github_client: GitHubClient | None = None
    if settings.github_app_id and settings.github_app_private_key:
        github_client = GitHubClient(
            app_id=settings.github_app_id,
            private_key_pem=settings.github_app_private_key,
        )
        logger.info("GitHub App client configured (app_id=%d)", settings.github_app_id)
    else:
        logger.warning("GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY not set — Phase 6 (GitHub patch) will be skipped")

    claude_classifier: ClaudeClassifier | None = None
    llm_budget: LLMBudget | None = None
    if settings.anthropic_api_key:
        claude_classifier = ClaudeClassifier(
            api_key=settings.anthropic_api_key,
            model=settings.claude_model,
            timeout_seconds=settings.claude_timeout_seconds,
            max_retries=settings.claude_max_retries,
            max_body_chars=settings.claude_max_body_chars,
        )
        llm_budget = LLMBudget(
            redis=redis_client,
            daily_limit=settings.claude_daily_call_limit_per_repo,
            breaker_threshold=settings.claude_breaker_threshold,
            cooldown_seconds=settings.claude_breaker_cooldown_seconds,
        )
        logger.info(
            "Claude hybrid fallback classifier configured (model=%s daily_limit_per_repo=%d max_priority=%s)",
            settings.claude_model,
            settings.claude_daily_call_limit_per_repo,
            settings.claude_max_priority,
        )
    else:
        logger.warning("ANTHROPIC_API_KEY not set — hybrid Claude fallback disabled, triage stays rule-only")

    triage_engine = TriageEngine(
        claude_classifier=claude_classifier,
        confidence_threshold=settings.claude_confidence_threshold,
        llm_budget=llm_budget,
        claude_max_priority=settings.claude_max_priority,
    )

    embedding_service, duplicate_detector = await load_duplicate_detection(settings)

    try:
        processor = EventProcessorService(
            session_factory=session_factory,
            triage_engine=triage_engine,
            github_client=github_client,
            embedding_service=embedding_service,
            duplicate_detector=duplicate_detector,
            duplicate_comment_enabled=settings.duplicate_comment_enabled,
        )
        consumer = QueueConsumer(redis=redis_client, processor=processor)
        await consumer.run_forever(stop_event=stop_event)
    finally:
        await redis_client.aclose()
        await engine.dispose()
        logger.info("Connections closed.")


if __name__ == "__main__":
    asyncio.run(main())
