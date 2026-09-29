import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np

from app.loss.domain import (
    LossModelType,
    LossProductType,
    LossRunContext,
    LossValueType,
)
from app.loss.exposure import build_exposure_dataset
from app.loss.artifacts import LossArtifactCodec
from app.loss.models_registry import load_parameter_set
from app.loss.region import load_region_loss_profile
from app.loss.service import LossAssessmentService
from app.loss.spatial import LossGridCell, TownIntensityShare


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


class _LockingSession:
    def __init__(self, owner):
        self._owner = owner

    @asynccontextmanager
    async def begin(self):
        async with self._owner.transaction_lock:
            self._owner.events.append("write_begin")
            yield self
            self._owner.events.append("write_commit")


class _LockingSessionFactory:
    def __init__(self):
        self.transaction_lock = asyncio.Lock()
        self.events = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return _LockingSession(self)

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeAssessmentRepository:
    def __init__(self):
        self.completed = []
        self.tasks = {}

    async def start_task(
        self,
        session,
        run_id,
        task_key,
        algorithm_version,
        input_fingerprint,
    ):
        task = self.tasks.get(task_key)
        if task is None:
            task = SimpleNamespace(
                id=uuid4(),
                status="running",
                task_key=task_key,
            )
            self.tasks[task_key] = task
        return task

    async def complete_task(
        self,
        session,
        task_id,
        output_checksum,
        result,
    ):
        task = next(
            task
            for task in self.tasks.values()
            if task.id == task_id
        )
        task.status = "succeeded"
        task.output_checksum = output_checksum
        self.completed.append(
            (task_id, output_checksum, dict(result))
        )
        return task

    async def record_task_failure_audit(self, *args, **kwargs):
        return None


class _FakeLossRepository:
    def __init__(self):
        self.rows: dict[LossProductType, SimpleNamespace] = {}
        self.writes = []

    async def get_product(self, session, run_id, product_type):
        return self.rows.get(product_type)

    async def list_products(self, session, run_id):
        return [
            row
            for product_type, row in self.rows.items()
            if product_type is not LossProductType.VALIDATION
        ]

    async def load_validation_inputs(self, session, run_id):
        from app.loss.validation import (
            GridResidual,
            LossValidationSnapshot,
        )

        decoded = {
            product_type: LossArtifactCodec.decode_validation_payload(
                product_type,
                row.write.statistics["validation_payload"],
            )
            for product_type, row in self.rows.items()
            if product_type is not LossProductType.VALIDATION
        }
        return LossValidationSnapshot(
            buildings=decoded[LossProductType.BUILDING_DAMAGE],
            population=decoded[LossProductType.POPULATION_IMPACT],
            casualties=decoded[LossProductType.CASUALTIES],
            economic=decoded[LossProductType.ECONOMIC_LOSS],
            resources=decoded[LossProductType.RESOURCE_DEMAND],
            coverage_ratio=1.0,
            grid_residuals=(GridResidual("town", "t1", 0.0),),
            product_snapshot_fingerprints={
                product_type: "f" * 64
                for product_type in decoded
            },
        )

    async def write_product(self, session, write):
        existing = self.rows.get(write.product_type)
        if existing is not None:
            return existing
        row = SimpleNamespace(
            id=uuid4(),
            status=write.status,
            output_checksum=write.output_checksum,
            write=write,
        )
        self.rows[write.product_type] = row
        self.writes.append(write)
        return row


class _FakeExposureService:
    async def prepare(self, session, *, run_id):
        return build_exposure_dataset(
            snapshot_checksum="f" * 64,
            towns=[
                {
                    "town_code": "t1",
                    "county_code": "c1",
                    "town_name": "测试镇",
                    "population_total": 10000.0,
                }
            ],
            geometries=[
                {
                    "town_code": "t1",
                    "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
                }
            ],
            buildings=[
                {
                    "town_code": "t1",
                    "structure": "rc_frame",
                    "era": None,
                    "area_m2": 10000.0,
                }
            ],
        )


class _FakeSpatialService:
    async def compute(self, session, *, run_id):
        return (
            TownIntensityShare(
                town_code="t1",
                intensity_bin=7,
                area_ratio=1.0,
                intensity_min=6.5,
                intensity_max=7.5,
            ),
        )

    async def load_cells(self, session, *, run_id):
        return (LossGridCell("0:0", "t1", 1.0, 1.0),)


class _FakeContextSession:
    def __init__(self, run):
        self.run = run

    async def get(self, model, identifier, *, with_for_update=False):
        assert identifier == self.run.id
        return self.run


class _FakeIntensityRepository:
    def __init__(self):
        self.fusion_id = uuid4()

    async def get_product(self, session, *, run_id, product_type):
        return {
            "id": self.fusion_id,
            "status": "available",
            "output_checksum": "a" * 64,
        }

    async def load_raster(self, session, product_id):
        assert product_id == self.fusion_id
        return [
            np.asarray([[6.5]], dtype=np.float64)
        ], {
            "grid_definition_version": "loss-service-test-grid-v1",
            "crs": "EPSG:32651",
            "resolution_m": 1000,
            "origin_x": 0.0,
            "origin_y": 1000.0,
            "width": 1,
            "height": 1,
        }


