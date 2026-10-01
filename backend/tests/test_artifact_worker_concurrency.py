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


async def test_artifact_worker_runs_retention_with_supplied_stop_event(
    monkeypatch,
) -> None:
    retention_started = asyncio.Event()
    retention_stopped = asyncio.Event()

    class BlockingWorker:
        async def run(self) -> None:
            await asyncio.Event().wait()

    async def retention_loop(**kwargs) -> None:
        del kwargs
        retention_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            retention_stopped.set()

    monkeypatch.setattr(
        artifact_worker,
        "build_artifact_worker",
        lambda **_kwargs: BlockingWorker(),
    )
    monkeypatch.setattr(
        artifact_worker,
        "_run_retention_loop",
        retention_loop,
    )
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(
        artifact_worker.run_artifact_worker(
            stop_event,
            client=object(),
            configured=SimpleNamespace(
                artifact_retention_enabled=True,
                artifact_retention_interval_seconds=60,
            ),
        )
    )

    await asyncio.wait_for(retention_started.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(worker_task, timeout=1)

    assert retention_stopped.is_set()


async def test_production_worker_entry_starts_and_stops_retention(
    monkeypatch,
) -> None:
    retention_started = asyncio.Event()
    retention_stopped = asyncio.Event()
    captured: list[asyncio.Event] = []

    def install_signal_handlers(loop, stop_event: asyncio.Event) -> None:
        del loop
        captured.append(stop_event)

    class BlockingWorker:
        async def run(self) -> None:
            await asyncio.Event().wait()

    async def retention_loop(**_kwargs) -> None:
        retention_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            retention_stopped.set()

    monkeypatch.setattr(
        artifact_worker,
        "_install_signal_handlers",
        install_signal_handlers,
    )
    monkeypatch.setattr(
        artifact_worker,
        "build_artifact_worker",
        lambda **_kwargs: BlockingWorker(),
    )
    monkeypatch.setattr(
        artifact_worker,
        "_run_retention_loop",
        retention_loop,
    )

    process_task = asyncio.create_task(artifact_worker._run_process("worker"))
    await asyncio.wait_for(retention_started.wait(), timeout=1)
    assert len(captured) == 1
    captured[0].set()
    await asyncio.wait_for(process_task, timeout=1)

    assert retention_stopped.is_set()
