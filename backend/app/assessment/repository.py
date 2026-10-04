from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.plan import AssessmentPlanBuilder
from app.config import settings
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.events.domain import EventKind
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox
from app.intensity.models import AssessmentTaskAttempt, IntensityFieldProduct
from app.loss.domain import LossProductStatus
from app.loss.models import LossProduct


class AssessmentRepository:
    def __init__(
        self,
        plan_builder: AssessmentPlanBuilder | None = None,
        data_asset_snapshot_service: DataAssetSnapshotService | None = None,
    ) -> None:
        self._plan_builder = plan_builder or AssessmentPlanBuilder()
        self._data_asset_snapshot_service = (
            data_asset_snapshot_service or DataAssetSnapshotService()
        )

    async def ensure_run_and_tasks(
        self,
        session: AsyncSession,
        *,
        event: EarthquakeEvent,
        revision: EarthquakeRevision,
        outbox: EventLifecycleOutbox,
    ) -> AssessmentRun:
        _validate_trigger_identity(event, revision, outbox)
        canonical = await session.get(EarthquakeEvent, event.id, with_for_update=True)
        if canonical is None:
            raise LookupError(f"event not found: {event.id}")
        basis_at = revision.ingested_at
        if basis_at is None:
            raise ValueError("assessment revision ingested_at is required")

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"assessment-outbox:{outbox.id}"},
        )

        existing = await session.scalar(
            select(AssessmentRun)
            .where(AssessmentRun.outbox_id == outbox.id)
            .with_for_update()
        )
        if existing is not None:
            return existing

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
        deadline_at = basis_at + timedelta(seconds=300)
        snapshot_t1 = canonical.t1_at.isoformat() if canonical.t1_at else None
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
                "t1_at": snapshot_t1,
                "response_rule_version": revision.response_rule_version,
                "region_boundary_version": revision.region_boundary_version,
            },
            created_at=outbox.created_at,
            updated_at=outbox.created_at,
        )
        session.add(run)
        await session.flush()

        # The event's current revision is authoritative. A delayed outbox for
        # a revision that is no longer current creates an historical run only.
        if canonical.current_revision_id == revision.id:
            canonical.latest_assessment_run_id = run.id
            previous_runs = (
                await session.scalars(
                    select(AssessmentRun)
                    .where(
                        AssessmentRun.event_id == canonical.id,
                        AssessmentRun.id != run.id,
                        AssessmentRun.superseded_at.is_(None),
                    )
                    .order_by(AssessmentRun.id)
                    .with_for_update()
                )
            ).all()
            for previous_run in previous_runs:
                previous_run.superseded_by_run_id = run.id
                previous_run.superseded_at = datetime.now(UTC)
        elif canonical.latest_assessment_run_id is not None:
            run.superseded_by_run_id = canonical.latest_assessment_run_id
            run.superseded_at = datetime.now(UTC)

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

        snapshot_result = await self._data_asset_snapshot_service.capture_required_assets(
            session,
            run_id=run.id,
            region_id=settings.data_asset_region_id,
            strict=False,
        )
        run.data_asset_snapshot_fingerprint = snapshot_result.fingerprint
        run.data_asset_snapshot_result = {
            "snapshot_count": snapshot_result.snapshot_count,
            "missing_required": list(snapshot_result.missing_required),
        }
        run.snapshot = {
            **dict(run.snapshot),
            "region_id": settings.data_asset_region_id,
            "data_asset_snapshot": run.data_asset_snapshot_result,
        }
        return run

    async def ensure_run_from_outbox(
        self,
        session: AsyncSession,
        *,
        event_id: str | UUID,
        revision_id: str | UUID,
        outbox_id: str | UUID,
    ) -> AssessmentRun:
        try:
            event_uuid = _coerce_uuid(event_id, "event_id")
            revision_uuid = _coerce_uuid(revision_id, "revision_id")
            outbox_uuid = _coerce_uuid(outbox_id, "outbox_id")
        except (TypeError, ValueError) as exc:
            raise ValueError("assessment trigger identifiers must be UUIDs") from exc

        outbox = await session.get(EventLifecycleOutbox, outbox_uuid)
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
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        _, run = await self._lock_event_then_run(
            session,
            run_event_id,
            run_id,
        )
        if run is None:
            raise LookupError("assessment run not found")
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

    async def mark_artifact_production_launched(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> AssessmentTask | None:
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        _, run = await self._lock_event_then_run(
            session,
            run_event_id,
            run_id,
        )
        if run is None:
            raise LookupError("assessment run not found")
        task = await session.scalar(
            select(AssessmentTask)
            .where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == "artifact.production",
            )
            .with_for_update()
        )
        if task is None:
            return None
        if task.status == "succeeded":
            return task
        if task.status not in {"pending", "running"}:
            raise ValueError(
                "artifact.production task is already terminal and cannot "
                "be marked launched"
            )

        algorithm_version = "artifact-production-v1"
        input_fingerprint = "artifact-production-child-started"
        await self.start_task(
            session,
            run_id,
            "artifact.production",
            algorithm_version,
            input_fingerprint,
        )
        await self.complete_task(
            session,
            task.id,
            input_fingerprint,
            {
                "production_run_id": None,
                "status": "launched",
            },
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
        deadline_evidence = (
            dict(task.result or {}).get("deadline_exceeded_at")
            if task.status != "succeeded"
            else None
        )
        task.status = "succeeded"
        task.output_checksum = output_checksum
        task.result = dict(result)
        if deadline_evidence is not None:
            task.result["deadline_exceeded_at"] = deadline_evidence
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
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == identifier)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        _, run = await self._lock_event_then_run(
            session,
            run_event_id,
            identifier,
        )
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
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        event, run = await self._lock_event_then_run(
            session,
            run_event_id,
            run_id,
        )
        if run is None:
            raise LookupError("assessment run not found")
        if (
            event.current_revision_id != run.revision_id
            or run.superseded_at is not None
        ):
            await self.terminalize_superseded_run(session, run)
            return run
        if run.status in {"completed", "running"}:
            return run
        if run.status != "pending":
            raise ValueError("run must be pending before it can start")
        run.status = "running"
        run.started_at = datetime.now(UTC)
        run.last_error = None
        return run

    async def terminalize_superseded_run(
        self,
        session: AsyncSession,
        run: AssessmentRun,
    ) -> None:
        now = datetime.now(UTC)
        if run.status not in {"completed", "failed"}:
            run.status = "failed"
            run.completed_at = now
            run.last_error = "assessment revision was superseded before production launch"
        if run.superseded_at is None:
            run.superseded_at = now
        tasks = (
            await session.scalars(
                select(AssessmentTask)
                .where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.status.in_(("pending", "running")),
                )
                .with_for_update()
            )
        ).all()
        for task in tasks:
            task.status = "failed"
            task.completed_at = now
            task.last_error = run.last_error
        await session.flush()

    async def complete_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        algorithm_bundle_version: str,
    ) -> AssessmentRun:
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        event, run = await self._lock_event_then_run(
            session,
            run_event_id,
            run_id,
        )
        if run is None:
            raise LookupError("assessment run not found")
        if run.status == "completed":
            if run.algorithm_bundle_version != algorithm_bundle_version:
                raise ValueError("run algorithm bundle version changed")
            if event is not None and event.latest_assessment_run_id == run.id:
                await self._publish_products(session, run.id)
            return run
        if run.status != "running":
            raise ValueError("run must be running before it can complete")
        # Phase-specific policy: instrument is deliberately optional because a
        # degraded or unavailable instrument can still produce a valid
        # model-only fusion. Every intensity prerequisite and loss product must
        # succeed before an assessment can complete.
        required_task_keys = {
            "intensity.model",
            "intensity.fusion",
            "loss.population",
            "loss.buildings",
            "loss.casualties",
            "loss.economic",
            "loss.resources",
            "loss.validate",
        }
        required_tasks = (
            await session.scalars(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key.in_(tuple(required_task_keys)),
                )
            )
        ).all()
        if {
            task.task_key for task in required_tasks if task.status == "succeeded"
        } != required_task_keys:
            raise ValueError("required assessment tasks have not succeeded")
        validation_product = await session.scalar(
            select(LossProduct).where(
                LossProduct.run_id == run.id,
                LossProduct.product_type == "validation",
            )
        )
        if (
            validation_product is not None
            and validation_product.status != LossProductStatus.COMPLETE.value
        ):
            raise ValueError("loss validation product is not complete")
        now = datetime.now(UTC)
        run.status = "completed"
        run.completed_at = now
        started_at = run.started_at or run.created_at
        run.duration_ms = int((now - started_at).total_seconds() * 1000)
        run.algorithm_bundle_version = algorithm_bundle_version
        if event is not None and event.latest_assessment_run_id == run.id:
            event.effective_assessment_run_id = run.id
            await self._publish_products(session, run.id)
        return run

    async def fail_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        error_summary: str,
    ) -> AssessmentRun:
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        _, run = await self._lock_event_then_run(
            session,
            run_event_id,
            run_id,
        )
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
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            return None
        _, run = await self._lock_event_then_run(
            session,
            run_event_id,
            run_id,
        )
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
        run_event_id = await session.scalar(
            select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
        )
        if run_event_id is None:
            raise LookupError("assessment run not found")
        _, run = await self._lock_event_then_run(
            session,
            run_event_id,
            run_id,
        )
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
        event = await session.get(EarthquakeEvent, event_uuid)
        if event is None or event.latest_assessment_run_id is None:
            return None
        return await session.get(AssessmentRun, event.latest_assessment_run_id)

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

    async def list_task_deadlines(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> list[datetime]:
        deadlines = (
            await session.scalars(
                select(AssessmentTask.deadline_at)
                .where(AssessmentTask.run_id == run_id)
                .distinct()
                .order_by(AssessmentTask.deadline_at)
            )
        ).all()
        return list(deadlines)

    async def observe_task_deadlines(
        self,
        session: AsyncSession,
        run_id: UUID,
        observed_at: datetime,
    ) -> list[str]:
        observed_at = _normalize_utc(observed_at, "observed_at")
        run = await session.get(AssessmentRun, run_id)
        if run is None:
            raise LookupError("assessment run not found")

        tasks = (
            await session.scalars(
                select(AssessmentTask)
                .where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.status.in_(("pending", "running")),
                    AssessmentTask.deadline_at <= observed_at,
                )
                .order_by(AssessmentTask.sequence, AssessmentTask.id)
                .with_for_update(skip_locked=True)
            )
        ).all()
        warned: list[str] = []
        for task in tasks:
            existing_result = dict(task.result or {})
            if "deadline_exceeded_at" in existing_result:
                continue
            existing_result["deadline_exceeded_at"] = observed_at.isoformat()
            task.result = existing_result
            warned.append(task.task_key)
        return warned

    async def reconcile_timeouts(
        self,
        session: AsyncSession,
        *,
        safety_timeout_seconds: int,
        observed_at: datetime,
    ) -> list[UUID]:
        if safety_timeout_seconds <= 0:
            raise ValueError("safety_timeout_seconds must be positive")
        observed_at = _normalize_utc(observed_at, "observed_at")
        cutoff = observed_at - timedelta(seconds=safety_timeout_seconds)
        run_ids = (
            await session.scalars(
                select(AssessmentRun.id)
                .where(
                    AssessmentRun.status == "running",
                    or_(
                        AssessmentRun.started_at <= cutoff,
                        and_(
                            AssessmentRun.started_at.is_(None),
                            AssessmentRun.created_at <= cutoff,
                        ),
                    ),
                )
                .order_by(AssessmentRun.id)
            )
        ).all()
        failed_ids: list[UUID] = []
        for run_id in run_ids:
            run_event_id = await session.scalar(
                select(AssessmentRun.event_id).where(AssessmentRun.id == run_id)
            )
            if run_event_id is None:
                continue
            _, run = await self._lock_event_then_run(
                session,
                run_event_id,
                run_id,
            )
            if (
                run is None
                or run.status != "running"
            ):
                continue
            effective_started_at = run.started_at or run.created_at
            if effective_started_at > cutoff:
                continue
            self._apply_timeout_failure(
                run,
                observed_at,
                safety_timeout_seconds,
            )
            tasks = (
                await session.scalars(
                    select(AssessmentTask)
                    .where(
                        AssessmentTask.run_id == run.id,
                        AssessmentTask.status.in_(("pending", "running")),
                    )
                    .with_for_update()
                )
            ).all()
            for task in tasks:
                task.status = "failed"
                task.completed_at = observed_at
                task.last_error = run.last_error
            failed_ids.append(run_id)
        return failed_ids

    async def _lock_event_then_run(
        self,
        session: AsyncSession,
        event_id: object,
        run_id: object | None = None,
    ) -> tuple[EarthquakeEvent, AssessmentRun | None]:
        event = await session.get(
            EarthquakeEvent,
            event_id,
            with_for_update=True,
        )
        if event is None:
            raise LookupError("assessment event not found")
        if run_id is None:
            return event, None
        run = await session.get(
            AssessmentRun,
            run_id,
            with_for_update=True,
        )
        return event, run

    async def _publish_products(
        self,
        session: AsyncSession,
        run_id: UUID,
    ) -> None:
        now = datetime.now(UTC)
        products = (
            await session.scalars(
                select(IntensityFieldProduct).where(
                    IntensityFieldProduct.run_id == run_id
                )
            )
        ).all()
        for product in products:
            if product.published_at is None:
                product.published_at = now
        loss_products = (
            await session.scalars(
                select(LossProduct).where(
                    LossProduct.run_id == run_id,
                    LossProduct.status == LossProductStatus.COMPLETE.value,
                )
            )
        ).all()
        for product in loss_products:
            if product.published_at is None:
                product.published_at = now

    @staticmethod
    def _apply_timeout_failure(
        run: AssessmentRun,
        observed_at: datetime,
        safety_timeout_seconds: int,
    ) -> None:
        if run.deadline_exceeded_at is None and observed_at > run.deadline_at:
            run.deadline_exceeded_at = observed_at
        started_at = run.started_at or run.created_at
        run.status = "failed"
        run.completed_at = observed_at
        run.duration_ms = int((observed_at - started_at).total_seconds() * 1000)
        run.last_error = (
            "assessment workflow safety timeout exceeded after "
            f"{safety_timeout_seconds} seconds"
        )


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


def _coerce_uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _normalize_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information")
    return value.astimezone(UTC)
