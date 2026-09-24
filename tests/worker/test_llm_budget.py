from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from buma.worker.services.llm_budget import LLMBudget


class FakeRedis:
    """Minimal in-memory stand-in for the redis.asyncio commands LLMBudget uses."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def exists(self, key: str) -> int:
        return int(key in self.store)

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def set(self, key: str, value: int, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return int(self.store.pop(key, None) is not None)


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


def _budget(redis: object, limit: int = 3, threshold: int = 2, cooldown: int = 300) -> LLMBudget:
    return LLMBudget(redis=redis, daily_limit=limit, breaker_threshold=threshold, cooldown_seconds=cooldown)


# ---------------------------------------------------------------------------
# Per-repo daily budget
# ---------------------------------------------------------------------------


async def test_calls_within_limit_are_allowed(redis: FakeRedis) -> None:
    budget = _budget(redis, limit=3)
    assert [await budget.allow(1) for _ in range(3)] == [True, True, True]


async def test_limit_plus_one_call_is_refused(redis: FakeRedis) -> None:
    budget = _budget(redis, limit=3)
    for _ in range(3):
        await budget.allow(1)
    assert await budget.allow(1) is False


async def test_budget_is_per_repo(redis: FakeRedis) -> None:
    budget = _budget(redis, limit=1)
    assert await budget.allow(1) is True
    assert await budget.allow(1) is False
    assert await budget.allow(2) is True


async def test_counter_key_gets_48h_expiry(redis: FakeRedis) -> None:
    await _budget(redis).allow(7)
    [key] = [k for k in redis.store if k.startswith("buma:llm_calls:7:")]
    assert redis.ttls[key] == 48 * 3600


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


async def test_breaker_opens_after_consecutive_failures(redis: FakeRedis) -> None:
    budget = _budget(redis, threshold=2, cooldown=300)
    await budget.record(ok=False)
    assert await budget.allow(1) is True
    await budget.record(ok=False)

    assert redis.ttls["buma:llm_breaker_open"] == 300
    assert await budget.allow(1) is False


async def test_success_resets_failure_streak(redis: FakeRedis) -> None:
    budget = _budget(redis, threshold=2)
    await budget.record(ok=False)
    await budget.record(ok=True)
    await budget.record(ok=False)
    assert await budget.allow(1) is True


async def test_open_breaker_does_not_consume_budget(redis: FakeRedis) -> None:
    await redis.set("buma:llm_breaker_open", 1)
    budget = _budget(redis)
    assert await budget.allow(1) is False
    assert not any(k.startswith("buma:llm_calls:") for k in redis.store)


# ---------------------------------------------------------------------------
# Redis failures
# ---------------------------------------------------------------------------


async def test_redis_error_fails_closed() -> None:
    broken = AsyncMock()
    broken.exists.side_effect = ConnectionError("redis down")
    assert await _budget(broken).allow(1) is False


async def test_record_never_raises_on_redis_error() -> None:
    broken = AsyncMock()
    broken.incr.side_effect = ConnectionError("redis down")
    broken.delete.side_effect = ConnectionError("redis down")
    await _budget(broken).record(ok=False)
    await _budget(broken).record(ok=True)
