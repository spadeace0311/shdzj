from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ProductionRun
from app.config import settings
from app.data_assets.models import DataAssetSnapshot
from app.data_assets.locks import lock_data_asset_catalog
from app.events.models import EarthquakeEvent
from app.knowledge.models import KnowledgeIndexVersion, KnowledgeSnapshot
from app.loss.region import load_region_loss_profile


class KnowledgeSnapshotService:
    async def create(
        self,
        session: AsyncSession,
        *,
        event_id: UUID | None = None,
        created_by: str | None = None,
    ) -> KnowledgeSnapshot:
        del created_by
        revision_id: UUID | None = None
        assessment_run_id: UUID | None = None
        artifact_production_run_id: UUID | None = None
        data_asset_versions: list[dict[str, str]] = []
        model_versions: dict[str, str] = {}

        if event_id is not None:
            event = await session.get(
                EarthquakeEvent,
                event_id,
                with_for_update=True,
            )
            if event is None:
                raise LookupError("earthquake event not found")
            revision_id = event.current_revision_id or event.latest_trigger_revision_id
            assessment_run_id = (
                event.effective_assessment_run_id
                or event.latest_assessment_run_id
            )
            await lock_data_asset_catalog(session)
            artifact_production_run_id = await _active_production_run_id(
                session,
                event_id,
                revision_id,
            )
            data_asset_versions = await _data_asset_versions(
                session,
                assessment_run_id,
            )
            model_versions = _model_versions()

        index_version = await _active_index_version(session, event_id)
        manifest = {
            "event_revision_id": (
                str(revision_id) if revision_id is not None else None
            ),
            "assessment_run_id": (
                str(assessment_run_id)
                if assessment_run_id is not None
                else None
            ),
            "artifact_production_run_id": (
                str(artifact_production_run_id)
                if artifact_production_run_id is not None
                else None
            ),
            "data_asset_versions": data_asset_versions,
            "model_versions": model_versions,
            "index_version_id": str(index_version.id),
        }
        fingerprint = _fingerprint(manifest)
        existing = await session.scalar(
            select(KnowledgeSnapshot).where(
                KnowledgeSnapshot.fingerprint == fingerprint
            )
        )
        if existing is not None:
            return existing

        snapshot = KnowledgeSnapshot(
            event_id=event_id,
            revision_id=revision_id,
            assessment_run_id=assessment_run_id,
            artifact_production_run_id=artifact_production_run_id,
            index_version_id=index_version.id,
            manifest=manifest,
            fingerprint=fingerprint,
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    async def get(
        self,
        session: AsyncSession,
        snapshot_id: UUID,
    ) -> KnowledgeSnapshot:
        snapshot = await session.get(KnowledgeSnapshot, snapshot_id)
        if snapshot is None:
            raise LookupError("knowledge snapshot not found")
        return snapshot


async def _active_index_version(
    session: AsyncSession,
    event_id: UUID | None,
) -> KnowledgeIndexVersion:
    rows = list(
        (
            await session.scalars(
                select(KnowledgeIndexVersion)
                .where(KnowledgeIndexVersion.status == "published")
                .order_by(
                    KnowledgeIndexVersion.activated_at.desc(),
                    KnowledgeIndexVersion.id.desc(),
                )
                .with_for_update()
            )
        ).all()
    )
    if not rows:
        raise LookupError("no active knowledge index version")
    if event_id is not None:
        matching = [
            row
            for row in rows
            if str((row.manifest or {}).get("event_id") or "") == str(event_id)
        ]
        if matching:
            return matching[0]
    return rows[0]


async def _active_production_run_id(
    session: AsyncSession,
    event_id: UUID,
    revision_id: UUID | None,
) -> UUID | None:
    statement = select(ProductionRun.id).where(
        ProductionRun.event_id == event_id,
        ProductionRun.is_current.is_(True),
        ProductionRun.superseded_at.is_(None),
    )
    if revision_id is not None:
        statement = statement.where(ProductionRun.revision_id == revision_id)
    statement = statement.order_by(
        ProductionRun.generation_seq.desc(),
        ProductionRun.created_at.desc(),
        ProductionRun.id.desc(),
    )
    return await session.scalar(statement.limit(1).with_for_update())


async def _data_asset_versions(
    session: AsyncSession,
    assessment_run_id: UUID | None,
) -> list[dict[str, str]]:
    if assessment_run_id is None:
        return []
    rows = list(
        (
            await session.scalars(
                select(DataAssetSnapshot)
                .where(DataAssetSnapshot.run_id == assessment_run_id)
                .order_by(
                    DataAssetSnapshot.asset_key,
                    DataAssetSnapshot.id,
                )
                .with_for_update()
            )
        ).all()
    )
    return [
        {
            "asset_key": row.asset_key,
            "version_id": str(row.asset_version_id),
            "checksum": row.checksum or "",
        }
        for row in rows
    ]


def _model_versions() -> dict[str, str]:
    try:
        profile = load_region_loss_profile(settings.loss_region_profile_path)
    except (OSError, ValueError):
        return {}
    return dict(profile.default_model_versions)


def _fingerprint(manifest: dict[str, object]) -> str:
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
