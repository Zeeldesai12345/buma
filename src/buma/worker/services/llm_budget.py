from __future__ import annotations

import logging
from datetime import UTC, datetime

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_CALLS_KEY = "buma:llm_calls:{repo_id}:{day}"
_FAILURES_KEY = "buma:llm_failures"
_BREAKER_KEY = "buma:llm_breaker_open"
_CALLS_KEY_TTL_SECONDS = 48 * 3600


class LLMBudget:
    """
    Cost gate in front of every Claude call, backed by Redis.

    - Per-repo daily call budget: `INCR buma:llm_calls:{repo_id}:{YYYY-MM-DD}` (UTC day).
    - Circuit breaker: `claude_breaker_threshold` consecutive failures open the breaker for
      `claude_breaker_cooldown_seconds`; while it is open every call is refused.

    Fails closed: if Redis itself errors, `allow()` returns False so an outage can never turn
    into unbounded API spend. `record()` never raises.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        daily_limit: int,
        breaker_threshold: int,
        cooldown_seconds: int,
    ) -> None:
        self._redis = redis
        self._daily_limit = daily_limit
        self._breaker_threshold = breaker_threshold
        self._cooldown_seconds = cooldown_seconds

    async def allow(self, repo_id: int) -> bool:
        try:
            if await self._redis.exists(_BREAKER_KEY):
                logger.warning("repo_id=%d — LLM circuit breaker open, skipping Claude", repo_id)
                return False

            # Counted before the call on purpose: a call that later fails or times out may still
            # have been billed, so it consumes budget too.
            key = _CALLS_KEY.format(repo_id=repo_id, day=f"{datetime.now(UTC):%Y-%m-%d}")
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, _CALLS_KEY_TTL_SECONDS)
        except Exception:
            logger.exception("repo_id=%d — LLM budget check failed, skipping Claude (fail closed)", repo_id)
            return False

        if count > self._daily_limit:
            logger.warning(
                "repo_id=%d — daily Claude budget exhausted (%d/%d), skipping Claude",
                repo_id,
                count,
                self._daily_limit,
            )
            return False
        return True

    async def record(self, ok: bool) -> None:
        try:
            if ok:
                await self._redis.delete(_FAILURES_KEY)
                return
            failures = await self._redis.incr(_FAILURES_KEY)
            if failures >= self._breaker_threshold:
                await self._redis.set(_BREAKER_KEY, 1, ex=self._cooldown_seconds)
                await self._redis.delete(_FAILURES_KEY)
                logger.warning(
                    "%d consecutive Claude failures — circuit breaker open for %ds",
                    failures,
                    self._cooldown_seconds,
                )
        except Exception:
            logger.exception("Failed to record Claude call outcome in LLM budget")
