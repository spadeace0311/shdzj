from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Callable
from uuid import UUID

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun
from app.assessment.repository import AssessmentRepository
from app.config import settings
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.intensity.artifacts import RasterCodec
from app.intensity.domain import GridDefinition, ProductType
from app.intensity.repository import IntensityRepository
from app.loss.artifacts import LossArtifactCodec
from app.loss.asset_bridge import load_locked_parameter_set
from app.loss.buildings import assess_building_damage
from app.loss.casualties import assess_casualties
from app.loss.domain import (
    DamageState,
    LossCalibrationStatus,
    LossMetricValueStatus,
    LossModelType,
    LossProductStatus,
    LossProductType,
    LossQualityGrade,
    LossRunContext,
    LossValueType,
    ParameterSet,
    ResourceKind,
    ScenarioParameters,
)
from app.loss.economic import assess_economic_loss
from app.loss.exposure import ExposureDataset, LossExposureService
from app.loss.population import assess_population_impact
from app.loss.region import (
    RegionLossProfile,
    load_region_loss_profile,
    load_region_loss_profile_from_mapping,
)
from app.loss.repository import (
    LossMetricValueWrite,
    LossProductWrite,
    LossRasterBandWrite,
    LossRasterWrite,
    LossRepository,
)
from app.loss.resources import assess_resource_demand
from app.loss.spatial import (
    LossGridCell,
    TownIntensityDistributionService,
    TownIntensityShare,
    allocate_continuous,
    allocate_integers,
)
from app.loss.validation import (
    ValidationInputUnavailable,
    ValidationContext,
    validate_loss_assessment,
)


VALIDATION_ALGORITHM_VERSION = "loss-validation-v1"
_LOSS_RASTER_CHECKSUM_NAMESPACE = "loss-raster-content-v1"
_SCENARIO_ORDER = (
    LossValueType.LOW,
    LossValueType.CENTRAL,
    LossValueType.HIGH,
)
_BAND_PREFIX = {
    LossProductType.BUILDING_DAMAGE: "buildings",
    LossProductType.POPULATION_IMPACT: "population",
    LossProductType.CASUALTIES: "casualties",
    LossProductType.ECONOMIC_LOSS: "economic",
    LossProductType.RESOURCE_DEMAND: "resources",
}


@dataclass(frozen=True, slots=True)
class LossTaskOutcome:
    product_id: UUID
    task_key: str
    status: str
    started_at: datetime
    completed_at: datetime
    stage_seconds: dict[str, float]


class LossValidationFailed(Exception):
    """Raised when loss validation produces blocking issues."""


@dataclass(frozen=True, slots=True)
class LossChainPerformanceResult:
    status: str
    report_ingested_at: datetime
    query_ready_at: datetime
    elapsed_seconds: float
    stage_seconds: dict[str, float]


@dataclass(frozen=True, slots=True)
class _MetricDescriptor:
    metric_key: str
    unit: str
    precision: int | None
    integer: bool
    scope: str
    extractor: Callable[[object], object]


@dataclass(frozen=True, slots=True)
class _TaskPreparation:
    run_id: UUID
    task_key: str
    product_type: LossProductType
    model_type: LossModelType | None
    algorithm_version: str
    model_version: str
    parameter_version: str
    input_fingerprint: str
    exposure: ExposureDataset
    context: LossRunContext
    profile: RegionLossProfile
    parameter_set: ParameterSet
    shares: tuple[TownIntensityShare, ...]
    coverage_ratio: float


