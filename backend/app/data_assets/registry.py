from app.data_assets.domain import (
    AssetContract,
    AssetDataType,
    AssetFieldContract,
    DataAssetDefinition,
)


def _field(name: str, python_type: str, *, required: bool = True, nonnegative: bool = False):
    return AssetFieldContract(
        name=name,
        python_type=python_type,
        required=required,
        nonnegative=nonnegative,
    )


# The numeric town/county tables currently have no published coarser numeric
# parent in the first-party catalog. Their aggregate source points at the same
# asset so validation measures drift against the previously published version.
FIRST_PARTY_ASSETS = (
    DataAssetDefinition(
        "shanghai.admin.city",
        "shanghai",
        "上海市行政边界",
        AssetDataType.VECTOR,
        "city",
        "信息中心",
        365,
        True,
        AssetContract(("ID",), (_field("ID", "string"), _field("NAME", "string")), "MULTIPOLYGON"),
        "city_code",
    ),
    DataAssetDefinition(
        "shanghai.admin.county",
        "shanghai",
        "上海市区县边界",
        AssetDataType.VECTOR,
        "county",
        "信息中心",
        365,
        True,
        AssetContract(("ID",), (_field("ID", "string"), _field("NAME", "string")), "MULTIPOLYGON"),
        "county_code",
    ),
    DataAssetDefinition(
        "shanghai.admin.town",
        "shanghai",
        "上海市街镇边界",
        AssetDataType.VECTOR,
        "town",
        "信息中心",
        365,
        True,
        AssetContract(("ID",), (_field("ID", "string"), _field("NAME", "string")), "MULTIPOLYGON"),
        "TOWN_CODE",
    ),
    DataAssetDefinition(
        "shanghai.population.town",
        "shanghai",
        "上海市街镇人口",
        AssetDataType.TABLE,
        "town",
        "信息中心",
        365,
        True,
        AssetContract(
            ("ID",),
            (
                _field("ID", "string"),
                _field("NAME", "string"),
                _field("total", "number", nonnegative=True),
                _field("resident", "number", nonnegative=True),
                _field("floating", "number", nonnegative=True),
                _field("family", "number", nonnegative=True),
                _field("under14", "number", nonnegative=True),
                _field("over65", "number", nonnegative=True),
            ),
            expected_record_count=212,
            excluded_business_keys=("31012000000000",),
            exclusion_reason="district aggregate row without matching TOWN_CODE",
            aggregate_of="shanghai.population.town",
        ),
        "TOWN_POPULATION",
    ),
    DataAssetDefinition(
        "shanghai.building.town",
        "shanghai",
        "上海市街镇房屋",
        AssetDataType.TABLE,
        "town",
        "信息中心",
        365,
        True,
        AssetContract(
            ("id",),
            (
                _field("id", "string"),
                _field("name", "string"),
                _field("TOTAL_AREA", "number", nonnegative=True),
                _field("HIGH_RISE", "number", nonnegative=True),
                _field("RCFRAME", "number", nonnegative=True),
                _field("BRICK_STRUCTURE", "number", nonnegative=True),
                _field("SINGLE_AREA", "number", nonnegative=True),
                _field("OTHER_STRUCTURE", "number", nonnegative=True),
            ),
            expected_record_count=212,
            aggregate_of="shanghai.building.town",
        ),
        "TOWN_BUILDING",
    ),
    DataAssetDefinition(
        "shanghai.economy.county",
        "shanghai",
        "上海市区县经济",
        AssetDataType.TABLE,
        "county",
        "信息中心",
        365,
        True,
        AssetContract(
            ("id",),
            (
                _field("id", "string"),
                _field("name", "string"),
                _field("gdp", "number", nonnegative=True),
                _field("industry_value", "number", nonnegative=True),
                _field("agri_value", "number", nonnegative=True),
                _field("service_value", "number", nonnegative=True),
                _field("income", "number", nonnegative=True),
            ),
            aggregate_of="shanghai.economy.county",
        ),
        "economy",
    ),
    DataAssetDefinition(
        "shanghai.fault",
        "shanghai",
        "上海市活动断层",
        AssetDataType.VECTOR,
        "feature",
        "信息中心",
        1095,
        False,
        AssetContract(
            ("OBJECTID",),
            (
                _field("OBJECTID", "integer"),
                _field("name", "string"),
                _field("strike", "number", required=False),
                _field("DIP_ANGLE", "number", required=False),
                _field("DIP_DIR", "number", required=False),
                _field("LENGTH", "number", required=False, nonnegative=True),
                _field("WIDTH", "number", required=False, nonnegative=True),
            ),
            "MULTILINESTRING",
        ),
        "ACTIVEFAULT",
    ),
    DataAssetDefinition(
        "shanghai.gdp.raster",
        "shanghai",
        "上海市 GDP 栅格",
        AssetDataType.RASTER,
        "raster",
        "信息中心",
        365,
        False,
        AssetContract((), (), None),
        None,
    ),
    DataAssetDefinition(
        "shanghai.dem.raster",
        "shanghai",
        "上海市 DEM 栅格",
        AssetDataType.RASTER,
        "raster",
        "信息中心",
        365,
        False,
        AssetContract((), (), None),
        None,
    ),
    DataAssetDefinition(
        "shanghai.loss.parameters",
        "shanghai",
        "上海市损失参数包",
        AssetDataType.PARAMETER,
        "region",
        "信息中心",
        365,
        True,
        AssetContract(("parameter_set_id",), (_field("parameter_set_id", "string"),)),
        None,
    ),
)

_BY_KEY = {item.asset_key: item for item in FIRST_PARTY_ASSETS}


def get_asset_definition(asset_key: str) -> DataAssetDefinition:
    try:
        return _BY_KEY[asset_key]
    except KeyError as exc:
        raise KeyError(f"unknown data asset: {asset_key}") from exc
