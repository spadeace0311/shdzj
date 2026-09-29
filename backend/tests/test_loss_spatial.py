from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.loss.region import load_region_loss_profile
from app.loss.spatial import (
    LossGridCell,
    TownIntensityDistributionService,
    _select_dominant_cells,
    allocate_continuous,
    allocate_integers,
)

PROFILE = load_region_loss_profile(Path("/config/loss/shanghai-region.yaml"))
RUN_ID = UUID("00000000-0000-0000-0000-000000000001")
TOWN_VERSION_ID = UUID("00000000-0000-0000-0000-000000000002")
RASTER_ID = UUID("00000000-0000-0000-0000-000000000003")


class _FakeSession:
    pass


class _FakeSpatialService(TownIntensityDistributionService):
    def __init__(
        self,
        profile,
        *,
        metadata,
        compute_rows=(),
        cell_rows=(),
        metric_towns=frozenset(),
    ) -> None:
        super().__init__(profile=profile)
        self._metadata = metadata
        self._compute_rows = compute_rows
        self._cell_rows = cell_rows
        self._metric_towns = metric_towns

    async def _run_region_id(self, session, run_id):
        return self._profile.region_id

    async def _town_version_id(self, session, *, run_id, region_id):
        return TOWN_VERSION_ID

    async def _fusion_raster_id(self, session, *, run_id):
        return RASTER_ID

    async def _raster_metadata(self, session, raster_id):
        return self._metadata

    async def _load_compute_rows(self, session, *, raster_id, town_version_id):
        return self._compute_rows

    async def _load_cell_rows(self, session, *, raster_id, town_version_id):
        return self._cell_rows

    async def _metric_town_codes(self, session, run_id):
        return set(self._metric_towns)


def _service(profile=PROFILE, **kwargs):
    return _FakeSpatialService(profile, **kwargs)


