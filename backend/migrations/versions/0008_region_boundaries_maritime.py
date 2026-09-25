"""Add explicit maritime geometry and required boundary audit fields."""

from collections.abc import Sequence

from alembic import op
from geoalchemy2 import Geometry
import sqlalchemy as sa

revision: str = "0008_region_boundaries_maritime"
down_revision: str | None = "0007_region_boundaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "region_boundaries",
        sa.Column(
            "maritime_geom",
            Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                spatial_index=False,
            ),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE region_boundaries
        SET maritime_geom = ST_GeomFromText('MULTIPOLYGON EMPTY', 4326)
        WHERE maritime_geom IS NULL
        """
    )
    op.alter_column(
        "region_boundaries",
        "maritime_geom",
        existing_type=Geometry(geometry_type="MULTIPOLYGON", srid=4326),
        nullable=False,
    )
    op.create_index(
        "ix_region_boundaries_maritime_geom",
        "region_boundaries",
        ["maritime_geom"],
        postgresql_using="gist",
    )

    connection = op.get_bind()
    null_audit_count = connection.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM region_boundaries
            WHERE source_uri IS NULL OR checksum IS NULL
            """
        )
    )
    if null_audit_count:
        raise RuntimeError(
            "region_boundaries contains NULL source_uri or checksum; "
            "cannot enforce audit provenance without inventing a source"
        )

    op.alter_column(
        "region_boundaries",
        "source_uri",
        existing_type=sa.String(length=512),
        nullable=False,
    )
    op.alter_column(
        "region_boundaries",
        "checksum",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "region_boundaries",
        "checksum",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.alter_column(
        "region_boundaries",
        "source_uri",
        existing_type=sa.String(length=512),
        nullable=True,
    )
    op.drop_index("ix_region_boundaries_maritime_geom", table_name="region_boundaries")
    op.drop_column("region_boundaries", "maritime_geom")
