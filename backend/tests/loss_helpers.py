import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from geoalchemy2.elements import WKTElement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.assessment.temporal import (
    AssessmentActivities,
    AssessmentWorkflow,
    AssessmentWorkflowInput,
)
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.data_assets.domain import NormalizedRecord, NormalizedTableData
from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
from app.data_assets.models import (
    DataAssetAuditLog,
    DataAssetSnapshot,
    DataAssetVersion,
)
from app.data_assets.repository import DataAssetRepository
from app.data_assets.service import DataAssetService
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import GridDefinition
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService
from app.loss.asset_bridge import load_locked_parameter_set
from app.loss.domain import (
    LossModelType,
    LossProductType,
    ResourceKind,
    ScenarioParameters,
)
from app.loss.models import LossMetricValue, LossProduct, LossProductRaster
from app.loss.repository import LossRepository
from app.loss.resources import ResourceDemandValue
from app.loss.service import LossAssessmentService, LossChainPerformanceResult
from app.main import app
from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary
from app.artifacts.worker import ArtifactActivities
from app.artifacts.workflow import ArtifactProductionWorkflow


LOSS_TEST_PREFIX = "LOSS-TEST-"
ASSET_VERSION = "loss-e2e-v1"
ASSET_VERSION_PREFIX = f"{ASSET_VERSION}-"
BASELINE_VERSION_PREFIX = "LOSS-BASELINE-"


class InjectedPublicationFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SeededLossProduct:
    run_id: UUID
    product_id: UUID | None
    status: str


@dataclass(frozen=True, slots=True)
class SeededLossRun:
    run_id: UUID
    workflow_request: AssessmentWorkflowInput


@dataclass(frozen=True, slots=True)
class PersistedResourceCheck:
    values: Mapping[ResourceKind, ResourceDemandValue]
    persisted_values: Mapping[str, LossMetricValue]


@dataclass(frozen=True, slots=True)
class BaselineAssetState:
    version_id: UUID
    created: bool
    previous_version_id: UUID | None


def _fixed_intensity_service(session_factory) -> IntensityService:
    return IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle(
            "/config/intensity/shanghai-2019.yaml"
        ),
        fixed_grid_definition=GridDefinition(
            "loss-e2e-grid",
            "EPSG:32651",
            1000,
            356000,
            3453000,
            2,
            2,
        ),
    )


async def _execute_loss_workflow(
    session_factory,
    request: AssessmentWorkflowInput,
    *,
    loss_service_factory=None,
    workflow_id_suffix: str = "main",
):
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _fixed_intensity_service(
            session_factory
        ),
        loss_service_factory=loss_service_factory,
    )
    task_queue = f"loss-e2e-{uuid4()}"
    artifact_activities = ArtifactActivities(session_factory)
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[AssessmentWorkflow, ArtifactProductionWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
                activities.mark_artifact_production_launched,
                artifact_activities.prepare_artifact_production,
                artifact_activities.wait_for_artifact_dependencies,
                artifact_activities.render_map_artifact,
                artifact_activities.compose_docx_artifact,
                artifact_activities.compose_pptx_artifact,
                artifact_activities.validate_artifact_production,
                artifact_activities.publish_artifact_production,
                artifact_activities.terminalize_artifact_production,
                artifact_activities.cancel_artifact_production,
                artifact_activities.mark_production_deadline_exceeded,
            ],
            max_concurrent_activities=128,
        ):
            return await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=(
                    f"loss-e2e:{request.event_id}:"
                    f"{request.revision_id}:{workflow_id_suffix}"
                ),
                task_queue=task_queue,
            )


