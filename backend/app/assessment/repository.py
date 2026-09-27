from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.plan import AssessmentPlanBuilder
from app.events.domain import EventKind
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox
from app.intensity.models import AssessmentTaskAttempt


class AssessmentRepository:
    def __init__(self, plan_builder: AssessmentPlanBuilder | None = None) -> None:
        self._plan_builder = plan_builder or AssessmentPlanBuilder()

    async def ensure_run_and_tasks(
        self,
        session: AsyncSession,
        *,
        event: EarthquakeEvent,
        revision: EarthquakeRevision,
        outbox: EventLifecycleOutbox,
    ) -> AssessmentRun:
        _validate_trigger_identity(event, revision, outbox)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"assessment-outbox:{outbox.id}"},
        )

        existing = await session.scalar(
            select(AssessmentRun).where(AssessmentRun.outbox_id == outbox.id)
        )
        if existing is not None:
            return existing

        canonical = await session.get(EarthquakeEvent, event.id, with_for_update=True)
        if canonical is None:
            raise LookupError(f"event not found: {event.id}")
        if canonical.t1_at is None:
            raise ValueError("event T1 is required before assessment orchestration")

        latest_run_no = await session.scalar(
            select(func.max(AssessmentRun.run_no)).where(
                AssessmentRun.event_id == canonical.id
            )
        )
        plan = self._plan_builder.build(
            event_kind=EventKind(revision.revision_kind),
            institutional_level=revision.institutional_level,
            service_level=revision.service_level,
        )
        basis_at = revision.ingested_at or canonical.t1_at
        deadline_at = basis_at + timedelta(seconds=300)
        run = AssessmentRun(
            event_id=canonical.id,
            revision_id=revision.id,
            outbox_id=outbox.id,
            run_no=int(latest_run_no or 0) + 1,
            trigger_reason=outbox.trigger_reason,
            status="pending",
            priority=max(task.priority for task in plan),
            t1_at=canonical.t1_at,
            report_ingested_at=basis_at,
            deadline_basis_at=basis_at,
            deadline_at=deadline_at,
            started_at=None,
            snapshot={
                "event_id": str(canonical.id),
                "revision_id": str(revision.id),
                "revision_no": revision.revision_no,
                "t1_at": canonical.t1_at.isoformat(),
                "response_rule_version": revision.response_rule_version,
                "region_boundary_version": revision.region_boundary_version,
            },
            created_at=outbox.created_at,
            updated_at=outbox.created_at,
        )
        session.add(run)
        await session.flush()
        canonical.latest_assessment_run_id = run.id
        await session.execute(
            update(AssessmentRun)
            .where(
                AssessmentRun.event_id == canonical.id,
                AssessmentRun.id != run.id,
                AssessmentRun.superseded_at.is_(None),
            )
            .values(
                superseded_by_run_id=run.id,
                superseded_at=outbox.created_at,
            )
        )

        for task in plan:
            session.add(
                AssessmentTask(
                    run_id=run.id,
                    task_key=task.task_key,
                    task_type=task.task_type,
                    component=task.component,
                    priority=task.priority,
                    sequence=task.sequence,
                    status="pending",
                    deadline_at=basis_at
                    + timedelta(seconds=task.deadline_offset_seconds),
                    attempt_count=0,
                    max_attempts=task.max_attempts,
                    created_at=outbox.created_at,
                    updated_at=outbox.created_at,
                )
            )
        await session.flush()
        return run

    async def ensure_run_from_outbox(
        self,
        session: AsyncSession,
        *,
        event_id: str,
        revision_id: str,
        outbox_id: str,
    ) -> AssessmentRun:
        try:
            event_uuid = UUID(event_id)
            revision_uuid = UUID(revision_id)
            outbox_uuid = UUID(outbox_id)
        except ValueError as exc:
            raise ValueError("assessment trigger identifiers must be UUIDs") from exc

        outbox = await session.get(
            EventLifecycleOutbox,
            outbox_uuid,
            with_for_update=True,
        )
        event = await session.get(EarthquakeEvent, event_uuid)
        revision = await session.get(EarthquakeRevision, revision_uuid)
        if outbox is None or event is None or revision is None:
            raise LookupError("assessment trigger not found")
        if outbox.event_id != event_uuid or outbox.revision_id != revision_uuid:
            raise ValueError("assessment trigger identifiers do not match")

        return await self.ensure_run_and_tasks(
            session,
            event=event,
            revision=revision,
            outbox=outbox,
        )

    async def start_task(
        self,
        session: AsyncSession,
        run_id: UUID,
        task_key: str,
        algorithm_version: str,
        input_fingerprint: str,
    ) -> AssessmentTask:
        task = await session.scalar(
            select(AssessmentTask)
            .where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == task_key,
            )
            .with_for_update()
        )
        if task is None:
            raise LookupError("assessment task not found")
        if task.status == "succeeded":
            if task.algorithm_version != algorithm_version:
                raise ValueError("task algorithm version changed")
            if task.input_fingerprint != input_fingerprint:
                raise ValueError("task input fingerprint changed")
            return task
        if task.status == "running":
            if task.algorithm_version != algorithm_version:
                raise ValueError("task algorithm version changed")
            if task.input_fingerprint != input_fingerprint:
                raise ValueError("task input fingerprint changed")
            return task
        if task.status not in {"pending", "failed"}:
            raise ValueError("terminal task cannot be restarted")
        if task.algorithm_version not in {None, algorithm_version}:
            raise ValueError("task algorithm version changed")
        if task.input_fingerprint not in {None, input_fingerprint}:
            raise ValueError("task input fingerprint changed")

        task.status = "running"
        task.algorithm_version = algorithm_version
        task.input_fingerprint = input_fingerprint
        task.started_at = task.started_at or datetime.now(UTC)
        task.completed_at = None
        task.last_error = None
        task.attempt_count += 1
        session.add(
            AssessmentTaskAttempt(
                task_id=task.id,
                attempt_number=task.attempt_count,
                status="running",
                started_at=datetime.now(UTC),
                input_fingerprint=input_fingerprint,
            )
        )
        return task

    async def complete_task(
        self,
        session: AsyncSession,
        task_id: UUID,
        output_checksum: str | None,
        result: dict,
    ) -> AssessmentTask:
        task = await session.get(AssessmentTask, task_id, with_for_update=True)
        if task is None:
            raise LookupError("assessment task not found")
        if task.status == "succeeded":
            if task.output_checksum != output_checksum:
                raise ValueError("task output checksum changed")
            return task
        if task.status != "running":
            raise ValueError("task must be running before it can complete")
        now = datetime.now(UTC)
        task.status = "succeeded"
        task.output_checksum = output_checksum
        task.result = result
        task.completed_at = now
        task.last_error = None
        attempt = await session.scalar(
            select(AssessmentTaskAttempt).where(
                AssessmentTaskAttempt.task_id == task.id,
                AssessmentTaskAttempt.attempt_number == task.attempt_count,
            )
        )
        if attempt is not None:
            attempt.status = "succeeded"
            attempt.output_checksum = output_checksum
            attempt.completed_at = now
        return task

    async def fail_task(
        self,
        session: AsyncSession,
        task_id: UUID,
        error_category: str,
        error_summary: str,
    ) -> AssessmentTask:
        task = await session.get(AssessmentTask, task_id, with_for_update=True)
        if task is None:
            raise LookupError("assessment task not found")
        if task.status in {"succeeded", "failed"}:
            raise ValueError("terminal task cannot be overwritten")
        if task.status != "running":
            raise ValueError("task must be running before it can fail")
        now = datetime.now(UTC)
        task.status = "failed"
        task.completed_at = now
        task.last_error = error_summary[:2000]
        attempt = await session.scalar(
            select(AssessmentTaskAttempt).where(
                AssessmentTaskAttempt.task_id == task.id,
                AssessmentTaskAttempt.attempt_number == task.attempt_count,
            )
        )
        if attempt is not None:
            attempt.status = "failed"
            attempt.error_category = error_category
            attempt.error_summary = error_summary[:2000]
            attempt.completed_at = now
        return task

    async def record_task_failure_audit(
        self,
        session: AsyncSession,
        run_id: UUID,
        task_key: str,
        error_category: str,
        error_summary: str,
    ) -> AssessmentTask | None:
        task = await session.scalar(
            select(AssessmentTask)
            .where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == task_key,
            )
            .with_for_update()
        )
        if task is None:
            return None
        if task.status in {"succeeded", "failed"}:
            return task

        now = datetime.now(UTC)
        bounded_summary = error_summary[:2000]
        task.status = "failed"
        task.completed_at = now
        task.last_error = bounded_summary

        if task.attempt_count == 0:
            task.attempt_count = 1
            session.add(
                AssessmentTaskAttempt(
                    task_id=task.id,
                    attempt_number=1,
                    status="failed",
                    started_at=now,
                    completed_at=now,
                    input_fingerprint=task.input_fingerprint,
                    error_category=error_category,
                    error_summary=bounded_summary,
                )
            )
            return task

        attempt = await session.scalar(
            select(AssessmentTaskAttempt).where(
                AssessmentTaskAttempt.task_id == task.id,
                AssessmentTaskAttempt.attempt_number == task.attempt_count,
            )
        )
        if attempt is not None:
            attempt.status = "failed"
            attempt.error_category = error_category
            attempt.error_summary = bounded_summary
            attempt.completed_at = now
        return task

    async def get_run(
        self,
        session: AsyncSession,
        run_id: str | UUID,
    ) -> AssessmentRun | None:
        try:
            identifier = run_id if isinstance(run_id, UUID) else UUID(run_id)
        except ValueError as exc:
            raise ValueError("run_id must be a UUID") from exc
        return await session.get(AssessmentRun, identifier)

    async def get_effective_run(
        self,
        session: AsyncSession,
        *,
        event_id: str,
    ) -> AssessmentRun | None:
        event = await session.get(EarthquakeEvent, UUID(event_id))
        if event is None or event.effective_assessment_run_id is None:
            return None
        return await session.get(AssessmentRun, event.effective_assessment_run_id)

    async def mark_deadline_exceeded(
        self,
        session: AsyncSession,
        run_id: str | UUID,
        observed_at: datetime,
    ) -> bool:
        try:
            identifier = run_id if isinstance(run_id, UUID) else UUID(run_id)
        except ValueError as exc:
            raise ValueError("run_id must be a UUID") from exc
        run = await session.get(AssessmentRun, identifier, with_for_update=True)
        if run is None:
            raise LookupError("assessment run not found")
        if run.deadline_exceeded_at is not None:
            return False
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        observed_at = observed_at.astimezone(UTC)
        if observed_at <= run.deadline_at:
            return False
        run.deadline_exceeded_at = observed_at
        return True

    async def skip_deferred_tasks(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> None:
        tasks = (
            await session.scalars(
                select(AssessmentTask)
                .where(
                    AssessmentTask.run_id == run_id,
                    AssessmentTask.status == "pending",
                )
                .with_for_update()
            )
        ).all()
        for task in tasks:
            task.status = "skipped"
            task.completed_at = datetime.now(UTC)
            task.result = {"reason": "out_of_phase_scope"}

    async def start_run(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> AssessmentRun:
        run = await session.get(AssessmentRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError("assessment run not found")
        if run.status == "running":
            return run
        if run.status != "pending":
            raise ValueError("run must be pending before it can start")
        run.status = "running"
        run.started_at = datetime.now(UTC)
        run.last_error = None
        return run

    async def complete_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        algorithm_bundle_version: str,
    ) -> AssessmentRun:
        # Lock the event before the run so run mutation and event-run creation use
        # the same event-first lock order.
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        event = await session.get(EarthquakeEvent, run_event_id, with_for_update=True)
        run = await session.get(AssessmentRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError("assessment run not found")
        if run.status == "completed":
            if run.algorithm_bundle_version != algorithm_bundle_version:
                raise ValueError("run algorithm bundle version changed")
            return run
        if run.status != "running":
            raise ValueError("run must be running before it can complete")
        # Phase-specific policy: model and fusion are required; instrument is
        # deliberately optional because a degraded or unavailable instrument can
        # still produce a valid model-only fusion.
        required_tasks = (
            await session.scalars(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key.in_(
                        ("intensity.model", "intensity.fusion")
                    ),
                )
            )
        ).all()
        if {
            task.task_key for task in required_tasks if task.status == "succeeded"
        } != {"intensity.model", "intensity.fusion"}:
            raise ValueError("required intensity tasks have not succeeded")
        now = datetime.now(UTC)
        run.status = "completed"
        run.completed_at = now
        started_at = run.started_at or run.created_at
        run.duration_ms = int((now - started_at).total_seconds() * 1000)
        run.algorithm_bundle_version = algorithm_bundle_version
        if event is not None and event.latest_assessment_run_id == run.id:
            event.effective_assessment_run_id = run.id
        return run

    async def fail_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        error_summary: str,
    ) -> AssessmentRun:
        run = await session.get(AssessmentRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError("assessment run not found")
        if run.status in {"completed", "failed"}:
            raise ValueError("terminal run cannot be overwritten")
        if run.status != "running":
            raise ValueError("run must be running before it can fail")
        run.status = "failed"
        run.completed_at = datetime.now(UTC)
        run.last_error = error_summary[:2000]
        return run

    async def record_run_failure_audit(
        self,
        session: AsyncSession,
        run_id: UUID,
        error_summary: str,
    ) -> AssessmentRun | None:
        run = await session.get(AssessmentRun, run_id, with_for_update=True)
        if run is None:
            return None
        if run.status in {"completed", "failed"}:
            return run
        run.status = "failed"
        run.completed_at = datetime.now(UTC)
        run.last_error = error_summary[:2000]
        return run

    async def mark_superseded(
        self,
        session: AsyncSession,
        run_id: UUID,
        successor_run_id: UUID,
    ) -> None:
        run = await session.get(AssessmentRun, run_id, with_for_update=True)
        if run is None:
            raise LookupError("assessment run not found")
        run.superseded_by_run_id = successor_run_id
        run.superseded_at = datetime.now(UTC)

    async def count_tasks(self, session: AsyncSession, run_id: object) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(AssessmentTask)
            .where(AssessmentTask.run_id == run_id)
        )
        return int(count or 0)

    async def get_current_run(
        self,
        session: AsyncSession,
        *,
        event_id: str,
    ) -> AssessmentRun | None:
        try:
            event_uuid = UUID(event_id)
        except ValueError as exc:
            raise ValueError("event_id must be a UUID") from exc
        return await session.scalar(
            select(AssessmentRun)
            .where(AssessmentRun.event_id == event_uuid)
            .order_by(
                AssessmentRun.run_no.desc(),
                AssessmentRun.created_at.desc(),
                AssessmentRun.id.desc(),
            )
            .limit(1)
        )

    async def list_tasks(
        self,
        session: AsyncSession,
        run_id: object,
    ) -> list[AssessmentTask]:
        tasks = await session.scalars(
            select(AssessmentTask)
            .where(AssessmentTask.run_id == run_id)
            .order_by(AssessmentTask.sequence, AssessmentTask.id)
        )
        return list(tasks.all())


def _validate_trigger_identity(
    event: EarthquakeEvent,
    revision: EarthquakeRevision,
    outbox: EventLifecycleOutbox,
) -> None:
    if revision.event_id != event.id:
        raise ValueError("revision does not belong to event")
    if outbox.event_id != event.id:
        raise ValueError("outbox does not belong to event")
    if outbox.revision_id != revision.id:
        raise ValueError("outbox does not belong to revision")
    if outbox.trigger_type != "assessment.requested":
        raise ValueError("outbox is not an assessment request")
