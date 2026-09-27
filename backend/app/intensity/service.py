from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun
from app.assessment.repository import AssessmentRepository
from app.config import settings
from app.events.models import EarthquakeRevision
from app.intensity.domain import DirectionDecision, GridDefinition
from app.intensity.instrument import (
    InstrumentIntensityProvider,
    UnavailableInstrumentProvider,
)
from app.intensity.model import FaultCandidate, resolve_direction
from app.intensity.parameters import ParameterBundle
from app.intensity.region import RegionProfile, load_region_profile
from app.intensity.repository import IntensityRepository


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
