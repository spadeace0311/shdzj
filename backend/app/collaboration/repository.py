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
