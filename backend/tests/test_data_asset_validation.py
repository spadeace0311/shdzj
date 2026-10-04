from app.data_assets.domain import (
    AssetVersionStatus,
    NormalizedRecord,
    NormalizedTableData,
)
from app.data_assets.registry import get_asset_definition
from app.data_assets.validators import DataAssetValidator


def test_validation_rejects_negative_population() -> None:
    definition = get_asset_definition("shanghai.population.town")
    normalized = NormalizedTableData(
        columns=("ID", "NAME", "total", "resident", "floating", "family", "under14", "over65"),
        records=(
            NormalizedRecord(
                1,
                "town-1",
                {
                    "ID": "town-1",
                    "NAME": "测试街镇",
                    "total": -1,
                    "resident": 0,
                    "floating": 0,
                    "family": 0,
                    "under14": 0,
                    "over65": 0,
                },
            ),
        ),
        source_crs="EPSG:4326",
        spatial_extent=None,
    )

    report = DataAssetValidator().validate(definition, normalized)

    assert report.status is AssetVersionStatus.REJECTED
    assert report.errors[0].code == "field_nonnegative"
    assert report.errors[0].row_number == 1


def test_validation_accepts_complete_contract() -> None:
    definition = get_asset_definition("shanghai.population.town")
    normalized = NormalizedTableData(
        columns=definition.contract.business_key_fields + tuple(
            field.name for field in definition.contract.fields
        ),
        records=(
            NormalizedRecord(
                1,
                "town-1",
                {
                    "ID": "town-1",
                    "NAME": "测试街镇",
                    "total": 100,
                    "resident": 80,
                    "floating": 20,
                    "family": 30,
                    "under14": 8,
                    "over65": 12,
                },
            ),
        ),
        source_crs="EPSG:4326",
        spatial_extent=None,
    )

    report = DataAssetValidator().validate(definition, normalized)

    assert report.status is AssetVersionStatus.VALIDATED
    assert report.publishable is True