async def _load_stage_seconds(
    session_factory,
    run_id: UUID,
) -> dict[str, float]:
    async with session_factory() as session:
        tasks = (
            await session.scalars(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run_id
                )
            )
        ).all()
    by_key = {task.task_key: task for task in tasks}

    def loss_stage(task_key: str, stage_name: str) -> float:
        task = by_key[task_key]
        if task.result is None:
            raise LookupError(f"{task_key} has no result")
        return float(task.result["stage_seconds"][stage_name])

    def task_duration(task_key: str) -> float:
        task = by_key[task_key]
        if task.started_at is None or task.completed_at is None:
            raise LookupError(f"{task_key} has no execution window")
        return (task.completed_at - task.started_at).total_seconds()

    intensity_seconds = sum(
        task_duration(key)
        for key in (
            "intensity.model",
            "intensity.instrument",
            "intensity.fusion",
        )
    )
    return {
        "snapshot_and_exposure": (
            intensity_seconds
            + loss_stage("loss.buildings", "snapshot_and_exposure")
        ),
        "buildings_and_population": (
            loss_stage("loss.buildings", "buildings_and_population")
            + loss_stage("loss.population", "buildings_and_population")
        ),
        "casualties_economic_resources": (
            loss_stage("loss.casualties", "casualties_economic_resources")
            + loss_stage("loss.economic", "casualties_economic_resources")
            + loss_stage("loss.resources", "casualties_economic_resources")
        ),
        "validate_and_persist": loss_stage(
            "loss.validate",
            "validate_and_persist",
        ),
    }


async def _seed_loss_boundary(session_factory) -> str:
    version = f"loss-test-boundary-{uuid4()}"
    geometry = WKTElement(
        "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
        "121.60 31.30, 121.40 31.30, 121.40 31.15)))",
        srid=4326,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                RegionBoundary(
                    version=version,
                    name="loss test boundary",
                    local_buffer_km=50,
                    geom=geometry,
                    maritime_geom=geometry,
                    source_uri="https://example.gov.invalid/loss-e2e/boundary",
                    checksum=hashlib.sha256(version.encode()).hexdigest(),
                    is_active=False,
                )
            )
    return version