async def _seed_postgis_rows(session, run_id):
    task_id = uuid4()
    product_id = uuid4()
    raster_id = uuid4()
    asset_id = uuid4()
    town_version_id = uuid4()
    record_id = uuid4()
    now = datetime.now(UTC)
    origin_x = 500000.0
    origin_y = 3401000.0
    min_x = 500000.0
    min_y = 3400000.0
    max_x = 501000.0
    max_y = 3401000.0
    srid = 32651

    await session.execute(
        text(
            """
            INSERT INTO assessment_tasks (
                id, run_id, task_key, task_type, component,
                sequence, deadline_at, created_at, updated_at
            )
            VALUES (
                :id, :run_id, :task_key, :task_type, :component,
                :sequence, :deadline_at, :created_at, :updated_at
            )
            """
        ),
        {
            "id": task_id,
            "run_id": run_id,
            "task_key": "raw-sql-spatial-test",
            "task_type": "intensity",
            "component": "loss_spatial_test",
            "sequence": 3,
            "deadline_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO intensity_field_products (
                id, run_id, task_id, product_type, status,
                algorithm_version, parameter_version, grid_definition_version,
                region_profile_version, input_fingerprint, input_checksum,
                coverage_ratio, statistics, created_at
            )
            VALUES (
                :id, :run_id, :task_id, 'raw-sql-test', 'available',
                'fusion-inverse-variance-v1', 'parameters-v1', 'grid-v1',
                'shanghai-region-v1', :input_fingerprint, :input_checksum,
                :coverage_ratio, :statistics, :created_at
            )
            """
        ),
        {
            "id": product_id,
            "run_id": run_id,
            "task_id": task_id,
            "input_fingerprint": "a" * 64,
            "input_checksum": "b" * 64,
            "coverage_ratio": 1.0,
            "statistics": "{}",
            "created_at": now,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO intensity_rasters (
                id, product_id, rast, band_manifest, checksum,
                width, height, srid, created_at
            )
            VALUES (
                :id, :product_id,
                ST_AddBand(
                    ST_MakeEmptyRaster(
                        1, 1,
                        CAST(:origin_x AS double precision),
                        CAST(:origin_y AS double precision),
                        CAST(:scale_x AS double precision),
                        CAST(:scale_y AS double precision),
                        0, 0,
                        CAST(:srid AS integer)
                    ),
                    1::integer,
                    '64BF'::text,
                    CAST(:intensity AS double precision),
                    NULL::double precision
                ),
                :band_manifest, :checksum,
                1, 1, :srid, :created_at
            )
            """
        ),
        {
            "id": raster_id,
            "product_id": product_id,
            "origin_x": origin_x,
            "origin_y": origin_y,
            "scale_x": 1000.0,
            "scale_y": -1000.0,
            "srid": srid,
            "intensity": 6.5,
            "band_manifest": "{}",
            "checksum": "c" * 64,
            "created_at": now,
        },
    )
    existing_asset_id = await session.scalar(
        text(
            """
            SELECT id
            FROM data_assets
            WHERE asset_key = :asset_key
              AND region_id = :region_id
            """
        ),
        {
            "asset_key": "shanghai.admin.town",
            "region_id": "shanghai",
        },
    )
    if existing_asset_id is None:
        await session.execute(
            text(
                """
                INSERT INTO data_assets (
                    id, asset_key, region_id, name, data_type,
                    spatial_granularity, responsibility_unit, update_interval_days,
                    is_core, contract, created_at, updated_at
                )
                VALUES (
                    :id, :asset_key, :region_id, :name, :data_type,
                    :spatial_granularity, :responsibility_unit, :update_interval_days,
                    :is_core, :contract, :created_at, :updated_at
                )
                """
            ),
            {
                "id": asset_id,
                "asset_key": "shanghai.admin.town",
                "region_id": "shanghai",
                "name": "test town boundary",
                "data_type": "vector",
                "spatial_granularity": "town",
                "responsibility_unit": "test",
                "update_interval_days": 365,
                "is_core": True,
                "contract": "{}",
                "created_at": now,
                "updated_at": now,
            },
        )
    else:
        asset_id = existing_asset_id
    await session.execute(
        text(
            """
            INSERT INTO data_asset_versions (
                id, asset_id, version, status, source_uri,
                schema_summary, record_count, source_crs,
                created_at, updated_at
            )
            VALUES (
                :id, :asset_id, :version, 'imported', :source_uri,
                :schema_summary, :record_count, :source_crs,
                :created_at, :updated_at
            )
            """
        ),
        {
            "id": town_version_id,
            "asset_id": asset_id,
            "version": "raw-sql-test",
            "source_uri": "test://raw-sql-town",
            "schema_summary": "{}",
            "record_count": 1,
            "source_crs": "EPSG:4326",
            "created_at": now,
            "updated_at": now,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO data_asset_records (
                id, version_id, row_number, business_key, properties,
                geom, created_at
            )
            VALUES (
                :id, :version_id, :row_number, :business_key, :properties,
                ST_Transform(
                    ST_MakeEnvelope(
                        :min_x, :min_y, :max_x, :max_y, :srid
                    ),
                    4326
                ),
                :created_at
            )
            """
        ),
        {
            "id": record_id,
            "version_id": town_version_id,
            "row_number": 1,
            "business_key": "t1",
            "properties": "{}",
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
            "srid": srid,
            "created_at": now,
        },
    )
    return raster_id, town_version_id


async def test_raw_sql_paths_return_expected_rows(
    session_factory,
    seeded_assessment_run,
) -> None:
    async with session_factory() as session:
        raster_id, town_version_id = await _seed_postgis_rows(
            session,
            seeded_assessment_run,
        )
        rows = await TownIntensityDistributionService()._load_compute_rows(
            session,
            raster_id=raster_id,
            town_version_id=town_version_id,
        )
        assert len(rows) == 1
        assert rows[0]["town_code"] == "t1"
        assert int(rows[0]["intensity_bin"]) == 6
        assert float(rows[0]["area_ratio"]) == pytest.approx(1.0)
        assert float(rows[0]["intensity_min"]) == pytest.approx(5.5)
        assert float(rows[0]["intensity_max"]) == pytest.approx(6.5)

        cell_rows = await TownIntensityDistributionService()._load_cell_rows(
            session,
            raster_id=raster_id,
            town_version_id=town_version_id,
        )
        assert len(cell_rows) == 1
        assert cell_rows[0]["cell_id"] == "0:0"
        assert cell_rows[0]["town_code"] == "t1"
        assert float(cell_rows[0]["area_ratio"]) == pytest.approx(1.0)
        assert float(cell_rows[0]["response_weight"]) == pytest.approx(6.5)
        await session.rollback()


def test_continuous_allocation_preserves_town_total() -> None:
    cells = (
        LossGridCell("g1", "t1", 0.25, 4.0),
        LossGridCell("g2", "t1", 0.75, 8.0),
    )
    allocated = allocate_continuous({"t1": 100.0}, cells)
    assert sum(allocated.values()) == pytest.approx(100.0)
    assert allocated["g1"] == pytest.approx(14.2857142857, rel=1e-9)
    assert allocated["g2"] == pytest.approx(85.7142857143, rel=1e-9)


def test_integer_allocation_preserves_total_with_largest_remainder() -> None:
    cells = (
        LossGridCell("g1", "t1", 0.25, 4.0),
        LossGridCell("g2", "t1", 0.75, 8.0),
    )
    allocated = allocate_integers({"t1": 7}, cells)
    assert sum(allocated.values()) == 7
    assert allocated == {"g1": 2, "g2": 5}


def test_zero_weight_cells_receive_zero() -> None:
    cells = (
        LossGridCell("g1", "t1", 1.0, 0.0),
        LossGridCell("g2", "t1", 0.0, 10.0),
    )
    assert allocate_continuous({"t1": 5.0}, cells) == {"g1": 5.0, "g2": 0.0}


def test_select_dominant_cells_uses_area_then_town_code() -> None:
    rows = (
        {"cell_id": "0:0", "town_code": "t2", "area_ratio": 0.4, "response_weight": 6.0},
        {"cell_id": "0:0", "town_code": "t1", "area_ratio": 0.6, "response_weight": 6.0},
    )
    assert _select_dominant_cells(rows)[0]["town_code"] == "t1"

    tied = (
        {"cell_id": "0:0", "town_code": "t2", "area_ratio": 0.6, "response_weight": 6.0},
        {"cell_id": "0:0", "town_code": "t1", "area_ratio": 0.6, "response_weight": 6.0},
    )
    assert _select_dominant_cells(tied)[0]["town_code"] == "t1"


async def test_load_cells_assigns_overlapping_cell_deterministically() -> None:
    service = _service(
        metadata={"srid": 32651, "resolution_m": 1000.0},
        cell_rows=(
            {"cell_id": "0:0", "town_code": "t2", "area_ratio": 0.4, "response_weight": 6.0},
            {"cell_id": "0:0", "town_code": "t1", "area_ratio": 0.6, "response_weight": 6.0},
        ),
    )
    cells = await service.load_cells(_FakeSession(), run_id=RUN_ID)
    assert [cell.town_code for cell in cells] == ["t1"]


async def test_load_cells_fails_for_metric_town_with_only_secondary_overlap() -> None:
    service = _service(
        metadata={"srid": 32651, "resolution_m": 1000.0},
        cell_rows=(
            {"cell_id": "0:0", "town_code": "t1", "area_ratio": 1.0, "response_weight": 6.0},
        ),
        metric_towns={"t1", "t2"},
    )
    with pytest.raises(LookupError, match="t2"):
        await service.load_cells(_FakeSession(), run_id=RUN_ID)


async def test_compute_accepts_matching_raster_metadata() -> None:
    service = _service(
        metadata={"srid": 32651, "resolution_m": 1000.0},
        compute_rows=(
            {
                "town_code": "t1",
                "intensity_bin": 6,
                "area_ratio": 1.0,
                "intensity_min": 5.5,
                "intensity_max": 6.5,
            },
        ),
    )
    shares = await service.compute(_FakeSession(), run_id=RUN_ID)
    assert shares[0].town_code == "t1"
    assert shares[0].intensity_bin == 6


@pytest.mark.parametrize(
    ("metadata", "match"),
    (
        ({"srid": 4326, "resolution_m": 1000.0}, "SRID"),
        ({"srid": 32651, "resolution_m": 500.0}, "resolution"),
    ),
)
async def test_compute_rejects_raster_metadata_mismatch(metadata, match) -> None:
    service = _service(metadata=metadata)
    with pytest.raises(ValueError, match=match):
        await service.compute(_FakeSession(), run_id=RUN_ID)


@pytest.mark.parametrize(
    ("metadata", "match"),
    (
        ({"srid": 4326, "resolution_m": 1000.0}, "SRID"),
        ({"srid": 32651, "resolution_m": 500.0}, "resolution"),
    ),
)
async def test_load_cells_rejects_raster_metadata_mismatch(metadata, match) -> None:
    service = _service(metadata=metadata)
    with pytest.raises(ValueError, match=match):
        await service.load_cells(_FakeSession(), run_id=RUN_ID)


async def test_load_cells_excludes_missing_intensity_instead_of_zeroing() -> None:
    service = _service(
        metadata={"srid": 32651, "resolution_m": 1000.0},
        cell_rows=(
            {"cell_id": "0:0", "town_code": "t1", "area_ratio": 1.0, "response_weight": None},
        ),
    )
    assert await service.load_cells(_FakeSession(), run_id=RUN_ID) == ()
