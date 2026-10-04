from pathlib import Path

import pytest

from app.data_assets.mdb_importer import MdbAssetImporter
from app.data_assets.registry import get_asset_definition
from app.data_assets.service import (
    build_aggregate_checks,
    evaluate_aggregate_checks,
)
from app.data_assets.validators import DataAssetValidator


SOURCE = Path(r"D:\地震应急辅助决策系统\基础数据\上海应急基础数据2022.mdb")


@pytest.mark.windows_mdb
@pytest.mark.skipif(not SOURCE.exists(), reason="Shanghai MDB is not installed on this host")
def test_real_town_population_table_matches_contract() -> None:
    result = MdbAssetImporter().load(
        SOURCE,
        get_asset_definition("shanghai.population.town"),
    )

    assert result.record_count == 212
    assert "31012000000000" not in {
        record.business_key for record in result.records
    }
    assert all(record.business_key for record in result.records)
    assert all("total" in record.properties for record in result.records)


@pytest.mark.windows_mdb
@pytest.mark.skipif(not SOURCE.exists(), reason="Shanghai MDB is not installed on this host")
def test_real_active_fault_geometry_is_valid() -> None:
    result = MdbAssetImporter().load(
        SOURCE,
        get_asset_definition("shanghai.fault"),
    )

    assert result.record_count == 23
    assert all(record.geometry_wkt for record in result.records)
    assert all(
        record.geometry_wkt.startswith("MULTILINESTRING")
        for record in result.records
    )


@pytest.mark.windows_mdb
@pytest.mark.skipif(not SOURCE.exists(), reason="Shanghai MDB is not installed on this host")
def test_real_parent_child_tables_match_field_specific_tolerances() -> None:
    importer = MdbAssetImporter()
    city_population = importer.load(
        SOURCE,
        get_asset_definition("shanghai.population.city"),
    )
    county_population = importer.load(
        SOURCE,
        get_asset_definition("shanghai.population.county"),
    )
    city_building = importer.load(
        SOURCE,
        get_asset_definition("shanghai.building.city"),
    )
    county_building = importer.load(
        SOURCE,
        get_asset_definition("shanghai.building.county"),
    )
    town_building = importer.load(
        SOURCE,
        get_asset_definition("shanghai.building.town"),
    )

    for definition, normalized in (
        (get_asset_definition("shanghai.population.city"), city_population),
        (get_asset_definition("shanghai.population.county"), county_population),
        (get_asset_definition("shanghai.building.city"), city_building),
        (get_asset_definition("shanghai.building.county"), county_building),
        (get_asset_definition("shanghai.building.town"), town_building),
    ):
        report = DataAssetValidator().validate(definition, normalized)
        assert report.publishable, [
            (issue.code, issue.field_name, issue.row_number)
            for issue in report.errors
        ]

    population_checks = build_aggregate_checks(
        get_asset_definition("shanghai.population.county"),
        county_population,
        city_population.records,
    )
    building_checks = build_aggregate_checks(
        get_asset_definition("shanghai.building.town"),
        town_building,
        county_building.records,
    )

    assert evaluate_aggregate_checks(population_checks).errors == ()
    assert evaluate_aggregate_checks(building_checks).errors == ()
    high_rise = next(
        check
        for check in building_checks
        if check["field_name"] == "HIGH_RISE"
    )
    assert high_rise["status"] == "not_applicable"
    assert high_rise["child_total"] is None
    assert high_rise["parent_total"] is None
