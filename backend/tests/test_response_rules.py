from decimal import Decimal

import pytest

from app.config import settings
from app.events.response_rules import (
    ResponseInput,
    ResponseRuleEngine,
    ResponseSuggestion,
)


def _engine() -> ResponseRuleEngine:
    return ResponseRuleEngine.from_yaml(settings.response_rules_path)


def _suggest(
    *,
    magnitude: str,
    depth_km: str,
    inside_shanghai: bool | None,
    distance_to_boundary_km: str | None = None,
    deaths: int | None = None,
    max_intensity: str | None = None,
) -> ResponseSuggestion:
    return _engine().suggest(
        ResponseInput(
            magnitude=Decimal(magnitude),
            depth_km=Decimal(depth_km),
            inside_shanghai=inside_shanghai,
            distance_to_boundary_km=(
                None if distance_to_boundary_km is None else Decimal(distance_to_boundary_km)
            ),
            deaths=deaths,
            max_intensity=None if max_intensity is None else Decimal(max_intensity),
        )
    )


def test_shanghai_m5_2_is_major_and_service_level_two() -> None:
    result = _suggest(
        magnitude="5.2",
        depth_km="12",
        inside_shanghai=True,
        max_intensity="6",
    )

    assert result.institutional_level == "major"
    assert result.service_level == 2
    assert result.downgraded is False
    assert result.rule_version == "2026.1"
    assert "上海行政区域" in result.causes[0]


def test_external_m4_4_at_30_km_has_no_institutional_recommendation() -> None:
    result = _suggest(
        magnitude="4.4",
        depth_km="10",
        inside_shanghai=False,
        distance_to_boundary_km="30",
        deaths=0,
        max_intensity="4",
    )

    assert result.institutional_level == "none"
    assert result.service_level is None
    assert result.downgraded is True


@pytest.mark.parametrize(
    ("magnitude", "institutional_level", "service_level"),
    [
        ("3.0", "general", 4),
        ("3.9", "general", 4),
        ("4.0", "larger", 3),
        ("4.9", "larger", 3),
        ("5.0", "major", 2),
        ("5.9", "major", 2),
        ("6.0", "special_major", 1),
    ],
)
def test_local_magnitude_band_boundaries_are_inclusive_or_exclusive(
    magnitude: str,
    institutional_level: str,
    service_level: int,
) -> None:
    result = _suggest(magnitude=magnitude, depth_km="12", inside_shanghai=True)

    assert result.institutional_level == institutional_level
    assert result.service_level == service_level
    assert result.downgraded is False


@pytest.mark.parametrize(
    ("deaths", "institutional_level"),
    [
        (0, "general"),
        (1, "larger"),
        (9, "larger"),
        (10, "major"),
        (99, "major"),
        (100, "special_major"),
    ],
)
def test_external_death_band_boundaries_are_inclusive_or_exclusive(
    deaths: int,
    institutional_level: str,
) -> None:
    result = _suggest(
        magnitude="4.4",
        depth_km="10",
        inside_shanghai=False,
        distance_to_boundary_km="10",
        deaths=deaths,
    )

    assert result.institutional_level == institutional_level
    assert result.service_level is None
    assert result.downgraded is False


def test_external_negative_deaths_has_no_institutional_recommendation() -> None:
    result = _suggest(
        magnitude="4.4",
        depth_km="10",
        inside_shanghai=False,
        distance_to_boundary_km="10",
        deaths=-1,
    )

    assert result.institutional_level == "none"
    assert result.service_level is None
    assert result.downgraded is False


@pytest.mark.parametrize("distance_km", ["20", "100"])
def test_external_boundary_edges_are_inclusive(distance_km: str) -> None:
    result = _suggest(
        magnitude="4.4",
        depth_km="10",
        inside_shanghai=False,
        distance_to_boundary_km=distance_km,
        deaths=0,
    )

    assert result.institutional_level == "none"
    assert result.service_level is None
    assert result.downgraded is True


@pytest.mark.parametrize("distance_km", ["19.9", "100.1"])
def test_external_outside_downgrade_window_keeps_level(distance_km: str) -> None:
    result = _suggest(
        magnitude="4.4",
        depth_km="10",
        inside_shanghai=False,
        distance_to_boundary_km=distance_km,
        deaths=100,
    )

    assert result.institutional_level == "special_major"
    assert result.service_level is None
    assert result.downgraded is False


def test_external_distance_20_to_100_downgrades_one_level() -> None:
    result = _suggest(
        magnitude="4.4",
        depth_km="10",
        inside_shanghai=False,
        distance_to_boundary_km="50",
        deaths=100,
    )

    assert result.institutional_level == "major"
    assert result.service_level is None
    assert result.downgraded is True


@pytest.mark.parametrize(
    ("depth_km", "institutional_level", "downgraded"),
    [
        ("60", "major", False),
        ("60.1", "larger", True),
    ],
)
def test_local_depth_downgrade_is_strictly_above_60_km(
    depth_km: str,
    institutional_level: str,
    downgraded: bool,
) -> None:
    result = _suggest(
        magnitude="5.2",
        depth_km=depth_km,
        inside_shanghai=True,
    )

    assert result.institutional_level == institutional_level
    assert result.downgraded is downgraded


@pytest.mark.parametrize(
    ("depth_km", "service_level"),
    [
        ("70", 2),
        ("70.1", 3),
    ],
)
def test_service_depth_downgrade_is_strictly_above_70_km(
    depth_km: str,
    service_level: int,
) -> None:
    result = _suggest(
        magnitude="5.2",
        depth_km=depth_km,
        inside_shanghai=True,
    )

    assert result.service_level == service_level


def test_service_depth_downgrade_caps_at_level_four() -> None:
    result = _suggest(magnitude="3.0", depth_km="80", inside_shanghai=True)

    assert result.institutional_level == "none"
    assert result.service_level == 4
    assert result.downgraded is True


def test_missing_shanghai_relation_is_pending() -> None:
    result = _suggest(
        magnitude="5.2",
        depth_km="12",
        inside_shanghai=None,
        distance_to_boundary_km="30",
    )

    assert result.institutional_level == "pending"
    assert result.service_level is None
    assert result.downgraded is False
    assert "缺少上海行政边界和震中位置关系" in result.causes[0]


def test_external_missing_deaths_is_none() -> None:
    result = _suggest(
        magnitude="5.2",
        depth_km="12",
        inside_shanghai=False,
        distance_to_boundary_km="10",
    )

    assert result.institutional_level == "none"
    assert result.service_level is None
    assert result.downgraded is False
    assert "缺少死亡人数" in result.causes[0]


def test_max_intensity_at_threshold_triggers_special_assessment() -> None:
    result = _suggest(
        magnitude="2.0",
        depth_km="12",
        inside_shanghai=True,
        max_intensity="2.0",
    )

    assert result.institutional_level == "none"
    assert any("触发专项评估" in cause for cause in result.causes)


def test_max_intensity_below_threshold_does_not_trigger() -> None:
    result = _suggest(
        magnitude="2.0",
        depth_km="12",
        inside_shanghai=True,
        max_intensity="1.9",
    )

    assert not any("触发专项评估" in cause for cause in result.causes)


def test_institutional_and_service_levels_do_not_mix() -> None:
    result = _suggest(magnitude="5.2", depth_km="80", inside_shanghai=True)

    assert result.institutional_level == "larger"
    assert result.service_level == 3
    assert result.downgraded is True
