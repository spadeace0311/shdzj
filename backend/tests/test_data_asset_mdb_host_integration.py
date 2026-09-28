from pathlib import Path

import pytest

from app.data_assets.mdb_importer import MdbAssetImporter
from app.data_assets.registry import get_asset_definition


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
