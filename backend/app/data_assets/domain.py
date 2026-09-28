from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlparse


class AssetDataType(StrEnum):
    VECTOR = "vector"
    TABLE = "table"
    RASTER = "raster"
    PARAMETER = "parameter"


class AssetVersionStatus(StrEnum):
    IMPORTED = "imported"
    VALIDATED = "validated"
    PUBLISHED = "published"
    RETIRED = "retired"
    REJECTED = "rejected"


class ImportJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class AssetQuality(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L0 = "L0"


class SnapshotRole(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    BACKGROUND = "background"


class SourceFormat(StrEnum):
    GEOJSON = "geojson"
    MDB = "mdb"
    GEOTIFF = "geotiff"
    PARAMETER_FILE = "parameter_file"


def validate_source_uri(value: str) -> str:
    normalized = value.strip()
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("source_uri must be an absolute http/https URL without credentials")
    return normalized


@dataclass(frozen=True, slots=True)
class AssetFieldContract:
    name: str
    python_type: str
    required: bool = True
    nonnegative: bool = False
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class AssetContract:
    business_key_fields: tuple[str, ...]
    fields: tuple[AssetFieldContract, ...]
    geometry_type: str | None = None
    source_crs: str = "EPSG:4326"
    aggregate_of: str | None = None
    expected_record_count: int | None = None
    excluded_business_keys: tuple[str, ...] = ()
    exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DataAssetDefinition:
    asset_key: str
    region_id: str
    name: str
    data_type: AssetDataType
    spatial_granularity: str
    responsibility_unit: str
    update_interval_days: int
    is_core: bool
    contract: AssetContract
    source_table: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    row_number: int | None = None
    field_name: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    version_id: str
    status: AssetVersionStatus
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]
    statistics: dict[str, object]
    checked_at: datetime

    @property
    def publishable(self) -> bool:
        return self.status is AssetVersionStatus.VALIDATED and not self.errors
