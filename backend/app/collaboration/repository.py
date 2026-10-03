from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.collaboration.models import (
    CollaborationTaskEvent,
    TaskDeliverable,
    TaskDeliverablePublication,
    TaskDeliverableVersion,
    WorkgroupMembership,
    WorkgroupTask,
    WorkgroupTaskContributor,
)


class CollaborationRepository:
    async def get_task(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> WorkgroupTask | None:
        statement = select(WorkgroupTask).where(WorkgroupTask.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def list_tasks(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> tuple[WorkgroupTask, ...]:
        rows = await session.scalars(
            select(WorkgroupTask)
            .where(WorkgroupTask.event_id == event_id)
            .order_by(WorkgroupTask.priority.desc(), WorkgroupTask.created_at)
        )
        return tuple(rows)

    async def get_user_by_id(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> User | None:
        return await session.get(User, user_id)

    async def get_user_by_username(
        self,
        session: AsyncSession,
        username: str,
    ) -> User | None:
        return await session.scalar(
            select(User).where(
                User.username == username,
                User.is_active.is_(True),
            )
        )

    async def get_active_membership(
        self,
        session: AsyncSession,
        group_code: str,
        user_id: uuid.UUID,
    ) -> WorkgroupMembership | None:
        return await session.scalar(
            select(WorkgroupMembership).where(
                WorkgroupMembership.user_id == user_id,
                WorkgroupMembership.workgroup_code == group_code,
                WorkgroupMembership.is_active.is_(True),
                WorkgroupMembership.effective_to.is_(None),
            )
        )

    async def list_active_memberships_for_user(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> tuple[WorkgroupMembership, ...]:
        rows = await session.scalars(
            select(WorkgroupMembership)
            .where(
                WorkgroupMembership.user_id == user_id,
                WorkgroupMembership.is_active.is_(True),
                WorkgroupMembership.effective_to.is_(None),
            )
            .order_by(WorkgroupMembership.workgroup_code)
        )
        return tuple(rows)

    async def upsert_contributor(
        self,
        session: AsyncSession,
        *,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        now: datetime,
    ) -> WorkgroupTaskContributor:
        contributor = await session.scalar(
            select(WorkgroupTaskContributor).where(
                WorkgroupTaskContributor.task_id == task_id,
                WorkgroupTaskContributor.user_id == user_id,
            )
        )
        if contributor is None:
            contributor = WorkgroupTaskContributor(
                task_id=task_id,
                user_id=user_id,
                contribution_count=1,
                first_contributed_at=now,
                last_contributed_at=now,
            )
            session.add(contributor)
        else:
            contributor.contribution_count += 1
            contributor.last_contributed_at = now
        await session.flush()
        return contributor

    async def list_contributors(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
    ) -> tuple[tuple[WorkgroupTaskContributor, User], ...]:
        rows = await session.execute(
            select(WorkgroupTaskContributor, User)
            .join(User, User.id == WorkgroupTaskContributor.user_id)
            .where(WorkgroupTaskContributor.task_id == task_id)
            .order_by(WorkgroupTaskContributor.first_contributed_at)
        )
        return tuple(
            (contributor, user) for contributor, user in rows.all()
        )

    async def next_event_sequence(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
    ) -> int:
        value = await session.scalar(
            select(func.max(CollaborationTaskEvent.event_seq)).where(
                CollaborationTaskEvent.task_id == task_id
            )
        )
        return int(value or 0) + 1

    async def get_event_by_idempotency_key(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        idempotency_key: str,
    ) -> CollaborationTaskEvent | None:
        return await session.scalar(
            select(CollaborationTaskEvent).where(
                CollaborationTaskEvent.task_id == task_id,
                CollaborationTaskEvent.idempotency_key == idempotency_key,
            )
        )

    async def get_event_by_idempotency_key_any(
        self,
        session: AsyncSession,
        idempotency_key: str,
    ) -> CollaborationTaskEvent | None:
        return await session.scalar(
            select(CollaborationTaskEvent)
            .where(
                CollaborationTaskEvent.idempotency_key == idempotency_key,
            )
            .order_by(CollaborationTaskEvent.occurred_at)
            .limit(1)
        )

    async def required_deliverables_satisfied(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
    ) -> bool:
        required_ids = tuple(
            await session.scalars(
                select(TaskDeliverable.id).where(
                    TaskDeliverable.task_id == task_id,
                    TaskDeliverable.is_required.is_(True),
                )
            )
        )
        for deliverable_id in required_ids:
            published = await session.scalar(
                select(TaskDeliverablePublication.id)
                .join(
                    TaskDeliverableVersion,
                    TaskDeliverableVersion.id
                    == TaskDeliverablePublication.version_id,
                )
                .where(
                    TaskDeliverableVersion.deliverable_id == deliverable_id,
                    TaskDeliverablePublication.superseded_at.is_(None),
                )
                .limit(1)
            )
            if published is not None:
                continue
            latest_version = await session.scalar(
                select(TaskDeliverableVersion)
                .where(
                    TaskDeliverableVersion.deliverable_id == deliverable_id,
                )
                .order_by(TaskDeliverableVersion.version_no.desc())
                .limit(1)
            )
            if (
                latest_version is None
                or latest_version.text_result is None
            ):
                return False
        return True

    async def get_deliverable(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> TaskDeliverable | None:
        statement = select(TaskDeliverable).where(
            TaskDeliverable.id == deliverable_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_deliverable_version(
        self,
        session: AsyncSession,
        version_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> TaskDeliverableVersion | None:
        statement = select(TaskDeliverableVersion).where(
            TaskDeliverableVersion.id == version_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def next_deliverable_version_no(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
    ) -> int:
        value = await session.scalar(
            select(func.max(TaskDeliverableVersion.version_no)).where(
                TaskDeliverableVersion.deliverable_id == deliverable_id
            )
        )
        return int(value or 0) + 1

    async def latest_deliverable_version(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> TaskDeliverableVersion | None:
        statement = (
            select(TaskDeliverableVersion)
            .where(TaskDeliverableVersion.deliverable_id == deliverable_id)
            .order_by(TaskDeliverableVersion.version_no.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def list_deliverable_versions(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
    ) -> tuple[TaskDeliverableVersion, ...]:
        rows = await session.scalars(
            select(TaskDeliverableVersion)
            .where(TaskDeliverableVersion.deliverable_id == deliverable_id)
            .order_by(TaskDeliverableVersion.version_no)
        )
        return tuple(rows)

    async def list_task_deliverables(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
    ) -> tuple[TaskDeliverable, ...]:
        rows = await session.scalars(
            select(TaskDeliverable)
            .where(TaskDeliverable.task_id == task_id)
            .order_by(TaskDeliverable.display_order, TaskDeliverable.created_at)
        )
        return tuple(rows)

    async def get_task_for_deliverable(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> WorkgroupTask | None:
        statement = (
            select(WorkgroupTask)
            .join(TaskDeliverable, TaskDeliverable.task_id == WorkgroupTask.id)
            .where(TaskDeliverable.id == deliverable_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_current_publication(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> TaskDeliverablePublication | None:
        statement = (
            select(TaskDeliverablePublication)
            .where(
                TaskDeliverablePublication.deliverable_id == deliverable_id,
                TaskDeliverablePublication.superseded_at.is_(None),
            )
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_publication(
        self,
        session: AsyncSession,
        publication_id: uuid.UUID,
    ) -> TaskDeliverablePublication | None:
        return await session.get(TaskDeliverablePublication, publication_id)

    async def publication_for_version(
        self,
        session: AsyncSession,
        version_id: uuid.UUID,
    ) -> TaskDeliverablePublication | None:
        return await session.scalar(
            select(TaskDeliverablePublication).where(
                TaskDeliverablePublication.version_id == version_id
            ).limit(1)
        )

    async def current_version_id(
        self,
        session: AsyncSession,
        deliverable_id: uuid.UUID,
    ) -> uuid.UUID | None:
        return await session.scalar(
            select(TaskDeliverablePublication.version_id)
            .where(
                TaskDeliverablePublication.deliverable_id == deliverable_id,
                TaskDeliverablePublication.superseded_at.is_(None),
            )
            .limit(1)
        )

    async def storage_key_reference_count(
        self,
        session: AsyncSession,
        storage_key: str,
        *,
        exclude_version_id: uuid.UUID,
    ) -> int:
        version_count = int(
            await session.scalar(
                select(func.count())
                .select_from(TaskDeliverableVersion)
                .where(
                    TaskDeliverableVersion.storage_key == storage_key,
                    TaskDeliverableVersion.id != exclude_version_id,
                )
            )
            or 0
        )
        from app.artifacts.models import GeneratedArtifact

        artifact_count = int(
            await session.scalar(
                select(func.count())
                .select_from(GeneratedArtifact)
                .where(GeneratedArtifact.storage_path == storage_key)
            )
            or 0
        )
        return version_count + artifact_count