class LossAssessmentService:
    ALGORITHM_VERSIONS = {
        LossModelType.BUILDING_DAMAGE: "building-structure-matrix-v1",
        LossModelType.POPULATION_IMPACT: "population-intensity-v1",
        LossModelType.CASUALTIES: "casualty-building-intensity-v1",
        LossModelType.ECONOMIC_LOSS: "economic-building-loss-v1",
        LossModelType.RESOURCE_DEMAND: "resource-linear-demand-v1",
    }

    def __init__(
        self,
        session_factory,
        *,
        exposure_service=None,
        parameter_loader=None,
        repository=None,
        assessment_repository=None,
        region_profile=None,
        intensity_repository=None,
        spatial_service=None,
    ) -> None:
        self._session_factory = session_factory
        self._region_profile = region_profile or load_region_loss_profile(
            settings.loss_region_profile_path
        )
        self._exposure_service = exposure_service or LossExposureService(
            snapshots=DataAssetSnapshotService(),
            profile=self._region_profile,
        )
        self._parameter_loader = parameter_loader or load_locked_parameter_set
        self._repository = repository or LossRepository()
        self._assessment_repository = assessment_repository or AssessmentRepository()
        self._intensity_repository = intensity_repository or IntensityRepository()
        self._spatial_service = spatial_service or TownIntensityDistributionService(
            profile=self._region_profile
        )
        self._frozen_region_profile: RegionLossProfile | None = None

    async def _load_context(
        self,
        session: AsyncSession,
        run_id: UUID,
        exposure: ExposureDataset,
    ) -> LossRunContext:
        run = await session.get(
            AssessmentRun,
            run_id,
            with_for_update=True,
        )
        if run is None:
            raise LookupError("assessment run not found")
        fusion = await self._intensity_repository.get_product(
            session,
            run_id=run_id,
            product_type=ProductType.FUSION,
        )
        if (
            fusion is None
            or fusion["status"] != "available"
            or not fusion["output_checksum"]
        ):
            raise LookupError("succeeded intensity fusion product not found")
        region_id = run.snapshot.get("region_id")
        if not isinstance(region_id, str) or not region_id:
            raise ValueError("assessment run snapshot requires region_id")

        run_snapshot = dict(run.snapshot or {})
        raw_profile = run_snapshot.get("region_profile")
        if raw_profile is None:
            frozen_profile = self._region_profile
            raw_profile = frozen_profile.to_snapshot()
            run_snapshot["region_profile"] = raw_profile
            run.snapshot = run_snapshot
        elif isinstance(raw_profile, dict):
            frozen_profile = load_region_loss_profile_from_mapping(raw_profile)
        else:
            raise ValueError("assessment run region_profile snapshot is invalid")

        if frozen_profile.region_id != region_id:
            raise ValueError("assessment run region profile does not match region_id")
        self._frozen_region_profile = frozen_profile
        return LossRunContext(
            run_id=str(run.id),
            event_id=str(run.event_id),
            revision_id=str(run.revision_id),
            report_ingested_at=run.report_ingested_at,
            region_id=region_id,
            region_profile_version=frozen_profile.version,
            minimum_town_coverage_ratio=frozen_profile.minimum_town_coverage_ratio,
            grid_residual_review_threshold=(
                frozen_profile.grid_residual_review_threshold
            ),
            fused_intensity_product_id=str(fusion["id"]),
            fused_intensity_checksum=fusion["output_checksum"],
            data_asset_snapshot_checksum=exposure.snapshot_checksum,
        )

    async def run_buildings(self, run_id: str) -> LossTaskOutcome:
        return await self._run_model_task(
            run_id,
            product_type=LossProductType.BUILDING_DAMAGE,
            task_key="loss.buildings",
            model_type=LossModelType.BUILDING_DAMAGE,
            scenario_runner=self._building_scenarios,
        )

    async def run_population(self, run_id: str) -> LossTaskOutcome:
        return await self._run_model_task(
            run_id,
            product_type=LossProductType.POPULATION_IMPACT,
            task_key="loss.population",
            model_type=LossModelType.POPULATION_IMPACT,
            scenario_runner=self._population_scenarios,
        )

    async def run_casualties(self, run_id: str) -> LossTaskOutcome:
        return await self._run_model_task(
            run_id,
            product_type=LossProductType.CASUALTIES,
            task_key="loss.casualties",
            model_type=LossModelType.CASUALTIES,
            scenario_runner=self._casualty_scenarios,
        )

    async def run_economic(self, run_id: str) -> LossTaskOutcome:
        return await self._run_model_task(
            run_id,
            product_type=LossProductType.ECONOMIC_LOSS,
            task_key="loss.economic",
            model_type=LossModelType.ECONOMIC_LOSS,
            scenario_runner=self._economic_scenarios,
        )

    async def run_resources(self, run_id: str) -> LossTaskOutcome:
        return await self._run_model_task(
            run_id,
            product_type=LossProductType.RESOURCE_DEMAND,
            task_key="loss.resources",
            model_type=LossModelType.RESOURCE_DEMAND,
            scenario_runner=self._resource_scenarios,
        )

    async def run_validate(self, run_id: str) -> LossTaskOutcome:
        started_at = datetime.now(UTC)
        run_uuid = _coerce_uuid(run_id)
        task_id: UUID | None = None
        validation_failed = False
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    prep_started = perf_counter()
                    exposure = await self._exposure_service.prepare(
                        session,
                        run_id=run_uuid,
                    )
                    context = await self._load_context(
                        session,
                        run_uuid,
                        exposure,
                    )
                    profile = self._resolved_profile(context)
                    parameter_set = await self._parameter_loader(
                        session,
                        run_id=run_uuid,
                        profile=profile,
                    )
                    prep_seconds = perf_counter() - prep_started
                    input_fingerprint = _input_fingerprint(
                        context,
                        profile,
                        parameter_set,
                        VALIDATION_ALGORITHM_VERSION,
                    )
                    task = await self._assessment_repository.start_task(
                        session,
                        run_uuid,
                        "loss.validate",
                        VALIDATION_ALGORITHM_VERSION,
                        input_fingerprint,
                    )
                    task_id = task.id
                    if task.status == "succeeded":
                        return await self._existing_outcome(
                            session,
                            run_uuid,
                            task,
                            LossProductType.VALIDATION,
                            {},
                            started_at,
                        )

                    validation_started = perf_counter()
                    snapshot = await self._repository.load_validation_inputs(
                        session,
                        run_uuid,
                    )
                    validation_context = ValidationContext(
                        region_profile_version=context.region_profile_version,
                        minimum_town_coverage_ratio=(
                            context.minimum_town_coverage_ratio
                        ),
                        grid_residual_review_threshold=(
                            context.grid_residual_review_threshold
                        ),
                        data_asset_snapshot_fingerprint=(
                            context.data_asset_snapshot_checksum
                        ),
                        product_snapshot_fingerprints=(
                            snapshot.product_snapshot_fingerprints
                        ),
                    )
                    validation_result = validate_loss_assessment(
                        snapshot.buildings,
                        snapshot.population,
                        snapshot.casualties,
                        snapshot.economic,
                        snapshot.resources,
                        coverage_ratio=snapshot.coverage_ratio,
                        grid_residuals=snapshot.grid_residuals,
                        context=validation_context,
                    )
                    stage_seconds = {
                        "validate_and_persist": (
                            perf_counter() - validation_started
                        )
                    }
                    statistics = {
                        "stage_seconds": stage_seconds,
                        "blocking_issues": [
                            _validation_issue_payload(issue)
                            for issue in validation_result.blocking_issues
                        ],
                        "review_issues": [
                            _validation_issue_payload(issue)
                            for issue in validation_result.review_issues
                        ],
                    }
                    output_checksum = _validation_checksum(validation_result)
                    write = LossProductWrite(
                        run_id=run_uuid,
                        task_id=task.id,
                        product_type=LossProductType.VALIDATION,
                        status=(
                            LossProductStatus.COMPLETE
                            if validation_result.valid
                            else LossProductStatus.INVALID
                        ),
                        quality_grade=validation_result.quality_grade,
                        calibration_status=parameter_set.calibration_status,
                        coverage_ratio=snapshot.coverage_ratio,
                        partial_scope=False,
                        needs_review=validation_result.needs_review,
                        spatialized_estimate=False,
                        algorithm_version=VALIDATION_ALGORITHM_VERSION,
                        parameter_version=parameter_set.version,
                        region_profile_version=context.region_profile_version,
                        input_fingerprint=input_fingerprint,
                        input_checksum=input_fingerprint,
                        output_checksum=output_checksum,
                        statistics=statistics,
                        metrics=(),
                        raster=None,
                        reason=None,
                    )
                    product = await self._repository.write_product(
                        session,
                        write,
                    )
                    if validation_result.valid:
                        await self._assessment_repository.complete_task(
                            session,
                            task.id,
                            output_checksum,
                            {
                                "product_id": str(product.id),
                                "task_key": "loss.validate",
                                "valid": validation_result.valid,
                                "needs_review": (
                                    validation_result.needs_review
                                ),
                                "quality_grade": (
                                    validation_result.quality_grade.value
                                ),
                                "stage_seconds": stage_seconds,
                            },
                        )
                        return LossTaskOutcome(
                            product_id=product.id,
                            task_key="loss.validate",
                            status="succeeded",
                            started_at=started_at,
                            completed_at=datetime.now(UTC),
                            stage_seconds={
                                **stage_seconds,
                                "snapshot_and_exposure": prep_seconds,
                            },
                        )
                    else:
                        await self._assessment_repository.fail_task(
                            session,
                            task.id,
                            "validation_failed",
                            "loss validation produced blocking issues",
                        )
                        validation_failed = True

            if validation_failed:
                raise LossValidationFailed(
                    "loss validation produced blocking issues"
                )
        except Exception as exc:
            await self._record_task_failure(
                run_id,
                "loss.validate",
                exc,
                task_id=task_id,
            )
            raise

    async def _run_model_task(
        self,
        run_id: str,
        *,
        product_type: LossProductType,
        task_key: str,
        model_type: LossModelType,
        scenario_runner,
    ) -> LossTaskOutcome:
        started_at = datetime.now(UTC)
        run_uuid = _coerce_uuid(run_id)
        task_id: UUID | None = None
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    prep_started = perf_counter()
                    exposure = await self._exposure_service.prepare(
                        session,
                        run_id=run_uuid,
                    )
                    context = await self._load_context(
                        session,
                        run_uuid,
                        exposure,
                    )
                    profile = self._resolved_profile(context)
                    parameter_set = await self._parameter_loader(
                        session,
                        run_id=run_uuid,
                        profile=profile,
                    )
                    model = parameter_set.models[model_type]
                    prep_seconds = perf_counter() - prep_started
                    input_fingerprint = _input_fingerprint(
                        context,
                        profile,
                        parameter_set,
                        model.formula_version,
                    )
                    algorithm_version = self.ALGORITHM_VERSIONS[model_type]
                    task = await self._assessment_repository.start_task(
                        session,
                        run_uuid,
                        task_key,
                        algorithm_version,
                        input_fingerprint,
                    )
                    task_id = task.id
                    if task.status == "succeeded":
                        return await self._existing_outcome(
                            session,
                            run_uuid,
                            task,
                            product_type,
                            {},
                            started_at,
                        )

            async with self._session_factory() as session:
                shares = await self._town_intensity_shares(
                    session,
                    run_uuid,
                )

            prep = _TaskPreparation(
                run_id=run_uuid,
                task_key=task_key,
                product_type=product_type,
                model_type=model_type,
                algorithm_version=algorithm_version,
                model_version=model.formula_version,
                parameter_version=parameter_set.version,
                input_fingerprint=input_fingerprint,
                exposure=exposure,
                context=context,
                profile=profile,
                parameter_set=parameter_set,
                shares=shares,
                coverage_ratio=_coverage_ratio(exposure, shares),
            )

            async with self._session_factory() as session:
                    model_started = perf_counter()
                    scenario_results = await scenario_runner(session, prep)
                    model_seconds = perf_counter() - model_started
                    central_result = scenario_results[LossValueType.CENTRAL]
                    metrics = _build_metrics(prep, scenario_results, central_result)
                    raster = await self._build_raster(
                        session,
                        prep,
                        scenario_results,
                        metrics,
                    )
                    stage_key = (
                        "buildings_and_population"
                        if model_type
                        in {
                            LossModelType.BUILDING_DAMAGE,
                            LossModelType.POPULATION_IMPACT,
                        }
                        else "casualties_economic_resources"
                    )
                    stage_seconds = {
                        stage_key: model_seconds,
                    }
                    if model_type is LossModelType.BUILDING_DAMAGE:
                        stage_seconds["snapshot_and_exposure"] = prep_seconds
                    quality_grade = _central_quality_grade(
                        prep,
                        central_result,
                    )
                    statistics = {
                        "data_asset_snapshot_fingerprint": (
                            exposure.snapshot_checksum
                        ),
                        "validation_payload": (
                            LossArtifactCodec.encode_validation_payload(
                                product_type,
                                central_result,
                            )
                        ),
                        "scenario_validation_payloads": {
                            scenario.value: (
                                LossArtifactCodec.encode_validation_payload(
                                    product_type,
                                    scenario_results[scenario],
                                )
                            )
                            for scenario in _SCENARIO_ORDER
                        },
                        "stage_seconds": stage_seconds,
                    }
                    output_checksum = _model_product_checksum(
                        product_type,
                        metrics,
                        statistics,
                    )
                    write = LossProductWrite(
                        run_id=run_uuid,
                        task_id=task.id,
                        product_type=product_type,
                        status=LossProductStatus.COMPLETE,
                        quality_grade=quality_grade,
                        calibration_status=parameter_set.calibration_status,
                        coverage_ratio=prep.coverage_ratio,
                        partial_scope=(
                            central_result.partial_scope
                            if product_type is LossProductType.ECONOMIC_LOSS
                            else False
                        ),
                        needs_review=False,
                        spatialized_estimate=raster is not None,
                        algorithm_version=algorithm_version,
                        parameter_version=parameter_set.version,
                        region_profile_version=context.region_profile_version,
                        input_fingerprint=input_fingerprint,
                        input_checksum=input_fingerprint,
                        output_checksum=output_checksum,
                        statistics=statistics,
                        metrics=metrics,
                        raster=raster,
                        reason=None,
                    )

            async with self._session_factory() as session:
                async with session.begin():
                    product = await self._repository.write_product(
                        session,
                        write,
                    )
                    await self._assessment_repository.complete_task(
                        session,
                        task.id,
                        output_checksum,
                        {
                            "product_id": str(product.id),
                            "task_key": task_key,
                            "stage_seconds": stage_seconds,
                        },
                    )
                return LossTaskOutcome(
                    product_id=product.id,
                    task_key=task_key,
                    status="succeeded",
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    stage_seconds=stage_seconds,
                )
        except Exception as exc:
            await self._record_task_failure(
                run_id,
                task_key,
                exc,
                task_id=task_id,
            )
            raise

    async def _existing_outcome(
        self,
        session,
        run_id: UUID,
        task,
        product_type: LossProductType,
        stage_seconds: dict[str, float],
        started_at: datetime,
    ) -> LossTaskOutcome:
        product = await self._repository.get_product(
            session,
            run_id,
            product_type,
        )
        if product is None:
            raise LookupError(
                f"completed {task.task_key} task has no persisted loss product"
            )
        return LossTaskOutcome(
            product_id=product.id,
            task_key=task.task_key,
            status="succeeded",
            started_at=started_at,
            completed_at=datetime.now(UTC),
            stage_seconds=stage_seconds,
        )

    async def _building_scenarios(self, session, prep):
        return {
            scenario: assess_building_damage(
                prep.exposure,
                prep.shares,
                _scenario_parameters(
                    prep.parameter_set,
                    LossModelType.BUILDING_DAMAGE,
                    scenario,
                ),
            )
            for scenario in _SCENARIO_ORDER
        }

    async def _population_scenarios(self, session, prep):
        return {
            scenario: assess_population_impact(
                prep.exposure,
                prep.shares,
                _scenario_parameters(
                    prep.parameter_set,
                    LossModelType.POPULATION_IMPACT,
                    scenario,
                ),
            )
            for scenario in _SCENARIO_ORDER
        }

    async def _casualty_scenarios(self, session, prep):
        buildings = await self._scenario_results(
            session,
            prep,
            LossProductType.BUILDING_DAMAGE,
        )
        population = await self._scenario_results(
            session,
            prep,
            LossProductType.POPULATION_IMPACT,
        )
        return {
            scenario: assess_casualties(
                buildings[scenario],
                population[scenario],
                _scenario_parameters(
                    prep.parameter_set,
                    LossModelType.CASUALTIES,
                    scenario,
                ),
            )
            for scenario in _SCENARIO_ORDER
        }

    async def _economic_scenarios(self, session, prep):
        buildings = await self._scenario_results(
            session,
            prep,
            LossProductType.BUILDING_DAMAGE,
        )
        return {
            scenario: assess_economic_loss(
                buildings[scenario],
                _scenario_parameters(
                    prep.parameter_set,
                    LossModelType.ECONOMIC_LOSS,
                    scenario,
                ),
            )
            for scenario in _SCENARIO_ORDER
        }

    async def _resource_scenarios(self, session, prep):
        buildings = await self._scenario_results(
            session,
            prep,
            LossProductType.BUILDING_DAMAGE,
        )
        population = await self._scenario_results(
            session,
            prep,
            LossProductType.POPULATION_IMPACT,
        )
        casualties = await self._scenario_results(
            session,
            prep,
            LossProductType.CASUALTIES,
        )
        return {
            scenario: assess_resource_demand(
                buildings[scenario],
                population[scenario],
                casualties[scenario],
                _scenario_parameters(
                    prep.parameter_set,
                    LossModelType.RESOURCE_DEMAND,
                    scenario,
                ),
            )
            for scenario in _SCENARIO_ORDER
        }

    async def _scenario_results(self, session, prep, product_type):
        product = await self._repository.get_product(
            session,
            prep.run_id,
            product_type,
        )
        if product is None:
            raise LookupError(
                f"loss dependency {product_type.value} product not found"
            )
        statistics = _product_statistics(product)
        payloads = statistics.get("scenario_validation_payloads")
        if isinstance(payloads, dict):
            return {
                LossValueType(scenario): LossArtifactCodec.decode_validation_payload(
                    product_type,
                    payload,
                )
                for scenario, payload in payloads.items()
            }
        central = statistics.get("validation_payload")
        if central is None:
            raise LookupError(
                f"loss dependency {product_type.value} has no validation payload"
            )
        return {
            LossValueType.CENTRAL: LossArtifactCodec.decode_validation_payload(
                product_type,
                central,
            )
        }

    async def _town_intensity_shares(
        self,
        session,
        run_id,
    ) -> tuple[TownIntensityShare, ...]:
        return tuple(
            await self._spatial_service.compute(
                session,
                run_id=run_id,
            )
        )

    async def _build_raster(
        self,
        session,
        prep: _TaskPreparation,
        scenario_results,
        metrics,
    ) -> LossRasterWrite | None:
        numeric_series = _town_metric_series(metrics)
        if not numeric_series:
            return None
        cells = tuple(
            await self._spatial_service.load_cells(
                session,
                run_id=prep.run_id,
            )
        )
        if not cells:
            raise LookupError("town metrics have no grid cells for rasterization")
        definition, positions = await self._resolve_grid(
            session,
            prep,
            cells,
        )

        bands: list[LossRasterBandWrite] = []
        manifest_bands: list[dict[str, object]] = []
        reconciliation: dict[str, dict[str, dict[str, float]]] = {
            "town": {}
        }
        for metric_key, scenario_values in sorted(numeric_series.items()):
            for scenario, town_values in sorted(
                scenario_values.items(),
                key=lambda item: _SCENARIO_ORDER.index(item[0]),
            ):
                band_name = _band_name(
                    prep.product_type,
                    metric_key,
                    scenario,
                )
                array = _allocated_array(
                    town_values,
                    cells,
                    positions,
                    definition,
                    integer=_integer_metric_key(
                        prep.product_type,
                        metric_key,
                    ),
                )
                bands.append(
                    LossRasterBandWrite(
                        name=band_name,
                        values=np.ascontiguousarray(array, dtype=np.float64),
                        unit=_metric_unit(metrics, metric_key),
                        precision=_metric_precision(metrics, metric_key),
                    )
                )
                manifest_bands.append(
                    {
                        "number": len(manifest_bands) + 1,
                        "name": band_name,
                        "metric_key": metric_key,
                        "scenario": scenario.value,
                        "unit": _metric_unit(metrics, metric_key),
                        "precision": _metric_precision(metrics, metric_key),
                    }
                )
                if (
                    prep.product_type is LossProductType.BUILDING_DAMAGE
                    and scenario is LossValueType.CENTRAL
                    and metric_key == "collapsed_area_m2"
                ):
                    for cell in cells:
                        town_values.setdefault(cell.town_code, 0.0)
                    for town_code in sorted(town_values):
                        allocated = sum(
                            array[positions[cell.cell_id]]
                            for cell in cells
                            if cell.town_code == town_code
                        )
                        reconciliation["town"][town_code] = {
                            "residual": float(
                                allocated - float(town_values[town_code])
                            )
                        }
        manifest = {
            "grid": {
                "version": definition.version,
                "crs": definition.crs,
                "resolution_m": definition.resolution_m,
                "origin_x": definition.origin_x,
                "origin_y": definition.origin_y,
                "width": definition.width,
                "height": definition.height,
            },
            "bands": manifest_bands,
            "reconciliation": reconciliation,
        }
        normalized_bands = [
            (band.name, np.asarray(band.values, dtype=np.float64))
            for band in bands
        ]
        checksum = RasterCodec.content_checksum(
            definition,
            normalized_bands,
            manifest,
            checksum_namespace=_LOSS_RASTER_CHECKSUM_NAMESPACE,
        )
        return LossRasterWrite(
            raster_version=definition.version,
            definition=definition,
            bands=tuple(bands),
            band_manifest=manifest,
            checksum=checksum,
            spatial_allocation_rule=prep.profile.spatial_allocation_rule,
            coverage_ratio=prep.coverage_ratio,
        )

    async def _resolve_grid(
        self,
        session,
        prep: _TaskPreparation,
        cells: tuple[LossGridCell, ...],
    ) -> tuple[GridDefinition, dict[str, tuple[int, int]]]:
        _, metadata = await self._intensity_repository.load_raster(
            session,
            UUID(prep.context.fused_intensity_product_id),
        )
        definition = GridDefinition(
            version=str(metadata["grid_definition_version"]),
            crs=str(metadata["crs"]),
            resolution_m=int(metadata["resolution_m"]),
            origin_x=float(metadata["origin_x"]),
            origin_y=float(metadata["origin_y"]),
            width=int(metadata["width"]),
            height=int(metadata["height"]),
        )
        positions = _grid_positions(cells, definition)
        return definition, positions

    async def _record_task_failure(
        self,
        run_id: str,
        task_key: str,
        exc: Exception,
        *,
        task_id: UUID | None,
    ) -> None:
        try:
            run_uuid = _coerce_uuid(run_id)
        except ValueError:
            return
        if isinstance(exc, LossValidationFailed):
            category = "validation_failed"
            summary = "loss validation produced blocking issues"
        elif isinstance(exc, ValidationInputUnavailable):
            category = "validation_input_unavailable"
            summary = "loss validation inputs are unavailable"
        elif isinstance(exc, LookupError):
            category = "lookup_error"
            summary = "required loss assessment data was not found"
        elif isinstance(exc, ValueError):
            category = "invalid_input"
            summary = "loss assessment input was invalid"
        elif isinstance(exc, ArithmeticError):
            category = "computation_error"
            summary = "loss assessment computation failed"
        else:
            category = "internal_error"
            summary = "loss assessment task failed"
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    if task_id is not None and hasattr(
                        self._assessment_repository,
                        "fail_task",
                    ):
                        try:
                            await self._assessment_repository.fail_task(
                                session,
                                task_id,
                                category,
                                summary,
                            )
                        except Exception:
                            pass
                    if hasattr(
                        self._assessment_repository,
                        "record_task_failure_audit",
                    ):
                        await self._assessment_repository.record_task_failure_audit(
                            session,
                            run_uuid,
                            task_key,
                            category,
                            summary,
                        )
        except Exception:
            # Failure auditing is best-effort; the original exception remains
            # authoritative.
            return

    def _resolved_profile(self, context: LossRunContext) -> RegionLossProfile:
        return self._frozen_region_profile or self._region_profile


