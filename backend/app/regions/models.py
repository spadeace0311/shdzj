import uuid
from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RegionBoundary(Base):
    __tablename__ = "region_boundaries"
    __table_args__ = (
        UniqueConstraint("version", name="uq_region_boundaries_version"),
        Index(
            "uq_region_boundaries_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index(
            "ix_region_boundaries_geom",
            "geom",
            postgresql_using="gist",
        ),
        Index(
            "ix_region_boundaries_maritime_geom",
            "maritime_geom",
            postgresql_using="gist",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    version: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    local_buffer_km: Mapped[Decimal] = mapped_column(
        Numeric(8, 2),
        default=Decimal("50"),
        server_default=text("50"),
    )
    geom = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=False,
    )
    maritime_geom = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False),
        nullable=False,
    )
    source_uri: Mapped[str] = mapped_column(String(512))
    checksum: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
    )
