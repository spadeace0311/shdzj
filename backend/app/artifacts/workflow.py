from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_RENDER_CONCURRENCY = 4


def artifact_production_workflow_id(production_run_id: object) -> str:
    return f"artifact-production:{production_run_id}"


@dataclass(frozen=True, slots=True)
class ArtifactProductionWorkflowInput:
    production_run_id: str
    assessment_run_id: str
    event_id: str
    revision_id: str
    deadline_at: str
    catalog_version: str
    context_fingerprint: str
    launch_mode: str
    generation_seq: int
    generation_scope: str
    required_outputs: tuple[tuple[str, str], ...]
    render_concurrency: int = DEFAULT_ARTIFACT_RENDER_CONCURRENCY
    deadline_basis_at: str = ""

    def __post_init__(self) -> None:
        # Temporal's default dataclass converter does not serialize datetime.
        # deadline_at deliberately uses an ISO-8601 string with a timezone.
        parsed = datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("deadline_at must include timezone information")
        if not 1 <= self.render_concurrency <= 8:
            raise ValueError("render_concurrency must be between 1 and 8")
        if self.deadline_basis_at:
            basis = datetime.fromisoformat(
                self.deadline_basis_at.replace("Z", "+00:00")
            )
            if basis.tzinfo is None or basis.utcoffset() is None:
                raise ValueError(
                    "deadline_basis_at must include timezone information"
                )


@dataclass(frozen=True, slots=True)
class ArtifactProductionWorkflowResult:
    production_run_id: str
    status: str
    completed_count: int
    failed_count: int
    required_outputs: tuple[tuple[str, str], ...]
    deadline_exceeded_at: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactTaskActivityInput:
    production_run_id: str
    production_task_id: str
    artifact_key: str
    output_profile: str
    context_fingerprint: str
    input_fingerprint: str
    deadline_at: str
    already_completed: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactDependencyWaitInput:
    production_run_id: str
    artifact_key: str
    output_profile: str
    deadline_at: str


@dataclass(frozen=True, slots=True)
class ArtifactValidationInput:
    production_run_id: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class ArtifactPublicationInput:
    production_run_id: str
    observed_at: str
    published_by: str | None = None
    forced: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactProductionDeadlineInput:
    production_run_id: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class ArtifactTerminalizationInput:
    production_run_id: str
    observed_at: str
    reason: str
    summary: str


@dataclass(frozen=True, slots=True)
class ArtifactProductionPrepared:
    production_run_id: str
    deadline_at: datetime
    context_fingerprint: str
    launch_mode: str
    background_outputs: tuple[tuple[str, str], ...]
    intensity_outputs: tuple[tuple[str, str], ...]
    loss_core_outputs: tuple[tuple[str, str], ...]
    loss_final_outputs: tuple[tuple[str, str], ...]
    deadline_basis_at: datetime | None = None
    clock_started_at: datetime | None = None


