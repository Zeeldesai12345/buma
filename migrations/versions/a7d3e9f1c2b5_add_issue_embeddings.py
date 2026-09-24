"""add_issue_embeddings

Revision ID: a7d3e9f1c2b5
Revises: c9f2a1b4e8d3
Create Date: 2026-09-24 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "a7d3e9f1c2b5"
down_revision: Union[str, Sequence[str], None] = "c9f2a1b4e8d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enable pgvector and add issue_embeddings for semantic duplicate detection (T3 / DD-25)."""
    # Requires a Postgres build that ships pgvector (docker-compose uses pgvector/pgvector:pg16).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "issue_embeddings",
        sa.Column("repo_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("issue_state", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("issue_state IN ('open','closed')", name=op.f("ck_issue_embeddings_issue_state")),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repo_config.repo_id"],
            name=op.f("fk_issue_embeddings_repo_id_repo_config"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("repo_id", "issue_number", name=op.f("pk_issue_embeddings")),
    )
    op.create_index(
        "ix_issue_embeddings_embedding_hnsw",
        "issue_embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    """Drop issue_embeddings. The vector extension is left installed (harmless, and other objects may use it)."""
    op.drop_index("ix_issue_embeddings_embedding_hnsw", table_name="issue_embeddings")
    op.drop_table("issue_embeddings")
