import pytest


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
        f"degraded={result.degraded_count}"
    )

    assert result.required_output_count == 39
    assert result.complete_count + result.degraded_count == 39
    assert result.failed_count == 0
    assert result.timeout_count == 0
    assert result.elapsed_seconds <= 300
    assert all("【测试】" in artifact.file_name for artifact in result.artifacts)
    assert len({artifact.context_fingerprint for artifact in result.artifacts}) == 1


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
