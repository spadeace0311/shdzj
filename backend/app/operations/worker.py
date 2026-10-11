from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.collaboration.retention import (
    CollaborationRetentionResult,
    CollaborationRetentionService,
)
from app.artifacts.retention import ArtifactRetentionService
from app.artifacts.storage import ArtifactStore
from app.config import Settings, settings
from app.db import SessionFactory
from app.event_object_cleanup import process_cleanup_intents
from app.operations.health import (
    OperationsHealthState,
    get_operations_health_state,
    set_operations_health_state,
)
from app.operations.priority import OperationsPriorityGuard
from app.operations.scheduler import DailyTestScheduler, OperationsSchedulerResult
from app.operations.service import OperationsService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OperationsWorkerCycleResult:
    schedules_created: int = 0
    schedules_due: int = 0
    tests_triggered: int = 0
    tests_deferred: int = 0
    schedules_failed: int = 0
    runs_reconciled: int = 0
    retention_ran: bool = False
    retention_test_deleted: int = 0
    retention_drill_deleted: int = 0
    artifact_test_deleted: int = 0
    artifact_drill_deleted: int = 0
    cleanup_failed_count: int = 0

    def counts(self) -> dict[str, int | bool]:
        return {
            "schedules_created": self.schedules_created,
            "schedules_due": self.schedules_due,
            "tests_triggered": self.tests_triggered,
            "tests_deferred": self.tests_deferred,
            "schedules_failed": self.schedules_failed,
            "runs_reconciled": self.runs_reconciled,
            "retention_ran": self.retention_ran,
            "retention_test_deleted": self.retention_test_deleted,
            "retention_drill_deleted": self.retention_drill_deleted,
            "artifact_test_deleted": self.artifact_test_deleted,
            "artifact_drill_deleted": self.artifact_drill_deleted,
            "cleanup_failed_count": self.cleanup_failed_count,
        }


async def run_operations_cycle(
    session_factory: object,
    observed_at: datetime,
    *,
    configured: Settings = settings,
    scheduler: object | None = None,
    operations_service: object | None = None,
    priority_guard: object | None = None,
    retention_service: object | None = None,
    artifact_retention_service: ArtifactRetentionService | None = None,
    artifact_store: ArtifactStore | None = None,
    health_state: OperationsHealthState | None = None,
    last_retention_at: datetime | None = None,
) -> OperationsWorkerCycleResult:
    observed_at = _as_utc(observed_at)
    now = lambda: observed_at  # noqa: E731
    scheduler = scheduler or DailyTestScheduler(session_factory=session_factory)
    operations_service = operations_service or OperationsService(
        session_factory=session_factory,
        now=now,
    )
    priority_guard = priority_guard or OperationsPriorityGuard()
    retention_service = retention_service or CollaborationRetentionService()
    artifact_retention_service = (
        artifact_retention_service or ArtifactRetentionService()
    )
    health_state = health_state or get_operations_health_state()

    async with session_factory() as session:
        async with session.begin():
            schedules_created = await _ensure_schedule(
                scheduler,
                session,
                observed_at,
            )

    scheduler_result: OperationsSchedulerResult = await scheduler.dispatch_due(
        observed_at,
        operations_service.trigger_scheduled_test,
    )

    async with session_factory() as session:
        async with session.begin():
            await priority_guard.release_deferred_runs(session, observed_at)
            runs_reconciled = (
                await operations_service.reconcile_runs(
                    session,
                    observed_at,
                )
                if hasattr(session, "scalars")
                else 0
            )

    retention_ran = False
    retention_result = CollaborationRetentionResult()
    artifact_retention_result = None
    cleanup_result = None
    if _retention_is_due(
        observed_at,
        last_retention_at,
        configured.operations_retention_interval_seconds,
    ):
        if isinstance(retention_service, CollaborationRetentionService):
            artifact_store = artifact_store or ArtifactStore(
                configured.artifact_storage_root,
                namespace=getattr(
                    configured,
                    "artifact_storage_namespace",
                    None,
                ),
            )
            if getattr(configured, "artifact_retention_enabled", True):
                async with session_factory() as session:
                    async with session.begin():
                        artifact_retention_result = (
                            await artifact_retention_service.retain_expired(
                                session,
                                observed_at=observed_at,
                                storage_namespace=artifact_store.storage_namespace,
                            )
                        )
            async with session_factory() as session:
                async with session.begin():
                    retention_result = await retention_service.purge_due(
                        session,
                        observed_at,
                        storage_namespace=artifact_store.storage_namespace,
                    )
            cleanup_result = await process_cleanup_intents(
                session_factory,
                artifact_store,
            )
            if not cleanup_result.succeeded:
                raise RuntimeError("operations retention cleanup incomplete")
        else:
            async with session_factory() as session:
                async with session.begin():
                    retention_result = await retention_service.purge_due(
                        session,
                        observed_at,
                    )
        retention_ran = True

    health_state.mark_alive(observed_at)
    result = OperationsWorkerCycleResult(
        schedules_created=schedules_created,
        schedules_due=scheduler_result.due,
        tests_triggered=scheduler_result.triggered,
        tests_deferred=scheduler_result.deferred,
        schedules_failed=scheduler_result.failed,
        runs_reconciled=runs_reconciled,
        retention_ran=retention_ran,
        retention_test_deleted=retention_result.test_deleted,
        retention_drill_deleted=retention_result.drill_deleted,
        artifact_test_deleted=(
            artifact_retention_result.test_deleted
            if artifact_retention_result is not None
            else 0
        ),
        artifact_drill_deleted=(
            artifact_retention_result.drill_deleted
            if artifact_retention_result is not None
            else 0
        ),
        cleanup_failed_count=(
            cleanup_result.failed_count + cleanup_result.dead_letter_count
            + cleanup_result.namespace_mismatch_count
            + cleanup_result.unprocessable_count
            if cleanup_result is not None
            else 0
        ),
    )
    _log_worker_cycle(result)
    return result


