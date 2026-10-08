from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from geoalchemy2.shape import WKTElement
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.artifacts.models import ProductionRun
from app.assessment.models import AssessmentRun
from app.config import settings
from app.data_assets.models import (
    DataAsset,
    DataAssetSnapshot,
    DataAssetVersion,
)
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.knowledge.models import (
    KnowledgeIndexVersion,
    KnowledgeSnapshot,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.knowledge.snapshot import KnowledgeSnapshotService
from app.loss.region import load_region_loss_profile


SNAPSHOT_ACTOR = "knowledge-snapshot-test"


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_global_snapshot_uses_active_index_and_empty_event_context(
    session_factory,
) -> None:
    try:
        index_version_id = await _seed_index(session_factory)

        snapshot = await _create_snapshot(session_factory)
        stored = await _get_snapshot(session_factory, snapshot.id)

        assert stored is not None
        assert stored.event_id is None
        assert stored.revision_id is None
        assert stored.assessment_run_id is None
        assert stored.artifact_production_run_id is None
        assert stored.index_version_id == index_version_id
        assert stored.manifest == {
            "event_revision_id": None,
            "assessment_run_id": None,
            "artifact_production_run_id": None,
            "data_asset_versions": [],
            "model_versions": {},
            "index_version_id": str(index_version_id),
        }
        assert len(stored.fingerprint) == 64
    finally:
        await _delete_actor_data(session_factory)


async def test_event_snapshot_locks_context_and_is_idempotent(
    session_factory,
) -> None:
    try:
        seeded = await _seed_event_graph(session_factory)
        index_version_id = await _seed_index(
            session_factory,
            source_key=f"event.{uuid4()}",
            event_id=seeded["event_id"],
        )

        first = await _create_snapshot(
            session_factory,
            event_id=seeded["event_id"],
            created_by=SNAPSHOT_ACTOR,
        )
        second = await _create_snapshot(
            session_factory,
            event_id=seeded["event_id"],
            created_by=SNAPSHOT_ACTOR,
        )

        assert first.id == second.id
        assert first.event_id == seeded["event_id"]
        assert first.revision_id == seeded["revision_id"]
        assert first.assessment_run_id == seeded["assessment_run_id"]
        assert first.artifact_production_run_id == seeded["production_run_id"]
        assert first.index_version_id == index_version_id

        data_assets = first.manifest["data_asset_versions"]
        assert data_assets == [
            {
                "asset_key": "loss.parameters",
                "version_id": str(seeded["data_asset_version_id"]),
                "checksum": "d" * 64,
            }
        ]
        expected_models = load_region_loss_profile(
            settings.loss_region_profile_path
        ).default_model_versions
        assert first.manifest["model_versions"] == expected_models
        assert first.manifest["event_revision_id"] == str(seeded["revision_id"])
        assert first.manifest["assessment_run_id"] == str(
            seeded["assessment_run_id"]
        )
        assert first.manifest["artifact_production_run_id"] == str(
            seeded["production_run_id"]
        )
        assert first.manifest["index_version_id"] == str(index_version_id)
    finally:
        await _delete_actor_data(session_factory)


async def test_snapshot_requires_active_index_version(session_factory) -> None:
    with pytest.raises(LookupError, match="index"):
        await _create_snapshot(session_factory)


async def _create_snapshot(session_factory, **kwargs):
    async with session_factory() as session:
        async with session.begin():
            return await KnowledgeSnapshotService().create(session, **kwargs)


async def _get_snapshot(session_factory, snapshot_id: UUID):
    async with session_factory() as session:
        return await KnowledgeSnapshotService().get(session, snapshot_id)


async def _seed_index(
    session_factory,
    *,
    source_key: str | None = None,
    event_id: UUID | None = None,
) -> UUID:
    async with session_factory() as session:
        async with session.begin():
            source = KnowledgeSource(
                source_key=source_key or f"snapshot.{uuid4()}",
                title="Snapshot Source",
                layer="local_authority",
                source_type="preplan",
                access_level="internal",
                created_by=SNAPSHOT_ACTOR,
            )
            session.add(source)
            await session.flush()
            version = KnowledgeSourceVersion(
                source_id=source.id,
                version="v1",
                status="indexed",
                checksum="a" * 64,
                created_by=SNAPSHOT_ACTOR,
                version_metadata={"event_id": str(event_id)} if event_id else {},
            )
            session.add(version)
            await session.flush()
            manifest = {
                "source_title": source.title,
                "published_at": datetime.now(UTC).isoformat(),
            }
            if event_id is not None:
                manifest["event_id"] = str(event_id)
            index_version = KnowledgeIndexVersion(
                source_version_id=version.id,
                version="v1",
                status="published",
                collection_name=(
                    f"{settings.qdrant_collection_prefix}-{source.source_key}"
                ),
                embedding_model=settings.embedding_model_name,
                reranker_model=settings.reranker_model_name,
                chunk_count=1,
                manifest=manifest,
                activated_at=datetime.now(UTC),
            )
            session.add(index_version)
            await session.flush()
            return index_version.id


async def _seed_event_graph(session_factory) -> dict[str, UUID]:
    async with session_factory() as session:
        async with session.begin():
            now = datetime.now(UTC)
            raw = RawMessage(
                source="snapshot-test",
                source_message_id=f"snapshot-{uuid4()}",
                message_kind="test",
                checksum=uuid4().hex + uuid4().hex,
                payload={},
                received_at=now,
            )
            event = EarthquakeEvent(
                source="snapshot-test",
                canonical_source_id=f"snapshot-{uuid4()}",
                event_type="formal",
                origin_time=now - timedelta(minutes=2),
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="snapshot test",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                lifecycle_state="active",
            )
            session.add_all([raw, event])
            await session.flush()

            revision = EarthquakeRevision(
                event_id=event.id,
                raw_message_id=raw.id,
                revision_no=1,
                revision_kind="formal",
                origin_time=event.origin_time,
                longitude=event.longitude,
                latitude=event.latitude,
                depth_km=event.depth_km,
                magnitude=event.magnitude,
                place=event.place,
                is_current=True,
            )
            session.add(revision)
            await session.flush()
            event.current_revision_id = revision.id

            outbox = EventLifecycleOutbox(
                event_id=event.id,
                revision_id=revision.id,
                trigger_type="snapshot-test",
                trigger_reason="live",
                payload={},
                status="published",
                created_at=now,
                available_at=now,
                published_at=now,
            )
            session.add(outbox)
            await session.flush()

            assessment = AssessmentRun(
                event_id=event.id,
                revision_id=revision.id,
                outbox_id=outbox.id,
                run_no=1,
                trigger_reason="live",
                status="running",
                deadline_at=now + timedelta(seconds=300),
                snapshot={},
                report_ingested_at=now,
                deadline_basis_at=now,
            )
            session.add(assessment)
            await session.flush()
            event.latest_assessment_run_id = assessment.id
            event.effective_assessment_run_id = assessment.id
            await session.flush()

            production = ProductionRun(
                assessment_run_id=assessment.id,
                event_id=event.id,
                revision_id=revision.id,
                revision_no=1,
                production_mode="live",
                launch_mode="assessment_child",
                status="running",
                deadline_basis_at=now,
                deadline_at=now + timedelta(seconds=300),
                deadline_kind="event_deadline",
                catalog_version="v1",
                generation_seq=1,
                generation_scope=f"snapshot-{event.id}",
                required_outputs=[],
                is_current=True,
            )
            session.add(production)
            await session.flush()

            asset = DataAsset(
                asset_key="loss.parameters",
                region_id=settings.data_asset_region_id,
                name="Loss Parameters",
                data_type="parameter_file",
                spatial_granularity="region",
                responsibility_unit="test",
                update_interval_days=30,
                is_core=True,
                contract={},
            )
            session.add(asset)
            await session.flush()
            asset_version = DataAssetVersion(
                asset_id=asset.id,
                version="v1",
                status="published",
                source_uri="upload://loss-parameters",
                checksum="d" * 64,
                schema_summary={},
                record_count=0,
                source_crs="EPSG:4326",
                imported_by=SNAPSHOT_ACTOR,
            )
            session.add(asset_version)
            await session.flush()
            data_snapshot = DataAssetSnapshot(
                run_id=assessment.id,
                asset_id=asset.id,
                asset_version_id=asset_version.id,
                region_id=settings.data_asset_region_id,
                asset_key="loss.parameters",
                version="v1",
                checksum="d" * 64,
                role="required",
                required=True,
            )
            session.add(data_snapshot)
            await session.flush()

            return {
                "event_id": event.id,
                "revision_id": revision.id,
                "assessment_run_id": assessment.id,
                "production_run_id": production.id,
                "data_asset_version_id": asset_version.id,
            }


async def _delete_actor_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(KnowledgeSnapshot))
            await session.execute(
                delete(KnowledgeSourceVersion).where(
                    KnowledgeSourceVersion.created_by == SNAPSHOT_ACTOR
                )
            )
            await session.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.created_by == SNAPSHOT_ACTOR
                )
            )
            await session.execute(
                delete(DataAssetVersion).where(
                    DataAssetVersion.imported_by == SNAPSHOT_ACTOR
                )
            )
            await session.execute(
                delete(DataAsset).where(DataAsset.responsibility_unit == "test")
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.source == "snapshot-test"
                )
            )
            await session.execute(
                delete(RawMessage).where(RawMessage.source == "snapshot-test")
            )
