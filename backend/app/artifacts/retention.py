from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactPublication, GeneratedArtifact, ProductionRun
from app.event_object_cleanup import ObjectCleanupIntent, ObjectCleanupResult
from app.event_object_locks import (
    lock_artifact_object,
    lock_event_write,
    storage_path_reference_count,
)


@dataclass(frozen=True, slots=True)
class ArtifactRetentionResult:
    test_deleted: int = 0
    drill_deleted: int = 0
    live_deleted: int = 0
    manual_deleted: int = 0
    replay_deleted: int = 0
    failed_deletions: int = 0
    protected_publication_count: int = 0
    protected_publication_reasons: tuple[tuple[str, int], ...] = ()
    cleanup_intents: tuple[ObjectCleanupIntent, ...] = ()

    def with_cleanup_result(
        self,
        cleanup_result: ObjectCleanupResult,
    ) -> "ArtifactRetentionResult":
        return replace(
            self,
            failed_deletions=len(cleanup_result.failed_paths),
        )


class ArtifactRetentionService:
    """Delete expired test/drill artifacts without touching live or manual output."""

    async def retain_expired(
        self,
        session: AsyncSession,
        observed_at: datetime,
    ) -> ArtifactRetentionResult:
        observed_at = _as_utc(observed_at)
        thresholds = (
            ("test", observed_at - timedelta(days=400)),
            ("drill", observed_at - timedelta(days=730)),
        )
        expired: list[GeneratedArtifact] = []
        for production_mode, cutoff in thresholds:
            rows = (
                await session.scalars(
                    select(GeneratedArtifact)
                    .join(
                        ProductionRun,
                        ProductionRun.id == GeneratedArtifact.production_run_id,
                    )
                    .where(
                        ProductionRun.production_mode == production_mode,
                        GeneratedArtifact.generated_at < cutoff,
                    )
                    .order_by(GeneratedArtifact.id)
                )
            ).all()
            expired.extend(rows)

        expired_by_id = {artifact.id: artifact for artifact in expired}
        if not expired_by_id:
            return ArtifactRetentionResult()

        expired_paths = {artifact.storage_path for artifact in expired}
        for event_id in sorted(
            {artifact.event_id for artifact in expired},
            key=str,
        ):
            await lock_event_write(session, event_id)
        for storage_path in sorted(expired_paths):
            await lock_artifact_object(session, storage_path)

        current_expired = (
            await session.scalars(
                select(GeneratedArtifact)
                .where(GeneratedArtifact.id.in_(list(expired_by_id)))
                .order_by(GeneratedArtifact.id)
                .with_for_update()
            )
        ).all()
        current_expired_by_id = {artifact.id: artifact for artifact in current_expired}
        if not current_expired_by_id:
            return ArtifactRetentionResult()

        path_artifacts = (
            await session.scalars(
                select(GeneratedArtifact)
                .where(GeneratedArtifact.storage_path.in_(expired_paths))
                .order_by(GeneratedArtifact.id)
                .with_for_update()
            )
        ).all()
        path_artifact_ids = [artifact.id for artifact in path_artifacts]
        publication_references = (
            await session.execute(
                select(
                    ArtifactPublication.artifact_id,
                    ArtifactPublication.superseded_at,
                ).where(ArtifactPublication.artifact_id.in_(path_artifact_ids))
            )
        ).all()
        protected_ids = {artifact_id for artifact_id, _superseded_at in publication_references}
        protection_reasons = {
            "current_publication": sum(
                superseded_at is None for _artifact_id, superseded_at in publication_references
            ),
            "superseded_publication": sum(
                superseded_at is not None for _artifact_id, superseded_at in publication_references
            ),
        }

        deletable: list[GeneratedArtifact] = []
        cleanup_owners: dict[str, uuid.UUID] = {}
        for storage_path in sorted(expired_paths):
            path_deletable = [
                artifact
                for artifact in path_artifacts
                if artifact.storage_path == storage_path
                and artifact.id in current_expired_by_id
                and artifact.id not in protected_ids
            ]
            if not path_deletable:
                continue
            deletable.extend(path_deletable)
            cleanup_owners[storage_path] = sorted(
                {artifact.event_id for artifact in path_deletable},
                key=str,
            )[0]

        if deletable:
            ids = [artifact.id for artifact in deletable]
            await session.execute(
                update(GeneratedArtifact)
                .where(GeneratedArtifact.superseded_by_id.in_(ids))
                .values(superseded_by_id=None)
            )
            for artifact in deletable:
                await session.delete(artifact)
            await session.flush()

        cleanup_by_event: dict[uuid.UUID, set[str]] = {}
        for storage_path, event_id in cleanup_owners.items():
            if await storage_path_reference_count(session, storage_path) == 0:
                cleanup_by_event.setdefault(event_id, set()).add(storage_path)

        return ArtifactRetentionResult(
            test_deleted=sum(1 for artifact in deletable if artifact.production_mode == "test"),
            drill_deleted=sum(1 for artifact in deletable if artifact.production_mode == "drill"),
            live_deleted=0,
            manual_deleted=0,
            replay_deleted=0,
            failed_deletions=0,
            protected_publication_count=len(publication_references),
            protected_publication_reasons=tuple(
                (reason, count) for reason, count in protection_reasons.items() if count
            ),
            cleanup_intents=tuple(
                ObjectCleanupIntent(event_id, tuple(sorted(paths)))
                for event_id, paths in sorted(
                    cleanup_by_event.items(),
                    key=lambda item: str(item[0]),
                )
            ),
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)
