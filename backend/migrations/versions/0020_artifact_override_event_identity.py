"""Add explicit event identity to artifact override requests."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0020_override_event"
down_revision: str | None = "0019_event_purge_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID_TYPE = postgresql.UUID(as_uuid=True)
EVENT_ENDPOINT_PATTERN = (
    r"^/api/v1/events/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/artifacts/"
)


def upgrade() -> None:
    op.add_column(
        "artifact_override_requests",
        sa.Column("event_id", UUID_TYPE, nullable=True),
    )
    op.execute(
        sa.text(
            f"""
            UPDATE artifact_override_requests AS request
            SET event_id = event.id
            FROM earthquake_events AS event
            WHERE request.event_id IS NULL
              AND request.endpoint ~* '{EVENT_ENDPOINT_PATTERN}'
              AND event.id = (
                  substring(request.endpoint from '{EVENT_ENDPOINT_PATTERN}')
              )::uuid
            """
        )
    )
    op.create_foreign_key(
        "fk_artifact_override_requests_event",
        "artifact_override_requests",
        "earthquake_events",
        ["event_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_artifact_override_requests_event_id",
        "artifact_override_requests",
        ["event_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifact_override_requests_event_id",
        table_name="artifact_override_requests",
    )
    op.drop_constraint(
        "fk_artifact_override_requests_event",
        "artifact_override_requests",
        type_="foreignkey",
    )
    op.drop_column("artifact_override_requests", "event_id")
