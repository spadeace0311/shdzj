from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from rasterio.io import MemoryFile
from sqlalchemy import delete, select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import EarthquakeEvent, EarthquakeRevision, EventLifecycleOutbox, RawMessage
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import GridDefinition, ProductStatus, ProductType
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.repository import IntensityProductWrite, IntensityRepository
from app.main import app
from app.regions.domain import RegionContext


_FUSION_GRID = GridDefinition(
    "fusion-api-grid",
    "EPSG:32651",
    1000,
    356000,
    3450000,
    1,
    1,
)


@pytest.fixture(autouse=True)
async def clean_api_data(session_factory):
    await engine.dispose()
    await _delete(session_factory)
    yield
    await _delete(session_factory)
    await engine.dispose()


async def _delete(session_factory):
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


async def _seed_product(session_factory) -> UUID:
    event = NormalizedEvent(
        EventKind.FORMAL,
        "cenc",
        "API-INTENSITY-1",
        datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        Decimal("121.5"),
        Decimal("31.2"),
        Decimal("10"),
        Decimal("5.2"),
        "test",
        datetime(2026, 9, 27, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "API-INTENSITY-1", "type": "reviewed"},
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
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
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
            await IntensityRepository().save_product(
                session,
                IntensityProductWrite(
                    run_id=run.id,
                    task_id=task.id,
                    product_type=ProductType.MODEL,
                    status=ProductStatus.AVAILABLE,
                    algorithm_version="model-axis-ratio-v1",
                    parameter_version="shanghai-2019.1",
                    strategy_version=None,
                    grid_definition=GridDefinition(
                        "grid-test",
                        "EPSG:32651",
                        1000,
                        0,
                        1000,
                        1,
                        1,
                    ),
                    region_profile_version="shanghai-v1",
                    input_fingerprint="f" * 64,
                    input_checksum="i" * 64,
                    quality_grade=None,
                    coverage_ratio=0.0,
                    statistics={"minimum": 5.0, "maximum": 5.0, "mean": 5.0},
                    source_product_id=None,
                    observed_at=received,
                    bands=[("value", __import__("numpy").array([[5.0]]))],
                ),
            )
            return run.id


async def _seed_superseding_run(session_factory) -> UUID:
    event = NormalizedEvent(
        EventKind.CORRECTION,
        "cenc",
        "API-INTENSITY-1",
        datetime(2026, 9, 27, 1, 0, tzinfo=UTC),
        Decimal("121.5"),
        Decimal("31.2"),
        Decimal("10"),
        Decimal("5.3"),
        "test",
        datetime(2026, 9, 27, 1, 4, tzinfo=UTC),
    )
    received = datetime(2026, 9, 27, 1, 5, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": "API-INTENSITY-1", "type": "reviewed"},
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
        region_context=RegionContext(True, 0, "grid-test", received),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            return run.id


def _fusion_write(
    run_id: UUID,
    task_id: UUID,
    *,
    status: ProductStatus = ProductStatus.AVAILABLE,
    bands: list[tuple[str, np.ndarray]] | None = None,
) -> IntensityProductWrite:
    normalized_bands = [
        ("value", np.asarray([[4.0]], dtype=np.float64)),
        ("sigma", np.asarray([[0.5]], dtype=np.float64)),
    ] if bands is None else bands
    return IntensityProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=ProductType.FUSION,
        status=status,
        algorithm_version="fusion-inverse-variance-v1",
        parameter_version="shanghai-2019.1",
        strategy_version="full",
        grid_definition=_FUSION_GRID,
        region_profile_version="shanghai-v1",
        input_fingerprint="b" * 64,
        input_checksum="c" * 64,
        quality_grade=None,
        coverage_ratio=1.0 if status is ProductStatus.AVAILABLE else 0.5,
        statistics={"minimum": 4.0, "maximum": 4.0, "mean": 4.0},
        source_product_id=None,
        observed_at=None,
        bands=normalized_bands,
    )


async def _seed_fusion_product(session_factory) -> tuple[UUID, UUID]:
    run_id = await _seed_product(session_factory)
    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run_id,
                    AssessmentTask.task_key == "intensity.fusion",
                )
            )
            assert task is not None
            product_id = await IntensityRepository().save_product(
                session,
                _fusion_write(run_id, task.id),
            )
            return run_id, product_id


