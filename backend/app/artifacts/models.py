import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ArtifactTemplate(Base):
    __tablename__ = "artifact_templates"
    __table_args__ = (
        UniqueConstraint("template_key", name="uq_artifact_template_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    versions: Mapped[list["ArtifactTemplateVersion"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
    )


class ArtifactTemplateVersion(Base):
    __tablename__ = "artifact_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_artifact_template_version"),
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_artifact_template_version_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact_templates.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(32),
        default="draft",
        server_default=text("'draft'"),
        index=True,
    )
    manifest: Mapped[dict] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    template: Mapped[ArtifactTemplate] = relationship(back_populates="versions")


class ProductionInputSnapshot(Base):
    __tablename__ = "production_input_snapshots"
    __table_args__ = (
        UniqueConstraint("production_run_id", name="uq_production_input_snapshot_run"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    production_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact_production_runs.id", ondelete="CASCADE"),
        index=True,
    )
    context_fingerprint: Mapped[str] = mapped_column(String(64))
    region_id: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    run: Mapped["ProductionRun"] = relationship(foreign_keys=[production_run_id])
    items: Mapped[list["ProductionInputSnapshotItem"]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
    )


class ProductionInputSnapshotItem(Base):
    __tablename__ = "production_input_snapshot_items"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "asset_key",
            "role",
            name="uq_production_input_snapshot_item",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("production_input_snapshots.id", ondelete="CASCADE"),
        index=True,
    )
    asset_key: Mapped[str] = mapped_column(String(160))
    asset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="CASCADE"),
        index=True,
    )
    checksum: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    coverage: Mapped[dict] = mapped_column(JSONB)
    selected_for_render: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )

    snapshot: Mapped[ProductionInputSnapshot] = relationship(back_populates="items")