async def _publish_fixed_assets(
    session_factory,
    *,
    include_buildings: bool = True,
    fail_after: int | None = None,
) -> dict[str, UUID]:
    town_codes = [f"310115{i:06d}" for i in range(1, 2)]
    town_records = [
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "ID": town_code,
                "NAME": f"测试街镇{index}",
            },
            geometry_wkt=(
                "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
                "121.60 31.30, 121.40 31.30, 121.40 31.15)))"
            ),
        )
        for index, town_code in enumerate(town_codes, start=1)
    ]
    population_records = [
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "ID": town_code,
                "NAME": f"测试街镇{index}",
                "total": 10000.0 if index == 1 else 0.0,
                "resident": 8000.0 if index == 1 else 0.0,
                "floating": 2000.0 if index == 1 else 0.0,
                "family": 3200.0 if index == 1 else 0.0,
                "under14": 1200.0 if index == 1 else 0.0,
                "over65": 1400.0 if index == 1 else 0.0,
            },
        )
        for index, town_code in enumerate(town_codes, start=1)
    ]
    building_records = [
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "id": town_code,
                "name": f"测试街镇{index}",
                "TOTAL_AREA": 500000.0 if index == 1 else 0.0,
                "HIGH_RISE": 0.0,
                "RCFRAME": 500000.0 if index == 1 else 0.0,
                "BRICK_STRUCTURE": 0.0,
                "SINGLE_AREA": 0.0,
                "OTHER_STRUCTURE": 0.0,
            },
        )
        for index, town_code in enumerate(town_codes, start=1)
    ]
    assets = {
        "shanghai.admin.town": _table(
            ("ID", "NAME", "geometry_wkt"),
            town_records,
        ),
        "shanghai.population.town": _table(
            (
                "ID",
                "NAME",
                "total",
                "resident",
                "floating",
                "family",
                "under14",
                "over65",
            ),
            population_records,
        ),
    }
    if include_buildings:
        assets["shanghai.building.town"] = _table(
            (
                "id",
                "name",
                "TOTAL_AREA",
                "HIGH_RISE",
                "RCFRAME",
                "BRICK_STRUCTURE",
                "SINGLE_AREA",
                "OTHER_STRUCTURE",
            ),
            building_records,
        )
    assets["shanghai.admin.city"] = _table(
        ("ID", "NAME", "geometry_wkt"),
        (
            NormalizedRecord(
                row_number=1,
                business_key="310000",
                properties={
                    "ID": "310000",
                    "NAME": "上海市",
                },
                geometry_wkt=(
                    "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
                    "121.60 31.30, 121.40 31.30, 121.40 31.15)))"
                ),
            ),
        ),
    )
    assets["shanghai.admin.county"] = _table(
        ("ID", "NAME", "geometry_wkt"),
        (
            NormalizedRecord(
                row_number=1,
                business_key="310115000",
                properties={
                    "ID": "310115000",
                    "NAME": "浦东新区",
                },
                geometry_wkt=(
                    "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
                    "121.60 31.30, 121.40 31.30, 121.40 31.15)))"
                ),
            ),
        ),
    )
    assets["shanghai.economy.county"] = _table(
        (
            "id",
            "name",
            "gdp",
            "industry_value",
            "agri_value",
            "service_value",
            "income",
        ),
        (
            NormalizedRecord(
                row_number=1,
                business_key="310115",
                properties={
                    "id": "310115",
                    "name": "浦东新区",
                    "gdp": 1.0,
                    "industry_value": 1.0,
                    "agri_value": 1.0,
                    "service_value": 1.0,
                    "income": 1.0,
                },
            ),
        ),
    )
    loss_document = yaml.safe_load(
        Path("/app/tests/fixtures/loss-test-parameters.yaml").read_text(
            encoding="utf-8"
        )
    )
    loss_document["parameter_set_id"] = loss_document["version"]
    building_models = loss_document["models"]["building_damage"]["scenarios"]
    for scenario in ("low", "central", "high"):
        values = building_models[scenario]["values"]
        for damage_state in (
            "basic",
            "slightly_damaged",
            "moderately_damaged",
            "severely_damaged",
            "collapsed",
        ):
            values[f"vulnerability.rc_frame.6.{damage_state}"] = values[
                f"vulnerability.rc_frame.7.{damage_state}"
            ]
            values[f"vulnerability.rc_frame.5.{damage_state}"] = values[
                f"vulnerability.rc_frame.7.{damage_state}"
            ]
    casualty_models = loss_document["models"]["casualties"]["scenarios"]
    for scenario in ("low", "central", "high"):
        values = casualty_models[scenario]["values"]
        values["injury_to_death_ratio.6"] = values["injury_to_death_ratio.7"]
        values["injury_to_death_ratio.5"] = values["injury_to_death_ratio.7"]
    assets["shanghai.loss.parameters"] = NormalizedTableData(
        columns=tuple(sorted(loss_document)),
        records=(
            NormalizedRecord(
                row_number=1,
                business_key=str(loss_document["parameter_set_id"]),
                properties=loss_document,
            ),
        ),
        source_crs="EPSG:4326",
        spatial_extent=None,
    )
    published: dict[str, UUID] = {}
    for index, (asset_key, normalized) in enumerate(
        sorted(assets.items()),
        start=1,
    ):
        published[asset_key] = await _publish_table_asset(
            session_factory,
            asset_key=asset_key,
            normalized=normalized,
        )
        if fail_after is not None and index == fail_after:
            raise InjectedPublicationFailure(
                "injected loss fixture publication failure"
            )
    return published


def _table(columns, records) -> NormalizedTableData:
    return NormalizedTableData(
        columns=tuple(columns),
        records=tuple(records),
        source_crs="EPSG:4326",
        spatial_extent=(121.40, 31.15, 121.60, 31.30),
    )


