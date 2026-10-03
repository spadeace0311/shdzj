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
from PIL import Image, ImageDraw
from sqlalchemy import delete, select, update

from app.artifacts.catalog import load_catalog
from app.artifacts.context import DocumentRenderContext, FrozenAssetVersion
from app.artifacts.domain import ArtifactCatalog, DependencyKind
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
from app.artifacts.service import ArtifactProductionService
from app.artifacts.workflow import ArtifactProductionWorkflowInput
from app.artifacts.models import ArtifactTemplate, ArtifactTemplateVersion
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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
class FixtureReference:
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
    production_mode: str
    resolved_sources: object


def _fixture_resolved_sources(artifact_key: str) -> dict:
    from app.artifacts.renderers.map_layers import MapLayerRegistry

    sources = {}
    for definition in MapLayerRegistry.definitions(artifact_key):
        if definition.source_key == "event":
            feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [121.5, 31.2]},
                "properties": {
                    "magnitude": 5.2,
                    "depth_km": 10.0,
                    "value_status": "available",
                },
            }
            style = {
                "type": "circle",
                "paint": {
                    "circle-radius": 10,
                    "circle-color": "#b3261e",
                    "circle-stroke-color": "#ffffff",
                    "circle-stroke-width": 2,
                },
            }
        elif definition.geometry_type in {"line", "multiline"}:
            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[121.42, 31.16], [121.5, 31.2], [121.58, 31.24]],
                },
                "properties": {"value_status": "available"},
            }
            style = {
                "type": "line",
                "paint": {"line-color": "#b3261e", "line-width": 3},
            }
        else:
            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [121.38, 31.1],
                            [121.62, 31.1],
                            [121.62, 31.3],
                            [121.38, 31.3],
                            [121.38, 31.1],
                        ]
                    ],
                },
                "properties": {"value_status": "available"},
            }
            style = {
                "type": "fill",
                "paint": {"fill-color": "#c77b3b", "fill-opacity": 0.42},
            }
        sources[definition.source_key] = {
            "source_key": definition.source_key,
            "kind": "vector",
            "status": "bound",
            "url": f"local://inline/{definition.source_key}",
            "source": {
                "type": "geojson",
                "data": {"type": "FeatureCollection", "features": [feature]},
            },
            "style": style,
            "checksum": None,
            "feature_count": 1,
            "metadata": {"verified_empty": False},
        }
    return sources