class ProductionRun(Base):
    __tablename__ = "artifact_production_runs"
    __table_args__ = (
        UniqueConstraint(
            "assessment_run_id",
            "generation_seq",
            name="uq_artifact_run_generation",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', "
            "'failed', 'canceled')",
            name="ck_artifact_run_status",
        ),
        CheckConstraint(
            "production_mode IN ('live', 'manual', 'test', 'drill', 'replay')",
            name="ck_artifact_production_mode",
        ),
        CheckConstraint(
            "launch_mode IN ('assessment_child', 'standalone')",
            name="ck_artifact_launch_mode",
        ),
        CheckConstraint(
            "deadline_kind IN ('event_deadline', 'rebuild_deadline')",
            name="ck_artifact_deadline_kind",
        ),
        Index(
            "uq_artifact_run_current_scope",
            "event_id",
            "revision_id",
            "generation_scope",
            unique=True,
            postgresql_where=text("is_current AND superseded_at IS NULL"),
        ),
        Index("ix_artifact_production_runs_event_id", "event_id"),
        Index("ix_artifact_production_runs_revision_id", "revision_id"),
        Index("ix_artifact_production_runs_status", "status"),
        Index("ix_artifact_production_runs_is_current", "is_current"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    assessment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="SET NULL"),
        index=True,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_revisions.id", ondelete="CASCADE"),
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    production_mode: Mapped[str] = mapped_column(String(32))
    launch_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending",
        server_default=text("'pending'"),
    )
    priority: Mapped[int] = mapped_column(
        Integer,
        default=100,
        server_default=text("100"),
    )
    deadline_basis_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_kind: Mapped[str] = mapped_column(String(32))
    deadline_exceeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_artifact_committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catalog_version: Mapped[str] = mapped_column(String(128))
    input_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "production_input_snapshots.id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    context_fingerprint: Mapped[str | None] = mapped_column(String(64))
    final_input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    generation_seq: Mapped[int] = mapped_column(Integer)
    generation_scope: Mapped[str] = mapped_column(String(256))
    required_outputs: Mapped[list] = mapped_column(JSONB)
    rebuild_parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "artifact_production_runs.id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    superseded_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "artifact_production_runs.id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot: Mapped[dict | None] = mapped_column(JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    input_snapshot: Mapped[ProductionInputSnapshot | None] = relationship(
        foreign_keys=[input_snapshot_id],
        viewonly=True,
    )
    tasks: Mapped[list["ProductionTask"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list["GeneratedArtifact"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )
    rebuild_parent: Mapped["ProductionRun | None"] = relationship(
        remote_side="ProductionRun.id",
        foreign_keys=[rebuild_parent_run_id],
    )
    superseded_by: Mapped["ProductionRun | None"] = relationship(
        remote_side="ProductionRun.id",
        foreign_keys=[superseded_by_run_id],
    )


class ProductionTask(Base):
    __tablename__ = "artifact_production_tasks"
    __table_args__ = (
        UniqueConstraint(
            "production_run_id",
            "artifact_key",
            "output_profile",
            name="uq_artifact_task_output",
        ),
        CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'succeeded', "
            "'degraded', 'failed', 'timed_out', 'canceled')",
            name="ck_artifact_task_status",
        ),
        Index("ix_artifact_production_tasks_run_id", "production_run_id"),
        Index("ix_artifact_production_tasks_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    production_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact_production_runs.id", ondelete="CASCADE"),
    )
    artifact_key: Mapped[str] = mapped_column(String(160))
    output_profile: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
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
    )
    depends_on: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    optional_depends_on: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    optional_dependency_wait_cutoff_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    output_checksum: Mapped[str | None] = mapped_column(String(64))
    final_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "generated_artifacts.id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    deadline_exceeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    run: Mapped[ProductionRun] = relationship(back_populates="tasks")
    dependency_bindings: Mapped[list["ArtifactTaskDependencyBinding"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list["GeneratedArtifact"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        foreign_keys="GeneratedArtifact.production_task_id",
    )
    final_artifact: Mapped["GeneratedArtifact | None"] = relationship(
        foreign_keys=[final_artifact_id],
    )


class ArtifactTaskDependencyBinding(Base):
    __tablename__ = "artifact_task_dependency_bindings"
    __table_args__ = (
        CheckConstraint(
            "dependency_kind IN ('assessment_product', 'artifact')",
            name="ck_artifact_dependency_kind",
        ),
        CheckConstraint(
            "resolution_status IN ('bound', 'degraded', 'failed', 'timed_out', "
            "'canceled', 'omitted_after_wait')",
            name="ck_artifact_dependency_resolution_status",
        ),
        CheckConstraint(
            "(dependency_kind = 'assessment_product' "
            "AND dependency_output_profile IS NULL) "
            "OR (dependency_kind = 'artifact' "
            "AND dependency_output_profile IS NOT NULL)",
            name="ck_artifact_dependency_kind_profile",
        ),
        CheckConstraint(
            "resolution_status != 'omitted_after_wait' OR is_optional",
            name="ck_artifact_dependency_optional",
        ),
        CheckConstraint(
            "((bound_entity_id IS NULL AND bound_version IS NULL "
            "AND bound_checksum IS NULL) "
            "OR (bound_entity_id IS NOT NULL AND bound_version IS NOT NULL "
            "AND bound_checksum IS NOT NULL)) "
            "AND (resolution_status NOT IN ('bound', 'degraded') "
            "OR (bound_entity_id IS NOT NULL AND bound_version IS NOT NULL "
            "AND bound_checksum IS NOT NULL))",
            name="ck_artifact_dependency_bound_fields",
        ),
        CheckConstraint(
            "resolution_status NOT IN "
            "('failed', 'timed_out', 'canceled', 'omitted_after_wait') "
            "OR (resolution_detail IS NOT NULL "
            "AND jsonb_typeof(resolution_detail) = 'object' "
            "AND resolution_detail != '{}'::jsonb "
            "AND resolved_at IS NOT NULL)",
            name="ck_artifact_dependency_terminal_detail",
        ),
        Index(
            "uq_artifact_dependency_product",
            "production_task_id",
            "dependency_key",
            unique=True,
            postgresql_where=text("dependency_kind = 'assessment_product'"),
        ),
        Index(
            "uq_artifact_dependency_artifact",
            "production_task_id",
            "dependency_key",
            "dependency_output_profile",
            unique=True,
            postgresql_where=text("dependency_kind = 'artifact'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    production_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact_production_tasks.id", ondelete="CASCADE"),
    )
    dependency_kind: Mapped[str] = mapped_column(
        ENUM(
            "assessment_product",
            "artifact",
            name="artifact_dependency_kind",
            create_type=False,
        )
    )
    dependency_key: Mapped[str] = mapped_column(String(160))
    dependency_output_profile: Mapped[str | None] = mapped_column(String(64))
    is_optional: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    bound_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    bound_version: Mapped[str | None] = mapped_column(String(128))
    bound_checksum: Mapped[str | None] = mapped_column(String(64))
    resolution_status: Mapped[str] = mapped_column(String(32))
    resolution_detail: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    task: Mapped[ProductionTask] = relationship(back_populates="dependency_bindings")


class GeneratedArtifact(Base):
    __tablename__ = "generated_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "artifact_key",
            "output_profile",
            "artifact_version",
            name="uq_generated_artifact_version",
        ),
        CheckConstraint(
            "status IN ('complete', 'degraded', 'failed')",
            name="ck_generated_artifact_status",
        ),
        CheckConstraint(
            "publication_mode IN ('automatic', 'rebuild', 'superadmin_override')",
            name="ck_generated_artifact_publication_mode",
        ),
        Index(
            "uq_generated_artifact_final_task",
            "production_task_id",
            unique=True,
            postgresql_where=text("is_final"),
        ),
        Index("ix_generated_artifacts_run_id", "production_run_id"),
        Index("ix_generated_artifacts_task_id", "production_task_id"),
        Index("ix_generated_artifacts_event_id", "event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    production_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact_production_runs.id", ondelete="CASCADE"),
    )
    production_task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact_production_tasks.id", ondelete="CASCADE"),
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_revisions.id", ondelete="CASCADE"),
    )
    artifact_key: Mapped[str] = mapped_column(String(160))
    output_profile: Mapped[str] = mapped_column(String(64))
    artifact_version: Mapped[int] = mapped_column(Integer)
    is_final: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    production_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    quality_grade: Mapped[str | None] = mapped_column(String(16))
    needs_review: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    publication_mode: Mapped[str] = mapped_column(String(32))
    generation_reason: Mapped[str | None] = mapped_column(Text)
    marker: Mapped[dict | None] = mapped_column(JSONB)
    file_name: Mapped[str] = mapped_column(String(512))
    format: Mapped[str] = mapped_column(String(16))
    storage_path: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    template_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    data_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    render_manifest: Mapped[dict | None] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "generated_artifacts.id",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )

    run: Mapped[ProductionRun] = relationship(back_populates="artifacts")
    task: Mapped[ProductionTask] = relationship(
        back_populates="artifacts",
        foreign_keys=[production_task_id],
    )
    publications: Mapped[list["ArtifactPublication"]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
    )
    superseded_by: Mapped["GeneratedArtifact | None"] = relationship(
        remote_side="GeneratedArtifact.id",
        foreign_keys=[superseded_by_id],
    )


