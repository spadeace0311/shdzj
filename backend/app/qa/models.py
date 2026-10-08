import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class QaSession(Base):
    __tablename__ = "qa_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    created_by: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_snapshots.id", ondelete="RESTRICT"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class QaQuestion(Base):
    __tablename__ = "qa_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    question_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32),
        default="running",
        server_default=text("'running'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class QaAnswer(Base):
    __tablename__ = "qa_answers"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'unavailable', 'failed')",
            name="ck_qa_answers_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_questions.id", ondelete="CASCADE"),
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="running",
        server_default=text("'running'"),
    )
    model_name: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(256))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    execution_plan: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    tool_call_summary: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    structured: Mapped[dict | None] = mapped_column(JSONB)
    citation_keys: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    degraded_reasons: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text: Mapped[str | None] = mapped_column(Text)


class QaCitation(Base):
    __tablename__ = "qa_citations"
    __table_args__ = (
        UniqueConstraint(
            "answer_id",
            "citation_key",
            name="uq_qa_citations_answer_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_answers.id", ondelete="CASCADE"),
        index=True,
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    citation_key: Mapped[str] = mapped_column(String(32))
    source_title: Mapped[str] = mapped_column(String(256))
    version_label: Mapped[str] = mapped_column(String(128))
    locator: Mapped[str | None] = mapped_column(String(256))
    excerpt: Mapped[str] = mapped_column(Text)
    source_uri: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class QaToolCall(Base):
    __tablename__ = "qa_tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_answers.id", ondelete="CASCADE"),
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_status: Mapped[str] = mapped_column(String(32))
    arguments: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    limitations: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class QaMapAction(Base):
    __tablename__ = "qa_map_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_answers.id", ondelete="CASCADE"),
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class QaFeedback(Base):
    __tablename__ = "qa_feedback"
    __table_args__ = (
        CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 5",
            name="ck_qa_feedback_rating",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("qa_answers.id", ondelete="CASCADE"),
        index=True,
    )
    created_by: Mapped[str] = mapped_column(String(64))
    helpful: Mapped[bool | None] = mapped_column(Boolean)
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class QaAdminAuditLog(Base):
    __tablename__ = "qa_admin_audit_logs"
    __table_args__ = (
        Index(
            "ix_qa_admin_audit_logs_resource",
            "resource_type",
            "resource_id",
        ),
        Index("ix_qa_admin_audit_logs_event_id", "event_id"),
        Index("ix_qa_admin_audit_logs_answer_id", "answer_id"),
        Index("ix_qa_admin_audit_logs_actor_created", "actor", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    answer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
