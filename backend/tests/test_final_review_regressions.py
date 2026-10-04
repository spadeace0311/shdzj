from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.response_rules import ResponseInput
from app.events.router import get_event_service
from app.events.schemas import RegionContext as RegionContextSchema
from app.events.service import EventService
from app.intensity.domain import (
    GridDefinition,
    InstrumentProduct,
    InstrumentProductFormat,
    InstrumentQuality,
    ProductStatus,
    ProductType,
)
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.parameters import load_parameter_bundle
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.intensity.service import DirectionInputs, IntensityService
from app.main import app
from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary


PARAMETERS_PATH = Path("/config/intensity/shanghai-2019.yaml")


@pytest.fixture(autouse=True)
async def clean_final_review_data(session_factory):
    await engine.dispose()
    await _delete_review_data(session_factory)
    yield
    await _delete_review_data(session_factory)
    await engine.dispose()


async def _delete_review_data(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(delete(IntensityRaster))
            await session.execute(delete(IntensityFieldProduct))
            await session.execute(delete(AssessmentTask))
            await session.execute(delete(AssessmentRun))
            await session.execute(delete(EventLifecycleOutbox))
            await session.execute(delete(EarthquakeRevision))
            await session.execute(delete(EarthquakeEvent))
            await session.execute(delete(RawMessage))
            await session.execute(
                delete(RegionBoundary).where(
                    RegionBoundary.version.like("final-review-boundary-%")
                )
            )


async def _ingest(
    session_factory,
    *,
    kind: EventKind,
    magnitude: str,
    report_number: int,
    received_at: datetime,
) -> tuple[str, str, str]:
    event = NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id="FINAL-REVIEW-EVENT",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="final review test",
        report_time=received_at - timedelta(minutes=1),
        report_number=report_number,
    )
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={
            "EventID": event.source_event_id,
            "type": "reviewed",
            "number": report_number,
        },
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received_at,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version="final-review-grid",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        outbox = await session.scalar(
            select(EventLifecycleOutbox).where(
                EventLifecycleOutbox.revision_id == outcome.revision_id,
                EventLifecycleOutbox.trigger_type == "assessment.requested",
            )
        )
        assert outbox is not None
        return outcome.event_id, outcome.revision_id, str(outbox.id)


async def _ensure_run(
    session_factory,
    event_id: str,
    revision_id: str,
    outbox_id: str,
) -> AssessmentRun:
    repository = AssessmentRepository()
    async with session_factory() as session:
        async with session.begin():
            return await repository.ensure_run_from_outbox(
                session,
                event_id=event_id,
                revision_id=revision_id,
                outbox_id=outbox_id,
            )


async def _complete_required_tasks(session, run_id) -> None:
    repository = AssessmentRepository()
    for task_key, algorithm_version, fingerprint in (
        ("intensity.model", "model-axis-ratio-v1", "a" * 64),
        ("intensity.fusion", "fusion-inverse-variance-v1", "b" * 64),
        ("loss.population", "population-intensity-v1", "c" * 64),
        ("loss.buildings", "building-structure-matrix-v1", "d" * 64),
        ("loss.casualties", "casualty-building-intensity-v1", "e" * 64),
        ("loss.economic", "economic-building-loss-v1", "f" * 64),
        ("loss.resources", "resource-linear-demand-v1", "0" * 64),
        ("loss.validate", "loss-validation-v1", "1" * 64),
    ):
        task = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == task_key,
            )
        )
        if task.status == "succeeded":
            continue
        await repository.start_task(
            session,
            run_id,
            task_key,
            algorithm_version,
            fingerprint,
        )
        await repository.complete_task(
            session,
            task.id,
            fingerprint,
            {"product_id": task_key},
        )