async def test_loss_service_executes_all_six_tasks_idempotently(
    monkeypatch,
) -> None:
    run_id = uuid4()
    repository = _FakeLossRepository()
    intensity_repository = _FakeIntensityRepository()
    profile = load_region_loss_profile(
        Path("/config/loss/shanghai-region.yaml")
    )
    parameters = load_parameter_set(
        Path("/app/tests/fixtures/loss-test-parameters.yaml")
    )

    async def load_parameters(session, *, run_id, profile):
        return parameters

    service = LossAssessmentService(
        _FakeSessionFactory(),
        exposure_service=_FakeExposureService(),
        parameter_loader=load_parameters,
        repository=repository,
        assessment_repository=_FakeAssessmentRepository(),
        region_profile=profile,
        spatial_service=_FakeSpatialService(),
        intensity_repository=intensity_repository,
    )

    async def load_context(self, session, run_id, exposure):
        return LossRunContext(
            run_id=str(run_id),
            event_id="e1",
            revision_id="r1",
            report_ingested_at=datetime(2026, 9, 28, tzinfo=UTC),
            region_id="shanghai",
            region_profile_version=profile.version,
            minimum_town_coverage_ratio=profile.minimum_town_coverage_ratio,
            grid_residual_review_threshold=(
                profile.grid_residual_review_threshold
            ),
            fused_intensity_product_id=str(intensity_repository.fusion_id),
            fused_intensity_checksum="a" * 64,
            data_asset_snapshot_checksum=exposure.snapshot_checksum,
        )

    monkeypatch.setattr(
        LossAssessmentService,
        "_load_context",
        load_context,
    )

    first = await service.run_buildings(str(run_id))
    second = await service.run_buildings(str(run_id))
    population = await service.run_population(str(run_id))
    casualties = await service.run_casualties(str(run_id))
    economic = await service.run_economic(str(run_id))
    resources = await service.run_resources(str(run_id))
    validation = await service.run_validate(str(run_id))

    assert first.product_id == second.product_id
    assert first.status == "succeeded"
    assert population.status == "succeeded"
    assert casualties.status == "succeeded"
    assert economic.status == "succeeded"
    assert resources.status == "succeeded"
    assert validation.status == "succeeded"
    assert len(service._assessment_repository.completed) == 6
    assert {
        result["task_key"]
        for _, _, result in service._assessment_repository.completed
    } == {
        "loss.buildings",
        "loss.population",
        "loss.casualties",
        "loss.economic",
        "loss.resources",
        "loss.validate",
    }


async def test_load_context_freezes_region_profile_and_thresholds() -> None:
    profile = load_region_loss_profile(
        Path("/config/loss/shanghai-region.yaml")
    )
    exposure = build_exposure_dataset(
        snapshot_checksum="f" * 64,
        towns=[],
        geometries=[],
        buildings=[],
    )
    run = SimpleNamespace(
        id=uuid4(),
        event_id=uuid4(),
        revision_id=uuid4(),
        report_ingested_at=datetime(2026, 9, 28, tzinfo=UTC),
        snapshot={"region_id": "shanghai"},
    )
    session = _FakeContextSession(run)
    service = LossAssessmentService(
        None,
        region_profile=profile,
        intensity_repository=_FakeIntensityRepository(),
    )

    first = await service._load_context(session, run.id, exposure)
    frozen_snapshot = dict(run.snapshot["region_profile"])
    service._region_profile = replace(
        profile,
        minimum_town_coverage_ratio=0.5,
        grid_residual_review_threshold=0.999,
    )
    second = await service._load_context(session, run.id, exposure)

    assert first.region_profile_version == second.region_profile_version
    assert first.minimum_town_coverage_ratio == profile.minimum_town_coverage_ratio
    assert second.minimum_town_coverage_ratio == profile.minimum_town_coverage_ratio
    assert first.grid_residual_review_threshold == (
        profile.grid_residual_review_threshold
    )
    assert second.grid_residual_review_threshold == (
        profile.grid_residual_review_threshold
    )
    assert run.snapshot["region_profile"] == frozen_snapshot


