from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactPublication, GeneratedArtifact, ProductionRun
from app.event_object_cleanup import (
    ARTIFACT_RETENTION_CLEANUP_SOURCE,
    enqueue_object_cleanup_intent,
)
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

        for artifact in deletable:
            if await storage_path_reference_count(session, artifact.storage_path) == 0:
                await enqueue_object_cleanup_intent(
                    session,
                    event_id=artifact.event_id,
                    source_kind=ARTIFACT_RETENTION_CLEANUP_SOURCE,
                    source_key=str(artifact.id),
                    storage_path=artifact.storage_path,
                )

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
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)
