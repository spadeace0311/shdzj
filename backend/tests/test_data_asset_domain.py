import pytest

from app.data_assets.domain import (
    AssetDataType,
    AssetVersionStatus,
    ImportJobStatus,
    SnapshotRole,
    SourceFormat,
    validate_source_uri,
)
from app.data_assets.registry import FIRST_PARTY_ASSETS, get_asset_definition


def test_data_asset_enums_have_stable_external_values() -> None:
    assert AssetDataType.VECTOR == "vector"
    assert AssetVersionStatus.IMPORTED == "imported"
    assert AssetVersionStatus.PUBLISHED == "published"
    assert ImportJobStatus.QUEUED == "queued"
    assert SnapshotRole.REQUIRED == "required"
    assert SourceFormat.MDB == "mdb"


def test_first_party_catalog_has_required_contracts() -> None:
    keys = {definition.asset_key for definition in FIRST_PARTY_ASSETS}
    assert keys == {
        "shanghai.admin.city",
        "shanghai.admin.county",
        "shanghai.admin.town",
        "shanghai.population.city",
        "shanghai.population.county",
        "shanghai.population.town",
        "shanghai.building.city",
        "shanghai.building.county",
        "shanghai.building.town",
        "shanghai.economy.county",
        "shanghai.fault",
        "shanghai.gdp.raster",
        "shanghai.dem.raster",
        "shanghai.loss.parameters",
    }
    town = get_asset_definition("shanghai.admin.town")
    assert town.contract.business_key_fields == ("ID",)
    assert town.contract.geometry_type == "MULTIPOLYGON"
    assert town.source_table == "TOWN_CODE"
    assert (
        get_asset_definition("shanghai.population.town").contract.aggregate_of
        == "shanghai.population.county"
    )
    assert (
        get_asset_definition("shanghai.population.county").contract.aggregate_of
        == "shanghai.population.city"
    )
    assert (
        get_asset_definition("shanghai.building.town").contract.aggregate_of
        == "shanghai.building.county"
    )
    assert (
        get_asset_definition("shanghai.building.county").contract.aggregate_of
        == "shanghai.building.city"
    )
    assert (
        get_asset_definition("shanghai.economy.county").contract.aggregate_of
        is None
    )
    assert get_asset_definition("shanghai.population.city").source_table == (
        "CITY_POPULATION"
    )
    assert get_asset_definition("shanghai.population.county").source_table == (
        "COUNTY_POPULATION"
    )
    assert get_asset_definition("shanghai.building.city").source_table == (
        "CITY_BUILDING"
    )
    assert get_asset_definition("shanghai.building.county").source_table == (
        "COUNTY_BUILDING"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://example.gov.invalid/town.geojson",
        "http://example.gov.invalid/data",
    ],
)
def test_source_uri_accepts_absolute_http_urls(value: str) -> None:
    assert validate_source_uri(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "town.geojson",
        "file:///D:/data/town.geojson",
        "https://user:secret@example.gov.invalid/data",
        "https:///missing-host",
    ],
)
def test_source_uri_rejects_local_or_credentialed_values(value: str) -> None:
    with pytest.raises(ValueError, match="source_uri"):
        validate_source_uri(value)