def _coerce_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("run_id must be a UUID") from exc


def _input_fingerprint(
    context: LossRunContext,
    profile: RegionLossProfile,
    parameter_set: ParameterSet,
    model_version: str,
) -> str:
    payload = {
        "run_id": context.run_id,
        "revision_id": context.revision_id,
        "fusion_product_id": context.fused_intensity_product_id,
        "fusion_checksum": context.fused_intensity_checksum,
        "data_snapshot_checksum": context.data_asset_snapshot_checksum,
        "region_profile_version": profile.version,
        "model_version": model_version,
        "parameter_version": parameter_set.version,
        "scenario_versions": ["low", "central", "high"],
    }
    return _sha256(payload)


def _sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_quality_grade(
    grade: LossQualityGrade,
    calibration_status: LossCalibrationStatus,
) -> LossQualityGrade:
    if (
        grade is LossQualityGrade.L1
        and calibration_status is not LossCalibrationStatus.CALIBRATED
    ):
        return LossQualityGrade.L2
    return grade


def _central_quality_grade(
    prep: _TaskPreparation,
    central_result,
) -> LossQualityGrade:
    if prep.product_type in {
        LossProductType.BUILDING_DAMAGE,
        LossProductType.POPULATION_IMPACT,
        LossProductType.CASUALTIES,
    }:
        grade = central_result.quality_grade
    else:
        grade = LossQualityGrade.L2
    return _safe_quality_grade(
        grade,
        prep.parameter_set.calibration_status,
    )