async def _publish_table_asset(
    session_factory,
    *,
    asset_key,
    normalized,
    version_prefix: str = ASSET_VERSION_PREFIX,
):
    service = DataAssetService()
    checksum = hashlib.sha256(
        repr((asset_key, ASSET_VERSION)).encode("utf-8")
    ).hexdigest()
    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key=asset_key,
                    version=f"{version_prefix}{uuid4()}",
                    source_uri=(
                        "https://example.gov.invalid/loss-e2e/"
                        f"{asset_key.replace('.', '-')}"
                    ),
                    license_name="test-only",
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="loss end-to-end fixture",
                    file_name=f"{asset_key}.json",
                    file_format="parameter_file",
                    file_size_bytes=1,
                    checksum=checksum,
                    relative_path=f"loss-e2e/{checksum}.json",
                    requested_by="loss-test",
                ),
            )
            await service.populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {"source": "loss-test"},
            )
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor="loss-test",
            )
            if not report.publishable:
                raise AssertionError(
                    f"loss fixture asset failed validation: {asset_key}"
                )
            version = await service.publish_version(
                session,
                job.asset_version_id,
                "loss-test",
                "loss end-to-end fixture",
            )
            return version.id


async def _seed_loss_run(
    session_factory,
    *,
    boundary_version: str,
    event_kind: EventKind = EventKind.FORMAL,
) -> SeededLossRun:
    source_event_id = f"{LOSS_TEST_PREFIX}{uuid4()}"
    now = datetime.now(UTC)
    event = NormalizedEvent(
        kind=event_kind,
        source="cenc",
        source_event_id=source_event_id,
        origin_time=now - timedelta(minutes=3),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="loss test",
        report_time=now - timedelta(minutes=1),
    )
    received = now
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": source_event_id, "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(
            True,
            0,
            boundary_version,
            received,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id,
                    EventLifecycleOutbox.trigger_type
                    == "assessment.requested",
                )
            )
            if outbox is None:
                raise LookupError("loss fixture outbox not found")
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            # The live dispatcher/worker services share this database. Keep
            # this fixture outbox invisible to them while the in-process
            # WorkflowEnvironment exercises the same AssessmentWorkflow.
            outbox.status = "published"
            outbox.published_at = received
            return SeededLossRun(
                run_id=run.id,
                workflow_request=AssessmentWorkflowInput(
                    event_id=outcome.event_id,
                    revision_id=outcome.revision_id,
                    outbox_id=str(outbox.id),
                ),
            )


async def _request_loss_path(path: str):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="loss-test",
        role="superadmin",
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(path)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


async def _request_loss_api(run_id: UUID):
    return await _request_loss_path(
        f"/api/v1/assessments/runs/{run_id}/loss"
    )


async def _request_loss_artifact(run_id: UUID, product_id: UUID):
    return await _request_loss_path(
        f"/api/v1/assessments/runs/{run_id}/loss/artifact"
        f"?product_id={product_id}"
    )


async def _request_loss_tile(
    run_id: UUID,
    product_id: UUID,
    band: str,
    z: int,
    x: int,
    y: int,
):
    return await _request_loss_path(
        f"/api/v1/assessments/runs/{run_id}/loss/artifact/"
        f"{product_id}/{band}/{z}/{x}/{y}.png"
    )


async def _restore_fixture_asset_publications(
    session,
) -> tuple[UUID, ...]:
    version_ids = tuple(
        (
            await session.scalars(
                select(DataAssetVersion.id).where(
                    DataAssetVersion.version.like(
                        f"{ASSET_VERSION_PREFIX}%"
                    )
                )
            )
        ).all()
    )
    if not version_ids:
        return ()

    audits = (
        await session.scalars(
            select(DataAssetAuditLog).where(
                DataAssetAuditLog.version_id.in_(version_ids),
                DataAssetAuditLog.action == "publish",
            )
        )
    ).all()
    fixture_version_id_set = set(version_ids)
    previous_version_ids = tuple(
        dict.fromkeys(
            previous_id
            for audit in audits
            if audit.details and audit.details.get("old_version_id")
            if (previous_id := UUID(str(audit.details["old_version_id"])))
            not in fixture_version_id_set
        )
    )

    await session.execute(
        update(DataAssetVersion)
        .where(DataAssetVersion.id.in_(version_ids))
        .values(status="retired", retired_at=datetime.now(UTC))
    )
    if previous_version_ids:
        await session.execute(
            update(DataAssetVersion)
            .where(DataAssetVersion.id.in_(previous_version_ids))
            .values(status="published", retired_at=None)
        )
    await session.execute(
        delete(DataAssetAuditLog).where(
            DataAssetAuditLog.actor == "loss-test"
        )
    )
    return version_ids


