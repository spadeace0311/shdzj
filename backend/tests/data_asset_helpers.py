import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.data_assets.domain import DataAssetDefinition

FIXTURE_ACTOR = "data-asset-fixture"
ASSESSMENT_OUTBOX_TYPE = "assessment.requested"


@dataclass(frozen=True, slots=True)
class SeededImportedVersion:
    version_id: UUID
    source_path: Path
    definition: DataAssetDefinition


@dataclass(frozen=True, slots=True)
class SeededOutbox:
    event_id: UUID
    revision_id: UUID
    outbox_id: UUID


def _town_codes(count: int = 212) -> tuple[str, ...]:
    return tuple(f"{310115000001 + index}" for index in range(count))


def _population_records():
    from app.data_assets.domain import NormalizedRecord, NormalizedTableData

    records = tuple(
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "ID": town_code,
                "NAME": f"town-{index}",
                "total": 100,
                "resident": 70,
                "floating": 20,
                "family": 50,
                "under14": 10,
                "over65": 15,
            },
        )
        for index, town_code in enumerate(_town_codes(), start=1)
    )
    return NormalizedTableData(
        columns=(
            "ID",
            "NAME",
            "family",
            "floating",
            "over65",
            "resident",
            "total",
            "under14",
        ),
        records=records,
        source_crs="EPSG:4326",
        spatial_extent=None,
    )


def _town_records():
    from app.data_assets.domain import NormalizedRecord, NormalizedTableData

    geometry_wkt = (
        "MULTIPOLYGON (((121.4 31.1, 121.6 31.1, 121.6 31.4, "
        "121.4 31.4, 121.4 31.1)))"
    )
    records = tuple(
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={"ID": town_code, "NAME": f"town-{index}"},
            geometry_wkt=geometry_wkt,
        )
        for index, town_code in enumerate(_town_codes(), start=1)
    )
    return NormalizedTableData(
        columns=("ID", "NAME"),
        records=records,
        source_crs="EPSG:4326",
        spatial_extent=(121.4, 31.1, 121.6, 31.4),
    )


def _definition(asset_key: str) -> DataAssetDefinition:
    from app.data_assets.registry import get_asset_definition

    return get_asset_definition(asset_key)


def _asset_contract(definition: DataAssetDefinition) -> dict:
    return {
        "business_key_fields": list(definition.contract.business_key_fields),
        "fields": [
            {
                "name": field.name,
                "python_type": field.python_type,
                "required": field.required,
                "nonnegative": field.nonnegative,
                "minimum": field.minimum,
                "maximum": field.maximum,
            }
            for field in definition.contract.fields
        ],
        "geometry_type": definition.contract.geometry_type,
        "source_crs": definition.contract.source_crs,
        "aggregate_of": definition.contract.aggregate_of,
        "expected_record_count": definition.contract.expected_record_count,
        "excluded_business_keys": list(definition.contract.excluded_business_keys),
        "exclusion_reason": definition.contract.exclusion_reason,
    }


async def _cleanup_fixture_data(session_factory) -> None:
    from sqlalchemy import delete

    from app.data_assets.models import DataAssetVersion

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(DataAssetVersion).where(
                    DataAssetVersion.imported_by == FIXTURE_ACTOR
                )
            )


async def _published_version_has_town_keys(
    session,
    version_id: UUID,
) -> bool:
    from sqlalchemy import select

    from app.data_assets.models import DataAssetRecord

    stored_keys = set(
        (
            await session.scalars(
                select(DataAssetRecord.business_key).where(
                    DataAssetRecord.version_id == version_id
                )
            )
        ).all()
    )
    return stored_keys == set(_town_codes())


async def _queue_candidate(
    session,
    *,
    asset_key: str,
    version: str,
    requested_by: str = FIXTURE_ACTOR,
):
    from app.data_assets.import_jobs import QueueImportRequest, queue_import_job

    return await queue_import_job(
        session,
        QueueImportRequest(
            asset_key=asset_key,
            version=version,
            source_uri="https://example.gov.invalid/data",
            license_name=None,
            acquired_at=None,
            valid_from=None,
            valid_to=None,
            change_note="synthetic data asset fixture",
            file_name="data",
            file_format="geojson",
            file_size_bytes=1,
            checksum="a" * 64,
            relative_path="aa/aa/aaaaaaaa-aa",
            requested_by=requested_by,
        ),
    )


async def _populate(
    session,
    version_id: UUID,
    normalized,
    *,
    importer: str,
):
    from app.data_assets.service import DataAssetService

    await DataAssetService().populate_candidate_version(
        session,
        version_id,
        normalized,
        {
            "importer": importer,
            "record_count": normalized.record_count,
            "source_crs": normalized.source_crs,
        },
    )


