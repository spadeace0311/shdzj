"""Add QA answer model and execution audit provenance."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0025_qa_audit_provenance"
down_revision: str | None = "0024_ai_knowledge_qa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "qa_answers",
        sa.Column("model_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "qa_answers",
        sa.Column("model_version", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "qa_answers",
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "qa_answers",
        sa.Column(
            "execution_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "qa_answers",
        sa.Column(
            "tool_call_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("qa_answers", "tool_call_summary")
    op.drop_column("qa_answers", "execution_plan")
    op.drop_column("qa_answers", "prompt_version")
    op.drop_column("qa_answers", "model_version")
    op.drop_column("qa_answers", "model_name")
