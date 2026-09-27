from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun
from app.assessment.repository import AssessmentRepository
from app.config import settings
from app.events.models import EarthquakeRevision
from app.intensity.domain import (
    DirectionDecision,
    DirectionStatus,
    GridDefinition,
    InstrumentProduct,
    IntensityEventSnapshot,
    ModelField,
    ProductStatus,
    ProductType,
)
from app.intensity.fusion import fuse
from app.intensity.grid import sample_grid
from app.intensity.instrument import (
    InstrumentRequest,
    InstrumentIntensityProvider,
    UnavailableInstrumentProvider,
    validate_instrument_product,
)
from app.intensity.model import FaultCandidate, evaluate_model, resolve_direction
from app.intensity.parameters import ParameterBundle
from app.intensity.region import RegionProfile, load_region_profile
from app.intensity.repository import IntensityProductWrite, IntensityRepository


@dataclass(frozen=True, slots=True)
class IntensityTaskOutcome:
    run_id: str
    task_key: str
    status: str
    product_id: str | None
    output_checksum: str | None


@dataclass(frozen=True, slots=True)
class DirectionInputs:
    override_deg: float | None = None
    focal_mechanism_deg: float | None = None
    finite_fault_deg: float | None = None
    candidates: tuple[FaultCandidate, ...] = ()


class DirectionInputProvider(Protocol):
    async def load(
        self,
        session: AsyncSession,
        revision: EarthquakeRevision,
    ) -> DirectionInputs: ...


class NoDirectionInputProvider:
    async def load(
        self,
        session: AsyncSession,
        revision: EarthquakeRevision,
    ) -> DirectionInputs:
        return DirectionInputs()


