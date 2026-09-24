from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

# Every transaction on these connections is READ ONLY at the Postgres level, so even a bug that
# issued an INSERT/UPDATE/DELETE would be rejected by the database ("cannot execute ... in a
# read-only transaction"). statement_timeout bounds any runaway query triggered through MCP.
READ_ONLY_CONNECT_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=5000"


def create_readonly_engine(database_url: str, extra_options: str = "") -> AsyncEngine:
    """`extra_options` appends more libpq `-c` settings (used by tests to set search_path)."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        # Fail fast (seconds, not minutes) when the database is down, so MCP clients get a clean error.
        connect_args={"options": f"{READ_ONLY_CONNECT_OPTIONS} {extra_options}".strip(), "connect_timeout": 5},
    )


def create_readonly_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
