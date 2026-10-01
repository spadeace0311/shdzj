from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.artifacts import worker as artifact_worker
from app.artifacts.worker import ArtifactActivities
from app.artifacts.workflow import ArtifactDependencyWaitInput


class BlockingReadySession:
    def __init__(self, expected_entries: int) -> None:
        self.expected_entries = expected_entries
        self.entered = 0
        self.release = asyncio.Event()
        self.run_id = uuid4()
        self.task_id = uuid4()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def begin(self):
        return self

    async def scalar(self, statement):
        del statement
        self.entered += 1
        if self.entered == self.expected_entries:
            self.release.set()
        await self.release.wait()
        return SimpleNamespace(
            production_run_id=self.run_id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            status="ready",
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            id=self.task_id,
            input_fingerprint=None,
        )

    async def get(self, model, value):
        del model, value
        return SimpleNamespace(
            id=self.run_id,
            context_fingerprint="a" * 64,
        )

    async def refresh(self, value) -> None:
        del value


class ReadyRepository:
    async def prepare_dependencies(self, session, production_run_id) -> None:
        del session, production_run_id

    async def start_task(
        self,
        session,
        production_task_id,
        activity_idempotency_key,
    ) -> None:
        del session, activity_idempotency_key
        return SimpleNamespace(
            id=production_task_id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            input_fingerprint="f" * 64,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
        )


class ReadyService:
    async def prepare_task(self, production_task_id):
        del production_task_id
        return SimpleNamespace(
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            input_fingerprint="f" * 64,
        )


async def test_dependency_waits_are_not_gated_by_render_slots(monkeypatch) -> None:
    session = BlockingReadySession(expected_entries=4)
    repository = ReadyRepository()
    service = ReadyService()
    monkeypatch.setattr(artifact_worker.settings, "artifact_render_concurrency", 1)
    monkeypatch.setattr(
        artifact_worker,
        "ArtifactProductionRepository",
        lambda: repository,
    )
    monkeypatch.setattr(
        artifact_worker,
        "ArtifactProductionService",
        lambda **_unused: service,
    )

    activities = ArtifactActivities(lambda: session)
    activities._render_slots = asyncio.Semaphore(0)
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    waits = [
        asyncio.create_task(
            activities.wait_for_artifact_dependencies(
                ArtifactDependencyWaitInput(
                    production_run_id=str(session.run_id),
                    artifact_key="map.epicenter",
                    output_profile="a3v-professional",
                    deadline_at=deadline.isoformat(),
                )
            )
        )
        for _ in range(4)
    ]

    await asyncio.wait_for(asyncio.gather(*waits), timeout=1)

    assert session.entered == 4
