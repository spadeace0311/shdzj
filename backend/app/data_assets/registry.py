from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.data_assets.domain import (
    AggregateFieldTolerance,
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


def _aggregate_tolerance(
    field_name: str,
    *,
    warning_threshold: float = 0.001,
    error_threshold: float,
    basis: str,
) -> AggregateFieldTolerance:
    return AggregateFieldTolerance(
        field_name=field_name,
        warning_threshold=warning_threshold,
        error_threshold=error_threshold,
        basis=basis,
    )


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
        "shanghai.population.city",
        "shanghai",
        "上海市人口（市级）",
        AssetDataType.TABLE,
        "city",
        "信息中心",
        365,
        False,
        AssetContract(
            ("ID",),
            (
                _field("ID", "string"),
                _field("TOTAL", "number", nonnegative=True),
                _field("RESIDENT", "number", nonnegative=True),
                _field("FAMILY", "number", nonnegative=True),
                _field("OVER65", "number", nonnegative=True),
                _field("UNDER14", "number", nonnegative=True),
            ),
            expected_record_count=1,
        ),
        "CITY_POPULATION",
    ),
    DataAssetDefinition(
        "shanghai.population.county",
        "shanghai",
        "上海市人口（区县级）",
        AssetDataType.TABLE,
        "county",
        "信息中心",
        365,
        False,
        AssetContract(
            ("ID",),
            (
                _field("ID", "string"),
                _field("TOTAL", "number", nonnegative=True),
                _field("RESIDENT", "number", nonnegative=True),
                _field("FAMILY", "number", nonnegative=True),
                _field("OVER65", "number", nonnegative=True),
                _field("UNDER14", "number", nonnegative=True),
            ),
            expected_record_count=17,
            aggregate_of="shanghai.population.city",
            aggregate_tolerances=(
                _aggregate_tolerance(
                    "FAMILY",
                    error_threshold=0.04,
                    basis=(
                        "2022 MDB city/county measured 3.501419%; "
                        "policy ceiling rounded to 4%"
                    ),
                ),
            ),
        ),
        "COUNTY_POPULATION",
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
            aggregate_of="shanghai.population.county",
        ),
        "TOWN_POPULATION",
    ),
    DataAssetDefinition(
        "shanghai.building.city",
        "shanghai",
        "上海市房屋（市级）",
        AssetDataType.TABLE,
        "city",
        "信息中心",
        365,
        False,
        AssetContract(
            ("id",),
            (
                _field("id", "string"),
                _field("TOTAL_AREA", "number", nonnegative=True),
                _field(
                    "HIGH_RISE",
                    "number",
                    required=False,
                    nonnegative=True,
                ),
                _field("RCFRAME", "number", nonnegative=True),
                _field("BRICK_STRUCTURE", "number", nonnegative=True),
                _field("SINGLE_AREA", "number", nonnegative=True),
                _field("OTHER_STRUCTURE", "number", nonnegative=True),
            ),
            expected_record_count=1,
        ),
        "CITY_BUILDING",
    ),
    DataAssetDefinition(
        "shanghai.building.county",
        "shanghai",
        "上海市房屋（区县级）",
        AssetDataType.TABLE,
        "county",
        "信息中心",
        365,
        False,
        AssetContract(
            ("id",),
            (
                _field("id", "string"),
                _field("TOTAL_AREA", "number", nonnegative=True),
                _field(
                    "HIGH_RISE",
                    "number",
                    required=False,
                    nonnegative=True,
                ),
                _field("RCFRAME", "number", nonnegative=True),
                _field("BRICK_STRUCTURE", "number", nonnegative=True),
                _field("SINGLE_AREA", "number", nonnegative=True),
                _field("OTHER_STRUCTURE", "number", nonnegative=True),
            ),
            expected_record_count=17,
            aggregate_of="shanghai.building.city",
        ),
        "COUNTY_BUILDING",
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
                _field(
                    "HIGH_RISE",
                    "number",
                    required=False,
                    nonnegative=True,
                ),
                _field("RCFRAME", "number", nonnegative=True),
                _field("BRICK_STRUCTURE", "number", nonnegative=True),
                _field("SINGLE_AREA", "number", nonnegative=True),
                _field("OTHER_STRUCTURE", "number", nonnegative=True),
            ),
            expected_record_count=212,
            aggregate_of="shanghai.building.county",
            aggregate_tolerances=(
                _aggregate_tolerance(
                    "TOTAL_AREA",
                    error_threshold=0.02,
                    basis=(
                        "2022 MDB county/town measured 1.540624%; "
                        "policy ceiling rounded to 2%"
                    ),
                ),
                _aggregate_tolerance(
                    "RCFRAME",
                    error_threshold=0.02,
                    basis=(
                        "2022 MDB county/town measured 1.609432%; "
                        "policy ceiling rounded to 2%"
                    ),
                ),
                _aggregate_tolerance(
                    "BRICK_STRUCTURE",
                    error_threshold=0.01,
                    basis=(
                        "2022 MDB county/town measured 0.821258%; "
                        "policy ceiling rounded to 1%"
                    ),
                ),
                _aggregate_tolerance(
                    "SINGLE_AREA",
                    error_threshold=0.03,
                    basis=(
                        "2022 MDB county/town measured 2.162433%; "
                        "policy ceiling rounded to 3%"
                    ),
                ),
                _aggregate_tolerance(
                    "OTHER_STRUCTURE",
                    error_threshold=0.05,
                    basis=(
                        "2022 MDB county/town measured 4.657517%; "
                        "policy ceiling rounded to 5%"
                    ),
                ),
            ),
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
            aggregate_of=None,
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


