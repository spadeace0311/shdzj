"""Add collector runtime state and dead-letter tables."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_collector_runtime"
down_revision: str | None = "0005_event_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collector_runtime_state",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "connected",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_processed_source_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reconnect_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("provider"),
    )
    op.create_index(
        "ix_collector_runtime_state_state",
        "collector_runtime_state",
        ["state"],
    )

    op.create_table(
        "collector_dead_letters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collector_dead_letters_provider",
        "collector_dead_letters",
        ["provider"],
    )
    op.create_index(
        "ix_collector_dead_letters_lane",
        "collector_dead_letters",
        ["lane"],
    )
    op.create_index(
        "ix_collector_dead_letters_source_message_id",
        "collector_dead_letters",
        ["source_message_id"],
    )
    op.create_index(
        "ix_collector_dead_letters_error_category",
        "collector_dead_letters",
        ["error_category"],
    )
    op.create_index(
        "ix_collector_dead_letters_status",
        "collector_dead_letters",
        ["status"],
    )
    op.create_index(
        "ix_collector_dead_letters_last_failed_at",
        "collector_dead_letters",
        ["last_failed_at"],
    )
    op.create_index(
        "ix_collector_dead_letters_open",
        "collector_dead_letters",
        ["status", "last_failed_at"],
    )


def downgrade() -> None:
    op.drop_table("collector_dead_letters")
    op.drop_table("collector_runtime_state")
