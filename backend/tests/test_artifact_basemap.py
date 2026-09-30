from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from math import asinh, pi, radians, tan
from typing import Callable

import pytest

from app.artifacts.basemap import (
    AllBasemapsUnavailableError,
    BasemapSelector,
    OfflineBasemapPackage,
    OfflineBasemapValidator,
    MapViewportTileManifest,
    TileKey,
    load_offline_basemap_packages,
)
from tests.basemap_fixtures import (
    make_in_memory_package,
    png_tile_bytes,
    write_basemap_package,
)


def _png_bytes() -> bytes:
    return png_tile_bytes()


def _make_package(
    manifest: MapViewportTileManifest,
    *,
    provider: str = "gaode",
    generated_at: datetime | None = None,
) -> OfflineBasemapPackage:
    tiles = manifest.tiles
    files = {tile: _png_bytes() for tile in tiles}
    package = OfflineBasemapPackage(
        provider=provider,
        package_id=f"{provider}-offline-v1",
        version="v1",
        checksum="0" * 64,
        generated_at=generated_at or datetime(2026, 9, 29, tzinfo=UTC),
        max_age_days=30,
        coverage_bounds=manifest.union_bounds_3857(),
        zoom_levels=manifest.zoom_levels,
        tiles=tiles,
        files=files,
        format="png",
        source_statement=f"{provider} offline basemap fixture",
    )
    return package.with_recomputed_checksum()


@pytest.fixture
def tile_manifest_factory() -> Callable[..., MapViewportTileManifest]:
    def factory(
        *,
        center: tuple[float, float] = (121.5, 31.2),
        radius_km: float = 50.0,
        output_width: int = 4761,
        output_height: int = 3369,
        zoom_levels: tuple[int, ...] = (9, 10),
        padding: int = 256,
    ) -> MapViewportTileManifest:
        return MapViewportTileManifest.build(
            center_lon=center[0],
            center_lat=center[1],
            radius_km=radius_km,
            output_width=output_width,
            output_height=output_height,
            zoom_levels=zoom_levels,
            padding=padding,
        )

    return factory


@pytest.fixture
def tile_manifest(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
) -> MapViewportTileManifest:
    return tile_manifest_factory()


@pytest.fixture
def offline_basemap_package(
    tile_manifest: MapViewportTileManifest,
) -> OfflineBasemapPackage:
    return _make_package(tile_manifest)


def test_manifest_contains_exact_required_xyz_without_duplicates(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
) -> None:
    manifest = tile_manifest_factory(center=(121.5, 31.2), zoom_levels=(9, 10))

    keys = [(tile.z, tile.x, tile.y) for tile in manifest.tiles]
    assert len(keys) == len(set(keys))
    assert manifest.required_count == len(keys)
    assert (-1, 0, 10) not in keys


