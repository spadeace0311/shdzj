"""Add event lifecycle fields and the assessment outbox."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_event_lifecycle"
down_revision: str | None = "0004_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "raw_messages",
        sa.Column("provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "raw_messages",
        sa.Column("ingest_lane", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_raw_messages_provider", "raw_messages", ["provider"])
    op.create_index("ix_raw_messages_ingest_lane", "raw_messages", ["ingest_lane"])

    op.add_column(
        "earthquake_events",
        sa.Column("t1_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "earthquake_events",
        sa.Column(
            "lifecycle_state",
            sa.String(length=32),
            server_default=sa.text("'auto_pending'"),
            nullable=False,
        ),
    )
    op.add_column(
        "earthquake_events",
        sa.Column("latest_trigger_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_earthquake_events_t1_at", "earthquake_events", ["t1_at"])
    op.create_index(
        "ix_earthquake_events_lifecycle_state",
        "earthquake_events",
        ["lifecycle_state"],
    )

    op.add_column(
        "earthquake_revisions",
        sa.Column("semantic_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("ingest_lane", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("inside_shanghai", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("distance_to_boundary_km", sa.Numeric(precision=10, scale=3), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("region_boundary_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "earthquake_revisions",
        sa.Column("region_computed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_earthquake_revisions_semantic_fingerprint",
        "earthquake_revisions",
        ["semantic_fingerprint"],
    )
    op.create_index(
        "ix_earthquake_revisions_provider",
        "earthquake_revisions",
        ["provider"],
    )
    op.create_index(
        "ix_earthquake_revisions_ingest_lane",
        "earthquake_revisions",
        ["ingest_lane"],
    )
    op.create_index(
        "ix_earthquake_revisions_ingested_at",
        "earthquake_revisions",
        ["ingested_at"],
    )
    op.create_index(
        "ix_earthquake_revisions_region_boundary_version",
        "earthquake_revisions",
        ["region_boundary_version"],
    )
    op.create_index(
        "uq_earthquake_revisions_semantic_fingerprint",
        "earthquake_revisions",
        ["event_id", "semantic_fingerprint"],
        unique=True,
        postgresql_where=sa.text("semantic_fingerprint IS NOT NULL"),
    )

    op.create_table(
        "event_lifecycle_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_type", sa.String(length=64), nullable=False),
        sa.Column("trigger_reason", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["earthquake_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "revision_id",
            "trigger_type",
            name="uq_event_lifecycle_outbox_event_revision_type",
        ),
    )
    op.create_index(
        "ix_event_lifecycle_outbox_event_id",
        "event_lifecycle_outbox",
        ["event_id"],
    )
    op.create_index(
        "ix_event_lifecycle_outbox_revision_id",
        "event_lifecycle_outbox",
        ["revision_id"],
    )
    op.create_index(
        "ix_event_lifecycle_outbox_status",
        "event_lifecycle_outbox",
        ["status"],
    )
    op.create_index(
        "ix_event_lifecycle_outbox_pending",
        "event_lifecycle_outbox",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_lifecycle_outbox_pending",
        table_name="event_lifecycle_outbox",
    )
    op.drop_index(
        "ix_event_lifecycle_outbox_status",
        table_name="event_lifecycle_outbox",
    )
    op.drop_index(
        "ix_event_lifecycle_outbox_revision_id",
        table_name="event_lifecycle_outbox",
    )
    op.drop_index(
        "ix_event_lifecycle_outbox_event_id",
        table_name="event_lifecycle_outbox",
    )
    op.drop_table("event_lifecycle_outbox")

    op.drop_index(
        "uq_earthquake_revisions_semantic_fingerprint",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_region_boundary_version",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_ingested_at",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_ingest_lane",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_provider",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_semantic_fingerprint",
        table_name="earthquake_revisions",
    )
    op.drop_column("earthquake_revisions", "region_computed_at")
    op.drop_column("earthquake_revisions", "region_boundary_version")
    op.drop_column("earthquake_revisions", "distance_to_boundary_km")
    op.drop_column("earthquake_revisions", "inside_shanghai")
    op.drop_column("earthquake_revisions", "ingested_at")
    op.drop_column("earthquake_revisions", "ingest_lane")
    op.drop_column("earthquake_revisions", "provider")
    op.drop_column("earthquake_revisions", "semantic_fingerprint")

    op.drop_index(
        "ix_earthquake_events_lifecycle_state",
        table_name="earthquake_events",
    )
    op.drop_index("ix_earthquake_events_t1_at", table_name="earthquake_events")
    op.drop_column("earthquake_events", "latest_trigger_revision_id")
    op.drop_column("earthquake_events", "lifecycle_state")
    op.drop_column("earthquake_events", "t1_at")

    op.drop_index("ix_raw_messages_ingest_lane", table_name="raw_messages")
    op.drop_index("ix_raw_messages_provider", table_name="raw_messages")
    op.drop_column("raw_messages", "ingest_lane")
    op.drop_column("raw_messages", "provider")