async def test_delayed_older_outbox_does_not_replace_newer_pointers(
    session_factory,
) -> None:
    first_received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    event_id, first_revision_id, first_outbox_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_number=1,
        received_at=first_received,
    )
    _, second_revision_id, second_outbox_id = await _ingest(
        session_factory,
        kind=EventKind.CORRECTION,
        magnitude="5.4",
        report_number=2,
        received_at=datetime(2026, 9, 27, 1, 5, tzinfo=UTC),
    )

    newer = await _ensure_run(
        session_factory,
        event_id,
        second_revision_id,
        second_outbox_id,
    )
    repository = AssessmentRepository()
    async with session_factory() as session:
        async with session.begin():
            await repository.start_run(session, newer.id)
            await _complete_required_tasks(session, newer.id)
            await repository.complete_run(session, newer.id, "bundle-v2")

    delayed_older = await _ensure_run(
        session_factory,
        event_id,
        first_revision_id,
        first_outbox_id,
    )
    async with session_factory() as session:
        async with session.begin():
            started = await repository.start_run(session, delayed_older.id)
            delayed_tasks = await repository.list_tasks(
                session,
                delayed_older.id,
            )

    async with session_factory() as session:
        event = await session.get(EarthquakeEvent, UUID(event_id))
        older = await session.get(AssessmentRun, delayed_older.id)
        current = await repository.get_current_run(session, event_id=event_id)
        assert event is not None
        assert older is not None
        assert current is not None

    assert started.status == "failed"
    assert started.superseded_at is not None
    assert started.completed_at is not None
    assert delayed_tasks
    assert all(task.status == "failed" for task in delayed_tasks)
    assert older.status == "failed"
    assert event.latest_assessment_run_id == newer.id
    assert event.effective_assessment_run_id == newer.id
    assert older.superseded_by_run_id == newer.id
    assert current.id == newer.id


async def test_start_run_is_idempotent_for_completed_prepare_retry(
    session_factory,
) -> None:
    event_id, revision_id, outbox_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_number=1,
        received_at=datetime(2026, 9, 27, 1, 3, tzinfo=UTC),
    )
    run = await _ensure_run(session_factory, event_id, revision_id, outbox_id)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            await repository.start_run(session, run.id)
            await _complete_required_tasks(session, run.id)
            completed = await repository.complete_run(session, run.id, "bundle-v1")
            retried = await repository.start_run(session, run.id)

    assert completed.status == "completed"
    assert retried.id == run.id
    assert retried.status == "completed"


async def test_timeout_reconciler_leaves_terminal_failure_evidence(
    session_factory,
) -> None:
    event_id, revision_id, outbox_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_number=1,
        received_at=datetime(2026, 9, 27, 1, 3, tzinfo=UTC),
    )
    run = await _ensure_run(session_factory, event_id, revision_id, outbox_id)
    repository = AssessmentRepository()
    observed_at = datetime(2026, 9, 27, 2, 3, tzinfo=UTC)

    async with session_factory() as session:
        async with session.begin():
            running = await repository.start_run(session, run.id)
            running.started_at = observed_at - timedelta(seconds=1_800)

    async with session_factory() as session:
        async with session.begin():
            failed_ids = await repository.reconcile_timeouts(
                session,
                safety_timeout_seconds=1_800,
                observed_at=observed_at,
            )

    async with session_factory() as session:
        failed = await session.get(AssessmentRun, run.id)
        tasks = (
            await session.scalars(
                select(AssessmentTask).where(AssessmentTask.run_id == run.id)
            )
        ).all()

    assert failed_ids == [run.id]
    assert failed is not None
    assert failed.status == "failed"
    assert failed.completed_at is not None
    assert failed.duration_ms is not None
    assert failed.last_error is not None
    assert "safety timeout" in failed.last_error
    assert failed.deadline_exceeded_at is not None
    assert all(task.status == "failed" for task in tasks)
    assert all("safety timeout" in (task.last_error or "") for task in tasks)


async def test_task_deadline_observation_persists_warning_evidence(
    session_factory,
) -> None:
    event_id, revision_id, outbox_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_number=1,
        received_at=datetime(2026, 9, 27, 1, 3, tzinfo=UTC),
    )
    run = await _ensure_run(session_factory, event_id, revision_id, outbox_id)
    repository = AssessmentRepository()
    observed_at = datetime(2026, 9, 27, 1, 4, 30, tzinfo=UTC)

    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            task.status = "running"
            task.deadline_at = observed_at - timedelta(seconds=1)
            other_tasks = (
                await session.scalars(
                    select(AssessmentTask).where(
                        AssessmentTask.run_id == run.id,
                        AssessmentTask.task_key != "intensity.model",
                    )
                )
            ).all()
            for other_task in other_tasks:
                other_task.deadline_at = observed_at + timedelta(seconds=60)
            first = await repository.observe_task_deadlines(
                session,
                run.id,
                observed_at,
            )
            second = await repository.observe_task_deadlines(
                session,
                run.id,
                observed_at + timedelta(seconds=1),
            )

    async with session_factory() as session:
        task = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run.id,
                AssessmentTask.task_key == "intensity.model",
            )
        )
        assert task is not None

    assert first == ["intensity.model"]
    assert second == []
    assert task.result["deadline_exceeded_at"] == observed_at.isoformat()