class ArtifactPublication(Base):
    __tablename__ = "artifact_publications"
    __table_args__ = (
        Index(
            "uq_artifact_publication_current",
            "event_id",
            "artifact_key",
            "output_profile",
            "production_mode",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_artifact_publications_event_id", "event_id"),
        Index("ix_artifact_publications_artifact_id", "artifact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="CASCADE"),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("earthquake_revisions.id", ondelete="CASCADE"),
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    production_mode: Mapped[str] = mapped_column(String(32))
    artifact_key: Mapped[str] = mapped_column(String(160))
    output_profile: Mapped[str] = mapped_column(String(64))
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("generated_artifacts.id", ondelete="CASCADE"),
    )
    production_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifact_production_runs.id", ondelete="CASCADE"),
    )
    generation_seq: Mapped[int] = mapped_column(Integer)
    published_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_forced: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    artifact: Mapped[GeneratedArtifact] = relationship(back_populates="publications")
    production_run: Mapped[ProductionRun] = relationship()


class ArtifactOverrideRequest(Base):
    __tablename__ = "artifact_override_requests"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "endpoint",
            "idempotency_key",
            name="uq_artifact_override_request",
        ),
        CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed')",
            name="ck_artifact_override_request_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    actor_id: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(256))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32),
        default="processing",
        server_default=text("'processing'"),
    )
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    production_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact_production_runs.id", ondelete="SET NULL"),
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("generated_artifacts.id", ondelete="SET NULL"),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    production_run: Mapped[ProductionRun | None] = relationship()
    artifact: Mapped[GeneratedArtifact | None] = relationship()