async def _request(
    path: str,
    *,
    role: str = "group_member",
):
    previous = app.dependency_overrides.get(get_current_user)
    if role is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: AuthUser(
            username="operator",
            role=role,
            workgroup="震害评估组",
        )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(path)
    finally:
        if role is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            if previous is None:
                app.dependency_overrides.pop(get_current_user, None)
            else:
                app.dependency_overrides[get_current_user] = previous


def _artifact_path(run_id: UUID, product_id: UUID, band: str | None = None) -> str:
    path = (
        f"/api/v1/assessments/runs/{run_id}/intensity/artifact"
        f"?product_id={product_id}"
    )
    if band is not None:
        path += f"&band={band}"
    return path


def _tile_path(
    run_id: UUID,
    product_id: UUID,
    band: str,
    z: int,
    x: int,
    y: int,
) -> str:
    return (
        f"/api/v1/assessments/runs/{run_id}/intensity/artifact/"
        f"{product_id}/{band}/{z}/{x}/{y}.png"
    )


async def _complete_required_tasks(session, run_id) -> None:
    repository = AssessmentRepository()
    for task_key, algorithm_version, fingerprint in (
        ("intensity.model", "model-axis-ratio-v1", "a" * 64),
        ("intensity.fusion", "fusion-inverse-variance-v1", "b" * 64),
        ("loss.population", "population-v1", "c" * 64),
        ("loss.buildings", "buildings-v1", "d" * 64),
        ("loss.casualties", "casualties-v1", "e" * 64),
        ("loss.economic", "economic-v1", "f" * 64),
        ("loss.resources", "resources-v1", "a" * 63 + "1"),
        ("loss.validate", "validate-v1", "b" * 63 + "2"),
    ):
        task = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == run_id,
                AssessmentTask.task_key == task_key,
            )
        )
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


async def test_get_intensity_result_summary(session_factory) -> None:
    run_id = await _seed_product(session_factory)
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="operator",
        role="group_member",
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/assessments/runs/{run_id}/intensity"
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["run_status"] in {"pending", "running", "completed", "failed"}
    assert body["effective_run_id"] is None
    assert body["products"][0]["product_type"] == "model"
    assert body["products"][0]["product_id"]
    assert body["products"][0]["algorithm_version"] == "model-axis-ratio-v1"


async def test_get_intensity_result_for_superseded_run(session_factory) -> None:
    first_run_id = await _seed_product(session_factory)
    repository = AssessmentRepository()

    async with session_factory() as session:
        async with session.begin():
            first = await session.get(AssessmentRun, first_run_id)
            event_id = first.event_id
            first_revision_id = first.revision_id
            await repository.start_run(session, first_run_id)
            await _complete_required_tasks(session, first_run_id)
            await repository.complete_run(session, first_run_id, "bundle-1")

    second_run_id = await _seed_superseding_run(session_factory)

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="operator",
        role="group_member",
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                f"/api/v1/assessments/runs/{first_run_id}/intensity"
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(first_run_id)
    assert body["event_id"] == str(event_id)
    assert body["revision_id"] == str(first_revision_id)
    assert body["run_status"] == "completed"
    assert body["superseded_by_run_id"] == str(second_run_id)
    assert body["effective_run_id"] == str(first_run_id)
    assert body["effective_revision_id"] == str(first_revision_id)
    assert body["is_latest_revision"] is False
    assert body["is_fallback"] is False
    assert body["products"][0]["product_type"] == "model"