class _FakeRegionRepository:
    def __init__(self, active) -> None:
        self.active = active

    async def get_active(self, session):
        del session
        return self.active


async def _direct_event(
    magnitude: str = "5.2",
    *,
    kind: EventKind = EventKind.FORMAL,
) -> NormalizedEvent:
    return NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id="DIRECT-FINAL-REVIEW",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal(magnitude),
        place="direct ingestion test",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )


async def test_direct_formal_ingestion_resolves_active_boundary_before_outbox(
    session_factory,
) -> None:
    event = await _direct_event()
    received_at = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    active = RegionBoundary(
        id=uuid4(),
        version="final-review-boundary-active",
        name="direct review boundary",
        local_buffer_km=Decimal("50"),
        geom=None,
        maritime_geom=None,
        source_uri="test://boundary",
        checksum="b" * 64,
        is_active=True,
    )
    service = EventService(
        session_factory,
        region_repository=_FakeRegionRepository(active),
    )

    outcome = await service.ingest_with_response_suggestion(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
        event=event,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        received_at=received_at,
        region_context=RegionContext(True, 0, None, received_at),
    )

    async with session_factory() as session:
        revision = await session.get(EarthquakeRevision, UUID(outcome.revision_id))
        outbox_count = await session.scalar(
            select(text("COUNT(*)")).select_from(EventLifecycleOutbox)
            .where(
                EventLifecycleOutbox.trigger_type == "assessment.requested"
            )
        )
        assert revision is not None

    assert outcome.triggered_assessment is True
    assert revision.region_boundary_version == active.version
    assert int(outbox_count) == 1


async def test_direct_formal_ingestion_rejects_missing_active_boundary_before_outbox(
    session_factory,
) -> None:
    event = await _direct_event()
    received_at = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    service = EventService(
        session_factory,
        region_repository=_FakeRegionRepository(None),
    )

    with pytest.raises(ValueError, match="active region boundary"):
        await service.ingest_with_response_suggestion(
            raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
            event=event,
            response_input=ResponseInput(
                event.magnitude,
                event.depth_km,
                True,
                0,
                None,
                None,
            ),
            received_at=received_at,
            region_context=RegionContext(True, 0, None, received_at),
        )

    async with session_factory() as session:
        outbox_count = await session.scalar(
            select(text("COUNT(*)")).select_from(EventLifecycleOutbox)
            .where(
                EventLifecycleOutbox.trigger_type == "assessment.requested"
            )
        )
    assert int(outbox_count) == 0


async def test_direct_formal_endpoint_resolves_active_boundary_before_outbox(
    session_factory,
) -> None:
    active = type(
        "_ActiveBoundary",
        (),
        {"version": "final-review-boundary-api"},
    )()
    service = EventService(
        session_factory,
        region_repository=_FakeRegionRepository(active),
    )
    previous_service = app.dependency_overrides.get(get_event_service)
    previous_user = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_event_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="operator",
        role="group_leader",
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/ingest/formal",
                json={
                    "eventId": "DIRECT-API-FINAL-REVIEW",
                    "reportType": "formal",
                    "originTime": "2026-09-27T01:00:00Z",
                    "longitude": 121.5,
                    "latitude": 31.2,
                    "magnitude": 5.2,
                    "depth": 10.0,
                    "place": "direct api review",
                    "regionContext": {
                        "insideShanghai": True,
                        "distanceToBoundaryKm": 0,
                    },
                },
            )
    finally:
        if previous_service is None:
            app.dependency_overrides.pop(get_event_service, None)
        else:
            app.dependency_overrides[get_event_service] = previous_service
        if previous_user is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user

    assert response.status_code == 201
    body = response.json()
    async with session_factory() as session:
        revision = await session.get(
            EarthquakeRevision,
            UUID(body["revision_id"]),
        )
        outbox_count = await session.scalar(
            select(text("COUNT(*)")).select_from(EventLifecycleOutbox)
            .where(
                EventLifecycleOutbox.trigger_type == "assessment.requested"
            )
        )
        assert revision is not None

    assert revision.region_boundary_version == active.version
    assert int(outbox_count) == 1


def test_api_region_context_accepts_explicit_boundary_version() -> None:
    context = RegionContextSchema.model_validate(
        {
            "insideShanghai": True,
            "distanceToBoundaryKm": 0,
            "boundaryVersion": "boundary-v1",
        }
    )

    assert context.boundary_version == "boundary-v1"


