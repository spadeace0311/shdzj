import hashlib
import uuid
from datetime import UTC, datetime
from io import BytesIO

import pytest
from sqlalchemy import select

from app.artifacts.domain import DependencyKind
from app.artifacts.basemap import (
    AllBasemapsUnavailableError,
    MapViewportTileManifest,
    VIEWPORT_RADII_KM,
)
from app.artifacts.context import ProductionContextService
from app.artifacts.models import (
    ArtifactTaskDependencyBinding,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionInputSnapshotItem,
)
from app.artifacts.repository import ArtifactProductionRepository
from app.artifacts.storage import ArtifactStore
from app.assessment.models import AssessmentRun, AssessmentTask
from app.config import settings
from app.data_assets.models import DataAsset, DataAssetRecord, DataAssetVersion
from app.data_assets.registry import get_asset_definition
from app.events.models import EarthquakeEvent
from app.loss.models import LossProduct
from app.loss.region import load_region_loss_profile
from tests.basemap_fixtures import (
    make_in_memory_package,
    png_tile_bytes,
    union_bounds_3857,
    write_basemap_package,
)
from tests.data_asset_helpers import (
    FIXTURE_ACTOR,
    _cleanup_fixture_data,
    publish_new_population_version,
)


async def _seed_document_data_asset(
    session,
    *,
    asset_key: str,
    properties: dict,
) -> uuid.UUID:
    try:
        definition = get_asset_definition(asset_key)
        region_id = definition.region_id
        name = definition.name
        data_type = definition.data_type.value
        granularity = definition.spatial_granularity
        responsibility = definition.responsibility_unit
        update_interval = definition.update_interval_days
        is_core = definition.is_core
    except KeyError:
        region_id = settings.data_asset_region_id
        name = asset_key
        data_type = "vector"
        granularity = "point"
        responsibility = "document-context-fixture"
        update_interval = 365
        is_core = False
    asset = await session.scalar(
        select(DataAsset).where(
            DataAsset.asset_key == asset_key,
            DataAsset.region_id == region_id,
        )
    )
    if asset is None:
        asset = DataAsset(
            asset_key=asset_key,
            region_id=region_id,
            name=name,
            data_type=data_type,
            spatial_granularity=granularity,
            responsibility_unit=responsibility,
            update_interval_days=update_interval,
            is_core=is_core,
            contract={"business_key_fields": ["ID"]},
        )
        session.add(asset)
        await session.flush()
    version = DataAssetVersion(
        asset_id=asset.id,
        version=f"document-{uuid.uuid4()}",
        status="imported",
        source_uri="https://example.gov.invalid/document-data",
        schema_summary={"columns": ["ID"]},
        record_count=1,
        source_crs="EPSG:4326",
        checksum=hashlib.sha256(asset_key.encode()).hexdigest(),
        imported_by=FIXTURE_ACTOR,
        imported_at=datetime.now(UTC),
    )
    session.add(version)
    await session.flush()
    session.add(
        DataAssetRecord(
            version_id=version.id,
            row_number=1,
            business_key=f"{asset_key}-1",
            properties=properties,
            geom=None,
        )
    )
    await session.flush()
    return version.id


def _snapshot_asset_entry(asset_key: str, version_id: uuid.UUID) -> dict:
    return {
        "asset_key": asset_key,
        "role": "optional",
        "resolution_status": "bound",
        "asset_version_id": str(version_id),
        "version": "document-v1",
        "checksum": hashlib.sha256(asset_key.encode()).hexdigest(),
        "coverage": {},
    }


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
def production_context_service() -> ProductionContextService:
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
    return ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(manifests, provider="gaode"),
            "tianditu": make_in_memory_package(manifests, provider="tianditu"),
        },
    )


