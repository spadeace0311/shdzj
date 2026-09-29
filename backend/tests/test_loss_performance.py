import pytest

from tests.loss_helpers import execute_fixed_loss_chain


@pytest.mark.performance
async def test_fixed_shanghai_loss_chain_meets_five_minute_budget(
    session_factory,
) -> None:
    result = await execute_fixed_loss_chain(session_factory)
    assert result.status == "completed"
    assert result.query_ready_at > result.report_ingested_at
    assert result.elapsed_seconds <= 300.0
    assert result.stage_seconds["snapshot_and_exposure"] <= 20.0
    assert result.stage_seconds["buildings_and_population"] <= 60.0
    assert result.stage_seconds["casualties_economic_resources"] <= 60.0
    assert result.stage_seconds["validate_and_persist"] <= 20.0