@workflow.defn
class ArtifactProductionWorkflow:
    def __init__(self) -> None:
        self._status = "pending"
        self._render_concurrency = DEFAULT_ARTIFACT_RENDER_CONCURRENCY
        self._deadline_exceeded_at: str | None = None
        self._intensity_ready = False
        self._loss_core_ready = False
        self._loss_final_ready = False
        self._assessment_failed = False
        self._cancel_reason: str | None = None
        self._completed_count = 0
        self._failed_count = 0
        self._phase_tasks: list[asyncio.Task] = []

    @workflow.signal
    async def intensity_ready(self) -> None:
        self._intensity_ready = True

    @workflow.signal
    async def loss_core_ready(self) -> None:
        self._loss_core_ready = True

    @workflow.signal
    async def loss_final_ready(self) -> None:
        self._loss_final_ready = True

    @workflow.signal
    async def assessment_failed(self) -> None:
        self._assessment_failed = True

    @workflow.signal
    async def cancel_requested(self, reason: str) -> None:
        self._cancel_reason = reason or "cancel_requested"
        for task in self._phase_tasks:
            if not task.done():
                task.cancel()

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.query
    def deadline_exceeded_at(self) -> str | None:
        return self._deadline_exceeded_at

    @workflow.run
    async def run(
        self,
        request: ArtifactProductionWorkflowInput,
    ) -> ArtifactProductionWorkflowResult:
        self._render_concurrency = request.render_concurrency
        self._render_semaphore = asyncio.Semaphore(self._render_concurrency)
        prepared_payload = await workflow.execute_activity(
            "prepare_artifact_production",
            request,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_retry_policy(),
        )
        prepared = _as_prepared(prepared_payload)
        self._status = "running"

        deadline_task = asyncio.create_task(
            self._mark_deadline_when_due(prepared)
        )
        try:
            if prepared.launch_mode == "standalone":
                await self._run_outputs(request.required_outputs, prepared)
            else:
                await self._run_assessment_child(prepared)

            observed_at = self._logical_now(prepared)
            if observed_at >= prepared.deadline_at:
                self._deadline_exceeded_at = observed_at.isoformat()
            early_reason = self._early_reason(prepared)
            if early_reason == "cancel":
                cancellation = await workflow.execute_activity(
                    "cancel_artifact_production",
                    ArtifactTerminalizationInput(
                        production_run_id=prepared.production_run_id,
                        observed_at=observed_at.isoformat(),
                        reason="cancel_requested",
                        summary=self._cancel_reason or "cancel_requested",
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=_retry_policy(),
                )
                return self._result(
                    request,
                    prepared,
                    status=str(_mapping_value(cancellation, "status", "canceled")),
                )
            if early_reason in {"assessment_failed", "deadline"}:
                terminal = await workflow.execute_activity(
                    "terminalize_artifact_production",
                    ArtifactTerminalizationInput(
                        production_run_id=prepared.production_run_id,
                        observed_at=observed_at.isoformat(),
                        reason=early_reason,
                        summary=(
                            "assessment unavailable"
                            if early_reason == "assessment_failed"
                            else "production deadline exceeded"
                        ),
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=_retry_policy(),
                )
                terminal_status = str(
                    _mapping_value(terminal, "status", "failed")
                )
                await workflow.execute_activity(
                    "publish_artifact_production",
                    ArtifactPublicationInput(
                        production_run_id=prepared.production_run_id,
                        observed_at=observed_at.isoformat(),
                    ),
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=_retry_policy(),
                )
                return self._result(
                    request,
                    prepared,
                    status=terminal_status,
                )
            validation = await workflow.execute_activity(
                "validate_artifact_production",
                ArtifactValidationInput(
                    production_run_id=prepared.production_run_id,
                    observed_at=observed_at.isoformat(),
                ),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=_retry_policy(),
            )
            publication = await workflow.execute_activity(
                "publish_artifact_production",
                ArtifactPublicationInput(
                    production_run_id=prepared.production_run_id,
                    observed_at=observed_at.isoformat(),
                ),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=_retry_policy(),
            )
            status = _publication_status(publication) or _validation_status(validation)
            self._status = status
            self._completed_count = int(
                _mapping_value(validation or publication, "completed_count", 0)
            )
            self._failed_count = int(
                _mapping_value(validation or publication, "failed_count", 0)
            )
            return self._result(request, prepared, status=self._status)
        finally:
            if not deadline_task.done():
                deadline_task.cancel()
            await asyncio.gather(deadline_task, return_exceptions=True)

    async def _run_assessment_child(
        self,
        prepared: ArtifactProductionPrepared,
    ) -> None:
        background_task = asyncio.create_task(
            self._run_outputs(prepared.background_outputs, prepared)
        )
        pending = [background_task]
        self._phase_tasks = pending.copy()

        try:
            await self._wait_for_signal(
                prepared,
                lambda: self._intensity_ready,
            )
            if not self._should_stop_phase(prepared):
                pending.append(
                    asyncio.create_task(
                        self._run_outputs(prepared.intensity_outputs, prepared)
                    )
                )
                self._phase_tasks = pending.copy()

            await self._wait_for_signal(
                prepared,
                lambda: self._loss_core_ready,
            )
            if not self._should_stop_phase(prepared):
                pending.append(
                    asyncio.create_task(
                        self._run_outputs(prepared.loss_core_outputs, prepared)
                    )
                )
                self._phase_tasks = pending.copy()

            await self._wait_for_signal(
                prepared,
                lambda: self._loss_final_ready,
            )
            if not self._should_stop_phase(prepared):
                pending.append(
                    asyncio.create_task(
                        self._run_outputs(prepared.loss_final_outputs, prepared)
                    )
                )
                self._phase_tasks = pending.copy()

            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _early_reason(self, prepared: ArtifactProductionPrepared) -> str | None:
        if self._cancel_reason is not None:
            return "cancel"
        if self._assessment_failed:
            return "assessment_failed"
        if self._logical_now(prepared) >= prepared.deadline_at:
            return "deadline"
        return None

    def _result(
        self,
        request: ArtifactProductionWorkflowInput,
        prepared: ArtifactProductionPrepared,
        *,
        status: str,
    ) -> ArtifactProductionWorkflowResult:
        self._status = status
        return ArtifactProductionWorkflowResult(
            production_run_id=prepared.production_run_id,
            status=status,
            completed_count=self._completed_count,
            failed_count=self._failed_count,
            required_outputs=request.required_outputs,
            deadline_exceeded_at=self._deadline_exceeded_at,
        )

    async def _wait_for_signal(
        self,
        prepared: ArtifactProductionPrepared,
        predicate: Any,
    ) -> None:
        delay = max(
            timedelta(seconds=0),
            prepared.deadline_at - self._logical_now(prepared),
        )
        try:
            await workflow.wait_condition(
                lambda: bool(predicate())
                or self._assessment_failed
                or self._cancel_reason is not None
                or self._logical_now(prepared) >= prepared.deadline_at,
                timeout=delay + timedelta(seconds=1),
            )
        except TimeoutError:
            return

    def _should_stop_phase(
        self,
        prepared: ArtifactProductionPrepared,
    ) -> bool:
        return (
            self._assessment_failed
            or self._cancel_reason is not None
            or self._logical_now(prepared) >= prepared.deadline_at
        )

    async def _run_outputs(
        self,
        outputs: tuple[tuple[str, str], ...],
        prepared: ArtifactProductionPrepared,
    ) -> None:
        tasks = [
            asyncio.create_task(
                self._run_output(artifact_key, output_profile, prepared)
            )
            for artifact_key, output_profile in outputs
        ]
        self._phase_tasks.extend(tasks)
        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                self._failed_count += 1
            else:
                self._completed_count += 1

    async def _run_output(
        self,
        artifact_key: str,
        output_profile: str,
        prepared: ArtifactProductionPrepared,
    ) -> None:
        wait_payload = await workflow.execute_activity(
            "wait_for_artifact_dependencies",
                ArtifactDependencyWaitInput(
                    production_run_id=prepared.production_run_id,
                    artifact_key=artifact_key,
                    output_profile=output_profile,
                    deadline_at=prepared.deadline_at.isoformat(),
            ),
            start_to_close_timeout=timedelta(seconds=320),
            retry_policy=_retry_policy(),
        )
        task_input = _as_task_input(wait_payload)
        if task_input.already_completed:
            return
        if not _task_ready(task_input):
            raise ApplicationError(
                "artifact dependencies are not ready",
                type="artifact_dependency_unavailable",
                non_retryable=True,
            )

        async with self._render_semaphore:
            if _is_map_artifact(artifact_key):
                await workflow.execute_activity(
                    "render_map_artifact",
                    task_input,
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=_retry_policy(),
                )
            elif _is_pptx_artifact(artifact_key):
                await workflow.execute_activity(
                    "compose_pptx_artifact",
                    task_input,
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=_retry_policy(),
                )
            else:
                await workflow.execute_activity(
                    "compose_docx_artifact",
                    task_input,
                    start_to_close_timeout=timedelta(seconds=300),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=_retry_policy(),
                )

    async def _mark_deadline_when_due(
        self,
        prepared: ArtifactProductionPrepared,
    ) -> None:
        delay = prepared.deadline_at - self._logical_now(prepared)
        if delay.total_seconds() > 0:
            await workflow.sleep(delay)
        self._deadline_exceeded_at = self._logical_now(prepared).isoformat()
        await workflow.execute_activity(
            "mark_production_deadline_exceeded",
            ArtifactProductionDeadlineInput(
                production_run_id=prepared.production_run_id,
                observed_at=self._deadline_exceeded_at,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_retry_policy(),
        )

    def _logical_now(self, prepared: ArtifactProductionPrepared) -> datetime:
        if (
            prepared.deadline_basis_at is None
            or prepared.clock_started_at is None
        ):
            return workflow.now()
        return prepared.deadline_basis_at + (
            workflow.now() - prepared.clock_started_at
        )


def _is_map_artifact(artifact_key: str) -> bool:
    return artifact_key.startswith("map.")


def _is_pptx_artifact(artifact_key: str) -> bool:
    return artifact_key.startswith("deck.")


def _task_ready(value: Any) -> bool:
    if isinstance(value, ArtifactTaskActivityInput):
        return True
    if isinstance(value, dict):
        return bool(value.get("input_fingerprint")) and bool(
            value.get("production_task_id")
        )
    return False


def _as_task_input(value: object) -> ArtifactTaskActivityInput:
    if isinstance(value, ArtifactTaskActivityInput):
        return value
    if not isinstance(value, dict):
        raise TypeError("wait_for_artifact_dependencies result must be a mapping")
    deadline = value.get("deadline_at")
    if not isinstance(deadline, str):
        deadline = str(deadline)
    return ArtifactTaskActivityInput(
        production_run_id=str(value["production_run_id"]),
        production_task_id=str(value["production_task_id"]),
        artifact_key=str(value["artifact_key"]),
        output_profile=str(value["output_profile"]),
        context_fingerprint=str(value["context_fingerprint"]),
        input_fingerprint=str(value["input_fingerprint"]),
        deadline_at=deadline,
        already_completed=bool(value.get("already_completed", False)),
    )


def _as_prepared(value: object) -> ArtifactProductionPrepared:
    if isinstance(value, ArtifactProductionPrepared):
        return value
    if not isinstance(value, dict):
        raise TypeError("prepare_artifact_production result must be a mapping")
    deadline = value.get("deadline_at")
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    elif isinstance(deadline, datetime):
        deadline = deadline
    else:
        raise TypeError("prepared artifact deadline must be a timestamp")
    return ArtifactProductionPrepared(
        production_run_id=str(value["production_run_id"]),
        deadline_at=deadline,
        context_fingerprint=str(value.get("context_fingerprint") or ""),
        launch_mode=str(value.get("launch_mode") or "assessment_child"),
        background_outputs=_outputs(value.get("background_outputs", ())),
        intensity_outputs=_outputs(value.get("intensity_outputs", ())),
        loss_core_outputs=_outputs(value.get("loss_core_outputs", ())),
        loss_final_outputs=_outputs(value.get("loss_final_outputs", ())),
        deadline_basis_at=_optional_datetime(
            value.get("deadline_basis_at")
        ),
        clock_started_at=_optional_datetime(
            value.get("clock_started_at")
        ),
    )


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError("prepared artifact clock value must be a timestamp")


def _outputs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[tuple[str, str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            result.append((str(item[0]), str(item[1])))
        elif isinstance(item, dict):
            result.append(
                (str(item.get("artifact_key")), str(item.get("output_profile")))
            )
    return tuple(result)


def _validation_status(value: object) -> str:
    return str(_mapping_value(value, "status", "partial"))


def _publication_status(value: object) -> str:
    return str(_mapping_value(value, "status", "partial"))


def _mapping_value(value: object, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return default


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(
        maximum_attempts=3,
        initial_interval=timedelta(seconds=2),
        maximum_interval=timedelta(seconds=8),
        backoff_coefficient=4.0,
    )