async def test_intensity_artifact_returns_verified_tile_template(
    session_factory,
) -> None:
    run_id, product_id = await _seed_fusion_product(session_factory)
    response = await _request(_artifact_path(run_id, product_id))

    assert response.status_code == 200
    payload = response.json()
    assert payload["product_id"] == str(product_id)
    assert payload["bands"][0]["name"] == "value"
    assert payload["tile_template"].endswith("/{z}/{x}/{y}.png")
    assert payload["srid"] == 32651
    assert payload["width"] == _FUSION_GRID.width
    assert payload["height"] == _FUSION_GRID.height


async def test_intensity_artifact_defaults_to_value_when_present(
    session_factory,
) -> None:
    run_id, product_id = await _seed_fusion_product(session_factory)
    response = await _request(_artifact_path(run_id, product_id))

    assert response.status_code == 200
    assert response.json()["bands"][0]["name"] == "value"
    assert response.json()["bands"][1]["name"] == "sigma"


@pytest.mark.filterwarnings("ignore:Use `@` matmul")
@pytest.mark.filterwarnings("ignore:Dataset has no geotransform")
async def test_intensity_tile_returns_nonblank_png(session_factory) -> None:
    run_id, product_id = await _seed_fusion_product(session_factory)
    response = await _request(
        _tile_path(run_id, product_id, "value", 10, 857, 418)
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    with MemoryFile(response.content) as memory:
        with memory.open() as dataset:
            pixels = dataset.read()
    assert pixels.size > 0
    assert np.any(pixels != 0)


async def test_intensity_artifact_and_tile_require_authentication(
    session_factory,
) -> None:
    run_id, product_id = await _seed_fusion_product(session_factory)

    artifact = await _request(_artifact_path(run_id, product_id), role=None)
    tile = await _request(
        _tile_path(run_id, product_id, "value", 0, 0, 0),
        role=None,
    )

    assert artifact.status_code == 401
    assert tile.status_code == 401


async def test_intensity_artifact_unknown_run_product_and_band_return_404(
    session_factory,
) -> None:
    run_id, product_id = await _seed_fusion_product(session_factory)

    unknown_run = await _request(
        _artifact_path(UUID("00000000-0000-0000-0000-000000000001"), product_id)
    )
    unknown_product = await _request(
        _artifact_path(run_id, UUID("00000000-0000-0000-0000-000000000002"))
    )
    unknown_band = await _request(
        _artifact_path(run_id, product_id, band="missing_band")
    )
    unknown_tile_band = await _request(
        _tile_path(run_id, product_id, "missing_band", 0, 0, 0)
    )

    assert unknown_run.status_code == 404
    assert unknown_product.status_code == 404
    assert unknown_band.status_code == 404
    assert unknown_tile_band.status_code == 404


async def test_intensity_artifact_rejects_unavailable_fusion_product(
    session_factory,
) -> None:
    run_id = await _seed_product(session_factory)
    async with session_factory() as session:
        async with session.begin():
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run_id,
                    AssessmentTask.task_key == "intensity.fusion",
                )
            )
            assert task is not None
            product_id = await IntensityRepository().save_product(
                session,
                _fusion_write(
                    run_id,
                    task.id,
                    status=ProductStatus.UNAVAILABLE,
                    bands=[],
                ),
            )

    response = await _request(_artifact_path(run_id, product_id))
    tile = await _request(_tile_path(run_id, product_id, "value", 0, 0, 0))

    assert response.status_code == 404
    assert tile.status_code == 404


async def test_intensity_tile_zoom_outside_range_returns_422(
    session_factory,
) -> None:
    run_id, product_id = await _seed_fusion_product(session_factory)

    too_low = await _request(
        _tile_path(run_id, product_id, "value", -1, 0, 0)
    )
    too_high = await _request(
        _tile_path(run_id, product_id, "value", 23, 0, 0)
    )

    assert too_low.status_code == 422
    assert too_high.status_code == 422
