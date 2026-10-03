from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.collaboration.domain import TaskStatus
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    WorkgroupTask,
)
from app.collaboration.repository import CollaborationRepository
from app.collaboration.roster import RosterService


class StaleTaskVersion(Exception):
    def __init__(self, expected_version: int, actual_version: int) -> None:
        super().__init__(
            f"expected task version {expected_version}, found {actual_version}"
        )
        self.expected_version = expected_version
        self.actual_version = actual_version


class MissingRequiredDeliverableError(Exception):
    pass


ALLOWED_TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.IN_PROGRESS, TaskStatus.NOT_REQUIRED},
    TaskStatus.IN_PROGRESS: {TaskStatus.PENDING_REVIEW, TaskStatus.NOT_REQUIRED},
    TaskStatus.PENDING_REVIEW: {
        TaskStatus.COMPLETED,
        TaskStatus.IN_PROGRESS,
    },
}

_GROUP_WORK_ROLES = {"leader", "deputy", "member"}


@dataclass(frozen=True, slots=True)
class _Actor:
    user_id: uuid.UUID
    username: str
    role: str
    workgroup: str | None


class CollaborationTaskService:
    def __init__(
        self,
        *,
        repository: CollaborationRepository | None = None,
        roster_service: RosterService | None = None,
    ) -> None:
        self.repository = repository or CollaborationRepository()
        self.roster_service = roster_service or RosterService()

    async def start(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        actor: object,
        expected_version: int,
        *,
        idempotency_key: str | None = None,
    ) -> WorkgroupTask:
        task, actor_identity, already_applied = await self._lock_task(
            session,
            task_id,
            actor,
            expected_version,
            idempotency_key=idempotency_key,
        )
        if already_applied:
            return task
        self._ensure_transition(task.status, TaskStatus.IN_PROGRESS)
        if not await self._can_work_task(
            session, actor_identity, task.workgroup_code
        ):
            raise PermissionError("Insufficient permissions")

        now = datetime.now(UTC)
        await self.repository.upsert_contributor(
            session,
            task_id=task.id,
            user_id=actor_identity.user_id,
            now=now,
        )
        self._apply_transition(task, TaskStatus.IN_PROGRESS, now=now)
        await self._append_ledger(
            session,
            task,
            event_type="task_started",
            actor=actor_identity.username,
            from_status=TaskStatus.PENDING.value,
            to_status=TaskStatus.IN_PROGRESS.value,
            payload={},
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return task

    async def submit(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        actor: object,
        expected_version: int,
        *,
        result_text: str | None = None,
        idempotency_key: str | None = None,
    ) -> WorkgroupTask:
        task, actor_identity, already_applied = await self._lock_task(
            session,
            task_id,
            actor,
            expected_version,
            idempotency_key=idempotency_key,
        )
        if already_applied:
            return task
        self._ensure_transition(task.status, TaskStatus.PENDING_REVIEW)
        if not await self._can_work_task(
            session, actor_identity, task.workgroup_code
        ):
            raise PermissionError("Insufficient permissions")

        now = datetime.now(UTC)
        await self.repository.upsert_contributor(
            session,
            task_id=task.id,
            user_id=actor_identity.user_id,
            now=now,
        )
        self._apply_transition(task, TaskStatus.PENDING_REVIEW, now=now)
        await self._append_ledger(
            session,
            task,
            event_type="task_submitted",
            actor=actor_identity.username,
            from_status=TaskStatus.IN_PROGRESS.value,
            to_status=TaskStatus.PENDING_REVIEW.value,
            payload={"result_text": result_text},
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return task

    async def return_to_work(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        actor: object,
        expected_version: int,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> WorkgroupTask:
        task, actor_identity, already_applied = await self._lock_task(
            session,
            task_id,
            actor,
            expected_version,
            idempotency_key=idempotency_key,
        )
        if already_applied:
            return task
        self._ensure_transition(task.status, TaskStatus.IN_PROGRESS)
        if not await self._can_confirm(
            session, task, actor_identity
        ):
            raise PermissionError("Insufficient permissions")

        now = datetime.now(UTC)
        self._apply_transition(task, TaskStatus.IN_PROGRESS, now=now)
        await self._append_ledger(
            session,
            task,
            event_type="task_returned",
            actor=actor_identity.username,
            from_status=TaskStatus.PENDING_REVIEW.value,
            to_status=TaskStatus.IN_PROGRESS.value,
            payload={"reason": reason},
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return task

    async def complete(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        actor: object,
        expected_version: int,
        *,
        idempotency_key: str | None = None,
    ) -> WorkgroupTask:
        task, actor_identity, already_applied = await self._lock_task(
            session,
            task_id,
            actor,
            expected_version,
            idempotency_key=idempotency_key,
        )
        if already_applied:
            return task
        self._ensure_transition(task.status, TaskStatus.COMPLETED)
        if not await self._can_confirm(
            session, task, actor_identity
        ):
            raise PermissionError("Insufficient permissions")
        if not await self.repository.required_deliverables_satisfied(
            session, task.id
        ):
            raise MissingRequiredDeliverableError(
                "task has unsatisfied required deliverables"
            )

        now = datetime.now(UTC)
        self._apply_transition(task, TaskStatus.COMPLETED, now=now)
        task.completed_at = now
        await self._append_ledger(
            session,
            task,
            event_type="task_completed",
            actor=actor_identity.username,
            from_status=TaskStatus.PENDING_REVIEW.value,
            to_status=TaskStatus.COMPLETED.value,
            payload={},
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return task

    async def cancel(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        actor: object,
        expected_version: int,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> WorkgroupTask:
        task, actor_identity, already_applied = await self._lock_task(
            session,
            task_id,
            actor,
            expected_version,
            idempotency_key=idempotency_key,
        )
        if already_applied:
            return task
        self._ensure_transition(task.status, TaskStatus.NOT_REQUIRED)
        if not await self._can_confirm(
            session, task, actor_identity
        ):
            raise PermissionError("Insufficient permissions")

        now = datetime.now(UTC)
        previous_status = task.status
        self._apply_transition(task, TaskStatus.NOT_REQUIRED, now=now)
        task.closed_at = now
        await self._append_ledger(
            session,
            task,
            event_type="task_cancelled",
            actor=actor_identity.username,
            from_status=previous_status,
            to_status=TaskStatus.NOT_REQUIRED.value,
            payload={"reason": reason},
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return task

    async def update(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        actor: object,
        expected_version: int,
        *,
        title: str | None = None,
        instruction: str | None = None,
        priority: int | None = None,
        due_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> WorkgroupTask:
        task, actor_identity, already_applied = await self._lock_task(
            session,
            task_id,
            actor,
            expected_version,
            idempotency_key=idempotency_key,
        )
        if already_applied:
            return task
        if not await self._can_confirm(
            session, task, actor_identity
        ):
            raise PermissionError("Insufficient permissions")

        now = datetime.now(UTC)
        changes: dict[str, Any] = {}
        if title is not None:
            task.title = title
            changes["title"] = title
        if instruction is not None:
            task.instruction = instruction
            changes["instruction"] = instruction
        if priority is not None:
            if priority < 0:
                raise ValueError("priority must be non-negative")
            task.priority = priority
            changes["priority"] = priority
        if due_at is not None:
            task.due_at = due_at
            changes["due_at"] = due_at.isoformat()

        task.row_version += 1
        task.updated_at = now
        await self._append_ledger(
            session,
            task,
            event_type="task_updated",
            actor=actor_identity.username,
            from_status=task.status,
            to_status=task.status,
            payload={"changes": changes},
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return task

    async def _lock_task(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        actor: object,
        expected_version: int,
        *,
        idempotency_key: str | None,
    ) -> tuple[WorkgroupTask, _Actor, bool]:
        if idempotency_key is not None:
            previous = await self.repository.get_event_by_idempotency_key(
                session, task_id, idempotency_key
            )
            if previous is not None:
                task = await self.repository.get_task(
                    session, task_id, for_update=True
                )
                if task is None:
                    raise LookupError("task_not_found")
                return task, _Actor(
                    user_id=uuid.UUID(int=0),
                    username="",
                    role="",
                    workgroup=None,
                ), True

        task = await self.repository.get_task(
            session, task_id, for_update=True
        )
        if task is None:
            raise LookupError("task_not_found")
        actor_identity = await self._resolve_actor(session, actor)
        if int(task.row_version) != int(expected_version):
            raise StaleTaskVersion(
                expected_version=int(expected_version),
                actual_version=task.row_version,
            )
        return task, actor_identity, False

    async def _resolve_actor(
        self,
        session: AsyncSession,
        actor: object,
    ) -> _Actor:
        actor_id = getattr(actor, "id", None)
        if isinstance(actor_id, uuid.UUID):
            return _Actor(
                user_id=actor_id,
                username=str(getattr(actor, "username", "")),
                role=str(getattr(actor, "role", "")),
                workgroup=getattr(actor, "workgroup", None),
            )

        username = str(getattr(actor, "username", ""))
        user = await self.repository.get_user_by_username(session, username)
        if user is None:
            raise PermissionError("Insufficient permissions")
        return _Actor(
            user_id=user.id,
            username=user.username,
            role=user.role,
            workgroup=user.workgroup,
        )

    async def _can_work_task(
        self,
        session: AsyncSession,
        actor: _Actor,
        group_code: str,
    ) -> bool:
        if actor.role == "superadmin":
            return True
        membership = await self.repository.get_active_membership(
            session, group_code, actor.user_id
        )
        return (
            membership is not None
            and membership.duty_role in _GROUP_WORK_ROLES
        )

    async def _can_confirm(
        self,
        session: AsyncSession,
        task: WorkgroupTask,
        actor: _Actor,
    ) -> bool:
        if actor.role == "superadmin":
            return True
        authority = await self.roster_service.resolve_confirming_authority(
            session, task.event_id, task.workgroup_code
        )
        return authority is not None and authority.user_id == actor.user_id

    @staticmethod
    def _ensure_transition(
        current_status: str,
        target_status: TaskStatus,
    ) -> None:
        current = TaskStatus(current_status)
        if current not in ALLOWED_TRANSITIONS:
            raise ValueError(f"task status {current.value} cannot transition")
        if target_status not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(
                f"task cannot transition from {current.value} "
                f"to {target_status.value}"
            )

    @staticmethod
    def _apply_transition(
        task: WorkgroupTask,
        target_status: TaskStatus,
        *,
        now: datetime,
    ) -> None:
        task.status = target_status.value
        task.row_version += 1
        task.updated_at = now

    async def _append_ledger(
        self,
        session: AsyncSession,
        task: WorkgroupTask,
        *,
        event_type: str,
        actor: str,
        from_status: str | None,
        to_status: str | None,
        payload: dict[str, Any],
        idempotency_key: str | None,
        occurred_at: datetime,
    ) -> None:
        event_seq = await self.repository.next_event_sequence(
            session, task.id
        )
        event_key = idempotency_key or _event_key(task.id, event_seq)
        event_payload = {
            **payload,
            "task_code": task.task_code,
            "row_version": task.row_version,
        }
        session.add(
            CollaborationTaskEvent(
                task_id=task.id,
                event_seq=event_seq,
                event_type=event_type,
                actor=actor,
                from_status=from_status,
                to_status=to_status,
                business_version=task.row_version,
                idempotency_key=event_key,
                payload=event_payload,
                occurred_at=occurred_at,
            )
        )
        session.add(
            CollaborationOutbox(
                event_id=task.event_id,
                task_id=task.id,
                event_type=event_type,
                idempotency_key=event_key,
                payload={
                    "task_id": str(task.id),
                    "event_id": str(task.event_id),
                    "task_code": task.task_code,
                    "workgroup_code": task.workgroup_code,
                    "status": task.status,
                    "row_version": task.row_version,
                    **payload,
                },
                status="pending",
                attempt_count=0,
                available_at=occurred_at,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
        )
        await session.flush()


def _event_key(task_id: uuid.UUID, event_seq: int) -> str:
    material = f"{task_id}:{event_seq}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
