"""Bind cleanup intents to an immutable artifact storage namespace."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0022_cleanup_namespace"
down_revision: str | None = "0021_cleanup_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_purge_receipts",
        sa.Column("storage_namespace", sa.String(length=128), nullable=True),
    )
    op.execute(
        """
        UPDATE event_purge_receipts
        SET storage_namespace = 'legacy-namespace-unavailable'
        WHERE storage_namespace IS NULL
           OR btrim(storage_namespace) = ''
        """
    )
    op.add_column(
        "event_object_cleanup_intents",
        sa.Column("storage_namespace", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "event_object_cleanup_intents",
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
    )
    op.drop_constraint(
        "ck_event_object_cleanup_status",
        "event_object_cleanup_intents",
        type_="check",
    )
    op.execute(
        """
        UPDATE event_object_cleanup_intents
        SET status = 'unprocessable',
            last_error = 'storage namespace unavailable; manual remediation required',
            lease_owner = NULL,
            lease_expires_at = NULL,
            updated_at = now()
        WHERE (
            storage_namespace IS NULL
            OR btrim(storage_namespace) = ''
        )
          AND status IN ('pending', 'processing')
        """
    )
    op.create_check_constraint(
        "ck_event_object_cleanup_status",
        "event_object_cleanup_intents",
        "status IN ("
        "'pending', 'processing', 'completed', 'skipped', "
        "'dead_letter', 'unprocessable'"
        ")",
    )
    op.drop_constraint(
        "uq_event_object_cleanup_source_path",
        "event_object_cleanup_intents",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_event_object_cleanup_namespace_source_path",
        "event_object_cleanup_intents",
        ["storage_namespace", "source_kind", "source_key", "storage_path"],
    )
    op.drop_index(
        "ix_event_object_cleanup_claim",
        table_name="event_object_cleanup_intents",
    )
    op.create_index(
        "ix_event_object_cleanup_claim",
        "event_object_cleanup_intents",
        ["storage_namespace", "status", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_column("event_purge_receipts", "storage_namespace")
    op.drop_index(
        "ix_event_object_cleanup_claim",
        table_name="event_object_cleanup_intents",
    )
    op.execute(
        """
        UPDATE event_object_cleanup_intents
        SET status = 'skipped',
            last_error = COALESCE(
                NULLIF(btrim(last_error), ''),
                'storage namespace metadata unavailable; manual remediation required'
            ),
            lease_owner = NULL,
            lease_expires_at = NULL,
            updated_at = now()
        WHERE status IN ('dead_letter', 'unprocessable')
        """
    )
    op.drop_constraint(
        "ck_event_object_cleanup_status",
        "event_object_cleanup_intents",
        type_="check",
    )
    op.create_check_constraint(
        "ck_event_object_cleanup_status",
        "event_object_cleanup_intents",
        "status IN ('pending', 'processing', 'completed', 'skipped')",
    )
    op.drop_constraint(
        "uq_event_object_cleanup_namespace_source_path",
        "event_object_cleanup_intents",
        type_="unique",
    )
    op.execute(
        """
        DELETE FROM event_object_cleanup_intents AS older
        USING event_object_cleanup_intents AS newer
        WHERE older.id < newer.id
          AND older.source_kind = newer.source_kind
          AND older.source_key = newer.source_key
          AND older.storage_path = newer.storage_path
        """
    )
    op.create_unique_constraint(
        "uq_event_object_cleanup_source_path",
        "event_object_cleanup_intents",
        ["source_kind", "source_key", "storage_path"],
    )
    op.drop_column("event_object_cleanup_intents", "max_attempts")
    op.drop_column("event_object_cleanup_intents", "storage_namespace")
    op.create_index(
        "ix_event_object_cleanup_claim",
        "event_object_cleanup_intents",
        ["status", "lease_expires_at"],
        unique=False,
    )