def _write_document_fixture_image(path: Path, marker: str | None) -> None:
    image = Image.new("RGB", (800, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 770, 570), outline=(20, 30, 40), width=4)
    draw.line((30, 300, 770, 300), fill=(150, 30, 30), width=3)
    draw.ellipse((330, 230, 470, 370), fill=(180, 50, 40))
    draw.text((45, 45), marker or "震中位置", fill=(20, 30, 40))
    image.save(path, format="JPEG", dpi=(96, 96), quality=92)


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
    def event(self) -> FixtureReference:
        return FixtureReference(id=self.assessment.event_id)

    @property
    def revision_id(self) -> uuid.UUID:
        return self.assessment.revision_id

    @property
    def revision(self) -> FixtureReference:
        return FixtureReference(id=self.assessment.revision_id)

    @property
    def assessment_run_id(self) -> uuid.UUID:
        return self.assessment.assessment_run_id

    @property
    def deadline_basis_at(self) -> datetime:
        return self.assessment.deadline_basis_at

    @property
    def deadline_at(self) -> datetime:
        return self.assessment.deadline_at

    @property
    def now(self) -> datetime:
        return self.assessment.deadline_basis_at

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
        resolved_sources = _fixture_resolved_sources(artifact_key)
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
            production_mode="live",
            resolved_sources=resolved_sources,
        )

    async def document_context(
        self,
        artifact_key: str,
        *,
        production_mode: str = "live",
        t1_at: datetime | None = None,
        failed_optional_artifacts: set[str] | None = None,
        missing_artifact_keys: set[str] | None = None,
        failed_products: set[str] | None = None,
    ) -> DocumentRenderContext:
        definition = self.catalog.get(artifact_key, "a3v-professional")
        marker = {
            "live": None,
            "manual": None,
            "test": "【测试】",
            "drill": "【演练】",
            "replay": "【测试回放】",
        }[production_mode]
        failed_optional_artifacts = set(failed_optional_artifacts or ())
        missing_artifact_keys = set(missing_artifact_keys or ())
        failed_products = set(failed_products or ())
        background_checksum = _sha256_file(
            Path(settings.artifact_template_root) / "background-template.docx"
        )
        decision_checksum = _sha256_file(
            Path(settings.artifact_template_root) / "decision-template.pptx"
        )
        template_versions = {
            "background-template": {
                "template_key": "background-template",
                "kind": "docx",
                "display_name": "背景文档模板",
                "version": "v1",
                "checksum": background_checksum,
                "published_at": self.deadline_basis_at.isoformat(),
            },
            "decision-template": {
                "template_key": "decision-template",
                "kind": "pptx",
                "display_name": "辅助决策演示模板",
                "version": "v1",
                "checksum": decision_checksum,
                "published_at": self.deadline_basis_at.isoformat(),
            },
        }

        required_asset_keys = set(definition.required_assets)
        optional_asset_keys = set(definition.optional_assets)
        artifact_dependency_keys = {
            dependency.key
            for dependency in definition.depends_on
            if dependency.kind is DependencyKind.ARTIFACT
        }
        artifact_optional_keys = {
            dependency.key
            for dependency in definition.optional_depends_on
            if dependency.kind is DependencyKind.ARTIFACT
        }
        if artifact_key == "deck.decision_report":
            artifact_dependency_keys.update(
                {
                    "map.intensity",
                    "map.economic_loss",
                    "map.rescue_demand",
                    "map.deaths",
                    "map.injuries",
                    "map.buried",
                    "map.material_demand",
                    "map.active_faults",
                    "map.key_targets",
                    "map.building_damage",
                    "map.epicenter",
                    "map.city_distances",
                }
            )

        artifact_paths: dict[str, Path] = {}
        asset_versions: dict[str, FrozenAssetVersion] = {}
        for asset_key in sorted(required_asset_keys | optional_asset_keys):
            asset_versions[asset_key] = FrozenAssetVersion(
                asset_key=asset_key,
                role="required" if asset_key in required_asset_keys else "optional",
                resolution_status="bound",
                asset_version_id=uuid.uuid4(),
                checksum=_checksum(f"{asset_key}-v1"),
                coverage={"status": "complete"},
                version="v1",
            )
        map_root = Path(settings.artifact_storage_root) / "document-fixtures"
        map_root.mkdir(parents=True, exist_ok=True)
        fixture_artifact_keys = (
            artifact_dependency_keys | artifact_optional_keys
        )
        for dependency_key in sorted(fixture_artifact_keys):
            if dependency_key in missing_artifact_keys:
                continue
            if (
                dependency_key in artifact_optional_keys
                and dependency_key in failed_optional_artifacts
            ):
                continue
            if dependency_key.startswith("map."):
                map_path = map_root / f"{dependency_key}.jpg"
                _write_document_fixture_image(map_path, marker)
                artifact_paths[dependency_key] = map_path
                map_checksum = _sha256_file(map_path)
                asset_versions[dependency_key] = FrozenAssetVersion(
                    asset_key=dependency_key,
                    role="artifact",
                    resolution_status="bound",
                    asset_version_id=uuid.uuid4(),
                    checksum=map_checksum,
                    coverage={"status": "complete"},
                    version="v1",
                )
            else:
                doc_path = map_root / f"{dependency_key}.docx"
                if not doc_path.exists():
                    doc_path.write_bytes(
                        b"PK\x03\x04fixture-document-dependency"
                    )
                artifact_paths[dependency_key] = doc_path
                asset_versions[dependency_key] = FrozenAssetVersion(
                    asset_key=dependency_key,
                    role="artifact",
                    resolution_status="bound",
                    asset_version_id=uuid.uuid4(),
                    checksum=_sha256_file(doc_path),
                    coverage={"status": "complete"},
                    version="v1",
                )

        event = {
            "event_id": str(self.event_id),
            "revision_id": str(self.revision_id),
            "revision_no": 1,
            "event_kind": "formal",
            "place": "artifact fixture event",
            "magnitude": 5.2,
            "origin_time": (self.deadline_basis_at - timedelta(minutes=2)).isoformat(),
            "longitude": 121.5,
            "latitude": 31.2,
            "depth_km": 10.0,
            "t1_at": t1_at.isoformat() if t1_at is not None else None,
        }
        assessment_products = {
            "intensity.fusion": {
                "version": "fusion-v1",
                "checksum": _checksum("fusion-v1"),
                "summary": "烈度 V",
            },
            "loss.buildings": {
                "version": "buildings-v1",
                "checksum": _checksum("buildings-v1"),
                "total": 10000,
                "slight": 1200,
                "moderate": 300,
                "severe": 80,
            },
            "loss.population": {
                "version": "population-v1",
                "checksum": _checksum("population-v1"),
                "resident": 120000,
                "floating": 18000,
                "households": 52000,
                "age_structure": "0-14岁 18.2%；15-64岁 71.6%；65岁及以上 10.2%",
                "affected": 4300,
            },
            "loss.economic": {
                "version": "economic-v1",
                "checksum": _checksum("economic-v1"),
                "gdp": 560000,
                "primary": 12000,
                "secondary": 220000,
                "tertiary": 328000,
                "loss": 8800,
            },
            "loss.casualties": {
                "version": "casualties-v1",
                "checksum": _checksum("casualties-v1"),
                "deaths": 18,
                "injuries": 42,
                "summary": "死亡 18 人，受伤 42 人",
            },
            "loss.resources": {
                "version": "resources-v1",
                "checksum": _checksum("resources-v1"),
                "summary": "救援力量需求 5 支",
            },
            "loss.validate": {
                "version": "validate-v1",
                "checksum": _checksum("validate-v1"),
                "grade": "通过",
                "summary": "评估结果通过校验",
            },
        }
        for product_key in failed_products:
            assessment_products.pop(product_key, None)

        manifest = {
            "event": event,
            "t1_at": event["t1_at"],
            "assessment": {
                "assessment_run_id": str(self.assessment_run_id),
                "data_asset_snapshot_fingerprint": _checksum("data-snapshot-v1"),
                "products": assessment_products,
            },
            "historical_earthquakes": {
                "radius_km": 50,
                "magnitude_threshold": 3.0,
                "summary": "半径内历史地震 12 条，最大震级 4.9",
                "disaster_summary": "灾害地震 3 条，需复核",
                "statistics": "12 条 / 3 条灾害",
            },
            "spatial_distances": {
                "city_distance": 8.6,
                "county_distance": 12.4,
                "town_distance": 5.2,
                "major_city_distance": 18.9,
                "key_target_distance": 6.3,
                "fault_distance": 21.7,
            },
            "targets": {
                "shelter": "避难场所 45 处",
                "school": "学校 118 所",
                "hospital": "医院 36 所",
                "hazard_source": "危险源 22 处",
                "rescue_team": "救援队 9 支",
                "cultural_relic": "文物 17 处",
                "key_target": "重点目标 64 处",
            },
            "area_overview": {
                "geography": "长江三角洲冲积平原，水网密集",
                "administration": "上海市及邻近行政区",
                "key_risks": "人口密集区、重点目标、危险源与断裂带",
            },
            "building_town": {
                "town_totals": 10000,
                "structure_type": "砖混 60%；框架 40%",
                "coverage_quality": "完整覆盖",
            },
            "faults": {
                "summary": "邻近断裂 3 条",
            },
            "population_town": {
                "resident": 120000,
                "floating": 18000,
                "household": 52000,
                "age_structure": "0-14岁 18.2%；15-64岁 71.6%；65岁及以上 10.2%",
            },
            "economy_county": {
                "gdp": 560000,
                "primary": 12000,
                "secondary": 220000,
                "tertiary": 328000,
            },
            "loss": {
                "parameter_package": {
                    "version": "parameters-v1",
                    "checksum": _checksum("parameters-v1"),
                }
            },
            "templates": [
                template_versions["background-template"],
                template_versions["decision-template"],
            ],
            "assets": [
                {
                    "asset_key": key,
                    "role": item.role,
                    "resolution_status": item.resolution_status,
                    "checksum": item.checksum,
                    "version": item.version,
                    "coverage": item.coverage,
                }
                for key, item in sorted(asset_versions.items())
            ],
        }
        context_fingerprint = _checksum(
            f"document-context-{artifact_key}-{self.event_id}"
        )
        return DocumentRenderContext(
            production_task_id=uuid.uuid4(),
            production_run_id=uuid.uuid4(),
            artifact_key=artifact_key,
            output_profile=definition.output_profile,
            document_type=definition.kind.value,
            context_fingerprint=context_fingerprint,
            template_versions=template_versions,
            asset_versions=asset_versions,
            manifest=manifest,
            production_mode=production_mode,
            marker=marker,
            artifact_paths=artifact_paths,
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

    async def create_rebuild_run(
        self,
        artifact_key: str,
        *,
        output_profile: str = "a3v-professional",
    ) -> object:
        return await ArtifactProductionService(
            session_factory=self._session_factory,
            catalog=self.catalog,
        ).create_rebuild_run(
            event_id=self.event_id,
            revision_id=self.revision_id,
            artifact_key=artifact_key,
            output_profile=output_profile,
            requested_by="artifact-workflow-test",
        )

    async def publish_epicenter_artifact(self) -> GeneratedArtifact:
        return await self.published_artifact(
            "map.epicenter",
            version=1,
        )

    async def publish_full_progress(self) -> ProductionRun:
        repository = ArtifactProductionRepository()
        run = await self.create_full_run()
        async with self._session_factory() as session:
            async with session.begin():
                tasks = await repository.list_tasks(session, run.id)
                for task in tasks:
                    await repository.mark_task_succeeded_for_test(
                        session,
                        task.id,
                    )
                return await repository.finalize_run(
                    session,
                    run.id,
                    datetime.now(UTC),
                )

    async def full_progress(self, event_id: uuid.UUID) -> str:
        async with self._session_factory() as session:
            run = await session.scalar(
                select(ProductionRun).where(
                    ProductionRun.event_id == event_id,
                    ProductionRun.generation_scope == "full",
                    ProductionRun.is_current.is_(True),
                    ProductionRun.superseded_at.is_(None),
                )
            )
            if run is None:
                return "0/0"
            tasks = await ArtifactProductionRepository().list_tasks(
                session,
                run.id,
            )
        completed = sum(
            task.status in {"succeeded", "degraded"}
            for task in tasks
        )
        return f"{completed}/{len(run.required_outputs)}"

    def standalone_input(
        self,
        run: object,
    ) -> ArtifactProductionWorkflowInput:
        return ArtifactProductionWorkflowInput(
            production_run_id=str(run.production_run_id),
            assessment_run_id=str(self.assessment_run_id),
            event_id=str(self.event_id),
            revision_id=str(self.revision_id),
            deadline_at=run.deadline_at.isoformat(),
            catalog_version=self.catalog.catalog_version,
            context_fingerprint="",
            launch_mode="standalone",
            generation_seq=1,
            generation_scope=f"artifact:{run.required_outputs[0][0]}:"
            f"{run.required_outputs[0][1]}",
            required_outputs=run.required_outputs,
            render_concurrency=settings.artifact_render_concurrency,
            deadline_basis_at=run.deadline_basis_at.isoformat(),
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
        production_mode: str = "live",
    ) -> GeneratedArtifact:
        repository = ArtifactProductionRepository()
        async with self._session_factory() as session:
            async with session.begin():
                command = (
                    self.full_run_command(production_mode=production_mode)
                    if version == 1
                    else self.rebuild_command(
                        artifact_key,
                        production_mode=production_mode,
                    )
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

    async def expired_artifact(
        self,
        production_mode: str,
        age_days: int,
    ) -> GeneratedArtifact:
        if production_mode not in {"test", "drill", "live"}:
            raise ValueError("retention fixture supports test, drill, and live modes")
        generated_at = self.assessment.deadline_basis_at - timedelta(days=age_days)
        async with self._session_factory() as session:
            async with session.begin():
                versions = await session.scalars(
                    select(GeneratedArtifact.artifact_version).where(
                        GeneratedArtifact.event_id == self.event_id,
                        GeneratedArtifact.artifact_key == "map.epicenter",
                    )
                )
                version = int(max(versions.all() or [0]) or 0) + 1
            artifact = await self.generated_artifact(
                "map.epicenter",
                version=version,
                production_mode=production_mode,
            )
        async with self._session_factory() as session:
            async with session.begin():
                stored = await session.get(GeneratedArtifact, artifact.id)
                if stored is None:
                    raise LookupError("expired artifact fixture was not persisted")
                stored.generated_at = generated_at
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


@dataclass(frozen=True, slots=True)
class ArtifactAcceptanceArtifact:
    file_name: str
    context_fingerprint: str
    artifact_key: str = ""
    output_profile: str = ""
    status: str = ""
    degradation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactAcceptanceResult:
    required_output_count: int
    complete_count: int
    degraded_count: int
    failed_count: int
    timeout_count: int
    elapsed_seconds: float
    artifacts: tuple[ArtifactAcceptanceArtifact, ...]
    production_run_id: str = ""
    deadline_basis_at: str = ""
    deadline_at: str = ""


def _acceptance_record(
    row_number: int,
    business_key: str,
    properties: dict,
    geometry_wkt: str | None,
) -> object:
    from app.data_assets.domain import NormalizedRecord

    return NormalizedRecord(
        row_number=row_number,
        business_key=business_key,
        properties=properties,
        geometry_wkt=geometry_wkt,
    )


def _town_code() -> str:
    return "310115000001"


def _town_geometry() -> str:
    return (
        "MULTIPOLYGON (((121.4 31.1, 121.6 31.1, "
        "121.6 31.4, 121.4 31.4, 121.4 31.1)))"
    )


def _city_geometry() -> str:
    return (
        "MULTIPOLYGON (((121.2 30.9, 121.8 30.9, "
        "121.8 31.5, 121.2 31.5, 121.2 30.9)))"
    )


def _point_geometry(longitude: float, latitude: float) -> str:
    return f"POINT ({longitude} {latitude})"


class ArtifactAcceptanceEnvironment:
    """Seed a deterministic production grid and run real Temporal workflows."""

    def __init__(self, session_factory, seeded: ArtifactAssessmentFixture) -> None:
        self._session_factory = session_factory
        self._seeded = seeded
        self._asset_ids: set[uuid.UUID] = set()
        self._version_ids: set[uuid.UUID] = set()
        self._seeded_runtime = False
        self._task_queue = f"artifact-acceptance-{uuid.uuid4().hex}"
        self._client = None
        self._worker = None
        self._worker_task = None

    async def seed(self) -> None:
        if self._seeded_runtime:
            return
        await self._seed_templates()
        await self._seed_basemap()
        await self._ensure_region_boundary()
        await self._seed_data_assets()
        await self._seed_assessment_products()
        self._seeded_runtime = True

    async def run_test_event(
        self,
        *,
        magnitude: float,
        production_mode: str,
    ) -> ArtifactAcceptanceResult:
        await self.seed()
        now = datetime.now(UTC)
        await self._set_event_magnitude(magnitude)
        run = await self._create_standalone_run(
            production_mode=production_mode,
            deadline_basis_at=now,
            deadline_at=now + timedelta(seconds=300),
        )
        return await self._execute_run(run)

    async def run_selected_outputs(
        self,
        *,
        required_outputs: tuple[tuple[str, str], ...],
        production_mode: str,
        magnitude: float | None = None,
    ) -> ArtifactAcceptanceResult:
        if not required_outputs:
            raise ValueError("required_outputs must not be empty")
        await self.seed()
        now = datetime.now(UTC)
        if magnitude is not None:
            await self._set_event_magnitude(magnitude)
        run = await self._create_standalone_run(
            production_mode=production_mode,
            deadline_basis_at=now,
            deadline_at=now + timedelta(seconds=300),
            required_outputs=required_outputs,
        )
        return await self._execute_run(run)

    async def run_replay_event(
        self,
        *,
        t1_at: str,
        production_mode: str,
    ) -> ArtifactAcceptanceResult:
        await self.seed()
        deadline_basis_at = datetime.fromisoformat(t1_at)
        declared_deadline_at = deadline_basis_at + timedelta(seconds=300)
        await self._prepare_replay_context(
            deadline_basis_at=deadline_basis_at,
            deadline_at=declared_deadline_at,
        )
        run = await self._create_standalone_run(
            production_mode=production_mode,
            deadline_basis_at=deadline_basis_at,
            deadline_at=declared_deadline_at,
        )
        return await self._execute_run(run)

    async def _prepare_replay_context(
        self,
        *,
        deadline_basis_at: datetime,
        deadline_at: datetime,
    ) -> None:
        from app.assessment.models import AssessmentRun, AssessmentTask
        from app.events.models import (
            EarthquakeEvent,
            EarthquakeRevision,
            EventLifecycleOutbox,
        )

        async with self._session_factory() as session:
            async with session.begin():
                assessment = await session.get(
                    AssessmentRun,
                    self._seeded.assessment_run_id,
                    with_for_update=True,
                )
                event = await session.get(
                    EarthquakeEvent,
                    self._seeded.event_id,
                    with_for_update=True,
                )
                revision = await session.get(
                    EarthquakeRevision,
                    self._seeded.revision_id,
                )
                if (
                    assessment is None
                    or event is None
                    or revision is None
                ):
                    raise LookupError("replay assessment context is missing")
                outbox = await session.get(
                    EventLifecycleOutbox,
                    assessment.outbox_id,
                )
                assessment.deadline_basis_at = deadline_basis_at
                assessment.deadline_at = deadline_at
                assessment.t1_at = deadline_basis_at
                assessment.report_ingested_at = deadline_basis_at
                event.t1_at = deadline_basis_at
                revision.ingested_at = deadline_basis_at
                if outbox is not None:
                    outbox.created_at = deadline_basis_at
                    outbox.available_at = deadline_basis_at
                tasks = (
                    await session.scalars(
                        select(AssessmentTask).where(
                            AssessmentTask.run_id == assessment.id
                        )
                    )
                ).all()
                for task in tasks:
                    task.deadline_at = deadline_at
                await session.flush()

    async def _set_event_magnitude(self, magnitude: float) -> None:
        from decimal import Decimal

        async with self._session_factory() as session:
            async with session.begin():
                event = await session.get(EarthquakeEvent, self._seeded.event_id)
                if event is None:
                    raise LookupError("acceptance event was not found")
                event.magnitude = Decimal(str(magnitude))

    async def _create_standalone_run(
        self,
        *,
        production_mode: str,
        deadline_basis_at: datetime,
        deadline_at: datetime,
        required_outputs: tuple[tuple[str, str], ...] | None = None,
    ) -> ProductionRun:
        repository = ArtifactProductionRepository()
        selected_outputs = (
            required_outputs
            if required_outputs is not None
            else self._seeded.catalog.full_required_outputs()
        )
        if len(selected_outputs) == 1:
            artifact_key, output_profile = selected_outputs[0]
            command = self._seeded.rebuild_command(
                artifact_key,
                output_profile=output_profile,
                production_mode=production_mode,
                deadline_basis_at=deadline_basis_at,
                deadline_at=deadline_at,
                deadline_kind="rebuild_deadline",
            )
        else:
            command = self._seeded.full_run_command(
                production_mode=production_mode,
                launch_mode="standalone",
                deadline_basis_at=deadline_basis_at,
                deadline_at=deadline_at,
                deadline_kind="rebuild_deadline",
                required_outputs=selected_outputs,
            )
        async with self._session_factory() as session:
            async with session.begin():
                return await repository.create_run(session, command)

    async def _execute_run(self, run: ProductionRun) -> ArtifactAcceptanceResult:
        from temporalio.common import WorkflowIDConflictPolicy

        from app.artifacts.workflow import (
            ArtifactProductionWorkflow,
            ArtifactProductionWorkflowInput,
        )

        async with self._session_factory() as session:
            async with session.begin():
                stored = await session.get(ProductionRun, run.id)
                if stored is None:
                    raise LookupError("acceptance production run was not found")
                request = ArtifactProductionWorkflowInput(
                    production_run_id=str(stored.id),
                    assessment_run_id=str(
                        stored.assessment_run_id
                        if stored.assessment_run_id is not None
                        else ""
                    ),
                    event_id=str(stored.event_id),
                    revision_id=str(stored.revision_id),
                    deadline_at=stored.deadline_at.isoformat(),
                    catalog_version=stored.catalog_version,
                    context_fingerprint="",
                    launch_mode="standalone",
                    generation_seq=stored.generation_seq,
                    generation_scope=stored.generation_scope,
                    required_outputs=tuple(
                        _required_output_pair(item)
                        for item in stored.required_outputs
                    ),
                    render_concurrency=settings.artifact_render_concurrency,
                    deadline_basis_at=stored.deadline_basis_at.isoformat(),
                )
        await self._ensure_worker()
        started = asyncio.get_running_loop().time()
        handle = await self._client.start_workflow(
            ArtifactProductionWorkflow.run,
            request,
            id=f"artifact-performance:{run.id}",
            task_queue=self._task_queue,
            execution_timeout=timedelta(seconds=900),
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        try:
            await asyncio.wait_for(
                handle.result(),
                timeout=360,
            )
        except asyncio.TimeoutError:
            try:
                await handle.terminate(reason="artifact acceptance bounded timeout")
            except Exception:
                pass
        elapsed_seconds = asyncio.get_running_loop().time() - started
        async with self._session_factory() as session:
            async with session.begin():
                return await self._build_result(
                    session,
                    run.id,
                    elapsed_seconds=elapsed_seconds,
                )

    async def _build_result(
        self,
        session,
        run_id: uuid.UUID,
        *,
        elapsed_seconds: float,
        artifacts: tuple[ArtifactAcceptanceArtifact, ...] | None = None,
    ) -> ArtifactAcceptanceResult:
        run = await session.get(ProductionRun, run_id)
        if run is None:
            raise LookupError("acceptance production run was not found")
        tasks = list(
            (
                await session.scalars(
                    select(ProductionTask).where(
                        ProductionTask.production_run_id == run.id
                    )
                )
            ).all()
        )
        task_counts = self._task_counts_for(tasks)
        if artifacts is None:
            artifact_rows = list(
                (
                    await session.scalars(
                        select(GeneratedArtifact).where(
                            GeneratedArtifact.production_run_id == run.id,
                            GeneratedArtifact.is_final.is_(True),
                        )
                    )
                ).all()
            )
            artifacts = self._artifacts_for(
                artifact_rows,
                run.context_fingerprint or "",
            )
        return ArtifactAcceptanceResult(
            required_output_count=len(run.required_outputs or ()),
            complete_count=task_counts["complete"],
            degraded_count=task_counts["degraded"],
            failed_count=task_counts["failed"],
            timeout_count=task_counts["timeout"],
            elapsed_seconds=elapsed_seconds,
            artifacts=artifacts,
            production_run_id=str(run.id),
            deadline_basis_at=run.deadline_basis_at.isoformat(),
            deadline_at=run.deadline_at.isoformat(),
        )

    def _task_counts_for(self, tasks: list[ProductionTask]) -> dict[str, int]:
        counts = {"complete": 0, "degraded": 0, "failed": 0, "timeout": 0}
        for task in tasks:
            if task.status == "succeeded":
                counts["complete"] += 1
            elif task.status == "degraded":
                counts["degraded"] += 1
            elif task.status in {"failed", "canceled"}:
                counts["failed"] += 1
            elif task.status == "timed_out":
                counts["timeout"] += 1
        return counts

    def _artifacts_for(
        self,
        artifacts: list[GeneratedArtifact],
        context_fingerprint: str,
    ) -> tuple[ArtifactAcceptanceArtifact, ...]:
        return tuple(
            ArtifactAcceptanceArtifact(
                file_name=artifact.file_name,
                context_fingerprint=context_fingerprint,
                artifact_key=artifact.artifact_key,
                output_profile=artifact.output_profile,
                status=(
                    "degraded"
                    if artifact.status == "degraded"
                    else "complete"
                ),
                degradation_reasons=_artifact_degradation_reasons(artifact),
            )
            for artifact in sorted(
                artifacts,
                key=lambda item: (item.artifact_key, item.output_profile),
            )
        )

    async def _seed_templates(self) -> None:
        from app.artifacts.catalog import load_catalog

        catalog = load_catalog(settings.artifact_catalog_path)
        now = datetime.now(UTC)
        definitions = {
            "background-template": (
                "docx",
                "背景文档模板",
                Path(settings.artifact_template_root)
                / "background-template.docx",
            ),
            "decision-template": (
                "pptx",
                "辅助决策演示模板",
                Path(settings.artifact_template_root)
                / "decision-template.pptx",
            ),
        }
        async with self._session_factory() as session:
            async with session.begin():
                existing = {
                    row.template_key: row
                    for row in (
                        await session.scalars(select(ArtifactTemplate))
                    ).all()
                }
                for template_key, (kind, display_name, path) in definitions.items():
                    checksum = _sha256_file(path)
                    template = existing.get(template_key)
                    if template is None:
                        template = ArtifactTemplate(
                            template_key=template_key,
                            kind=kind,
                            display_name=display_name,
                        )
                        session.add(template)
                        await session.flush()
                    version = await session.scalar(
                        select(ArtifactTemplateVersion).where(
                            ArtifactTemplateVersion.template_id == template.id,
                            ArtifactTemplateVersion.status == "published",
                        )
                    )
                    if version is None:
                        session.add(
                            ArtifactTemplateVersion(
                                template_id=template.id,
                                version="v1",
                                status="published",
                                manifest={
                                    "catalog_version": catalog.catalog_version,
                                    "template_key": template_key,
                                },
                                checksum=checksum,
                                storage_path=path.name,
                                created_by="artifact-acceptance",
                                published_at=now,
                            )
                        )

    async def _seed_basemap(self) -> None:
        import shutil

        from app.artifacts.basemap import MapViewportTileManifest, VIEWPORT_RADII_KM
        from tests.basemap_fixtures import png_tile_bytes, write_basemap_package

        manifests = tuple(
            MapViewportTileManifest.build(
                center_lon=121.5,
                center_lat=31.2,
                radius_km=radius_km,
                output_width=settings.artifact_basemap_output_width,
                output_height=settings.artifact_basemap_output_height,
                zoom_levels=settings.artifact_basemap_zoom_levels,
                padding=settings.artifact_basemap_buffer_pixels,
            )
            for radius_km in VIEWPORT_RADII_KM
        )
        tiles = tuple(
            sorted(
                {
                    tile
                    for manifest in manifests
                    for tile in manifest.tiles
                }
            )
        )
        zoom_levels = tuple(sorted({tile.z for tile in tiles}))
        bounds = _tile_bounds_for_acceptance(tiles)
        provider_dir = Path(settings.artifact_basemap_root) / "gaode"
        if provider_dir.exists():
            shutil.rmtree(provider_dir)
        write_basemap_package(
            Path(settings.artifact_basemap_root),
            provider="gaode",
            package_format="mbtiles",
            tiles=tiles,
            zoom_levels=zoom_levels,
            coverage_bounds=bounds,
            tile_bytes=png_tile_bytes(color=(70, 130, 180)),
            generated_at=datetime.now(UTC),
            package_id="gaode-offline-v1",
            version="v1",
        )

    async def _seed_data_assets(self) -> None:
        await self._publish_vector_asset(
            "shanghai.admin.city",
            (
                _acceptance_record(
                    1,
                    "shanghai",
                    {
                        "ID": "shanghai",
                        "NAME": "Shanghai",
                        "geography": "长江三角洲冲积平原",
                        "administration": "上海市及邻近行政区",
                        "key_risks": "人口密集区、重点目标、危险源与断裂带",
                    },
                    _city_geometry(),
                ),
            ),
            spatial_extent=(121.2, 30.9, 121.8, 31.5),
        )
        await self._publish_vector_asset(
            "shanghai.admin.town",
            tuple(
                _acceptance_record(
                    index,
                    town_code,
                    {"ID": town_code, "NAME": f"acceptance town {index}"},
                    _town_geometry(),
                )
                for index, town_code in enumerate(
                    (f"{310115000001 + offset}" for offset in range(2)),
                    start=1,
                )
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.4),
        )
        await self._publish_table_asset(
            "shanghai.population.town",
            (
                _acceptance_record(
                    1,
                    _town_code(),
                    {
                        "ID": _town_code(),
                        "NAME": "acceptance town",
                        "total": 120000,
                        "resident": 100000,
                        "floating": 20000,
                        "family": 52000,
                        "under14": 21840,
                        "over65": 12240,
                    },
                    None,
                ),
            ),
        )
        await self._publish_table_asset(
            "shanghai.building.town",
            tuple(
                _acceptance_record(
                    index,
                    town_code,
                    {
                        "id": town_code,
                        "name": f"town-{index}",
                        "TOTAL_AREA": 1000,
                        "HIGH_RISE": 200,
                        "RCFRAME": 300,
                        "BRICK_STRUCTURE": 250,
                        "SINGLE_AREA": 150,
                        "OTHER_STRUCTURE": 100,
                    },
                    None,
                )
                for index, town_code in enumerate(
                    (f"{310115000001 + offset}" for offset in range(2)),
                    start=1,
                )
            ),
        )
        await self._publish_table_asset(
            "shanghai.economy.county",
            (
                _acceptance_record(
                    1,
                    "310115",
                    {
                        "ID": "310115",
                        "NAME": "acceptance county",
                        "gdp": 560000,
                        "agri_value": 12000,
                        "industry_value": 220000,
                        "service_value": 328000,
                    },
                    None,
                ),
            ),
        )
        await self._publish_vector_asset(
            "shanghai.historical.earthquakes",
            (
                _acceptance_record(
                    1,
                    "history-1",
                    {
                        "event_id": "history-1",
                        "origin_time": "1990-01-01T00:00:00+00:00",
                        "magnitude": 4.8,
                        "depth_km": 10.0,
                        "place": "historical fixture",
                        "source": "fixture",
                        "disaster_flag": False,
                        "summary": "历史地震 1 条",
                    },
                    _point_geometry(121.5, 31.2),
                ),
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.3),
        )
        await self._publish_vector_asset(
            "shanghai.hazard_source",
            (
                _acceptance_record(
                    1,
                    "hazard-1",
                    {
                        "id": "hazard-1",
                        "name": "acceptance hazard",
                        "category": "chemical",
                        "risk_level": "high",
                        "authority": "fixture",
                        "updated_at": "2026-01-01",
                    },
                    _point_geometry(121.5, 31.2),
                ),
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.3),
        )
        await self._publish_vector_asset(
            "shanghai.education.school",
            (
                _acceptance_record(
                    1,
                    "school-1",
                    {
                        "id": "school-1",
                        "name": "acceptance school",
                        "school_level": "primary",
                        "address": "fixture",
                        "capacity": 1000,
                        "building_area": 5000,
                        "updated_at": "2026-01-01",
                    },
                    _point_geometry(121.5, 31.2),
                ),
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.3),
        )
        await self._publish_vector_asset(
            "shanghai.health.hospital",
            (
                _acceptance_record(
                    1,
                    "hospital-1",
                    {
                        "id": "hospital-1",
                        "name": "acceptance hospital",
                        "hospital_level": "tertiary",
                        "address": "fixture",
                        "beds": 500,
                        "emergency_capacity": 200,
                        "updated_at": "2026-01-01",
                    },
                    _point_geometry(121.5, 31.2),
                ),
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.3),
        )
        await self._publish_vector_asset(
            "shanghai.fault",
            (
                _acceptance_record(
                    1,
                    "fault-1",
                    {
                        "OBJECTID": 1,
                        "name": "far fault",
                        "LENGTH": 100.0,
                    },
                    "MULTILINESTRING ((126.0 36.0, 126.6 36.6))",
                ),
            ),
            spatial_extent=(126.0, 36.0, 126.6, 36.6),
        )
        await self._publish_vector_asset(
            "shanghai.key_target",
            (
                _acceptance_record(
                    1,
                    "target-1",
                    {
                        "id": "target-1",
                        "name": "acceptance target",
                        "category": "government",
                        "criticality": "high",
                        "address": "fixture",
                        "authority": "fixture",
                    },
                    _point_geometry(121.5, 31.2),
                ),
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.3),
        )
        await self._publish_vector_asset(
            "shanghai.lifeline",
            (
                _acceptance_record(
                    1,
                    "lifeline-1",
                    {
                        "id": "lifeline-1",
                        "name": "acceptance lifeline",
                        "category": "power",
                        "criticality": "high",
                        "authority": "fixture",
                    },
                    "MULTILINESTRING ((121.4 31.1, 121.6 31.3))",
                ),
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.3),
        )
        await self._publish_vector_asset(
            "shanghai.distance.reference_points",
            (
                _acceptance_record(
                    1,
                    "distance-1",
                    {
                        "id": "distance-1",
                        "name": "Shanghai",
                        "category": "city",
                        "longitude": 121.5,
                        "latitude": 31.2,
                        "city_distance": 8.6,
                        "county_distance": 12.4,
                        "town_distance": 5.2,
                        "major_city_distance": 18.9,
                        "key_target_distance": 6.3,
                        "fault_distance": 21.7,
                    },
                    _point_geometry(121.5, 31.2),
                ),
            ),
            spatial_extent=(121.4, 31.1, 121.6, 31.3),
        )
        await self._seed_gdp_raster()

    async def _ensure_region_boundary(self) -> None:
        from decimal import Decimal

        from geoalchemy2.elements import WKTElement

        from app.regions.models import RegionBoundary

        async with self._session_factory() as session:
            async with session.begin():
                active = await session.scalar(
                    select(RegionBoundary).where(
                        RegionBoundary.is_active.is_(True)
                    )
                )
                if active is not None:
                    return
                geometry = WKTElement(
                    "MULTIPOLYGON (((120.8 30.6, 122.2 30.6, "
                    "122.2 31.9, 120.8 31.9, 120.8 30.6)))",
                    srid=4326,
                )
                session.add(
                    RegionBoundary(
                        version=f"artifact-acceptance-{uuid.uuid4()}",
                        name="artifact acceptance boundary",
                        local_buffer_km=Decimal("50"),
                        geom=geometry,
                        maritime_geom=geometry,
                        source_uri=(
                            "https://example.gov.invalid/artifact-acceptance"
                        ),
                        checksum="a" * 64,
                        is_active=True,
                    )
                )

    async def _publish_vector_asset(
        self,
        asset_key: str,
        records: tuple[object, ...],
        *,
        spatial_extent: tuple[float, float, float, float] | None,
    ) -> None:
        await self._insert_published_asset(
            asset_key,
            records,
            spatial_extent=spatial_extent,
            data_type="vector",
        )

    async def _publish_table_asset(
        self,
        asset_key: str,
        records: tuple[object, ...],
    ) -> None:
        await self._insert_published_asset(
            asset_key,
            records,
            spatial_extent=None,
            data_type="table",
        )

    async def _insert_published_asset(
        self,
        asset_key: str,
        records: tuple[object, ...],
        *,
        spatial_extent: tuple[float, float, float, float] | None,
        data_type: str,
    ) -> None:
        from geoalchemy2.elements import WKTElement

        from app.data_assets.domain import NormalizedRecord, NormalizedTableData
        from app.data_assets.models import DataAsset, DataAssetRecord, DataAssetVersion
        from app.data_assets.repository import DataAssetRepository
        from app.data_assets.service import compute_table_checksum

        columns = tuple(
            sorted({key for record in records for key in record.properties})
        )
        normalized = NormalizedTableData(
            columns=columns,
            records=records,
            source_crs="EPSG:4326",
            spatial_extent=spatial_extent,
        )
        checksum = compute_table_checksum(normalized)
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                asset = await session.scalar(
                    select(DataAsset).where(
                        DataAsset.asset_key == asset_key,
                        DataAsset.region_id == "shanghai",
                    )
                )
                if asset is None:
                    asset = DataAsset(
                        asset_key=asset_key,
                        region_id="shanghai",
                        name=asset_key,
                        data_type=data_type,
                        spatial_granularity="feature",
                        responsibility_unit="artifact-acceptance",
                        update_interval_days=365,
                        is_core=False,
                        contract={},
                    )
                    session.add(asset)
                    await session.flush()
                    self._asset_ids.add(asset.id)
                await session.execute(
                    update(DataAssetVersion)
                    .where(
                        DataAssetVersion.asset_id == asset.id,
                        DataAssetVersion.status == "published",
                    )
                    .values(status="retired", retired_at=now)
                )
                version = DataAssetVersion(
                    asset_id=asset.id,
                    version=f"acceptance-{asset_key}-{uuid.uuid4()}",
                    status="imported",
                    source_uri="https://example.gov.invalid/artifact-acceptance",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    quality_grade="A",
                    change_note="artifact acceptance fixture",
                    schema_summary={
                        "columns": list(columns),
                        "normalized_checksum": checksum,
                    },
                    record_count=normalized.record_count,
                    spatial_extent=(
                        WKTElement(
                            "POLYGON("
                            f"({spatial_extent[0]} {spatial_extent[1]}, "
                            f"{spatial_extent[2]} {spatial_extent[1]}, "
                            f"{spatial_extent[2]} {spatial_extent[3]}, "
                            f"{spatial_extent[0]} {spatial_extent[3]}, "
                            f"{spatial_extent[0]} {spatial_extent[1]}))",
                            srid=4326,
                        )
                        if spatial_extent is not None
                        else None
                    ),
                    source_crs="EPSG:4326",
                    checksum=checksum,
                    managed_path=f"fixture/{asset_key}",
                    imported_by="artifact-acceptance",
                    reviewed_by=None,
                    imported_at=now,
                    validated_at=now,
                    published_at=now,
                    retired_at=None,
                )
                session.add(version)
                await session.flush()
                self._version_ids.add(version.id)
                for record in records:
                    session.add(
                        DataAssetRecord(
                            version_id=version.id,
                            row_number=record.row_number,
                            business_key=record.business_key,
                            properties=dict(record.properties),
                            geom=(
                                WKTElement(record.geometry_wkt, srid=4326)
                                if record.geometry_wkt is not None
                                else None
                            ),
                        )
                    )
                await session.flush()
                persisted_records = await DataAssetRepository().list_records(
                    session,
                    version.id,
                )
                persisted_checksum = compute_table_checksum(
                    NormalizedTableData(
                        columns=columns,
                        records=tuple(
                            NormalizedRecord(
                                row_number=record.row_number,
                                business_key=record.business_key,
                                properties=dict(record.properties),
                                geometry_wkt=record.geometry_wkt,
                            )
                            for record in persisted_records
                        ),
                        source_crs="EPSG:4326",
                        spatial_extent=None,
                    )
                )
                version.checksum = persisted_checksum
                version.schema_summary = {
                    "columns": list(columns),
                    "normalized_checksum": persisted_checksum,
                }
                await session.flush()
                version.status = "validated"
                version.validated_at = now
                version.published_at = now
                await session.flush()
                version.status = "published"

    async def _seed_gdp_raster(self) -> None:
        import hashlib
        import tempfile
        from pathlib import Path

        import numpy as np
        import rasterio
        from rasterio.transform import Affine

        from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
        from app.data_assets.registry import get_asset_definition
        from app.data_assets.raster_importer import GeoTiffAssetImporter
        from app.data_assets.service import DataAssetService

        bounds = (120.8, 30.6, 122.2, 31.9)
        width = 16
        height = 16
        min_x, min_y, max_x, max_y = bounds
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "gdp-acceptance.tif"
            transform = Affine(
                (max_x - min_x) / width,
                0,
                min_x,
                0,
                -(max_y - min_y) / height,
                max_y,
            )
            with rasterio.open(
                source_path,
                "w",
                driver="GTiff",
                width=width,
                height=height,
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                nodata=-9999.0,
                transform=transform,
            ) as target:
                target.write(
                    np.full((height, width), 100.0, dtype="float32"),
                    1,
                )
            definition = get_asset_definition("shanghai.gdp.raster")
            descriptor = GeoTiffAssetImporter().load(source_path, definition)
            source_checksum = hashlib.sha256(source_path.read_bytes()).hexdigest()
            async with self._session_factory() as session:
                async with session.begin():
                    await self._retire_published_asset(session, "shanghai.gdp.raster")
                    job = await queue_import_job(
                        session,
                        QueueImportRequest(
                            asset_key="shanghai.gdp.raster",
                            version=f"acceptance-gdp-{uuid.uuid4()}",
                            source_uri=(
                                "https://example.gov.invalid/artifact-acceptance/gdp"
                            ),
                            license_name=None,
                            acquired_at=None,
                            valid_from=None,
                            valid_to=None,
                            change_note="artifact acceptance gdp fixture",
                            file_name="gdp-acceptance.tif",
                            file_format="geotiff",
                            file_size_bytes=source_path.stat().st_size,
                            checksum=source_checksum,
                            relative_path="fixture/gdp-acceptance.tif",
                            requested_by="artifact-acceptance",
                        ),
                    )
                    service = DataAssetService()
                    await service.populate_candidate_version(
                        session,
                        job.asset_version_id,
                        descriptor,
                        {
                            "file_format": "geotiff",
                            "source_crs": descriptor.source_crs,
                        },
                        source_path=source_path,
                    )
                    report = await service.validate_version(
                        session,
                        job.asset_version_id,
                        actor="artifact-acceptance",
                    )
                    if not report.publishable:
                        raise AssertionError(
                            "GDP acceptance raster failed validation: "
                            f"{[issue.message for issue in report.errors]}"
                        )
                    version = await service.publish_version(
                        session,
                        job.asset_version_id,
                        "artifact-acceptance",
                        "artifact acceptance gdp fixture",
                    )
                    self._version_ids.add(version.id)

    async def _retire_published_asset(self, session, asset_key: str) -> None:
        from app.data_assets.models import DataAsset, DataAssetVersion

        await session.execute(
            update(DataAssetVersion)
            .where(
                DataAssetVersion.asset_id.in_(
                    select(DataAsset.id).where(
                        DataAsset.asset_key == asset_key,
                        DataAsset.region_id == "shanghai",
                    )
                ),
                DataAssetVersion.status == "published",
            )
            .values(status="retired", retired_at=datetime.now(UTC))
        )

    async def _seed_assessment_products(self) -> None:
        await self._seed_fusion_product()
        await self._seed_loss_products()

    async def _seed_fusion_product(self) -> None:
        import numpy as np

        from app.assessment.models import AssessmentTask
        from app.intensity.domain import ProductStatus, ProductType
        from app.intensity.repository import (
            IntensityProductWrite,
            IntensityRepository,
        )

        async with self._session_factory() as session:
            async with session.begin():
                task = await session.scalar(
                    select(AssessmentTask).where(
                        AssessmentTask.run_id == self._seeded.assessment_run_id,
                        AssessmentTask.task_key == "intensity.fusion",
                    )
                )
                if task is None:
                    task = AssessmentTask(
                        run_id=self._seeded.assessment_run_id,
                        task_key="intensity.fusion",
                        task_type="intensity",
                        component="artifact-acceptance",
                        sequence=1,
                        deadline_at=self._seeded.deadline_at,
                        status="succeeded",
                    )
                    session.add(task)
                    await session.flush()
                existing = await session.scalar(
                    select(IntensityFieldProduct).where(
                        IntensityFieldProduct.run_id
                        == self._seeded.assessment_run_id,
                        IntensityFieldProduct.product_type == "fusion",
                    )
                )
                if existing is None:
                    await IntensityRepository().save_product(
                        session,
                        IntensityProductWrite(
                            run_id=self._seeded.assessment_run_id,
                            task_id=task.id,
                            product_type=ProductType.FUSION,
                            status=ProductStatus.AVAILABLE,
                            algorithm_version="fusion-v1",
                            parameter_version="parameters-v1",
                            strategy_version="strategy-v1",
                            grid_definition=_acceptance_grid(),
                            region_profile_version="region-v1",
                            input_fingerprint="b" * 64,
                            input_checksum="c" * 64,
                            quality_grade="A",
                            coverage_ratio=1,
                            statistics={
                                "minimum": 5.0,
                                "maximum": 5.0,
                                "mean": 5.0,
                            },
                            source_product_id=None,
                            observed_at=self._seeded.now,
                            bands=[("value", np.array([[5.0]]))],
                        ),
                    )

    async def _seed_loss_products(self) -> None:
        from app.assessment.models import AssessmentTask
        from app.loss.domain import (
            LossCalibrationStatus,
            LossMetricValueStatus,
            LossProductStatus,
            LossProductType,
            LossQualityGrade,
            LossValueType,
        )
        from app.loss.repository import (
            LossMetricValueWrite,
            LossProductWrite,
            LossRepository,
        )
        from app.loss.models import LossProduct
        from app.loss.service import recompute_product_checksum

        product_specs = {
            "building_damage": {
                "metrics": (
                    ("severe_or_collapsed_area_m2", 1200.0, "available", "m2"),
                ),
                "quality": LossQualityGrade.L2,
            },
            "casualties": {
                "metrics": (
                    ("deaths", 12.0, "available", "count"),
                    ("injuries", 42.0, "available", "count"),
                    ("buried", 4.0, "available", "count"),
                ),
                "quality": LossQualityGrade.L1,
            },
            "economic_loss": {
                "metrics": (("total_loss_yuan", 8800.0, "available", "yuan"),),
                "quality": LossQualityGrade.L1,
            },
            "resource_demand": {
                "metrics": (
                    ("rescue_team.quantity", 5.0, "available", "team"),
                    ("tent.quantity", 100.0, "available", "count"),
                    ("drinking_water.quantity", 200.0, "available", "count"),
                    ("food.quantity", 300.0, "available", "count"),
                    ("clothing.quantity", 400.0, "available", "count"),
                    ("quilt.quantity", 500.0, "available", "count"),
                    ("blanket.quantity", 600.0, "available", "count"),
                    ("stretcher.quantity", 700.0, "available", "count"),
                    ("sickbed.quantity", 800.0, "available", "count"),
                    ("toilet.quantity", 900.0, "available", "count"),
                ),
                "quality": LossQualityGrade.L1,
            },
            "population_impact": {
                "metrics": (("affected", 4300.0, "available", "count"),),
                "quality": LossQualityGrade.L1,
            },
            "validation": {
                "metrics": (),
                "quality": LossQualityGrade.L1,
            },
        }
        for product_key, spec in product_specs.items():
            product_type = LossProductType(product_key)
            metrics = tuple(
                LossMetricValueWrite(
                    area_scope=(
                        "city"
                        if product_key == "resource_demand"
                        else "town"
                    ),
                    area_code=(
                        "shanghai"
                        if product_key == "resource_demand"
                        else _town_code()
                    ),
                    area_name="acceptance area",
                    metric_key=metric_key,
                    value_type=LossValueType.CENTRAL,
                    value_status=LossMetricValueStatus.AVAILABLE,
                    numeric_value=(
                        int(round(value))
                        if metric_key.endswith(".quantity")
                        else float(value)
                    ),
                    unit=unit,
                    precision=2,
                    quality_grade=LossQualityGrade.L1,
                    note=None,
                )
                for metric_key, value, _status, unit in spec["metrics"]
            )
            output_checksum = recompute_product_checksum(
                product_type,
                metrics,
                {},
            )
            async with self._session_factory() as session:
                async with session.begin():
                    task = await session.scalar(
                        select(AssessmentTask).where(
                            AssessmentTask.run_id
                            == self._seeded.assessment_run_id,
                            AssessmentTask.task_key == f"loss.{product_key}",
                        )
                    )
                    if task is None:
                        task = AssessmentTask(
                            run_id=self._seeded.assessment_run_id,
                            task_key=f"loss.{product_key}",
                            task_type="loss",
                            component="artifact-acceptance",
                            sequence=1,
                            deadline_at=self._seeded.deadline_at,
                            status="succeeded",
                        )
                        session.add(task)
                        await session.flush()
                    existing = await session.scalar(
                        select(LossProduct).where(
                            LossProduct.run_id == self._seeded.assessment_run_id,
                            LossProduct.product_type == product_key,
                        )
                    )
                    if existing is not None:
                        continue
                    raster = None
                    if product_key == "building_damage":
                        raster = _building_grid_raster()
                    await LossRepository().write_product(
                        session,
                        LossProductWrite(
                            run_id=self._seeded.assessment_run_id,
                            task_id=task.id,
                            product_type=product_type,
                            status=LossProductStatus.COMPLETE,
                            quality_grade=spec["quality"],
                            calibration_status=(
                                LossCalibrationStatus.REFERENCE_UNCALIBRATED
                            ),
                            coverage_ratio=1.0,
                            partial_scope=False,
                            needs_review=False,
                            spatialized_estimate=product_key
                            == "building_damage",
                            algorithm_version=f"{product_key}-v1",
                            parameter_version="parameters-v1",
                            region_profile_version="region-v1",
                            input_fingerprint="d" * 64,
                            input_checksum="e" * 64,
                            output_checksum=output_checksum,
                            statistics={},
                            metrics=metrics,
                            raster=raster,
                            reason=None,
                        ),
                    )

    async def cleanup(self) -> None:
        from sqlalchemy import delete

        await self._stop_worker()
        async with self._session_factory() as session:
            async with session.begin():
                if self._version_ids:
                    from app.data_assets.models import DataAssetVersion

                    await session.execute(
                        delete(DataAssetVersion).where(
                            DataAssetVersion.id.in_(tuple(self._version_ids))
                        )
                    )
                if self._asset_ids:
                    from app.data_assets.models import DataAsset

                    await session.execute(
                        delete(DataAsset).where(
                            DataAsset.id.in_(tuple(self._asset_ids))
                        )
                    )
        self._version_ids.clear()
        self._asset_ids.clear()

    async def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        from temporalio.client import Client

        from app.artifacts.worker import build_artifact_worker

        self._client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
        )
        await self._terminate_running_artifact_workflows()
        configured = settings.model_copy(
            update={"temporal_task_queue": self._task_queue}
        )
        self._worker = build_artifact_worker(
            client=self._client,
            session_factory=self._session_factory,
            configured=configured,
        )
        self._worker_task = asyncio.create_task(self._worker.run())

    async def _terminate_running_artifact_workflows(self) -> None:
        async for item in self._client.list_workflows(
            'WorkflowType="ArtifactProductionWorkflow"'
        ):
            if item.status.name != "RUNNING":
                continue
            try:
                await self._client.get_workflow_handle(item.id).terminate(
                    reason="artifact acceptance fixture preflight"
                )
            except Exception:
                pass

    async def _stop_worker(self) -> None:
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._worker.shutdown(), timeout=10)
        except asyncio.TimeoutError:
            if self._worker_task is not None and not self._worker_task.done():
                self._worker_task.cancel()
        if self._worker_task is not None:
            await asyncio.gather(self._worker_task, return_exceptions=True)
        self._worker = None
        self._worker_task = None


def _acceptance_grid():
    from app.intensity.domain import GridDefinition

    return GridDefinition(
        "artifact-acceptance-grid",
        "EPSG:32651",
        1000,
        356000,
        3450000,
        1,
        1,
    )


def _building_grid_raster():
    import numpy as np

    from app.intensity.artifacts import RasterCodec
    from app.intensity.domain import GridDefinition
    from app.loss.repository import LossRasterBandWrite, LossRasterWrite

    definition = GridDefinition(
        "building-grid-v1",
        "EPSG:32651",
        1000,
        356000,
        3450000,
        2,
        2,
    )
    band_values = np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
    band_name = "buildings_collapsed_area_m2_central"
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
        "bands": [
            {
                "number": 1,
                "name": band_name,
                "unit": "m2",
                "precision": 2,
            }
        ],
    }
    checksum = RasterCodec.content_checksum(
        definition,
        [(band_name, band_values)],
        manifest,
        checksum_namespace="loss-raster-content-v1",
    )
    return LossRasterWrite(
        raster_version="building-grid-v1",
        definition=definition,
        bands=(
            LossRasterBandWrite(
                name=band_name,
                values=band_values,
                unit="m2",
                precision=2,
            ),
        ),
        band_manifest=manifest,
        checksum=checksum,
        spatial_allocation_rule="town-uniform-v1",
        coverage_ratio=1.0,
    )


