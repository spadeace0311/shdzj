from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete

from app.assessment.models import AssessmentRun
from app.auth.service import AuthUser
from app.data_assets.models import (
    DataAsset,
    DataAssetRecord,
    DataAssetSnapshot,
    DataAssetVersion,
)
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.qa.tools.event import GetEventContextTool, GetEventRevisionTool
from app.qa.tools.fault import FaultNearestTool
from app.qa.tools.region import RegionLookupTool
from app.qa.tools.registry import ToolContext
from app.qa.tools.seismicity import SeismicityDistanceTool, SeismicityWithinRadiusTool


FIXTURE_ACTOR = "qa-spatial-tool-test"


@pytest.fixture(autouse=True)
async def dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def spatial_context(session_factory):
    event_id = uuid4()
    revision_id = uuid4()
    other_event_id = uuid4()
    other_revision_id = uuid4()
    assessment_run_id = uuid4()
    now = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)

    try:
        async with session_factory() as session:
            async with session.begin():
                raw = RawMessage(
                    source=FIXTURE_ACTOR,
                    source_message_id=str(event_id),
                    message_kind="test",
                    checksum=uuid4().hex + uuid4().hex,
                    payload={},
                    received_at=now,
                )
                other_raw = RawMessage(
                    source=FIXTURE_ACTOR,
                    source_message_id=str(other_event_id),
                    message_kind="test",
                    checksum=uuid4().hex + uuid4().hex,
                    payload={},
                    received_at=now,
                )
                session.add_all([raw, other_raw])
                await session.flush()

                event = EarthquakeEvent(
                    id=event_id,
                    source=FIXTURE_ACTOR,
                    canonical_source_id=f"qa-spatial-{event_id}",
                    event_type="formal",
                    origin_time=now - timedelta(minutes=5),
                    longitude=Decimal("121.500000"),
                    latitude=Decimal("31.200000"),
                    depth_km=Decimal("10.00"),
                    magnitude=Decimal("5.2"),
                    place="QA spatial event",
                    geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                    institutional_level="larger",
                    service_level=2,
                    t1_at=now,
                    lifecycle_state="formal_triggered",
                )
                other_event = EarthquakeEvent(
                    id=other_event_id,
                    source=FIXTURE_ACTOR,
                    canonical_source_id=f"qa-spatial-{other_event_id}",
                    event_type="formal",
                    origin_time=now - timedelta(days=1),
                    longitude=Decimal("122.000000"),
                    latitude=Decimal("31.200000"),
                    depth_km=Decimal("10.00"),
                    magnitude=Decimal("4.0"),
                    place="Other QA spatial event",
                    geom=WKTElement("POINT(122.0 31.2)", srid=4326),
                    lifecycle_state="formal_triggered",
                )
                session.add_all([event, other_event])
                await session.flush()

                revision = EarthquakeRevision(
                    id=revision_id,
                    event_id=event.id,
                    raw_message_id=raw.id,
                    revision_no=2,
                    revision_kind="formal",
                    origin_time=event.origin_time,
                    longitude=event.longitude,
                    latitude=event.latitude,
                    depth_km=event.depth_km,
                    magnitude=event.magnitude,
                    place=event.place,
                    institutional_level=event.institutional_level,
                    service_level=event.service_level,
                    is_current=True,
                    ingested_at=now,
                )
                other_revision = EarthquakeRevision(
                    id=other_revision_id,
                    event_id=other_event.id,
                    raw_message_id=other_raw.id,
                    revision_no=1,
                    revision_kind="formal",
                    origin_time=other_event.origin_time,
                    longitude=other_event.longitude,
                    latitude=other_event.latitude,
                    depth_km=other_event.depth_km,
                    magnitude=other_event.magnitude,
                    place=other_event.place,
                    is_current=True,
                    ingested_at=now,
                )
                session.add_all([revision, other_revision])
                await session.flush()
                event.current_revision_id = revision.id
                event.latest_trigger_revision_id = revision.id
                other_event.current_revision_id = other_revision.id
                other_event.latest_trigger_revision_id = other_revision.id

                outbox = EventLifecycleOutbox(
                    event_id=event.id,
                    revision_id=revision.id,
                    trigger_type="assessment.requested",
                    trigger_reason="live",
                    payload={},
                    status="published",
                    created_at=now,
                    available_at=now,
                    published_at=now,
                )
                session.add(outbox)
                await session.flush()
                run = AssessmentRun(
                    id=assessment_run_id,
                    event_id=event.id,
                    revision_id=revision.id,
                    outbox_id=outbox.id,
                    run_no=1,
                    trigger_reason="live",
                    status="completed",
                    deadline_at=now + timedelta(minutes=5),
                    report_ingested_at=now,
                    deadline_basis_at=now,
                    snapshot={},
                )
                session.add(run)
                await session.flush()
                event.latest_assessment_run_id = run.id
                event.effective_assessment_run_id = run.id

                await _publish_asset(
                    session,
                    assessment_run_id=run.id,
                    asset_key="shanghai.fault",
                    records=(
                        (
                            "fault-1",
                            {"OBJECTID": 1, "name": "nearest fault"},
                            "LINESTRING (121.5 31.65, 121.6 31.65)",
                        ),
                    ),
                )
                await _publish_asset(
                    session,
                    assessment_run_id=run.id,
                    asset_key="shanghai.historical.earthquakes",
                    records=(
                        (
                            "hist-1",
                            {
                                "event_id": "hist-1",
                                "origin_time": "1990-01-01T00:00:00+00:00",
                                "magnitude": 4.8,
                                "depth_km": 10.0,
                                "place": "historical fixture",
                                "source": "fixture",
                                "disaster_flag": False,
                            },
                            "POINT (122.55 31.2)",
                        ),
                        (
                            "hist-2",
                            {
                                "event_id": "hist-2",
                                "origin_time": "1980-01-01T00:00:00+00:00",
                                "longitude": 124.0,
                                "latitude": 31.2,
                                "magnitude": 5.5,
                                "depth_km": 8.0,
                                "place": "far historical fixture",
                                "source": "fixture",
                                "disaster_flag": False,
                            },
                            None,
                        ),
                    ),
                )
                await _publish_asset(
                    session,
                    assessment_run_id=run.id,
                    asset_key="shanghai.admin.city",
                    records=(
                        (
                            "310000",
                            {"ID": "310000", "NAME": "上海市"},
                            "MULTIPOLYGON (((121.0 30.8, 123.0 30.8, 123.0 31.8, "
                            "121.0 31.8, 121.0 30.8)))",
                        ),
                    ),
                )
                await _publish_asset(
                    session,
                    assessment_run_id=run.id,
                    asset_key="shanghai.admin.county",
                    records=(
                        (
                            "310115000",
                            {"ID": "310115000", "NAME": "浦东新区"},
                            "MULTIPOLYGON (((121.2 31.0, 122.0 31.0, 122.0 31.5, "
                            "121.2 31.5, 121.2 31.0)))",
                        ),
                    ),
                )
                await _publish_asset(
                    session,
                    assessment_run_id=run.id,
                    asset_key="shanghai.admin.town",
                    records=(
                        (
                            "310115000001",
                            {"ID": "310115000001", "NAME": "陆家嘴街道"},
                            "MULTIPOLYGON (((121.4 31.1, 121.6 31.1, 121.6 31.3, "
                            "121.4 31.3, 121.4 31.1)))",
                        ),
                    ),
                )

        yield ToolContext(
            session=None,  # type: ignore[arg-type]
            user=AuthUser(username="qa-test", role="viewer", workgroup=None),
            event_id=event_id,
            revision_id=revision_id,
            assessment_run_id=assessment_run_id,
            snapshot_id=uuid4(),
            index_version_id=uuid4(),
        ), other_revision_id
    finally:
        await _cleanup(session_factory)


