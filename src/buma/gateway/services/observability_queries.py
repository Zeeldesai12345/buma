"""
Read-only query functions shared by the observability REST routes and the MCP server (N1 / DD-26).

Every function takes the caller's AsyncSession, only SELECTs, never commits, and returns ORM rows
or plain values — shaping them into a response (REST schema, MCP output) is the caller's job.
There is exactly one query path per dataset; do not copy SQL into routes or MCP tools.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import RowMapping, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from buma.db.models import DeveloperProfile, IssueSnapshot, RepoConfig, TriageDecision

# Maps window → (lookback interval, bucket trunc unit, bucket step interval)
# All strings are compile-time constants — safe to embed directly in SQL via f-string.
PRODUCTIVITY_WINDOWS: dict[str, tuple[str, str, str]] = {
    "7d": ("7 days", "day", "1 day"),
    "30d": ("30 days", "week", "1 week"),
    "90d": ("90 days", "month", "1 month"),
    "all": ("12 months", "month", "1 month"),
}


async def get_repo(db: AsyncSession, repo_id: int) -> RepoConfig | None:
    result = await db.execute(select(RepoConfig).where(RepoConfig.repo_id == repo_id))
    return result.scalar_one_or_none()


async def list_repos(db: AsyncSession) -> list[RepoConfig]:
    result = await db.execute(select(RepoConfig).order_by(RepoConfig.repo_full_name))
    return list(result.scalars().all())


async def get_triage_page(
    db: AsyncSession, repo_id: int, limit: int, offset: int = 0
) -> tuple[list[TriageDecision], int]:
    """Triage decisions for a repo, most recent first, plus the repo's total decision count."""
    count_result = await db.execute(
        select(func.count()).select_from(TriageDecision).where(TriageDecision.repo_id == repo_id)
    )
    total = count_result.scalar_one()

    rows_result = await db.execute(
        select(TriageDecision)
        .where(TriageDecision.repo_id == repo_id)
        .order_by(TriageDecision.decided_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows_result.scalars().all()), total


async def get_issue_titles(db: AsyncSession, repo_id: int, event_ids: Sequence[str]) -> dict[str, str]:
    """Issue title per event_id (from issue_snapshot). Events without a snapshot are omitted."""
    if not event_ids:
        return {}
    result = await db.execute(
        select(IssueSnapshot.event_id, IssueSnapshot.title).where(
            IssueSnapshot.repo_id == repo_id,
            IssueSnapshot.event_id.in_(list(event_ids)),
        )
    )
    return {row.event_id: row.title for row in result}


async def get_workload(db: AsyncSession, repo_id: int) -> list[DeveloperProfile]:
    """Developer profiles for a repo, busiest first."""
    result = await db.execute(
        select(DeveloperProfile)
        .where(DeveloperProfile.repo_id == repo_id)
        .order_by(DeveloperProfile.open_assignments.desc())
    )
    return list(result.scalars().all())


async def get_productivity(
    db: AsyncSession, repo_id: int, window: str
) -> tuple[Sequence[RowMapping], Sequence[RowMapping]]:
    """
    Per-developer aggregates and zero-filled time buckets for `window` (a PRODUCTIVITY_WINDOWS key).

    Returns (aggregate_rows, bucket_rows) as row mappings.
    """
    lookback, trunc_unit, step = PRODUCTIVITY_WINDOWS[window]

    # Aggregate stats per developer over the selected window
    agg_sql = text(f"""
        SELECT
            dp.github_login,
            dp.open_assignments,
            dp.max_capacity,
            COUNT(td.event_id) FILTER (WHERE td.closed_at IS NOT NULL)        AS resolved_count,
            AVG(
                EXTRACT(EPOCH FROM (td.closed_at - td.decided_at)) / 3600.0
            ) FILTER (WHERE td.closed_at IS NOT NULL)                          AS avg_resolution_hours
        FROM developer_profile dp
        LEFT JOIN triage_decision td
               ON td.repo_id = dp.repo_id
              AND td.selected_assignee_login = dp.github_login
              AND td.closed_at >= NOW() - INTERVAL '{lookback}'
        WHERE dp.repo_id = :repo_id
        GROUP BY dp.github_login, dp.open_assignments, dp.max_capacity
        ORDER BY resolved_count DESC
        """)
    agg_result = await db.execute(agg_sql, {"repo_id": repo_id})
    agg_rows = agg_result.mappings().all()

    # Time-series buckets per developer (all periods filled even if zero)
    bucket_sql = text(f"""
        SELECT
            dp.github_login,
            gs.period_start::date                                              AS period_start,
            COUNT(td.event_id)                                                 AS resolved
        FROM developer_profile dp
        CROSS JOIN LATERAL generate_series(
            date_trunc('{trunc_unit}', NOW() - INTERVAL '{lookback}'),
            date_trunc('{trunc_unit}', NOW()),
            INTERVAL '{step}'
        ) AS gs(period_start)
        LEFT JOIN triage_decision td
               ON td.repo_id = dp.repo_id
              AND td.selected_assignee_login = dp.github_login
              AND td.closed_at IS NOT NULL
              AND date_trunc('{trunc_unit}', td.closed_at) = gs.period_start
        WHERE dp.repo_id = :repo_id
        GROUP BY dp.github_login, gs.period_start
        ORDER BY dp.github_login, gs.period_start
        """)
    bucket_result = await db.execute(bucket_sql, {"repo_id": repo_id})
    bucket_rows = bucket_result.mappings().all()

    return agg_rows, bucket_rows


async def get_issue_page(
    db: AsyncSession, repo_id: int, limit: int, offset: int = 0
) -> tuple[list[IssueSnapshot], int]:
    """Issue snapshots for a repo, most recent snapshot first, plus the total count."""
    count_result = await db.execute(
        select(func.count()).select_from(IssueSnapshot).where(IssueSnapshot.repo_id == repo_id)
    )
    total = count_result.scalar_one()

    rows_result = await db.execute(
        select(IssueSnapshot)
        .where(IssueSnapshot.repo_id == repo_id)
        .order_by(IssueSnapshot.snapshot_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows_result.scalars().all()), total
