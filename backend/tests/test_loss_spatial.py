import pytest

from app.loss.spatial import (
    LossGridCell,
    allocate_continuous,
    allocate_integers,
)


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