class IntensityService:
    MODEL_ALGORITHM_VERSION = "model-axis-ratio-v1"
    INSTRUMENT_ALGORITHM_VERSION = "instrument-grid-v1"
    FUSION_ALGORITHM_VERSION = "fusion-inverse-variance-v1"

    def __init__(
        self,
        *,
        session_factory,
        parameters: ParameterBundle,
        region_profile: RegionProfile | None = None,
        fixed_grid_definition: GridDefinition | None = None,
        instrument_provider: InstrumentIntensityProvider | None = None,
        direction_provider: DirectionInputProvider | None = None,
        assessment_repository: AssessmentRepository | None = None,
        intensity_repository: IntensityRepository | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.parameters = parameters
        self.region_profile = region_profile or load_region_profile(
            settings.intensity_region_profile_path
        )
        if self.region_profile.model_parameter_version != parameters.version:
            raise ValueError("region profile model parameter version mismatch")
        if self.region_profile.fusion_strategy_version != self.FUSION_ALGORITHM_VERSION:
            raise ValueError("region profile fusion strategy version mismatch")
        self.instrument_provider = instrument_provider or UnavailableInstrumentProvider()
        self.direction_provider = direction_provider or NoDirectionInputProvider()
        self.assessment_repository = assessment_repository or AssessmentRepository()
        self.intensity_repository = intensity_repository or IntensityRepository()
        self._fixed_grid_definition = fixed_grid_definition

    @property
    def grid_definition(self) -> GridDefinition:
        if self._fixed_grid_definition is None:
            raise RuntimeError("grid definition has not been resolved")
        return self._fixed_grid_definition

    async def _load_context(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> tuple[AssessmentRun, EarthquakeRevision]:
        try:
            run_uuid = UUID(run_id)
        except ValueError as exc:
            raise ValueError("run_id must be a UUID") from exc
        run = await session.get(AssessmentRun, run_uuid)
        if run is None:
            raise LookupError("assessment run not found")
        revision = await session.get(EarthquakeRevision, run.revision_id)
        if revision is None:
            raise LookupError("earthquake revision not found")
        return run, revision

    async def _direction(
        self,
        session: AsyncSession,
        revision: EarthquakeRevision,
    ) -> DirectionDecision:
        inputs = await self.direction_provider.load(session, revision)
        return resolve_direction(
            override_deg=inputs.override_deg,
            focal_mechanism_deg=inputs.focal_mechanism_deg,
            finite_fault_deg=inputs.finite_fault_deg,
            candidates=inputs.candidates,
        )

    async def _ensure_grid(
        self,
        session: AsyncSession,
        run: AssessmentRun,
    ) -> GridDefinition:
        if self._fixed_grid_definition is not None:
            return self._fixed_grid_definition
        boundary_version = (
            run.snapshot.get("region_boundary_version")
            or self.region_profile.boundary_version
        )
        if not isinstance(boundary_version, str) or not boundary_version:
            raise ValueError("run region boundary version is required")
        return await self.intensity_repository.resolve_grid_definition(
            session,
            self.region_profile,
            boundary_version,
        )

    async def _record_task_failure(
        self,
        run_id: str,
        task_key: str,
        exc: Exception,
    ) -> None:
        try:
            run_uuid = UUID(run_id)
        except ValueError:
            return
        async with self.session_factory() as session:
            async with session.begin():
                await self.assessment_repository.record_task_failure_audit(
                    session,
                    run_uuid,
                    task_key,
                    type(exc).__name__,
                    _safe_error(exc),
                )

    async def run_model(self, run_id: str) -> IntensityTaskOutcome:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    run, revision = await self._load_context(session, run_id)
                    definition = await self._ensure_grid(session, run)
                    task = await self.assessment_repository.start_task(
                        session,
                        run.id,
                        "intensity.model",
                        self.MODEL_ALGORITHM_VERSION,
                        _fingerprint(self, run, revision, definition),
                    )
                    assert task.input_fingerprint is not None
                    samples = sample_grid(
                        definition,
                        epicenter_longitude=float(revision.longitude),
                        epicenter_latitude=float(revision.latitude),
                    )
                    direction = await self._direction(session, revision)
                    model = evaluate_model(
                        IntensityEventSnapshot(
                            event_id=str(run.event_id),
                            revision_id=str(run.revision_id),
                            magnitude=float(revision.magnitude),
                            longitude=float(revision.longitude),
                            latitude=float(revision.latitude),
                            report_ingested_at=run.report_ingested_at,
                        ),
                        samples,
                        direction,
                        self.parameters.model,
                    )
                    model_values = np.asarray(model.values, dtype=np.float64).reshape(
                        definition.height,
                        definition.width,
                    )
                    model_sigma = np.asarray(model.sigma, dtype=np.float64).reshape(
                        definition.height,
                        definition.width,
                    )
                    product_id = await self.intensity_repository.save_product(
                        session,
                        IntensityProductWrite(
                            run_id=run.id,
                            task_id=task.id,
                            product_type=ProductType.MODEL,
                            status=ProductStatus.AVAILABLE,
                            algorithm_version=self.MODEL_ALGORITHM_VERSION,
                            parameter_version=self.parameters.version,
                            strategy_version=None,
                            grid_definition=definition,
                            region_profile_version=self.region_profile.version,
                            input_fingerprint=task.input_fingerprint,
                            input_checksum=task.input_fingerprint,
                            quality_grade=None,
                            coverage_ratio=1.0,
                            statistics={
                                **_statistics(model_values),
                                "model_extrapolated": model.extrapolated,
                                "direction_status": model.direction.status.value,
                                "direction_source": model.direction.source,
                                "direction_strike_deg": model.direction.strike_deg,
                            },
                            source_product_id=None,
                            observed_at=revision.ingested_at,
                            bands=[("value", model_values), ("sigma", model_sigma)],
                        ),
                    )
                    product = await self.intensity_repository.get_product(
                        session,
                        run_id=run.id,
                        product_type=ProductType.MODEL,
                    )
                    output_checksum = (
                        product["output_checksum"] if product is not None else None
                    )
                    await self.assessment_repository.complete_task(
                        session,
                        task.id,
                        output_checksum,
                        {
                            "product_id": str(product_id),
                            "grid_definition_version": definition.version,
                        },
                    )
                    await self.assessment_repository.mark_deadline_exceeded(
                        session,
                        run.id,
                        datetime.now(UTC),
                    )
                    return IntensityTaskOutcome(
                        run_id=str(run.id),
                        task_key=task.task_key,
                        status="succeeded",
                        product_id=str(product_id),
                        output_checksum=output_checksum,
                    )
        except Exception as exc:
            try:
                await self._record_task_failure(run_id, "intensity.model", exc)
            except Exception:
                pass
            raise

    async def run_instrument(self, run_id: str) -> IntensityTaskOutcome:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    run, revision = await self._load_context(session, run_id)
                    definition = await self._ensure_grid(session, run)
                    task = await self.assessment_repository.start_task(
                        session,
                        run.id,
                        "intensity.instrument",
                        self.INSTRUMENT_ALGORITHM_VERSION,
                        _fingerprint(self, run, revision, definition),
                    )
                    assert task.input_fingerprint is not None
                    request = InstrumentRequest(
                        event_id=str(run.event_id),
                        revision_id=str(run.revision_id),
                        definition=definition,
                        observed_at=run.report_ingested_at,
                    )
                    try:
                        raw_product = await self.instrument_provider.fetch(request)
                    except Exception as exc:
                        raw_product = InstrumentProduct(
                            status=ProductStatus.UNAVAILABLE,
                            product_id=None,
                            product_version=None,
                            observed_at=None,
                            source="provider_error",
                            format=None,
                            source_verified=False,
                            grid_version=definition.version,
                            values=None,
                            sigma=None,
                            quality_codes=None,
                            coverage_ratio=0.0,
                            reason=f"provider_error:{type(exc).__name__}",
                        )
                    try:
                        product = validate_instrument_product(raw_product, definition)
                    except ValueError as exc:
                        product = InstrumentProduct(
                            status=ProductStatus.INVALID,
                            product_id=raw_product.product_id,
                            product_version=raw_product.product_version,
                            observed_at=raw_product.observed_at,
                            source=raw_product.source,
                            format=raw_product.format,
                            source_verified=raw_product.source_verified,
                            grid_version=raw_product.grid_version,
                            values=None,
                            sigma=None,
                            quality_codes=None,
                            coverage_ratio=0.0,
                            reason=str(exc),
                        )

                    bands = []
                    if product.status in {
                        ProductStatus.AVAILABLE,
                        ProductStatus.PARTIAL,
                    }:
                        assert product.values is not None
                        assert product.sigma is not None
                        assert product.quality_codes is not None
                        bands = [
                            (
                                "value",
                                np.asarray(product.values, dtype=np.float64).reshape(
                                    definition.height,
                                    definition.width,
                                ),
                            ),
                            (
                                "sigma",
                                np.asarray(product.sigma, dtype=np.float64).reshape(
                                    definition.height,
                                    definition.width,
                                ),
                            ),
                            (
                                "quality_code",
                                _quality_codes_to_numeric(product.quality_codes).reshape(
                                    definition.height,
                                    definition.width,
                                ),
                            ),
                        ]

                    product_id = await self.intensity_repository.save_product(
                        session,
                        IntensityProductWrite(
                            run_id=run.id,
                            task_id=task.id,
                            product_type=ProductType.INSTRUMENT,
                            status=product.status,
                            algorithm_version=self.INSTRUMENT_ALGORITHM_VERSION,
                            parameter_version=self.parameters.version,
                            strategy_version=None,
                            grid_definition=definition,
                            region_profile_version=self.region_profile.version,
                            input_fingerprint=task.input_fingerprint,
                            input_checksum=task.input_fingerprint,
                            quality_grade=None,
                            coverage_ratio=float(product.coverage_ratio),
                            statistics=_instrument_statistics(product, definition),
                            source_product_id=product.product_id,
                            observed_at=product.observed_at,
                            bands=bands,
                        ),
                    )
                    stored_product = await self.intensity_repository.get_product(
                        session,
                        run_id=run.id,
                        product_type=ProductType.INSTRUMENT,
                    )
                    output_checksum = (
                        stored_product["output_checksum"]
                        if stored_product is not None
                        else None
                    )
                    await self.assessment_repository.complete_task(
                        session,
                        task.id,
                        output_checksum,
                        {
                            "product_id": str(product_id),
                            "grid_definition_version": definition.version,
                        },
                    )
                    await self.assessment_repository.mark_deadline_exceeded(
                        session,
                        run.id,
                        datetime.now(UTC),
                    )
                    return IntensityTaskOutcome(
                        run_id=str(run.id),
                        task_key=task.task_key,
                        status="succeeded",
                        product_id=str(product_id),
                        output_checksum=output_checksum,
                    )
        except Exception as exc:
            try:
                await self._record_task_failure(run_id, "intensity.instrument", exc)
            except Exception:
                pass
            raise

    async def run_fusion(self, run_id: str) -> IntensityTaskOutcome:
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    run, revision = await self._load_context(session, run_id)
                    definition = await self._ensure_grid(session, run)
                    model_arrays = await self.intensity_repository.load_product_arrays(
                        session,
                        run_id=run.id,
                        product_type=ProductType.MODEL,
                    )
                    instrument_arrays = (
                        await self.intensity_repository.load_product_arrays(
                            session,
                            run_id=run.id,
                            product_type=ProductType.INSTRUMENT,
                        )
                    )
                    fusion_task = await self.assessment_repository.start_task(
                        session,
                        run.id,
                        "intensity.fusion",
                        self.FUSION_ALGORITHM_VERSION,
                        _fingerprint(
                            self,
                            run,
                            revision,
                            definition,
                            extra={
                                "instrument_product_id": (
                                    str(instrument_arrays.product_id)
                                    if instrument_arrays is not None
                                    else None
                                ),
                                "instrument_product_checksum": (
                                    instrument_arrays.statistics.get(
                                        "normalized_checksum"
                                    )
                                    if instrument_arrays is not None
                                    else None
                                ),
                                "instrument_status": (
                                    instrument_arrays.status
                                    if instrument_arrays is not None
                                    else None
                                ),
                            },
                        ),
                    )
                    assert fusion_task.input_fingerprint is not None
                    if model_arrays is None:
                        raise LookupError(
                            "model intensity product is required before fusion"
                        )
                    model = ModelField(
                        values=model_arrays.bands["value"],
                        sigma=model_arrays.bands["sigma"],
                        extrapolated=bool(
                            model_arrays.statistics["model_extrapolated"]
                        ),
                        direction=DirectionDecision(
                            DirectionStatus(
                                model_arrays.statistics["direction_status"]
                            ),
                            model_arrays.statistics.get("direction_source"),
                            model_arrays.statistics.get("direction_strike_deg"),
                        ),
                    )
                    instrument = _instrument_from_arrays(instrument_arrays)
                    fusion = fuse(model, instrument, self.parameters.fusion)
                    if instrument is None:
                        instrument_values_or_nan = np.full(
                            fusion.values.shape,
                            np.nan,
                            dtype=np.float64,
                        )
                    else:
                        instrument_values_or_nan = np.asarray(
                            instrument.values,
                            dtype=np.float64,
                        )
                    bands = [
                        ("value", fusion.values),
                        ("sigma", fusion.sigma),
                        ("p10", fusion.p10),
                        ("p90", fusion.p90),
                        ("model_weight", fusion.model_weight),
                        ("instrument_weight", fusion.instrument_weight),
                        (
                            "quality_code",
                            _quality_codes_to_numeric(fusion.quality_codes),
                        ),
                        ("model_value", model.values),
                        ("instrument_value", instrument_values_or_nan),
                    ]
                    product_id = await self.intensity_repository.save_product(
                        session,
                        IntensityProductWrite(
                            run_id=run.id,
                            task_id=fusion_task.id,
                            product_type=ProductType.FUSION,
                            status=ProductStatus.AVAILABLE,
                            algorithm_version=self.FUSION_ALGORITHM_VERSION,
                            parameter_version=self.parameters.version,
                            strategy_version=self.parameters.version,
                            grid_definition=definition,
                            region_profile_version=self.region_profile.version,
                            input_fingerprint=fusion_task.input_fingerprint,
                            input_checksum=fusion_task.input_fingerprint,
                            quality_grade=fusion.quality.value,
                            coverage_ratio=fusion.coverage_ratio,
                            statistics={
                                **_statistics(fusion.values),
                                "fusion_mode": fusion.mode.value,
                                "instrument_status": (
                                    instrument_arrays.status
                                    if instrument_arrays is not None
                                    else "unavailable"
                                ),
                                "model_extrapolated": model.extrapolated,
                            },
                            source_product_id=None,
                            observed_at=revision.ingested_at,
                            bands=bands,
                        ),
                    )
                    stored_product = await self.intensity_repository.get_product(
                        session,
                        run_id=run.id,
                        product_type=ProductType.FUSION,
                    )
                    output_checksum = (
                        stored_product["output_checksum"]
                        if stored_product is not None
                        else None
                    )
                    await self.assessment_repository.complete_task(
                        session,
                        fusion_task.id,
                        output_checksum,
                        {
                            "product_id": str(product_id),
                            "grid_definition_version": definition.version,
                        },
                    )
                    await self.assessment_repository.mark_deadline_exceeded(
                        session,
                        run.id,
                        datetime.now(UTC),
                    )
                    return IntensityTaskOutcome(
                        run_id=str(run.id),
                        task_key=fusion_task.task_key,
                        status="succeeded",
                        product_id=str(product_id),
                        output_checksum=output_checksum,
                    )
        except Exception as exc:
            try:
                await self._record_task_failure(run_id, "intensity.fusion", exc)
            except Exception:
                pass
            raise

    async def record_task_failure(
        self,
        run_id: str,
        task_key: str,
        exc: Exception,
    ) -> None:
        try:
            try:
                run_uuid = UUID(run_id)
            except ValueError:
                pass
            else:
                async with self.session_factory() as session:
                    async with session.begin():
                        await self.assessment_repository.record_task_failure_audit(
                            session,
                            run_uuid,
                            task_key,
                            type(exc).__name__,
                            _safe_error(exc),
                        )
        finally:
            raise exc

    async def record_run_failure(
        self,
        run_id: str,
        exc: Exception,
    ) -> None:
        try:
            try:
                run_uuid = UUID(run_id)
            except ValueError:
                pass
            else:
                async with self.session_factory() as session:
                    async with session.begin():
                        await self.assessment_repository.record_run_failure_audit(
                            session,
                            run_uuid,
                            _safe_error(exc),
                        )
        finally:
            raise exc


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _statistics(values) -> dict:
    array = np.asarray(values, dtype=np.float64)
    return {
        "minimum": float(np.min(array)),
        "maximum": float(np.max(array)),
        "mean": float(np.mean(array)),
    }


def _instrument_from_arrays(arrays):
    if arrays is None:
        return None
    required = {"value", "sigma", "quality_code"}
    if arrays.status == ProductStatus.UNAVAILABLE.value or not required <= set(
        arrays.bands
    ):
        return None
    values = np.asarray(arrays.bands["value"], dtype=np.float64)
    sigma = np.asarray(arrays.bands["sigma"], dtype=np.float64)
    codes = _quality_codes_from_numeric(arrays.bands["quality_code"])
    return InstrumentProduct(
        status=ProductStatus(arrays.status),
        product_id=str(arrays.product_id),
        product_version=arrays.statistics.get("product_version"),
        observed_at=_parse_optional_datetime(arrays.statistics.get("observed_at")),
        source=arrays.statistics.get("source", "instrument"),
        format=None,
        source_verified=False,
        grid_version=arrays.statistics.get("grid_definition_version"),
        values=values,
        sigma=sigma,
        quality_codes=codes,
        coverage_ratio=float(arrays.statistics.get("coverage_ratio", 0.0)),
        reason=arrays.statistics.get("reason"),
        generated_at=_parse_optional_datetime(
            arrays.statistics.get("generated_at")
        ),
        crs=arrays.statistics.get("crs"),
        resolution_m=arrays.statistics.get("resolution_m"),
        raw_checksum=arrays.statistics.get("raw_checksum"),
        normalized_checksum=arrays.statistics.get("normalized_checksum"),
    )


def _parse_optional_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


_QUALITY_CODE_VALUES = {
    "Q0": 0.0,
    "Q1": 1.0,
    "Q2": 2.0,
    "Q3": 3.0,
}
_QUALITY_CODE_NAMES = {value: name for name, value in _QUALITY_CODE_VALUES.items()}


def _quality_codes_to_numeric(values) -> np.ndarray:
    array = np.asarray(values)
    flat = array.reshape(-1)
    numeric = np.asarray(
        [_QUALITY_CODE_VALUES[str(code)] for code in flat],
        dtype=np.float64,
    )
    return numeric.reshape(array.shape)


def _quality_codes_from_numeric(values) -> np.ndarray:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    return np.asarray(
        [_QUALITY_CODE_NAMES[int(code)] for code in flat],
        dtype=object,
    )


def _instrument_statistics(
    product: InstrumentProduct,
    definition: GridDefinition,
) -> dict:
    statistics = {
        "source": product.source,
        "product_version": product.product_version,
        "observed_at": _isoformat_optional(product.observed_at),
        "generated_at": _isoformat_optional(product.generated_at),
        "grid_definition_version": definition.version,
        "coverage_ratio": float(product.coverage_ratio),
        "raw_checksum": product.raw_checksum,
        "normalized_checksum": product.normalized_checksum,
        "crs": product.crs,
        "resolution_m": product.resolution_m,
    }
    if product.reason is not None:
        statistics["reason"] = product.reason
    return statistics


def _isoformat_optional(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _fingerprint(
    self,
    run,
    revision,
    definition: GridDefinition,
    *,
    extra=None,
) -> str:
    payload = {
        "run_id": str(run.id),
        "revision_id": str(revision.id),
        "magnitude": str(revision.magnitude),
        "longitude": str(revision.longitude),
        "latitude": str(revision.latitude),
        "grid_definition_version": definition.version,
        "parameter_version": self.parameters.version,
        "region_profile_version": self.region_profile.version,
        "region_profile_checksum": self.region_profile.checksum,
        "parameter_checksum": self.parameters.checksum,
        "extra": extra or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
