import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.artifacts.models import (
    ArtifactPublication,
    ArtifactTaskDependencyBinding,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.domain import DependencyKind, DependencySpec
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactQuality,
    CreateProductionRunCommand,
    ProductionDependencyBinding,
)
from app.artifacts.service import ArtifactProductionService
from app.db import engine


@pytest.fixture(autouse=True)
async def dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_full_run_creates_exactly_39_tasks(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    tasks = await artifact_repository.list_tasks(session, run.id)

    assert len(tasks) == 39
    assert len({(task.artifact_key, task.output_profile) for task in tasks}) == 39
    assert all(task.status == "pending" for task in tasks)


async def test_single_artifact_rebuild_does_not_replace_full_progress(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    full = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    rebuild = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.rebuild_command("map.epicenter"),
    )

    assert full.id != rebuild.id
    assert rebuild.generation_scope == "artifact:map.epicenter:a3v-professional"
    assert len(await artifact_repository.list_tasks(session, rebuild.id)) == 1
    assert await artifact_repository.get_current_full_run(
        session,
        event_id=full.event_id,
        revision_id=full.revision_id,
    ) == full


async def test_same_scope_replacement_cancels_old_run_and_tasks(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    old = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    replacement = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(
            generation_seq=2,
            snapshot={"fixture": "same-scope-replacement"},
            reuse_existing=False,
        ),
    )
    old = await session.get(ProductionRun, old.id)
    old_tasks = await artifact_repository.list_tasks(session, old.id)

    assert replacement.id != old.id
    assert old is not None
    assert old.status == "canceled"
    assert old.cancel_reason == "same_scope_replacement"
    assert old.is_current is False
    assert old.superseded_by_run_id == replacement.id
    assert old_tasks
    assert all(task.status == "canceled" for task in old_tasks)


async def test_finalize_run_partial_keeps_successful_artifacts(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    tasks = await artifact_repository.list_tasks(session, run.id)
    await artifact_repository.mark_task_succeeded_for_test(session, tasks[0].id)
    await artifact_repository.mark_task_failed_for_test(
        session,
        tasks[1].id,
        observed_at=run.deadline_at + timedelta(seconds=1),
    )

    finalized = await artifact_repository.finalize_run(
        session,
        run.id,
        observed_at=run.deadline_at + timedelta(seconds=1),
    )

    assert finalized.status == "partial"
    assert finalized.deadline_exceeded_at is not None
    assert finalized.last_artifact_committed_at is not None


async def test_finalize_after_deadline_with_early_artifacts_is_completed(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    tasks = await artifact_repository.list_tasks(session, run.id)
    committed_at = run.deadline_at - timedelta(seconds=1)
    for task in tasks:
        await artifact_repository.mark_task_succeeded_for_test(
            session,
            task.id,
            observed_at=committed_at,
        )

    finalized = await artifact_repository.finalize_run(
        session,
        run.id,
        observed_at=run.deadline_at + timedelta(seconds=1),
    )

    assert finalized.status == "completed"
    assert finalized.deadline_exceeded_at is None


async def test_publish_artifact_atomically_supersedes_previous(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    first = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )
    second = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=2,
    )
    assert second.publication_mode == "rebuild"
    old_publication = await artifact_repository.get_publication(
        session,
        event_id=first.event_id,
        artifact_key=first.artifact_key,
        output_profile=first.output_profile,
        production_mode=first.production_mode,
    )

    publication = await artifact_repository.publish_artifact(
        session,
        second.id,
        published_by="superadmin",
        forced=False,
    )
    previous = await artifact_repository.get_publication(
        session,
        event_id=first.event_id,
        artifact_key=first.artifact_key,
        output_profile=first.output_profile,
        production_mode=first.production_mode,
    )

    assert publication.artifact_id == second.id
    assert publication.is_forced is False
    assert old_publication is not None
    await session.refresh(old_publication)
    assert old_publication.superseded_at is not None
    assert previous is not None
    assert previous.artifact_id == second.id


async def test_correction_supersedes_old_revision_and_requests_cancel(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    old = await seeded_artifact_assessment.create_full_run(revision_no=1)
    correction = await seeded_artifact_assessment.create_correction_revision(
        revision_no=2
    )

    canceled_ids = await artifact_repository.supersede_runs_for_revision(
        session,
        event_id=old.event_id,
        new_revision_id=correction.id,
        new_revision_no=2,
    )
    old = await session.get(ProductionRun, old.id)
    assert old is not None

    assert old.id in canceled_ids
    assert old.is_current is False
    assert old.superseded_at is not None
    assert old.cancel_reason == "revision_superseded"


async def test_correction_cancels_unfinished_tasks_and_supersedes_publication(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    artifact = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )
    old_run = await session.get(ProductionRun, artifact.production_run_id)
    assert old_run is not None
    publication = await artifact_repository.get_publication(
        session,
        event_id=artifact.event_id,
        artifact_key=artifact.artifact_key,
        output_profile=artifact.output_profile,
        production_mode=artifact.production_mode,
    )
    assert publication is not None
    correction = await seeded_artifact_assessment.create_correction_revision(
        revision_no=2
    )

    await artifact_repository.supersede_runs_for_revision(
        session,
        event_id=old_run.event_id,
        new_revision_id=correction.id,
        new_revision_no=2,
    )

    tasks = await artifact_repository.list_tasks(session, old_run.id)
    await session.refresh(publication)
    assert {task.status for task in tasks} == {"succeeded", "canceled"}
    assert next(
        task for task in tasks if task.artifact_key == "map.epicenter"
    ).status == "succeeded"
    assert publication.superseded_at is not None


async def test_real_event_cancels_test_and_drill_runs(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    test_run = await seeded_artifact_assessment.create_full_run(
        production_mode="test"
    )
    drill_run = await seeded_artifact_assessment.create_full_run(
        production_mode="drill"
    )

    canceled_ids = await artifact_repository.cancel_non_live_runs_for_real_event(
        session,
        event_id=test_run.event_id,
    )

    assert set(canceled_ids) == {test_run.id, drill_run.id}
    assert (
        await session.get(type(test_run), test_run.id)
    ).cancel_reason == "real_event_priority"
    assert (
        await session.get(type(drill_run), drill_run.id)
    ).cancel_reason == "real_event_priority"


async def test_start_and_complete_task_are_idempotent(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.epicenter"
    )
    fingerprint = "f" * 64
    key = f"artifact:{run.id}:map.epicenter:a3v-professional:{fingerprint}"
    result = ArtifactGenerationResult(
        file_name="epicenter.jpg",
        format="jpg",
        storage_path="objects/aa/epicenter.jpg",
        checksum="a" * 64,
        size_bytes=100,
        quality=ArtifactQuality(grade="A", needs_review=False),
        generated_at=run.deadline_at - timedelta(seconds=10),
    )

    await artifact_repository.freeze_task_fingerprint(session, task.id, fingerprint)
    first_start = await artifact_repository.start_task(session, task.id, key)
    second_start = await artifact_repository.start_task(session, task.id, key)
    first_artifact = await artifact_repository.complete_task(
        session,
        task.id,
        result,
        "succeeded",
    )
    second_artifact = await artifact_repository.complete_task(
        session,
        task.id,
        result,
        "succeeded",
    )
    artifact_count = await session.scalar(
        select(func.count())
        .select_from(GeneratedArtifact)
        .where(GeneratedArtifact.production_task_id == task.id)
    )

    assert first_start.id == second_start.id
    assert first_start.attempt_count == 1
    assert first_artifact.id == second_artifact.id
    assert artifact_count == 1


async def test_complete_task_rejects_after_deadline(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.epicenter"
    )
    await artifact_repository.freeze_task_fingerprint(
        session,
        task.id,
        "f" * 64,
    )
    await artifact_repository.start_task(session, task.id, "late-result")

    with pytest.raises(ValueError, match="deadline"):
        await artifact_repository.complete_task(
            session,
            task.id,
            ArtifactGenerationResult(
                file_name="late.jpg",
                format="jpg",
                storage_path="objects/late.jpg",
                checksum="b" * 64,
                size_bytes=100,
                quality=ArtifactQuality(grade="A"),
                generated_at=run.deadline_at + timedelta(seconds=1),
            ),
            "succeeded",
        )


@pytest.mark.parametrize("run_status", ["completed", "partial"])
async def test_complete_task_rejects_terminal_run(
    seeded_artifact_assessment,
    artifact_repository,
    session,
    run_status,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.epicenter"
    )
    await artifact_repository.freeze_task_fingerprint(
        session,
        task.id,
        "f" * 64,
    )
    await artifact_repository.start_task(session, task.id, "terminal-result")
    run.status = run_status
    await session.flush()

    with pytest.raises(ValueError, match="terminal production run"):
        await artifact_repository.complete_task(
            session,
            task.id,
            ArtifactGenerationResult(
                file_name="terminal.jpg",
                format="jpg",
                storage_path="objects/terminal.jpg",
                checksum="c" * 64,
                size_bytes=100,
                quality=ArtifactQuality(grade="A"),
                generated_at=run.deadline_at - timedelta(seconds=1),
            ),
            "succeeded",
        )


async def test_prepare_dependencies_binds_real_assessment_product(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    product = await seeded_artifact_assessment.create_fusion_product()
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.intensity"
    )

    prepared = await artifact_repository.prepare_dependencies(session, run.id)
    bindings = (
        await session.scalars(
            select(ArtifactTaskDependencyBinding).where(
                ArtifactTaskDependencyBinding.production_task_id == task.id
            )
        )
    ).all()

    assert task.id in {item.id for item in prepared}
    assert len(bindings) == 1
    assert bindings[0].bound_entity_id == product.id
    assert bindings[0].bound_version == product.algorithm_version
    assert bindings[0].bound_checksum == product.output_checksum
    assert bindings[0].resolution_status == "bound"


async def test_prepare_dependencies_omits_optional_artifacts_after_wait_cutoff(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "doc.rapid_report"
    )
    task.optional_dependency_wait_cutoff_at = datetime.now(UTC) - timedelta(
        seconds=1
    )
    await session.flush()

    await artifact_repository.prepare_dependencies(session, run.id)
    bindings = (
        await session.scalars(
            select(ArtifactTaskDependencyBinding).where(
                ArtifactTaskDependencyBinding.production_task_id == task.id,
                ArtifactTaskDependencyBinding.is_optional.is_(True),
            )
        )
    ).all()

    assert len(bindings) == len(task.optional_depends_on)
    assert {binding.resolution_status for binding in bindings} == {
        "omitted_after_wait"
    }


async def test_optional_dependencies_block_ready_and_fingerprint_until_cutoff(
    seeded_artifact_assessment,
    artifact_repository,
    session_factory,
) -> None:
    service = ArtifactProductionService(
        session_factory=session_factory,
        repository=artifact_repository,
    )
    optional_dependency = DependencySpec(
        kind=DependencyKind.ARTIFACT,
        key="map.epicenter",
        output_profile="a3v-professional",
    )
    async with session_factory() as session:
        async with session.begin():
            run = await artifact_repository.create_run(
                session,
                seeded_artifact_assessment.full_run_command(),
            )
            task = next(
                item
                for item in await artifact_repository.list_tasks(session, run.id)
                if item.artifact_key == "doc.rapid_report"
            )
            task.depends_on = []
            task.optional_depends_on = [optional_dependency.to_dict()]
            task.optional_dependency_wait_cutoff_at = datetime.now(UTC) + timedelta(
                seconds=60
            )
            await artifact_repository.create_input_snapshot(
                session,
                run.id,
                context_fingerprint="a" * 64,
                region_id="shanghai",
                manifest={"fixture": "optional-cutoff"},
            )
            task_id = task.id

    async with session_factory() as session:
        async with session.begin():
            await artifact_repository.prepare_dependencies(session, run.id)
            task = await session.get(ProductionTask, task_id)
            assert task is not None
            assert task.status == "pending"
            assert task.input_fingerprint is None
            binding_count = await session.scalar(
                select(func.count())
                .select_from(ArtifactTaskDependencyBinding)
                .where(
                    ArtifactTaskDependencyBinding.production_task_id == task.id
                )
            )
            assert binding_count == 0

    with pytest.raises(ValueError, match="not ready"):
        await service.prepare_task(task_id)

    async with session_factory() as session:
        async with session.begin():
            task = await session.get(ProductionTask, task_id)
            assert task is not None
            task.optional_dependency_wait_cutoff_at = datetime.now(UTC) - timedelta(
                seconds=1
            )

    async with session_factory() as session:
        async with session.begin():
            await artifact_repository.prepare_dependencies(session, run.id)
            task = await session.get(ProductionTask, task_id)
            assert task is not None
            assert task.status == "ready"
            bindings = (
                await session.scalars(
                    select(ArtifactTaskDependencyBinding).where(
                        ArtifactTaskDependencyBinding.production_task_id
                        == task.id
                    )
                )
            ).all()
            assert [binding.resolution_status for binding in bindings] == [
                "omitted_after_wait"
            ]

    render_input = await service.prepare_task(task_id)
    assert len(render_input.input_fingerprint) == 64


async def test_replay_optional_dependency_uses_effective_run_clock(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    basis = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    deadline = basis + timedelta(seconds=300)
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(
            production_mode="replay",
            launch_mode="standalone",
            deadline_basis_at=basis,
            deadline_at=deadline,
        ),
    )
    run.status = "running"
    run.started_at = datetime.now(UTC)
    await session.flush()
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "doc.rapid_report"
    )
    assert task.optional_dependency_wait_cutoff_at is not None

    await artifact_repository.prepare_dependencies(session, run.id)
    bindings = (
        await session.scalars(
            select(ArtifactTaskDependencyBinding).where(
                ArtifactTaskDependencyBinding.production_task_id == task.id,
                ArtifactTaskDependencyBinding.is_optional.is_(True),
            )
        )
    ).all()

    assert bindings == []
    assert task.status == "pending"

    run.started_at = datetime.now(UTC) - timedelta(seconds=301)
    await session.flush()
    await artifact_repository.prepare_dependencies(session, run.id)
    bindings = (
        await session.scalars(
            select(ArtifactTaskDependencyBinding).where(
                ArtifactTaskDependencyBinding.production_task_id == task.id,
                ArtifactTaskDependencyBinding.is_optional.is_(True),
            )
        )
    ).all()

    assert bindings
    assert {binding.resolution_status for binding in bindings} == {
        "omitted_after_wait"
    }


async def test_replay_failure_before_logical_deadline_is_not_deadline_exceeded(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    basis = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    deadline = basis + timedelta(seconds=300)
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(
            production_mode="replay",
            launch_mode="standalone",
            deadline_basis_at=basis,
            deadline_at=deadline,
        ),
    )
    run.status = "running"
    run.started_at = datetime.now(UTC)
    await session.flush()
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.epicenter"
    )
    await artifact_repository.freeze_task_fingerprint(
        session,
        task.id,
        "a" * 64,
    )
    await artifact_repository.start_task(
        session,
        task.id,
        f"replay-failure:{task.id}:{'a' * 64}",
    )

    failed = await artifact_repository.fail_task(
        session,
        task.id,
        "render_failed",
        "logical replay failure",
    )

    assert failed.deadline_exceeded_at is None
    assert failed.completed_at is not None
    assert failed.completed_at < deadline


async def test_replay_cancel_audit_uses_effective_run_clock(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    basis = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    deadline = basis + timedelta(seconds=300)
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(
            production_mode="replay",
            launch_mode="standalone",
            deadline_basis_at=basis,
            deadline_at=deadline,
        ),
    )
    run.status = "running"
    run.started_at = datetime.now(UTC)
    await session.flush()
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.epicenter"
    )
    await artifact_repository.freeze_task_fingerprint(
        session,
        task.id,
        "b" * 64,
    )
    await artifact_repository.start_task(
        session,
        task.id,
        f"replay-cancel:{task.id}:{'b' * 64}",
    )

    canceled = await artifact_repository.cancel_task(
        session,
        task.id,
        "replay cancellation",
    )

    assert canceled.completed_at is not None
    assert canceled.completed_at < deadline


async def test_bind_dependency_rejects_wrong_resolution_shape(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.intensity"
    )

    with pytest.raises(ValueError, match="bound"):
        await artifact_repository.bind_dependency(
            session,
            task.id,
            ProductionDependencyBinding(
                dependency_kind="assessment_product",
                dependency_key="intensity.fusion",
                dependency_output_profile=None,
                is_optional=False,
                bound_entity_id=None,
                bound_version=None,
                bound_checksum=None,
                resolution_status="bound",
                resolution_detail={"source": "invalid"},
                resolved_at=datetime.now(UTC),
            ),
        )


async def test_create_input_snapshot_is_linked_and_immutable(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )

    snapshot = await artifact_repository.create_input_snapshot(
        session,
        run.id,
        context_fingerprint="c" * 64,
        region_id="shanghai",
        manifest={"catalog_version": run.catalog_version},
    )
    await session.refresh(run)

    assert run.input_snapshot_id == snapshot.id
    assert snapshot.production_run_id == run.id
    assert await session.scalar(
        select(func.count())
        .select_from(ProductionInputSnapshot)
        .where(ProductionInputSnapshot.production_run_id == run.id)
    ) == 1


async def test_concurrent_publish_leaves_one_current_publication(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    first = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=1,
    )
    second = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=2,
    )

    async def publish(artifact_id):
        from app.artifacts.repository import ArtifactProductionRepository

        async with session_factory() as publish_session:
            async with publish_session.begin():
                return await ArtifactProductionRepository().publish_artifact(
                    publish_session,
                    artifact_id,
                    published_by="concurrency-test",
                    forced=True,
                )

    await asyncio.gather(publish(first.id), publish(second.id))

    async with session_factory() as check_session:
        current = (
            await check_session.scalars(
                select(ArtifactPublication).where(
                    ArtifactPublication.event_id == first.event_id,
                    ArtifactPublication.artifact_key == first.artifact_key,
                    ArtifactPublication.output_profile == first.output_profile,
                    ArtifactPublication.production_mode == first.production_mode,
                    ArtifactPublication.superseded_at.is_(None),
                )
            )
        ).all()
        all_publications = (
            await check_session.scalars(
                select(ArtifactPublication).where(
                    ArtifactPublication.event_id == first.event_id,
                    ArtifactPublication.artifact_key == first.artifact_key,
                    ArtifactPublication.output_profile == first.output_profile,
                    ArtifactPublication.production_mode == first.production_mode,
                )
            )
        ).all()

    assert len(current) == 1
    assert len(all_publications) == 2
    assert current[0].artifact_id in {first.id, second.id}