async def test_uncalibrated_l1_product_is_demoted_to_l2(monkeypatch) -> None:
    from app.loss.domain import LossQualityGrade
    from tests.loss_factories import building_damage_result

    run_id = uuid4()
    repository = _FakeLossRepository()
    intensity_repository = _FakeIntensityRepository()
    profile = load_region_loss_profile(
        Path("/config/loss/shanghai-region.yaml")
    )
    parameters = load_parameter_set(
        Path("/app/tests/fixtures/loss-test-parameters.yaml")
    )

    async def load_parameters(session, *, run_id, profile):
        return parameters

    service = LossAssessmentService(
        _FakeSessionFactory(),
        exposure_service=_FakeExposureService(),
        parameter_loader=load_parameters,
        repository=repository,
        assessment_repository=_FakeAssessmentRepository(),
        region_profile=profile,
        spatial_service=_FakeSpatialService(),
        intensity_repository=intensity_repository,
    )

    async def load_context(self, session, run_id, exposure):
        return LossRunContext(
            run_id=str(run_id),
            event_id="e1",
            revision_id="r1",
            report_ingested_at=datetime(2026, 9, 28, tzinfo=UTC),
            region_id="shanghai",
            region_profile_version=profile.version,
            minimum_town_coverage_ratio=profile.minimum_town_coverage_ratio,
            grid_residual_review_threshold=(
                profile.grid_residual_review_threshold
            ),
            fused_intensity_product_id=str(intensity_repository.fusion_id),
            fused_intensity_checksum="a" * 64,
            data_asset_snapshot_checksum=exposure.snapshot_checksum,
        )

    monkeypatch.setattr(
        LossAssessmentService,
        "_load_context",
        load_context,
    )
    l1_result = replace(
        building_damage_result(),
        quality_grade=LossQualityGrade.L1,
    )
    monkeypatch.setattr(
        "app.loss.service.assess_building_damage",
        lambda exposure, shares, parameters: l1_result,
    )

    outcome = await service.run_buildings(str(run_id))

    assert outcome.status == "succeeded"
    assert repository.writes[0].quality_grade is LossQualityGrade.L2
    assert {
        metric.quality_grade for metric in repository.writes[0].metrics
    } == {LossQualityGrade.L2}


async def test_parallel_loss_tasks_commit_start_task_before_compute(monkeypatch) -> None:
    from tests.loss_factories import (
        building_damage_result,
        population_impact_result,
    )

    run_id = uuid4()
    session_factory = _LockingSessionFactory()
    repository = _FakeLossRepository()
    intensity_repository = _FakeIntensityRepository()
    profile = load_region_loss_profile(
        Path("/config/loss/shanghai-region.yaml")
    )
    parameters = load_parameter_set(
        Path("/app/tests/fixtures/loss-test-parameters.yaml")
    )

    async def load_parameters(session, *, run_id, profile):
        return parameters

    service = LossAssessmentService(
        session_factory,
        exposure_service=_FakeExposureService(),
        parameter_loader=load_parameters,
        repository=repository,
        assessment_repository=_FakeAssessmentRepository(),
        region_profile=profile,
        spatial_service=_FakeSpatialService(),
        intensity_repository=intensity_repository,
    )

    async def load_context(self, session, run_id, exposure):
        return LossRunContext(
            run_id=str(run_id),
            event_id="e1",
            revision_id="r1",
            report_ingested_at=datetime(2026, 9, 28, tzinfo=UTC),
            region_id="shanghai",
            region_profile_version=profile.version,
            minimum_town_coverage_ratio=profile.minimum_town_coverage_ratio,
            grid_residual_review_threshold=(
                profile.grid_residual_review_threshold
            ),
            fused_intensity_product_id=str(intensity_repository.fusion_id),
            fused_intensity_checksum="a" * 64,
            data_asset_snapshot_checksum=exposure.snapshot_checksum,
        )

    monkeypatch.setattr(
        LossAssessmentService,
        "_load_context",
        load_context,
    )

    building_started = asyncio.Event()
    population_started = asyncio.Event()
    release = asyncio.Event()

    async def building_runner(session, prep):
        building_started.set()
        await release.wait()
        result = building_damage_result()
        return {
            scenario: result
            for scenario in (
                LossValueType.LOW,
                LossValueType.CENTRAL,
                LossValueType.HIGH,
            )
        }

    async def population_runner(session, prep):
        population_started.set()
        await release.wait()
        result = population_impact_result()
        return {
            scenario: result
            for scenario in (
                LossValueType.LOW,
                LossValueType.CENTRAL,
                LossValueType.HIGH,
            )
        }

    building_task = asyncio.create_task(
        service._run_model_task(
            str(run_id),
            product_type=LossProductType.BUILDING_DAMAGE,
            task_key="loss.buildings",
            model_type=LossModelType.BUILDING_DAMAGE,
            scenario_runner=building_runner,
        )
    )
    population_task = asyncio.create_task(
        service._run_model_task(
            str(run_id),
            product_type=LossProductType.POPULATION_IMPACT,
            task_key="loss.population",
            model_type=LossModelType.POPULATION_IMPACT,
            scenario_runner=population_runner,
        )
    )

    await asyncio.wait_for(building_started.wait(), timeout=1.0)
    await asyncio.wait_for(population_started.wait(), timeout=1.0)
    release.set()
    await asyncio.gather(building_task, population_task)

    assert session_factory.events.count("write_commit") >= 2