async def _ensure_published_admin_town(session_factory) -> UUID:
    from sqlalchemy import select

    from app.data_assets.models import DataAsset, DataAssetVersion
    from app.data_assets.service import DataAssetService

    definition = _definition("shanghai.admin.town")
    async with session_factory() as session:
        async with session.begin():
            existing = await session.scalar(
                select(DataAssetVersion)
                .join(DataAsset, DataAssetVersion.asset_id == DataAsset.id)
                .where(DataAssetVersion.status == "published")
                .where(DataAsset.asset_key == definition.asset_key)
                .where(DataAsset.region_id == definition.region_id)
            )
            if existing is not None and await _published_version_has_town_keys(
                session,
                existing.id,
            ):
                return existing.id
            job = await _queue_candidate(
                session,
                asset_key=definition.asset_key,
                version=f"admin-town-{uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                _town_records(),
                importer="geojson",
            )
            service = DataAssetService()
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor=FIXTURE_ACTOR,
            )
            if not report.publishable:
                raise ValueError("synthetic admin town version is not publishable")
            await service.publish_version(
                session,
                job.asset_version_id,
                FIXTURE_ACTOR,
                "synthetic admin town fixture",
            )
            return job.asset_version_id


async def publish_new_population_version(
    session_factory,
    version: str,
) -> UUID:
    from app.data_assets.service import DataAssetService

    definition = _definition("shanghai.population.town")
    await _ensure_published_admin_town(session_factory)
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key=definition.asset_key,
                version=version,
            )
            await _populate(
                session,
                job.asset_version_id,
                _population_records(),
                importer="geojson",
            )
            service = DataAssetService()
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor=FIXTURE_ACTOR,
            )
            if not report.publishable:
                raise ValueError("synthetic population version is not publishable")
            await service.publish_version(
                session,
                job.asset_version_id,
                FIXTURE_ACTOR,
                "synthetic population fixture",
            )
            return job.asset_version_id


async def wait_for_import_job(session_factory, job_id: UUID) -> UUID:
    from app.data_assets.models import DataAssetImportJob
    from app.data_assets.worker import process_import_job

    async with session_factory() as session:
        async with session.begin():
            job = await session.get(DataAssetImportJob, job_id, with_for_update=True)
            if job is None:
                raise LookupError("data asset import job not found")
            if job.status == "queued":
                job.status = "running"
                await process_import_job(session, job)
            if job.status in {"completed", "rejected", "failed"}:
                if job.asset_version_id is None:
                    raise TimeoutError(
                        f"import job {job_id} ended as {job.status} without a version"
                    )
                return job.asset_version_id
            raise TimeoutError(
                f"import job {job_id} did not reach a terminal state: {job.status}"
            )


@pytest.fixture
async def candidate_factory(session_factory):
    async def factory(version: str) -> UUID:
        definition = _definition("shanghai.population.town")
        async with session_factory() as session:
            async with session.begin():
                job = await _queue_candidate(
                    session,
                    asset_key=definition.asset_key,
                    version=version,
                )
                await _populate(
                    session,
                    job.asset_version_id,
                    _population_records(),
                    importer="geojson",
                )
                return job.asset_version_id

    yield factory
    await _cleanup_fixture_data(session_factory)


@pytest.fixture
async def published_population_asset(session_factory) -> UUID:
    version_id = await publish_new_population_version(session_factory, "2022.1")
    yield version_id
    await _cleanup_fixture_data(session_factory)


async def _ensure_active_boundary(session) -> str:
    from sqlalchemy import select

    from app.regions.models import RegionBoundary

    active = await session.scalar(
        select(RegionBoundary).where(RegionBoundary.is_active.is_(True))
    )
    if active is not None:
        return active.version
    from decimal import Decimal

    from geoalchemy2.elements import WKTElement

    geometry = WKTElement(
        "MULTIPOLYGON (((120.8 30.6, 122.2 30.6, 122.2 31.9, "
        "120.8 31.9, 120.8 30.6)))",
        srid=4326,
    )
    boundary = RegionBoundary(
        version=f"data-asset-boundary-{uuid4()}",
        name="data asset test boundary",
        local_buffer_km=Decimal("50"),
        geom=geometry,
        maritime_geom=geometry,
        source_uri="https://example.gov.invalid/boundary",
        checksum="a" * 64,
        is_active=True,
    )
    session.add(boundary)
    await session.flush()
    return boundary.version


