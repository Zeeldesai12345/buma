from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from buma.db.models import EMBEDDING_DIM

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_MAX_CHARS = 2000


class EmbeddingError(RuntimeError):
    """The embedding model returned something other than one EMBEDDING_DIM-long vector per input."""


class EmbeddingService:
    """
    Local CPU text embeddings for semantic duplicate detection (T3 / DD-25).

    Build it ONCE per process with `EmbeddingService.load()` — loading the ONNX model takes
    seconds and ~100 MB of RAM. Inference is CPU-bound, so `embed()` / `embed_batch()` run it in
    a worker thread via `asyncio.to_thread` and never block the event loop.

    Unlike ClaudeClassifier, this service DOES raise (EmbeddingError, or whatever the model
    raises). The caller — EventProcessorService — owns the "never block triage" contract.
    """

    def __init__(self, model: Any, model_version: str, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self._model = model
        self.model_version = model_version
        self._max_chars = max_chars

    @classmethod
    def load(
        cls,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        cache_dir: str | None = None,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> EmbeddingService:
        """Load the fastembed model (slow, blocking). Call once at startup, e.g. via asyncio.to_thread."""
        # Imported here so modules that only need the type (and tests) don't pay for onnxruntime.
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        logger.info("Embedding model loaded: %s (dim=%d)", model_name, EMBEDDING_DIM)
        return cls(model=model, model_version=model_name, max_chars=max_chars)

    def build_text(self, title: str, body: str | None) -> str:
        """Text that represents an issue: title plus the first `max_chars` of the body."""
        body = (body or "")[: self._max_chars]
        return f"{title}\n{body}".strip()

    async def embed(self, title: str, body: str | None) -> list[float]:
        [vector] = await self.embed_batch([(title, body)])
        return vector

    async def embed_batch(self, issues: Sequence[tuple[str, str | None]]) -> list[list[float]]:
        if not issues:
            return []
        texts = [self.build_text(title, body) for title, body in issues]
        return await asyncio.to_thread(self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        vectors = [[float(x) for x in vector] for vector in self._model.embed(texts)]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"Embedding model returned {len(vectors)} vectors for {len(texts)} inputs")
        for vector in vectors:
            if len(vector) != EMBEDDING_DIM:
                raise EmbeddingError(
                    f"Embedding model {self.model_version} returned a {len(vector)}-dim vector, "
                    f"expected {EMBEDDING_DIM}"
                )
        return vectors
