from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactPublication, GeneratedArtifact, ProductionRun
from app.artifacts.storage import ArtifactStore, StoredArtifactFile
from app.config import settings


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

    def __init__(self, store: ArtifactStore | None = None) -> None:
        self._store = store

    @property
    def _artifact_store(self) -> ArtifactStore:
        if self._store is None:
            self._store = ArtifactStore(settings.artifact_storage_root)
        return self._store

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

        expired_ids = [artifact.id for artifact in expired]
        publication_references = (
            await session.execute(
                select(
                    ArtifactPublication.artifact_id,
                    ArtifactPublication.superseded_at,
                ).where(
                    ArtifactPublication.artifact_id.in_(expired_ids)
                )
            )
        ).all()
        protected_ids = {
            artifact_id for artifact_id, _superseded_at in publication_references
        }
        protection_reasons = {
            "current_publication": sum(
                superseded_at is None
                for _artifact_id, superseded_at in publication_references
            ),
            "superseded_publication": sum(
                superseded_at is not None
                for _artifact_id, superseded_at in publication_references
            ),
        }

        deletable: list[GeneratedArtifact] = []
        failed_deletions = 0
        for artifact in expired:
            if artifact.id in protected_ids:
                continue
            if not await self._delete_object(artifact):
                failed_deletions += 1
                continue
            deletable.append(artifact)

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

        test_deleted = sum(
            1
            for artifact in deletable
            if artifact.production_mode == "test"
        )
        drill_deleted = sum(
            1
            for artifact in deletable
            if artifact.production_mode == "drill"
        )
        return ArtifactRetentionResult(
            test_deleted=test_deleted,
            drill_deleted=drill_deleted,
            live_deleted=0,
            manual_deleted=0,
            replay_deleted=0,
            failed_deletions=failed_deletions,
            protected_publication_count=len(publication_references),
            protected_publication_reasons=tuple(
                (reason, count)
                for reason, count in protection_reasons.items()
                if count
            ),
        )

    async def _delete_object(self, artifact: GeneratedArtifact) -> bool:
        try:
            path = self._artifact_store.resolve(artifact.storage_path)
            self._artifact_store.delete_unreferenced(
                StoredArtifactFile(
                    file_name=artifact.file_name,
                    relative_path=artifact.storage_path,
                    managed_path=path,
                    size_bytes=artifact.size_bytes,
                    checksum=artifact.checksum,
                )
            )
        except (OSError, ValueError):
            return False
        return True
def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)