async def _publish_asset(
    session,
    *,
    assessment_run_id: UUID,
    asset_key: str,
    records: tuple[tuple[str, dict, str | None], ...],
) -> None:
    asset = DataAsset(
        asset_key=asset_key,
        region_id="shanghai",
        name=asset_key,
        data_type="vector",
        spatial_granularity="feature",
        responsibility_unit="test",
        update_interval_days=365,
        is_core=False,
        contract={},
    )
    session.add(asset)
    await session.flush()
    version = DataAssetVersion(
        asset_id=asset.id,
        version=f"{asset_key}-v1",
        status="imported",
        source_uri=f"test://{asset_key}",
        schema_summary={},
        record_count=len(records),
        source_crs="EPSG:4326",
        checksum=uuid4().hex + uuid4().hex,
        imported_by=FIXTURE_ACTOR,
    )
    session.add(version)
    await session.flush()
    for row_number, (business_key, properties, geometry_wkt) in enumerate(records, start=1):
        session.add(
            DataAssetRecord(
                version_id=version.id,
                row_number=row_number,
                business_key=business_key,
                properties=properties,
                geom=(
                    WKTElement(geometry_wkt, srid=4326)
                    if geometry_wkt is not None
                    else None
                ),
            )
        )
    await session.flush()
    version.status = "validated"
    await session.flush()
    version.status = "published"
    await session.flush()
    session.add(
        DataAssetSnapshot(
            run_id=assessment_run_id,
            asset_id=asset.id,
            region_id=asset.region_id,
            asset_version_id=version.id,
            asset_key=asset.asset_key,
            version=version.version,
            checksum=version.checksum,
            role="required",
            required=True,
        )
    )
    await session.flush()


