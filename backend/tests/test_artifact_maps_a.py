from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.artifacts.basemap import MapViewportTileManifest, VIEWPORT_RADII_KM
from app.artifacts.renderers.map_layers import MapLayerRegistry, MapQualityPolicy
from app.artifacts.renderers.map_renderer import BrowserPool, MapRenderer, MapSpecBuilder
from app.artifacts.repository import ArtifactProductionRepository
from app.assessment.models import AssessmentRun, AssessmentTask
from app.config import settings
from app.db import engine
from app.loss.models import LossProduct
from tests.basemap_fixtures import make_in_memory_package, png_tile_bytes


A_CLASS_ARTIFACTS = (
    "map.intensity",
    "map.economic_loss",
    "map.rescue_demand",
    "map.deaths",
    "map.injuries",
    "map.buried",
    "map.material_demand",
    "map.gdp",
    "map.transport",
    "map.historical_earthquakes",
    "map.population",
    "map.hazard_sources",
    "map.schools",
    "map.hospitals",
    "map.active_faults",
    "map.building_damage",
    "map.key_targets",
    "map.epicenter",
    "map.city_distances",
)

LOSS_PRODUCT_TYPES = {
    "loss.buildings": "building_damage",
    "loss.population": "population_impact",
    "loss.casualties": "casualties",
    "loss.economic": "economic_loss",
    "loss.resources": "resource_demand",
}


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


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


@pytest.fixture
async def map_renderer(monkeypatch):
    package = make_in_memory_package(
        _settings_manifests(),
        provider="gaode",
        tile_bytes=png_tile_bytes(color=(70, 130, 180)),
    )
    package.checksum = "a" * 64
    monkeypatch.setattr(
        "app.artifacts.renderers.map_renderer.load_offline_basemap_candidates",
        lambda root: ({"gaode": package}, {}),
    )
    pool = BrowserPool(max_slots=1)
    await pool.start()
    yield MapRenderer(pool)
    await pool.close()


async def _create_loss_product(
    fixture,
    *,
    product_type: str,
    version: str,
    checksum: str,
) -> LossProduct:
    async with fixture._session_factory() as session:
        async with session.begin():
            run = await session.get(AssessmentRun, fixture.assessment_run_id)
            assert run is not None
            task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run.id,
                    AssessmentTask.task_key == f"loss.{product_type}",
                )
            )
            if task is None:
                task = AssessmentTask(
                    run_id=run.id,
                    task_key=f"loss.{product_type}",
                    task_type="loss",
                    component="fixture",
                    sequence=1,
                    deadline_at=run.deadline_at,
                    status="succeeded",
                )
                session.add(task)
                await session.flush()
            product = LossProduct(
                run_id=run.id,
                task_id=task.id,
                product_type=product_type,
                status="complete",
                quality_grade="L1",
                calibration_status="calibrated",
                coverage_ratio=Decimal("1.000000"),
                partial_scope=False,
                needs_review=False,
                spatialized_estimate=False,
                algorithm_version=version,
                parameter_version="parameter-v1",
                region_profile_version="region-v1",
                input_fingerprint="c" * 64,
                input_checksum="d" * 64,
                output_checksum=checksum,
                statistics={},
            )
            session.add(product)
            await session.flush()
            return product


async def _prepare_product_dependencies(fixture, artifact_key: str):
    from app.artifacts.catalog import load_catalog

    definition = load_catalog(settings.artifact_catalog_path).get(
        artifact_key,
        "a3v-professional",
    )
    product_keys = {
        dependency.key
        for dependency in definition.depends_on
        if dependency.kind.value == "assessment_product"
    }
    if "intensity.fusion" in product_keys:
        await fixture.create_fusion_product()
    for product_key in sorted(product_keys & set(LOSS_PRODUCT_TYPES)):
        await _create_loss_product(
            fixture,
            product_type=LOSS_PRODUCT_TYPES[product_key],
            version=f"{product_key}-v1",
            checksum="e" * 64,
        )

    run = await fixture.create_full_run()
    task = await fixture.first_task(run.id, artifact_key)
    async with fixture._session_factory() as session:
        async with session.begin():
            await ArtifactProductionRepository().prepare_dependencies(
                session,
                run.id,
            )
    return task


@pytest.mark.parametrize("artifact_key", A_CLASS_ARTIFACTS)
async def test_a_class_artifacts_are_rendered_with_required_layers(
    artifact_key,
    seeded_artifact_assessment,
    map_renderer,
    tmp_path,
) -> None:
    context = await seeded_artifact_assessment.map_context(artifact_key)
    layers = MapLayerRegistry.build(artifact_key, context)
    quality = MapQualityPolicy.evaluate(artifact_key, layers)
    spec = MapSpecBuilder().build(context)
    result = await map_renderer.render(spec, tmp_path / f"{artifact_key}.jpg")

    assert layers
    assert quality.grade == "A"
    assert quality.needs_review is False
    assert result.width == 4761
    assert result.height == 3369
    assert result.quality.grade == "A"
    assert result.quality.needs_review is False
    assert result.render_manifest["base_provider"] in {"gaode", "tianditu"}


@pytest.mark.parametrize(
    "artifact_key,product_key",
    (
        ("map.intensity", "intensity.fusion"),
        ("map.economic_loss", "loss.economic"),
        ("map.rescue_demand", "loss.resources"),
        ("map.deaths", "loss.casualties"),
        ("map.injuries", "loss.casualties"),
        ("map.buried", "loss.casualties"),
        ("map.material_demand", "loss.resources"),
        ("map.building_damage", "loss.buildings"),
    ),
)
async def test_a_class_product_dependencies_are_persisted(
    artifact_key,
    product_key,
    seeded_artifact_assessment,
) -> None:
    task = await _prepare_product_dependencies(
        seeded_artifact_assessment,
        artifact_key,
    )
    bindings = await seeded_artifact_assessment.bindings_for(task.id)

    binding = next(
        item
        for item in bindings
        if item.dependency_kind == "assessment_product"
        and item.dependency_key == product_key
    )
    assert binding.resolution_status == "bound"
    assert binding.bound_entity_id is not None
    assert binding.bound_checksum is not None
