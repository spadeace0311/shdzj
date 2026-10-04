import uuid
from datetime import datetime

from geoalchemy2 import Geometry, Raster
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


class DataAsset(Base):
    __tablename__ = "data_assets"
    __table_args__ = (
        UniqueConstraint("asset_key", "region_id", name="uq_data_assets_key_region"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    asset_key: Mapped[str] = mapped_column(String(160))
    region_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(256))
    data_type: Mapped[str] = mapped_column(String(32))
    spatial_granularity: Mapped[str] = mapped_column(String(32))
    responsibility_unit: Mapped[str] = mapped_column(String(64))
    update_interval_days: Mapped[int] = mapped_column(Integer)
    is_core: Mapped[bool] = mapped_column(Boolean)
    contract: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class DataAssetVersion(Base):
    __tablename__ = "data_asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_data_asset_versions_asset_version"),
        Index(
            "uq_data_asset_versions_published_asset",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        CheckConstraint(
            "status IN ('imported', 'validated', 'published', 'retired', 'rejected')",
            name="ck_data_asset_versions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_assets.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(32),
        default="imported",
        server_default=text("'imported'"),
        index=True,
    )
    source_uri: Mapped[str] = mapped_column(String(2048))
    license_name: Mapped[str | None] = mapped_column(String(256))
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quality_grade: Mapped[str | None] = mapped_column(String(16), index=True)
    change_note: Mapped[str | None] = mapped_column(Text)
    schema_summary: Mapped[dict] = mapped_column(JSONB)
    record_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
    )
    spatial_extent: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326),
        nullable=True,
    )
    source_crs: Mapped[str] = mapped_column(
        String(16),
        default="EPSG:4326",
        server_default=text("'EPSG:4326'"),
    )
    checksum: Mapped[str | None] = mapped_column(String(64))
    managed_path: Mapped[str | None] = mapped_column(String(1024))
    imported_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class DataAssetImportJob(Base):
    __tablename__ = "data_asset_import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_assets.id", ondelete="CASCADE"),
        index=True,
    )
    asset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="CASCADE"),
        index=True,
    )
    file_name: Mapped[str] = mapped_column(String(255))
    file_format: Mapped[str] = mapped_column(String(32))
    source_uri: Mapped[str] = mapped_column(String(2048))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    raw_checksum: Mapped[str] = mapped_column(String(64))
    managed_path: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(
        String(32),
        default="queued",
        server_default=text("'queued'"),
        index=True,
    )
    validation_errors: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    validation_warnings: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    statistics: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    error_summary: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class DataAssetSnapshot(Base):
    __tablename__ = "data_asset_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "region_id",
            "asset_key",
            name="uq_data_asset_snapshots_run_region_asset",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="CASCADE"),
        index=True,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_assets.id", ondelete="CASCADE"),
        index=True,
    )
    region_id: Mapped[str] = mapped_column(String(64), index=True)
    asset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="CASCADE"),
        index=True,
    )
    asset_key: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(128))
    checksum: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    required: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class DataAssetAuditLog(Base):
    __tablename__ = "data_asset_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_assets.id", ondelete="CASCADE"),
        index=True,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="CASCADE"),
        index=True,
    )
    action: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class DataAssetRecord(Base):
    __tablename__ = "data_asset_records"
    __table_args__ = (
        UniqueConstraint("version_id", "row_number", name="uq_data_asset_records_version_row"),
        Index("ix_data_asset_records_geom", "geom", postgresql_using="gist"),
        Index("ix_data_asset_records_properties", "properties", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="CASCADE"),
        index=True,
    )
    row_number: Mapped[int] = mapped_column(Integer)
    business_key: Mapped[str] = mapped_column(String(512))
    properties: Mapped[dict] = mapped_column(JSONB)
    geom: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class DataAssetRaster(Base):
    __tablename__ = "data_asset_rasters"
    __table_args__ = (
        UniqueConstraint("version_id", name="uq_data_asset_rasters_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("data_asset_versions.id", ondelete="CASCADE"),
    )
    rast: Mapped[object] = mapped_column(Raster)
    band_manifest: Mapped[dict] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    srid: Mapped[int] = mapped_column(Integer)
    spatial_extent: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
