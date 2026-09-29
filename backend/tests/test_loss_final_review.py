from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.assessment.models import AssessmentRun, AssessmentTask
from app.events.models import EarthquakeEvent
from app.loss.asset_bridge import load_locked_parameter_set
from app.loss.domain import (
    LossModelType,
    LossProductStatus,
    LossProductType,
    ScenarioParameters,
)
from app.loss.models import LossProduct
from app.loss.repository import LossRepository
from app.loss.service import LossAssessmentService
from tests.loss_helpers import (
    _cleanup_loss_fixture,
    _execute_loss_workflow,
    _metric_rows_for_product,
    _publish_fixed_assets,
    _request_loss_path,
    _seed_loss_boundary,
    _seed_loss_run,
)


class _FakeSession:
    @asynccontextmanager
    async def begin(self):
        yield self


class _FakeSessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return _FakeSession()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _RecordingAssessmentRepository:
    def __init__(self):
        self.failures = []
        self.audits = []

    async def fail_task(
        self,
        session,
        task_id,
        error_category,
        error_summary,
    ):
        self.failures.append((error_category, error_summary))

    async def record_task_failure_audit(
        self,
        session,
        run_id,
        task_key,
        error_category,
        error_summary,
    ):
        self.audits.append((error_category, error_summary))


async def _with_loss_workflow(
    session_factory,
    callback,
    *,
    loss_service_factory=None,
):
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
            loss_service_factory=loss_service_factory,
        )
        return await callback(seeded, workflow_result)
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def _invalid_resource_parameter_service(session_factory):
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
                    key: ("bad" if key == "tent.base" else value)
                    for key, value in parameters.values.items()
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

    return LossAssessmentService(
        session_factory,
        parameter_loader=load_parameters,
    )


async def test_successful_workflow_publishes_loss_products_and_effective_pointer(
    session_factory,
) -> None:
    async def assert_completed(seeded, workflow_result):
        assert workflow_result.status == "completed"
        async with session_factory() as session:
            run = await session.get(AssessmentRun, seeded.run_id)
            assert run is not None and run.status == "completed"
            event = await session.get(EarthquakeEvent, run.event_id)
            products = list(
                (
                    await session.scalars(
                        select(LossProduct).where(
                            LossProduct.run_id == seeded.run_id
                        )
                    )
                ).all()
            )
            assert len(products) == 6
            assert all(product.status == LossProductStatus.COMPLETE for product in products)
            assert all(product.published_at is not None for product in products)
            assert event is not None
            assert event.effective_assessment_run_id == seeded.run_id

    await _with_loss_workflow(session_factory, assert_completed)


async def test_successful_workflow_persists_all_advertised_area_scopes(
    session_factory,
) -> None:
    model_product_types = (
        LossProductType.BUILDING_DAMAGE,
        LossProductType.POPULATION_IMPACT,
        LossProductType.CASUALTIES,
        LossProductType.ECONOMIC_LOSS,
    )

    async def assert_scopes(seeded, workflow_result):
        assert workflow_result.status == "completed"
        building_product_id = None
        async with session_factory() as session:
            for product_type in model_product_types:
                product = await LossRepository().get_product(
                    session,
                    seeded.run_id,
                    product_type,
                )
                assert product is not None
                metrics = await _metric_rows_for_product(
                    session_factory,
                    product.id,
                )
                scopes = {metric.area_scope for metric in metrics}
                assert {"town", "county", "city"} <= scopes
                if product_type is LossProductType.BUILDING_DAMAGE:
                    building_product_id = product.id

        assert building_product_id is not None
        building_metrics = await _metric_rows_for_product(
            session_factory,
            building_product_id,
        )
        central = [
            metric
            for metric in building_metrics
            if metric.metric_key == "collapsed_area_m2"
            and metric.value_type == "central"
        ]
        city_metrics = [
            metric for metric in central if metric.area_scope == "city"
        ]
        assert city_metrics
        assert {metric.area_code for metric in city_metrics} == {"310000"}
        assert {metric.area_name for metric in city_metrics} == {"上海市"}
        town_total = sum(
            float(metric.numeric_value)
            for metric in central
            if metric.area_scope == "town"
            and metric.numeric_value is not None
        )
        city_value = next(
            float(metric.numeric_value)
            for metric in city_metrics
            if metric.numeric_value is not None
        )
        assert city_value == pytest.approx(town_total)

        county_response = await _request_loss_path(
            f"/api/v1/assessments/runs/{seeded.run_id}/loss/areas?scope=county"
        )
        city_response = await _request_loss_path(
            f"/api/v1/assessments/runs/{seeded.run_id}/loss/areas?scope=city"
        )
        assert county_response.status_code == 200
        assert county_response.json()["scope"] == "county"
        assert county_response.json()["features"]
        assert city_response.status_code == 200
        assert city_response.json()["scope"] == "city"
        city_features = city_response.json()["features"]
        assert len(city_features) == 1
        city_feature = city_features[0]
        assert city_feature["area_code"] == "310000"
        assert city_feature["area_name"] == "上海市"
        assert city_feature["geometry"]

    await _with_loss_workflow(session_factory, assert_scopes)


async def test_invalid_validation_fails_run_and_keeps_products_unpublished(
    session_factory,
) -> None:
    service = await _invalid_resource_parameter_service(session_factory)

    async def assert_invalid(seeded, workflow_result):
        assert workflow_result.status == "failed"
        async with session_factory() as session:
            run = await session.get(AssessmentRun, seeded.run_id)
            assert run is not None and run.status == "failed"
            event = await session.get(EarthquakeEvent, run.event_id)
            assert event is not None
            assert event.effective_assessment_run_id is None
            validation_task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == seeded.run_id,
                    AssessmentTask.task_key == "loss.validate",
                )
            )
            assert validation_task is not None
            assert validation_task.status == "failed"
            validation_product = await session.scalar(
                select(LossProduct).where(
                    LossProduct.run_id == seeded.run_id,
                    LossProduct.product_type == LossProductType.VALIDATION.value,
                )
            )
            assert validation_product is not None
            assert validation_product.status == LossProductStatus.INVALID
            assert validation_product.published_at is None
            products = list(
                (
                    await session.scalars(
                        select(LossProduct).where(
                            LossProduct.run_id == seeded.run_id
                        )
                    )
                ).all()
            )
            assert all(product.published_at is None for product in products)

    await _with_loss_workflow(
        session_factory,
        assert_invalid,
        loss_service_factory=lambda: service,
    )


async def test_failure_summary_is_sanitized() -> None:
    repository = _RecordingAssessmentRepository()
    service = LossAssessmentService(
        _FakeSessionFactory(),
        assessment_repository=repository,
    )
    await service._record_task_failure(
        str(uuid4()),
        "loss.buildings",
        RuntimeError(
            "connection failed postgres://user:secret@db:5432/earthquake"
        ),
        task_id=uuid4(),
    )

    assert repository.failures
    category, summary = repository.failures[0]
    assert category == "internal_error"
    assert summary == "loss assessment task failed"
    assert "postgres" not in summary
    assert "secret" not in summary
    assert "RuntimeError" not in summary
