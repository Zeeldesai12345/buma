"""
Process-wide resources for the chat assistant (DD-27) and the per-question orchestration.

Created lazily and once per gateway process: the Anthropic client, a READ-ONLY database engine
(the MCP server's engine factory: default_transaction_read_only=on + statement_timeout) and the
query embedding model. The embedding model is ~100 MB and takes seconds to load, so it is loaded
on the first chat question, not at gateway start-up; if it cannot load, search falls back to
keyword search for the life of the process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from functools import lru_cache
from typing import Any

import anthropic
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buma.core.config import Settings
from buma.gateway.chat.agent import ERROR_LLM_UNAVAILABLE, ChatAgent
from buma.gateway.chat.tools import ToolContext
from buma.mcp_server.db import create_readonly_engine, create_readonly_session_factory
from buma.worker.services.embedding_service import EmbeddingService
from buma.worker.services.llm_budget import LLMBudget

logger = logging.getLogger(__name__)

BUDGET_NAMESPACE = "chat"


def chat_enabled(settings: Settings) -> bool:
    return settings.chat_enabled and bool(settings.anthropic_api_key)


def chat_budget(redis: aioredis.Redis, settings: Settings) -> LLMBudget:
    return LLMBudget(
        redis=redis,
        daily_limit=settings.chat_daily_question_limit_per_repo,
        breaker_threshold=settings.chat_breaker_threshold,
        cooldown_seconds=settings.chat_breaker_cooldown_seconds,
        namespace=BUDGET_NAMESPACE,
    )


@lru_cache
def _anthropic_client(api_key: str, timeout_seconds: float) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=2)


@lru_cache
def _readonly_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    return create_readonly_session_factory(create_readonly_engine(database_url))


class _QueryEmbedder:
    """Loads the embedding model at most once per process; remembers a failed load."""

    def __init__(self) -> None:
        self._service: EmbeddingService | None = None
        self._failed = False
        self._lock: asyncio.Lock | None = None

    async def get(self, settings: Settings) -> EmbeddingService | None:
        if not settings.chat_semantic_search_enabled or self._failed:
            return None
        if self._service is not None:
            return self._service
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._service is None and not self._failed:
                try:
                    self._service = await asyncio.to_thread(
                        EmbeddingService.load,
                        settings.embedding_model,
                        cache_dir=settings.embedding_cache_dir,
                        max_chars=settings.embedding_max_chars,
                    )
                except Exception:
                    self._failed = True
                    logger.exception("Chat: embedding model failed to load — issue search will use keywords only")
        return self._service


_query_embedder = _QueryEmbedder()


class ChatService:
    """Runs one question end to end: embedder, read-only DB session, agent loop, budget outcome."""

    def __init__(
        self,
        settings: Settings,
        agent: ChatAgent,
        session_factory: Callable[[], AsyncSession],
        load_embedder: Callable[[], Any],
    ) -> None:
        self._settings = settings
        self._agent = agent
        self._session_factory = session_factory
        self._load_embedder = load_embedder

    @classmethod
    def from_settings(cls, settings: Settings) -> ChatService:
        agent = ChatAgent(
            client=_anthropic_client(settings.anthropic_api_key, settings.chat_timeout_seconds),
            model=settings.chat_model,
            effort=settings.chat_effort,
            max_tokens=settings.chat_max_tokens,
            max_tool_rounds=settings.chat_max_tool_rounds,
        )
        return cls(
            settings=settings,
            agent=agent,
            session_factory=_readonly_session_factory(settings.database_url),
            load_embedder=lambda: _query_embedder.get(settings),
        )

    async def stream(
        self,
        repo_id: int,
        repo_full_name: str,
        question: str,
        history: Sequence[dict[str, str]],
    ) -> AsyncIterator[dict[str, Any]]:
        """Yields agent events. Never raises: unexpected errors become an error event + done."""
        llm_ok = True
        try:
            embedder = await self._load_embedder()
            async with self._session_factory() as db:
                ctx = ToolContext(db=db, repo_id=repo_id, embedder=embedder)
                async for event in self._agent.run(ctx, repo_full_name, history, question):
                    if event.get("code") == ERROR_LLM_UNAVAILABLE:
                        llm_ok = False
                    yield event
        except Exception:
            logger.exception("repo_id=%d — chat failed unexpectedly", repo_id)
            yield {"type": "error", "code": "internal", "message": "Something went wrong. Please try again."}
            yield {"type": "done"}
            return

        # Recorded only when the stream ran to completion (not when the browser disconnected).
        await self._record(llm_ok)

    async def _record(self, ok: bool) -> None:
        # Own short-lived client: the request's Redis dependency may already be closed while streaming.
        client = aioredis.from_url(self._settings.redis_url, decode_responses=True)
        try:
            await chat_budget(client, self._settings).record(ok)
        finally:
            await client.aclose()
