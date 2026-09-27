from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    GridDefinition,
    GridSamples,
    IntensityEventSnapshot,
)
from app.intensity.model import (
    FaultCandidate,
    ModelFieldConvergenceError,
    axis_intensity,
    evaluate_model,
    resolve_direction,
)
from app.intensity.parameters import load_parameter_bundle


PARAMETERS = load_parameter_bundle("/config/intensity/shanghai-2019.yaml")


def test_direction_priority_and_stable_fault_tie_break() -> None:
    candidates = (
        FaultCandidate(fault_id="b", strike_deg=30.0, distance_km=5.0),
        FaultCandidate(fault_id="a", strike_deg=20.0, distance_km=5.0),
    )

    assert resolve_direction(
        override_deg=10.0,
        focal_mechanism_deg=20.0,
        finite_fault_deg=30.0,
        candidates=candidates,
    ).source == "manual_override"
    assert resolve_direction(
        override_deg=None,
        focal_mechanism_deg=20.0,
        finite_fault_deg=30.0,
        candidates=candidates,
    ).strike_deg == 20.0
    assert resolve_direction(
        override_deg=None,
        focal_mechanism_deg=None,
        finite_fault_deg=30.0,
        candidates=candidates,
    ).strike_deg == 30.0
    decision = resolve_direction(
        override_deg=None,
        focal_mechanism_deg=None,
        finite_fault_deg=None,
        candidates=candidates,
    )
    assert decision.candidate_fault_id == "a"
    assert decision.strike_deg == 20.0


def test_no_direction_uses_axis_average() -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([10.0]),
        y_km=np.array([0.0]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([10.0]),
        azimuth_deg=np.array([0.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    result = evaluate_model(
        snapshot,
        samples,
        DirectionDecision(DirectionStatus.UNCERTAIN, None, None),
        PARAMETERS.model,
    )
    expected = (5.806964124806861 + 5.552603218791649) / 2
    assert result.values[0] == pytest.approx(expected, abs=1e-6)
    assert result.sigma[0] == pytest.approx(
        ((0.6310**2 + 0.6638**2) / 2) ** 0.5
    )
    assert result.direction.status is DirectionStatus.UNCERTAIN
    assert not result.extrapolated


def test_axis_ratio_field_matches_axes_and_marks_extrapolation() -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 2, 1),
        rows=np.array([0, 0]),
        columns=np.array([0, 1]),
        x_km=np.array([0.0, 10.0]),
        y_km=np.array([10.0, 0.0]),
        longitude=np.array([121.5, 121.5]),
        latitude=np.array([31.2, 31.2]),
        distance_km=np.array([10.0, 10.0]),
        azimuth_deg=np.array([0.0, 90.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    result = evaluate_model(
        snapshot,
        samples,
        DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
        PARAMETERS.model,
    )

    assert result.values[0] == pytest.approx(5.806964124806861, abs=1e-5)
    assert result.values[1] == pytest.approx(5.552603218791649, abs=1e-5)
    assert result.sigma[0] == pytest.approx(0.6310)
    assert result.sigma[1] == pytest.approx(0.6638)


@pytest.mark.parametrize("magnitude", [3.5, 6.5])
def test_model_marks_out_of_range_magnitude(magnitude: float) -> None:
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=magnitude,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([10.0]),
        y_km=np.array([0.0]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([10.0]),
        azimuth_deg=np.array([0.0]),
    )

    result = evaluate_model(
        snapshot,
        samples,
        DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
        PARAMETERS.model,
    )

    assert result.extrapolated is True


def test_fault_candidates_are_limited_to_20_km_boundary() -> None:
    decision = resolve_direction(
        override_deg=None,
        focal_mechanism_deg=None,
        finite_fault_deg=None,
        candidates=(
            FaultCandidate(fault_id="at-boundary", strike_deg=12.0, distance_km=20.0),
            FaultCandidate(fault_id="outside", strike_deg=25.0, distance_km=20.1),
        ),
    )

    assert decision.candidate_fault_id == "at-boundary"
    assert decision.candidate_distance_km == pytest.approx(20.0)
    assert decision.strike_deg == 12.0

    uncertain = resolve_direction(
        override_deg=None,
        focal_mechanism_deg=None,
        finite_fault_deg=None,
        candidates=(
            FaultCandidate(fault_id="outside", strike_deg=25.0, distance_km=20.1),
        ),
    )
    assert uncertain.status is DirectionStatus.UNCERTAIN


def test_near_epicenter_long_axis_matches_axis_formula() -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([0.0]),
        y_km=np.array([0.1]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([0.1]),
        azimuth_deg=np.array([0.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )

    result = evaluate_model(
        snapshot,
        samples,
        DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
        PARAMETERS.model,
    )
    expected = axis_intensity(
        5.0,
        np.array([0.1]),
        PARAMETERS.model.long_axis,
    )[0]

    assert result.values[0] == pytest.approx(expected, abs=1e-6)
    assert result.sigma[0] == pytest.approx(PARAMETERS.model.long_axis.sigma)


@pytest.mark.parametrize("bad_distance", [-0.1, np.nan, np.inf])
def test_resolved_branch_rejects_non_finite_or_negative_distances(
    bad_distance: float,
) -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([1.0]),
        y_km=np.array([0.0]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([bad_distance]),
        azimuth_deg=np.array([0.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )

    with pytest.raises(ValueError):
        evaluate_model(
            snapshot,
            samples,
            DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
            PARAMETERS.model,
        )


def test_non_convergent_field_raises() -> None:
    samples = GridSamples(
        definition=GridDefinition("g", "EPSG:32651", 1000, 0, 0, 1, 1),
        rows=np.array([0]),
        columns=np.array([0]),
        x_km=np.array([1.0]),
        y_km=np.array([0.0]),
        longitude=np.array([121.5]),
        latitude=np.array([31.2]),
        distance_km=np.array([1.0]),
        azimuth_deg=np.array([0.0]),
    )
    snapshot = IntensityEventSnapshot(
        event_id="e",
        revision_id="r",
        magnitude=5.0,
        longitude=121.5,
        latitude=31.2,
        report_ingested_at=datetime(2026, 9, 27, tzinfo=UTC),
    )
    broken = replace(
        PARAMETERS.model,
        solver_intensity_min=100.0,
        solver_intensity_max=101.0,
    )

    with pytest.raises(ModelFieldConvergenceError):
        evaluate_model(
            snapshot,
            samples,
            DirectionDecision(DirectionStatus.RESOLVED, "finite_fault", 0.0),
            broken,
        )
