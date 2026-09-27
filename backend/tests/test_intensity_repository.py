from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import numpy as np
import pytest
from sqlalchemy import delete, func, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.artifacts import RasterCodec
from app.intensity.domain import GridDefinition, ProductStatus, ProductType
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.regions.domain import RegionContext


@pytest.fixture(autouse=True)
async def clean_intensity_data(session_factory):
    async def clean() -> None:
        await engine.dispose()
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

    await clean()
    yield
    await clean()


async def _seed_run(session_factory) -> tuple[UUID, UUID]:
    event = NormalizedEvent(
        kind=EventKind.FORMAL,
        source="cenc",
        source_event_id="INTENSITY-REPO-1",
        origin_time=datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="上海测试位置",
        report_time=datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received_at = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": event.source_event_id, "type": "reviewed"},
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
            boundary_version="test-grid",
            computed_at=received_at,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            from app.assessment.repository import AssessmentRepository

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


async def test_product_and_raster_round_trip(session_factory) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    definition = GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1)
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
        coverage_ratio=0.0,
        statistics={"minimum": 4.0, "maximum": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )

    async with session_factory() as session:
        async with session.begin():
            product_id = await repository.save_product(session, write)

    async with session_factory() as session:
        product = await repository.get_product(
            session,
            run_id=run_id,
            product_type=ProductType.MODEL,
        )
        bands, metadata = await repository.load_raster(session, product_id)

    assert product is not None
    assert product["product_type"] == "model"
    assert bands[0][0, 0] == pytest.approx(4.0)
    assert metadata["grid_definition_version"] == "grid-1"
    assert metadata["bands"] == [{"number": 1, "name": "value"}]
    assert metadata["crs"] == "EPSG:32651"
    assert metadata["origin_x"] == 500000.0
    assert metadata["origin_y"] == 3500000.0
    assert metadata["resolution_m"] == 1000.0
    assert metadata["srid"] == 32651


async def test_multi_band_round_trip_preserves_order_metadata_and_exact_values(
    session_factory,
) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    definition = GridDefinition(
        "grid-multi",
        "EPSG:32651",
        250,
        600000.0,
        3400000.0,
        2,
        2,
    )
    values = np.array([[1.125, 2.25], [3.375, 4.5]], dtype=np.float64)
    sigma = np.array([[0.0625, 0.125], [0.1875, 0.25]], dtype=np.float64)
    expected_manifest = [
        {"number": 1, "name": "value"},
        {"number": 2, "name": "sigma"},
    ]
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v2",
        parameter_version="parameters-v2",
        strategy_version=None,
        grid_definition=definition,
        region_profile_version="shanghai-v2",
        input_fingerprint="a" * 64,
        input_checksum="b" * 64,
        quality_grade=None,
        coverage_ratio=0.75,
        statistics={"minimum": 0.0625, "maximum": 4.5},
        source_product_id=None,
        observed_at=None,
        bands=[("value", values), ("sigma", sigma)],
    )

    async with session_factory() as session:
        async with session.begin():
            product_id = await repository.save_product(session, write)

    async with session_factory() as session:
        product = await repository.get_product(
            session,
            run_id=run_id,
            product_type=ProductType.MODEL,
        )
        bands, metadata = await repository.load_raster(session, product_id)
        raster_count = await session.scalar(
            select(func.count())
            .select_from(IntensityRaster)
            .where(IntensityRaster.product_id == product_id)
        )
        stored_checksum = await session.scalar(
            select(IntensityRaster.checksum).where(
                IntensityRaster.product_id == product_id
            )
        )

    assert product is not None
    assert product["output_checksum"] == stored_checksum
    assert raster_count == 1
    assert len(bands) == 2
    np.testing.assert_array_equal(bands[0], values)
    np.testing.assert_array_equal(bands[1], sigma)
    assert metadata["grid_definition_version"] == "grid-multi"
    assert metadata["bands"] == expected_manifest
    assert metadata["crs"] == "EPSG:32651"
    assert metadata["srid"] == 32651
    assert metadata["origin_x"] == 600000.0
    assert metadata["origin_y"] == 3400000.0
    assert metadata["resolution_m"] == 250.0


