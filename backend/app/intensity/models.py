import uuid
from datetime import datetime

from geoalchemy2 import Geometry, Raster
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AssessmentTaskAttempt(Base):
    __tablename__ = "assessment_task_attempts"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_number",
            name="uq_assessment_task_attempts_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_tasks.id", ondelete="CASCADE"),
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    output_checksum: Mapped[str | None] = mapped_column(String(64))
    error_category: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class IntensityFieldProduct(Base):
    __tablename__ = "intensity_field_products"
    __table_args__ = (
        UniqueConstraint("run_id", "product_type", name="uq_intensity_product_run_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_runs.id", ondelete="CASCADE"),
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment_tasks.id", ondelete="CASCADE"),
        index=True,
    )
    product_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    algorithm_version: Mapped[str] = mapped_column(String(128))
    parameter_version: Mapped[str] = mapped_column(String(128))
    strategy_version: Mapped[str | None] = mapped_column(String(128))
    grid_definition_version: Mapped[str] = mapped_column(String(128))
    region_profile_version: Mapped[str] = mapped_column(String(128))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    input_checksum: Mapped[str] = mapped_column(String(64))
    output_checksum: Mapped[str | None] = mapped_column(String(64))
    quality_grade: Mapped[str | None] = mapped_column(String(16), index=True)
    coverage_ratio: Mapped[float] = mapped_column(Numeric(8, 6), default=0)
    spatial_extent: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326),
        nullable=True,
    )
    statistics: Mapped[dict] = mapped_column(JSONB)
    source_product_id: Mapped[str | None] = mapped_column(String(160), index=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntensityRaster(Base):
    __tablename__ = "intensity_rasters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("intensity_field_products.id", ondelete="CASCADE"),
        unique=True,
    )
    rast: Mapped[object] = mapped_column(Raster)
    band_manifest: Mapped[dict] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    srid: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
