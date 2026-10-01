from datetime import UTC, datetime, timedelta

import pytest


EXPECTED_COMPLETE_OUTPUTS = frozenset(
    {
        "map.intensity",
        "map.economic_loss",
        "map.rescue_demand",
        "map.deaths",
        "map.injuries",
        "map.buried",
        "map.material_demand",
        "map.gdp",
        "map.historical_earthquakes",
        "map.population",
        "map.hazard_sources",
        "map.schools",
        "map.hospitals",
        "map.active_faults",
        "map.building_damage",
        "map.key_targets",
        "map.epicenter",
        "map.city_distances",
        "doc.background",
        "doc.housing",
        "doc.economy",
        "doc.population",
        "doc.area_overview",
        "doc.historical_catalog",
        "doc.rapid_brief",
        "doc.rapid_report",
        "doc.decision_report",
        "deck.decision_report",
    }
)

EXPECTED_DEGRADED_REASONS = {
    "doc.key_targets": (
        "shanghai.shelter.emergency 数据缺失",
        "shanghai.rescue_team 数据缺失",
        "shanghai.cultural_relic 数据缺失",
    ),
    "doc.spatial_distances": ("shanghai.road.network 数据缺失",),
    "map.building_grid": ("模型分配，待复核",),
    "map.cultural_relics": ("文物数据待复核",),
    "map.metro": ("轨道交通数据待复核",),
    "map.pga_zoning": ("区划数据待复核",),
    "map.rescue_teams": ("救援队伍数据待复核",),
    "map.reservoirs": ("水库数据待复核",),
    "map.seismic_stations": ("台站数据待复核",),
    "map.shelter_emergency": ("疏散场地数据待复核",),
    "map.transport": ("路网数据待复核",),
}


def _assert_expected_quality(result) -> None:
    statuses = {
        artifact.artifact_key: artifact.status
        for artifact in result.artifacts
    }
    reasons = {
        artifact.artifact_key: artifact.degradation_reasons
        for artifact in result.artifacts
        if artifact.status == "degraded"
    }

    assert len(statuses) == 39
    assert {
        key for key, status in statuses.items() if status == "complete"
    } == EXPECTED_COMPLETE_OUTPUTS
    assert {
        key for key, status in statuses.items() if status == "degraded"
    } == set(EXPECTED_DEGRADED_REASONS)
    assert reasons == EXPECTED_DEGRADED_REASONS


@pytest.fixture(autouse=True)
async def _dispose_engine_between_performance_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.performance
async def test_test_event_completes_39_outputs_within_five_minutes(
    artifact_acceptance_environment,
) -> None:
    result = await artifact_acceptance_environment.run_test_event(
        magnitude=5.1,
        production_mode="test",
    )
    print(
        "first_test_event_elapsed_seconds="
        f"{result.elapsed_seconds:.3f} "
        f"complete={result.complete_count} "
        f"degraded={result.degraded_count} "
        "degraded_outputs="
        f"{[(item.artifact_key, item.degradation_reasons) for item in result.artifacts if item.status == 'degraded']}"
    )

    assert result.required_output_count == 39
    assert result.complete_count + result.degraded_count == 39
    assert result.failed_count == 0
    assert result.timeout_count == 0
    assert result.elapsed_seconds <= 300
    assert all("【测试】" in artifact.file_name for artifact in result.artifacts)
    assert len({artifact.context_fingerprint for artifact in result.artifacts}) == 1
    _assert_expected_quality(result)


@pytest.mark.performance
async def test_three_preheated_runs_take_worst_case(
    artifact_acceptance_environment,
) -> None:
    results = [
        await artifact_acceptance_environment.run_test_event(
            magnitude=5.1,
            production_mode="test",
        )
        for _ in range(3)
    ]
    for index, result in enumerate(results, start=1):
        print(
            f"preheated_run_{index}_elapsed_seconds="
            f"{result.elapsed_seconds:.3f} "
            f"complete={result.complete_count} "
            f"degraded={result.degraded_count}"
        )

    assert max(result.elapsed_seconds for result in results) <= 300
    assert all(
        result.complete_count + result.degraded_count == 39
        and result.failed_count == 0
        and result.timeout_count == 0
        for result in results
    )
    for result in results:
        _assert_expected_quality(result)


