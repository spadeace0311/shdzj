from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isclose, isfinite
from uuid import UUID

from pyproj import CRS
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun
from app.config import settings
from app.loss.region import RegionLossProfile, load_region_loss_profile


@dataclass(frozen=True, slots=True)
class TownIntensityShare:
    town_code: str
    intensity_bin: int
    area_ratio: float
    intensity_min: float
    intensity_max: float


@dataclass(frozen=True, slots=True)
class LossGridCell:
    cell_id: str
    town_code: str
    area_ratio: float
    response_weight: float

    def __post_init__(self) -> None:
        if not self.cell_id.strip():
            raise ValueError("cell_id must not be empty")
        if not self.town_code.strip():
            raise ValueError("town_code must not be empty")
        if not isfinite(self.area_ratio) or not 0.0 <= self.area_ratio <= 1.0:
            raise ValueError("area_ratio must be between zero and one")
        if not isfinite(self.response_weight) or self.response_weight < 0.0:
            raise ValueError("response_weight must not be negative")


def _validate_cells(cells: Sequence[LossGridCell]) -> None:
    seen: set[str] = set()
    for cell in cells:
        if cell.cell_id in seen:
            raise ValueError("duplicate cell_id")
        seen.add(cell.cell_id)


def allocate_continuous(
    town_values: Mapping[str, float],
    cells: Sequence[LossGridCell],
) -> dict[str, float]:
    _validate_cells(cells)
    result = {cell.cell_id: 0.0 for cell in cells}
    for town_code in sorted(town_values):
        town_cells = sorted(
            (cell for cell in cells if cell.town_code == town_code),
            key=lambda cell: cell.cell_id,
        )
        target = float(town_values[town_code])
        weights = [
            cell.area_ratio * cell.response_weight for cell in town_cells
        ]
        total_weight = sum(weights)
        if total_weight <= 0.0:
            weights = [cell.area_ratio for cell in town_cells]
            total_weight = sum(weights)
        if target < 0.0 or total_weight <= 0.0:
            if target:
                raise ValueError(
                    "positive continuous allocation requires positive weight"
                )
            continue
        for cell, weight in zip(town_cells, weights, strict=True):
            result[cell.cell_id] = target * weight / total_weight
    return result


def allocate_integers(
    town_values: Mapping[str, int],
    cells: Sequence[LossGridCell],
) -> dict[str, int]:
    _validate_cells(cells)
    result = {cell.cell_id: 0 for cell in cells}
    for town_code in sorted(town_values):
        town_cells = sorted(
            (cell for cell in cells if cell.town_code == town_code),
            key=lambda cell: cell.cell_id,
        )
        target = int(town_values[town_code])
        total_weight = sum(cell.response_weight for cell in town_cells)
        if target < 0 or total_weight <= 0:
            if target:
                raise ValueError("positive integer allocation requires positive weight")
            continue
        exact = [
            target * cell.response_weight / total_weight
            for cell in town_cells
        ]
        floors = [int(value) for value in exact]
        remainder = target - sum(floors)
        order = sorted(
            range(len(town_cells)),
            key=lambda index: (
                -(exact[index] - floors[index]),
                town_cells[index].cell_id,
            ),
        )
        for index in order[:remainder]:
            floors[index] += 1
        for cell, value in zip(town_cells, floors, strict=True):
            result[cell.cell_id] = value
    return result


