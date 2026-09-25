"""Create versioned region boundary table."""

from collections.abc import Sequence

from alembic import op
from geoalchemy2 import Geometry
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0007_region_boundaries"
down_revision: str | None = "0006_collector_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "region_boundaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "local_buffer_km",
            sa.Numeric(precision=8, scale=2),
            server_default=sa.text("50"),
            nullable=False,
        ),
        sa.Column(
            "geom",
            Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                spatial_index=False,
            ),
            nullable=False,
        ),
        sa.Column("source_uri", sa.String(length=512), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version", name="uq_region_boundaries_version"),
    )
    op.create_index(
        "ix_region_boundaries_version",
        "region_boundaries",
        ["version"],
    )
    op.create_index(
        "ix_region_boundaries_is_active",
        "region_boundaries",
        ["is_active"],
    )
    op.create_index(
        "uq_region_boundaries_single_active",
        "region_boundaries",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_region_boundaries_geom",
        "region_boundaries",
        ["geom"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_region_boundaries_geom", table_name="region_boundaries")
    op.drop_index("uq_region_boundaries_single_active", table_name="region_boundaries")
    op.drop_index("ix_region_boundaries_is_active", table_name="region_boundaries")
    op.drop_index("ix_region_boundaries_version", table_name="region_boundaries")
    op.drop_table("region_boundaries")
