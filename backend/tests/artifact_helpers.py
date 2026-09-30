import asyncio
import hashlib
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select

from app.artifacts.catalog import load_catalog
from app.artifacts.domain import ArtifactCatalog
from app.artifacts.models import (
    ArtifactTaskDependencyBinding,
    GeneratedArtifact,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import (
    ArtifactGenerationResult,
    ArtifactProductionRepository,
    ArtifactQuality,
    CreateProductionRunCommand,
)
from app.assessment.models import AssessmentRun, AssessmentTask
from app.config import settings
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.intensity.models import IntensityFieldProduct


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MapEventFixture:
    id: uuid.UUID
    place: str
    magnitude: Decimal
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    depth_km: Decimal


@dataclass(frozen=True, slots=True)
class MapRevisionFixture:
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class MapRenderFixtureContext:
    artifact_key: str
    display_name: str
    output_profile: str
    context_fingerprint: str
    selected_basemap: object
    event: MapEventFixture
    revision: MapRevisionFixture
    layers: tuple[object, ...]
    legend: tuple[object, ...]
    source_notes: tuple[str, ...]
    quality: object
    marker: str | None
    base_style: str


@dataclass(frozen=True, slots=True)
class SeededArtifactAssessment:
    event_id: uuid.UUID
    revision_id: uuid.UUID
    assessment_run_id: uuid.UUID
    deadline_basis_at: datetime
    deadline_at: datetime
    catalog: ArtifactCatalog

    def full_run_command(self, **overrides) -> CreateProductionRunCommand:
        values = {
            "assessment_run_id": self.assessment_run_id,
            "event_id": self.event_id,
            "revision_id": self.revision_id,
            "revision_no": 1,
            "production_mode": "live",
            "launch_mode": "assessment_child",
            "deadline_basis_at": self.deadline_basis_at,
            "deadline_at": self.deadline_at,
            "deadline_kind": "event_deadline",
            "catalog_version": self.catalog.catalog_version,
            "generation_scope": "full",
            "required_outputs": self.catalog.full_required_outputs(),
            "snapshot": {
                "event_id": str(self.event_id),
                "revision_id": str(self.revision_id),
            },
        }
        values.update(overrides)
        return CreateProductionRunCommand(**values)

    def rebuild_command(
        self,
        artifact_key: str,
        *,
        output_profile: str = "a3v-professional",
        **overrides,
    ) -> CreateProductionRunCommand:
        now = datetime.now(UTC)
        values = {
            "assessment_run_id": self.assessment_run_id,
            "event_id": self.event_id,
            "revision_id": self.revision_id,
            "revision_no": 1,
            "production_mode": "live",
            "launch_mode": "standalone",
            "deadline_basis_at": now,
            "deadline_at": now + timedelta(seconds=300),
            "deadline_kind": "rebuild_deadline",
            "catalog_version": self.catalog.catalog_version,
            "generation_scope": f"artifact:{artifact_key}:{output_profile}",
            "required_outputs": ((artifact_key, output_profile),),
            "snapshot": {"requested_by": "artifact-test"},
        }
        values.update(overrides)
        return CreateProductionRunCommand(**values)


class ArtifactAssessmentFixture:
    def __init__(self, session_factory, assessment: SeededArtifactAssessment) -> None:
        self._session_factory = session_factory
        self.assessment = assessment
        self.catalog = assessment.catalog

    @property
    def event_id(self) -> uuid.UUID:
        return self.assessment.event_id

    @property
    def revision_id(self) -> uuid.UUID:
        return self.assessment.revision_id

    @property
    def assessment_run_id(self) -> uuid.UUID:
        return self.assessment.assessment_run_id

    @property
    def deadline_basis_at(self) -> datetime:
        return self.assessment.deadline_basis_at

    @property
    def deadline_at(self) -> datetime:
        return self.assessment.deadline_at

    def full_run_command(self, **overrides) -> CreateProductionRunCommand:
        return self.assessment.full_run_command(**overrides)

    def rebuild_command(
        self,
        artifact_key: str,
        *,
        output_profile: str = "a3v-professional",
        **overrides,
    ) -> CreateProductionRunCommand:
        return self.assessment.rebuild_command(
            artifact_key,
            output_profile=output_profile,
            **overrides,
        )

    async def map_context(
        self,
        artifact_key: str,
        *,
        base_style: str | None = None,
    ) -> MapRenderFixtureContext:
        from app.artifacts.basemap import SelectedBasemap
        from app.artifacts.renderers.base import RenderQuality
        from app.artifacts.renderers.map_renderer import MapLayer

        definition = self.catalog.get(artifact_key, "a3v-professional")
        selected_basemap = SelectedBasemap(
            provider="gaode",
            package_id="gaode-offline-v1",
            version="v1",
            checksum="a" * 64,
            selection_reason="gaode validated",
        )
        fixture_source = Path(
            "/app/tests/fixtures/artifact_maps/epicenter.geojson"
        )
        fixture_target = (
            Path(settings.artifact_storage_root)
            / "artifact_maps"
            / "epicenter.geojson"
        )
        fixture_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture_source, fixture_target)
        layer = MapLayer(
            id="epicenter",
            url="local://artifact_maps/epicenter.geojson",
            source={
                "type": "geojson",
                "data": "local://artifact_maps/epicenter.geojson",
            },
            style={
                "type": "circle",
                "paint": {
                    "circle-radius": 10,
                    "circle-color": "#b3261e",
                    "circle-stroke-color": "#ffffff",
                    "circle-stroke-width": 2,
                },
            },
        )
        return MapRenderFixtureContext(
            artifact_key=artifact_key,
            display_name=definition.display_name,
            output_profile=definition.output_profile,
            context_fingerprint=_checksum(
                f"map-context-{artifact_key}-{self.event_id}"
            ),
            selected_basemap=selected_basemap,
            event=MapEventFixture(
                id=self.event_id,
                place="artifact fixture event",
                magnitude=Decimal("5.2"),
                origin_time=self.deadline_basis_at - timedelta(minutes=2),
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
            ),
            revision=MapRevisionFixture(id=self.revision_id),
            layers=(layer,),
            legend=(),
            source_notes=("offline gaode basemap",),
            quality=RenderQuality(grade=definition.quality_policy),
            marker=None,
            base_style=base_style or selected_basemap.provider_style_key,
        )

    async def create_full_run(
        self,
        *,
        revision_no: int = 1,
        production_mode: str = "live",
    ) -> ProductionRun:
        if revision_no != 1:
            raise ValueError("the base fixture revision number is 1")
        async with self._session_factory() as session:
            async with session.begin():
                return await ArtifactProductionRepository().create_run(
                    session,
                    self.full_run_command(production_mode=production_mode),
                )

    async def create_correction_revision(
        self,
        *,
        revision_no: int,
    ) -> EarthquakeRevision:
        now = self.assessment.deadline_basis_at + timedelta(minutes=revision_no)
        async with self._session_factory() as session:
            async with session.begin():
                event = await session.get(
                    EarthquakeEvent,
                    self.assessment.event_id,
                    with_for_update=True,
                )
                assert event is not None
                current_revision = await session.scalar(
                    select(EarthquakeRevision).where(
                        EarthquakeRevision.event_id == event.id,
                        EarthquakeRevision.is_current.is_(True),
                    )
                )
                if current_revision is not None:
                    current_revision.is_current = False
                    await session.flush()
                raw = RawMessage(
                    source="artifact-fixture",
                    source_message_id=f"correction-{uuid.uuid4()}",
                    message_kind="test",
                    checksum=_checksum(f"correction-{uuid.uuid4()}"),
                    payload={"fixture": "artifact-correction"},
                    received_at=now,
                )
                session.add(raw)
                await session.flush()
                revision = EarthquakeRevision(
                    event_id=event.id,
                    raw_message_id=raw.id,
                    revision_no=revision_no,
                    revision_kind="correction",
                    source_event_id=f"artifact-correction-{uuid.uuid4()}",
                    origin_time=now,
                    longitude=Decimal("121.500000"),
                    latitude=Decimal("31.200000"),
                    depth_km=Decimal("10.00"),
                    magnitude=Decimal("5.3"),
                    place="artifact fixture correction",
                    ingested_at=now,
                    is_current=True,
                )
                session.add(revision)
                await session.flush()
                event.current_revision_id = revision.id
                return revision

    async def first_task(
        self,
        run_id: uuid.UUID,
        artifact_key: str | None = None,
    ) -> ProductionTask:
        async with self._session_factory() as session:
            query = select(ProductionTask).where(
                ProductionTask.production_run_id == run_id
            )
            if artifact_key is not None:
                query = query.where(ProductionTask.artifact_key == artifact_key)
            task = await session.scalar(query.order_by(ProductionTask.sequence))
            assert task is not None
            return task

    async def create_fusion_product(
        self,
        *,
        version: str = "fusion-v1",
        checksum: str = "a" * 64,
    ) -> IntensityFieldProduct:
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(
                    AssessmentRun,
                    self.assessment.assessment_run_id,
                )
                assert run is not None
                assessment_task = await session.scalar(
                    select(AssessmentTask).where(
                        AssessmentTask.run_id == run.id,
                        AssessmentTask.task_key == "intensity.fusion",
                    )
                )
                if assessment_task is None:
                    assessment_task = AssessmentTask(
                        run_id=run.id,
                        task_key="intensity.fusion",
                        task_type="intensity",
                        component="fixture",
                        sequence=1,
                        deadline_at=run.deadline_at,
                        status="succeeded",
                    )
                    session.add(assessment_task)
                    await session.flush()
                product = IntensityFieldProduct(
                    run_id=run.id,
                    task_id=assessment_task.id,
                    product_type="fusion",
                    status="complete",
                    algorithm_version=version,
                    parameter_version="parameters-v1",
                    strategy_version="strategy-v1",
                    grid_definition_version="grid-v1",
                    region_profile_version="region-v1",
                    input_fingerprint="b" * 64,
                    input_checksum="c" * 64,
                    output_checksum=checksum,
                    quality_grade="A",
                    coverage_ratio=1,
                    statistics={},
                )
                session.add(product)
                await session.flush()
                return product

    async def published_artifact(
        self,
        artifact_key: str,
        *,
        version: int,
    ) -> GeneratedArtifact:
        artifact = await self.generated_artifact(artifact_key, version=version)
        async with self._session_factory() as session:
            async with session.begin():
                await ArtifactProductionRepository().publish_artifact(
                    session,
                    artifact.id,
                    published_by="fixture",
                    forced=False,
                )
        return artifact

    async def generated_artifact(
        self,
        artifact_key: str,
        *,
        version: int,
    ) -> GeneratedArtifact:
        repository = ArtifactProductionRepository()
        async with self._session_factory() as session:
            async with session.begin():
                command = (
                    self.full_run_command()
                    if version == 1
                    else self.rebuild_command(artifact_key)
                )
                run = await repository.create_run(
                    session,
                    command,
                )
                task = await session.scalar(
                    select(ProductionTask).where(
                        ProductionTask.production_run_id == run.id,
                        ProductionTask.artifact_key == artifact_key,
                    )
                )
                assert task is not None
                await repository.freeze_task_fingerprint(
                    session,
                    task.id,
                    _checksum(f"{artifact_key}-{uuid.uuid4()}"),
                )
                await repository.start_task(
                    session,
                    task.id,
                    f"fixture:{run.id}:{artifact_key}:{uuid.uuid4()}",
                )
                artifact = await repository.complete_task(
                    session,
                    task.id,
                    ArtifactGenerationResult(
                        file_name=f"{artifact_key}-v{version}.jpg",
                        format="jpg",
                        storage_path=f"fixtures/{artifact_key}/v{version}.jpg",
                        checksum=_checksum(f"{artifact_key}-{version}"),
                        size_bytes=1024,
                        quality=ArtifactQuality(grade="A", needs_review=False),
                        width=4761,
                        height=3369,
                        generated_at=datetime.now(UTC),
                    ),
                    "succeeded",
                )
                if artifact.artifact_version != version:
                    raise AssertionError(
                        f"fixture expected artifact version {version}, "
                        f"got {artifact.artifact_version}"
                    )
                return artifact

    async def bindings_for(
        self,
        task_id: uuid.UUID,
    ) -> tuple[ArtifactTaskDependencyBinding, ...]:
        async with self._session_factory() as session:
            bindings = (
                await session.scalars(
                    select(ArtifactTaskDependencyBinding)
                    .where(
                        ArtifactTaskDependencyBinding.production_task_id == task_id
                    )
                    .order_by(ArtifactTaskDependencyBinding.dependency_key)
                )
            ).all()
            return tuple(bindings)


