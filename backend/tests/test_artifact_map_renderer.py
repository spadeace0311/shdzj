from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from app.artifacts.basemap import (
    MapViewportTileManifest,
    VIEWPORT_RADII_KM,
    load_offline_basemap_packages,
)
from app.artifacts.context import MapRenderContext, ProductionContextService
from app.artifacts.repository import ArtifactProductionRepository
from app.config import settings
from app.db import engine
from app.data_assets.domain import NormalizedRecord, NormalizedTableData
from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
from app.data_assets.registry import get_asset_definition
from app.data_assets.service import DataAssetService
from app.artifacts.renderers.map_renderer import (
    BrowserPool,
    MapLayer,
    MapRenderer,
    MapSpecBuilder,
    RemoteAssetForbiddenError,
    _validate_spec,
)
from tests.basemap_fixtures import (
    png_tile_bytes,
    union_bounds_3857,
    write_basemap_package,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def browser_pool() -> BrowserPool:
    pool = BrowserPool(max_slots=1)
    await pool.start()
    yield pool
    await pool.close()


@pytest.fixture
def map_renderer(browser_pool: BrowserPool) -> MapRenderer:
    return MapRenderer(browser_pool)


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


def _write_real_basemap_package(root: Path) -> None:
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
        provider="gaode",
        package_format="mbtiles",
        tiles=tiles,
        zoom_levels=tuple(sorted({tile.z for tile in tiles})),
        coverage_bounds=union_bounds_3857(tiles),
        tile_bytes=png_tile_bytes(color=(70, 130, 180)),
        generated_at=datetime.now(UTC),
        tile_scheme="tms",
    )


async def _publish_admin_city(session_factory) -> None:
    definition = get_asset_definition("shanghai.admin.city")
    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key=definition.asset_key,
                    version=f"admin-city-{uuid4()}",
                    source_uri="https://example.gov.invalid/admin-city",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="map renderer admin city fixture",
                    file_name="admin-city",
                    file_format="geojson",
                    file_size_bytes=1,
                    checksum="a" * 64,
                    relative_path="fixture/admin-city",
                    requested_by="map-renderer-fixture",
                ),
            )
            normalized = NormalizedTableData(
                columns=("ID", "NAME"),
                records=(
                    NormalizedRecord(
                        row_number=1,
                        business_key="shanghai",
                        properties={"ID": "shanghai", "NAME": "Shanghai"},
                        geometry_wkt=(
                            "MULTIPOLYGON (((121.2 30.9, 121.8 30.9, "
                            "121.8 31.5, 121.2 31.5, 121.2 30.9)))"
                        ),
                    ),
                ),
                source_crs="EPSG:4326",
                spatial_extent=(121.2, 30.9, 121.8, 31.5),
            )
            await DataAssetService().populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {
                    "importer": "geojson",
                    "record_count": normalized.record_count,
                    "source_crs": normalized.source_crs,
                },
            )
            service = DataAssetService()
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor="map-renderer-fixture",
            )
            if not report.publishable:
                raise AssertionError(
                    f"admin city fixture failed validation: "
                    f"{[issue.message for issue in report.errors]}"
                )
            await service.publish_version(
                session,
                job.asset_version_id,
                "map-renderer-fixture",
                "map renderer admin city fixture",
            )


def _pixel_fraction(path: Path, predicate) -> float:
    with Image.open(path).convert("RGB") as image:
        pixels = list(image.getdata())
    if not pixels:
        return 0.0
    return sum(1 for pixel in pixels if predicate(pixel)) / len(pixels)