def _coverage_ratio(
    exposure: ExposureDataset,
    shares: tuple[TownIntensityShare, ...],
) -> float:
    if not exposure.towns:
        return 1.0
    covered = {share.town_code for share in shares}
    required = {
        town.town_code
        for town in exposure.towns
        if town.population_total > 0.0 or town.buildings
    }
    if not required:
        return 1.0
    return len(required & covered) / len(required)


def _scenario_parameters(
    parameter_set: ParameterSet,
    model_type: LossModelType,
    scenario: LossValueType,
) -> ScenarioParameters:
    return parameter_set.models[model_type].scenarios[scenario]


def _metric_descriptors(product_type: LossProductType) -> tuple[_MetricDescriptor, ...]:
    if product_type is LossProductType.BUILDING_DAMAGE:
        return (
            _MetricDescriptor(
                "total_area_m2",
                "m2",
                2,
                False,
                "town",
                lambda town: town.total_area_m2,
            ),
            _MetricDescriptor(
                "severe_or_collapsed_area_m2",
                "m2",
                2,
                False,
                "town",
                lambda town: sum(
                    state.area_m2
                    for state in town.states
                    if state.damage_state
                    in {
                        DamageState.SEVERELY_DAMAGED,
                        DamageState.COLLAPSED,
                    }
                ),
            ),
            _MetricDescriptor(
                "collapsed_area_m2",
                "m2",
                2,
                False,
                "town",
                lambda town: sum(
                    state.area_m2
                    for state in town.states
                    if state.damage_state is DamageState.COLLAPSED
                ),
            ),
        )
    if product_type is LossProductType.POPULATION_IMPACT:
        return tuple(
            _MetricDescriptor(
                key,
                "count",
                2,
                False,
                "town",
                lambda town, key=key: getattr(town, key),
            )
            for key in (
                "full_population",
                "affected_population",
                "emergency_shelter_population",
                "temporary_shelter_population",
            )
        )
    if product_type is LossProductType.CASUALTIES:
        return tuple(
            _MetricDescriptor(
                key,
                "count",
                2,
                False,
                "town",
                lambda town, key=key: getattr(town, key),
            )
            for key in ("deaths", "injuries", "buried")
        )
    if product_type is LossProductType.ECONOMIC_LOSS:
        return tuple(
            _MetricDescriptor(
                key,
                "yuan",
                2,
                False,
                "town",
                lambda town, key=key: getattr(town, key),
            )
            for key in (
                "reconstruction_loss_yuan",
                "contents_loss_yuan",
                "total_loss_yuan",
            )
        )
    return tuple(
        _MetricDescriptor(
            f"{kind.value}.quantity",
            "count",
            0,
            True,
            "city",
            lambda result, kind=kind: result.values[kind].quantity,
        )
        for kind in ResourceKind
    )