@pytest.mark.parametrize(
    "source",
    (
        PARAMETERS_PATH.read_text(encoding="utf-8").replace(
            "sigma: 0.6310",
            "sigma: .nan",
        ),
        PARAMETERS_PATH.read_text(encoding="utf-8").replace(
            "epsilon: 0.000001",
            "epsilon: .inf",
        ),
    ),
)
def test_parameter_loader_rejects_non_finite_values(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "parameters.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match="finite"):
        load_parameter_bundle(path)


def test_parameter_loader_rejects_contradictory_ranges(tmp_path: Path) -> None:
    source = PARAMETERS_PATH.read_text(encoding="utf-8")
    source = source.replace("min: 4.0", "min: 6.5")
    source = source.replace("max: 6.2", "max: 4.5")
    path = tmp_path / "parameters.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match="magnitude"):
        load_parameter_bundle(path)


def test_fusion_parameters_reject_inconsistent_quality_thresholds() -> None:
    parameters = load_parameter_bundle(PARAMETERS_PATH)
    with pytest.raises(ValueError, match="F1"):
        replace(
            parameters.fusion,
            f1_min_coverage=0.30,
            f2_min_coverage=0.80,
        )


def test_model_and_fusion_domain_reject_non_finite_outputs() -> None:
    from app.intensity.domain import (
        DirectionDecision,
        DirectionStatus,
        FusionField,
        FusionMode,
        FusionQuality,
        ModelField,
    )

    direction = DirectionDecision(
        DirectionStatus.UNCERTAIN,
        None,
        None,
    )
    with pytest.raises(ValueError, match="finite"):
        ModelField(
            values=np.array([np.nan]),
            sigma=np.array([0.6]),
            extrapolated=False,
            direction=direction,
        )

    with pytest.raises(ValueError, match="finite"):
        FusionField(
            values=np.array([np.nan]),
            sigma=np.array([0.6]),
            p10=np.array([0.0]),
            p90=np.array([1.0]),
            model_weight=np.array([1.0]),
            instrument_weight=np.array([0.0]),
            quality_codes=np.array(["Q0"], dtype=object),
            mode=FusionMode.MODEL_ONLY,
            quality=FusionQuality.F3,
            coverage_ratio=0.0,
        )


async def _seed_run_and_task(session_factory, boundary_version: str):
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="RASTER-FINAL-REVIEW",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="raster review test",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
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
        region_context=RegionContext(True, 0, boundary_version, received),
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
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == "intensity.model",
                )
            )
            return run.id, task.id


async def test_raster_metadata_manifest_and_checksum_are_verified_on_read(
    session_factory,
) -> None:
    run_id, task_id = await _seed_run_and_task(
        session_factory,
        "final-review-raster-grid",
    )
    repository = IntensityRepository()
    definition = GridDefinition(
        "raster-review-grid",
        "EPSG:32651",
        1000,
        500000,
        3500000,
        2,
        1,
    )
    values = np.array([[1.0, 2.0]], dtype=np.float64)
    weights = np.array([[0.25, 0.5]], dtype=np.float64)
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=definition,
        region_profile_version="shanghai-v1",
        input_fingerprint="f" * 64,
        input_checksum="i" * 64,
        quality_grade=None,
        coverage_ratio=1.0,
        statistics={
            "minimum": 1.0,
            "maximum": 2.0,
            "mean": 1.5,
            "count": 2,
            "std": 0.5,
            "p10": 1.1,
            "p50": 1.5,
            "p90": 1.9,
        },
        source_product_id=None,
        observed_at=None,
        bands=[
            ("value", values),
            ("model_weight", weights),
        ],
        band_metadata={
            "value": {
                "type": "float64",
                "unit": "intensity_degree",
                "nodata": None,
                "scale": 1.0,
            },
            "model_weight": {
                "type": "float64",
                "unit": "weight",
                "nodata": None,
                "scale": 1.0,
            },
        },
    )

    async with session_factory() as session:
        async with session.begin():
            product_id = await repository.save_product(session, write)

    async with session_factory() as session:
        bands, metadata = await repository.load_raster(session, product_id)
        raster = await session.scalar(
            select(IntensityRaster).where(IntensityRaster.product_id == product_id)
        )
        assert raster is not None

    np.testing.assert_array_equal(bands[0], values)
    np.testing.assert_array_equal(bands[1], weights)
    assert metadata["srid"] == 32651
    assert metadata["origin_x"] == 500000.0
    assert metadata["origin_y"] == 3500000.0
    assert metadata["bands"][0]["type"] == "float64"
    assert metadata["bands"][0]["unit"] == "intensity_degree"
    assert metadata["bands"][0]["nodata"] is None
    assert metadata["bands"][0]["scale"] == 1.0
    assert len(metadata["bands"][0]["checksum"]) == 64
    assert raster.checksum == metadata["checksum"]