def _select_dominant_cells(
    rows: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    """Select one town per grid cell deterministically.

    The task contract requires each ``LossGridCell`` to have a unique
    ``cell_id``. For cells that overlap several town polygons, this is an
    approximation: the town with the largest intersection area owns the cell,
    with ``town_code`` as the deterministic tie-break. Secondary overlaps are
    intentionally not represented as separate cell rows.
    """
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        cell_id = str(row["cell_id"])
        if float(row["area_ratio"]) <= 0.0:
            continue
        if row.get("response_weight") is None:
            continue
        grouped.setdefault(cell_id, []).append(row)

    selected: list[Mapping[str, object]] = []
    for cell_id, candidates in grouped.items():
        winner = sorted(
            candidates,
            key=lambda row: (
                -float(row["area_ratio"]),
                str(row["town_code"]),
            ),
        )[0]
        selected.append(winner)
    return tuple(
        sorted(
            selected,
            key=lambda row: (str(row["cell_id"]), str(row["town_code"])),
        )
    )


class TownIntensityDistributionService:
    def __init__(self, *, profile: RegionLossProfile | None = None) -> None:
        self._profile = profile or load_region_loss_profile(
            settings.loss_region_profile_path
        )

    async def compute(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
    ) -> tuple[TownIntensityShare, ...]:
        region_id = await self._run_region_id(session, run_id)
        town_version_id = await self._town_version_id(
            session,
            run_id=run_id,
            region_id=region_id,
        )
        raster_id = await self._fusion_raster_id(session, run_id=run_id)
        await self._ensure_raster_metadata(session, raster_id)
        rows = await self._load_compute_rows(
            session,
            raster_id=raster_id,
            town_version_id=town_version_id,
        )
        return tuple(
            TownIntensityShare(
                town_code=str(row["town_code"]),
                intensity_bin=int(row["intensity_bin"]),
                area_ratio=float(row["area_ratio"]),
                intensity_min=float(row["intensity_min"]),
                intensity_max=float(row["intensity_max"]),
            )
            for row in rows
        )

    async def load_cells(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
    ) -> tuple[LossGridCell, ...]:
        region_id = await self._run_region_id(session, run_id)
        town_version_id = await self._town_version_id(
            session,
            run_id=run_id,
            region_id=region_id,
        )
        raster_id = await self._fusion_raster_id(session, run_id=run_id)
        await self._ensure_raster_metadata(session, raster_id)
        rows = await self._load_cell_rows(
            session,
            raster_id=raster_id,
            town_version_id=town_version_id,
        )
        rows = _select_dominant_cells(rows)
        cells = tuple(
            LossGridCell(
                cell_id=str(row["cell_id"]),
                town_code=str(row["town_code"]),
                area_ratio=float(row["area_ratio"]),
                response_weight=float(row["response_weight"]),
            )
            for row in rows
        )
        await self._ensure_metric_towns_covered(session, run_id, cells)
        return cells

    def _expected_area_srid(self) -> int:
        srid = CRS.from_user_input(self._profile.area_crs).to_epsg()
        if srid is None:
            raise ValueError("region profile area CRS must map to an EPSG code")
        return srid

    async def _ensure_raster_metadata(
        self,
        session: AsyncSession,
        raster_id: UUID,
    ) -> None:
        row = await self._raster_metadata(session, raster_id)
        expected_srid = self._expected_area_srid()
        if int(row["srid"]) != expected_srid:
            raise ValueError(
                "fused intensity raster SRID does not match region profile area CRS"
            )
        if not isclose(
            float(row["resolution_m"]),
            float(self._profile.grid_resolution_m),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise ValueError(
                "fused intensity raster resolution does not match "
                "region profile grid_resolution_m"
            )

    async def _raster_metadata(
        self,
        session: AsyncSession,
        raster_id: UUID,
    ) -> Mapping[str, object]:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        ST_SRID(rast) AS srid,
                        ABS(ST_ScaleX(rast)) AS resolution_m
                    FROM intensity_rasters
                    WHERE id = :raster_id
                    """
                ),
                {"raster_id": raster_id},
            )
        ).mappings().one_or_none()
        if row is None:
            raise LookupError("fused intensity raster metadata not found")
        return row

    async def _load_compute_rows(
        self,
        session: AsyncSession,
        *,
        raster_id: UUID,
        town_version_id: UUID,
    ) -> tuple[Mapping[str, object], ...]:
        rows = (
            await session.execute(
                text(
                    """
                    WITH raster AS (
                        SELECT
                            rast,
                            ST_Width(rast) AS width,
                            ST_Height(rast) AS height,
                            ABS(ST_ScaleX(rast)) AS resolution_m,
                            ST_UpperLeftX(rast) AS origin_x,
                            ST_UpperLeftY(rast) AS origin_y,
                            ST_SRID(rast) AS srid
                        FROM intensity_rasters
                        WHERE id = :raster_id
                    ),
                    towns AS (
                        SELECT
                            business_key AS town_code,
                            geom AS geom_4326
                        FROM data_asset_records
                        WHERE version_id = :town_version_id
                          AND geom IS NOT NULL
                    ),
                    cells AS (
                        SELECT
                            row_number AS row_index,
                            column_number AS column_index,
                            ST_MakeEnvelope(
                                raster.origin_x
                                    + column_number * raster.resolution_m,
                                raster.origin_y
                                    - (row_number + 1) * raster.resolution_m,
                                raster.origin_x
                                    + (column_number + 1) * raster.resolution_m,
                                raster.origin_y
                                    - row_number * raster.resolution_m,
                                raster.srid
                            ) AS cell_geom,
                            raster.rast
                        FROM raster
                        CROSS JOIN LATERAL
                            generate_series(0, raster.height - 1) AS row_number
                        CROSS JOIN LATERAL
                            generate_series(0, raster.width - 1) AS column_number
                    ),
                    cell_town AS (
                        SELECT
                            towns.town_code,
                            cells.rast,
                            cells.cell_geom,
                            ST_Area(
                                ST_Intersection(
                                    cells.cell_geom,
                                    ST_Transform(
                                        towns.geom_4326,
                                        ST_SRID(cells.cell_geom)
                                    )
                                )
                            ) AS intersection_area,
                            ST_Value(
                                cells.rast,
                                1,
                                (cells.column_index + 1)::integer,
                                (cells.row_index + 1)::integer
                            ) AS intensity_value
                        FROM cells
                        JOIN towns
                          ON ST_Intersects(
                              cells.cell_geom,
                              ST_Transform(
                                  towns.geom_4326,
                                  ST_SRID(cells.cell_geom)
                              )
                          )
                    ),
                    valid_cells AS (
                        SELECT
                            town_code,
                            LEAST(
                                12,
                                GREATEST(
                                    1,
                                    FLOOR(intensity_value)::integer
                                )
                            ) AS intensity_bin,
                            intersection_area
                        FROM cell_town
                        WHERE intensity_value IS NOT NULL
                          AND intersection_area > 0
                    ),
                    town_areas AS (
                        SELECT
                            town_code,
                            SUM(intersection_area) AS town_intersection_area
                        FROM valid_cells
                        GROUP BY town_code
                    )
                    SELECT
                        valid_cells.town_code,
                        valid_cells.intensity_bin,
                        SUM(valid_cells.intersection_area)
                            / town_areas.town_intersection_area AS area_ratio,
                        valid_cells.intensity_bin - 0.5 AS intensity_min,
                        valid_cells.intensity_bin + 0.5 AS intensity_max
                    FROM valid_cells
                    JOIN town_areas USING (town_code)
                    GROUP BY
                        valid_cells.town_code,
                        valid_cells.intensity_bin,
                        town_areas.town_intersection_area
                    ORDER BY
                        valid_cells.town_code,
                        valid_cells.intensity_bin
                    """
                ),
                {
                    "raster_id": raster_id,
                    "town_version_id": town_version_id,
                },
            )
        ).mappings()
        return tuple(rows)

    async def _load_cell_rows(
        self,
        session: AsyncSession,
        *,
        raster_id: UUID,
        town_version_id: UUID,
    ) -> tuple[Mapping[str, object], ...]:
        rows = (
            await session.execute(
                text(
                    """
                    WITH raster AS (
                        SELECT
                            rast,
                            ST_Width(rast) AS width,
                            ST_Height(rast) AS height,
                            ABS(ST_ScaleX(rast)) AS resolution_m,
                            ST_UpperLeftX(rast) AS origin_x,
                            ST_UpperLeftY(rast) AS origin_y,
                            ST_SRID(rast) AS srid
                        FROM intensity_rasters
                        WHERE id = :raster_id
                    ),
                    towns AS (
                        SELECT
                            business_key AS town_code,
                            geom AS geom_4326
                        FROM data_asset_records
                        WHERE version_id = :town_version_id
                          AND geom IS NOT NULL
                    ),
                    cells AS (
                        SELECT
                            row_number AS row_index,
                            column_number AS column_index,
                            ST_MakeEnvelope(
                                raster.origin_x
                                    + column_number * raster.resolution_m,
                                raster.origin_y
                                    - (row_number + 1) * raster.resolution_m,
                                raster.origin_x
                                    + (column_number + 1) * raster.resolution_m,
                                raster.origin_y
                                    - row_number * raster.resolution_m,
                                raster.srid
                            ) AS cell_geom,
                            raster.rast
                        FROM raster
                        CROSS JOIN LATERAL
                            generate_series(0, raster.height - 1) AS row_number
                        CROSS JOIN LATERAL
                            generate_series(0, raster.width - 1) AS column_number
                    ),
                    intersections AS (
                        SELECT
                            cells.row_index::text
                                || ':'
                                || cells.column_index::text AS cell_id,
                            towns.town_code,
                            ST_Area(
                                ST_Intersection(
                                    cells.cell_geom,
                                    ST_Transform(
                                        towns.geom_4326,
                                        ST_SRID(cells.cell_geom)
                                    )
                                )
                            ) / ST_Area(cells.cell_geom) AS area_ratio,
                            ST_Value(
                                cells.rast,
                                1,
                                (cells.column_index + 1)::integer,
                                (cells.row_index + 1)::integer
                            ) AS response_weight
                        FROM cells
                        JOIN towns
                          ON ST_Intersects(
                              cells.cell_geom,
                              ST_Transform(
                                  towns.geom_4326,
                                  ST_SRID(cells.cell_geom)
                              )
                          )
                    )
                    SELECT
                        cell_id,
                        town_code,
                        area_ratio,
                        response_weight
                    FROM intersections
                    WHERE response_weight IS NOT NULL
                      AND area_ratio > 0
                    ORDER BY cell_id, town_code
                    """
                ),
                {
                    "raster_id": raster_id,
                    "town_version_id": town_version_id,
                },
            )
        ).mappings()
        return tuple(rows)

    async def _metric_town_codes(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> set[str]:
        values = await session.scalars(
            text(
                """
                SELECT DISTINCT metric.area_code
                FROM loss_metric_values AS metric
                JOIN loss_products AS product
                  ON product.id = metric.product_id
                WHERE product.run_id = :run_id
                  AND metric.area_scope = 'town'
                """
            ),
            {"run_id": run_id},
        )
        return {str(value) for value in values.all()}

    async def _run_region_id(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> str:
        run = await session.get(AssessmentRun, run_id)
        if run is None:
            raise LookupError("assessment run not found")
        region_id = run.snapshot.get("region_id")
        if not isinstance(region_id, str) or not region_id.strip():
            raise ValueError("assessment run snapshot requires region_id")
        if region_id != self._profile.region_id:
            raise ValueError("loss region profile does not match run region_id")
        return region_id

    async def _town_version_id(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        region_id: str,
    ) -> UUID:
        version_id = (
            await session.execute(
                text(
                    """
                    SELECT asset_version_id
                    FROM data_asset_snapshots
                    WHERE run_id = :run_id
                      AND asset_key = :asset_key
                      AND region_id = :region_id
                    LIMIT 1
                    """
                ),
                {
                    "run_id": run_id,
                    "asset_key": self._profile.asset_keys.admin_town,
                    "region_id": region_id,
                },
            )
        ).scalar_one_or_none()
        if version_id is None:
            raise LookupError("locked administrative-town geometry not found")
        return UUID(str(version_id))

    async def _fusion_raster_id(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
    ) -> UUID:
        raster_id = (
            await session.execute(
                text(
                    """
                    SELECT raster.id
                    FROM intensity_field_products AS product
                    JOIN intensity_rasters AS raster
                      ON raster.product_id = product.id
                    WHERE product.run_id = :run_id
                      AND product.product_type = 'fusion'
                      AND product.status IN ('available', 'partial')
                    ORDER BY
                        product.completed_at DESC NULLS LAST,
                        product.created_at DESC
                    LIMIT 1
                    """
                ),
                {"run_id": run_id},
            )
        ).scalar_one_or_none()
        if raster_id is None:
            raise LookupError("succeeded intensity fusion raster not found")
        return UUID(str(raster_id))

    async def _ensure_metric_towns_covered(
        self,
        session: AsyncSession,
        run_id: UUID,
        cells: Sequence[LossGridCell],
    ) -> None:
        metric_towns = await self._metric_town_codes(session, run_id)
        covered = {cell.town_code for cell in cells}
        missing = sorted(metric_towns - covered)
        if missing:
            raise LookupError(
                "persisted town metrics have no grid cell: "
                + ", ".join(missing)
            )
