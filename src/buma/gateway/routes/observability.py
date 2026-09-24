from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from buma.gateway.deps import get_db, require_session
from buma.gateway.services import observability_queries as queries
from buma.schemas.api.issue import IssueListResponse, IssueSnapshotResponse
from buma.schemas.api.productivity import DeveloperProductivity, ProductivityBucket, ProductivityResponse
from buma.schemas.api.triage import TriageDecisionResponse, TriageHistoryResponse
from buma.schemas.api.workload import DeveloperWorkload, WorkloadResponse

# Query logic lives in buma.gateway.services.observability_queries, shared with the MCP server
# (N1 / DD-26). These routes only handle HTTP concerns: auth, validation, 404s, response shaping.

router = APIRouter(prefix="/api", tags=["observability"])


async def _require_repo(db: AsyncSession, repo_id: int) -> None:
    if await queries.get_repo(db, repo_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Repo {repo_id} not found.")


@router.get("/triage/{repo_id}", response_model=TriageHistoryResponse)
async def triage_history(
    repo_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _session: Annotated[str, Depends(require_session)],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> TriageHistoryResponse:
    await _require_repo(db, repo_id)
    rows, total = await queries.get_triage_page(db, repo_id, limit=limit, offset=offset)
    decisions = [TriageDecisionResponse.model_validate(row, from_attributes=True) for row in rows]
    return TriageHistoryResponse(repo_id=repo_id, decisions=decisions, total=total, limit=limit, offset=offset)


@router.get("/workload/{repo_id}", response_model=WorkloadResponse)
async def workload(
    repo_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _session: Annotated[str, Depends(require_session)],
) -> WorkloadResponse:
    await _require_repo(db, repo_id)
    rows = await queries.get_workload(db, repo_id)
    developers = [DeveloperWorkload.model_validate(row, from_attributes=True) for row in rows]
    return WorkloadResponse(repo_id=repo_id, developers=developers)


@router.get("/productivity/{repo_id}", response_model=ProductivityResponse)
async def productivity(
    repo_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _session: Annotated[str, Depends(require_session)],
    window: str = Query(default="30d", pattern="^(7d|30d|90d|all)$"),
) -> ProductivityResponse:
    await _require_repo(db, repo_id)
    agg_rows, bucket_rows = await queries.get_productivity(db, repo_id, window)

    # Group buckets by developer
    buckets_by_login: dict[str, list[ProductivityBucket]] = {}
    for row in bucket_rows:
        login = row["github_login"]
        buckets_by_login.setdefault(login, []).append(
            ProductivityBucket(period_start=row["period_start"], resolved=row["resolved"])
        )

    developers = [
        DeveloperProductivity(
            github_login=row["github_login"],
            resolved_count=row["resolved_count"] or 0,
            avg_resolution_hours=row["avg_resolution_hours"],
            open_assignments=row["open_assignments"],
            max_capacity=row["max_capacity"],
            buckets=buckets_by_login.get(row["github_login"], []),
        )
        for row in agg_rows
    ]

    return ProductivityResponse(repo_id=repo_id, window=window, developers=developers)


@router.get("/issues/{repo_id}", response_model=IssueListResponse)
async def get_all_issues(
    repo_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    _session: Annotated[str, Depends(require_session)],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> IssueListResponse:
    await _require_repo(db, repo_id)
    rows, total = await queries.get_issue_page(db, repo_id, limit=limit, offset=offset)
    issues = [IssueSnapshotResponse.model_validate(row, from_attributes=True) for row in rows]
    return IssueListResponse(repo_id=repo_id, issues=issues, total=total, limit=limit, offset=offset)