def _build_metrics(
    prep: _TaskPreparation,
    scenario_results,
    central_result,
) -> tuple[LossMetricValueWrite, ...]:
    descriptors = _metric_descriptors(prep.product_type)
    town_names = {
        town.town_code: town.town_name for town in prep.exposure.towns
    }
    town_counties = {
        town.town_code: town.county_code for town in prep.exposure.towns
    }
    quality_grade = _central_quality_grade(prep, central_result)
    writes: list[LossMetricValueWrite] = []
    for scenario in _SCENARIO_ORDER:
        result = scenario_results[scenario]
        for descriptor in descriptors:
            if descriptor.scope == "town":
                town_values: dict[str, object] = {}
                for town_code in sorted(result.towns):
                    value = descriptor.extractor(result.towns[town_code])
                    town_values[town_code] = value
                    writes.append(
                        _metric_write(
                            prep,
                            descriptor,
                            scenario,
                            value,
                            area_scope="town",
                            area_code=town_code,
                            area_name=town_names.get(town_code),
                            note=None,
                            quality_grade=quality_grade,
                        )
                    )
                county_values: dict[str, dict[str, object]] = {}
                for town_code, value in town_values.items():
                    county_code = town_counties.get(
                        town_code,
                        town_code[:9],
                    )
                    county_values.setdefault(county_code, {})[
                        town_code
                    ] = value
                for county_code in sorted(county_values):
                    writes.append(
                        _metric_write(
                            prep,
                            descriptor,
                            scenario,
                            _aggregate_metric_value(
                                county_values[county_code]
                            ),
                            area_scope="county",
                            area_code=county_code,
                            area_name=None,
                            note=None,
                            quality_grade=quality_grade,
                        )
                    )
                writes.append(
                    _metric_write(
                        prep,
                        descriptor,
                        scenario,
                        _aggregate_metric_value(town_values),
                        area_scope="city",
                        area_code=prep.exposure.city.area_code,
                        area_name=prep.exposure.city.area_name,
                        note=None,
                        quality_grade=quality_grade,
                    )
                )
            else:
                value = descriptor.extractor(result)
                writes.append(
                    _metric_write(
                        prep,
                        descriptor,
                        scenario,
                        value,
                        area_scope="city",
                        area_code=prep.exposure.city.area_code,
                        area_name=prep.exposure.city.area_name,
                        note=(
                            result.values[
                                ResourceKind(descriptor.metric_key.split(".", 1)[0])
                            ].reason
                            if descriptor.integer
                            and value is None
                            else None
                        ),
                        quality_grade=quality_grade,
                    )
                )
    return tuple(writes)


