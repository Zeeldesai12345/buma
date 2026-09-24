from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from buma.db.models import DeveloperProfile, DLQRecord, IssueSnapshot, RepoConfig, TriageDecision
from buma.schemas.normalized_event import NormalizedEvent
from buma.worker.services.assignee_selector import AssigneeSelector
from buma.worker.services.github_client import GitHubClient
from buma.worker.services.triage_engine import TriageEngine, TriageResult

logger = logging.getLogger(__name__)


def _build_explanation(result: TriageResult, assignee_login: str | None) -> str:
    assignee_line = f"@{assignee_login}" if assignee_login else "*no assignee found*"
    return (
        "🤖 **buma triage**\n"
        f"- **Category:** {result.category}\n"
        f"- **Priority:** {result.priority}\n"
        f"- **Assigned to:** {assignee_line}\n"
        f"- **Confidence:** {result.confidence:.0%}\n"
        f"- **Engine version:** {result.engine_version}"
    )


def _build_labels(existing: list[str], category: str, priority: str) -> list[str]:
    """Return existing labels with buma's category and priority appended (deduplicated)."""
    final = list(existing)
    for label in (category, priority):
        if label not in final:
            final.append(label)
    return final


class EventProcessorService:
    """
    Orchestrates the full triage pipeline for a single NormalizedEvent.

    Phases:
    1. Log receipt
    2. Load RepoConfig from DB — skip if repo not enrolled
    3. Classify category + priority (TriageEngine) — skip if not a bug
    4. Assignee selection (skills + capacity + optimistic locking)
    5. Persist IssueSnapshot + TriageDecision
    6. GitHub patch (labels, assignee, explanation comment)
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        triage_engine: TriageEngine | None = None,
        assignee_selector: AssigneeSelector | None = None,
        github_client: GitHubClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._engine = triage_engine or TriageEngine()
        self._selector = assignee_selector or AssigneeSelector()
        self._github_client = github_client

    async def process(self, event: NormalizedEvent) -> None:
        if event.action == "closed":
            await self._handle_closed(event)
            return

        # Phase 1 — log receipt
        logger.info(
            "event_id=%s repo=%s issue=#%d action=%s — received, loading config",
            event.event_id,
            event.repo.full_name,
            event.issue.number,
            event.action,
        )

        # Phase 2 — load RepoConfig
        async with self._session_factory() as session:
            repo_config = await self._load_repo_config(session, event.repo.id)

        if repo_config is None:
            logger.info(
                "event_id=%s repo=%s — not enrolled, skipping",
                event.event_id,
                event.repo.full_name,
            )
            return

        # Phase 3 — classify (rule-based, with optional low-confidence Claude fallback)
        result = await self._engine.classify_with_fallback(event.issue, repo_config.config)

        if result.category != "bug":
            logger.info(
                "event_id=%s repo=%s issue=#%d — category=%s, not a bug, skipping",
                event.event_id,
                event.repo.full_name,
                event.issue.number,
                result.category,
            )
            return

        logger.info(
            "event_id=%s repo=%s issue=#%d — category=%s priority=%s confidence=%.2f engine=%s",
            event.event_id,
            event.repo.full_name,
            event.issue.number,
            result.category,
            result.priority,
            result.confidence,
            result.engine_version,
        )

        # Phase 4 — assignee selection
        # Phase 5 — persist IssueSnapshot + TriageDecision
        # Both phases share one session so their writes commit atomically (DD-20).
        async with self._session_factory() as session:
            assignee_login = await self._selector.select(session, event.repo.id, result.category)

            explanation = _build_explanation(result, assignee_login)
            session.add(
                IssueSnapshot(
                    event_id=event.event_id,
                    delivery_id=event.delivery_id,
                    repo_id=event.repo.id,
                    issue_number=event.issue.number,
                    issue_id=event.issue.id,
                    issue_node_id=event.issue.node_id,
                    title=event.issue.title,
                    body=event.issue.body,
                    labels=event.issue.labels,
                    author_login=event.issue.author_login,
                    issue_created_at=event.issue.created_at,
                    issue_updated_at=event.issue.updated_at,
                )
            )
            session.add(
                TriageDecision(
                    event_id=event.event_id,
                    delivery_id=event.delivery_id,
                    repo_id=event.repo.id,
                    issue_number=event.issue.number,
                    predicted_category=result.category,
                    predicted_priority=result.priority,
                    confidence=result.confidence,
                    selected_assignee_login=assignee_login,
                    explanation=explanation,
                )
            )

            try:
                await session.commit()
            except IntegrityError:
                logger.info(
                    "event_id=%s — already persisted (duplicate event_id), skipping",
                    event.event_id,
                )
                await session.rollback()
                return

        logger.info(
            "event_id=%s repo=%s issue=#%d — assignee=%s",
            event.event_id,
            event.repo.full_name,
            event.issue.number,
            assignee_login or "none",
        )

        # Phase 6 — GitHub patch
        if self._github_client is None:
            logger.warning(
                "event_id=%s — github_client not configured, skipping patch",
                event.event_id,
            )
            return

        owner, repo_name = event.repo.full_name.split("/", 1)
        final_labels = _build_labels(event.issue.labels, result.category, result.priority)

        try:
            token = await self._github_client.get_installation_token(event.installation_id)
            await self._github_client.patch_issue(
                token, owner, repo_name, event.issue.number, final_labels, assignee_login
            )
            await self._github_client.post_comment(token, owner, repo_name, event.issue.number, explanation)
        except httpx.HTTPStatusError as exc:
            await self._handle_patch_error(event, exc)
            return

        async with self._session_factory() as session:
            await session.execute(
                update(TriageDecision).where(TriageDecision.event_id == event.event_id).values(patch_state="APPLIED")
            )
            await session.commit()

        logger.info(
            "event_id=%s repo=%s issue=#%d — patch APPLIED",
            event.event_id,
            event.repo.full_name,
            event.issue.number,
        )

    async def _handle_closed(self, event: NormalizedEvent) -> None:
        """Handle issues.closed — set closed_at and decrement assignee open_assignments."""
        logger.info(
            "event_id=%s repo=%s issue=#%d action=closed — received, loading config",
            event.event_id,
            event.repo.full_name,
            event.issue.number,
        )

        async with self._session_factory() as session:
            repo_config = await self._load_repo_config(session, event.repo.id)

        if repo_config is None:
            logger.info(
                "event_id=%s repo=%s — not enrolled, skipping closed event",
                event.event_id,
                event.repo.full_name,
            )
            return

        async with self._session_factory() as session:
            result = await session.execute(
                select(TriageDecision)
                .where(
                    TriageDecision.repo_id == event.repo.id,
                    TriageDecision.issue_number == event.issue.number,
                )
                .order_by(TriageDecision.decided_at.desc())
                .limit(1)
            )
            decision = result.scalar_one_or_none()

            if decision is None:
                logger.info(
                    "event_id=%s repo=%s issue=#%d — no triage decision found, skipping closed event",
                    event.event_id,
                    event.repo.full_name,
                    event.issue.number,
                )
                return

            decision.closed_at = datetime.now(UTC)

            if decision.selected_assignee_login:
                await session.execute(
                    update(DeveloperProfile)
                    .where(
                        DeveloperProfile.repo_id == event.repo.id,
                        DeveloperProfile.github_login == decision.selected_assignee_login,
                        DeveloperProfile.open_assignments > 0,
                    )
                    .values(open_assignments=DeveloperProfile.open_assignments - 1)
                )

            await session.commit()

        logger.info(
            "event_id=%s repo=%s issue=#%d assignee=%s — closed, open_assignments decremented",
            event.event_id,
            event.repo.full_name,
            event.issue.number,
            decision.selected_assignee_login or "none",
        )

    async def _handle_patch_error(self, event: NormalizedEvent, exc: httpx.HTTPStatusError) -> None:
        status = exc.response.status_code
        error_message = f"HTTP {status}: {exc.response.text[:200]}"
        is_transient = status == 429 or status >= 500

        logger.warning(
            "event_id=%s — GitHub patch failed (status=%d transient=%s): %s",
            event.event_id,
            status,
            is_transient,
            error_message,
        )

        async with self._session_factory() as session:
            await session.execute(
                update(TriageDecision)
                .where(TriageDecision.event_id == event.event_id)
                .values(
                    patch_state="FAILED_RETRY",
                    patch_attempts=TriageDecision.patch_attempts + 1,
                    last_error=error_message,
                )
            )
            if not is_transient:
                session.add(
                    DLQRecord(
                        event_id=event.event_id,
                        delivery_id=event.delivery_id,
                        event_payload=event.model_dump(mode="json"),
                        error_message=error_message,
                        error_type="GITHUB_PATCH",
                    )
                )
            await session.commit()

    async def _load_repo_config(self, session: AsyncSession, repo_id: int) -> RepoConfig | None:
        result = await session.execute(select(RepoConfig).where(RepoConfig.repo_id == repo_id))
        return result.scalar_one_or_none()
