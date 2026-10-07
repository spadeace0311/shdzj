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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


SOURCE_LAYER_VALUES = (
    "local_authority",
    "structured_live",
    "public_reference",
)
KNOWLEDGE_VERSION_STATUS_VALUES = (
    "registered",
    "uploaded",
    "parsing",
    "parsed",
    "embedding",
    "indexed",
    "published",
    "failed",
    "disabled",
)
KNOWLEDGE_JOB_STATUS_VALUES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "dead_letter",
)


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_knowledge_sources_source_key"),
        CheckConstraint(
            "layer IN ('local_authority', 'structured_live', 'public_reference')",
            name="ck_knowledge_sources_layer",
        ),
        CheckConstraint(
            "access_level IN ('public', 'internal', 'restricted')",
            name="ck_knowledge_sources_access_level",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    source_key: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(256))
    layer: Mapped[str] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(64))
    access_level: Mapped[str] = mapped_column(
        String(32),
        default="internal",
        server_default=text("'internal'"),
    )
    origin: Mapped[str | None] = mapped_column(String(2048))
    allow_online_refresh: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
    )
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class KnowledgeSourceVersion(Base):
    __tablename__ = "knowledge_source_versions"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "version",
            name="uq_knowledge_source_versions_source_version",
        ),
        CheckConstraint(
            "status IN ('registered', 'uploaded', 'parsing', 'parsed', "
            "'embedding', 'indexed', 'published', 'failed', 'disabled')",
            name="ck_knowledge_source_versions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(32),
        default="registered",
        server_default=text("'registered'"),
    )
    file_name: Mapped[str | None] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    source_uri: Mapped[str | None] = mapped_column(Text)
    storage_path: Mapped[str | None] = mapped_column(Text)
    parsed_text_path: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    version_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    parse_manifest: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "chunk_no",
            name="uq_knowledge_chunks_version_chunk",
        ),
        Index(
            "ix_knowledge_chunks_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source_versions.id", ondelete="CASCADE"),
        index=True,
    )
    chunk_no: Mapped[int] = mapped_column(Integer)
    section_path: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    table_range: Mapped[list | None] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    qdrant_point_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    chunk_metadata: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    search_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    text: Mapped[str] = mapped_column(Text)


class KnowledgeIndexVersion(Base):
    __tablename__ = "knowledge_index_versions"
    __table_args__ = (
        UniqueConstraint(
            "source_version_id",
            name="uq_knowledge_index_versions_source_version",
        ),
        CheckConstraint(
            "status IN ('registered', 'uploaded', 'parsing', 'parsed', "
            "'embedding', 'indexed', 'published', 'failed', 'disabled')",
            name="ck_knowledge_index_versions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    source_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source_versions.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(32),
        default="indexed",
        server_default=text("'indexed'"),
    )
    collection_name: Mapped[str] = mapped_column(String(256))
    embedding_model: Mapped[str] = mapped_column(String(256))
    reranker_model: Mapped[str] = mapped_column(String(256))
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    manifest: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class KnowledgeJob(Base):
    __tablename__ = "knowledge_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'dead_letter')",
            name="ck_knowledge_jobs_status",
        ),
        Index(
            "ix_knowledge_jobs_status_available_at",
            "status",
            "available_at",
        ),
        Index(
            "ix_knowledge_jobs_version_job_type",
            "version_id",
            "job_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source_versions.id", ondelete="CASCADE"),
        index=True,
    )
    job_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32),
        default="queued",
        server_default=text("'queued'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer,
        default=5,
        server_default=text("5"),
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_payload: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    result_payload: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeWebSnapshot(Base):
    __tablename__ = "knowledge_web_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_source_versions.id", ondelete="CASCADE"),
        index=True,
    )
    requested_url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(255))
    headers: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    body_text: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeSnapshot(Base):
    __tablename__ = "knowledge_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "fingerprint",
            name="uq_knowledge_snapshots_fingerprint",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("earthquake_events.id", ondelete="SET NULL"),
        index=True,
    )
    revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("earthquake_revisions.id", ondelete="SET NULL"),
        index=True,
    )
    assessment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="SET NULL"),
        index=True,
    )
    artifact_production_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifact_production_runs.id", ondelete="SET NULL"),
        index=True,
    )
    index_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_index_versions.id", ondelete="RESTRICT"),
        index=True,
    )
    manifest: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
