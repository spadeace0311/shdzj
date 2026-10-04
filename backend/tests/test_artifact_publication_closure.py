import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.artifacts.models import (
    ArtifactPublication,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactProductionRepository,
    ArtifactQuality,
)
from app.artifacts.worker import ArtifactActivities
from app.artifacts.workflow import ArtifactPublicationInput
from app.db import engine


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _seed_publication_run(seeded_artifact_assessment, session_factory):
    repository = ArtifactProductionRepository()
    old = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=1,
    )
    run = await seeded_artifact_assessment.create_full_run()

    async with session_factory() as session:
        async with session.begin():
            stored_run = await session.get(ProductionRun, run.id)
            assert stored_run is not None
            task_rows = (
                await session.scalars(
                    select(ProductionTask)
                    .where(
                        ProductionTask.production_run_id == run.id,
                        ProductionTask.artifact_key.in_(
                            ("map.epicenter", "doc.rapid_report")
                        ),
                    )
                    .order_by(ProductionTask.artifact_key)
                )
            ).all()
            assert len(task_rows) == 2
            task_ids: dict[str, object] = {}
            completed_task_ids = {task.id for task in task_rows}
            for index, task in enumerate(task_rows):
                fingerprint = _checksum(f"{task.artifact_key}-{index}")
                await repository.freeze_task_fingerprint(
                    session,
                    task.id,
                    fingerprint,
                )
                await repository.start_task(
                    session,
                    task.id,
                    f"publication:{task.id}:{fingerprint}",
                )
                await repository.complete_task(
                    session,
                    task.id,
                    ArtifactGenerationResult(
                        file_name=f"{task.artifact_key}-new.jpg",
                        format="jpg",
                        storage_path=(
                            f"fixtures/publication/{task.artifact_key}.jpg"
                        ),
                        checksum=_checksum(
                            f"{task.artifact_key}-new"
                        ),
                        size_bytes=1024,
                        quality=ArtifactQuality(
                            grade="A",
                            needs_review=False,
                        ),
                        generated_at=stored_run.deadline_at
                        - timedelta(seconds=10),
                    ),
                    "succeeded",
                )
                task_ids[task.artifact_key] = task.id

            all_tasks = (
                await session.scalars(
                    select(ProductionTask).where(
                        ProductionTask.production_run_id == run.id
                    )
                )
            ).all()
            for task in all_tasks:
                if task.id in completed_task_ids:
                    continue
                await repository.mark_task_succeeded_for_test(
                    session,
                    task.id,
                    observed_at=stored_run.deadline_at
                    - timedelta(seconds=20),
                )

            current_conflict = ArtifactPublication(
                event_id=old.event_id,
                revision_id=old.revision_id,
                revision_no=stored_run.revision_no,
                production_mode=old.production_mode,
                artifact_key=old.artifact_key,
                output_profile=old.output_profile,
                artifact_id=old.id,
                production_run_id=old.production_run_id,
                generation_seq=stored_run.generation_seq,
                published_at=datetime.now(UTC),
            )
            session.add(current_conflict)
            await session.flush()

    return run.id, old.id, task_ids


async def test_publication_failure_prevents_completed_run_status(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    run_id, old_artifact_id, task_ids = await _seed_publication_run(
        seeded_artifact_assessment,
        session_factory,
    )
    async with session_factory() as session:
        run = await session.get(ProductionRun, run_id)
        assert run is not None
        observed_at = run.deadline_at - timedelta(seconds=1)

    activities = ArtifactActivities(session_factory)
    result = await activities.publish_artifact_production(
        ArtifactPublicationInput(
            production_run_id=str(run_id),
            observed_at=observed_at.isoformat(),
            published_by="publication-closure-test",
        )
    )

    assert result["status"] == "partial"
    assert result["publication_failed_count"] == 1
    async with session_factory() as session:
        stored_run = await session.get(ProductionRun, run_id)
        assert stored_run is not None
        tasks = (
            await session.scalars(
                select(ProductionTask).where(
                    ProductionTask.production_run_id == run_id,
                    ProductionTask.artifact_key.in_(
                        ("map.epicenter", "doc.rapid_report")
                    ),
                )
            )
        ).all()
        current = (
            await session.scalars(
                select(ArtifactPublication).where(
                    ArtifactPublication.event_id == stored_run.event_id,
                    ArtifactPublication.superseded_at.is_(None),
                )
            )
        ).all()

    task_by_key = {task.artifact_key: task for task in tasks}
    publication_by_key = {
        publication.artifact_key: publication for publication in current
    }
    assert stored_run.status == "partial"
    assert task_by_key["map.epicenter"].status == "failed"
    assert task_by_key["map.epicenter"].result["publication_status"] == "failed"
    assert task_by_key["doc.rapid_report"].status == "succeeded"
    assert publication_by_key["map.epicenter"].artifact_id == old_artifact_id
    assert publication_by_key["doc.rapid_report"].production_run_id == run_id


async def test_publication_failure_downgrades_preterminalized_run(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    run_id, old_artifact_id, _task_ids = await _seed_publication_run(
        seeded_artifact_assessment,
        session_factory,
    )
    repository = ArtifactProductionRepository()
    async with session_factory() as session:
        async with session.begin():
            run = await session.get(ProductionRun, run_id)
            assert run is not None
            observed_at = run.deadline_at - timedelta(seconds=1)
            finalized = await repository.finalize_run(
                session,
                run.id,
                observed_at,
            )
    assert finalized.status == "completed"

    activities = ArtifactActivities(session_factory)
    result = await activities.publish_artifact_production(
        ArtifactPublicationInput(
            production_run_id=str(run_id),
            observed_at=observed_at.isoformat(),
            published_by="publication-closure-test",
        )
    )

    async with session_factory() as session:
        stored_run = await session.get(ProductionRun, run_id)
        task = await session.scalar(
            select(ProductionTask).where(
                ProductionTask.production_run_id == run_id,
                ProductionTask.artifact_key == "map.epicenter",
            )
        )
        current = await session.scalar(
            select(ArtifactPublication).where(
                ArtifactPublication.event_id == stored_run.event_id,
                ArtifactPublication.artifact_key == "map.epicenter",
                ArtifactPublication.superseded_at.is_(None),
            )
        )

    assert result["status"] == "partial"
    assert stored_run is not None
    assert stored_run.status == "partial"
    assert task is not None
    assert task.status == "failed"
    assert current is not None
    assert current.artifact_id == old_artifact_id
