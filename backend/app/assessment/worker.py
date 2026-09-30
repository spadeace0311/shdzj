from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.worker import Worker

from app.assessment.dispatcher import AssessmentDispatcher
from app.assessment.temporal import (
    AssessmentActivities,
    AssessmentWorkflow,
    AssessmentWorkflowInput,
)
from app.artifacts.worker import ArtifactActivities
from app.artifacts.workflow import ArtifactProductionWorkflow
from app.config import Settings, settings
from app.db import SessionFactory

logger = logging.getLogger(__name__)


class TemporalAssessmentStarter:
    def __init__(
        self,
        client: Client,
        *,
        task_queue: str,
        execution_timeout: timedelta,
    ) -> None:
        self._client = client
        self._task_queue = task_queue
        self._execution_timeout = execution_timeout

    async def start_assessment(
        self,
        *,
        workflow_id: str,
        payload: dict[str, object],
    ) -> None:
        request = AssessmentWorkflowInput(
            event_id=_required_payload_text(payload, "event_id"),
            revision_id=_required_payload_text(payload, "revision_id"),
            outbox_id=_required_payload_text(payload, "outbox_id"),
        )
        await self._client.start_workflow(
            AssessmentWorkflow.run,
            request,
            id=workflow_id,
            task_queue=self._task_queue,
            execution_timeout=self._execution_timeout,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )


def build_dispatcher(
    *,
    client: Client,
    session_factory: object,
    configured: Settings = settings,
) -> AssessmentDispatcher:
    return AssessmentDispatcher(
        session_factory=session_factory,
        starter=TemporalAssessmentStarter(
            client,
            task_queue=configured.temporal_task_queue,
            execution_timeout=timedelta(
                seconds=configured.assessment_workflow_safety_timeout_seconds
            ),
        ),
        batch_size=configured.assessment_outbox_batch_size,
        max_attempts=configured.assessment_outbox_max_attempts,
        lease_seconds=configured.assessment_outbox_lease_seconds,
    )


def build_worker(
    *,
    client: Client,
    session_factory: object,
    configured: Settings = settings,
) -> Worker:
    activities = AssessmentActivities(session_factory)
    artifact_activities = ArtifactActivities(session_factory)
    return Worker(
        client,
        task_queue=configured.temporal_task_queue,
        workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
        activities=[
            activities.prepare_assessment,
            activities.run_intensity_model,
            activities.run_intensity_instrument,
            activities.run_intensity_fusion,
            activities.run_loss_buildings,
            activities.run_loss_population,
            activities.run_loss_casualties,
            activities.run_loss_economic,
            activities.run_loss_resources,
            activities.run_loss_validate,
            activities.mark_deadline_exceeded,
            activities.observe_task_deadlines,
            activities.reconcile_assessment_timeouts,
            activities.finalize_assessment,
            activities.mark_artifact_production_launched,
            artifact_activities.prepare_artifact_production,
            artifact_activities.wait_for_artifact_dependencies,
            artifact_activities.render_map_artifact,
            artifact_activities.compose_docx_artifact,
            artifact_activities.compose_pptx_artifact,
            artifact_activities.validate_artifact_production,
            artifact_activities.publish_artifact_production,
            artifact_activities.mark_production_deadline_exceeded,
        ],
        graceful_shutdown_timeout=timedelta(seconds=10),
    )


async def run_dispatcher(
    stop_event: asyncio.Event | None = None,
    *,
    client: Client | None = None,
    session_factory: object = SessionFactory,
    configured: Settings = settings,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if not configured.assessment_dispatcher_enabled:
        raise RuntimeError("assessment dispatcher is disabled")

    stop_event = stop_event or asyncio.Event()
    client = client or await _connect_temporal(configured)
    dispatcher = build_dispatcher(
        client=client,
        session_factory=session_factory,
        configured=configured,
    )
    while not stop_event.is_set():
        dispatched = await dispatcher.dispatch_once()
        await _reconcile_assessment_timeouts(
            session_factory,
            configured,
        )
        if dispatched == 0:
            await sleep(configured.assessment_outbox_poll_seconds)


async def run_worker(
    stop_event: asyncio.Event | None = None,
    *,
    client: Client | None = None,
    session_factory: object = SessionFactory,
    configured: Settings = settings,
) -> None:
    client = client or await _connect_temporal(configured)
    worker = build_worker(
        client=client,
        session_factory=session_factory,
        configured=configured,
    )
    if stop_event is None:
        await worker.run()
        return
    await _run_until_stopped(worker.run(), stop_event)


async def _connect_temporal(configured: Settings) -> Client:
    return await Client.connect(
        configured.temporal_address,
        namespace=configured.temporal_namespace,
    )


async def _reconcile_assessment_timeouts(
    session_factory: object,
    configured: Settings,
) -> list[str]:
    from app.assessment.repository import AssessmentRepository

    repository = AssessmentRepository()
    observed_at = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            failed_ids = await repository.reconcile_timeouts(
                session,
                safety_timeout_seconds=configured.assessment_workflow_safety_timeout_seconds,
                observed_at=observed_at,
            )
    for run_id in failed_ids:
        logger.error(
            "assessment workflow safety timeout run_id=%s",
            run_id,
        )
    return [str(run_id) for run_id in failed_ids]


async def _run_until_stopped(
    worker_run: Awaitable[None],
    stop_event: asyncio.Event,
) -> None:
    worker_task = asyncio.create_task(worker_run)
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {worker_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if worker_task in done:
        await worker_task


def _required_payload_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"assessment workflow payload requires {key}")
    return value


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda _signum, _frame: stop_event.set())


async def _run_process(mode: str) -> None:
    stop_event = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop_event)
    if mode == "dispatcher":
        await run_dispatcher(stop_event)
    else:
        await run_worker(stop_event)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("process", choices=("dispatcher", "worker"))
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    )
    try:
        asyncio.run(_run_process(args.process))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