async def _cleanup_fixture_data(session_factory, event_id: uuid.UUID) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with session_factory() as session:
                async with session.begin():
                    raw_ids = (
                        await session.scalars(
                            select(EarthquakeRevision.raw_message_id).where(
                                EarthquakeRevision.event_id == event_id
                            )
                        )
                    ).all()
                    await session.execute(
                        delete(EarthquakeEvent).where(EarthquakeEvent.id == event_id)
                    )
                    if raw_ids:
                        await session.execute(
                            delete(RawMessage).where(RawMessage.id.in_(raw_ids))
                        )
            return
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.05 * (2**attempt))
    assert last_error is not None
    raise last_error


@pytest.fixture
async def seeded_artifact_assessment(session_factory):
    now = datetime.now(UTC)
    event_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    assessment_run_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    raw_id = uuid.uuid4()

    async with session_factory() as session:
        async with session.begin():
            raw = RawMessage(
                id=raw_id,
                source="artifact-fixture",
                source_message_id=f"formal-{event_id}",
                message_kind="test",
                checksum=_checksum(f"formal-{event_id}"),
                payload={"fixture": "artifact-assessment"},
                received_at=now,
            )
            event = EarthquakeEvent(
                id=event_id,
                source="artifact-fixture",
                canonical_source_id=f"artifact-{event_id}",
                event_type="formal",
                origin_time=now - timedelta(minutes=2),
                longitude=Decimal("121.500000"),
                latitude=Decimal("31.200000"),
                depth_km=Decimal("10.00"),
                magnitude=Decimal("5.2"),
                place="artifact fixture event",
                geom=WKTElement("POINT(121.5 31.2)", srid=4326),
                t1_at=now,
                lifecycle_state="active",
            )
            session.add_all([raw, event])
            await session.flush()
            revision = EarthquakeRevision(
                id=revision_id,
                event_id=event.id,
                raw_message_id=raw.id,
                revision_no=1,
                revision_kind="formal",
                source_event_id=f"formal-{event_id}",
                origin_time=event.origin_time,
                longitude=event.longitude,
                latitude=event.latitude,
                depth_km=event.depth_km,
                magnitude=event.magnitude,
                place=event.place,
                ingested_at=now,
                is_current=True,
            )
            session.add(revision)
            await session.flush()
            event.current_revision_id = revision.id
            outbox = EventLifecycleOutbox(
                id=outbox_id,
                event_id=event.id,
                revision_id=revision.id,
                trigger_type="assessment.requested",
                trigger_reason="live",
                payload={"event_id": str(event.id), "revision_id": str(revision.id)},
                status="published",
                created_at=now,
                available_at=now,
                published_at=now,
            )
            session.add(outbox)
            await session.flush()
            assessment_run = AssessmentRun(
                id=assessment_run_id,
                event_id=event.id,
                revision_id=revision.id,
                outbox_id=outbox.id,
                run_no=1,
                trigger_reason="live",
                status="running",
                priority=100,
                t1_at=event.t1_at,
                deadline_at=now + timedelta(seconds=300),
                report_ingested_at=now,
                deadline_basis_at=now,
                snapshot={},
            )
            session.add(assessment_run)
            event.latest_assessment_run_id = assessment_run.id
            event.effective_assessment_run_id = assessment_run.id

    assessment = SeededArtifactAssessment(
        event_id=event_id,
        revision_id=revision_id,
        assessment_run_id=assessment_run_id,
        deadline_basis_at=now,
        deadline_at=now + timedelta(seconds=300),
        catalog=load_catalog(settings.artifact_catalog_path),
    )
    fixture = ArtifactAssessmentFixture(session_factory, assessment)
    yield fixture
    await _cleanup_fixture_data(session_factory, event_id)


@pytest.fixture
def artifact_repository() -> ArtifactProductionRepository:
    return ArtifactProductionRepository()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as session:
        async with session.begin():
            yield session
