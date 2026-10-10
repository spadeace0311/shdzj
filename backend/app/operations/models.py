import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OperationsSettings(Base):
    __tablename__ = "operations_settings"
    __table_args__ = (
        UniqueConstraint("settings_key", name="uq_operations_settings_key"),
        CheckConstraint(
            "test_magnitude_min BETWEEN -2.0 AND 12.0 "
            "AND test_magnitude_max BETWEEN -2.0 AND 12.0",
            name="ck_operations_settings_magnitude_range",
        ),
        CheckConstraint(
            "test_depth_min_km BETWEEN 0 AND 1000 " "AND test_depth_max_km BETWEEN 0 AND 1000",
            name="ck_operations_settings_depth_range",
        ),
        CheckConstraint(
            "test_magnitude_min <= test_magnitude_max",
            name="ck_operations_settings_magnitude_order",
        ),
        CheckConstraint(
            "test_depth_min_km <= test_depth_max_km",
            name="ck_operations_settings_depth_order",
        ),
        CheckConstraint(
            "daily_window_start < daily_window_end",
            name="ck_operations_settings_window_order",
        ),
        CheckConstraint(
            "drill_conflict_policy IN ('pause', 'terminate')",
            name="ck_operations_settings_drill_conflict_policy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    settings_key: Mapped[str] = mapped_column(String(32))
    daily_test_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="Asia/Shanghai",
        server_default=text("'Asia/Shanghai'"),
    )
    daily_window_start: Mapped[time] = mapped_column(
        Time,
        default=time(2, 0),
    )
    daily_window_end: Mapped[time] = mapped_column(
        Time,
        default=time(4, 0),
    )
    test_magnitude_min: Mapped[Decimal] = mapped_column(
        Numeric(4, 1),
        default=Decimal("5.0"),
        server_default=text("5.0"),
    )
    test_magnitude_max: Mapped[Decimal] = mapped_column(
        Numeric(4, 1),
        default=Decimal("5.9"),
        server_default=text("5.9"),
    )
    test_depth_min_km: Mapped[Decimal] = mapped_column(
        Numeric(6, 2),
        default=Decimal("5.00"),
        server_default=text("5.00"),
    )
    test_depth_max_km: Mapped[Decimal] = mapped_column(
        Numeric(6, 2),
        default=Decimal("20.00"),
        server_default=text("20.00"),
    )
    drill_conflict_policy: Mapped[str] = mapped_column(
        String(16),
        default="pause",
        server_default=text("'pause'"),
    )
    row_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    updated_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class OperationsTestSchedule(Base):
    __tablename__ = "operations_test_schedules"
    __table_args__ = (
        UniqueConstraint(
            "local_date",
            name="uq_operations_test_schedule_local_date",
        ),
        CheckConstraint(
            "state IN ('scheduled', 'due', 'deferred', 'triggered', 'skipped', 'failed')",
            name="ck_operations_test_schedule_state",
        ),
        CheckConstraint(
            "state <> 'triggered' " "OR (event_id IS NOT NULL AND triggered_at IS NOT NULL)",
            name="ck_operations_test_schedule_triggered",
        ),
        Index("ix_operations_test_schedules_event_id", "event_id"),
        Index("ix_operations_test_schedules_state", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    local_date: Mapped[date] = mapped_column(Date)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16))
    scenario: Mapped[dict | None] = mapped_column(JSONB)
    random_seed: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class OperationsRun(Base):
    __tablename__ = "operations_runs"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_operations_run_event"),
        UniqueConstraint("schedule_id", name="uq_operations_run_schedule"),
        UniqueConstraint("idempotency_key", name="uq_operations_run_idempotency"),
        CheckConstraint(
            "trigger_mode IN ('scheduled_test', 'manual_test', 'drill', 'manual_event')",
            name="ck_operations_run_trigger_mode",
        ),
        CheckConstraint(
            "operation_state IN ('queued', 'running', 'paused', "
            "'completed', 'canceled', 'failed')",
            name="ck_operations_run_operation_state",
        ),
        Index("ix_operations_runs_operation_state", "operation_state"),
        Index("ix_operations_runs_priority", "priority"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations_test_schedules.id", ondelete="SET NULL"),
    )
    trigger_mode: Mapped[str] = mapped_column(String(32))
    operation_state: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(SmallInteger)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    triggered_by: Mapped[str] = mapped_column(String(64))
    resume_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("false"),
    )
    detail: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class OperationsAuditLog(Base):
    __tablename__ = "operations_audit_logs"
    __table_args__ = (
        Index(
            "ix_operations_audit_logs_resource",
            "resource_type",
            "resource_id",
        ),
        Index("ix_operations_audit_logs_actor_created", "actor", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128))
    before_state: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    after_state: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    detail: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
