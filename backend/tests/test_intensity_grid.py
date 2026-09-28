import numpy as np
import pytest

from app.intensity.domain import GridDefinition
from app.intensity.grid import (
    grid_definition_checksum,
    grid_definition_from_bounds,
    sample_grid,
)


def test_grid_definition_aligns_to_whole_cells() -> None:
    definition = grid_definition_from_bounds(
        min_x=999.0,
        min_y=1999.0,
        max_x=3001.0,
        max_y=5001.0,
        resolution_m=1000,
        crs="EPSG:32651",
        version="grid-test-1",
    )

    assert definition.origin_x == 0.0
    assert definition.origin_y == 6000.0
    assert definition.width == 4
    assert definition.height == 5
    assert definition.center_xy(0, 0) == (500.0, 5500.0)
    assert len(grid_definition_checksum(definition)) == 64


def test_sampling_returns_stable_rows_and_distances() -> None:
    definition = GridDefinition(
        version="grid-test-1",
        crs="EPSG:32651",
        resolution_m=1000,
        origin_x=0.0,
        origin_y=2000.0,
        width=2,
        height=2,
    )
    epicenter_lon = 121.5
    epicenter_lat = 31.2
    first = sample_grid(
        definition,
        epicenter_longitude=epicenter_lon,
        epicenter_latitude=epicenter_lat,
    )
    second = sample_grid(
        definition,
        epicenter_longitude=epicenter_lon,
        epicenter_latitude=epicenter_lat,
    )

    assert first.rows.tolist() == [0, 0, 1, 1]
    assert first.columns.tolist() == [0, 1, 0, 1]
    assert first.distance_km.shape == (4,)
    assert first.azimuth_deg.shape == (4,)
    assert first.x_km.shape == (4,)
    assert first.y_km.shape == (4,)
    assert np.allclose(first.longitude, second.longitude)
    assert np.allclose(first.distance_km, second.distance_km)
    assert np.all(first.distance_km >= 0)


def test_definition_rejects_invalid_geometry() -> None:
    with pytest.raises(ValueError):
        GridDefinition(
            version="bad",
            crs="EPSG:32651",
            resolution_m=0,
            origin_x=0,
            origin_y=0,
            width=1,
            height=1,
        )