@pytest.mark.parametrize(
    "mutation",
    ("srid", "width", "georeference", "value", "checksum"),
)
async def test_raster_read_rejects_persisted_mutation(
    session_factory,
    mutation: str,
) -> None:
    run_id, task_id = await _seed_run_and_task(
        session_factory,
        "final-review-raster-mutation",
    )
    repository = IntensityRepository()
    definition = GridDefinition(
        "raster-mutation-grid",
        "EPSG:32651",
        1000,
        500000,
        3500000,
        1,
        1,
    )
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=definition,
        region_profile_version="shanghai-v1",
        input_fingerprint="f" * 64,
        input_checksum="i" * 64,
        quality_grade=None,
        coverage_ratio=1.0,
        statistics={"minimum": 4.0, "maximum": 4.0, "mean": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )
    async with session_factory() as session:
        async with session.begin():
            product_id = await repository.save_product(session, write)

    async with session_factory() as session:
        async with session.begin():
            if mutation == "srid":
                await session.execute(
                    text(
                        "UPDATE intensity_rasters SET srid = 4326 "
                        "WHERE product_id = :product_id"
                    ),
                    {"product_id": product_id},
                )
            elif mutation == "width":
                await session.execute(
                    text(
                        "UPDATE intensity_rasters SET width = 2 "
                        "WHERE product_id = :product_id"
                    ),
                    {"product_id": product_id},
                )
            elif mutation == "georeference":
                await session.execute(
                    text(
                        "UPDATE intensity_rasters "
                        "SET rast = ST_SetUpperLeft(rast, 501000, 3500000) "
                        "WHERE product_id = :product_id"
                    ),
                    {"product_id": product_id},
                )
            elif mutation == "value":
                await session.execute(
                    text(
                        "UPDATE intensity_rasters "
                        "SET rast = ST_SetValue(rast, 1, 1, 1, 999.0) "
                        "WHERE product_id = :product_id"
                    ),
                    {"product_id": product_id},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE intensity_rasters SET checksum = :checksum "
                        "WHERE product_id = :product_id"
                    ),
                    {"checksum": "0" * 64, "product_id": product_id},
                )

    with pytest.raises(RuntimeError, match="raster"):
        async with session_factory() as session:
            await repository.load_raster(session, product_id)


async def test_service_persists_model_weight_complete_stats_and_publication(
    session_factory,
) -> None:
    event_id, revision_id, outbox_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_number=1,
        received_at=datetime(2026, 9, 27, 1, 3, tzinfo=UTC),
    )
    run = await _ensure_run(session_factory, event_id, revision_id, outbox_id)
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle(PARAMETERS_PATH),
        fixed_grid_definition=GridDefinition(
            "final-review-service-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
    )
    repository = AssessmentRepository()

    model = await service.run_model(str(run.id))
    instrument = await service.run_instrument(str(run.id))
    fusion = await service.run_fusion(str(run.id))
    async with session_factory() as session:
        async with session.begin():
            await repository.start_run(session, run.id)
            await _complete_required_tasks(session, run.id)
            await repository.complete_run(session, run.id, "intensity-v1")

    async with session_factory() as session:
        model_product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run.id,
                IntensityFieldProduct.product_type == ProductType.MODEL.value,
            )
        )
        raster = await session.scalar(
            select(IntensityRaster).where(
                IntensityRaster.product_id == model_product.id
            )
        )
        assert model_product is not None
        assert raster is not None
        assert model_product.published_at is not None

    band_names = [band["name"] for band in raster.band_manifest["bands"]]
    assert {"value", "sigma", "model_weight"} <= set(band_names)
    for band in raster.band_manifest["bands"]:
        assert {"type", "unit", "nodata", "scale", "checksum"} <= set(band)
    for key in ("count", "std", "p10", "p50", "p90"):
        assert key in model_product.statistics

    assert model.status == "succeeded"
    assert instrument.status == "succeeded"
    assert fusion.status == "succeeded"