async def _cleanup_loss_fixture(
    session_factory,
    boundary_version: str,
    *,
    asset_version_ids: tuple[UUID, ...] = (),
) -> None:
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            event_ids = select(EarthquakeRevision.event_id).where(
                EarthquakeRevision.source_event_id.like(
                    f"{LOSS_TEST_PREFIX}%"
                )
            )
            raw_message_ids = tuple(
                (
                    await session.scalars(
                        select(EarthquakeRevision.raw_message_id).where(
                            EarthquakeRevision.event_id.in_(event_ids)
                        )
                    )
                ).all()
            )
            run_ids = select(AssessmentRun.id).where(
                AssessmentRun.event_id.in_(event_ids)
            )
            product_ids = select(LossProduct.id).where(
                LossProduct.run_id.in_(run_ids)
            )
            await session.execute(
                delete(LossProductRaster).where(
                    LossProductRaster.product_id.in_(product_ids)
                )
            )
            await session.execute(
                delete(LossMetricValue).where(
                    LossMetricValue.product_id.in_(product_ids)
                )
            )
            await session.execute(
                delete(LossProduct).where(
                    LossProduct.run_id.in_(run_ids)
                )
            )
            await session.execute(
                delete(IntensityRaster).where(
                    IntensityRaster.product_id.in_(
                        select(IntensityFieldProduct.id).where(
                            IntensityFieldProduct.run_id.in_(run_ids)
                        )
                    )
                )
            )
            await session.execute(
                delete(IntensityFieldProduct).where(
                    IntensityFieldProduct.run_id.in_(run_ids)
                )
            )
            await session.execute(
                delete(AssessmentTask).where(
                    AssessmentTask.run_id.in_(run_ids)
                )
            )
            discovered_version_ids = await _restore_fixture_asset_publications(session)
            fixture_asset_version_ids = tuple(
                dict.fromkeys((*asset_version_ids, *discovered_version_ids))
            )
            if fixture_asset_version_ids:
                await session.execute(
                    delete(DataAssetSnapshot).where(
                        DataAssetSnapshot.asset_version_id.in_(
                            fixture_asset_version_ids
                        )
                    )
                )
            await session.execute(
                delete(AssessmentRun).where(
                    AssessmentRun.id.in_(run_ids)
                )
            )
            await session.execute(
                delete(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id.in_(event_ids)
                )
            )
            await session.execute(
                delete(EarthquakeRevision).where(
                    EarthquakeRevision.event_id.in_(event_ids)
                )
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.id.in_(event_ids)
                )
            )
            await session.execute(
                delete(RawMessage).where(RawMessage.id.in_(raw_message_ids))
            )
            await session.execute(
                delete(RegionBoundary).where(
                    RegionBoundary.version == boundary_version
                )
            )
            if fixture_asset_version_ids:
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.id.in_(fixture_asset_version_ids)
                    )
                )
    await engine.dispose()


async def _publish_baseline_asset(
    session_factory,
    *,
    asset_key: str,
) -> BaselineAssetState:
    repository = DataAssetRepository()
    async with session_factory() as session:
        existing = await repository.get_published_version(
            session,
            asset_key=asset_key,
            region_id="shanghai",
        )
        if existing is not None:
            return BaselineAssetState(
                version_id=existing.id,
                created=False,
                previous_version_id=None,
            )

    normalized = _table(
        ("ID", "NAME", "geometry_wkt"),
        (
            NormalizedRecord(
                row_number=1,
                business_key="310000",
                properties={"ID": "310000", "NAME": "上海市"},
                geometry_wkt=(
                    "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
                    "121.60 31.30, 121.40 31.30, 121.40 31.15)))"
                ),
            ),
        ),
    )
    version_id = await _publish_table_asset(
        session_factory,
        asset_key=asset_key,
        normalized=normalized,
        version_prefix=BASELINE_VERSION_PREFIX,
    )
    return BaselineAssetState(
        version_id=version_id,
        created=True,
        previous_version_id=None,
    )