def _aggregate_metric_value(values: dict[str, object]) -> object:
    if not values:
        return None
    total = 0.0
    for value in values.values():
        if value is None:
            return None
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            return None
        total += numeric
    return total


def _metric_write(
    prep: _TaskPreparation,
    descriptor: _MetricDescriptor,
    scenario: LossValueType,
    value: object,
    *,
    area_scope: str,
    area_code: str,
    area_name: str | None,
    note: str | None,
    quality_grade: LossQualityGrade,
) -> LossMetricValueWrite:
    if value is None:
        return LossMetricValueWrite(
            area_scope=area_scope,
            area_code=area_code,
            area_name=area_name,
            metric_key=descriptor.metric_key,
            value_type=scenario,
            value_status=LossMetricValueStatus.UNAVAILABLE,
            numeric_value=None,
            unit=descriptor.unit,
            precision=descriptor.precision,
            quality_grade=quality_grade,
            note=note,
        )
    numeric = int(round(float(value))) if descriptor.integer else float(value)
    status = (
        LossMetricValueStatus.ZERO
        if numeric == 0
        else LossMetricValueStatus.AVAILABLE
    )
    return LossMetricValueWrite(
        area_scope=area_scope,
        area_code=area_code,
        area_name=area_name,
        metric_key=descriptor.metric_key,
        value_type=scenario,
        value_status=status,
        numeric_value=numeric,
        unit=descriptor.unit,
        precision=descriptor.precision,
        quality_grade=quality_grade,
        note=note,
    )