@pytest.mark.performance
async def test_formal_replay_uses_t1_as_deadline_basis(
    artifact_acceptance_environment,
) -> None:
    result = await artifact_acceptance_environment.run_replay_event(
        t1_at="2026-09-30T07:00:00+00:00",
        production_mode="replay",
    )
    print(
        "replay_elapsed_seconds="
        f"{result.elapsed_seconds:.3f} "
        f"complete={result.complete_count} "
        f"degraded={result.degraded_count}"
    )

    assert result.deadline_basis_at == "2026-09-30T07:00:00+00:00"
    assert result.deadline_at == "2026-09-30T07:05:00+00:00"
    assert all("【测试回放】" in artifact.file_name for artifact in result.artifacts)
    assert result.required_output_count == 39
    assert result.complete_count + result.degraded_count == 39
    assert result.failed_count == 0
    assert result.timeout_count == 0
    assert result.elapsed_seconds <= 300

    from sqlalchemy import select

    from app.artifacts.models import ProductionTask

    async with artifact_acceptance_environment._session_factory() as session:
        task_deadlines = set(
            (
                await session.scalars(
                    select(ProductionTask.deadline_at).where(
                        ProductionTask.production_run_id
                        == result.production_run_id
                    )
                )
            ).all()
        )
    assert task_deadlines == {
        datetime.fromisoformat("2026-09-30T07:05:00+00:00")
    }


@pytest.mark.performance
async def test_replay_fixture_creates_t1_semantics_before_workflow(
    artifact_acceptance_environment,
    monkeypatch,
) -> None:
    from sqlalchemy import select

    from app.artifacts.models import ProductionRun, ProductionTask
    from app.assessment.models import AssessmentRun, AssessmentTask
    from app.artifacts.router import _workflow_input

    target_t1 = datetime(2026, 9, 30, 7, 0, tzinfo=UTC)
    observed: dict[str, object] = {}

    async def fake_execute(run) -> object:
        async with artifact_acceptance_environment._session_factory() as session:
            stored = await session.get(ProductionRun, run.id)
            assert stored is not None
            assessment = await session.get(
                AssessmentRun,
                stored.assessment_run_id,
            )
            assert assessment is not None
            task_deadlines = (
                await session.scalars(
                    select(AssessmentTask.deadline_at).where(
                        AssessmentTask.run_id == assessment.id
                    )
                )
            ).all()
            production_task_deadlines = (
                await session.scalars(
                    select(ProductionTask.deadline_at).where(
                        ProductionTask.production_run_id == stored.id
                    )
                )
            ).all()
            request = _workflow_input(stored)
        observed["run_basis"] = stored.deadline_basis_at
        observed["run_deadline"] = stored.deadline_at
        observed["assessment_basis"] = assessment.deadline_basis_at
        observed["assessment_deadline"] = assessment.deadline_at
        observed["task_deadlines"] = set(task_deadlines)
        observed["production_task_deadlines"] = set(
            production_task_deadlines
        )
        observed["input_basis"] = request.deadline_basis_at
        observed["render_concurrency"] = request.render_concurrency
        from tests.artifact_helpers import ArtifactAcceptanceResult

        return ArtifactAcceptanceResult(
            required_output_count=1,
            complete_count=1,
            degraded_count=0,
            failed_count=0,
            timeout_count=0,
            elapsed_seconds=0,
            artifacts=(),
            production_run_id=str(stored.id),
            deadline_basis_at=stored.deadline_basis_at.isoformat(),
            deadline_at=stored.deadline_at.isoformat(),
        )

    monkeypatch.setattr(
        artifact_acceptance_environment,
        "_execute_run",
        fake_execute,
    )
    result = await artifact_acceptance_environment.run_replay_event(
        t1_at=target_t1.isoformat(),
        production_mode="replay",
    )

    assert result.deadline_basis_at == target_t1.isoformat()
    assert observed == {
        "run_basis": target_t1,
        "run_deadline": target_t1 + timedelta(seconds=300),
        "assessment_basis": target_t1,
        "assessment_deadline": target_t1 + timedelta(seconds=300),
        "task_deadlines": {
            target_t1 + timedelta(seconds=300)
        },
        "production_task_deadlines": {
            target_t1 + timedelta(seconds=300)
        },
        "input_basis": target_t1.isoformat(),
        "render_concurrency": 4,
    }