async def _delete_baseline_asset(
    session_factory,
    *,
    asset_key: str,
    baseline: BaselineAssetState,
) -> None:
    if not baseline.created:
        return
    async with session_factory() as session:
        async with session.begin():
            version_id = baseline.version_id
            if baseline.previous_version_id is not None:
                await session.execute(
                    update(DataAssetVersion)
                    .where(DataAssetVersion.id == baseline.previous_version_id)
                    .values(status="published", retired_at=None)
                )
            await session.execute(
                delete(DataAssetSnapshot).where(
                    DataAssetSnapshot.asset_version_id == version_id
                )
            )
            await session.execute(
                delete(DataAssetVersion).where(
                    DataAssetVersion.id == version_id
                )
            )


async def execute_fixed_loss_chain(
    session_factory,
) -> LossChainPerformanceResult:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        async with session_factory() as session:
            run = await session.get(AssessmentRun, seeded.run_id)
            if run is None:
                raise LookupError("loss fixture run not found")
            report_ingested_at = run.report_ingested_at

        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "completed":
            raise AssertionError(
                f"assessment workflow failed: {workflow_result.status}"
            )

        response = await _request_loss_api(seeded.run_id)
        query_ready_at = datetime.now(UTC)
        if response.status_code != 200:
            raise AssertionError(
                f"loss API did not become query-ready: {response.status_code}"
            )
        products = response.json()["products"]
        if len(products) != 6:
            raise AssertionError("loss API did not return all six products")
        stage_seconds = await _load_stage_seconds(
            session_factory,
            seeded.run_id,
        )
        elapsed = (query_ready_at - report_ingested_at).total_seconds()
        return LossChainPerformanceResult(
            status="completed",
            report_ingested_at=report_ingested_at,
            query_ready_at=query_ready_at,
            elapsed_seconds=elapsed,
            stage_seconds=stage_seconds,
        )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_resources_with_missing(
    session_factory,
    missing_parameter: str,
) -> PersistedResourceCheck:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )

        async def load_parameters(session, *, run_id, profile):
            parameter_set = await load_locked_parameter_set(
                session,
                run_id=run_id,
                profile=profile,
            )
            model = parameter_set.models[LossModelType.RESOURCE_DEMAND]
            scenarios = {
                scenario: ScenarioParameters(
                    values={
                        key: value
                        for key, value in parameters.values.items()
                        if key != missing_parameter
                    }
                )
                for scenario, parameters in model.scenarios.items()
            }
            return replace(
                parameter_set,
                models={
                    **parameter_set.models,
                    LossModelType.RESOURCE_DEMAND: replace(
                        model,
                        scenarios=scenarios,
                    ),
                },
            )

        service = LossAssessmentService(
            session_factory,
            parameter_loader=load_parameters,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
            loss_service_factory=lambda: service,
        )
        if workflow_result.status != "completed":
            raise AssertionError(
                f"assessment workflow failed: {workflow_result.status}"
            )

        async with session_factory() as session:
            product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.RESOURCE_DEMAND,
            )
        if product is None:
            raise LookupError("resource product not found")
        metrics = (
            await _metric_rows_for_product(session_factory, product.id)
        )
        values = {
            ResourceKind(metric.metric_key.split(".", 1)[0]): ResourceDemandValue(
                quantity=(
                    int(metric.numeric_value)
                    if metric.numeric_value is not None
                    else None
                ),
                status=str(
                    getattr(metric.value_status, "value", metric.value_status)
                ),
                reason=metric.note,
            )
            for metric in metrics
        }
        persisted = {
            metric.metric_key: metric
            for metric in metrics
        }
        return PersistedResourceCheck(
            values=values,
            persisted_values=persisted,
        )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_without_vulnerability_row(
    session_factory,
) -> SeededLossProduct:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(
            session_factory,
            include_buildings=False,
        )
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "failed":
            raise AssertionError("workflow should fail without vulnerability data")
        async with session_factory() as session:
            run = await session.get(AssessmentRun, seeded.run_id)
            if run is None:
                raise LookupError("loss fixture run not found")
            failed_task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == seeded.run_id,
                    AssessmentTask.task_key == "loss.buildings",
                )
            )
            if (
                failed_task is None
                or failed_task.status != "failed"
                or not failed_task.last_error
            ):
                raise AssertionError("failed building task was not audited")
            return SeededLossProduct(
                run_id=seeded.run_id,
                product_id=None,
                status=run.status,
            )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_buildings_twice(
    session_factory,
) -> tuple[SeededLossProduct, SeededLossProduct]:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        first_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
            workflow_id_suffix="first",
        )
        second_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
            workflow_id_suffix="second",
        )
        if first_result.status != "completed" or second_result.status != "completed":
            raise AssertionError("duplicate workflow did not complete")
        async with session_factory() as session:
            first_product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.BUILDING_DAMAGE,
            )
        if first_product is None:
            raise LookupError("building product not found")
        return (
            SeededLossProduct(
                seeded.run_id,
                first_product.id,
                first_product.status,
            ),
            SeededLossProduct(
                seeded.run_id,
                first_product.id,
                first_product.status,
            ),
        )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_formal(session_factory) -> SeededLossProduct:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "completed":
            raise AssertionError("formal workflow did not complete")
        async with session_factory() as session:
            product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.BUILDING_DAMAGE,
            )
        if product is None:
            raise LookupError("building product not found")
        return SeededLossProduct(seeded.run_id, product.id, product.status)
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_correction(session_factory) -> SeededLossProduct:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
            event_kind=EventKind.CORRECTION,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "completed":
            raise AssertionError("correction workflow did not complete")
        async with session_factory() as session:
            product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.BUILDING_DAMAGE,
            )
        if product is None:
            raise LookupError("building product not found")
        return SeededLossProduct(seeded.run_id, product.id, product.status)
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_partial_asset_publish_recovery(session_factory) -> None:
    asset_key = "shanghai.admin.city"
    baseline_state = await _publish_baseline_asset(
        session_factory,
        asset_key=asset_key,
    )
    try:
        try:
            await _publish_fixed_assets(
                session_factory,
                fail_after=1,
            )
        except InjectedPublicationFailure:
            pass
        else:
            raise AssertionError("partial publication failure was not raised")
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            f"loss-cleanup-{uuid4()}",
        )

    try:
        async with session_factory() as session:
            baseline_row = await session.get(
                DataAssetVersion,
                baseline_state.version_id,
            )
            remaining = await session.scalar(
                select(DataAssetVersion.id).where(
                    DataAssetVersion.version.like(
                        f"{ASSET_VERSION_PREFIX}%"
                    )
                )
            )
        if baseline_row is None or baseline_row.status != "published":
            raise AssertionError("previous published version was not restored")
        if remaining is not None:
            raise AssertionError("fixture asset version was not removed")
    finally:
        try:
            await _delete_baseline_asset(
                session_factory,
                asset_key=asset_key,
                baseline=baseline_state,
            )
        finally:
            await engine.dispose()


async def _metric_rows_for_product(
    session_factory,
    product_id: UUID,
) -> list[LossMetricValue]:
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(LossMetricValue)
                    .where(LossMetricValue.product_id == product_id)
                    .order_by(
                        LossMetricValue.metric_key,
                        LossMetricValue.value_type,
                    )
                )
            ).all()
        )
