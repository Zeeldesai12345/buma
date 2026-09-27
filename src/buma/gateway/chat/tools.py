"""
Read-only tools the chat assistant's model can call (DD-27).

Security boundary:
- The repo is fixed by the caller in ToolContext (from the URL the user is authorised for), never
  taken from model arguments — no tool accepts a repo_id.
- Every tool only reads, through buma.gateway.services.observability_queries, on a session whose
  transaction the caller has set READ ONLY.
- GitHub-authored or GitHub-derived text is returned only through untrusted_text() in untrusted_*
  fields, with DATA_NOTICE alongside, exactly as the MCP server does (DD-26).
- Model-supplied arguments are validated by pydantic before any query runs; invalid input raises
  ToolInputError, which the agent returns to the model as an is_error tool_result.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from buma.gateway.services import observability_queries as queries
from buma.mcp_server.untrusted import DATA_NOTICE, untrusted_text
from buma.worker.services.embedding_service import EmbeddingService

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 200
EXCERPT_MAX_CHARS = 300
BODY_MAX_CHARS = 1500
EXPLANATION_MAX_CHARS = 500
LABEL_MAX_CHARS = 50
MAX_LABELS = 20
MAX_SKILLS = 20


class ToolInputError(ValueError):
    """The model called an unknown tool or passed arguments that failed validation."""


@dataclass
class ToolContext:
    db: AsyncSession
    repo_id: int
    embedder: EmbeddingService | None
    # issue_number -> sanitized title for every issue any tool returned; used to build citations.
    seen_issues: dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Inputs — also the source of each tool's input_schema, so the two cannot drift
# ---------------------------------------------------------------------------


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchIssuesInput(_Input):
    query: str = Field(
        min_length=1,
        max_length=300,
        description="What to look for, in plain words, e.g. 'login page crashes on Safari'.",
    )
    limit: int = Field(default=5, ge=1, le=10, description="How many issues to return (1-10).")


class GetIssueInput(_Input):
    issue_number: int = Field(ge=1, description="The GitHub issue number, e.g. 123 for #123.")


class RecentTriageInput(_Input):
    limit: int = Field(default=10, ge=1, le=20, description="How many recent decisions to return (1-20).")


class WorkloadInput(_Input):
    pass


class ProductivityInput(_Input):
    window: Literal["7d", "30d", "90d", "all"] = Field(
        default="30d", description="Look-back window for resolved-issue counts."
    )


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


_TOOLS: list[tuple[str, type[_Input], str]] = [
    (
        "search_issues",
        SearchIssuesInput,
        "Find issues in this repository by topic or symptom. Uses semantic (meaning-based) search "
        "when available, otherwise keyword search; `search_mode` in the result says which ran. "
        "Returns each issue's number, title, a short excerpt and its latest triage decision. "
        "Results are ranked, not filtered: check each title/excerpt before calling it relevant.",
    ),
    (
        "get_issue",
        GetIssueInput,
        "Full details of one issue: title, body (truncated), labels, author, dates, and Buma's latest "
        "triage decision (category, priority, confidence, assignee, explanation, closed_at).",
    ),
    (
        "get_recent_triage",
        RecentTriageInput,
        "Buma's most recent triage decisions in this repository, newest first, with the total "
        "number of decisions ever made.",
    ),
    (
        "get_workload",
        WorkloadInput,
        "Each developer's current open assignments, capacity, spare capacity and skills.",
    ),
    (
        "get_productivity",
        ProductivityInput,
        "Per-developer resolved-issue counts and average resolution time (hours) over a window.",
    ),
]

# Stable order and content: the tool list is part of the request prefix.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {"name": name, "description": description, "input_schema": _schema(model), "eager_input_streaming": True}
    for name, model, description in _TOOLS
]
_INPUT_MODELS: dict[str, type[_Input]] = {name: model for name, model, _ in _TOOLS}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def execute_tool(name: str, raw_input: object, ctx: ToolContext) -> str:
    """
    Run one tool and return its result as a JSON string for a tool_result block.
    Raises ToolInputError for an unknown tool or invalid arguments; database errors propagate.
    """
    model = _INPUT_MODELS.get(name)
    if model is None:
        raise ToolInputError(f"Unknown tool {name!r}.")
    if not isinstance(raw_input, dict):
        raise ToolInputError("Tool input must be a JSON object.")
    try:
        args = model.model_validate(raw_input)
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
        raise ToolInputError(f"Invalid input for {name}: {errors}") from exc

    handler = _HANDLERS[name]
    result = await handler(args, ctx)
    return json.dumps(result, default=_json_default)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serialisable: {type(value).__name__}")


def _title(ctx: ToolContext, issue_number: int, raw_title: str | None) -> dict | None:
    wrapped = untrusted_text(raw_title, TITLE_MAX_CHARS, single_line=True)
    if wrapped is not None:
        ctx.seen_issues[issue_number] = wrapped.text
    return wrapped.model_dump() if wrapped else None


def _untrusted(value: str | None, max_chars: int, single_line: bool = False) -> dict | None:
    wrapped = untrusted_text(value, max_chars, single_line=single_line)
    return wrapped.model_dump() if wrapped else None


def _decision_summary(decision: Any) -> dict | None:
    if decision is None:
        return None
    return {
        "category": decision.predicted_category,
        "priority": decision.predicted_priority,
        "assignee": decision.selected_assignee_login,
        "decided_at": decision.decided_at,
        "closed_at": decision.closed_at,
    }


async def _search_issues(args: SearchIssuesInput, ctx: ToolContext) -> dict:
    vector: list[float] | None = None
    if ctx.embedder is not None:
        try:
            vector = await ctx.embedder.embed_query(args.query)
        except Exception:
            # Only embedding failures degrade to keyword search; database errors propagate.
            logger.exception("repo_id=%d — query embedding failed, falling back to keyword search", ctx.repo_id)

    hits: list[tuple[int, str | None, float | None]]
    if vector is not None:
        mode = "semantic"
        hits = list(
            await queries.search_issues_by_embedding(
                ctx.db, ctx.repo_id, ctx.embedder.model_version, vector, args.limit
            )
        )
    else:
        mode = "keyword"
        numbers = await queries.search_issues_by_keyword(ctx.db, ctx.repo_id, args.query, args.limit)
        hits = [(number, None, None) for number in numbers]

    numbers = [number for number, _, _ in hits]
    snapshots = await queries.get_latest_snapshots(ctx.db, ctx.repo_id, numbers)
    decisions = await queries.get_latest_decisions(ctx.db, ctx.repo_id, numbers)

    results = []
    for number, state, similarity in hits:
        snapshot = snapshots.get(number)
        results.append(
            {
                "issue_number": number,
                "state": state,
                "similarity": round(similarity, 3) if similarity is not None else None,
                "untrusted_title": _title(ctx, number, snapshot.title if snapshot else None),
                "untrusted_excerpt": _untrusted(snapshot.body if snapshot else None, EXCERPT_MAX_CHARS, True),
                "triage": _decision_summary(decisions.get(number)),
            }
        )
    return {"search_mode": mode, "returned": len(results), "results": results, "data_notice": DATA_NOTICE}


async def _get_issue(args: GetIssueInput, ctx: ToolContext) -> dict:
    number = args.issue_number
    snapshot = (await queries.get_latest_snapshots(ctx.db, ctx.repo_id, [number])).get(number)
    decision = (await queries.get_latest_decisions(ctx.db, ctx.repo_id, [number])).get(number)
    if snapshot is None and decision is None:
        return {"issue_number": number, "found": False, "message": f"Buma has no record of issue #{number}."}

    triage = None
    if decision is not None:
        triage = {
            **_decision_summary(decision),
            "confidence": decision.confidence,
            "patch_state": decision.patch_state,
            "untrusted_explanation": _untrusted(decision.explanation, EXPLANATION_MAX_CHARS),
        }
    return {
        "issue_number": number,
        "found": True,
        "untrusted_title": _title(ctx, number, snapshot.title if snapshot else None),
        "untrusted_body": _untrusted(snapshot.body if snapshot else None, BODY_MAX_CHARS),
        "untrusted_labels": [
            untrusted_text(str(label), LABEL_MAX_CHARS, single_line=True).text
            for label in (snapshot.labels if snapshot else [])[:MAX_LABELS]
        ],
        "author": snapshot.author_login if snapshot else None,
        "created_at": snapshot.issue_created_at if snapshot else None,
        "updated_at": snapshot.issue_updated_at if snapshot else None,
        "triage": triage,
        "data_notice": DATA_NOTICE,
    }


async def _get_recent_triage(args: RecentTriageInput, ctx: ToolContext) -> dict:
    rows, total = await queries.get_triage_page(ctx.db, ctx.repo_id, limit=args.limit)
    titles = await queries.get_issue_titles(ctx.db, ctx.repo_id, [row.event_id for row in rows])
    decisions = [
        {
            "issue_number": row.issue_number,
            "untrusted_title": _title(ctx, row.issue_number, titles.get(row.event_id)),
            **_decision_summary(row),
            "confidence": row.confidence,
            "patch_state": row.patch_state,
        }
        for row in rows
    ]
    return {"total_decisions": total, "returned": len(decisions), "decisions": decisions, "data_notice": DATA_NOTICE}


async def _get_workload(_args: WorkloadInput, ctx: ToolContext) -> dict:
    profiles = await queries.get_workload(ctx.db, ctx.repo_id)
    developers = [
        {
            "github_login": p.github_login,
            "untrusted_skills": [
                untrusted_text(str(s), LABEL_MAX_CHARS, single_line=True).text for s in p.skills[:MAX_SKILLS]
            ],
            "open_assignments": p.open_assignments,
            "max_capacity": p.max_capacity,
            "available_capacity": max(0, p.max_capacity - p.open_assignments),
            "at_capacity": p.open_assignments >= p.max_capacity,
        }
        for p in profiles
    ]
    return {"developers": developers, "data_notice": DATA_NOTICE}


async def _get_productivity(args: ProductivityInput, ctx: ToolContext) -> dict:
    agg_rows, _buckets = await queries.get_productivity(ctx.db, ctx.repo_id, args.window)
    developers = [
        {
            "github_login": row["github_login"],
            "resolved_count": row["resolved_count"] or 0,
            "avg_resolution_hours": (
                round(float(row["avg_resolution_hours"]), 1) if row["avg_resolution_hours"] is not None else None
            ),
            "open_assignments": row["open_assignments"],
            "max_capacity": row["max_capacity"],
        }
        for row in agg_rows
    ]
    return {"window": args.window, "developers": developers}


_HANDLERS = {
    "search_issues": _search_issues,
    "get_issue": _get_issue,
    "get_recent_triage": _get_recent_triage,
    "get_workload": _get_workload,
    "get_productivity": _get_productivity,
}