async def test_completed_product_idempotency_rejects_changed_algorithm(
    session_factory,
) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    grid = GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1)
    fingerprint = "7" * 64
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=grid,
        region_profile_version="shanghai-v1",
        input_fingerprint=fingerprint,
        input_checksum="8" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={"minimum": 4.0, "maximum": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )
    changed_algorithm = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v2",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=grid,
        region_profile_version="shanghai-v1",
        input_fingerprint=fingerprint,
        input_checksum="8" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={"minimum": 4.0, "maximum": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )

    async with session_factory() as session:
        async with session.begin():
            await repository.save_product(session, write)

    with pytest.raises(ValueError, match="algorithm"):
        async with session_factory() as session:
            async with session.begin():
                await repository.save_product(session, changed_algorithm)


async def test_available_product_without_bands_is_rejected(session_factory) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1),
        region_profile_version="shanghai-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={},
        source_product_id=None,
        observed_at=None,
        bands=[],
    )

    with pytest.raises(ValueError, match="requires raster bands"):
        async with session_factory() as session:
            async with session.begin():
                await repository.save_product(session, write)

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(IntensityFieldProduct)
        )
    assert count == 0


async def test_terminal_product_without_bands_is_complete_and_not_loadable(
    session_factory,
) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    write = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.INSTRUMENT,
        status=ProductStatus.UNAVAILABLE,
        algorithm_version="instrument-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1),
        region_profile_version="shanghai-v1",
        input_fingerprint="e" * 64,
        input_checksum="f" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={},
        source_product_id=None,
        observed_at=None,
        bands=[],
    )

    async with session_factory() as session:
        async with session.begin():
            product_id = await repository.save_product(session, write)

    async with session_factory() as session:
        product = await session.get(IntensityFieldProduct, product_id)
        assert product is not None
        assert product.completed_at is not None
        assert product.output_checksum is None
        with pytest.raises(LookupError, match="raster not found"):
            await repository.load_raster(session, product_id)


async def test_existing_incomplete_product_is_rejected(session_factory) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    grid = GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1)
    fingerprint = "9" * 64
    incomplete = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.UNAVAILABLE,
        algorithm_version="model-v1",
        parameter_version="parameters-v1",
        strategy_version=None,
        grid_definition=grid,
        region_profile_version="shanghai-v1",
        input_fingerprint=fingerprint,
        input_checksum="8" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={},
        source_product_id=None,
        observed_at=None,
        bands=[],
    )
    completed = IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.MODEL,
        status=ProductStatus.AVAILABLE,
        algorithm_version="model-v2",
        parameter_version="parameters-v2",
        strategy_version=None,
        grid_definition=grid,
        region_profile_version="shanghai-v1",
        input_fingerprint=fingerprint,
        input_checksum="8" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={"minimum": 4.0, "maximum": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )

    async with session_factory() as session:
        async with session.begin():
            await repository.save_product(session, incomplete)

    with pytest.raises(ValueError, match="incomplete"):
        async with session_factory() as session:
            async with session.begin():
                await repository.save_product(session, completed)


async def test_checksum_verification_failure_rolls_back(
    session_factory,
    monkeypatch,
) -> None:
    run_id, task_id = await _seed_run(session_factory)
    repository = IntensityRepository()
    definition = GridDefinition("grid-1", "EPSG:32651", 1000, 500000, 3500000, 1, 1)
    original_encode = RasterCodec.encode
    calls = 0

    def mismatched_encode(definition, bands, band_manifest):
        nonlocal calls
        calls += 1
        payload = original_encode(definition, bands, band_manifest)
        if calls == 2:
            return payload + b"\x00"
        return payload

    monkeypatch.setattr(RasterCodec, "encode", mismatched_encode)
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
        input_fingerprint="1" * 64,
        input_checksum="2" * 64,
        quality_grade=None,
        coverage_ratio=0.0,
        statistics={"minimum": 4.0, "maximum": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=[("value", np.array([[4.0]]))],
    )

    with pytest.raises(RuntimeError, match="checksum verification failed"):
        async with session_factory() as session:
            async with session.begin():
                await repository.save_product(session, write)

    async with session_factory() as session:
        product_count = await session.scalar(
            select(func.count()).select_from(IntensityFieldProduct)
        )
        raster_count = await session.scalar(
            select(func.count()).select_from(IntensityRaster)
        )
    assert product_count == 0
    assert raster_count == 0