def _town_metric_series(
    metrics: tuple[LossMetricValueWrite, ...],
) -> dict[str, dict[LossValueType, dict[str, float | int]]]:
    series: dict[str, dict[LossValueType, dict[str, float | int]]] = {}
    for metric in metrics:
        if metric.area_scope != "town" or metric.numeric_value is None:
            continue
        series.setdefault(metric.metric_key, {})[metric.value_type] = {
            **series.setdefault(metric.metric_key, {}).get(
                metric.value_type,
                {},
            ),
            metric.area_code: metric.numeric_value,
        }
    return series


def _metric_unit(
    metrics: tuple[LossMetricValueWrite, ...],
    metric_key: str,
) -> str:
    return next(
        metric.unit for metric in metrics if metric.metric_key == metric_key
    )


def _metric_precision(
    metrics: tuple[LossMetricValueWrite, ...],
    metric_key: str,
) -> int | None:
    return next(
        metric.precision for metric in metrics if metric.metric_key == metric_key
    )


def _integer_metric_key(
    product_type: LossProductType,
    metric_key: str,
) -> bool:
    return (
        product_type is LossProductType.RESOURCE_DEMAND
        and metric_key.endswith(".quantity")
    )


def _band_name(
    product_type: LossProductType,
    metric_key: str,
    scenario: LossValueType | None = None,
) -> str:
    suffix = f"_{scenario.value}" if scenario is not None else ""
    return f"{_BAND_PREFIX[product_type]}_{metric_key}{suffix}"


