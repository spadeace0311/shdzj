from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.storage import ArtifactStore
from app.collaboration.domain import (
    DeliverableSourceKind,
    TaskStatus,
)
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskEvent,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupTask,
)
from app.collaboration.repository import CollaborationRepository
from app.collaboration.roster import RosterService
from app.config import settings


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
            event_type="task_started",
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
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="task_started",
            actor=actor_identity.username,
            actor_id=actor_identity.user_id,
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
            event_type="task_submitted",
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
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="task_submitted",
            actor=actor_identity.username,
            actor_id=actor_identity.user_id,
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
            event_type="task_returned",
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
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="task_returned",
            actor=actor_identity.username,
            actor_id=actor_identity.user_id,
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
            event_type="task_completed",
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
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="task_completed",
            actor=actor_identity.username,
            actor_id=actor_identity.user_id,
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
            event_type="task_cancelled",
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
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="task_cancelled",
            actor=actor_identity.username,
            actor_id=actor_identity.user_id,
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
            event_type="task_updated",
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
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="task_updated",
            actor=actor_identity.username,
            actor_id=actor_identity.user_id,
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
        event_type: str,
    ) -> tuple[WorkgroupTask, _Actor, bool]:
        task = await self.repository.get_task(
            session, task_id, for_update=True
        )
        if task is None:
            raise LookupError("task_not_found")
        actor_identity = await _resolve_actor(
            session, self.repository, actor
        )

        if idempotency_key is not None:
            previous = await self.repository.get_event_by_idempotency_key(
                session, task_id, idempotency_key
            )
            if previous is not None:
                _validate_idempotent_replay(
                    previous,
                    actor_identity,
                    event_type,
                )
                return task, actor_identity, True

        if int(task.row_version) != int(expected_version):
            raise StaleTaskVersion(
                expected_version=int(expected_version),
                actual_version=task.row_version,
            )
        return task, actor_identity, False

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