def test_one_missing_exact_tile_fails_package_even_if_200_samples_pass(
    offline_basemap_package: OfflineBasemapPackage,
    tile_manifest: MapViewportTileManifest,
) -> None:
    offline_basemap_package.remove_tile(tile_manifest.tiles[137])

    result = OfflineBasemapValidator().validate_package(
        offline_basemap_package,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.valid is False
    assert result.error_category == "required_tile_missing"
    assert result.missing_tiles == (
        (tile_manifest.tiles[137].z, tile_manifest.tiles[137].x, tile_manifest.tiles[137].y),
    )


def test_selector_uses_tianditu_only_after_gaode_fails(
    offline_basemap_package: OfflineBasemapPackage,
    tile_manifest: MapViewportTileManifest,
) -> None:
    gaode = offline_basemap_package.with_missing_tile(tile_manifest.tiles[0])
    tianditu = offline_basemap_package.valid_copy()

    selected = BasemapSelector().select(
        gaode,
        tianditu,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert selected.provider == "tianditu"
    assert selected.package_id == tianditu.package_id


def test_expired_package_is_rejected_without_internet_fallback(
    offline_basemap_package: OfflineBasemapPackage,
    tile_manifest: MapViewportTileManifest,
) -> None:
    with pytest.raises(AllBasemapsUnavailableError):
        BasemapSelector().select(
            offline_basemap_package.expired(),
            offline_basemap_package.expired(),
            [tile_manifest],
            observed_at=datetime(2026, 9, 30, tzinfo=UTC),
        )


def test_manifest_tiles_are_sorted_and_deduplicated(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
) -> None:
    manifest = tile_manifest_factory(zoom_levels=(10, 9, 10))

    assert manifest.zoom_levels == (9, 10)
    assert manifest.tiles == tuple(sorted(manifest.tiles))
    assert len(manifest.tiles) == len(set(manifest.tiles))


@pytest.mark.parametrize(
    "radius_km",
    (2.5, 5.0, 10.0, 30.0, 50.0),
)
def test_five_required_viewport_variants_are_supported(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
    radius_km: float,
) -> None:
    manifest = tile_manifest_factory(radius_km=radius_km, zoom_levels=(9, 10, 11, 12))

    assert manifest.radius_km == radius_km
    assert manifest.required_count > 0
    assert all(0 <= tile.x < 2**tile.z for tile in manifest.tiles)
    assert all(0 <= tile.y < 2**tile.z for tile in manifest.tiles)


def test_tile_index_uses_epsg3857_web_mercator_center(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
) -> None:
    manifest = tile_manifest_factory(
        center=(121.5, 31.2),
        radius_km=0.1,
        output_width=256,
        output_height=256,
        zoom_levels=(12,),
        padding=0,
    )

    expected_x = int((121.5 + 180.0) / 360.0 * 2**12)
    latitude_radians = radians(31.2)
    expected_y = int(
        (
            1.0
            - asinh(tan(latitude_radians)) / pi
        )
        / 2.0
        * 2**12
    )

    assert any(tile.z == 12 and tile.x == expected_x and tile.y == expected_y for tile in manifest.tiles)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"center_lon": 181.0},
        {"center_lat": 86.0},
        {"radius_km": 0.0},
        {"output_width": 0},
        {"output_height": -1},
        {"zoom_levels": ()},
        {"zoom_levels": (23,)},
        {"zoom_levels": (-1,)},
    ),
)
def test_manifest_rejects_out_of_range_inputs(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
    kwargs: dict[str, object],
) -> None:
    values = {
        "center_lon": 121.5,
        "center_lat": 31.2,
        "radius_km": 10.0,
        "output_width": 256,
        "output_height": 256,
        "zoom_levels": (9,),
        "padding": 256,
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        MapViewportTileManifest.build(**values)


def test_package_checksum_and_record_file_counts_are_validated(
    offline_basemap_package: OfflineBasemapPackage,
    tile_manifest: MapViewportTileManifest,
) -> None:
    validator = OfflineBasemapValidator()
    observed_at = datetime(2026, 9, 30, tzinfo=UTC)

    assert validator.validate_package(
        offline_basemap_package,
        [tile_manifest],
        observed_at=observed_at,
    ).valid

    bad_checksum = offline_basemap_package.with_checksum("0" * 64)
    assert (
        validator.validate_package(
            bad_checksum,
            [tile_manifest],
            observed_at=observed_at,
        ).error_category
        == "manifest_checksum_mismatch"
    )

    bad_counts = offline_basemap_package.with_declared_counts(
        file_count=1,
        record_count=1,
    )
    assert (
        validator.validate_package(
            bad_counts,
            [tile_manifest],
            observed_at=observed_at,
        ).error_category
        == "file_record_count_mismatch"
    )


def test_package_health_check_decodes_at_least_200_required_tiles(
    offline_basemap_package: OfflineBasemapPackage,
    tile_manifest: MapViewportTileManifest,
) -> None:
    result = OfflineBasemapValidator().validate_package(
        offline_basemap_package,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.valid is True
    assert result.sampled_decoded >= 200
    assert result.required_tiles == tuple(
        (tile.z, tile.x, tile.y) for tile in tile_manifest.tiles
    )


def test_manifest_checksum_is_lowercase_sha256(
    tile_manifest: MapViewportTileManifest,
) -> None:
    checksum = tile_manifest.manifest_checksum

    assert checksum == checksum.lower()
    assert len(checksum) == 64
    assert tile_manifest.manifest_checksum == checksum


def test_coverage_boundary_is_checked_before_tile_decode(
    offline_basemap_package: OfflineBasemapPackage,
    tile_manifest: MapViewportTileManifest,
) -> None:
    west, south, east, north = tile_manifest.union_bounds_3857()
    too_small = OfflineBasemapPackage(
        provider=offline_basemap_package.provider,
        package_id=offline_basemap_package.package_id,
        version=offline_basemap_package.version,
        generated_at=offline_basemap_package.generated_at,
        max_age_days=offline_basemap_package.max_age_days,
        coverage_bounds=(west + 1.0, south + 1.0, east - 1.0, north - 1.0),
        zoom_levels=offline_basemap_package.zoom_levels,
        tiles=offline_basemap_package.tiles,
        files=offline_basemap_package.files,
        format=offline_basemap_package.format,
        source_statement=offline_basemap_package.source_statement,
        checksum="0" * 64,
    )
    too_small = too_small.with_recomputed_checksum()

    result = OfflineBasemapValidator().validate_package(
        too_small,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.error_category == "coverage_boundary_mismatch"


def test_tile_key_rejects_invalid_indices() -> None:
    with pytest.raises(ValueError):
        TileKey(9, -1, 10)
    with pytest.raises(ValueError):
        TileKey(9, 0, 512)


def test_selector_prefers_gaode_without_switching(
    offline_basemap_package: OfflineBasemapPackage,
    tile_manifest: MapViewportTileManifest,
) -> None:
    selected = BasemapSelector().select(
        offline_basemap_package,
        offline_basemap_package.with_missing_tile(tile_manifest.tiles[0]),
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert selected.provider == "gaode"
    assert selected.selection_reason == "gaode validated"


def _load_one_package(
    root,
    provider: str,
    manifest: MapViewportTileManifest,
) -> OfflineBasemapPackage:
    packages = load_offline_basemap_packages(root)
    assert provider in packages
    return packages[provider]


def test_loads_and_validates_real_mbtiles_package(
    tmp_path,
    tile_manifest: MapViewportTileManifest,
) -> None:
    write_basemap_package(
        tmp_path,
        provider="gaode",
        package_format="mbtiles",
        tiles=tile_manifest.tiles,
        zoom_levels=tile_manifest.zoom_levels,
        coverage_bounds=tile_manifest.union_bounds_3857(),
        tile_bytes=_png_bytes(),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )

    package = _load_one_package(tmp_path, "gaode", tile_manifest)
    result = OfflineBasemapValidator().validate_package(
        package,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert package.package_format == "mbtiles"
    assert result.valid is True
    assert result.sampled_decoded >= 200


def test_loads_and_validates_real_pmtiles_package(
    tmp_path,
    tile_manifest: MapViewportTileManifest,
) -> None:
    write_basemap_package(
        tmp_path,
        provider="tianditu",
        package_format="pmtiles",
        tiles=tile_manifest.tiles,
        zoom_levels=tile_manifest.zoom_levels,
        coverage_bounds=tile_manifest.union_bounds_3857(),
        tile_bytes=_png_bytes(),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )

    package = _load_one_package(tmp_path, "tianditu", tile_manifest)
    result = OfflineBasemapValidator().validate_package(
        package,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert package.package_format == "pmtiles"
    assert result.valid is True
    assert result.sampled_decoded >= 200


def test_missing_physical_tile_in_mbtiles_is_required_tile_missing(
    tmp_path,
    tile_manifest: MapViewportTileManifest,
) -> None:
    write_basemap_package(
        tmp_path,
        provider="gaode",
        package_format="mbtiles",
        tiles=tile_manifest.tiles,
        zoom_levels=tile_manifest.zoom_levels,
        coverage_bounds=tile_manifest.union_bounds_3857(),
        tile_bytes=_png_bytes(),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    missing = tile_manifest.tiles[137]
    connection = sqlite3.connect(tmp_path / "gaode" / "gaode.mbtiles")
    try:
        connection.execute(
            "DELETE FROM tiles WHERE zoom_level = ? AND tile_column = ? "
            "AND tile_row = ?",
            (missing.z, missing.x, missing.y),
        )
        connection.commit()
    finally:
        connection.close()

    package = _load_one_package(tmp_path, "gaode", tile_manifest)
    result = OfflineBasemapValidator().validate_package(
        package,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.valid is False
    assert result.error_category == "required_tile_missing"
    assert result.missing_tiles == ((missing.z, missing.x, missing.y),)


def test_declared_count_mismatch_is_detected(
    tmp_path,
    tile_manifest: MapViewportTileManifest,
) -> None:
    write_basemap_package(
        tmp_path,
        provider="gaode",
        package_format="mbtiles",
        tiles=tile_manifest.tiles,
        zoom_levels=tile_manifest.zoom_levels,
        coverage_bounds=tile_manifest.union_bounds_3857(),
        tile_bytes=_png_bytes(),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
        record_count=len(tile_manifest.tiles) + 1,
    )

    package = _load_one_package(tmp_path, "gaode", tile_manifest)
    result = OfflineBasemapValidator().validate_package(
        package,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.valid is False
    assert result.error_category == "file_record_count_mismatch"


def test_corrupt_tile_decode_is_structured_validation_result(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
) -> None:
    manifest = tile_manifest_factory(
        center=(121.5, 31.2),
        radius_km=0.1,
        output_width=256,
        output_height=256,
        zoom_levels=(12,),
        padding=0,
    )
    package = make_in_memory_package(
        (manifest,),
        provider="gaode",
        tile_bytes=b"not-a-real-png",
    )

    result = OfflineBasemapValidator().validate_package(
        package,
        [manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.valid is False
    assert result.error_category == "tile_decode_failed"
    assert result.tile_coordinates == manifest.tiles[0].to_tuple()


def test_web_mercator_radius_uses_latitude_scale(
    tile_manifest_factory: Callable[..., MapViewportTileManifest],
) -> None:
    equator = tile_manifest_factory(
        center=(0.0, 0.0),
        radius_km=10.0,
        output_width=256,
        output_height=256,
        zoom_levels=(12,),
        padding=0,
    )
    high_latitude = tile_manifest_factory(
        center=(0.0, 80.0),
        radius_km=10.0,
        output_width=256,
        output_height=256,
        zoom_levels=(12,),
        padding=0,
    )

    assert equator.mercator_radius_m < high_latitude.mercator_radius_m
    assert high_latitude.mercator_radius_m > 10_000.0


def test_loader_rejects_provider_identity_mismatch(
    tmp_path,
    tile_manifest: MapViewportTileManifest,
) -> None:
    write_basemap_package(
        tmp_path,
        provider="gaode",
        package_format="mbtiles",
        tiles=tile_manifest.tiles,
        zoom_levels=tile_manifest.zoom_levels,
        coverage_bounds=tile_manifest.union_bounds_3857(),
        tile_bytes=_png_bytes(),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    manifest_path = tmp_path / "gaode" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["provider"] = "tianditu"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_offline_basemap_packages(tmp_path)


def test_selector_validates_both_candidates_before_selection(
    tmp_path,
    tile_manifest: MapViewportTileManifest,
) -> None:
    write_basemap_package(
        tmp_path,
        provider="gaode",
        package_format="mbtiles",
        tiles=tile_manifest.tiles,
        zoom_levels=tile_manifest.zoom_levels,
        coverage_bounds=tile_manifest.union_bounds_3857(),
        tile_bytes=_png_bytes(),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    write_basemap_package(
        tmp_path,
        provider="tianditu",
        package_format="pmtiles",
        tiles=tile_manifest.tiles,
        zoom_levels=tile_manifest.zoom_levels,
        coverage_bounds=tile_manifest.union_bounds_3857(),
        tile_bytes=_png_bytes(),
        generated_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    packages = load_offline_basemap_packages(tmp_path)

    selected = BasemapSelector().select(
        packages["gaode"],
        packages["tianditu"],
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert selected.provider == "gaode"
