"""Add knowledge ingestion and grounded QA persistence tables."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0024_ai_knowledge_qa"
down_revision: str | None = "0023_workgroup_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID_TYPE = postgresql.UUID(as_uuid=True)
JSONB_TYPE = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "knowledge_sources",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_key", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("layer", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column(
            "access_level",
            sa.String(length=32),
            server_default=sa.text("'internal'"),
            nullable=False,
        ),
        sa.Column("origin", sa.String(length=2048), nullable=True),
        sa.Column(
            "allow_online_refresh",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "access_level IN ('public', 'internal', 'restricted')",
            name="ck_knowledge_sources_access_level",
        ),
        sa.CheckConstraint(
            "layer IN ('local_authority', 'structured_live', 'public_reference')",
            name="ck_knowledge_sources_layer",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key", name="uq_knowledge_sources_source_key"),
    )

    op.create_table(
        "qa_admin_audit_logs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=True),
        sa.Column("answer_id", UUID_TYPE, nullable=True),
        sa.Column(
            "details",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_qa_admin_audit_logs_actor_created",
        "qa_admin_audit_logs",
        ["actor", "created_at"],
    )
    op.create_index(
        "ix_qa_admin_audit_logs_answer_id",
        "qa_admin_audit_logs",
        ["answer_id"],
    )
    op.create_index(
        "ix_qa_admin_audit_logs_event_id",
        "qa_admin_audit_logs",
        ["event_id"],
    )
    op.create_index(
        "ix_qa_admin_audit_logs_resource",
        "qa_admin_audit_logs",
        ["resource_type", "resource_id"],
    )

    op.create_table(
        "knowledge_source_versions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_id", UUID_TYPE, nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'registered'"),
            nullable=False,
        ),
        sa.Column("file_name", sa.String(length=512), nullable=True),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("parsed_text_path", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "metadata",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "parse_manifest",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('registered', 'uploaded', 'parsing', 'parsed', "
            "'embedding', 'indexed', 'published', 'failed', 'disabled')",
            name="ck_knowledge_source_versions_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["knowledge_sources.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "version",
            name="uq_knowledge_source_versions_source_version",
        ),
    )
    op.create_index(
        op.f("ix_knowledge_source_versions_source_id"),
        "knowledge_source_versions",
        ["source_id"],
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("version_id", UUID_TYPE, nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column(
            "section_path",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("page_from", sa.Integer(), nullable=True),
        sa.Column("page_to", sa.Integer(), nullable=True),
        sa.Column("table_range", JSONB_TYPE, nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("qdrant_point_id", UUID_TYPE, nullable=True),
        sa.Column(
            "metadata",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["knowledge_source_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "version_id",
            "chunk_no",
            name="uq_knowledge_chunks_version_chunk",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_search_trgm",
        "knowledge_chunks",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index(
        op.f("ix_knowledge_chunks_version_id"),
        "knowledge_chunks",
        ["version_id"],
    )

    op.create_table(
        "knowledge_index_versions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("source_version_id", UUID_TYPE, nullable=False),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'indexed'"),
            nullable=False,
        ),
        sa.Column("collection_name", sa.String(length=256), nullable=False),
        sa.Column("embedding_model", sa.String(length=256), nullable=False),
        sa.Column("reranker_model", sa.String(length=256), nullable=False),
        sa.Column(
            "chunk_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "manifest",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('registered', 'uploaded', 'parsing', 'parsed', "
            "'embedding', 'indexed', 'published', 'failed', 'disabled')",
            name="ck_knowledge_index_versions_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"],
            ["knowledge_source_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_version_id",
            name="uq_knowledge_index_versions_source_version",
        ),
    )
    op.create_index(
        op.f("ix_knowledge_index_versions_source_version_id"),
        "knowledge_index_versions",
        ["source_version_id"],
    )

    op.create_table(
        "knowledge_jobs",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("version_id", UUID_TYPE, nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "request_payload",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "result_payload",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'dead_letter')",
            name="ck_knowledge_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["knowledge_source_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_jobs_status_available_at",
        "knowledge_jobs",
        ["status", "available_at"],
    )
    op.create_index(
        op.f("ix_knowledge_jobs_version_id"),
        "knowledge_jobs",
        ["version_id"],
    )
    op.create_index(
        "ix_knowledge_jobs_version_job_type",
        "knowledge_jobs",
        ["version_id", "job_type"],
    )

    op.create_table(
        "knowledge_web_snapshots",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("version_id", UUID_TYPE, nullable=False),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column(
            "headers",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["knowledge_source_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_knowledge_web_snapshots_version_id"),
        "knowledge_web_snapshots",
        ["version_id"],
    )

    op.create_table(
        "knowledge_snapshots",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=True),
        sa.Column("revision_id", UUID_TYPE, nullable=True),
        sa.Column("assessment_run_id", UUID_TYPE, nullable=True),
        sa.Column("artifact_production_run_id", UUID_TYPE, nullable=True),
        sa.Column("index_version_id", UUID_TYPE, nullable=False),
        sa.Column(
            "manifest",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_production_run_id"],
            ["artifact_production_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_run_id"],
            ["assessment_runs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["index_version_id"],
            ["knowledge_index_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["earthquake_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_knowledge_snapshots_fingerprint"),
    )
    op.create_index(
        op.f("ix_knowledge_snapshots_artifact_production_run_id"),
        "knowledge_snapshots",
        ["artifact_production_run_id"],
    )
    op.create_index(
        op.f("ix_knowledge_snapshots_assessment_run_id"),
        "knowledge_snapshots",
        ["assessment_run_id"],
    )
    op.create_index(
        op.f("ix_knowledge_snapshots_event_id"),
        "knowledge_snapshots",
        ["event_id"],
    )
    op.create_index(
        op.f("ix_knowledge_snapshots_index_version_id"),
        "knowledge_snapshots",
        ["index_version_id"],
    )
    op.create_index(
        op.f("ix_knowledge_snapshots_revision_id"),
        "knowledge_snapshots",
        ["revision_id"],
    )

    op.create_table(
        "qa_sessions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("event_id", UUID_TYPE, nullable=True),
        sa.Column("snapshot_id", UUID_TYPE, nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["earthquake_events.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["knowledge_snapshots.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_sessions_event_id"), "qa_sessions", ["event_id"])
    op.create_index(op.f("ix_qa_sessions_snapshot_id"), "qa_sessions", ["snapshot_id"])

    op.create_table(
        "qa_questions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("session_id", UUID_TYPE, nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["qa_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_questions_session_id"), "qa_questions", ["session_id"])

    op.create_table(
        "qa_answers",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("question_id", UUID_TYPE, nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'running'"),
            nullable=False,
        ),
        sa.Column("structured", JSONB_TYPE, nullable=True),
        sa.Column(
            "citation_keys",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "degraded_reasons",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'unavailable', 'failed')",
            name="ck_qa_answers_status",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["qa_questions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_answers_question_id"), "qa_answers", ["question_id"])

    op.create_table(
        "qa_citations",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("answer_id", UUID_TYPE, nullable=False),
        sa.Column("chunk_id", UUID_TYPE, nullable=True),
        sa.Column("citation_key", sa.String(length=32), nullable=False),
        sa.Column("source_title", sa.String(length=256), nullable=False),
        sa.Column("version_label", sa.String(length=128), nullable=False),
        sa.Column("locator", sa.String(length=256), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["qa_answers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "answer_id",
            "citation_key",
            name="uq_qa_citations_answer_key",
        ),
    )
    op.create_index(op.f("ix_qa_citations_answer_id"), "qa_citations", ["answer_id"])

    op.create_table(
        "qa_feedback",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("answer_id", UUID_TYPE, nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("helpful", sa.Boolean(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 5",
            name="ck_qa_feedback_rating",
        ),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["qa_answers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_feedback_answer_id"), "qa_feedback", ["answer_id"])

    op.create_table(
        "qa_map_actions",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("answer_id", UUID_TYPE, nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column(
            "payload",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["qa_answers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_map_actions_answer_id"), "qa_map_actions", ["answer_id"])

    op.create_table(
        "qa_tool_calls",
        sa.Column("id", UUID_TYPE, nullable=False),
        sa.Column("answer_id", UUID_TYPE, nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("tool_status", sa.String(length=32), nullable=False),
        sa.Column(
            "arguments",
            JSONB_TYPE,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", JSONB_TYPE, nullable=True),
        sa.Column(
            "limitations",
            JSONB_TYPE,
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["answer_id"],
            ["qa_answers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qa_tool_calls_answer_id"), "qa_tool_calls", ["answer_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_qa_tool_calls_answer_id"), table_name="qa_tool_calls")
    op.drop_table("qa_tool_calls")
    op.drop_index(op.f("ix_qa_map_actions_answer_id"), table_name="qa_map_actions")
    op.drop_table("qa_map_actions")
    op.drop_index(op.f("ix_qa_feedback_answer_id"), table_name="qa_feedback")
    op.drop_table("qa_feedback")
    op.drop_index(op.f("ix_qa_citations_answer_id"), table_name="qa_citations")
    op.drop_table("qa_citations")
    op.drop_index(op.f("ix_qa_answers_question_id"), table_name="qa_answers")
    op.drop_table("qa_answers")
    op.drop_index(op.f("ix_qa_questions_session_id"), table_name="qa_questions")
    op.drop_table("qa_questions")
    op.drop_index(op.f("ix_qa_sessions_snapshot_id"), table_name="qa_sessions")
    op.drop_index(op.f("ix_qa_sessions_event_id"), table_name="qa_sessions")
    op.drop_table("qa_sessions")

    op.drop_index(
        op.f("ix_knowledge_snapshots_revision_id"),
        table_name="knowledge_snapshots",
    )
    op.drop_index(
        op.f("ix_knowledge_snapshots_index_version_id"),
        table_name="knowledge_snapshots",
    )
    op.drop_index(
        op.f("ix_knowledge_snapshots_event_id"),
        table_name="knowledge_snapshots",
    )
    op.drop_index(
        op.f("ix_knowledge_snapshots_assessment_run_id"),
        table_name="knowledge_snapshots",
    )
    op.drop_index(
        op.f("ix_knowledge_snapshots_artifact_production_run_id"),
        table_name="knowledge_snapshots",
    )
    op.drop_table("knowledge_snapshots")

    op.drop_index(
        op.f("ix_knowledge_web_snapshots_version_id"),
        table_name="knowledge_web_snapshots",
    )
    op.drop_table("knowledge_web_snapshots")

    op.drop_index("ix_knowledge_jobs_version_job_type", table_name="knowledge_jobs")
    op.drop_index(op.f("ix_knowledge_jobs_version_id"), table_name="knowledge_jobs")
    op.drop_index(
        "ix_knowledge_jobs_status_available_at",
        table_name="knowledge_jobs",
    )
    op.drop_table("knowledge_jobs")

    op.drop_index(
        op.f("ix_knowledge_index_versions_source_version_id"),
        table_name="knowledge_index_versions",
    )
    op.drop_table("knowledge_index_versions")

    op.drop_index(op.f("ix_knowledge_chunks_version_id"), table_name="knowledge_chunks")
    op.drop_index(
        "ix_knowledge_chunks_search_trgm",
        table_name="knowledge_chunks",
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.drop_table("knowledge_chunks")

    op.drop_index(
        op.f("ix_knowledge_source_versions_source_id"),
        table_name="knowledge_source_versions",
    )
    op.drop_table("knowledge_source_versions")

    op.drop_index("ix_qa_admin_audit_logs_resource", table_name="qa_admin_audit_logs")
    op.drop_index("ix_qa_admin_audit_logs_event_id", table_name="qa_admin_audit_logs")
    op.drop_index("ix_qa_admin_audit_logs_answer_id", table_name="qa_admin_audit_logs")
    op.drop_index(
        "ix_qa_admin_audit_logs_actor_created",
        table_name="qa_admin_audit_logs",
    )
    op.drop_table("qa_admin_audit_logs")
    op.drop_table("knowledge_sources")
