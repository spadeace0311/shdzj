from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import process_health
from app.collaboration.retention import CollaborationRetentionResult
from app.operations.health import (
    OperationsHealthState,
    get_operations_health_state,
    health_app,
    set_operations_health_state,
)
from app.operations.scheduler import OperationsSchedulerResult
from app.operations.worker import (
    OperationsWorkerCycleResult,
    run_operations_cycle,
    run_operations_worker,
)


OBSERVED_AT = datetime(2026, 10, 11, 2, 30, tzinfo=UTC)


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _FakeSession:
        return self


class _FakeSessionFactory:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


class _FakeScheduler:
    def __init__(self, result: OperationsSchedulerResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def ensure_schedule(self, session: object, observed_at: datetime) -> object:
        del session, observed_at
        self.calls.append("ensure_schedule")
        return object()

    async def dispatch_due(
        self,
        observed_at: datetime,
        trigger: object,
    ) -> OperationsSchedulerResult:
        del observed_at, trigger
        self.calls.append("dispatch_due")
        return self.result


class _FakePriorityGuard:
    def __init__(self, released: int = 1) -> None:
        self.released = released
        self.calls: list[tuple[datetime]] = []

    async def release_deferred_runs(
        self,
        session: object,
        observed_at: datetime,
    ) -> int:
        del session
        self.calls.append((observed_at,))
        return self.released


class _FakeRetentionService:
    def __init__(
        self,
        result: CollaborationRetentionResult | None = None,
    ) -> None:
        self.result = result or CollaborationRetentionResult()
        self.calls: list[tuple[datetime]] = []

    async def purge_due(
        self,
        session: object,
        observed_at: datetime,
    ) -> CollaborationRetentionResult:
        del session
        self.calls.append((observed_at,))
        return self.result


class _FakeHealthState:
    def __init__(self) -> None:
        self.marked_at: datetime | None = None

    def mark_alive(self, now: datetime | None = None) -> None:
        self.marked_at = now


def _scheduler_result(
    *,
    created: int = 0,
    due: int = 0,
    triggered: int = 0,
    deferred: int = 0,
    failed: int = 0,
) -> OperationsSchedulerResult:
    return OperationsSchedulerResult(
        created=created,
        due=due,
        triggered=triggered,
        deferred=deferred,
        failed=failed,
    )


def _configured(*, interval_seconds: int = 60) -> SimpleNamespace:
    return SimpleNamespace(
        operations_retention_interval_seconds=interval_seconds,
    )


async def test_cycle_ensures_before_dispatch_and_aggregates_counts() -> None:
    scheduler = _FakeScheduler(
        _scheduler_result(
            created=1,
            due=1,
            triggered=1,
            deferred=2,
            failed=1,
        )
    )
    priority_guard = _FakePriorityGuard()
    health = _FakeHealthState()

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=scheduler,
        priority_guard=priority_guard,
        retention_service=_FakeRetentionService(),
        health_state=health,
        last_retention_at=OBSERVED_AT,
    )

    assert scheduler.calls == ["ensure_schedule", "dispatch_due"]
    assert priority_guard.calls == [(OBSERVED_AT,)]
    assert result.schedules_created == 1
    assert result.schedules_due == 1
    assert result.tests_triggered == 1
    assert result.tests_deferred == 2
    assert result.schedules_failed == 1
    assert result.retention_ran is False
    assert result.retention_test_deleted == 0
    assert result.retention_drill_deleted == 0
    assert health.marked_at == OBSERVED_AT


async def test_cycle_runs_retention_after_interval() -> None:
    retention = _FakeRetentionService(
        CollaborationRetentionResult(test_deleted=3, drill_deleted=2)
    )

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=_FakeScheduler(_scheduler_result()),
        priority_guard=_FakePriorityGuard(),
        retention_service=retention,
        health_state=_FakeHealthState(),
        last_retention_at=OBSERVED_AT - timedelta(seconds=61),
    )

    assert retention.calls == [(OBSERVED_AT,)]
    assert result.retention_ran is True
    assert result.retention_test_deleted == 3
    assert result.retention_drill_deleted == 2