@pytest.fixture
async def seeded_outbox(session_factory) -> SeededOutbox:
    from datetime import UTC, datetime
    from decimal import Decimal

    from sqlalchemy import select

    from app.events.domain import EventKind, NormalizedEvent
    from app.events.models import EventLifecycleOutbox
    from app.events.response_rules import ResponseInput
    from app.events.service import EventService
    from app.regions.domain import RegionContext

    boundary_version = await _with_boundary(session_factory)
    source_event_id = f"DATA-ASSET-TEST-{uuid4()}"
    received_at = datetime(2026, 9, 26, 1, 3, tzinfo=UTC)
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id=source_event_id,
        origin_time=datetime(2026, 9, 26, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="Shanghai data asset test",
        report_time=datetime(2026, 9, 26, 1, 2, tzinfo=UTC),
    )
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": source_event_id, "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            deaths=None,
            max_intensity=None,
        ),
        region_context=RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version=boundary_version,
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id,
                    EventLifecycleOutbox.trigger_type == ASSESSMENT_OUTBOX_TYPE,
                )
            )
            if outbox is None:
                raise LookupError("assessment outbox was not created")
            return SeededOutbox(
                event_id=UUID(outcome.event_id),
                revision_id=UUID(outcome.revision_id),
                outbox_id=outbox.id,
            )


async def _with_boundary(session_factory) -> str:
    async with session_factory() as session:
        async with session.begin():
            return await _ensure_active_boundary(session)


@pytest.fixture
async def seeded_assessment_run(
    session_factory,
    seeded_outbox,
) -> UUID:
    from app.assessment.repository import AssessmentRepository

    async with session_factory() as session:
        async with session.begin():
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=str(seeded_outbox.event_id),
                revision_id=str(seeded_outbox.revision_id),
                outbox_id=str(seeded_outbox.outbox_id),
            )
            return run.id


@pytest.fixture
async def data_asset_client():
    from httpx import ASGITransport, AsyncClient

    from app.auth.router import get_current_user
    from app.auth.service import AuthUser
    from app.main import app

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="superadmin",
        role="superadmin",
        workgroup=None,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


@pytest.fixture
def geojson_town_file(tmp_path: Path) -> Path:
    import json

    source = tmp_path / "towns.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"ID": "310115000001", "NAME": "test town"},
                        "geometry": {
                            "type": "MultiPolygon",
                            "coordinates": [
                                [
                                    [
                                        [121.4, 31.1],
                                        [121.6, 31.1],
                                        [121.6, 31.4],
                                        [121.4, 31.4],
                                        [121.4, 31.1],
                                    ]
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return source


@pytest.fixture
async def seeded_imported_version(
    session_factory,
    tmp_path: Path,
) -> SeededImportedVersion:
    from datetime import UTC, datetime

    import numpy as np
    import rasterio
    from rasterio.transform import Affine
    from sqlalchemy import select

    from app.data_assets.models import DataAsset, DataAssetVersion

    source_path = tmp_path / "gdp.tif"
    transform = Affine(0.01, 0, 121.0, 0, -0.01, 31.5)
    with rasterio.open(
        source_path,
        "w",
        driver="GTiff",
        width=4,
        height=5,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=0,
    ) as target:
        target.write(np.ones((5, 4), dtype="uint8"), 1)
    source_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()

    definition = _definition("shanghai.gdp.raster")
    version = f"gdp-raster-{uuid4()}"
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            asset = await session.scalar(
                select(DataAsset).where(
                    DataAsset.asset_key == definition.asset_key,
                    DataAsset.region_id == definition.region_id,
                )
            )
            if asset is None:
                asset = DataAsset(
                    asset_key=definition.asset_key,
                    region_id=definition.region_id,
                    name=definition.name,
                    data_type=definition.data_type.value,
                    spatial_granularity=definition.spatial_granularity,
                    responsibility_unit=definition.responsibility_unit,
                    update_interval_days=definition.update_interval_days,
                    is_core=definition.is_core,
                    contract=_asset_contract(definition),
                    created_at=now,
                    updated_at=now,
                )
                session.add(asset)
                await session.flush()
            version_row = DataAssetVersion(
                asset_id=asset.id,
                version=version,
                status="imported",
                source_uri="https://example.gov.invalid/gdp.tif",
                schema_summary={},
                record_count=0,
                spatial_extent=None,
                source_crs="EPSG:4326",
                checksum=source_checksum,
                managed_path=None,
                imported_by=FIXTURE_ACTOR,
                imported_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(version_row)
            await session.flush()
            version_id = version_row.id
    yield SeededImportedVersion(version_id, source_path, definition)
    await _cleanup_fixture_data(session_factory)
