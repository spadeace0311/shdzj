import uuid
from datetime import datetime

from geoalchemy2 import Raster
from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


class LossModelDefinition(Base):
    __tablename__ = "loss_model_definitions"
    __table_args__ = (
        UniqueConstraint(
            "model_id",
            "formula_version",
            name="uq_loss_model_formula",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    model_id: Mapped[str] = mapped_column(String(160))
    model_type: Mapped[str] = mapped_column(String(48))
    formula_version: Mapped[str] = mapped_column(String(128))
    applicable_region: Mapped[str] = mapped_column(String(64))
    applicable_admin_levels: Mapped[list] = mapped_column(JSONB)
    input_contract: Mapped[dict] = mapped_column(JSONB)
    output_contract: Mapped[dict] = mapped_column(JSONB)
    source_citations: Mapped[list] = mapped_column(JSONB)
    calibration_status: Mapped[str] = mapped_column(String(32))
    is_default: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LossParameterSet(Base):
    __tablename__ = "loss_parameter_sets"
    __table_args__ = (
        UniqueConstraint(
            "parameter_set_id",
            "version",
            name="uq_loss_parameter_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    parameter_set_id: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(128))
    model_id: Mapped[str] = mapped_column(String(160))
    formula_version: Mapped[str] = mapped_column(String(128))
    region_id: Mapped[str] = mapped_column(String(64))
    calibration_status: Mapped[str] = mapped_column(String(32))
    quality_grade: Mapped[str] = mapped_column(String(16))
    scenarios: Mapped[dict] = mapped_column(JSONB)
    parameters: Mapped[dict] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LossProduct(Base):
    __tablename__ = "loss_products"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "product_type",
            name="uq_loss_product_run_type",
        ),
        CheckConstraint(
            "status IN ('complete','partial','unavailable','invalid')",
            name="ck_loss_product_status_value",
        ),
        CheckConstraint(
            "quality_grade IN ('L1','L2','L3','L0')",
            name="ck_loss_product_quality_value",
        ),
        CheckConstraint(
            "product_type IN "
            "('building_damage','population_impact','casualties',"
            "'economic_loss','resource_demand','validation')",
            name="ck_loss_product_type",
        ),
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
    )
    product_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    quality_grade: Mapped[str] = mapped_column(String(16))
    calibration_status: Mapped[str] = mapped_column(String(32))
    coverage_ratio: Mapped[float] = mapped_column(Numeric(8, 6))
    partial_scope: Mapped[bool] = mapped_column(Boolean)
    needs_review: Mapped[bool] = mapped_column(Boolean)
    spatialized_estimate: Mapped[bool] = mapped_column(Boolean)
    algorithm_version: Mapped[str] = mapped_column(String(128))
    parameter_version: Mapped[str] = mapped_column(String(128))
    region_profile_version: Mapped[str] = mapped_column(String(128))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    input_checksum: Mapped[str] = mapped_column(String(64))
    output_checksum: Mapped[str] = mapped_column(String(64))
    statistics: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LossMetricValue(Base):
    __tablename__ = "loss_metric_values"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "area_scope",
            "area_code",
            "metric_key",
            "value_type",
            name="uq_loss_metric_value",
        ),
        CheckConstraint(
            "value_type IN ('low','central','high')",
            name="ck_loss_metric_value_type",
        ),
        CheckConstraint(
            "value_status IN "
            "('available','zero','rounded_to_zero','unavailable','not_applicable')",
            name="ck_loss_metric_value_status",
        ),
        CheckConstraint(
            """
            (value_status IN ('available','zero','rounded_to_zero')
             AND numeric_value IS NOT NULL AND numeric_value >= 0)
            OR
            (value_status IN ('unavailable','not_applicable')
             AND numeric_value IS NULL)
            """,
            name="ck_loss_metric_value_presence",
        ),
        CheckConstraint(
            "(value_status = 'zero' AND numeric_value = 0) "
            "OR value_status <> 'zero'",
            name="ck_loss_metric_zero_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("loss_products.id", ondelete="CASCADE"),
    )
    area_scope: Mapped[str] = mapped_column(String(32))
    area_code: Mapped[str] = mapped_column(String(64))
    area_name: Mapped[str | None] = mapped_column(String(256))
    metric_key: Mapped[str] = mapped_column(String(96))
    value_type: Mapped[str] = mapped_column(String(16))
    value_status: Mapped[str] = mapped_column(String(32))
    numeric_value: Mapped[float | None] = mapped_column(Numeric(24, 6))
    unit: Mapped[str] = mapped_column(String(32))
    precision: Mapped[int | None] = mapped_column(Integer)
    quality_grade: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )


class LossProductRaster(Base):
    __tablename__ = "loss_product_rasters"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "raster_version",
            name="uq_loss_product_raster_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("loss_products.id", ondelete="CASCADE"),
    )
    raster_version: Mapped[str] = mapped_column(String(128))
    rast: Mapped[object] = mapped_column(Raster)
    band_manifest: Mapped[dict] = mapped_column(JSONB)
    checksum: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    srid: Mapped[int] = mapped_column(Integer)
    spatial_allocation_rule: Mapped[str] = mapped_column(String(128))
    coverage_ratio: Mapped[float] = mapped_column(Numeric(8, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