def _settings_manifests() -> tuple[MapViewportTileManifest, ...]:
    return tuple(
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


def _write_context_package(
    root,
    *,
    provider: str,
    package_format: str = "mbtiles",
    tile_scheme: str = "tms",
) -> tuple[MapViewportTileManifest, ...]:
    manifests = _settings_manifests()
    tiles = tuple(
        sorted(
            {
                tile
                for manifest in manifests
                for tile in manifest.tiles
            }
        )
    )
    write_basemap_package(
        root,
        provider=provider,
        package_format=package_format,
        tiles=tiles,
        zoom_levels=tuple(sorted({tile.z for tile in tiles})),
        coverage_bounds=union_bounds_3857(tiles),
        tile_bytes=png_tile_bytes(),
        generated_at=datetime.now(UTC),
        tile_scheme=tile_scheme,
    )
    return manifests


@pytest.fixture(autouse=True)
async def _seed_template_versions_and_context_methods(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            for definition in seeded_artifact_assessment.catalog.definitions:
                template_key = definition.template_key
                template = await session.scalar(
                    select(ArtifactTemplate).where(
                        ArtifactTemplate.template_key == template_key
                    )
                )
                if template is None:
                    template = ArtifactTemplate(
                        template_key=template_key,
                        kind=definition.kind.value,
                        display_name=definition.display_name,
                    )
                    session.add(template)
                    await session.flush()
                existing = await session.scalar(
                    select(ArtifactTemplateVersion).where(
                        ArtifactTemplateVersion.template_id == template.id,
                        ArtifactTemplateVersion.version == "v1",
                    )
                )
                if existing is None:
                    session.add(
                        ArtifactTemplateVersion(
                            template_id=template.id,
                            version="v1",
                            status="published",
                            manifest={"version": "v1"},
                            checksum=hashlib.sha256(
                                f"{template_key}:v1".encode()
                            ).hexdigest(),
                            storage_path=f"templates/{template_key}/v1",
                            created_by="context-fixture",
                            published_at=datetime.now(UTC),
                        )
                    )
                versions = (
                    await session.scalars(
                        select(ArtifactTemplateVersion)
                        .where(
                            ArtifactTemplateVersion.template_id == template.id
                        )
                        .order_by(ArtifactTemplateVersion.version)
                    )
                ).all()
                for version in versions:
                    version.status = "published" if version.version == "v1" else "retired"

    original_create_full_run = seeded_artifact_assessment.create_full_run

    async def create_full_run(
        *,
        revision_no: int = 1,
        production_mode: str = "live",
        missing_optional_assets=None,
        t1_at=None,
    ):
        _ = missing_optional_assets
        if t1_at is None:
            async with session_factory() as session:
                async with session.begin():
                    event = await session.get(
                        EarthquakeEvent,
                        seeded_artifact_assessment.event_id,
                    )
                    event.t1_at = None
        return await original_create_full_run(
            revision_no=revision_no,
            production_mode=production_mode,
        )

    async def publish_new_template_version(
        template_key: str,
        version: str,
    ) -> ArtifactTemplateVersion:
        async with session_factory() as session:
            async with session.begin():
                template = await session.scalar(
                    select(ArtifactTemplate).where(
                        ArtifactTemplate.template_key == template_key
                    )
                )
                assert template is not None
                existing = (
                    await session.scalars(
                        select(ArtifactTemplateVersion)
                        .where(ArtifactTemplateVersion.template_id == template.id)
                    )
                ).all()
                for item in existing:
                    item.status = "retired"
                existing_version = next(
                    (
                        item
                        for item in existing
                        if item.version == version
                    ),
                    None,
                )
                if existing_version is not None:
                    existing_version.status = "published"
                    existing_version.manifest = {"version": version}
                    existing_version.checksum = hashlib.sha256(
                        f"{template_key}:{version}".encode()
                    ).hexdigest()
                    existing_version.storage_path = (
                        f"templates/{template_key}/{version}"
                    )
                    existing_version.published_at = datetime.now(UTC)
                    await session.flush()
                    return existing_version
                published = ArtifactTemplateVersion(
                    template_id=template.id,
                    version=version,
                    status="published",
                    manifest={"version": version},
                    checksum=hashlib.sha256(
                        f"{template_key}:{version}".encode()
                    ).hexdigest(),
                    storage_path=f"templates/{template_key}/{version}",
                    created_by="context-fixture",
                    published_at=datetime.now(UTC),
                )
                session.add(published)
                await session.flush()
                return published

    seeded_artifact_assessment.create_full_run = create_full_run
    seeded_artifact_assessment.publish_new_template_version = (
        publish_new_template_version
    )
    yield


@pytest.fixture
async def published_population_version(session_factory) -> uuid.UUID:
    version_id = await publish_new_population_version(
        session_factory,
        f"context-{uuid.uuid4()}",
    )
    yield version_id
    await _cleanup_fixture_data(session_factory)


@pytest.fixture
async def published_loss_parameter_version(session_factory) -> uuid.UUID:
    definition = get_asset_definition("shanghai.loss.parameters")
    version = f"loss-parameter-{uuid.uuid4()}"
    await _cleanup_fixture_data(session_factory)
    async with session_factory() as session:
        async with session.begin():
            asset = await session.scalar(
                select(DataAsset).where(
                    DataAsset.asset_key == definition.asset_key,
                    DataAsset.region_id == definition.region_id,
                )
            )
            if asset is None:
                asset = DataAsset(
                    asset_key=definition.asset_key,
                    region_id=definition.region_id,
                    name=definition.name,
                    data_type=definition.data_type.value,
                    spatial_granularity=definition.spatial_granularity,
                    responsibility_unit="fixture",
                    update_interval_days=definition.update_interval_days,
                    is_core=definition.is_core,
                    contract={
                        "business_key_fields": ["parameter_set_id"],
                        "fields": [
                            {
                                "name": "parameter_set_id",
                                "python_type": "string",
                                "required": True,
                                "nonnegative": False,
                                "minimum": None,
                                "maximum": None,
                            }
                        ],
                    },
                )
                session.add(asset)
                await session.flush()
            version_id = DataAssetVersion(
                asset_id=asset.id,
                version=version,
                status="published",
                source_uri="https://example.gov.invalid/loss-parameters",
                source_crs="EPSG:4326",
                checksum="b" * 64,
                schema_summary={"parameter_set_id": "loss-parameter-fixture"},
                imported_by=FIXTURE_ACTOR,
                imported_at=datetime.now(UTC),
                published_at=datetime.now(UTC),
            )
            session.add(version_id)
            await session.flush()
            result = version_id.id
    yield result
    await _cleanup_fixture_data(session_factory)


async def test_static_context_is_deterministic_and_does_not_drift(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    first = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )
    await seeded_artifact_assessment.publish_new_template_version(
        "map.epicenter",
        "v2",
    )
    second = await production_context_service.build_task_context(
        session,
        (await seeded_artifact_assessment.first_task(run.id)).id,
    )
    latest_template = await session.scalar(
        select(ArtifactTemplateVersion)
        .join(ArtifactTemplate, ArtifactTemplateVersion.template_id == ArtifactTemplate.id)
        .where(
            ArtifactTemplate.template_key == "map.epicenter",
            ArtifactTemplateVersion.status == "published",
        )
        .order_by(ArtifactTemplateVersion.published_at.desc())
        .limit(1)
    )

    assert len(first.catalog_version) == len("2026.09.30-professional-v1")
    assert second.context_fingerprint == first.context_fingerprint
    assert second.template_versions["map.epicenter"]["version"] == "v1"
    assert latest_template is not None
    assert latest_template.version == "v2"


async def test_document_context_resolves_generated_artifact_path(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )
    map_task = await seeded_artifact_assessment.first_task(
        run.id,
        "map.epicenter",
    )
    stored = ArtifactStore(settings.artifact_storage_root).store_immutable_stream(
        BytesIO(b"fixture-map-artifact"),
        file_name="epicenter.jpg",
    )
    artifact = GeneratedArtifact(
        production_run_id=run.id,
        production_task_id=map_task.id,
        event_id=seeded_artifact_assessment.event_id,
        revision_id=seeded_artifact_assessment.revision_id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        artifact_version=1,
        is_final=True,
        production_mode="live",
        status="complete",
        quality_grade="A",
        needs_review=False,
        publication_mode="automatic",
        marker=None,
        file_name="epicenter.jpg",
        format="jpg",
        storage_path=stored.relative_path,
        checksum=stored.checksum,
        size_bytes=stored.size_bytes,
        width=4761,
        height=3369,
        generated_at=datetime.now(UTC),
    )
    session.add(artifact)
    await session.flush()

    document_task = await seeded_artifact_assessment.first_task(
        run.id,
        "doc.area_overview",
    )
    context = await production_context_service.build_document_context(
        session,
        document_task.id,
        "docx",
    )

    assert context.artifact_paths["map.epicenter"] == stored.managed_path
    assert context.artifacts["map.epicenter"].checksum == stored.checksum


async def test_document_context_merges_bound_assessment_products(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )
    document_task = await seeded_artifact_assessment.first_task(
        run.id,
        "doc.housing",
    )
    assessment_task = AssessmentTask(
        run_id=run.assessment_run_id,
        task_key="loss.buildings",
        task_type="loss",
        component="document-context-fixture",
        sequence=1,
        status="succeeded",
        deadline_at=run.deadline_at,
    )
    session.add(assessment_task)
    await session.flush()
    product = LossProduct(
        run_id=run.assessment_run_id,
        task_id=assessment_task.id,
        product_type="building_damage",
        status="complete",
        quality_grade="L1",
        calibration_status="calibrated",
        coverage_ratio=1,
        partial_scope=False,
        needs_review=False,
        spatialized_estimate=False,
        algorithm_version="buildings-v1",
        parameter_version="parameters-v1",
        region_profile_version="region-v1",
        input_fingerprint="a" * 64,
        input_checksum="b" * 64,
        output_checksum="c" * 64,
        statistics={
            "total": 10000,
            "slight": 1200,
            "moderate": 300,
            "severe": 80,
        },
    )
    session.add(product)
    await session.flush()
    session.add(
        ArtifactTaskDependencyBinding(
            production_task_id=document_task.id,
            dependency_kind="assessment_product",
            dependency_key="loss.buildings",
            dependency_output_profile=None,
            is_optional=False,
            bound_entity_id=product.id,
            bound_version=product.algorithm_version,
            bound_checksum=product.output_checksum,
            resolution_status="bound",
            resolved_at=datetime.now(UTC),
        )
    )
    await session.flush()

    context = await production_context_service.build_document_context(
        session,
        document_task.id,
        "docx",
    )
    products = context.manifest["assessment"]["products"]["loss.buildings"]

    assert products["total"] == 10000
    assert products["slight"] == 1200
    assert products["moderate"] == 300
    assert products["severe"] == 80


async def test_document_context_builds_persisted_background_payload(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )
    snapshot = await session.scalar(
        select(ProductionInputSnapshot).where(
            ProductionInputSnapshot.production_run_id == run.id
        )
    )
    assert snapshot is not None

    historical_version = await _seed_document_data_asset(
        session,
        asset_key="shanghai.historical.earthquakes",
        properties={
            "radius_km": 50,
            "magnitude_threshold": 3.0,
            "summary": "半径内历史地震 12 条，最大震级 4.9",
            "disaster_summary": "灾害地震 3 条，需复核",
            "statistics": "12 条 / 3 条灾害",
        },
    )
    distance_version = await _seed_document_data_asset(
        session,
        asset_key="shanghai.distance.reference_points",
        properties={
            "city_distance": 8.6,
            "county_distance": 12.4,
            "town_distance": 5.2,
            "major_city_distance": 18.9,
            "key_target_distance": 6.3,
            "fault_distance": 21.7,
        },
    )
    admin_version = await _seed_document_data_asset(
        session,
        asset_key="shanghai.admin.city",
        properties={
            "geography": "长江三角洲冲积平原，水网密集",
            "administration": "上海市及邻近行政区",
            "key_risks": "人口密集区、重点目标、危险源与断裂带",
        },
    )
    assets = list(snapshot.manifest["assets"])
    assets_by_key = {entry["asset_key"]: entry for entry in assets}
    assets_by_key["shanghai.historical.earthquakes"] = _snapshot_asset_entry(
        "shanghai.historical.earthquakes",
        historical_version,
    )
    assets_by_key["shanghai.distance.reference_points"] = _snapshot_asset_entry(
        "shanghai.distance.reference_points",
        distance_version,
    )
    assets_by_key["shanghai.admin.city"] = _snapshot_asset_entry(
        "shanghai.admin.city",
        admin_version,
    )
    snapshot.manifest["assets"] = sorted(
        assets_by_key.values(),
        key=lambda item: item["asset_key"],
    )
    await session.flush()

    document_task = await seeded_artifact_assessment.first_task(
        run.id,
        "doc.background",
    )
    context = await production_context_service.build_document_context(
        session,
        document_task.id,
        "docx",
    )

    assert context.manifest["historical_earthquakes"]["summary"] == (
        "半径内历史地震 12 条，最大震级 4.9"
    )
    assert context.manifest["spatial_distances"]["city_distance"] == 8.6
    assert context.manifest["area_overview"]["administration"] == (
        "上海市及邻近行政区"
    )


async def test_missing_but_optional_asset_is_recorded_not_invented(
    seeded_artifact_assessment,
    production_context_service,
    session_factory,
    published_population_version,
) -> None:
    run = await seeded_artifact_assessment.create_full_run(
        missing_optional_assets={"shanghai.reservoir"},
    )
    async with session_factory() as freeze_session:
        async with freeze_session.begin():
            snapshot = await production_context_service.freeze_static_context(
                freeze_session,
                run.id,
                seeded_artifact_assessment.catalog,
            )

            item = snapshot.item("shanghai.reservoir")
            assert item.resolution_status == "missing"
            assert item.asset_version_id is None
            assert item.checksum is None

            items = (
                await freeze_session.scalars(
                    select(ProductionInputSnapshotItem).where(
                        ProductionInputSnapshotItem.snapshot_id.in_(
                            select(ProductionInputSnapshotItem.snapshot_id).where(
                                ProductionInputSnapshotItem.asset_key
                                == "shanghai.population.town"
                            )
                        )
                    )
                )
            ).all()
            by_key = {item.asset_key: item for item in items}
            assert "shanghai.population.town" in by_key
            assert "shanghai.reservoir" in by_key
            assert (
                by_key["shanghai.population.town"].asset_version_id
                == published_population_version
            )
            assert by_key["shanghai.population.town"].checksum is not None
            assert by_key["shanghai.reservoir"].asset_version_id is None
            assert by_key["shanghai.reservoir"].checksum is None

            manifest_by_key = {
                entry["asset_key"]: entry for entry in snapshot.manifest["assets"]
            }
            assert (
                manifest_by_key["shanghai.reservoir"]["asset_version_id"]
                is None
            )
            assert manifest_by_key["shanghai.reservoir"]["checksum"] is None


async def test_t1_none_is_serialized_as_null(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run(t1_at=None)
    context = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )

    assert context.manifest["t1_at"] is None


async def test_static_manifest_freezes_design_identity_fields(
    seeded_artifact_assessment,
    production_context_service,
    session,
    session_factory,
) -> None:
    async with session_factory() as preparation_session:
        async with preparation_session.begin():
            assessment = await preparation_session.get(
                AssessmentRun,
                seeded_artifact_assessment.assessment_run_id,
            )
            assert assessment is not None
            assessment.data_asset_snapshot_fingerprint = "d" * 64

    run = await seeded_artifact_assessment.create_full_run()
    context = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )

    expected_assessment_products = sorted(
        {
            dependency.key
            for definition in seeded_artifact_assessment.catalog.definitions
            for dependency in (
                *definition.depends_on,
                *definition.optional_depends_on,
            )
            if dependency.kind is DependencyKind.ASSESSMENT_PRODUCT
        }
    )
    assert (
        context.manifest["assessment"]["assessment_run_id"]
        == str(seeded_artifact_assessment.assessment_run_id)
    )
    assert (
        context.manifest["assessment"]["allowed_assessment_product_types"]
        == expected_assessment_products
    )
    assert (
        context.manifest["assessment"]["data_asset_snapshot_fingerprint"]
        == "d" * 64
    )

    profile = load_region_loss_profile(settings.loss_region_profile_path)
    assert context.manifest["loss"]["region_profile_version"] == profile.version
    assert context.manifest["loss"]["model_versions"] == profile.default_model_versions
    assert "parameter_package" in context.manifest["loss"]

    assert context.manifest["fonts"]["version"] != "shanghai-artifact-fonts-v1"
    assert len(context.manifest["fonts"]["checksum"]) == 64

    templates = {
        entry["template_key"]: entry for entry in context.manifest["templates"]
    }
    assert templates["map.epicenter"]["version"] == "v1"
    assert len(templates["map.epicenter"]["checksum"]) == 64
    assert templates["map.epicenter"]["kind"] == "map"


async def test_loss_parameter_package_freezes_published_identity(
    seeded_artifact_assessment,
    production_context_service,
    session_factory,
    published_loss_parameter_version,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()

    async with session_factory() as freeze_session:
        async with freeze_session.begin():
            context = await production_context_service.freeze_static_context(
                freeze_session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            version = await freeze_session.get(
                DataAssetVersion,
                published_loss_parameter_version,
            )
            assert version is not None

            package = context.manifest["loss"]["parameter_package"]
            assert package["resolution_status"] == "bound"
            assert package["version"] == version.version
            assert package["checksum"] == version.checksum

            item = context.item("shanghai.loss.parameters")
            assert item.resolution_status == "bound"
            assert item.asset_version_id == version.id
            assert item.checksum == version.checksum

            persisted = await freeze_session.scalar(
                select(ProductionInputSnapshotItem)
                .join(
                    ProductionInputSnapshot,
                    ProductionInputSnapshotItem.snapshot_id
                    == ProductionInputSnapshot.id,
                )
                .where(
                    ProductionInputSnapshot.production_run_id == run.id,
                    ProductionInputSnapshotItem.asset_key
                    == "shanghai.loss.parameters",
                )
            )
            assert persisted is not None
            assert persisted.asset_version_id == version.id
            assert persisted.checksum == version.checksum


async def test_static_context_freezes_both_basemap_candidates_and_selection(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    context = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )

    basemaps = context.manifest["basemaps"]
    assert context.selected_basemap is not None
    assert context.selected_basemap.provider == "gaode"
    assert basemaps["selected_basemap"]["provider"] == "gaode"
    assert basemaps["gaode"]["provider"] == "gaode"
    assert basemaps["gaode"]["version"] == "v1"
    assert len(basemaps["gaode"]["checksum"]) == 64
    assert basemaps["tianditu"]["provider"] == "tianditu"
    assert basemaps["tianditu"]["version"] == "v1"
    assert len(basemaps["tianditu"]["checksum"]) == 64


async def test_static_context_fails_hard_when_basemap_is_required_but_missing(
    seeded_artifact_assessment,
    session,
) -> None:
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={},
    )
    run = await seeded_artifact_assessment.create_full_run()

    with pytest.raises(AllBasemapsUnavailableError):
        await service.freeze_static_context(
            session,
            run.id,
            seeded_artifact_assessment.catalog,
        )


async def test_map_fallback_accepts_valid_gaode_when_tianditu_is_broken(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    _write_context_package(tmp_path, provider="gaode")
    _write_context_package(tmp_path, provider="tianditu")
    (tmp_path / "tianditu" / "tianditu.mbtiles").unlink()
    monkeypatch.setattr(settings, "artifact_basemap_root", str(tmp_path))

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
    )
    run = await seeded_artifact_assessment.create_full_run()
    async with session_factory() as freeze_session:
        async with freeze_session.begin():
            context = await service.freeze_static_context(
                freeze_session,
                run.id,
                seeded_artifact_assessment.catalog,
            )

    assert context.selected_basemap is not None
    assert context.selected_basemap.provider == "gaode"


async def test_map_fallback_uses_tianditu_when_gaode_is_broken(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    _write_context_package(tmp_path, provider="gaode")
    _write_context_package(tmp_path, provider="tianditu")
    (tmp_path / "gaode" / "gaode.mbtiles").unlink()
    monkeypatch.setattr(settings, "artifact_basemap_root", str(tmp_path))

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
    )
    run = await seeded_artifact_assessment.create_full_run()
    async with session_factory() as freeze_session:
        async with freeze_session.begin():
            context = await service.freeze_static_context(
                freeze_session,
                run.id,
                seeded_artifact_assessment.catalog,
            )

    assert context.selected_basemap is not None
    assert context.selected_basemap.provider == "tianditu"


async def test_non_map_run_does_not_touch_broken_basemap_packages(
    seeded_artifact_assessment,
    session_factory,
    tmp_path,
    monkeypatch,
) -> None:
    provider_dir = tmp_path / "gaode"
    provider_dir.mkdir(parents=True)
    (provider_dir / "manifest.json").write_text("{bad-json", encoding="utf-8")
    monkeypatch.setattr(settings, "artifact_basemap_root", str(tmp_path))

    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
    )
    async with session_factory() as create_session:
        async with create_session.begin():
            run = await ArtifactProductionRepository().create_run(
                create_session,
                seeded_artifact_assessment.rebuild_command("doc.background"),
            )
            context = await service.freeze_static_context(
                create_session,
                run.id,
                seeded_artifact_assessment.catalog,
            )

    assert context.selected_basemap is None
