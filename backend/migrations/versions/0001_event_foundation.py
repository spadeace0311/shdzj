"""Create raw message, earthquake event, and revision tables."""

from collections.abc import Sequence

from alembic import op
from geoalchemy2 import Geometry
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_event_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "raw_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=True),
        sa.Column("message_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checksum", name="uq_raw_messages_checksum"),
    )
    op.create_index("ix_raw_messages_message_kind", "raw_messages", ["message_kind"])
    op.create_index("ix_raw_messages_received_at", "raw_messages", ["received_at"])
    op.create_index("ix_raw_messages_source", "raw_messages", ["source"])
    op.create_index(
        "ix_raw_messages_source_message_id",
        "raw_messages",
        ["source_message_id"],
    )

    op.create_table(
        "earthquake_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("canonical_source_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("origin_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("depth_km", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("magnitude", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("place", sa.String(length=256), nullable=False),
        sa.Column(
            "geom",
            Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("institutional_level", sa.String(length=32), nullable=True),
        sa.Column("service_level", sa.Integer(), nullable=True),
        sa.Column("response_suggestion", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_rule_version", sa.String(length=32), nullable=True),
        sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_earthquake_events_canonical_source_id",
        "earthquake_events",
        ["canonical_source_id"],
        unique=True,
    )
    op.create_index("ix_earthquake_events_event_type", "earthquake_events", ["event_type"])
    op.create_index(
        "ix_earthquake_events_institutional_level", "earthquake_events", ["institutional_level"]
    )
    op.create_index("ix_earthquake_events_magnitude", "earthquake_events", ["magnitude"])
    op.create_index("ix_earthquake_events_origin_time", "earthquake_events", ["origin_time"])
    op.create_index("ix_earthquake_events_service_level", "earthquake_events", ["service_level"])
    op.create_index("ix_earthquake_events_source", "earthquake_events", ["source"])
    op.create_index(
        "ix_earthquake_events_geom",
        "earthquake_events",
        ["geom"],
        postgresql_using="gist",
    )

    op.create_table(
        "earthquake_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("revision_kind", sa.String(length=32), nullable=False),
        sa.Column("source_event_id", sa.String(length=128), nullable=True),
        sa.Column("origin_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("depth_km", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("magnitude", sa.Numeric(precision=4, scale=1), nullable=False),
        sa.Column("place", sa.String(length=256), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["raw_message_id"], ["raw_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "revision_no",
            name="uq_earthquake_revisions_event_revision",
        ),
    )
    op.create_index("ix_earthquake_revisions_event_id", "earthquake_revisions", ["event_id"])
    op.create_index(
        "ix_earthquake_revisions_is_current",
        "earthquake_revisions",
        ["is_current"],
    )
    op.create_index(
        "ix_earthquake_revisions_raw_message_id",
        "earthquake_revisions",
        ["raw_message_id"],
        unique=True,
    )
    op.create_index(
        "ix_earthquake_revisions_revision_kind",
        "earthquake_revisions",
        ["revision_kind"],
    )
    op.create_index(
        "ix_earthquake_revisions_source_event_id",
        "earthquake_revisions",
        ["source_event_id"],
    )
    op.create_index(
        "uq_earthquake_revisions_event_current",
        "earthquake_revisions",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_earthquake_revisions_event_current",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_source_event_id",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_revision_kind",
        table_name="earthquake_revisions",
    )
    op.drop_index(
        "ix_earthquake_revisions_raw_message_id",
        table_name="earthquake_revisions",
    )
    op.drop_index("ix_earthquake_revisions_is_current", table_name="earthquake_revisions")
    op.drop_index("ix_earthquake_revisions_event_id", table_name="earthquake_revisions")
    op.drop_table("earthquake_revisions")

    op.drop_index("ix_earthquake_events_geom", table_name="earthquake_events")
    op.drop_index("ix_earthquake_events_source", table_name="earthquake_events")
    op.drop_index("ix_earthquake_events_service_level", table_name="earthquake_events")
    op.drop_index("ix_earthquake_events_origin_time", table_name="earthquake_events")
    op.drop_index("ix_earthquake_events_magnitude", table_name="earthquake_events")
    op.drop_index(
        "ix_earthquake_events_institutional_level",
        table_name="earthquake_events",
    )
    op.drop_index("ix_earthquake_events_event_type", table_name="earthquake_events")
    op.drop_index(
        "ix_earthquake_events_canonical_source_id",
        table_name="earthquake_events",
    )
    op.drop_table("earthquake_events")

    op.drop_index(
        "ix_raw_messages_source_message_id",
        table_name="raw_messages",
    )
    op.drop_index("ix_raw_messages_source", table_name="raw_messages")
    op.drop_index("ix_raw_messages_received_at", table_name="raw_messages")
    op.drop_index("ix_raw_messages_message_kind", table_name="raw_messages")
    op.drop_table("raw_messages")