async def test_cycle_skips_retention_before_interval() -> None:
    retention = _FakeRetentionService(
        CollaborationRetentionResult(test_deleted=1)
    )

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=_FakeScheduler(_scheduler_result()),
        priority_guard=_FakePriorityGuard(),
        retention_service=retention,
        health_state=_FakeHealthState(),
        last_retention_at=OBSERVED_AT - timedelta(seconds=59),
    )

    assert retention.calls == []
    assert result.retention_ran is False
    assert result.retention_test_deleted == 0


async def test_schedule_failure_does_not_block_retention() -> None:
    retention = _FakeRetentionService(
        CollaborationRetentionResult(test_deleted=1)
    )

    result = await run_operations_cycle(
        session_factory=_FakeSessionFactory(),
        observed_at=OBSERVED_AT,
        configured=_configured(),
        scheduler=_FakeScheduler(
            _scheduler_result(due=1, failed=1)
        ),
        priority_guard=_FakePriorityGuard(),
        retention_service=retention,
        health_state=_FakeHealthState(),
        last_retention_at=OBSERVED_AT - timedelta(seconds=61),
    )

    assert retention.calls == [(OBSERVED_AT,)]
    assert result.schedules_failed == 1
    assert result.retention_ran is True
    assert result.retention_test_deleted == 1


async def test_worker_loop_stops_before_cycle_when_event_is_set() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    cycles: list[OperationsWorkerCycleResult] = []

    async def fake_cycle(**kwargs: object) -> OperationsWorkerCycleResult:
        del kwargs
        cycles.append(OperationsWorkerCycleResult())
        return cycles[-1]

    await run_operations_worker(
        stop_event,
        session_factory=_FakeSessionFactory(),
        configured=SimpleNamespace(
            operations_worker_enabled=True,
            operations_worker_poll_seconds=0.01,
        ),
        worker_cycle=fake_cycle,
    )

    assert cycles == []


async def test_worker_loop_isolates_cycle_exceptions() -> None:
    stop_event = asyncio.Event()
    calls = 0

    async def fake_cycle(**kwargs: object) -> OperationsWorkerCycleResult:
        del kwargs
        nonlocal calls
        calls += 1
        stop_event.set()
        raise RuntimeError("cycle failed")

    await run_operations_worker(
        stop_event,
        session_factory=_FakeSessionFactory(),
        configured=SimpleNamespace(
            operations_worker_enabled=True,
            operations_worker_poll_seconds=0.01,
        ),
        worker_cycle=fake_cycle,
    )

    assert calls == 1


def test_health_state_returns_ok_until_stale_window() -> None:
    now = datetime(2026, 10, 11, 2, 30, tzinfo=UTC)
    state = OperationsHealthState(stale_seconds=30)

    state.mark_alive(now)

    assert state.payload(now)["status"] == "ok"
    assert state.payload(now + timedelta(seconds=31))["status"] == "unavailable"


def test_health_endpoint_uses_operations_state() -> None:
    state = OperationsHealthState(stale_seconds=30)
    state.mark_alive(OBSERVED_AT)
    set_operations_health_state(state)

    try:
        response = TestClient(health_app).get("/health")
    finally:
        set_operations_health_state(OperationsHealthState(stale_seconds=30))

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert get_operations_health_state() is not state


def _write_cmdline(path: Path, *parts: str) -> None:
    path.write_bytes(b"\0".join(part.encode() for part in parts) + b"\0")


def test_find_process_matches_operations_worker(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "126"
    process_dir.mkdir(parents=True)
    _write_cmdline(
        process_dir / "cmdline",
        "python",
        "-m",
        "app.operations.worker",
    )

    assert process_health.find_process(
        "operations",
        proc_root=proc_root,
        own_pid=999,
    ) == 126