async def _ensure_schedule(
    scheduler: object,
    session: object,
    observed_at: datetime,
) -> int:
    if hasattr(scheduler, "ensure_schedule_with_created"):
        _schedule, created = await scheduler.ensure_schedule_with_created(
            session,
            observed_at,
        )
        return 1 if created else 0
    await scheduler.ensure_schedule(session, observed_at)
    return 0


async def run_operations_worker(
    stop_event: asyncio.Event | None = None,
    *,
    session_factory: object = SessionFactory,
    configured: Settings = settings,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    worker_cycle: Callable[..., Awaitable[OperationsWorkerCycleResult]] | None = None,
) -> None:
    if not configured.operations_worker_enabled:
        raise RuntimeError("operations worker is disabled")
    stop_event = stop_event or asyncio.Event()
    cycle = worker_cycle or run_operations_cycle
    last_retention_at: datetime | None = None

    while not stop_event.is_set():
        observed_at = _as_utc(now())
        try:
            result = await cycle(
                session_factory=session_factory,
                observed_at=observed_at,
                configured=configured,
                last_retention_at=last_retention_at,
            )
            if result.retention_ran:
                last_retention_at = observed_at
        except Exception:
            logger.exception("operations worker cycle failed")
        await _wait_for_stop_or_sleep(
            stop_event,
            configured.operations_worker_poll_seconds,
            sleep,
        )


def _retention_is_due(
    observed_at: datetime,
    last_retention_at: datetime | None,
    interval_seconds: int,
) -> bool:
    if last_retention_at is None:
        return True
    return observed_at - _as_utc(last_retention_at) >= timedelta(
        seconds=interval_seconds
    )


def _log_worker_cycle(result: OperationsWorkerCycleResult) -> None:
    payload = json.dumps(
        result.counts(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    logger.info("operations worker cycle counts=%s", payload)


async def _wait_for_stop_or_sleep(
    stop_event: asyncio.Event,
    delay: float,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    if stop_event.is_set():
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        await sleep(0)


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda _signum, _frame: stop_event.set())


async def _run_process() -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)
    await run_operations_worker(stop_event)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s",'
        '"logger":"%(name)s","message":"%(message)s"}',
    )
    health_file = os.environ.get(
        "OPERATIONS_HEALTH_FILE",
        "/tmp/operations-worker-health.json",
    )
    set_operations_health_state(
        OperationsHealthState(
            stale_seconds=30,
            health_file=health_file,
        )
    )
    try:
        asyncio.run(_run_process())
    except KeyboardInterrupt:
        pass


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("datetime must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(UTC)


if __name__ == "__main__":
    main()