@dataclass(frozen=True, slots=True)
class ArtifactAssetContract:
    asset_key: str
    data_type: str
    geometry_type: str | None
    required_fields: tuple[str, ...]
    crs: str
    artifact_usage: tuple[str, ...]
    classification: str


def load_artifact_asset_contracts(
    path: str | Path,
) -> tuple[ArtifactAssetContract, ...]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("artifact asset contracts must be a list")

    contracts: list[ArtifactAssetContract] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"artifact asset contract at index {index} must be a mapping")
        asset_key = _required_string(item, "asset_key", index)
        data_type = _required_string(item, "data_type", index)
        geometry_type = item.get("geometry_type")
        if geometry_type is not None and not isinstance(geometry_type, str):
            raise ValueError(
                f"artifact asset contract {asset_key} geometry_type must be a string or null"
            )
        crs = _required_string(item, "crs", index)
        classification = _required_string(item, "classification", index)
        required_fields = _string_tuple(
            item.get("required_fields"),
            f"artifact asset contract {asset_key}.required_fields",
            allow_empty=True,
        )
        artifact_usage = _string_tuple(
            item.get("artifact_usage"),
            f"artifact asset contract {asset_key}.artifact_usage",
            allow_empty=False,
        )
        contracts.append(
            ArtifactAssetContract(
                asset_key=asset_key,
                data_type=data_type,
                geometry_type=geometry_type,
                required_fields=required_fields,
                crs=crs,
                artifact_usage=artifact_usage,
                classification=classification,
            )
        )

    keys = [contract.asset_key for contract in contracts]
    if len(keys) != len(set(keys)):
        raise ValueError("artifact asset contract keys must not overlap")
    return tuple(contracts)


def get_artifact_asset_contracts(
    path: str | Path | None = None,
) -> tuple[ArtifactAssetContract, ...]:
    if path is None:
        from app.config import settings

        path = settings.artifact_asset_catalog_path
    return load_artifact_asset_contracts(path)


def registered_artifact_asset_keys(
    path: str | Path | None = None,
) -> tuple[str, ...]:
    return tuple(
        contract.asset_key
        for contract in get_artifact_asset_contracts(path)
    )


def get_artifact_asset_keys(
    path: str | Path | None = None,
) -> tuple[str, ...]:
    return registered_artifact_asset_keys(path)


def _required_string(item: dict[str, Any], field: str, index: int) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"artifact asset contract at index {index} requires {field}")
    return value


def _string_tuple(
    value: Any,
    label: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if value is None:
        value = []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list")
    values = tuple(str(item) for item in value)
    if not allow_empty and not values:
        raise ValueError(f"{label} must not be empty")
    if any(not item.strip() for item in values):
        raise ValueError(f"{label} must contain non-empty strings")
    return values
