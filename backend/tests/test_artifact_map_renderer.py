from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.db import engine
from app.artifacts.renderers.map_renderer import (
    BrowserPool,
    MapLayer,
    MapRenderer,
    MapSpecBuilder,
    RemoteAssetForbiddenError,
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


async def test_epicenter_map_renders_a3v_professional(
    seeded_artifact_assessment,
    map_renderer,
    tmp_path: Path,
) -> None:
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


async def test_cancel_closes_page_and_releases_browser_slot(
    browser_pool,
    seeded_artifact_assessment,
) -> None:
    await seeded_artifact_assessment.map_context("map.epicenter")
    token = await browser_pool.reserve_slot(priority=100)
    await browser_pool.cancel(token)

    assert browser_pool.active_pages == 0
    assert browser_pool.available_slots == browser_pool.max_slots
