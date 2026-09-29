from pathlib import Path

import pytest

from app.loss.domain import (
    LossModelType,
    LossQualityGrade,
    LossValueType,
    ScenarioParameters,
)
from app.loss.exposure import build_exposure_dataset
from app.loss.models_registry import load_parameter_set
from app.loss.population import (
    PopulationImpactUnavailable,
    assess_population_impact,
)
from app.loss.spatial import TownIntensityShare


PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")


def _parameters() -> object:
    return load_parameter_set(PARAMETERS).models[
        LossModelType.POPULATION_IMPACT
    ].scenarios[LossValueType.CENTRAL]


def _parameters_with(values: dict[str, float]) -> ScenarioParameters:
    return ScenarioParameters(values=values)


def _exposure(*, population: float = 10000.0, town_code: str = "t1") -> object:
    return build_exposure_dataset(
        snapshot_checksum="a" * 64,
        city=[{"ID": "310000", "NAME": "上海市"}],
        towns=[
            {
                "town_code": town_code,
                "county_code": "c1",
                "town_name": "测试镇",
                "population_total": population,
            }
        ],
        geometries=[
            {
                "town_code": town_code,
                "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            }
        ],
        buildings=[],
    )


def _share(
    *,
    town_code: str = "t1",
    intensity_bin: int = 7,
    area_ratio: float = 1.0,
) -> TownIntensityShare:
    return TownIntensityShare(
        town_code=town_code,
        intensity_bin=intensity_bin,
        area_ratio=area_ratio,
        intensity_min=intensity_bin - 0.5,
        intensity_max=intensity_bin + 0.5,
    )


def test_population_threshold_and_shelter_totals() -> None:
    result = assess_population_impact(
        _exposure(),
        (
            _share(intensity_bin=5, area_ratio=0.2),
            _share(intensity_bin=6, area_ratio=0.3),
            _share(intensity_bin=7, area_ratio=0.5),
        ),
        _parameters(),
    )
    town = result.towns["t1"]
    assert town.full_population == pytest.approx(10000.0)
    assert town.affected_population == pytest.approx(8000.0)
    assert town.emergency_shelter_population == pytest.approx(650.0)
    assert town.temporary_shelter_population == pytest.approx(325.0)
    assert town.emergency_shelter_population <= town.affected_population
    assert town.temporary_shelter_population <= town.affected_population
    assert result.quality_grade is LossQualityGrade.L2


def test_below_threshold_bins_remain_in_by_intensity() -> None:
    result = assess_population_impact(
        _exposure(),
        (
            _share(intensity_bin=5, area_ratio=0.2),
            _share(intensity_bin=6, area_ratio=0.3),
        ),
        _parameters(),
    )
    town = result.towns["t1"]
    assert [(item.intensity_bin, item.population) for item in town.by_intensity] == [
        (5, pytest.approx(2000.0)),
        (6, pytest.approx(3000.0)),
    ]
    assert town.affected_population == pytest.approx(3000.0)


def test_by_intensity_is_ordered_by_town_code_then_intensity_bin() -> None:
    exposure = build_exposure_dataset(
        snapshot_checksum="b" * 64,
        city=[{"ID": "310000", "NAME": "上海市"}],
        towns=[
            {
                "town_code": "t2",
                "county_code": "c1",
                "town_name": "测试镇二",
                "population_total": 1000.0,
            },
            {
                "town_code": "t1",
                "county_code": "c1",
                "town_name": "测试镇一",
                "population_total": 1000.0,
            },
        ],
        geometries=[
            {
                "town_code": "t1",
                "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            },
            {
                "town_code": "t2",
                "geometry_wkt": "POLYGON((1 0,2 0,2 1,1 1,1 0))",
            },
        ],
        buildings=[],
    )
    result = assess_population_impact(
        exposure,
        (
            _share(town_code="t2", intensity_bin=7),
            _share(town_code="t1", intensity_bin=6),
        ),
        _parameters(),
    )
    assert tuple(result.towns) == ("t1", "t2")
    assert [(item.town_code, item.intensity_bin) for item in result.towns["t1"].by_intensity] == [
        ("t1", 6)
    ]
    assert [(item.town_code, item.intensity_bin) for item in result.towns["t2"].by_intensity] == [
        ("t2", 7)
    ]


def test_empty_exposure_is_unavailable() -> None:
    exposure = build_exposure_dataset(
        snapshot_checksum="c" * 64,
        city=[{"ID": "310000", "NAME": "上海市"}],
        towns=[],
        geometries=[],
        buildings=[],
    )
    with pytest.raises(PopulationImpactUnavailable):
        assess_population_impact(exposure, (), _parameters())


def test_positive_population_town_without_share_is_unavailable() -> None:
    with pytest.raises(PopulationImpactUnavailable):
        assess_population_impact(_exposure(), (), _parameters())


def test_zero_population_town_without_share_is_zero() -> None:
    result = assess_population_impact(
        _exposure(population=0.0),
        (),
        _parameters(),
    )
    town = result.towns["t1"]
    assert town.full_population == 0.0
    assert town.affected_population == 0.0
    assert town.emergency_shelter_population == 0.0
    assert town.temporary_shelter_population == 0.0
    assert town.by_intensity == ()


@pytest.mark.parametrize("area_ratio", [-0.01, 1.01, float("nan"), float("inf")])
def test_invalid_area_ratio_is_rejected(area_ratio: float) -> None:
    with pytest.raises(ValueError, match="area_ratio"):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=6, area_ratio=area_ratio),),
            _parameters(),
        )


