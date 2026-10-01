from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.artifacts import worker as artifact_worker
from app.artifacts.models import ArtifactPublication
from app.artifacts.retention import ArtifactRetentionResult


@pytest.fixture(autouse=True)
async def _dispose_engine_between_retention_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_retention_removes_expired_test_and_drill_but_not_live(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    test_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    drill_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="drill",
        age_days=731,
    )
    live_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="live",
        age_days=10_000,
    )

    removed = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
    )

    assert removed.test_deleted == 1
    assert removed.drill_deleted == 1
    assert removed.live_deleted == 0
    assert await session.get(type(test_artifact), test_artifact.id) is None
    assert await session.get(type(drill_artifact), drill_artifact.id) is None
    assert await session.get(type(live_artifact), live_artifact.id) is not None


async def _publish_artifact(
    seeded_artifact_assessment,
    session,
    artifact,
    *,
    superseded: bool,
) -> ArtifactPublication:
    from app.artifacts.models import ProductionRun

    run = await session.get(ProductionRun, artifact.production_run_id)
    assert run is not None
    publication = ArtifactPublication(
        event_id=artifact.event_id,
        revision_id=artifact.revision_id,
        revision_no=run.revision_no,
        production_mode=artifact.production_mode,
        artifact_key=artifact.artifact_key,
        output_profile=artifact.output_profile,
        artifact_id=artifact.id,
        production_run_id=run.id,
        generation_seq=run.generation_seq,
        published_at=artifact.generated_at,
        superseded_at=(
            seeded_artifact_assessment.now if superseded else None
        ),
    )
    session.add(publication)
    await session.flush()
    return publication


async def test_retention_protects_current_and_superseded_publications(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    current = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    superseded = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=402,
    )
    current_publication = await _publish_artifact(
        seeded_artifact_assessment,
        session,
        current,
        superseded=False,
    )
    superseded_publication = await _publish_artifact(
        seeded_artifact_assessment,
        session,
        superseded,
        superseded=True,
    )

    result = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
    )

    assert result.test_deleted == 0
    assert result.protected_publication_count == 2
    assert dict(result.protected_publication_reasons) == {
        "current_publication": 1,
        "superseded_publication": 1,
    }
    assert await session.get(type(current), current.id) is not None
    assert await session.get(type(superseded), superseded.id) is not None
    assert await session.get(
        ArtifactPublication,
        current_publication.id,
    ) is not None
    assert await session.get(
        ArtifactPublication,
        superseded_publication.id,
    ) is not None


async def test_retention_retries_after_object_delete_failure(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    store = FlakyStore()
    service = artifact_retention_service.__class__(store=store)

    first = await service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
    )

    assert first.test_deleted == 0
    assert first.failed_deletions == 1
    assert await session.get(type(artifact), artifact.id) is not None

    second = await service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
    )

    assert second.test_deleted == 1
    assert second.failed_deletions == 0
    assert await session.get(type(artifact), artifact.id) is None


class FlakyStore:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, relative_path: str):
        del relative_path
        return object()

    def delete_unreferenced(self, artifact) -> None:
        del artifact
        self.calls += 1
        if self.calls == 1:
            raise OSError("injected object deletion failure")


async def test_retention_loop_logs_all_modes_and_protected_counts(
    monkeypatch,
    caplog,
) -> None:
    result = ArtifactRetentionResult(
        test_deleted=1,
        drill_deleted=2,
        live_deleted=3,
        manual_deleted=4,
        replay_deleted=5,
        failed_deletions=6,
        protected_publication_count=7,
        protected_publication_reasons=(
            ("current_publication", 2),
            ("superseded_publication", 5),
        ),
    )
    sleep_count = 0

    class Service:
        async def retain_expired(self, session, *, observed_at):
            del session, observed_at
            return result

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

        def begin(self):
            return self

    async def sleep(_seconds: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(
        artifact_worker,
        "ArtifactRetentionService",
        lambda: Service(),
    )
    monkeypatch.setattr(artifact_worker.asyncio, "sleep", sleep)

    with caplog.at_level(logging.INFO, logger=artifact_worker.__name__):
        with pytest.raises(asyncio.CancelledError):
            await artifact_worker._run_retention_loop(
                session_factory=Session,
                configured=SimpleNamespace(
                    artifact_retention_enabled=True,
                    artifact_retention_interval_seconds=60,
                ),
            )

    message = caplog.records[-1].getMessage()
    assert '"test":1' in message
    assert '"drill":2' in message
    assert '"live":3' in message
    assert '"manual":4' in message
    assert '"replay":5' in message
    assert '"failed":6' in message
    assert '"protected_publications":7' in message
    assert '"current_publication":2' in message
    assert '"superseded_publication":5' in message