async def test_failed_publish_does_not_supersede_old_publication(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    published = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )
    invalid = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=2,
    )
    invalid_run = await session.get(
        ProductionRun,
        invalid.production_run_id,
        with_for_update=True,
    )
    assert invalid_run is not None
    invalid_run.is_current = False
    await session.flush()

    current_before = await artifact_repository.get_publication(
        session,
        event_id=published.event_id,
        artifact_key=published.artifact_key,
        output_profile=published.output_profile,
        production_mode=published.production_mode,
    )
    assert current_before is not None
    assert current_before.artifact_id == published.id

    with pytest.raises(ValueError):
        await artifact_repository.publish_artifact(
            session,
            invalid.id,
            published_by="invalid",
            forced=False,
        )

    current_after = await artifact_repository.get_publication(
        session,
        event_id=published.event_id,
        artifact_key=published.artifact_key,
        output_profile=published.output_profile,
        production_mode=published.production_mode,
    )
    assert current_after is not None
    assert current_after.artifact_id == published.id
    assert current_after.superseded_at is None


async def test_publish_database_failure_rolls_back_old_publication(
    seeded_artifact_assessment,
    artifact_repository,
    session_factory,
) -> None:
    published = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=1,
    )
    replacement = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=2,
    )
    repository = artifact_repository

    with pytest.raises(DBAPIError):
        async with session_factory() as failing_session:
            async with failing_session.begin():
                await repository.publish_artifact(
                    failing_session,
                    replacement.id,
                    published_by="x" * 65,
                    forced=False,
                )

    async with session_factory() as check_session:
        current = await repository.get_publication(
            check_session,
            event_id=published.event_id,
            artifact_key=published.artifact_key,
            output_profile=published.output_profile,
            production_mode=published.production_mode,
        )
    assert current is not None
    assert current.artifact_id == published.id


