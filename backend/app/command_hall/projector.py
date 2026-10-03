from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactPublication, GeneratedArtifact
from app.auth.models import User
from app.collaboration.models import (
    CommandHallAlertProjection,
    CommandHallEventProjection,
    CommandHallGroupProjection,
    NotificationDelivery,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupDefinition,
    WorkgroupTask,
    WorkgroupTaskContributor,
)
from app.collaboration.roster import EventRosterGroup, RosterService
from app.events.models import EarthquakeEvent, EarthquakeRevision


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    event_id: uuid.UUID
    event_projection_id: uuid.UUID
    group_projection_ids: tuple[uuid.UUID, ...]
    alert_count: int
    projection_version: int

    @property
    def group_projection_count(self) -> int:
        return len(self.group_projection_ids)


class CommandHallProjector:
    def __init__(self, roster_service: RosterService | None = None) -> None:
        self._roster_service = roster_service or RosterService()

    async def refresh_event(
        self,
        session: AsyncSession,
        event_id: object,
    ) -> ProjectionResult:
        event_uuid = _coerce_uuid(event_id, "event_id")
        event = await session.get(
            EarthquakeEvent,
            event_uuid,
            with_for_update=True,
        )
        if event is None:
            raise LookupError("event_not_found")

        now = datetime.now(UTC)
        revision = await self._current_revision(session, event)
        definitions = await self._roster_service.list_workgroups(session)
        rosters = await self._roster_service.event_rosters(session, event.id)
        rosters_by_code = {group.code: group for group in rosters}

        tasks = list(
            await session.scalars(
                select(WorkgroupTask)
                .where(WorkgroupTask.event_id == event.id)
                .order_by(WorkgroupTask.priority.desc(), WorkgroupTask.created_at)
            )
        )
        task_ids = [task.id for task in tasks]
        task_envelopes = await self._task_envelopes(session, task_ids)

        event_task_counts = _event_task_counts(
            [task_envelopes[task.id] for task in tasks]
        )
        artifact_summary = await self._artifact_summary(session, event.id)
        alerts = await self._build_alerts(
            session,
            event,
            tasks,
            task_envelopes,
            rosters_by_code,
        )
        alert_summary = _alert_summary(alerts)

        event_projection = await session.scalar(
            select(CommandHallEventProjection)
            .where(CommandHallEventProjection.event_id == event.id)
            .with_for_update()
        )
        next_version = (
            event_projection.projection_version + 1
            if event_projection is not None
            else 1
        )
        event_snapshot = _event_snapshot(event, revision)
        if event_projection is None:
            event_projection = CommandHallEventProjection(
                event_id=event.id,
                event_snapshot=event_snapshot,
                task_counts=event_task_counts,
                artifact_summary=artifact_summary,
                alert_summary=alert_summary,
                projection_version=next_version,
            )
            session.add(event_projection)
            await session.flush()
        else:
            event_projection.event_snapshot = event_snapshot
            event_projection.task_counts = event_task_counts
            event_projection.artifact_summary = artifact_summary
            event_projection.alert_summary = alert_summary
            event_projection.projection_version = next_version
            event_projection.updated_at = now

        group_projection_ids: list[uuid.UUID] = []
        for definition in definitions:
            current = await session.scalar(
                select(CommandHallGroupProjection)
                .where(
                    CommandHallGroupProjection.event_id == event.id,
                    CommandHallGroupProjection.workgroup_code
                    == definition.code,
                    CommandHallGroupProjection.is_current.is_(True),
                )
                .with_for_update()
            )
            if current is not None:
                current.is_current = False
                current.updated_at = now

            group_tasks = [
                task_envelopes[task.id]
                for task in tasks
                if task.workgroup_code == definition.code
            ]
            group_snapshot = _group_snapshot(
                event,
                definition,
                rosters_by_code.get(definition.code),
                group_tasks,
            )
            group_alerts = [
                alert
                for alert in alerts
                if alert.get("workgroup_code") == definition.code
            ]
            group_projection = CommandHallGroupProjection(
                event_id=event.id,
                workgroup_code=definition.code,
                group_snapshot=group_snapshot,
                task_counts=_group_task_counts(group_tasks),
                latest_deliverable=_latest_deliverable(group_tasks),
                alert_summary=_alert_summary(group_alerts),
                projection_version=next_version,
                is_current=True,
            )
            session.add(group_projection)
            await session.flush()
            group_projection_ids.append(group_projection.id)

        await session.execute(
            delete(CommandHallAlertProjection).where(
                CommandHallAlertProjection.event_id == event.id
            )
        )
        for alert in alerts:
            session.add(
                CommandHallAlertProjection(
                    event_id=event.id,
                    workgroup_code=alert.get("workgroup_code"),
                    task_id=_optional_uuid(alert.get("task_id")),
                    alert_key=alert["alert_key"],
                    alert_type=alert["alert_type"],
                    severity=alert["severity"],
                    status="open",
                    title=alert["title"],
                    detail=_json_value(alert.get("detail") or {}),
                    projection_version=next_version,
                    updated_at=now,
                )
            )
        await session.flush()

        return ProjectionResult(
            event_id=event.id,
            event_projection_id=event_projection.id,
            group_projection_ids=tuple(group_projection_ids),
            alert_count=len(alerts),
            projection_version=next_version,
        )

    async def _current_revision(
        self,
        session: AsyncSession,
        event: EarthquakeEvent,
    ) -> EarthquakeRevision | None:
        if event.current_revision_id is not None:
            revision = await session.get(
                EarthquakeRevision,
                event.current_revision_id,
            )
            if revision is not None and revision.event_id == event.id:
                return revision
        return await session.scalar(
            select(EarthquakeRevision)
            .where(
                EarthquakeRevision.event_id == event.id,
                EarthquakeRevision.is_current.is_(True),
            )
            .order_by(EarthquakeRevision.revision_no.desc())
            .limit(1)
        )

    async def _task_envelopes(
        self,
        session: AsyncSession,
        task_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, dict[str, Any]]:
        if not task_ids:
            return {}

        tasks = list(
            await session.scalars(
                select(WorkgroupTask).where(WorkgroupTask.id.in_(task_ids))
            )
        )
        contributor_rows = (
            await session.execute(
                select(WorkgroupTaskContributor, User)
                .join(User, User.id == WorkgroupTaskContributor.user_id)
                .where(WorkgroupTaskContributor.task_id.in_(task_ids))
                .order_by(WorkgroupTaskContributor.first_contributed_at)
            )
        ).all()
        contributors: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
        for contributor, user in contributor_rows:
            contributors[contributor.task_id].append(
                {
                    "user_id": str(contributor.user_id),
                    "username": user.username,
                    "contribution_count": contributor.contribution_count,
                    "first_contributed_at": _json_value(
                        contributor.first_contributed_at
                    ),
                    "last_contributed_at": _json_value(
                        contributor.last_contributed_at
                    ),
                }
            )

        deliverables = list(
            await session.scalars(
                select(TaskDeliverable).where(
                    TaskDeliverable.task_id.in_(task_ids)
                )
            )
        )
        deliverable_ids = [item.id for item in deliverables]
        deliverables_by_task: dict[uuid.UUID, list[TaskDeliverable]] = (
            defaultdict(list)
        )
        for deliverable in deliverables:
            deliverables_by_task[deliverable.task_id].append(deliverable)

        versions: dict[uuid.UUID, list[TaskDeliverableVersion]] = defaultdict(list)
        if deliverable_ids:
            version_rows = await session.scalars(
                select(TaskDeliverableVersion)
                .where(TaskDeliverableVersion.deliverable_id.in_(deliverable_ids))
                .order_by(TaskDeliverableVersion.version_no)
            )
            for version in version_rows:
                versions[version.deliverable_id].append(version)

        publications_by_deliverable: dict[
            uuid.UUID, TaskDeliverablePublication
        ] = {}
        if deliverable_ids:
            publication_rows = await session.scalars(
                select(TaskDeliverablePublication)
                .where(
                    TaskDeliverablePublication.deliverable_id.in_(
                        deliverable_ids
                    ),
                    TaskDeliverablePublication.superseded_at.is_(None),
                )
                .order_by(TaskDeliverablePublication.published_at.desc())
            )
            for publication in publication_rows:
                publications_by_deliverable.setdefault(
                    publication.deliverable_id,
                    publication,
                )

        notification_rows = await session.scalars(
            select(NotificationDelivery).where(
                NotificationDelivery.task_id.in_(task_ids)
            )
        )
        notifications_by_task: dict[uuid.UUID, list[NotificationDelivery]] = (
            defaultdict(list)
        )
        for notification in notification_rows:
            notifications_by_task[notification.task_id].append(notification)

        result: dict[uuid.UUID, dict[str, Any]] = {}
        for task in tasks:
            task_deliverables = deliverables_by_task[task.id]
            required_count = sum(
                1 for item in task_deliverables if item.is_required
            )
            satisfied_count = sum(
                1
                for item in task_deliverables
                if item.is_required
                and (
                    item.id in publications_by_deliverable
                    or bool(versions[item.id])
                )
            )
            dual_version_deliverable_count = sum(
                1 for item in task_deliverables if len(versions[item.id]) > 1
            )
            current_publication = publications_by_deliverable.get(
                task_deliverables[0].id
            ) if task_deliverables else None
            latest = _latest_deliverable_for_task(
                task_deliverables,
                versions,
                publications_by_deliverable,
            )
            task_notifications = notifications_by_task[task.id]
            result[task.id] = {
                "id": str(task.id),
                "event_id": str(task.event_id),
                "workgroup_code": task.workgroup_code,
                "task_code": task.task_code,
                "title": task.title,
                "instruction": task.instruction,
                "status": task.status,
                "timeliness_state": task.timeliness_state,
                "phase_code": task.phase_code,
                "priority": task.priority,
                "source_type": task.source_type,
                "source_ref": task.source_ref,
                "activated_at": _json_value(task.activated_at),
                "due_at": _json_value(task.due_at),
                "completed_at": _json_value(task.completed_at),
                "closed_at": _json_value(task.closed_at),
                "row_version": task.row_version,
                "created_at": _json_value(task.created_at),
                "updated_at": _json_value(task.updated_at),
                "contributors": contributors[task.id],
                "deliverable_count": len(task_deliverables),
                "required_deliverable_count": required_count,
                "satisfied_required_deliverable_count": satisfied_count,
                "dual_version_deliverable_count": (
                    dual_version_deliverable_count
                ),
                "latest_deliverable": latest,
                "current_publication": _publication_summary(
                    current_publication
                ),
                "notification_status_counts": _notification_counts(
                    task_notifications
                ),
            }
        return result

    async def _artifact_summary(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> dict[str, Any]:
        rows = (
            await session.execute(
                select(ArtifactPublication, GeneratedArtifact)
                .join(
                    GeneratedArtifact,
                    GeneratedArtifact.id == ArtifactPublication.artifact_id,
                )
                .where(
                    ArtifactPublication.event_id == event_id,
                    ArtifactPublication.superseded_at.is_(None),
                )
                .order_by(ArtifactPublication.published_at.desc())
            )
        ).all()
        artifacts = [
            {
                "publication_id": str(publication.id),
                "artifact_id": str(artifact.id),
                "artifact_key": artifact.artifact_key,
                "output_profile": artifact.output_profile,
                "artifact_version": artifact.artifact_version,
                "status": artifact.status,
                "quality_grade": artifact.quality_grade,
                "production_mode": publication.production_mode,
                "publication_mode": artifact.publication_mode,
                "is_forced": publication.is_forced,
                "published_at": _json_value(publication.published_at),
                "file_name": artifact.file_name,
                "format": artifact.format,
            }
            for publication, artifact in rows
        ]
        status_counts = {
            "complete": 0,
            "degraded": 0,
            "failed": 0,
        }
        for artifact in artifacts:
            status = artifact["status"]
            if status in status_counts:
                status_counts[status] += 1
        return {
            "published_count": len(artifacts),
            "status_counts": status_counts,
            "production_modes": _counter(
                item["production_mode"] for item in artifacts
            ),
            "latest_artifacts": artifacts[:50],
        }

    async def _build_alerts(
        self,
        session: AsyncSession,
        event: EarthquakeEvent,
        tasks: list[WorkgroupTask],
        task_envelopes: dict[uuid.UUID, dict[str, Any]],
        rosters_by_code: dict[str, EventRosterGroup],
    ) -> list[dict[str, Any]]:
        del event
        alerts: list[dict[str, Any]] = []
        for task in tasks:
            envelope = task_envelopes[task.id]
            if task.status == "failed":
                alerts.append(
                    _alert(
                        alert_type="task.failed",
                        severity="critical",
                        workgroup_code=task.workgroup_code,
                        task_id=task.id,
                        title=f"任务失败: {task.title}",
                        detail={"task_id": str(task.id)},
                    )
                )
            if task.timeliness_state == "overdue" and not _is_terminal(
                task.status
            ):
                alerts.append(
                    _alert(
                        alert_type="task.overdue",
                        severity="warning",
                        workgroup_code=task.workgroup_code,
                        task_id=task.id,
                        title=f"任务超时: {task.title}",
                        detail={"task_id": str(task.id)},
                    )
                )
            if (
                not _is_terminal(task.status)
                and envelope["required_deliverable_count"]
                > envelope["satisfied_required_deliverable_count"]
            ):
                alerts.append(
                    _alert(
                        alert_type="deliverable.missing",
                        severity="warning",
                        workgroup_code=task.workgroup_code,
                        task_id=task.id,
                        title=f"必需成果缺失: {task.title}",
                        detail={"task_id": str(task.id)},
                    )
                )
            roster = rosters_by_code.get(task.workgroup_code)
            if (
                not _is_terminal(task.status)
                and roster is not None
                and roster.authority is None
            ):
                alerts.append(
                    _alert(
                        alert_type="authority.missing",
                        severity="warning",
                        workgroup_code=task.workgroup_code,
                        task_id=task.id,
                        title=f"确认权限缺失: {task.title}",
                        detail={"task_id": str(task.id)},
                    )
                )

        notification_rows = await session.scalars(
            select(NotificationDelivery).where(
                NotificationDelivery.task_id.in_([task.id for task in tasks]),
                NotificationDelivery.status == "failed",
            )
        )
        for notification in notification_rows:
            alerts.append(
                _alert(
                    alert_type="notification.failed",
                    severity="warning",
                    workgroup_code=None,
                    task_id=notification.task_id,
                    title="通知发送失败",
                    detail={
                        "notification_id": str(notification.id),
                        "channel": notification.channel,
                    },
                )
            )
        return alerts


def _event_snapshot(
    event: EarthquakeEvent,
    revision: EarthquakeRevision | None,
) -> dict[str, Any]:
    return _json_value(
        {
            "event_id": str(event.id),
            "source": event.source,
            "canonical_source_id": event.canonical_source_id,
            "event_type": event.event_type,
            "event_kind": event.event_type,
            "lifecycle_state": event.lifecycle_state,
            "origin_time": event.origin_time,
            "longitude": event.longitude,
            "latitude": event.latitude,
            "depth_km": event.depth_km,
            "magnitude": event.magnitude,
            "place": event.place,
            "institutional_level": event.institutional_level,
            "service_level": event.service_level,
            "response_suggestion": event.response_suggestion,
            "response_rule_version": event.response_rule_version,
            "t1_at": event.t1_at,
            "current_revision": (
                {
                    "id": str(revision.id),
                    "revision_no": revision.revision_no,
                    "revision_kind": revision.revision_kind,
                    "ingested_at": revision.ingested_at,
                }
                if revision is not None
                else None
            ),
            "updated_at": event.updated_at,
        }
    )


def _group_snapshot(
    event: EarthquakeEvent,
    definition: WorkgroupDefinition,
    roster: EventRosterGroup | None,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    attendance = [
        {
            "user_id": str(item.user_id),
            "state": item.state,
            "duty_role_in_snapshot": item.duty_role_in_snapshot,
            "deputy_order_in_snapshot": item.deputy_order_in_snapshot,
            "checked_in_at": _json_value(item.checked_in_at),
            "checked_out_at": _json_value(item.checked_out_at),
            "updated_by": item.updated_by,
        }
        for item in (roster.attendance if roster is not None else ())
    ]
    return _json_value(
        {
            "event_id": str(event.id),
            "workgroup_code": definition.code,
            "name": definition.name,
            "display_order": definition.display_order,
            "roster_version": roster.roster_version if roster is not None else None,
            "roster_fingerprint": (
                roster.roster_fingerprint if roster is not None else None
            ),
            "leader": roster.leader if roster is not None else None,
            "deputies": list(roster.deputies) if roster is not None else [],
            "members": list(roster.members) if roster is not None else [],
            "attendance": attendance,
            "confirming_authority": (
                {
                    "user_id": str(roster.authority.user_id),
                    "role": roster.authority.role.value,
                    "deputy_order": roster.authority.deputy_order,
                }
                if roster is not None and roster.authority is not None
                else None
            ),
            "tasks": tasks,
            "task_count": len(tasks),
        }
    )


def _event_task_counts(
    tasks: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {
        "total": len(tasks),
        "pending": 0,
        "in_progress": 0,
        "pending_review": 0,
        "completed": 0,
        "not_required": 0,
        "failed": 0,
        "overdue": 0,
        "at_risk": 0,
        "dual_version_count": 0,
    }
    for task in tasks:
        status = task["status"]
        if status in counts:
            counts[status] += 1
        if task["timeliness_state"] == "overdue" and not _is_terminal(
            task["status"]
        ):
            counts["overdue"] += 1
        elif task["timeliness_state"] == "at_risk":
            counts["at_risk"] += 1
        counts["dual_version_count"] += int(
            task.get("dual_version_deliverable_count", 0)
        )
    return counts


def _group_task_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    return _event_task_counts(tasks)


def _latest_deliverable(
    tasks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [
        task.get("latest_deliverable")
        for task in tasks
        if task.get("latest_deliverable") is not None
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: str(item.get("published_at") or ""),
    )


def _latest_deliverable_for_task(
    deliverables: list[TaskDeliverable],
    versions: dict[uuid.UUID, list[TaskDeliverableVersion]],
    publications: dict[uuid.UUID, TaskDeliverablePublication],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for deliverable in deliverables:
        publication = publications.get(deliverable.id)
        version_rows = versions[deliverable.id]
        if publication is not None:
            version = next(
                (
                    item
                    for item in version_rows
                    if item.id == publication.version_id
                ),
                None,
            )
            candidate = {
                "deliverable_id": str(deliverable.id),
                "deliverable_code": deliverable.deliverable_code,
                "title": deliverable.title,
                "published_by": publication.published_by,
                "published_role": publication.published_role,
                "published_at": _json_value(publication.published_at),
                "version_no": (
                    version.version_no if version is not None else None
                ),
                "source_kind": (
                    version.source_kind if version is not None else None
                ),
                "file_name": (
                    version.file_name if version is not None else None
                ),
                "text_result": (
                    version.text_result if version is not None else None
                ),
            }
            if best is None or str(candidate.get("published_at") or "") > str(
                best.get("published_at") or ""
            ):
                best = candidate
    return best


def _publication_summary(
    publication: TaskDeliverablePublication | None,
) -> dict[str, Any] | None:
    if publication is None:
        return None
    return _json_value(
        {
            "id": str(publication.id),
            "deliverable_id": str(publication.deliverable_id),
            "version_id": str(publication.version_id),
            "published_by": publication.published_by,
            "published_role": publication.published_role,
            "published_at": publication.published_at,
        }
    )


def _notification_counts(
    notifications: list[NotificationDelivery],
) -> dict[str, int]:
    return _counter(item.status for item in notifications)


def _alert(
    *,
    alert_type: str,
    severity: str,
    workgroup_code: str | None,
    task_id: uuid.UUID | None,
    title: str,
    detail: dict[str, Any],
) -> dict[str, Any]:
    material = "|".join(
        (
            alert_type,
            workgroup_code or "",
            str(task_id or ""),
        )
    )
    alert_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return {
        "alert_key": alert_key,
        "alert_type": alert_type,
        "severity": severity,
        "workgroup_code": workgroup_code,
        "task_id": str(task_id) if task_id is not None else None,
        "title": title,
        "detail": detail,
    }


def _alert_summary(alerts: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(alerts),
        "critical": _counter(
            item["severity"] for item in alerts
        ).get("critical", 0),
        "warning": _counter(
            item["severity"] for item in alerts
        ).get("warning", 0),
        "info": _counter(item["severity"] for item in alerts).get("info", 0),
    }


def _counter(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value)
        result[key] = result.get(key, 0) + 1
    return result


def _is_terminal(status: str) -> bool:
    return status in {"completed", "not_required", "failed"}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    return uuid.UUID(str(value))


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