def _is_blue_basemap(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return (
        abs(red - 70) < 35
        and abs(green - 130) < 35
        and abs(blue - 180) < 35
    )


def _is_red_epicenter(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red > 140 and green < 90 and blue < 90


async def test_epicenter_map_renders_a3v_professional(
    seeded_artifact_assessment,
    map_renderer,
    tmp_path: Path,
    monkeypatch,
) -> None:
    basemap_root = tmp_path / "basemaps"
    _write_real_basemap_package(basemap_root)
    monkeypatch.setattr(settings, "artifact_basemap_root", str(basemap_root))
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = MapSpecBuilder().build(context)
    actual_package = load_offline_basemap_packages(basemap_root)["gaode"]
    spec = replace(
        spec,
        selected_basemap={
            **spec.selected_basemap,
            "checksum": actual_package.checksum,
        },
    )
    result = await map_renderer.render(spec, tmp_path / "epicenter.jpg")

    assert spec.title == "震中位置分布图"
    assert spec.output.width == 4761
    assert spec.output.height == 3369
    assert result.width == 4761
    assert result.height == 3369
    assert result.dpi == 300
    assert result.non_empty_ratio > 0.2
    assert Image.open(result.path).format == "JPEG"
    assert _pixel_fraction(result.path, _is_blue_basemap) > 0.2
    assert _pixel_fraction(result.path, _is_red_epicenter) > 0.0001


async def test_formal_context_renders_real_offline_basemap_and_epicenter(
    seeded_artifact_assessment,
    session_factory,
    map_renderer,
    tmp_path: Path,
    monkeypatch,
) -> None:
    basemap_root = tmp_path / "basemaps"
    _write_real_basemap_package(basemap_root)
    monkeypatch.setattr(settings, "artifact_basemap_root", str(basemap_root))
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages=load_offline_basemap_packages(basemap_root),
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.epicenter")
    await _publish_admin_city(session_factory)

    async with session_factory() as session:
        async with session.begin():
            await service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )
            context = await service.build_map_context(session, task.id)

    assert isinstance(context, MapRenderContext)
    spec = MapSpecBuilder().build(context)
    result = await map_renderer.render(spec, tmp_path / "formal-epicenter.jpg")

    assert result.width == 4761
    assert result.height == 3369
    assert result.render_manifest["base_provider"] == "gaode"
    assert result.render_manifest["base_package_checksum"] == (
        context.selected_basemap.checksum
    )
    assert _pixel_fraction(result.path, _is_blue_basemap) > 0.2
    assert _pixel_fraction(result.path, _is_red_epicenter) > 0.0001


async def test_renderer_never_loads_remote_assets(
    map_renderer,
    seeded_artifact_assessment,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.map_context(
        "map.epicenter",
        base_style="gaode-local-v1",
    )
    spec = replace(
        MapSpecBuilder().build(context),
        layers=(MapLayer(url="https://example.invalid/tile.png"),),
    )
    with pytest.raises(RemoteAssetForbiddenError):
        await map_renderer.render(spec, tmp_path / "rejected.jpg")


async def test_renderer_rejects_nested_remote_source_url(
    map_renderer,
    seeded_artifact_assessment,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = replace(
        MapSpecBuilder().build(context),
        layers=(
            MapLayer(
                id="remote-source",
                url="local://artifact_maps/epicenter.geojson",
                source={
                    "type": "vector",
                    "url": "https://example.invalid/tile.json",
                },
            ),
        ),
    )

    with pytest.raises(RemoteAssetForbiddenError):
        await map_renderer.render(spec, tmp_path / "rejected-source.jpg")


async def test_spec_rejects_remote_geojson_data(
    seeded_artifact_assessment,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = replace(
        MapSpecBuilder().build(context),
        layers=(
            MapLayer(
                id="remote-geojson",
                url="local://artifact_maps/epicenter.geojson",
                source={
                    "type": "geojson",
                    "data": "https://example.invalid/remote.geojson",
                },
            ),
        ),
    )

    with pytest.raises(RemoteAssetForbiddenError):
        _validate_spec(spec)


async def test_spec_rejects_nested_remote_url_in_geojson_feature(
    seeded_artifact_assessment,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = replace(
        MapSpecBuilder().build(context),
        layers=(
            MapLayer(
                id="nested-remote-geojson",
                url="local://artifact_maps/epicenter.geojson",
                source={
                    "type": "geojson",
                    "data": {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {
                                    "url": "https://example.invalid/nested"
                                },
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [121.5, 31.2],
                                },
                            }
                        ],
                    },
                },
            ),
        ),
    )

    with pytest.raises(RemoteAssetForbiddenError):
        _validate_spec(spec)


async def test_spec_accepts_inline_geojson_feature_collection(
    seeded_artifact_assessment,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = replace(
        MapSpecBuilder().build(context),
        layers=(
            MapLayer(
                id="inline-geojson",
                url="local://artifact_maps/epicenter.geojson",
                source={
                    "type": "geojson",
                    "data": {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"kind": "epicenter"},
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [121.5, 31.2],
                                },
                            }
                        ],
                    },
                },
            ),
        ),
    )

    _validate_spec(spec)


async def test_renderer_rejects_remote_url_inside_sprite_objects(
    map_renderer,
    seeded_artifact_assessment,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = replace(
        MapSpecBuilder().build(context),
        layers=(
            MapLayer(
                id="remote-sprite",
                url="local://artifact_maps/epicenter.geojson",
                style={
                    "sprite": [
                        {
                            "id": "basic",
                            "url": "https://example.invalid/sprite.json",
                        }
                    ]
                },
            ),
        ),
    )

    with pytest.raises(RemoteAssetForbiddenError):
        await map_renderer.render(spec, tmp_path / "rejected-sprite.jpg")


async def test_selected_basemap_checksum_mismatch_fails_hard(
    map_renderer,
    seeded_artifact_assessment,
    tmp_path: Path,
    monkeypatch,
) -> None:
    basemap_root = tmp_path / "basemaps"
    _write_real_basemap_package(basemap_root)
    monkeypatch.setattr(settings, "artifact_basemap_root", str(basemap_root))
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = MapSpecBuilder().build(context)
    spec = replace(
        spec,
        selected_basemap={
            **spec.selected_basemap,
            "checksum": "0" * 64,
        },
    )

    with pytest.raises(FileNotFoundError):
        await map_renderer.render(spec, tmp_path / "wrong-checksum.jpg")


async def test_missing_selected_basemap_fails_hard(
    map_renderer,
    seeded_artifact_assessment,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "artifact_basemap_root", str(tmp_path / "empty"))
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = MapSpecBuilder().build(context)

    with pytest.raises(FileNotFoundError):
        await map_renderer.render(spec, tmp_path / "missing-basemap.jpg")


async def test_cancel_closes_page_and_releases_browser_slot(
    browser_pool,
    seeded_artifact_assessment,
) -> None:
    await seeded_artifact_assessment.map_context("map.epicenter")
    slot = await browser_pool.acquire_page(priority=100)
    assert browser_pool.active_pages == 1

    await browser_pool.cancel(slot.token)
    assert browser_pool.active_pages == 0
    assert browser_pool.available_slots == browser_pool.max_slots
