from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import GeneratedArtifact
from app.collaboration.models import TaskDeliverableVersion


EVENT_WRITE_LOCK_PREFIX = "event-object-write"
ARTIFACT_OBJECT_LOCK_PREFIX = "artifact-object"


async def lock_event_write(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    exclusive: bool = False,
) -> None:
    lock_function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await session.execute(
        text(f"SELECT {lock_function}(" "hashtextextended(:lock_key, 0))"),
        {
            "lock_key": f"{EVENT_WRITE_LOCK_PREFIX}:{event_id}",
        },
    )


async def lock_artifact_object(
    session: AsyncSession,
    relative_path: str,
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(" "hashtextextended(:lock_key, 0))"),
        {
            "lock_key": f"{ARTIFACT_OBJECT_LOCK_PREFIX}:{relative_path}",
        },
    )


async def lock_event_and_artifact_object(
    session: AsyncSession,
    event_id: uuid.UUID,
    relative_path: str,
) -> None:
    await lock_event_write(session, event_id)
    await lock_artifact_object(session, relative_path)


async def lock_purge_event_objects(
    session: AsyncSession,
    event_id: uuid.UUID,
    relative_paths: Iterable[str],
) -> None:
    await lock_event_write(session, event_id, exclusive=True)
    for relative_path in sorted(set(relative_paths)):
        await lock_artifact_object(session, relative_path)


async def storage_path_reference_count(
    session: AsyncSession,
    relative_path: str,
) -> int:
    artifact_count = int(
        await session.scalar(
            select(func.count())
            .select_from(GeneratedArtifact)
            .where(GeneratedArtifact.storage_path == relative_path)
        )
        or 0
    )
    deliverable_count = int(
        await session.scalar(
            select(func.count())
            .select_from(TaskDeliverableVersion)
            .where(TaskDeliverableVersion.storage_key == relative_path)
        )
        or 0
    )
    return artifact_count + deliverable_count


async def unreferenced_storage_paths(
    session: AsyncSession,
    candidate_paths: Iterable[str],
) -> tuple[str, ...]:
    unreferenced: list[str] = []
    for relative_path in sorted(set(candidate_paths)):
        if await storage_path_reference_count(session, relative_path) == 0:
            unreferenced.append(relative_path)
    return tuple(unreferenced)
