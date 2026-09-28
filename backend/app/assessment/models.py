import uuid
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AssessmentRun(Base):
    __tablename__ = "assessment_runs"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_assessment_runs_revision"),
        UniqueConstraint("outbox_id", name="uq_assessment_runs_outbox"),
        UniqueConstraint("event_id", "run_no", name="uq_assessment_runs_event_run_no"),
        Index("ix_assessment_runs_status_deadline", "status", "deadline_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    outbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("event_lifecycle_outbox.id", ondelete="CASCADE"),
        index=True,
    )
    run_no: Mapped[int] = mapped_column(Integer)
    trigger_reason: Mapped[str] = mapped_column(String(32), default="live")
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default=text("'pending'"),
        index=True,
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=100,
        server_default=text("100"),
    )
    t1_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    report_ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    deadline_basis_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    deadline_exceeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    algorithm_bundle_version: Mapped[str | None] = mapped_column(String(128))
    superseded_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="SET NULL", use_alter=True),
        index=True,
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_asset_snapshot_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        index=True,
    )
    data_asset_snapshot_result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    tasks: Mapped[list["AssessmentTask"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class AssessmentTask(Base):
    __tablename__ = "assessment_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "task_key", name="uq_assessment_tasks_run_task_key"),
        Index("ix_assessment_tasks_status_deadline", "status", "deadline_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="CASCADE"),
        index=True,
    )
    task_key: Mapped[str] = mapped_column(String(96))
    task_type: Mapped[str] = mapped_column(String(48), index=True)
    component: Mapped[str] = mapped_column(String(48), index=True)
    priority: Mapped[int] = mapped_column(
        Integer,
        default=100,
        server_default=text("100"),
    )
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default=text("'pending'"),
        index=True,
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=3,
        server_default=text("3"),
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    output_checksum: Mapped[str | None] = mapped_column(String(64))
    algorithm_version: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    run: Mapped[AssessmentRun] = relationship(back_populates="tasks")
