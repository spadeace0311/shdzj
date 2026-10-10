"""Add activation time and collaboration marker provenance."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0027_operations_safety_hardening"
down_revision: str | None = "0026_operations_automation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "earthquake_events",
        sa.Column("real_event_activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "earthquake_events",
        sa.Column("real_event_closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_earthquake_events_real_event_activated_at",
        "earthquake_events",
        ["real_event_activated_at"],
        unique=False,
    )
    op.create_index(
        "ix_earthquake_events_real_event_closed_at",
        "earthquake_events",
        ["real_event_closed_at"],
        unique=False,
    )

    op.add_column(
        "collaboration_deliverable_versions",
        sa.Column("operation_marker", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "collaboration_deliverable_publications",
        sa.Column("operation_marker", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        "ck_collaboration_deliverable_operation_marker",
        "collaboration_deliverable_versions",
        "operation_marker IS NULL OR operation_marker IN ('test', 'drill')",
    )
    op.create_check_constraint(
        "ck_collaboration_publication_operation_marker",
        "collaboration_deliverable_publications",
        "operation_marker IS NULL OR operation_marker IN ('test', 'drill')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_collaboration_publication_operation_marker",
        "collaboration_deliverable_publications",
        type_="check",
    )
    op.drop_constraint(
        "ck_collaboration_deliverable_operation_marker",
        "collaboration_deliverable_versions",
        type_="check",
    )
    op.drop_column("collaboration_deliverable_publications", "operation_marker")
    op.drop_column("collaboration_deliverable_versions", "operation_marker")

    op.drop_index(
        "ix_earthquake_events_real_event_closed_at",
        table_name="earthquake_events",
    )
    op.drop_index(
        "ix_earthquake_events_real_event_activated_at",
        table_name="earthquake_events",
    )
    op.drop_column("earthquake_events", "real_event_closed_at")
    op.drop_column("earthquake_events", "real_event_activated_at")
