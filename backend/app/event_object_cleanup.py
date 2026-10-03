from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.storage import ArtifactStore, StoredArtifactFile
from app.event_object_locks import (
    lock_artifact_object,
    lock_event_write,
    unreferenced_storage_paths,
)


@dataclass(frozen=True, slots=True)
class ObjectCleanupIntent:
    event_id: uuid.UUID
    storage_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "storage_paths",
            tuple(sorted({path for path in self.storage_paths if path})),
        )


@dataclass(frozen=True, slots=True)
class ObjectCleanupResult:
    deleted_paths: tuple[str, ...] = ()
    retained_paths: tuple[str, ...] = ()
    failed_paths: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.failed_paths


async def cleanup_object_intents(
    session: AsyncSession,
    intents: tuple[ObjectCleanupIntent, ...] | list[ObjectCleanupIntent],
    artifact_store: ArtifactStore,
    *,
    exclusive_event_locks: bool = False,
) -> ObjectCleanupResult:
    normalized = tuple(intent for intent in intents if intent.storage_paths)
    if not normalized:
        return ObjectCleanupResult()

    event_ids = sorted({intent.event_id for intent in normalized}, key=str)
    candidate_paths = sorted({path for intent in normalized for path in intent.storage_paths})

    # Lock every event before any object so multi-event cleanup cannot deadlock
    # with writers that acquire event then object locks.
    for event_id in event_ids:
        await lock_event_write(
            session,
            event_id,
            exclusive=exclusive_event_locks,
        )
    for storage_path in candidate_paths:
        await lock_artifact_object(session, storage_path)

    unreferenced = set(await unreferenced_storage_paths(session, candidate_paths))
    retained = tuple(path for path in candidate_paths if path not in unreferenced)
    deleted: list[str] = []
    failed: list[str] = []
    for storage_path in candidate_paths:
        if storage_path not in unreferenced:
            continue
        try:
            artifact_store.delete_unreferenced(
                StoredArtifactFile(
                    file_name=Path(storage_path).name,
                    relative_path=storage_path,
                    managed_path=artifact_store.resolve(storage_path),
                    size_bytes=0,
                    checksum="",
                )
            )
        except FileNotFoundError:
            deleted.append(storage_path)
        except (OSError, ValueError):
            failed.append(storage_path)
        else:
            deleted.append(storage_path)
    return ObjectCleanupResult(
        deleted_paths=tuple(deleted),
        retained_paths=retained,
        failed_paths=tuple(failed),
    )


async def cleanup_event_objects(
    session: AsyncSession,
    event_id: uuid.UUID,
    storage_paths: tuple[str, ...] | list[str],
    artifact_store: ArtifactStore,
    *,
    exclusive_event_lock: bool = False,
) -> ObjectCleanupResult:
    return await cleanup_object_intents(
        session,
        (ObjectCleanupIntent(event_id, tuple(storage_paths)),),
        artifact_store,
        exclusive_event_locks=exclusive_event_lock,
    )