@pytest.mark.parametrize("intensity_bin", [0, 13, 6.5])
def test_invalid_intensity_bin_is_rejected(intensity_bin: float) -> None:
    with pytest.raises(ValueError, match="intensity"):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=intensity_bin),),
            _parameters(),
        )


def test_invalid_shelter_ratio_is_rejected() -> None:
    parameters = _parameters_with(
        {
            "affected_population_min_intensity": 6.0,
            "shelter_ratio.6": 1.01,
            "temporary_shelter_ratio": 0.5,
        }
    )
    with pytest.raises(ValueError, match="shelter_ratio"):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=6),),
            parameters,
        )


def test_invalid_temporary_shelter_ratio_is_rejected() -> None:
    parameters = _parameters_with(
        {
            "affected_population_min_intensity": 6.0,
            "shelter_ratio.6": 0.1,
            "temporary_shelter_ratio": 1.01,
        }
    )
    with pytest.raises(ValueError, match="temporary_shelter_ratio"):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=6),),
            parameters,
        )


def test_temporary_shelter_is_emergency_shelter_times_ratio() -> None:
    result = assess_population_impact(
        _exposure(population=1000.0),
        (_share(intensity_bin=7, area_ratio=1.0),),
        _parameters(),
    )
    town = result.towns["t1"]
    assert town.temporary_shelter_population == pytest.approx(
        town.emergency_shelter_population * 0.5
    )


def test_missing_affected_population_min_intensity_is_unavailable() -> None:
    parameters = _parameters_with(
        {
            "temporary_shelter_ratio": 0.5,
            "shelter_ratio.6": 0.1,
        }
    )
    with pytest.raises(
        PopulationImpactUnavailable,
        match="affected_population_min_intensity",
    ):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=6),),
            parameters,
        )


def test_missing_shelter_ratio_for_affected_bin_is_unavailable() -> None:
    parameters = _parameters_with(
        {
            "affected_population_min_intensity": 6.0,
            "temporary_shelter_ratio": 0.5,
        }
    )
    with pytest.raises(PopulationImpactUnavailable, match="shelter_ratio.6"):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=6),),
            parameters,
        )


def test_missing_temporary_shelter_ratio_is_unavailable() -> None:
    parameters = _parameters_with(
        {
            "affected_population_min_intensity": 6.0,
            "shelter_ratio.6": 0.1,
        }
    )
    with pytest.raises(
        PopulationImpactUnavailable,
        match="temporary_shelter_ratio",
    ):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=6),),
            parameters,
        )


def test_non_finite_affected_population_min_intensity_is_unavailable() -> None:
    parameters = _parameters_with(
        {
            "affected_population_min_intensity": float("nan"),
            "temporary_shelter_ratio": 0.5,
            "shelter_ratio.6": 0.1,
        }
    )
    with pytest.raises(
        PopulationImpactUnavailable,
        match="affected_population_min_intensity",
    ):
        assess_population_impact(
            _exposure(),
            (_share(intensity_bin=6),),
            parameters,
        )
