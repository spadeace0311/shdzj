from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite

from shapely import wkt as shapely_wkt

from app.data_assets.domain import (
    AssetDataType,
    AssetVersionStatus,
    DataAssetDefinition,
    NormalizedAssetData,
    NormalizedRasterData,
    NormalizedTableData,
    ValidationIssue,
    ValidationReport,
)


class DataAssetValidator:
    def validate(
        self,
        definition: DataAssetDefinition,
        normalized: NormalizedAssetData,
    ) -> ValidationReport:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        self._validate_type(definition, normalized, errors)
        if isinstance(normalized, NormalizedTableData):
            self._validate_records(definition, normalized, errors, warnings)
        else:
            self._validate_raster(normalized, errors)
        status = (
            AssetVersionStatus.REJECTED
            if errors
            else AssetVersionStatus.VALIDATED
        )
        return ValidationReport(
            version_id="pending",
            status=status,
            errors=tuple(errors),
            warnings=tuple(warnings),
            statistics=self._statistics(normalized),
            checked_at=datetime.now(UTC),
        )

    @staticmethod
    def _validate_type(
        definition: DataAssetDefinition,
        normalized: NormalizedAssetData,
        errors: list[ValidationIssue],
    ) -> None:
        expects_table = definition.data_type in {
            AssetDataType.VECTOR,
            AssetDataType.TABLE,
            AssetDataType.PARAMETER,
        }
        actual_is_table = isinstance(normalized, NormalizedTableData)
        if (expects_table and not actual_is_table) or (
            not expects_table and actual_is_table
        ):
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="asset_type_mismatch",
                    message=(
                        f"asset {definition.asset_key} expects "
                        f"{definition.data_type.value} data"
                    ),
                )
            )

    @staticmethod
    def _validate_records(
        definition: DataAssetDefinition,
        normalized: NormalizedTableData,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        if not normalized.records:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="empty_dataset",
                    message="data asset contains no records",
                )
            )

        seen_business_keys: set[str] = set()
        for record in normalized.records:
            missing_business_keys = [
                field
                for field in definition.contract.business_key_fields
                if field not in record.properties
                or record.properties.get(field) is None
                or (
                    isinstance(record.properties.get(field), str)
                    and not record.properties[field].strip()
                )
            ]
            if missing_business_keys:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="business_key_missing",
                        message=(
                            "business key fields are missing or empty: "
                            + ", ".join(missing_business_keys)
                        ),
                        row_number=record.row_number,
                    )
                )
            elif record.business_key in seen_business_keys:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="business_key_duplicate",
                        message=(
                            f"business key is duplicated at row {record.row_number}"
                        ),
                        row_number=record.row_number,
                    )
                )
            seen_business_keys.add(record.business_key)

            for field in definition.contract.fields:
                value = record.properties.get(field.name)
                if value is None or (
                    isinstance(value, str) and not value.strip()
                ):
                    if field.required:
                        errors.append(
                            ValidationIssue(
                                severity="error",
                                code="required_field_missing",
                                message=(
                                    f"required field {field.name} is missing"
                                ),
                                row_number=record.row_number,
                                field_name=field.name,
                            )
                        )
                    continue

                if not _matches_python_type(value, field.python_type):
                    errors.append(
                        ValidationIssue(
                            severity="error",
                            code="field_type_invalid",
                            message=(
                                f"field {field.name} must be "
                                f"{field.python_type}"
                            ),
                            row_number=record.row_number,
                            field_name=field.name,
                        )
                    )
                    continue

                if field.python_type in {"number", "integer"}:
                    numeric_value = float(value)
                    if field.nonnegative and numeric_value < 0:
                        errors.append(
                            ValidationIssue(
                                severity="error",
                                code="field_nonnegative",
                                message=(
                                    f"field {field.name} must not be negative"
                                ),
                                row_number=record.row_number,
                                field_name=field.name,
                            )
                        )
                    if (
                        field.minimum is not None
                        and numeric_value < field.minimum
                    ):
                        errors.append(
                            ValidationIssue(
                                severity="error",
                                code="field_below_minimum",
                                message=(
                                    f"field {field.name} is below "
                                    f"{field.minimum}"
                                ),
                                row_number=record.row_number,
                                field_name=field.name,
                            )
                        )
                    if (
                        field.maximum is not None
                        and numeric_value > field.maximum
                    ):
                        errors.append(
                            ValidationIssue(
                                severity="error",
                                code="field_above_maximum",
                                message=(
                                    f"field {field.name} is above "
                                    f"{field.maximum}"
                                ),
                                row_number=record.row_number,
                                field_name=field.name,
                            )
                        )

            if definition.contract.geometry_type is not None:
                if record.geometry_wkt is None:
                    errors.append(
                        ValidationIssue(
                            severity="error",
                            code="geometry_missing",
                            message="geometry is required",
                            row_number=record.row_number,
                        )
                    )
                else:
                    try:
                        geometry = shapely_wkt.loads(record.geometry_wkt)
                    except Exception:
                        errors.append(
                            ValidationIssue(
                                severity="error",
                                code="geometry_invalid",
                                message="geometry is invalid",
                                row_number=record.row_number,
                            )
                        )
                    else:
                        if not _matches_geometry_type(
                            geometry.geom_type,
                            definition.contract.geometry_type,
                        ):
                            errors.append(
                                ValidationIssue(
                                    severity="error",
                                    code="geometry_type_mismatch",
                                    message=(
                                        "geometry type mismatch: expected "
                                        f"{definition.contract.geometry_type}, "
                                        f"got {geometry.geom_type}"
                                    ),
                                    row_number=record.row_number,
                                )
                            )

        if not _valid_extent(normalized.spatial_extent):
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="spatial_extent_invalid",
                    message="spatial extent is invalid",
                )
            )
        if (
            definition.contract.expected_record_count is not None
            and len(normalized.records)
            != definition.contract.expected_record_count
        ):
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="record_count_outside_expected",
                    message=(
                        "record count differs from expected "
                        f"{definition.contract.expected_record_count}"
                    ),
                )
            )

    @staticmethod
    def _validate_raster(
        normalized: NormalizedRasterData,
        errors: list[ValidationIssue],
    ) -> None:
        if normalized.width <= 0 or normalized.height <= 0:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="raster_dimension_invalid",
                    message="raster dimensions must be positive",
                )
            )
        if normalized.srid <= 0:
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="raster_crs_missing",
                    message="raster CRS is required",
                )
            )
        if (
            not isfinite(normalized.resolution_x)
            or normalized.resolution_x <= 0
            or not isfinite(normalized.resolution_y)
            or normalized.resolution_y <= 0
        ):
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="raster_resolution_invalid",
                    message="raster resolution is invalid",
                )
            )
        if normalized.nodata is not None and not isfinite(
            float(normalized.nodata)
        ):
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="raster_nodata_invalid",
                    message="raster nodata must be finite",
                )
            )
        if not _valid_extent(normalized.spatial_extent):
            errors.append(
                ValidationIssue(
                    severity="error",
                    code="spatial_extent_invalid",
                    message="spatial extent is invalid",
                )
            )

    @staticmethod
    def _statistics(normalized: NormalizedAssetData) -> dict[str, object]:
        if isinstance(normalized, NormalizedTableData):
            return {
                "record_count": len(normalized.records),
                "column_count": len(normalized.columns),
            }
        return {
            "width": normalized.width,
            "height": normalized.height,
            "band_count": normalized.band_count,
            "srid": normalized.srid,
        }


def _matches_python_type(value: object, python_type: str) -> bool:
    if python_type == "string":
        return isinstance(value, str)
    if python_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if python_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    return True


def _matches_geometry_type(actual: str, expected: str) -> bool:
    actual = actual.upper()
    expected = expected.upper()
    if actual == expected:
        return True
    if expected == "MULTIPOLYGON" and actual == "POLYGON":
        return True
    if expected == "MULTILINESTRING" and actual == "LINESTRING":
        return True
    return False


def _valid_extent(extent: tuple[float, float, float, float] | None) -> bool:
    if extent is None:
        return True
    if len(extent) != 4 or not all(isfinite(float(value)) for value in extent):
        return False
    return extent[0] <= extent[2] and extent[1] <= extent[3]