async def _resolve_actor(
    session: AsyncSession,
    repository: CollaborationRepository,
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
    user = await repository.get_user_by_username(session, username)
    if user is None:
        raise PermissionError("Insufficient permissions")
    return _Actor(
        user_id=user.id,
        username=user.username,
        role=user.role,
        workgroup=user.workgroup,
    )


def _validate_idempotent_replay(
    previous: CollaborationTaskEvent,
    actor: _Actor,
    event_type: str,
) -> None:
    if previous.event_type != event_type:
        raise ValueError(
            "idempotency key was used for a different operation"
        )
    stored_actor_id = previous.payload.get("actor_id")
    if stored_actor_id is not None:
        try:
            if uuid.UUID(str(stored_actor_id)) != actor.user_id:
                raise PermissionError("Insufficient permissions")
        except ValueError as exc:
            raise PermissionError("Insufficient permissions") from exc
        return
    if previous.actor != actor.username:
        raise PermissionError("Insufficient permissions")


async def _append_ledger(
    session: AsyncSession,
    repository: CollaborationRepository,
    task: WorkgroupTask,
    *,
    event_type: str,
    actor: str,
    actor_id: uuid.UUID,
    from_status: str | None,
    to_status: str | None,
    payload: dict[str, Any],
    idempotency_key: str | None,
    occurred_at: datetime,
) -> None:
    event_seq = await repository.next_event_sequence(session, task.id)
    event_key = idempotency_key or _event_key(task.id, event_seq)
    event_payload = {
        **payload,
        "task_code": task.task_code,
        "row_version": task.row_version,
        "actor_id": str(actor_id),
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


class DeliverableService:
    def __init__(
        self,
        *,
        repository: CollaborationRepository | None = None,
        roster_service: RosterService | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.repository = repository or CollaborationRepository()
        self.roster_service = roster_service or RosterService()
        self.artifact_store = artifact_store or ArtifactStore(
            settings.artifact_storage_root
        )

    async def add_text_version(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        actor: object,
        *,
        text_result: dict[str, Any],
        basis_text: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> TaskDeliverableVersion:
        deliverable, task, actor_identity, previous = (
            await self._lock_deliverable(
                session,
                deliverable_id,
                actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                event_type="deliverable_version_added",
            )
        )
        if previous is not None:
            return await self._version_from_event(
                session, task, idempotency_key
            )
        if not await self._can_work_task(
            session, actor_identity, task.workgroup_code
        ):
            raise PermissionError("Insufficient permissions")
        if not isinstance(text_result, dict) or not text_result:
            raise ValueError("text_result must be a non-empty object")

        return await self._create_version(
            session,
            deliverable,
            task,
            actor_identity,
            source_kind=DeliverableSourceKind.MANUAL,
            text_result=text_result,
            basis_text=basis_text,
            idempotency_key=idempotency_key,
        )

    async def add_manual_version(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        actor: object,
        *,
        source: BinaryIO,
        file_name: str,
        mime_type: str | None = None,
        text_result: dict[str, Any] | None = None,
        basis_text: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> TaskDeliverableVersion:
        deliverable, task, actor_identity, previous = (
            await self._lock_deliverable(
                session,
                deliverable_id,
                actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                event_type="deliverable_version_added",
            )
        )
        if previous is not None:
            return await self._version_from_event(
                session, task, idempotency_key
            )
        if not await self._can_work_task(
            session, actor_identity, task.workgroup_code
        ):
            raise PermissionError("Insufficient permissions")

        stored = self.artifact_store.store_immutable_stream(
            source,
            file_name=file_name,
        )
        return await self._create_version(
            session,
            deliverable,
            task,
            actor_identity,
            source_kind=DeliverableSourceKind.MANUAL,
            storage_key=stored.relative_path,
            file_name=stored.file_name,
            checksum=stored.checksum,
            mime_type=mime_type,
            size_bytes=stored.size_bytes,
            text_result=text_result,
            basis_text=basis_text,
            idempotency_key=idempotency_key,
        )

    async def publish(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        actor: object,
        *,
        version_id: uuid.UUID,
        publication_note: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> TaskDeliverablePublication:
        return await self._publish_existing_version(
            session,
            deliverable_id,
            actor,
            version_id=version_id,
            publication_note=publication_note,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            event_type="deliverable_published",
        )

    async def restore(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        actor: object,
        *,
        version_id: uuid.UUID,
        publication_note: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> TaskDeliverablePublication:
        return await self._publish_existing_version(
            session,
            deliverable_id,
            actor,
            version_id=version_id,
            publication_note=publication_note,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            event_type="deliverable_restored",
        )

    async def override(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        actor: object,
        *,
        source: BinaryIO | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
        text_result: dict[str, Any] | None = None,
        basis_text: str | None = None,
        publication_note: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> TaskDeliverablePublication:
        deliverable, task, actor_identity, previous = (
            await self._lock_deliverable(
                session,
                deliverable_id,
                actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                event_type="deliverable_override_published",
            )
        )
        if previous is not None:
            return await self._publication_from_event(
                session, task, idempotency_key
            )
        if actor_identity.role != "superadmin":
            raise PermissionError("Insufficient permissions")

        version = await self._build_override_version(
            session,
            deliverable,
            actor_identity,
            source=source,
            file_name=file_name,
            mime_type=mime_type,
            text_result=text_result,
            basis_text=basis_text,
        )
        session.add(version)
        await session.flush()
        return await self._create_publication(
            session,
            deliverable,
            task,
            version,
            actor_identity,
            published_role="superadmin",
            publication_note=publication_note,
            idempotency_key=idempotency_key,
            event_type="deliverable_override_published",
        )

    async def delete_candidate(
        self,
        session: AsyncSession,
        version_id: uuid.UUID,
        actor: object,
        *,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> None:
        deliverable, task, version, actor_identity, previous = (
            await self._lock_version(
                session,
                version_id,
                actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                event_type="deliverable_candidate_deleted",
            )
        )
        if previous is not None:
            return
        if version.source_kind != DeliverableSourceKind.MANUAL.value:
            raise ValueError("only manual candidate versions can be deleted")
        if (
            await self.repository.publication_for_version(session, version.id)
            is not None
        ):
            raise ValueError("published versions cannot be deleted")

        can_delete = actor_identity.username == version.created_by
        if not can_delete:
            try:
                await self._confirming_role(session, task, actor_identity)
                can_delete = True
            except PermissionError:
                can_delete = False
        if not can_delete:
            raise PermissionError("Insufficient permissions")

        now = datetime.now(UTC)
        await session.delete(version)
        deliverable.updated_at = now
        _bump_task_version(task, now=now)
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="deliverable_candidate_deleted",
            actor=actor_identity.username,
            actor_id=actor_identity.user_id,
            from_status=task.status,
            to_status=task.status,
            payload={
                "deliverable_id": str(deliverable.id),
                "deliverable_code": deliverable.deliverable_code,
                "version_id": str(version.id),
                "version_no": version.version_no,
            },
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()

    async def current_version_id(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
    ) -> uuid.UUID | None:
        return await self.repository.current_version_id(
            session, deliverable_id
        )

    async def _publish_existing_version(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        actor: object,
        *,
        version_id: uuid.UUID,
        publication_note: str | None,
        idempotency_key: str | None,
        expected_version: int | None,
        event_type: str,
    ) -> TaskDeliverablePublication:
        deliverable, task, actor_identity, previous = (
            await self._lock_deliverable(
                session,
                deliverable_id,
                actor,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                event_type=event_type,
            )
        )
        if previous is not None:
            return await self._publication_from_event(
                session, task, idempotency_key
            )

        published_role = await self._confirming_role(
            session, task, actor_identity
        )
        version = await self.repository.get_deliverable_version(
            session, version_id, for_update=True
        )
        if version is None or version.deliverable_id != deliverable.id:
            raise ValueError("version does not belong to deliverable")
        await _validate_version_for_publication(
            session, version, self.artifact_store
        )
        return await self._create_publication(
            session,
            deliverable,
            task,
            version,
            actor_identity,
            published_role=published_role,
            publication_note=publication_note,
            idempotency_key=idempotency_key,
            event_type=event_type,
        )

    async def _create_version(
        self,
        session: AsyncSession,
        deliverable: TaskDeliverable,
        task: WorkgroupTask,
        actor: _Actor,
        *,
        source_kind: DeliverableSourceKind,
        storage_key: str | None = None,
        file_name: str | None = None,
        checksum: str | None = None,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        text_result: dict[str, Any] | None = None,
        basis_text: str | None = None,
        idempotency_key: str | None,
    ) -> TaskDeliverableVersion:
        now = datetime.now(UTC)
        version_no = await self.repository.next_deliverable_version_no(
            session, deliverable.id
        )
        previous = await self.repository.latest_deliverable_version(
            session, deliverable.id, for_update=True
        )
        version = TaskDeliverableVersion(
            deliverable_id=deliverable.id,
            version_no=version_no,
            source_kind=source_kind.value,
            storage_key=storage_key,
            file_name=file_name,
            checksum=checksum,
            mime_type=mime_type,
            size_bytes=size_bytes,
            text_result=text_result,
            created_by=actor.username,
            basis_text=basis_text,
            supersedes_version_id=previous.id if previous is not None else None,
        )
        session.add(version)
        deliverable.updated_at = now
        _bump_task_version(task, now=now)
        await session.flush()
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type="deliverable_version_added",
            actor=actor.username,
            actor_id=actor.user_id,
            from_status=task.status,
            to_status=task.status,
            payload={
                "deliverable_id": str(deliverable.id),
                "deliverable_code": deliverable.deliverable_code,
                "version_id": str(version.id),
                "version_no": version.version_no,
                "source_kind": version.source_kind,
            },
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return version

    async def _create_publication(
        self,
        session: AsyncSession,
        deliverable: TaskDeliverable,
        task: WorkgroupTask,
        version: TaskDeliverableVersion,
        actor: _Actor,
        *,
        published_role: str,
        publication_note: str | None,
        idempotency_key: str | None,
        event_type: str,
    ) -> TaskDeliverablePublication:
        now = datetime.now(UTC)
        current = await self.repository.get_current_publication(
            session, deliverable.id, for_update=True
        )
        if current is not None:
            current.superseded_at = now
            await session.flush()

        publication = TaskDeliverablePublication(
            deliverable_id=deliverable.id,
            version_id=version.id,
            published_by=actor.username,
            published_role=published_role,
            published_at=now,
            publication_note=publication_note,
        )
        session.add(publication)
        deliverable.updated_at = now
        _bump_task_version(task, now=now)
        await session.flush()
        await _append_ledger(
            session,
            self.repository,
            task,
            event_type=event_type,
            actor=actor.username,
            actor_id=actor.user_id,
            from_status=task.status,
            to_status=task.status,
            payload={
                "deliverable_id": str(deliverable.id),
                "deliverable_code": deliverable.deliverable_code,
                "version_id": str(version.id),
                "version_no": version.version_no,
                "publication_id": str(publication.id),
                "published_role": published_role,
                "publication_note": publication_note,
            },
            idempotency_key=idempotency_key,
            occurred_at=now,
        )
        await session.flush()
        return publication

    async def _build_override_version(
        self,
        session: AsyncSession,
        deliverable: TaskDeliverable,
        actor: _Actor,
        *,
        source: BinaryIO | None,
        file_name: str | None,
        mime_type: str | None,
        text_result: dict[str, Any] | None,
        basis_text: str | None,
    ) -> TaskDeliverableVersion:
        next_version = await self.repository.next_deliverable_version_no(
            session, deliverable.id
        )
        previous = await self.repository.latest_deliverable_version(
            session, deliverable.id, for_update=True
        )
        if source is not None:
            if not file_name:
                raise ValueError("file_name is required for file overrides")
            stored = self.artifact_store.store_immutable_stream(
                source,
                file_name=file_name,
            )
            return TaskDeliverableVersion(
                deliverable_id=deliverable.id,
                version_no=next_version,
                source_kind=DeliverableSourceKind.SUPERADMIN_OVERRIDE.value,
                storage_key=stored.relative_path,
                file_name=stored.file_name,
                checksum=stored.checksum,
                mime_type=mime_type,
                size_bytes=stored.size_bytes,
                text_result=text_result,
                created_by=actor.username,
                basis_text=basis_text,
                supersedes_version_id=previous.id if previous is not None else None,
            )
        if not isinstance(text_result, dict) or not text_result:
            raise ValueError("text_result is required for text overrides")
        return TaskDeliverableVersion(
            deliverable_id=deliverable.id,
            version_no=next_version,
            source_kind=DeliverableSourceKind.SUPERADMIN_OVERRIDE.value,
            text_result=text_result,
            created_by=actor.username,
            basis_text=basis_text,
            supersedes_version_id=previous.id if previous is not None else None,
        )

    async def _lock_deliverable(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        actor: object,
        *,
        expected_version: int | None,
        idempotency_key: str | None,
        event_type: str,
    ) -> tuple[
        TaskDeliverable,
        WorkgroupTask,
        _Actor,
        CollaborationTaskEvent | None,
    ]:
        deliverable = await self.repository.get_deliverable(
            session, deliverable_id
        )
        if deliverable is None:
            raise LookupError("deliverable_not_found")
        task = await self.repository.get_task(
            session, deliverable.task_id, for_update=True
        )
        if task is None:
            raise LookupError("task_not_found")
        actor_identity = await _resolve_actor(
            session, self.repository, actor
        )
        if idempotency_key is not None:
            previous = await self.repository.get_event_by_idempotency_key(
                session, task.id, idempotency_key
            )
            if previous is not None:
                _validate_idempotent_replay(
                    previous, actor_identity, event_type
                )
                return deliverable, task, actor_identity, previous
        if expected_version is not None and int(task.row_version) != int(
            expected_version
        ):
            raise StaleTaskVersion(
                expected_version=int(expected_version),
                actual_version=task.row_version,
            )
        deliverable = await self.repository.get_deliverable(
            session, deliverable_id, for_update=True
        )
        if deliverable is None:
            raise LookupError("deliverable_not_found")
        return deliverable, task, actor_identity, None

    async def _lock_version(
        self,
        session: AsyncSession,
        version_id: uuid.UUID,
        actor: object,
        *,
        expected_version: int | None,
        idempotency_key: str | None,
        event_type: str,
    ) -> tuple[
        TaskDeliverable,
        WorkgroupTask,
        TaskDeliverableVersion,
        _Actor,
        CollaborationTaskEvent | None,
    ]:
        version = await self.repository.get_deliverable_version(
            session, version_id
        )
        if version is None:
            raise LookupError("deliverable_version_not_found")
        deliverable = await self.repository.get_deliverable(
            session, version.deliverable_id
        )
        if deliverable is None:
            raise LookupError("deliverable_not_found")
        task = await self.repository.get_task(
            session, deliverable.task_id, for_update=True
        )
        if task is None:
            raise LookupError("task_not_found")
        actor_identity = await _resolve_actor(
            session, self.repository, actor
        )
        if idempotency_key is not None:
            previous = await self.repository.get_event_by_idempotency_key(
                session, task.id, idempotency_key
            )
            if previous is not None:
                _validate_idempotent_replay(
                    previous, actor_identity, event_type
                )
                return deliverable, task, version, actor_identity, previous
        if expected_version is not None and int(task.row_version) != int(
            expected_version
        ):
            raise StaleTaskVersion(
                expected_version=int(expected_version),
                actual_version=task.row_version,
            )
        deliverable = await self.repository.get_deliverable(
            session, deliverable.id, for_update=True
        )
        version = await self.repository.get_deliverable_version(
            session, version_id, for_update=True
        )
        if deliverable is None or version is None:
            raise LookupError("deliverable_not_found")
        return deliverable, task, version, actor_identity, None

    async def _version_from_event(
        self,
        session: AsyncSession,
        task: WorkgroupTask,
        idempotency_key: str | None,
    ) -> TaskDeliverableVersion:
        assert idempotency_key is not None
        previous = await self.repository.get_event_by_idempotency_key(
            session, task.id, idempotency_key
        )
        assert previous is not None
        version_id = uuid.UUID(str(previous.payload["version_id"]))
        version = await self.repository.get_deliverable_version(
            session, version_id
        )
        if version is None:
            raise LookupError("deliverable_version_not_found")
        return version

    async def _publication_from_event(
        self,
        session: AsyncSession,
        task: WorkgroupTask,
        idempotency_key: str | None,
    ) -> TaskDeliverablePublication:
        assert idempotency_key is not None
        previous = await self.repository.get_event_by_idempotency_key(
            session, task.id, idempotency_key
        )
        assert previous is not None
        publication_id = uuid.UUID(str(previous.payload["publication_id"]))
        publication = await self.repository.get_publication(
            session, publication_id
        )
        if publication is None:
            raise LookupError("deliverable_publication_not_found")
        return publication

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

    async def _confirming_role(
        self,
        session: AsyncSession,
        task: WorkgroupTask,
        actor: _Actor,
    ) -> str:
        if actor.role == "superadmin":
            return "superadmin"
        authority = await self.roster_service.resolve_confirming_authority(
            session, task.event_id, task.workgroup_code
        )
        if authority is None or authority.user_id != actor.user_id:
            raise PermissionError("Insufficient permissions")
        return authority.role.value

async def _validate_version_for_publication(
    session: AsyncSession,
    version: TaskDeliverableVersion,
    store: ArtifactStore,
) -> None:
    if version.storage_key is not None:
        path = store.resolve(version.storage_key)
        if not path.is_file():
            raise ValueError("deliverable storage object is missing")
        if version.checksum != _sha256_file(path):
            raise ValueError("deliverable checksum does not match stored object")
        return
    if version.text_result is not None:
        return
    if version.artifact_id is not None:
        from app.artifacts.models import GeneratedArtifact

        artifact = await session.get(GeneratedArtifact, version.artifact_id)
        if artifact is None:
            raise ValueError("automatic artifact is missing")
        path = store.resolve(artifact.storage_path)
        if not path.is_file():
            raise ValueError("deliverable storage object is missing")
        if version.checksum is not None and version.checksum != _sha256_file(path):
            raise ValueError("deliverable checksum does not match stored object")
        if artifact.checksum != _sha256_file(path):
            raise ValueError("automatic artifact checksum does not match")
        return
    raise ValueError("deliverable version has no publishable content")


def _bump_task_version(task: WorkgroupTask, *, now: datetime) -> None:
    task.row_version += 1
    task.updated_at = now


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _event_key(task_id: uuid.UUID, event_seq: int) -> str:
    material = f"{task_id}:{event_seq}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