async def test_finalize_before_deadline_rejects_unfinished_tasks(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    tasks = await artifact_repository.list_tasks(session, run.id)
    await artifact_repository.mark_task_succeeded_for_test(
        session,
        tasks[0].id,
        observed_at=run.deadline_at - timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="unfinished"):
        await artifact_repository.finalize_run(
            session,
            run.id,
            observed_at=run.deadline_at - timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "run_status",
    ["completed", "partial", "failed", "canceled"],
)
async def test_fail_task_rejects_terminal_run(
    seeded_artifact_assessment,
    artifact_repository,
    session,
    run_status,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.epicenter"
    )
    run.status = run_status
    await session.flush()

    with pytest.raises(ValueError, match="terminal production run"):
        await artifact_repository.fail_task(
            session,
            task.id,
            "render_failed",
            "renderer exited",
        )


@pytest.mark.parametrize("task_status", ["timed_out", "canceled"])
async def test_fail_task_rejects_terminal_task(
    seeded_artifact_assessment,
    artifact_repository,
    session,
    task_status,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    task = next(
        item
        for item in await artifact_repository.list_tasks(session, run.id)
        if item.artifact_key == "map.epicenter"
    )
    task.status = task_status
    await session.flush()

    with pytest.raises(ValueError, match="terminal task"):
        await artifact_repository.fail_task(
            session,
            task.id,
            "render_failed",
            "renderer exited",
        )


async def test_timeout_run_does_not_mark_deadline_without_timeouts(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    tasks = await artifact_repository.list_tasks(session, run.id)
    committed_at = run.deadline_at - timedelta(seconds=1)
    for task in tasks:
        await artifact_repository.mark_task_succeeded_for_test(
            session,
            task.id,
            observed_at=committed_at,
        )

    finalized = await artifact_repository.timeout_run(
        session,
        run.id,
        observed_at=run.deadline_at + timedelta(seconds=1),
    )

    assert finalized.status == "completed"
    assert finalized.deadline_exceeded_at is None


async def test_service_initial_run_preserves_assessment_deadlines(
    seeded_artifact_assessment,
    artifact_repository,
    session_factory,
) -> None:
    service = ArtifactProductionService(
        session_factory=session_factory,
        repository=artifact_repository,
    )

    prepared = await service.create_initial_run(
        assessment_run_id=seeded_artifact_assessment.assessment_run_id,
        event_id=seeded_artifact_assessment.event_id,
        revision_id=seeded_artifact_assessment.revision_id,
    )

    async with session_factory() as session:
        run = await session.get(ProductionRun, prepared.production_run_id)
        assert run is not None
        assert run.deadline_basis_at == seeded_artifact_assessment.deadline_basis_at
        assert run.deadline_at == seeded_artifact_assessment.deadline_at
        assert prepared.task_count == 39


async def test_service_rebuild_uses_five_minute_rebuild_deadline(
    seeded_artifact_assessment,
    artifact_repository,
    session_factory,
) -> None:
    service = ArtifactProductionService(
        session_factory=session_factory,
        repository=artifact_repository,
    )
    before = datetime.now(UTC)

    prepared = await service.create_rebuild_run(
        event_id=seeded_artifact_assessment.event_id,
        revision_id=seeded_artifact_assessment.revision_id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        requested_by="superadmin",
    )

    async with session_factory() as session:
        run = await session.get(ProductionRun, prepared.production_run_id)
        assert run is not None
        assert run.deadline_kind == "rebuild_deadline"
        assert timedelta(seconds=299) <= run.deadline_at - run.deadline_basis_at
        assert run.deadline_at - run.deadline_basis_at <= timedelta(seconds=301)
        assert run.deadline_basis_at >= before
        assert prepared.task_count == 1


async def test_service_prepare_task_freezes_fingerprint_and_commits_result(
    seeded_artifact_assessment,
    artifact_repository,
    session_factory,
) -> None:
    service = ArtifactProductionService(
        session_factory=session_factory,
        repository=artifact_repository,
    )
    prepared_run = await service.create_initial_run(
        assessment_run_id=seeded_artifact_assessment.assessment_run_id,
        event_id=seeded_artifact_assessment.event_id,
        revision_id=seeded_artifact_assessment.revision_id,
    )
    async with session_factory() as session:
        async with session.begin():
            run = await session.get(ProductionRun, prepared_run.production_run_id)
            assert run is not None
            task = await session.scalar(
                select(ProductionTask).where(
                    ProductionTask.production_run_id == run.id,
                    ProductionTask.artifact_key == "map.epicenter",
                )
            )
            assert task is not None
            await artifact_repository.create_input_snapshot(
                session,
                run.id,
                context_fingerprint="d" * 64,
                region_id="shanghai",
                manifest={"fixture": "artifact-service"},
            )

    render_input = await service.prepare_task(task.id)
    repeated = await service.prepare_task(task.id)
    async with session_factory() as session:
        async with session.begin():
            await artifact_repository.start_task(
                session,
                task.id,
                (
                    f"artifact:{run.id}:map.epicenter:a3v-professional:"
                    f"{render_input.input_fingerprint}"
                ),
            )

    artifact = await service.commit_task_result(
        task.id,
        ArtifactGenerationResult(
            file_name="service-epicenter.jpg",
            format="jpg",
            storage_path="objects/service-epicenter.jpg",
            checksum="e" * 64,
            size_bytes=200,
            quality=ArtifactQuality(grade="A"),
            generated_at=run.deadline_at - timedelta(seconds=5),
        ),
    )

    assert render_input.input_fingerprint == repeated.input_fingerprint
    assert len(render_input.input_fingerprint) == 64
    assert artifact.production_task_id == task.id


async def test_override_run_is_completed_and_marked_as_override(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    now = datetime.now(UTC)
    command = CreateProductionRunCommand(
        assessment_run_id=seeded_artifact_assessment.assessment_run_id,
        event_id=seeded_artifact_assessment.event_id,
        revision_id=seeded_artifact_assessment.revision_id,
        revision_no=1,
        production_mode="live",
        launch_mode="standalone",
        deadline_basis_at=now,
        deadline_at=now + timedelta(seconds=300),
        deadline_kind="rebuild_deadline",
        catalog_version=seeded_artifact_assessment.catalog.catalog_version,
        generation_scope="artifact:map.epicenter:a3v-professional",
        required_outputs=(("map.epicenter", "a3v-professional"),),
        artifact_result=ArtifactGenerationResult(
            file_name="override-epicenter.jpg",
            format="jpg",
            storage_path="objects/override-epicenter.jpg",
            checksum="f" * 64,
            size_bytes=300,
            quality=ArtifactQuality(grade="A"),
            generated_at=now,
            publication_mode="superadmin_override",
        ),
        published_by="superadmin",
        forced=True,
    )

    run, task, artifact, publication = (
        await artifact_repository.create_override_run(session, command)
    )

    assert run.status == "completed"
    assert task.kind == "superadmin_override"
    assert artifact.publication_mode == "superadmin_override"
    assert publication.is_forced is True
