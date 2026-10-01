"""Add durable artifact production cancellation outbox."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0017_production_cancel_outbox"
down_revision: str | None = "0016_artifact_context_freeze"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_production_cancel_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "production_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("workflow_id", sa.String(256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "dispatched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["production_run_id"],
            ["artifact_production_runs.id"],
            name="fk_artifact_production_cancel_requests_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "production_run_id",
            name="uq_artifact_production_cancel_request_run",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'dead_letter')",
            name="ck_artifact_production_cancel_request_status",
        ),
    )
    op.create_index(
        "ix_artifact_production_cancel_requests_pending",
        "artifact_production_cancel_requests",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifact_production_cancel_requests_pending",
        table_name="artifact_production_cancel_requests",
    )
    op.drop_table("artifact_production_cancel_requests")
