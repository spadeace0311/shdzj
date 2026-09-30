from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from app.artifacts.basemap import MapViewportTileManifest, VIEWPORT_RADII_KM
from app.artifacts.context import MapRenderContext, ProductionContextService
from app.artifacts.repository import ArtifactProductionRepository
from app.config import settings
from app.db import engine
from app.artifacts.renderers.map_renderer import (
    BrowserPool,
    MapLayer,
    MapRenderer,
    MapSpecBuilder,
    RemoteAssetForbiddenError,
)
from tests.basemap_fixtures import (
    make_in_memory_package,
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
    manifests = _settings_manifests()
    service = ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(manifests, provider="gaode"),
            "tianditu": make_in_memory_package(manifests, provider="tianditu"),
        },
    )
    run = await seeded_artifact_assessment.create_full_run()
    task = await seeded_artifact_assessment.first_task(run.id, "map.epicenter")

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