def _allocated_array(
    town_values: dict[str, float | int],
    cells: tuple[LossGridCell, ...],
    positions: dict[str, tuple[int, int]],
    definition: GridDefinition,
    *,
    integer: bool,
) -> np.ndarray:
    array = np.zeros(
        (definition.height, definition.width),
        dtype=np.float64,
    )
    if integer:
        allocated = allocate_integers(
            {town: int(value) for town, value in town_values.items()},
            cells,
        )
    else:
        allocated = allocate_continuous(
            {town: float(value) for town, value in town_values.items()},
            cells,
        )
    for cell in cells:
        row, column = positions[cell.cell_id]
        array[row, column] = float(allocated[cell.cell_id])
    return array


def _grid_positions(
    cells: tuple[LossGridCell, ...],
    definition: GridDefinition,
) -> dict[str, tuple[int, int]]:
    positions: dict[str, tuple[int, int]] = {}
    for cell in cells:
        try:
            row_text, column_text = cell.cell_id.split(":", 1)
            row = int(row_text)
            column = int(column_text)
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                "grid cell_id must encode row:column"
            ) from exc
        if not 0 <= row < definition.height:
            raise ValueError("grid cell row is outside the raster")
        if not 0 <= column < definition.width:
            raise ValueError("grid cell column is outside the raster")
        positions[cell.cell_id] = (row, column)
    return positions


def _product_statistics(product) -> dict:
    if hasattr(product, "write"):
        return dict(getattr(product.write, "statistics", {}) or {})
    return dict(getattr(product, "statistics", {}) or {})


def _model_product_checksum(
    product_type: LossProductType,
    metrics: tuple[LossMetricValueWrite, ...],
    statistics: dict,
) -> str:
    payload = {
        "namespace": "loss-service-product-v1",
        "product_type": product_type.value,
        "metrics": [
            {
                "area_scope": metric.area_scope,
                "area_code": metric.area_code,
                "area_name": metric.area_name,
                "metric_key": metric.metric_key,
                "value_type": metric.value_type.value,
                "value_status": metric.value_status.value,
                "numeric_value": metric.numeric_value,
                "unit": metric.unit,
                "precision": metric.precision,
                "quality_grade": metric.quality_grade.value,
                "note": metric.note,
            }
            for metric in metrics
        ],
        "statistics": statistics,
    }
    return _sha256(payload)


def _validation_issue_payload(issue) -> dict:
    return {
        "code": issue.code,
        "message": issue.message,
        "blocking": issue.blocking,
        "context": dict(issue.context),
    }


def _validation_checksum(result) -> str:
    return _sha256(
        {
            "namespace": "loss-validation-result-v1",
            "valid": result.valid,
            "needs_review": result.needs_review,
            "quality_grade": result.quality_grade.value,
            "blocking_issues": [
                _validation_issue_payload(issue)
                for issue in result.blocking_issues
            ],
            "review_issues": [
                _validation_issue_payload(issue)
                for issue in result.review_issues
            ],
        }
    )
