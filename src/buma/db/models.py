from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from buma.db.base import Base


class WebhookDelivery(Base):
    """
    Tracks each GitHub webhook delivery (ingestion-level idempotency).
    delivery_id should be UNIQUE.
    """

    __tablename__ = "webhook_delivery"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_name: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)

    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)

    received_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'RECEIVED'"))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RepoConfig(Base):
    """
    Stores per-repo configuration used by triage/assignment logic.
    """

    __tablename__ = "repo_config"

    repo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_full_name: Mapped[str] = mapped_column(Text, nullable=False)

    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DeveloperProfile(Base):
    """
    Developer attributes and capacity for assignment.
    """

    __tablename__ = "developer_profile"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repo_config.repo_id", ondelete="CASCADE"), nullable=False
    )

    github_login: Mapped[str] = mapped_column(Text, nullable=False)

    skills: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    max_capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    open_assignments: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Optional optimistic concurrency knob for updates
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("repo_id", "github_login", name="uq_dev_profile_repo_login"),
        Index("ix_dev_profile_repo_open_assignments", "repo_id", "open_assignments"),
    )


class IssueSnapshot(Base):
    """
    Stores the normalized issue snapshot derived from NormalizedEvent.
    event_id should be UNIQUE (one snapshot per event).
    """

    __tablename__ = "issue_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)

    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repo_config.repo_id", ondelete="CASCADE"), nullable=False
    )

    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issue_node_id: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("ARRAY[]::TEXT[]"))

    author_login: Mapped[str] = mapped_column(Text, nullable=False)

    issue_created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    issue_updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)

    snapshot_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index(
            "ix_issue_snapshot_repo_issue_time",
            "repo_id",
            "issue_number",
            text("snapshot_at DESC"),
        ),
    )


EMBEDDING_DIM = 384


class IssueEmbedding(Base):
    """
    One embedding per issue (not per event) for semantic duplicate detection (T3 / DD-25).
    Keyed by (repo_id, issue_number) so re-processing an issue overwrites its row instead of
    creating a second vector that would match itself. `model_version` records which embedding
    model produced the vector — vectors from different models are not comparable.
    """

    __tablename__ = "issue_embeddings"

    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repo_config.repo_id", ondelete="CASCADE"), primary_key=True
    )
    issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    issue_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("issue_state IN ('open','closed')", name="issue_state"),
        Index(
            "ix_issue_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class TriageDecision(Base):
    """
    Stores triage/assignment decision for an event.
    event_id should be UNIQUE (idempotency for decisions).
    """

    __tablename__ = "triage_decision"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)

    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repo_config.repo_id", ondelete="CASCADE"), nullable=False
    )
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)

    decided_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    predicted_priority: Mapped[str | None] = mapped_column(Text, nullable=True)
    predicted_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)

    selected_assignee_login: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    patch_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'DECIDED'"))
    patch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "patch_state IN ('DECIDED','APPLIED','FAILED_RETRY','SKIPPED_DUPLICATE')",
            name="patch_state",
        ),
        Index("ix_triage_decision_repo_issue_time", "repo_id", "issue_number"),
    )


class DLQRecord(Base):
    __tablename__ = "dlq_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    delivery_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # serialised NormalizedEvent
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    error_type: Mapped[str] = mapped_column(Text, nullable=False)  # CLASSIFICATION | ASSIGNMENT | GITHUB_PATCH
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'FAILED'")
    )  # FAILED | REPLAY_REQUESTED | REPLAY_SUCCEEDED | REPLAY_FAILED
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_error_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        Index("ix_dlq_records_status", "status"),
        Index("ix_dlq_records_delivery_id", "delivery_id"),
    )
