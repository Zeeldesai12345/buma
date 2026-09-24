FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first — this layer is cached unless pyproject.toml/uv.lock changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Bake the embedding model into the image so the worker never downloads it at runtime (T3 / DD-25).
# Keep EMBEDDING_MODEL in sync with the default in src/buma/core/config.py.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_CACHE_DIR=/opt/fastembed_cache
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBEDDING_MODEL}', cache_dir='${EMBEDDING_CACHE_DIR}')"

# Copy source and install the project itself
COPY README.md ./
COPY src/ ./src/
RUN rm -rf src/*.egg-info
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --no-editable

# Run as non-root
RUN useradd --system --create-home buma
RUN chown -R buma:buma /opt/fastembed_cache
USER buma