def _tile_bounds_for_acceptance(tiles) -> tuple[float, float, float, float]:
    if not tiles:
        return (0.0, 0.0, 0.0, 0.0)
    from app.artifacts.basemap import EARTH_CIRCUMFERENCE_M

    def bounds(tile):
        tile_width = EARTH_CIRCUMFERENCE_M / (2**tile.z)
        west = -EARTH_CIRCUMFERENCE_M / 2.0 + tile.x * tile_width
        east = west + tile_width
        north = EARTH_CIRCUMFERENCE_M / 2.0 - tile.y * tile_width
        south = north - tile_width
        return west, south, east, north

    all_bounds = [bounds(tile) for tile in tiles]
    return (
        min(item[0] for item in all_bounds),
        min(item[1] for item in all_bounds),
        max(item[2] for item in all_bounds),
        max(item[3] for item in all_bounds),
    )


def _required_output_pair(item: object) -> tuple[str, str]:
    if isinstance(item, dict):
        return str(item["artifact_key"]), str(item["output_profile"])
    return str(item[0]), str(item[1])


def _artifact_degradation_reasons(
    artifact: GeneratedArtifact,
) -> tuple[str, ...]:
    manifest = artifact.render_manifest or {}
    quality = manifest.get("quality") if isinstance(manifest, dict) else None
    if not isinstance(quality, dict):
        quality = {}
    reasons = quality.get("degradation_reasons", ())
    if not reasons and artifact.status == "degraded":
        return ("unspecified",)
    return tuple(str(reason) for reason in reasons)


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
async def artifact_acceptance_environment(
    seeded_artifact_assessment,
    session_factory,
):
    environment = ArtifactAcceptanceEnvironment(
        session_factory,
        seeded_artifact_assessment,
    )
    yield environment
    await environment.cleanup()


@pytest.fixture
def artifact_repository() -> ArtifactProductionRepository:
    return ArtifactProductionRepository()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as session:
        async with session.begin():
            yield session
