from pathlib import Path
from uuid import UUID

import pytest

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
