from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from buma.core.config import Settings
from buma.worker import runner
from buma.worker.services import embedding_service as embedding_module
from buma.worker.services.duplicate_detector import DuplicateDetector
from buma.worker.services.embedding_service import EmbeddingError, EmbeddingService


class FakeModel:
    """Stands in for fastembed.TextEmbedding: records every input, returns fixed-size vectors."""

    instances = 0

    def __init__(self, model_name: str = "fake", cache_dir: str | None = None, dim: int = 384) -> None:
        FakeModel.instances += 1
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.dim = dim
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]):
        self.calls.append(list(texts))
        for i, _ in enumerate(texts):
            yield [float(i)] * self.dim


@pytest.fixture(autouse=True)
def _reset_fake_model_count() -> None:
    FakeModel.instances = 0


def _service(dim: int = 384, max_chars: int = 2000) -> tuple[EmbeddingService, FakeModel]:
    model = FakeModel(dim=dim)
    return EmbeddingService(model=model, model_version="BAAI/bge-small-en-v1.5", max_chars=max_chars), model


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


async def test_embed_returns_384_floats() -> None:
    service, _ = _service()
    vector = await service.embed("App crashes", "stack trace")
    assert len(vector) == 384
    assert all(isinstance(x, float) for x in vector)


async def test_embed_runs_inference_in_a_thread() -> None:
    service, _ = _service()
    real_to_thread = embedding_module.asyncio.to_thread
    with patch.object(embedding_module.asyncio, "to_thread", side_effect=real_to_thread) as to_thread:
        await service.embed("title", "body")
    to_thread.assert_called_once()
    assert to_thread.call_args.args[0] == service._embed_sync


async def test_embed_batch_is_a_single_model_call() -> None:
    service, model = _service()
    vectors = await service.embed_batch([("a", "1"), ("b", None), ("c", "3")])
    assert len(vectors) == 3
    assert len(model.calls) == 1
    assert len(model.calls[0]) == 3


async def test_embed_batch_of_nothing_skips_the_model() -> None:
    service, model = _service()
    assert await service.embed_batch([]) == []
    assert model.calls == []


async def test_wrong_dimension_raises() -> None:
    service, _ = _service(dim=768)
    with pytest.raises(EmbeddingError, match="768-dim"):
        await service.embed("title", "body")


def test_body_is_cut_to_max_chars() -> None:
    service, _ = _service(max_chars=10)
    assert service.build_text("Title", "0123456789-TAIL") == "Title\n0123456789"


def test_missing_body_uses_title_only() -> None:
    service, _ = _service()
    assert service.build_text("Title", None) == "Title"


async def test_many_embeds_reuse_the_same_model() -> None:
    service, model = _service()
    for i in range(5):
        await service.embed(f"issue {i}", None)
    assert FakeModel.instances == 1
    assert len(model.calls) == 5


def test_load_builds_the_fastembed_model_with_name_and_cache_dir() -> None:
    fake_fastembed = types.SimpleNamespace(TextEmbedding=FakeModel)
    with patch.dict(sys.modules, {"fastembed": fake_fastembed}):
        service = EmbeddingService.load("BAAI/bge-small-en-v1.5", cache_dir="/opt/cache", max_chars=500)
    assert FakeModel.instances == 1
    assert service.model_version == "BAAI/bge-small-en-v1.5"
    assert service._model.cache_dir == "/opt/cache"
    assert service._max_chars == 500


# ---------------------------------------------------------------------------
# Worker startup — runner.load_duplicate_detection
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    # Explicit values so a developer's local .env (loaded by conftest) can't change the outcome.
    base = {
        "database_url": "postgresql+psycopg://x/y",
        "github_webhook_secret": "s",
        "embedding_enabled": True,
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "embedding_cache_dir": None,
        "embedding_max_chars": 2000,
    }
    return Settings(_env_file=None, **{**base, **overrides})


async def test_worker_startup_loads_the_model_exactly_once() -> None:
    service, _ = _service()
    with patch.object(EmbeddingService, "load", return_value=service) as load:
        embedding_service, detector = await runner.load_duplicate_detection(_settings(duplicate_top_k=3))
    load.assert_called_once_with("BAAI/bge-small-en-v1.5", cache_dir=None, max_chars=2000)
    assert embedding_service is service
    assert isinstance(detector, DuplicateDetector)
    assert detector.model_version == "BAAI/bge-small-en-v1.5"
    assert detector._top_k == 3


async def test_worker_startup_disabled_by_setting() -> None:
    with patch.object(EmbeddingService, "load") as load:
        assert await runner.load_duplicate_detection(_settings(embedding_enabled=False)) == (None, None)
    load.assert_not_called()


async def test_model_load_failure_disables_feature_instead_of_crashing() -> None:
    with patch.object(EmbeddingService, "load", side_effect=OSError("model files missing")):
        assert await runner.load_duplicate_detection(_settings()) == (None, None)


def test_duplicate_comments_are_disabled_by_default() -> None:
    assert Settings.model_fields["duplicate_comment_enabled"].default is False
