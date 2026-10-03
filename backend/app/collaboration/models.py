import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.collaboration.domain import WorkgroupCode
from app.db import Base


WORKGROUP_CODES_SQL = ", ".join(
    f"'{workgroup_code.value}'" for workgroup_code in WorkgroupCode
)


class WorkgroupDefinition(Base):
    __tablename__ = "workgroup_definitions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_workgroup_definition_code"),
        CheckConstraint(
            f"code IN ({WORKGROUP_CODES_SQL})",
            name="ck_workgroup_definition_code",
        ),
        CheckConstraint(
            "display_order BETWEEN 1 AND 7",
            name="ck_workgroup_definition_display_order",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    display_order: Mapped[int] = mapped_column(Integer, unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class WorkgroupMembership(Base):
    __tablename__ = "workgroup_memberships"
    __table_args__ = (
        CheckConstraint(
            "duty_role != 'deputy' OR deputy_order IS NOT NULL",
            name="ck_workgroup_membership_deputy_order_required",
        ),
        CheckConstraint(
            "duty_role IN ('leader', 'deputy', 'member', 'viewer')",
            name="ck_workgroup_membership_duty_role",
        ),
        CheckConstraint(
            "deputy_order IS NULL OR deputy_order > 0",
            name="ck_workgroup_membership_deputy_order_positive",
        ),
        Index(
            "uq_workgroup_membership_active_deputy_order",
            "workgroup_code",
            "deputy_order",
            unique=True,
            postgresql_where=text(
                "is_active AND duty_role = 'deputy' AND effective_to IS NULL"
            ),
        ),
        Index(
            "uq_workgroup_membership_active_leader",
            "workgroup_code",
            unique=True,
            postgresql_where=text(
                "is_active AND duty_role = 'leader' AND effective_to IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    workgroup_code: Mapped[str] = mapped_column(
        ForeignKey("workgroup_definitions.code", ondelete="CASCADE"),
        index=True,
    )
    duty_role: Mapped[str] = mapped_column(String(16))
    deputy_order: Mapped[int | None] = mapped_column(Integer)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class WorkgroupRosterSnapshot(Base):
    __tablename__ = "workgroup_roster_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "workgroup_code",
            "roster_version",
            name="uq_workgroup_roster_event_group_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    workgroup_code: Mapped[str] = mapped_column(
        ForeignKey("workgroup_definitions.code", ondelete="CASCADE"),
        index=True,
    )
    roster_version: Mapped[int] = mapped_column(Integer)
    roster_fingerprint: Mapped[str] = mapped_column(String(64))
    leader_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
    )
    deputies: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    members: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    snapshot: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class WorkgroupAttendance(Base):
    __tablename__ = "workgroup_attendance"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "workgroup_code",
            "user_id",
            name="uq_workgroup_attendance_event_group_user",
        ),
        CheckConstraint(
            "state IN ('unknown', 'present', 'absent', 'departed')",
            name="ck_workgroup_attendance_state",
        ),
        CheckConstraint(
            "duty_role_in_snapshot IN ('leader', 'deputy', 'member', 'viewer')",
            name="ck_workgroup_attendance_snapshot_role",
        ),
        CheckConstraint(
            "duty_role_in_snapshot != 'deputy' "
            "OR deputy_order_in_snapshot IS NOT NULL",
            name="ck_workgroup_attendance_deputy_order_required",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    workgroup_code: Mapped[str] = mapped_column(
        ForeignKey("workgroup_definitions.code", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    duty_role_in_snapshot: Mapped[str] = mapped_column(String(16))
    deputy_order_in_snapshot: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(
        String(16),
        default="unknown",
        server_default=text("'unknown'"),
    )
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CollaborationSettings(Base):
    __tablename__ = "collaboration_settings"
    __table_args__ = (
        CheckConstraint(
            "intensity_threshold >= 0",
            name="ck_collaboration_settings_intensity_threshold",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    intensity_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 1),
        default=Decimal("2.0"),
        server_default=text("2.0"),
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


class CollaborationTaskTemplate(Base):
    __tablename__ = "collaboration_task_templates"
    __table_args__ = (
        UniqueConstraint("code", name="uq_collaboration_task_template_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    code: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(64))
    workgroup_code: Mapped[str] = mapped_column(
        ForeignKey("workgroup_definitions.code", ondelete="CASCADE"),
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CollaborationTaskTemplateVersion(Base):
    __tablename__ = "collaboration_task_template_versions"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "version",
            name="uq_collaboration_task_template_version",
        ),
        UniqueConstraint(
            "template_code",
            "version",
            name="uq_collaboration_task_template_code_version",
        ),
        CheckConstraint(
            "start_offset_seconds >= 0",
            name="ck_collaboration_template_start_offset",
        ),
        CheckConstraint(
            "due_offset_seconds IS NULL "
            "OR due_offset_seconds >= start_offset_seconds",
            name="ck_collaboration_template_due_offset",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collaboration_task_templates.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[str] = mapped_column(String(128))
    template_code: Mapped[str] = mapped_column(String(160), index=True)
    phase_code: Mapped[str] = mapped_column(String(64))
    start_offset_seconds: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    due_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    continues_until_response_end: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=100,
        server_default=text("100"),
    )
    required_deliverables: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    optional_deliverables: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    artifact_bindings: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    applicability: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    instruction: Mapped[str] = mapped_column(
        Text,
        default="",
        server_default=text("''"),
    )
    response_basis: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        index=True,
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class WorkgroupTask(Base):
    __tablename__ = "collaboration_tasks"
    __table_args__ = (
        Index(
            "uq_collaboration_task_preplan_identity",
            "event_id",
            "template_version_id",
            "task_code",
            unique=True,
            postgresql_where=text("template_version_id IS NOT NULL"),
        ),
        Index(
            "uq_collaboration_task_ad_hoc_identity",
            "event_id",
            "task_code",
            unique=True,
            postgresql_where=text("template_version_id IS NULL"),
        ),
        CheckConstraint(
            "source_type IN ('preplan', 'correction', 'ad_hoc', 'system_review')",
            name="ck_collaboration_task_source_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'pending_review', "
            "'completed', 'not_required', 'failed')",
            name="ck_collaboration_task_status",
        ),
        CheckConstraint(
            "timeliness_state IN ('on_time', 'at_risk', 'overdue')",
            name="ck_collaboration_task_timeliness",
        ),
        Index("ix_collaboration_tasks_status_due", "status", "due_at"),
        Index("ix_collaboration_tasks_group_status", "workgroup_code", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    trigger_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_revisions.id", ondelete="CASCADE"),
        index=True,
    )
    assessment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="SET NULL"),
        index=True,
    )
    template_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "collaboration_task_template_versions.id",
            ondelete="SET NULL",
        ),
        index=True,
    )
    task_code: Mapped[str] = mapped_column(String(160))
    source_type: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str | None] = mapped_column(String(256))
    workgroup_code: Mapped[str] = mapped_column(
        ForeignKey("workgroup_definitions.code", ondelete="CASCADE"),
        index=True,
    )
    title: Mapped[str] = mapped_column(String(256))
    instruction: Mapped[str] = mapped_column(
        Text,
        default="",
        server_default=text("''"),
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=100,
        server_default=text("100"),
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default=text("'pending'"),
    )
    timeliness_state: Mapped[str] = mapped_column(
        String(32),
        default="on_time",
        server_default=text("'on_time'"),
    )
    phase_code: Mapped[str | None] = mapped_column(String(64))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class WorkgroupTaskContributor(Base):
    __tablename__ = "collaboration_task_contributors"
    __table_args__ = (
        Index(
            "uq_collaboration_task_contributor",
            "task_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collaboration_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    contribution_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    first_contributed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    last_contributed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class TaskDeliverable(Base):
    __tablename__ = "collaboration_task_deliverables"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "deliverable_code",
            name="uq_collaboration_task_deliverable_code",
        ),
        CheckConstraint(
            "requirement_kind IN ('automatic_artifact', 'manual_file', "
            "'manual_text', 'manual_file_or_text')",
            name="ck_collaboration_deliverable_requirement_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collaboration_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    deliverable_code: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(256))
    is_required: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    requirement_kind: Mapped[str] = mapped_column(String(32))
    artifact_binding: Mapped[dict | None] = mapped_column(JSONB)
    display_order: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class TaskDeliverableVersion(Base):
    __tablename__ = "collaboration_deliverable_versions"
    __table_args__ = (
        UniqueConstraint(
            "deliverable_id",
            "version_no",
            name="uq_collaboration_deliverable_version_no",
        ),
        CheckConstraint(
            "source_kind IN ('automatic', 'manual', 'superadmin_override')",
            name="ck_collaboration_deliverable_source_kind",
        ),
        Index(
            "uq_collaboration_deliverable_artifact_publication",
            "deliverable_id",
            "artifact_publication_id",
            unique=True,
            postgresql_where=text("artifact_publication_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    deliverable_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collaboration_task_deliverables.id", ondelete="CASCADE"),
        index=True,
    )
    version_no: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(32))
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generated_artifacts.id", ondelete="SET NULL"),
        index=True,
    )
    artifact_publication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact_publications.id", ondelete="SET NULL"),
        index=True,
    )
    storage_key: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(512))
    checksum: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    text_result: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(String(64))
    basis_text: Mapped[str | None] = mapped_column(Text)
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "collaboration_deliverable_versions.id",
            ondelete="SET NULL",
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class TaskDeliverablePublication(Base):
    __tablename__ = "collaboration_deliverable_publications"
    __table_args__ = (
        Index(
            "uq_collaboration_deliverable_current_publication",
            "deliverable_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        CheckConstraint(
            "published_role IN ('leader', 'deputy', 'superadmin')",
            name="ck_collaboration_publication_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    deliverable_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collaboration_task_deliverables.id", ondelete="CASCADE"),
        index=True,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collaboration_deliverable_versions.id", ondelete="CASCADE"),
        index=True,
    )
    published_by: Mapped[str] = mapped_column(String(64))
    published_role: Mapped[str] = mapped_column(String(32))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publication_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CollaborationTaskEvent(Base):
    __tablename__ = "collaboration_task_events"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "event_seq",
            name="uq_collaboration_task_event_sequence",
        ),
        Index(
            "uq_collaboration_task_event_idempotency",
            "task_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collaboration_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    event_seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    business_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class NotificationDelivery(Base):
    __tablename__ = "collaboration_notification_deliveries"
    __table_args__ = (
        CheckConstraint(
            "intent_type IN ('assigned', 'due_soon', 'overdue', "
            "'status_changed', 'failure')",
            name="ck_collaboration_notification_intent",
        ),
        CheckConstraint(
            "channel IN ('in_app', 'wecom', 'email', 'phone')",
            name="ck_collaboration_notification_channel",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'failed', "
            "'fallback_sent')",
            name="ck_collaboration_notification_status",
        ),
        Index(
            "uq_collaboration_notification_task_delivery",
            "event_id",
            "task_id",
            "recipient_user_id",
            "channel",
            "dedupe_key",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
        ),
        Index(
            "uq_collaboration_notification_event_delivery",
            "event_id",
            "recipient_user_id",
            "channel",
            "dedupe_key",
            unique=True,
            postgresql_where=text("task_id IS NULL"),
        ),
        Index("ix_collaboration_notifications_pending", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collaboration_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    intent_type: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(256))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CollaborationOutbox(Base):
    __tablename__ = "collaboration_projection_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'published', 'dead_letter')",
            name="ck_collaboration_outbox_status",
        ),
        Index("ix_collaboration_outbox_pending", "status", "available_at"),
        Index(
            "uq_collaboration_outbox_idempotency",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collaboration_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default=text("'pending'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CommandHallEventProjection(Base):
    __tablename__ = "command_hall_event_projections"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            name="uq_command_hall_event_projection_event",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    event_snapshot: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    task_counts: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    artifact_summary: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    alert_summary: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    projection_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CommandHallGroupProjection(Base):
    __tablename__ = "command_hall_group_projections"
    __table_args__ = (
        Index(
            "uq_command_hall_group_projection_event_group",
            "event_id",
            "workgroup_code",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    workgroup_code: Mapped[str] = mapped_column(
        ForeignKey("workgroup_definitions.code", ondelete="CASCADE"),
        index=True,
    )
    group_snapshot: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    task_counts: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    latest_deliverable: Mapped[dict | None] = mapped_column(JSONB)
    alert_summary: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    projection_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class CommandHallAlertProjection(Base):
    __tablename__ = "command_hall_alert_projections"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_command_hall_alert_severity",
        ),
        Index("ix_command_hall_alerts_event_status", "event_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
        index=True,
    )
    workgroup_code: Mapped[str | None] = mapped_column(
        ForeignKey("workgroup_definitions.code", ondelete="SET NULL"),
        index=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("collaboration_tasks.id", ondelete="SET NULL"),
        index=True,
    )
    alert_key: Mapped[str] = mapped_column(String(160))
    alert_type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(32),
        default="open",
        server_default=text("'open'"),
    )
    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    projection_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