async def _cleanup(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(EarthquakeEvent).where(EarthquakeEvent.source == FIXTURE_ACTOR)
            )
            await session.execute(
                delete(RawMessage).where(RawMessage.source == FIXTURE_ACTOR)
            )
            await session.execute(
                delete(DataAssetVersion).where(
                    DataAssetVersion.imported_by == FIXTURE_ACTOR
                )
            )
            await session.execute(
                delete(DataAsset).where(DataAsset.responsibility_unit == "test")
            )


async def test_event_tools_use_locked_revision_and_reject_cross_event_revision(
    session_factory,
    spatial_context,
) -> None:
    context, other_revision_id = spatial_context
    async with session_factory() as session:
        context = ToolContext(
            session=session,
            user=context.user,
            event_id=context.event_id,
            revision_id=context.revision_id,
            assessment_run_id=context.assessment_run_id,
            snapshot_id=context.snapshot_id,
            index_version_id=context.index_version_id,
        )
        result = await GetEventContextTool().handle({}, context)
        invalid = await GetEventRevisionTool().handle(
            {"revision_id": str(other_revision_id)},
            context,
        )

    assert result.status == "ok"
    assert result.value["event_id"] == str(context.event_id)
    assert result.value["revision_id"] == str(context.revision_id)
    assert result.value["revision_no"] == 2
    assert result.value["t1_at"] == "2026-10-08T02:00:00+00:00"
    assert invalid.status == "invalid"
    assert invalid.limitations == ("revision_not_in_event",)


async def test_fault_history_distance_and_region_tools_use_postgis(
    session_factory,
    spatial_context,
) -> None:
    context, _ = spatial_context
    async with session_factory() as session:
        async with session.begin():
            context = ToolContext(
                session=session,
                user=context.user,
                event_id=context.event_id,
                revision_id=context.revision_id,
                assessment_run_id=context.assessment_run_id,
                snapshot_id=context.snapshot_id,
                index_version_id=context.index_version_id,
            )
            fault_result = await FaultNearestTool().handle({}, context)
            history = await SeismicityWithinRadiusTool().handle(
                {"radius_km": "150"},
                context,
            )
            distance = await SeismicityDistanceTool().handle(
                {"historical_event_id": "hist-1"},
                context,
            )
            region = await RegionLookupTool().handle({}, context)

    assert fault_result.status == "ok"
    assert fault_result.value["distance_km"] == pytest.approx(50.0, abs=0.2)
    assert fault_result.value["business_key"] == "fault-1"
    assert history.status == "ok"
    assert [item["event_id"] for item in history.value["events"]] == ["hist-1"]
    assert history.value["events"][0]["distance_km"] == pytest.approx(
        100.0,
        abs=0.2,
    )
    assert distance.status == "ok"
    assert distance.value["distance_km"] == pytest.approx(100.0, abs=0.2)
    assert region.status == "ok"
    assert region.value["smallest_level"] == "town"
    assert [item["level"] for item in region.value["regions"]] == [
        "town",
        "county",
        "city",
    ]
