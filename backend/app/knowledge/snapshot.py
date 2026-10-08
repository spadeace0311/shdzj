from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ProductionRun
from app.config import settings
from app.data_assets.models import DataAssetSnapshot
from app.data_assets.locks import lock_data_asset_catalog
from app.events.models import EarthquakeEvent
from app.knowledge.models import (
    KnowledgeIndexVersion,
    KnowledgeSnapshot,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.loss.region import load_region_loss_profile

_INDEX_VERSION_IDS_KEY = "index_version_ids"
_LEGACY_INDEX_VERSION_ID_KEY = "index_version_id"
_KNOWLEDGE_INDEX_VERSIONS_KEY = "knowledge_index_versions"


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

        index_versions = await _active_index_versions(session, event_id)
        index_version = index_versions[0][0]
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
            "index_version_ids": [
                str(stored_index.id)
                for stored_index, _stored_version, _stored_source
                in index_versions
            ],
            "knowledge_index_versions": _knowledge_index_versions(
                index_versions,
            ),
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


def locked_index_version_ids(
    manifest: dict[str, object] | None,
    legacy_index_version_id: UUID | None,
) -> tuple[UUID, ...]:
    values: list[object] = []
    stored_manifest = manifest or {}
    candidate = stored_manifest.get(_INDEX_VERSION_IDS_KEY)
    if isinstance(candidate, list):
        values = list(candidate)
    else:
        details = stored_manifest.get(_KNOWLEDGE_INDEX_VERSIONS_KEY)
        if isinstance(details, list):
            values = [
                item.get("index_version_id")
                for item in details
                if isinstance(item, Mapping)
            ]
    if not values:
        values = [stored_manifest.get(_LEGACY_INDEX_VERSION_ID_KEY)]

    parsed: list[UUID] = []
    for value in values:
        try:
            parsed_value = value if isinstance(value, UUID) else UUID(str(value))
        except (TypeError, ValueError):
            continue
        if parsed_value not in parsed:
            parsed.append(parsed_value)

    if not parsed and legacy_index_version_id is not None:
        parsed.append(legacy_index_version_id)
    return tuple(parsed)


async def _active_index_versions(
    session: AsyncSession,
    event_id: UUID | None,
) -> list[
    tuple[
        KnowledgeIndexVersion,
        KnowledgeSourceVersion,
        KnowledgeSource,
    ]
]:
    rows = list(
        (
            await session.execute(
                select(
                    KnowledgeIndexVersion,
                    KnowledgeSourceVersion,
                    KnowledgeSource,
                )
                .join(
                    KnowledgeSourceVersion,
                    KnowledgeIndexVersion.source_version_id
                    == KnowledgeSourceVersion.id,
                )
                .join(
                    KnowledgeSource,
                    KnowledgeSourceVersion.source_id == KnowledgeSource.id,
                )
                .where(
                    KnowledgeIndexVersion.status == "published",
                    KnowledgeSourceVersion.status == "published",
                    KnowledgeSource.is_active.is_(True),
                )
                .order_by(
                    KnowledgeSource.source_key,
                    KnowledgeSourceVersion.version,
                    KnowledgeIndexVersion.id,
                )
                .with_for_update()
            )
        ).all()
    )
    if not rows:
        raise LookupError("no active knowledge index version")

    matching: list[
        tuple[
            KnowledgeIndexVersion,
            KnowledgeSourceVersion,
            KnowledgeSource,
        ]
    ] = []
    for index_version, source_version, source in rows:
        index_event_id = _index_event_id(index_version, source_version)
        if event_id is not None:
            if index_event_id is not None and index_event_id != event_id:
                continue
            if index_event_id is None:
                if source.access_level not in {
                    "public",
                    "internal",
                }:
                    continue
        else:
            if index_event_id is not None:
                continue
            if source.access_level not in {
                "public",
                "internal",
            }:
                continue
        matching.append((index_version, source_version, source))

    if not matching:
        raise LookupError("no active knowledge index version for snapshot scope")
    return matching


def _knowledge_index_versions(
    index_versions: list[
        tuple[
            KnowledgeIndexVersion,
            KnowledgeSourceVersion,
            KnowledgeSource,
        ]
    ],
) -> list[dict[str, object]]:
    return [
        {
            "index_version_id": str(index_version.id),
            "source_version_id": str(index_version.source_version_id),
            "source_key": source.source_key,
            "version": index_version.version,
            "event_id": (
                str(event_id) if event_id is not None else None
            ),
            "chunk_count": int(index_version.chunk_count),
        }
        for index_version, _source_version, source in index_versions
        for event_id in [
            _index_event_id(index_version, _source_version),
        ]
    ]


def _index_event_id(
    index_version: KnowledgeIndexVersion,
    source_version: KnowledgeSourceVersion,
) -> UUID | None:
    manifest_value = (index_version.manifest or {}).get("event_id")
    if manifest_value not in {None, ""}:
        try:
            return UUID(str(manifest_value))
        except (TypeError, ValueError):
            pass

    metadata_value = (source_version.version_metadata or {}).get("event_id")
    if metadata_value in {None, ""}:
        return None
    try:
        return UUID(str(metadata_value))
    except (TypeError, ValueError):
        return None


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