class _ChangingInstrumentProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, request):
        self.calls += 1
        if self.calls == 1:
            return InstrumentProduct(
                status=ProductStatus.UNAVAILABLE,
                product_id=None,
                product_version=None,
                observed_at=None,
                source="first",
                format=None,
                source_verified=False,
                grid_version=request.definition.version,
                values=None,
                sigma=None,
                quality_codes=None,
                coverage_ratio=0.0,
            )
        shape = (request.definition.height, request.definition.width)
        return InstrumentProduct(
            status=ProductStatus.AVAILABLE,
            product_id="changed",
            product_version="1",
            observed_at=datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
            source="second",
            format=InstrumentProductFormat.GRID,
            source_verified=True,
            grid_version=request.definition.version,
            values=np.full(shape, 5.0),
            sigma=np.full(shape, 0.2),
            quality_codes=np.full(shape, InstrumentQuality.Q1, dtype=object),
            coverage_ratio=1.0,
        )


class _DirectionProvider:
    def __init__(self, strike_deg: float) -> None:
        self.strike_deg = strike_deg

    async def load(self, session, revision):
        return DirectionInputs(override_deg=self.strike_deg)


async def test_instrument_duplicate_retry_keeps_succeeded_unavailable_result(
    session_factory,
) -> None:
    event_id, revision_id, outbox_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_number=1,
        received_at=datetime(2026, 9, 27, 1, 3, tzinfo=UTC),
    )
    run = await _ensure_run(session_factory, event_id, revision_id, outbox_id)
    provider = _ChangingInstrumentProvider()
    service = IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle(PARAMETERS_PATH),
        fixed_grid_definition=GridDefinition(
            "final-review-instrument-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
        instrument_provider=provider,
    )

    await service.run_model(str(run.id))
    first = await service.run_instrument(str(run.id))
    second = await service.run_instrument(str(run.id))

    async with session_factory() as session:
        product = await session.scalar(
            select(IntensityFieldProduct).where(
                IntensityFieldProduct.run_id == run.id,
                IntensityFieldProduct.product_type == ProductType.INSTRUMENT.value,
            )
        )
        assert product is not None

    assert first.product_id == second.product_id
    assert second.status == "succeeded"
    assert provider.calls == 1
    assert product.status == ProductStatus.UNAVAILABLE.value


async def test_model_fingerprint_includes_direction_provider_output(
    session_factory,
) -> None:
    event_id, revision_id, outbox_id = await _ingest(
        session_factory,
        kind=EventKind.FORMAL,
        magnitude="5.2",
        report_number=1,
        received_at=datetime(2026, 9, 27, 1, 3, tzinfo=UTC),
    )
    run = await _ensure_run(session_factory, event_id, revision_id, outbox_id)
    parameters = load_parameter_bundle(PARAMETERS_PATH)
    grid = GridDefinition(
        "final-review-direction-grid",
        "EPSG:32651",
        1000,
        0,
        2000,
        2,
        2,
    )
    first = IntensityService(
        session_factory=session_factory,
        parameters=parameters,
        fixed_grid_definition=grid,
        direction_provider=_DirectionProvider(0.0),
    )
    second = IntensityService(
        session_factory=session_factory,
        parameters=parameters,
        fixed_grid_definition=grid,
        direction_provider=_DirectionProvider(10.0),
    )

    assert (await first.run_model(str(run.id))).status == "succeeded"
    with pytest.raises(ValueError, match="fingerprint"):
        await second.run_model(str(run.id))


class _RecordingLockSession:
    def __init__(self, event, run) -> None:
        self.event = event
        self.run = run
        self.gets = []

    async def get(self, model, identifier, with_for_update=False):
        self.gets.append((model, identifier, with_for_update))
        if model is EarthquakeEvent:
            return self.event
        if model is AssessmentRun:
            return self.run
        raise AssertionError(model)


async def test_repository_uses_event_before_run_lock_order() -> None:
    event_id = uuid4()
    run_id = uuid4()
    event = EarthquakeEvent(id=event_id)
    run = AssessmentRun(id=run_id)
    session = _RecordingLockSession(event, run)
    repository = AssessmentRepository()

    locked_event, locked_run = await repository._lock_event_then_run(
        session,
        event_id,
        run_id,
    )

    assert locked_event is event
    assert locked_run is run
    assert session.gets == [
        (EarthquakeEvent, event_id, True),
        (AssessmentRun, run_id, True),
    ]
