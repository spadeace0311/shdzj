from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.plan import AssessmentPlanBuilder
from app.events.domain import EventKind
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox


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
        deadline_seconds = max(task.deadline_offset_seconds for task in plan)
        run = AssessmentRun(
            event_id=canonical.id,
            revision_id=revision.id,
            outbox_id=outbox.id,
            run_no=int(latest_run_no or 0) + 1,
            trigger_reason=outbox.trigger_reason,
            status="pending",
            priority=max(task.priority for task in plan),
            t1_at=canonical.t1_at,
            deadline_at=canonical.t1_at + timedelta(seconds=deadline_seconds),
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
                    deadline_at=canonical.t1_at
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
