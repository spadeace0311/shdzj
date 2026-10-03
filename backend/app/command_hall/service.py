from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.collaboration.models import (
    CollaborationTaskEvent,
    CommandHallAlertProjection,
    CommandHallEventProjection,
    CommandHallGroupProjection,
    NotificationDelivery,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupTask,
    WorkgroupTaskContributor,
)
from app.command_hall.schemas import (
    ActiveEventResponse,
    EventOverview,
    GroupDetail,
    ProjectionTaskCounts,
    ProjectionUpdatedPayload,
    TaskDetail,
)


class CommandHallService:
    async def active_event(
        self,
        session: AsyncSession,
    ) -> uuid.UUID | None:
        event_type = func.jsonb_extract_path_text(
            CommandHallEventProjection.event_snapshot,
            "event_type",
        )
        lifecycle_state = func.jsonb_extract_path_text(
            CommandHallEventProjection.event_snapshot,
            "lifecycle_state",
        )
        rank = case(
            (
                and_(
                    event_type.in_(("formal", "manual")),
                    lifecycle_state != "not_applicable",
                ),
                1,
            ),
            (
                and_(
                    event_type == "drill",
                    lifecycle_state != "not_applicable",
                ),
                2,
            ),
            (
                and_(
                    event_type == "test",
                    lifecycle_state != "not_applicable",
                ),
                3,
            ),
            else_=4,
        )
        return await session.scalar(
            select(CommandHallEventProjection.event_id)
            .order_by(
                rank,
                func.jsonb_extract_path_text(
                    CommandHallEventProjection.event_snapshot,
                    "origin_time",
                ).desc(),
            )
            .limit(1)
        )

    async def active_event_response(
        self,
        session: AsyncSession,
    ) -> ActiveEventResponse:
        return ActiveEventResponse(
            event_id=await self.active_event(session)
        )

    async def overview(
        self,
        session: AsyncSession,
        event_id: object,
    ) -> EventOverview:
        projection = await self.get_event_projection(session, event_id)
        if projection is None:
            raise LookupError("command_hall_event_not_found")

        group_rows = list(
            await session.scalars(
                select(CommandHallGroupProjection)
                .where(
                    CommandHallGroupProjection.event_id == projection.event_id,
                    CommandHallGroupProjection.is_current.is_(True),
                )
            )
        )
        group_rows.sort(
            key=lambda row: int(
                (row.group_snapshot or {}).get("display_order", 0)
            )
        )
        alerts = await self._alerts(session, projection.event_id)
        groups = [
            {
                **(row.group_snapshot or {}),
                "task_counts": row.task_counts,
                "latest_deliverable": row.latest_deliverable,
                "alert_summary": row.alert_summary,
                "projection_version": row.projection_version,
                "updated_at": _json_value(row.updated_at),
            }
            for row in group_rows
        ]
        task_counts = ProjectionTaskCounts(**(projection.task_counts or {}))
        return EventOverview(
            event_id=projection.event_id,
            event=projection.event_snapshot or {},
            group_count=len(groups),
            groups=groups,
            alerts=alerts,
            task_counts=task_counts,
            artifact_summary=projection.artifact_summary or {},
            alert_summary=projection.alert_summary or {},
            dual_version_count=int(
                (projection.task_counts or {}).get(
                    "dual_version_count",
                    0,
                )
            ),
            projection_version=projection.projection_version,
            updated_at=projection.updated_at,
        )

    async def group_detail(
        self,
        session: AsyncSession,
        event_id: object,
        group_code: str,
    ) -> GroupDetail:
        event_uuid = _coerce_uuid(event_id, "event_id")
        row = await session.scalar(
            select(CommandHallGroupProjection)
            .where(
                CommandHallGroupProjection.event_id == event_uuid,
                CommandHallGroupProjection.workgroup_code == group_code,
                CommandHallGroupProjection.is_current.is_(True),
            )
            .limit(1)
        )
        if row is None:
            raise LookupError("command_hall_group_not_found")
        tasks = (row.group_snapshot or {}).get("tasks", [])
        alerts = await self._alerts(
            session,
            event_uuid,
            group_code=group_code,
        )
        return GroupDetail(
            event_id=event_uuid,
            workgroup_code=row.workgroup_code,
            group=row.group_snapshot or {},
            tasks=tasks,
            alerts=alerts,
            alert_summary=row.alert_summary or {},
            task_counts=ProjectionTaskCounts(**(row.task_counts or {})),
            projection_version=row.projection_version,
            updated_at=row.updated_at,
        )

    async def task_detail(
        self,
        session: AsyncSession,
        task_id: object,
    ) -> TaskDetail:
        task_uuid = _coerce_uuid(task_id, "task_id")
        task = await session.scalar(
            select(WorkgroupTask).where(WorkgroupTask.id == task_uuid)
        )
        if task is None:
            raise LookupError("task_not_found")

        contributors = (
            await session.execute(
                select(WorkgroupTaskContributor, User)
                .join(User, User.id == WorkgroupTaskContributor.user_id)
                .where(WorkgroupTaskContributor.task_id == task.id)
                .order_by(WorkgroupTaskContributor.first_contributed_at)
            )
        ).all()
        deliverables = list(
            await session.scalars(
                select(TaskDeliverable)
                .where(TaskDeliverable.task_id == task.id)
                .order_by(TaskDeliverable.display_order, TaskDeliverable.created_at)
            )
        )
        versions_by_deliverable: dict[
            uuid.UUID, list[TaskDeliverableVersion]
        ] = defaultdict(list)
        if deliverables:
            versions = await session.scalars(
                select(TaskDeliverableVersion)
                .where(
                    TaskDeliverableVersion.deliverable_id.in_(
                        [item.id for item in deliverables]
                    )
                )
                .order_by(TaskDeliverableVersion.version_no)
            )
            for version in versions:
                versions_by_deliverable[version.deliverable_id].append(version)
        publications_by_deliverable: dict[
            uuid.UUID, TaskDeliverablePublication
        ] = {}
        if deliverables:
            publications = await session.scalars(
                select(TaskDeliverablePublication)
                .where(
                    TaskDeliverablePublication.deliverable_id.in_(
                        [item.id for item in deliverables]
                    ),
                    TaskDeliverablePublication.superseded_at.is_(None),
                )
                .order_by(TaskDeliverablePublication.published_at.desc())
            )
            for publication in publications:
                publications_by_deliverable.setdefault(
                    publication.deliverable_id,
                    publication,
                )
        task_events = list(
            await session.scalars(
                select(CollaborationTaskEvent)
                .where(CollaborationTaskEvent.task_id == task.id)
                .order_by(CollaborationTaskEvent.event_seq)
            )
        )
        notifications = list(
            await session.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.task_id == task.id)
                .order_by(NotificationDelivery.created_at)
            )
        )
        projection = await session.scalar(
            select(CommandHallEventProjection)
            .where(CommandHallEventProjection.event_id == task.event_id)
            .limit(1)
        )
        return TaskDetail(
            id=task.id,
            event_id=task.event_id,
            task=_task_dict(
                task,
                contributors=[
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
                    for contributor, user in contributors
                ],
            ),
            contributors=[
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
                for contributor, user in contributors
            ],
            deliverables=[
                {
                    "id": str(deliverable.id),
                    "task_id": str(deliverable.task_id),
                    "deliverable_code": deliverable.deliverable_code,
                    "title": deliverable.title,
                    "is_required": deliverable.is_required,
                    "requirement_kind": deliverable.requirement_kind,
                    "artifact_binding": deliverable.artifact_binding,
                    "display_order": deliverable.display_order,
                    "current_publication": _publication_dict(
                        publications_by_deliverable.get(deliverable.id)
                    ),
                    "versions": [
                        _version_dict(version)
                        for version in versions_by_deliverable[deliverable.id]
                    ],
                }
                for deliverable in deliverables
            ],
            task_events=[
                _task_event_dict(item) for item in task_events
            ],
            notifications=[
                _notification_dict(item) for item in notifications
            ],
            projection_version=(
                projection.projection_version if projection is not None else None
            ),
        )

    async def get_event_projection(
        self,
        session: AsyncSession,
        event_id: object,
    ) -> CommandHallEventProjection | None:
        event_uuid = _coerce_uuid(event_id, "event_id")
        return await session.scalar(
            select(CommandHallEventProjection)
            .where(CommandHallEventProjection.event_id == event_uuid)
            .limit(1)
        )

    async def _alerts(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        *,
        group_code: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = select(CommandHallAlertProjection).where(
            CommandHallAlertProjection.event_id == event_id
        )
        if group_code is not None:
            statement = statement.where(
                CommandHallAlertProjection.workgroup_code == group_code
            )
        rows = await session.scalars(
            statement.order_by(
                CommandHallAlertProjection.severity,
                CommandHallAlertProjection.first_seen_at,
            )
        )
        return [_alert_dict(row) for row in rows]

    async def stream_projection(
        self,
        event_id: object,
        *,
        read_projection: Callable[
            [],
            Awaitable[CommandHallEventProjection | None],
        ],
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 15.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> AsyncIterator[str]:
        event_uuid = _coerce_uuid(event_id, "event_id")
        last_version: int | None = None
        last_heartbeat = now()
        while True:
            if is_disconnected is not None and await is_disconnected():
                return
            projection = await read_projection()
            if projection is None:
                return
            if last_version is None:
                last_version = projection.projection_version
            elif projection.projection_version != last_version:
                payload = ProjectionUpdatedPayload(
                    event_id=event_uuid,
                    projection_version=projection.projection_version,
                ).model_dump_json()
                yield f"event: projection.updated\ndata: {payload}\n\n"
                last_version = projection.projection_version

            current_time = now()
            if (
                current_time - last_heartbeat
            ).total_seconds() >= heartbeat_seconds:
                yield f": heartbeat {current_time.isoformat()}\n\n"
                last_heartbeat = current_time
            await sleep(poll_seconds)


def _task_dict(
    task: WorkgroupTask,
    *,
    contributors: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
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
        "contributors": contributors,
    }


def _version_dict(version: TaskDeliverableVersion) -> dict[str, Any]:
    return _json_value(
        {
            "id": str(version.id),
            "deliverable_id": str(version.deliverable_id),
            "version_no": version.version_no,
            "source_kind": version.source_kind,
            "artifact_id": str(version.artifact_id)
            if version.artifact_id is not None
            else None,
            "artifact_publication_id": str(version.artifact_publication_id)
            if version.artifact_publication_id is not None
            else None,
            "storage_key": version.storage_key,
            "file_name": version.file_name,
            "checksum": version.checksum,
            "mime_type": version.mime_type,
            "size_bytes": version.size_bytes,
            "text_result": version.text_result,
            "created_by": version.created_by,
            "basis_text": version.basis_text,
            "supersedes_version_id": str(version.supersedes_version_id)
            if version.supersedes_version_id is not None
            else None,
            "created_at": version.created_at,
        }
    )


def _publication_dict(
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
            "superseded_at": publication.superseded_at,
            "publication_note": publication.publication_note,
        }
    )


def _task_event_dict(event: CollaborationTaskEvent) -> dict[str, Any]:
    return _json_value(
        {
            "id": str(event.id),
            "task_id": str(event.task_id),
            "event_seq": event.event_seq,
            "event_type": event.event_type,
            "actor": event.actor,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "business_version": event.business_version,
            "idempotency_key": event.idempotency_key,
            "payload": event.payload,
            "occurred_at": event.occurred_at,
        }
    )


def _notification_dict(
    notification: NotificationDelivery,
) -> dict[str, Any]:
    return _json_value(
        {
            "id": str(notification.id),
            "event_id": str(notification.event_id),
            "task_id": str(notification.task_id)
            if notification.task_id is not None
            else None,
            "recipient_user_id": str(notification.recipient_user_id),
            "intent_type": notification.intent_type,
            "channel": notification.channel,
            "dedupe_key": notification.dedupe_key,
            "status": notification.status,
            "attempt_count": notification.attempt_count,
            "available_at": notification.available_at,
            "provider_message_id": notification.provider_message_id,
            "last_error": notification.last_error,
            "sent_at": notification.sent_at,
            "created_at": notification.created_at,
        }
    )


def _alert_dict(alert: CommandHallAlertProjection) -> dict[str, Any]:
    return _json_value(
        {
            "id": str(alert.id),
            "event_id": str(alert.event_id),
            "workgroup_code": alert.workgroup_code,
            "task_id": str(alert.task_id)
            if alert.task_id is not None
            else None,
            "alert_key": alert.alert_key,
            "alert_type": alert.alert_type,
            "severity": alert.severity,
            "status": alert.status,
            "title": alert.title,
            "detail": alert.detail,
            "first_seen_at": alert.first_seen_at,
            "resolved_at": alert.resolved_at,
            "updated_at": alert.updated_at,
        }
    )


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


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
