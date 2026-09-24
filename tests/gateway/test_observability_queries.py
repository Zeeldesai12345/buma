"""
Unit tests for the shared read-only query layer (N1 / DD-26).

Mocked session: these check which statements are issued, their scoping and ordering, and that
results are passed back unchanged. Real-database behaviour is covered in
tests/integration/test_mcp_server_pg.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import TextClause

from buma.gateway.services import observability_queries as queries


def _compiled(session: AsyncMock, call: int = 0) -> str:
    statement = session.execute.call_args_list[call].args[0]
    if isinstance(statement, TextClause):
        return statement.text
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _session(*results: object) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=list(results))
    return session


def _scalars(rows: list) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _count(n: int) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = n
    return result


async def test_get_repo_returns_row_or_none() -> None:
    repo = MagicMock()
    found = MagicMock(scalar_one_or_none=MagicMock(return_value=repo))
    missing = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    session = _session(found, missing)

    assert await queries.get_repo(session, 111) is repo
    assert await queries.get_repo(session, 222) is None
    assert "WHERE repo_config.repo_id = 111" in _compiled(session, 0)


async def test_list_repos_orders_by_name() -> None:
    rows = [MagicMock(), MagicMock()]
    session = _session(_scalars(rows))

    assert await queries.list_repos(session) == rows
    assert "ORDER BY repo_config.repo_full_name" in _compiled(session)


async def test_get_triage_page_counts_then_pages_most_recent_first() -> None:
    rows = [MagicMock()]
    session = _session(_count(7), _scalars(rows))

    result_rows, total = await queries.get_triage_page(session, 111, limit=20, offset=5)

    assert (result_rows, total) == (rows, 7)
    count_sql, page_sql = _compiled(session, 0), _compiled(session, 1)
    assert "count(*)" in count_sql and "triage_decision.repo_id = 111" in count_sql
    assert "triage_decision.repo_id = 111" in page_sql
    assert "ORDER BY triage_decision.decided_at DESC" in page_sql
    assert "LIMIT 20 OFFSET 5" in page_sql


async def test_get_issue_titles_is_repo_scoped_and_maps_by_event_id() -> None:
    result = [MagicMock(event_id="e1", title="Crash"), MagicMock(event_id="e2", title="Leak")]
    session = _session(result)

    titles = await queries.get_issue_titles(session, 111, ["e1", "e2", "e3"])

    assert titles == {"e1": "Crash", "e2": "Leak"}
    sql = _compiled(session)
    assert "issue_snapshot.repo_id = 111" in sql
    assert "issue_snapshot.event_id IN ('e1', 'e2', 'e3')" in sql


async def test_get_issue_titles_with_no_events_skips_the_query() -> None:
    session = _session()
    assert await queries.get_issue_titles(session, 111, []) == {}
    session.execute.assert_not_called()


async def test_get_workload_busiest_first() -> None:
    rows = [MagicMock()]
    session = _session(_scalars(rows))

    assert await queries.get_workload(session, 111) == rows
    sql = _compiled(session)
    assert "developer_profile.repo_id = 111" in sql
    assert "ORDER BY developer_profile.open_assignments DESC" in sql


@pytest.mark.parametrize(
    ("window", "lookback", "unit"),
    [("7d", "7 days", "day"), ("30d", "30 days", "week"), ("90d", "90 days", "month"), ("all", "12 months", "month")],
)
async def test_get_productivity_uses_window_constants(window: str, lookback: str, unit: str) -> None:
    agg = MagicMock()
    agg.mappings.return_value.all.return_value = ["agg-row"]
    buckets = MagicMock()
    buckets.mappings.return_value.all.return_value = ["bucket-row"]
    session = _session(agg, buckets)

    agg_rows, bucket_rows = await queries.get_productivity(session, 111, window)

    assert (agg_rows, bucket_rows) == (["agg-row"], ["bucket-row"])
    assert f"INTERVAL '{lookback}'" in _compiled(session, 0)
    assert f"date_trunc('{unit}'" in _compiled(session, 1)
    # repo_id is always a bound parameter, never interpolated
    for call in session.execute.call_args_list:
        assert call.args[1] == {"repo_id": 111}


async def test_get_productivity_rejects_unknown_window() -> None:
    with pytest.raises(KeyError):
        await queries.get_productivity(_session(), 111, "1y; DROP TABLE triage_decision")


async def test_get_issue_page_counts_then_pages_most_recent_first() -> None:
    rows = [MagicMock()]
    session = _session(_count(3), _scalars(rows))

    assert await queries.get_issue_page(session, 111, limit=10) == (rows, 3)
    page_sql = _compiled(session, 1)
    assert "ORDER BY issue_snapshot.snapshot_at DESC" in page_sql
    assert "LIMIT 10 OFFSET 0" in page_sql


def test_query_module_contains_no_write_statements() -> None:
    import inspect

    source = inspect.getsource(queries).upper()
    write_markers = (
        "INSERT INTO", "INSERT(", "UPDATE(", "UPDATE ", "DELETE(", "DELETE FROM", "DROP ", "TRUNCATE",
        ".ADD(", ".FLUSH(", ".COMMIT(", ".MERGE(",
    )  # fmt: skip
    for marker in write_markers:
        assert marker not in source, marker
