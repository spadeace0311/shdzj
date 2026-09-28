# Data Asset Center Minimum Closed Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the versioned data asset center required by the loss assessment specification, including import, validation, lifecycle, snapshots, permissions, APIs, and a basic management UI.

**Architecture:** Add a focused `app/data_assets` package to the existing modular monolith. PostGIS stores immutable asset records and raster content, SQLAlchemy repositories own lifecycle persistence, FastAPI exposes the management contract, and the assessment run transaction captures the exact published versions used by loss tasks.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, PostgreSQL 16/PostGIS 3.4 with PostGIS Raster, Alembic, Pydantic 2, Rasterio, Shapely 2, PyProj, pyodbc on Windows for MDB, Pytest, Ruff, React 19, React Router 7, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-28-loss-assessment-design.md`

## Global Constraints

- Implement only the data asset center minimum closed loop from specification sections 6, 11.1, 13.1, 14.1, 17.3, 18, and 19; loss formulas, maps, reports, AI, and workgroup collaboration remain out of scope.
- Use revision `0012_data_asset_center` with `down_revision = "0011_intensity_assessment"`.
- Use the fixed asset catalog keys `shanghai.admin.city`, `shanghai.admin.county`, `shanghai.admin.town`, `shanghai.population.town`, `shanghai.building.town`, `shanghai.economy.county`, `shanghai.fault`, `shanghai.gdp.raster`, `shanghai.dem.raster`, and `shanghai.loss.parameters`.
- Use `region_id = "shanghai"` for the first catalog and keep every import, version, record, raster, and snapshot keyed by region.
- Keep asset versions immutable after publication; `imported`, `validated`, `published`, `retired`, and `rejected` are the only version statuses.
- Allow exactly one published version per asset and region.
- Store vector and table rows in `data_asset_records`; store GeoTIFF and PostGIS raster content in `data_asset_rasters`.
- Store external coordinates as EPSG:4326 and calculate area with the region profile projection, initially EPSG:32651.
- Open MDB files read-only through the 64-bit Microsoft Access ODBC driver; never modify the source file.
- Reject checksums, geometry, field contracts, aggregate totals, raster metadata, version transitions, and snapshot references that do not validate.
- Evaluation runs read only versions captured in `data_asset_snapshots`; a later import, publication, retirement, or rollback must not alter historical runs.
- The current intensity run may complete without loss assets, but snapshot capture must record missing required assets so the later loss implementation can fail its required loss tasks explicitly.
- Data maintainers may import, validate, and submit a version; only superadmins and data publishers may publish, retire, or roll back.
- Do not read, print, commit, or expose `.env`, connection strings, API keys, passwords, tokens, or credentials.
- All behavior changes use red-green TDD, focused tests, and one commit per task.

## Frozen Contracts For The Later Loss Assessment Plan

The later loss plan may depend on these tables without renaming or replacing them:

- `data_assets`
- `data_asset_versions`
- `data_asset_import_jobs`
- `data_asset_snapshots`
- `data_asset_audit_logs`
- `data_asset_records`
- `data_asset_rasters`

The later loss plan may depend on these service calls:

```python
class DataAssetSnapshotService:
    async def capture_required_assets(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        region_id: str,
        strict: bool,
    ) -> DataAssetSnapshotResult: ...

    async def get_locked_version(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> DataAssetVersion | None: ...

    async def list_locked_records(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> list[AssetRecord]: ...

    async def get_locked_raster(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> AssetRaster | None: ...
```

The later loss plan may depend on these HTTP routes:

```text
GET  /api/v1/data-assets
POST /api/v1/data-assets/{asset_key}/import
GET  /api/v1/data-asset-versions
GET  /api/v1/data-asset-versions/{version_id}
POST /api/v1/data-asset-versions/{version_id}/validate
POST /api/v1/data-asset-versions/{version_id}/publish
POST /api/v1/data-asset-versions/{version_id}/retire
POST /api/v1/data-asset-versions/{version_id}/rollback
```

---

### Task 1: Domain Contracts, Storage Configuration, and Reversible Migration

**Files:**

- Create: `backend/app/data_assets/__init__.py`
- Create: `backend/app/data_assets/domain.py`
- Create: `backend/app/data_assets/registry.py`
- Create: `backend/app/data_assets/storage.py`
- Create: `backend/app/data_assets/models.py`
- Create: `backend/migrations/versions/0012_data_asset_center.py`
- Create: `backend/tests/test_data_asset_domain.py`
- Create: `backend/tests/test_data_asset_storage.py`
- Create: `backend/tests/test_data_asset_schema.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/assessment/models.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/test_assessment_domain.py`
- Modify: `backend/tests/test_config.py`
- Modify: `backend/tests/test_migrations.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/Dockerfile`
- Modify: `.env.example`
- Modify: `infra/compose.yaml`

**Interfaces:**

- Consumes: existing `Base`, `Settings`, Alembic revision pattern, and region profile naming.
- Produces:
  - `AssetDataType`, `AssetVersionStatus`, `ImportJobStatus`, `AssetQuality`, `SnapshotRole`, `SourceFormat`
  - `AssetFieldContract`, `AssetContract`, `DataAssetDefinition`, `ValidationIssue`, `ValidationReport`
  - `FIRST_PARTY_ASSETS`, `get_asset_definition(asset_key: str) -> DataAssetDefinition`
  - `ManagedFileStore.store_upload(source, file_name: str, expected_checksum: str | None = None) -> StoredFile`
  - `ManagedFileStore.resolve(relative_path: str) -> Path`
  - SQLAlchemy models `DataAsset`, `DataAssetVersion`, `DataAssetImportJob`, `DataAssetSnapshot`, `DataAssetAuditLog`, `DataAssetRecord`, `DataAssetRaster`

- [ ] **Step 1: Add failing configuration and domain tests**

Add these dependencies to `backend/pyproject.toml`:

```toml
dependencies = [
  "alembic==1.14.0",
  "asyncpg==0.30.0",
  "fastapi==0.115.6",
  "geoalchemy2==0.16.0",
  "httpx==0.28.1",
  "numpy==2.2.2",
  "pyjwt==2.10.1",
  "pyodbc==5.2.0",
  "pwdlib[argon2]==0.2.1",
  "pydantic-settings==2.7.0",
  "pyproj==3.7.1",
  "python-multipart==0.0.20",
  "pyyaml==6.0.2",
  "rasterio==1.4.3",
  "shapely==2.0.6",
  "sqlalchemy[asyncio]==2.0.36",
  "temporalio==1.33.0",
  "uvicorn[standard]==0.34.0",
  "websockets==15.0.1",
]
```

Add to `backend/Dockerfile` before the application copy:

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends unixodbc \
    && rm -rf /var/lib/apt/lists/*
```

Add to `backend/app/config.py`:

```python
data_asset_region_id: str = "shanghai"
data_asset_required_registry_path: str = "/config/data_assets/shanghai-required-assets.yaml"
data_asset_storage_root: str = "/var/lib/data-assets"
data_asset_max_upload_bytes: int = 1_073_741_824
data_asset_mdb_driver: str = "Microsoft Access Driver (*.mdb, *.accdb)"
```

Add validation in `validate_collector_configuration`:

```python
if not self.data_asset_storage_root.strip():
    raise ValueError("DATA_ASSET_STORAGE_ROOT must not be empty")
if not self.data_asset_region_id.strip():
    raise ValueError("DATA_ASSET_REGION_ID must not be empty")
if not self.data_asset_required_registry_path.strip():
    raise ValueError("DATA_ASSET_REQUIRED_REGISTRY_PATH must not be empty")
if self.data_asset_max_upload_bytes < 1:
    raise ValueError("DATA_ASSET_MAX_UPLOAD_BYTES must be positive")
if not self.data_asset_mdb_driver.strip():
    raise ValueError("DATA_ASSET_MDB_DRIVER must not be empty")
```

Add to `.env.example`:

```dotenv
DATA_ASSET_REGION_ID=shanghai
DATA_ASSET_REQUIRED_REGISTRY_PATH=/config/data_assets/shanghai-required-assets.yaml
DATA_ASSET_STORAGE_ROOT=/var/lib/data-assets
DATA_ASSET_STORAGE_HOST_DIR=../data/data-assets
DATA_ASSET_MAX_UPLOAD_BYTES=1073741824
DATA_ASSET_MDB_DRIVER=Microsoft Access Driver (*.mdb, *.accdb)
```

Add this bind mount to the `api`, `assessment-dispatcher`, and
`temporal-worker` service definitions in `infra/compose.yaml`:

```yaml
      - ${DATA_ASSET_STORAGE_HOST_DIR:-../data/data-assets}:/var/lib/data-assets
```

Do not declare a Docker named volume for this path. The host directory is the
single storage authority, so a Windows-host CLI and Linux containers resolve
the same bytes. `../data/data-assets` is relative to `infra/compose.yaml` and
resolves to the repository-level `data/data-assets` directory.

Task 5 creates `data-asset-worker` and adds this same bind mount to that
service definition when it is introduced.

Create `backend/tests/test_data_asset_domain.py`:

```python
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
        "shanghai.population.town",
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
```

Create `backend/tests/test_data_asset_storage.py`:

```python
import hashlib
from io import BytesIO
from pathlib import Path

import pytest

from app.data_assets.storage import ManagedFileStore


def test_store_upload_writes_checksum_addressed_file(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_upload_bytes=1024)
    payload = b'{"type":"FeatureCollection","features":[]}'

    stored = store.store_upload(
        BytesIO(payload),
        file_name="boundary.geojson",
        expected_checksum=hashlib.sha256(payload).hexdigest(),
    )

    assert stored.checksum == hashlib.sha256(payload).hexdigest()
    assert stored.size_bytes == len(payload)
    assert Path(stored.managed_path).read_bytes() == payload
    assert store.resolve(stored.relative_path) == Path(stored.managed_path)


def test_store_upload_rejects_traversal_and_oversize(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_upload_bytes=4)

    with pytest.raises(ValueError, match="file name"):
        store.store_upload(BytesIO(b"data"), file_name="../escape.geojson")
    with pytest.raises(ValueError, match="maximum"):
        store.store_upload(BytesIO(b"12345"), file_name="large.geojson")
```

Add these assertions to `backend/tests/test_assessment_domain.py`:

```python
assert configured.data_asset_storage_root == "/var/lib/data-assets"
assert configured.data_asset_max_upload_bytes == 1_073_741_824
```

Add these invalid overrides to `test_assessment_settings_validate_bounded_values`:

```python
{"data_asset_storage_root": " "},
{"data_asset_max_upload_bytes": 0},
{"data_asset_mdb_driver": ""},
```

- [ ] **Step 2: Run the domain tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml build api
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_domain.py tests/test_data_asset_storage.py -v
```

Expected: FAIL because `app.data_assets` and the new settings do not exist.

- [ ] **Step 3: Implement the immutable domain and catalog**

Create `backend/app/data_assets/domain.py`:

```python
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
```

Create `backend/app/data_assets/registry.py` with the exact catalog:

```python
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
```

Create `backend/app/data_assets/storage.py`:

```python
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class StoredFile:
    file_name: str
    relative_path: str
    managed_path: str
    size_bytes: int
    checksum: str


class ManagedFileStore:
    def __init__(self, root: str | Path, *, max_upload_bytes: int) -> None:
        self._root = Path(root).resolve()
        self._max_upload_bytes = max_upload_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    def store_upload(
        self,
        source: BinaryIO,
        *,
        file_name: str,
        expected_checksum: str | None = None,
    ) -> StoredFile:
        safe_name = self._safe_name(file_name)
        digest = hashlib.sha256()
        size = 0
        temporary = self._root / f".upload-{os.getpid()}-{id(source)}.tmp"
        try:
            with temporary.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > self._max_upload_bytes:
                        raise ValueError("upload exceeds configured maximum size")
                    digest.update(chunk)
                    target.write(chunk)
            checksum = digest.hexdigest()
            if expected_checksum is not None and checksum != expected_checksum.lower():
                raise ValueError("upload checksum does not match")
            relative = Path(checksum[:2]) / checksum[2:4] / f"{checksum}-{safe_name}"
            destination = self._root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary.unlink()
            else:
                temporary.replace(destination)
            return StoredFile(
                file_name=safe_name,
                relative_path=relative.as_posix(),
                managed_path=str(destination),
                size_bytes=size,
                checksum=checksum,
            )
        finally:
            if temporary.exists():
                temporary.unlink()

    def resolve(self, relative_path: str) -> Path:
        candidate = (self._root / relative_path).resolve()
        if self._root not in candidate.parents:
            raise ValueError("managed path escapes storage root")
        return candidate

    @staticmethod
    def _safe_name(file_name: str) -> str:
        if not file_name or Path(file_name).name != file_name:
            raise ValueError("file name must not contain path segments")
        if any(character in file_name for character in ("\x00", "\r", "\n")):
            raise ValueError("file name contains invalid characters")
        return file_name[:255]


def copy_host_file(
    store: ManagedFileStore,
    source_path: str | Path,
    *,
    file_name: str,
) -> StoredFile:
    with Path(source_path).open("rb") as source:
        return store.store_upload(source, file_name=file_name)
```

- [ ] **Step 4: Implement SQLAlchemy models and migration**

Create `backend/app/data_assets/models.py` with these exact table names and relationships:

```python
class DataAsset(Base):
    __tablename__ = "data_assets"
    __table_args__ = (UniqueConstraint("asset_key", "region_id", name="uq_data_assets_key_region"),)


class DataAssetVersion(Base):
    __tablename__ = "data_asset_versions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_data_asset_versions_asset_version"),
        Index(
            "uq_data_asset_versions_published_asset",
            "asset_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )


class DataAssetImportJob(Base):
    __tablename__ = "data_asset_import_jobs"


class DataAssetSnapshot(Base):
    __tablename__ = "data_asset_snapshots"
    __table_args__ = (UniqueConstraint("run_id", "asset_key", name="uq_data_asset_snapshots_run_asset"),)


class DataAssetAuditLog(Base):
    __tablename__ = "data_asset_audit_logs"


class DataAssetRecord(Base):
    __tablename__ = "data_asset_records"
    __table_args__ = (
        UniqueConstraint("version_id", "row_number", name="uq_data_asset_records_version_row"),
        Index("ix_data_asset_records_geom", "geom", postgresql_using="gist"),
        Index("ix_data_asset_records_properties", "properties", postgresql_using="gin"),
    )


class DataAssetRaster(Base):
    __tablename__ = "data_asset_rasters"
    __table_args__ = (UniqueConstraint("version_id", name="uq_data_asset_rasters_version"),)
```

Use these exact column groups:

- `data_assets`: `id`, `asset_key`, `region_id`, `name`, `data_type`, `spatial_granularity`, `responsibility_unit`, `update_interval_days`, `is_core`, `contract`, `created_at`, `updated_at`.
- `data_asset_versions`: `id`, `asset_id`, `version`, `status`, `source_uri`, `license_name`, `acquired_at`, `valid_from`, `valid_to`, `quality_grade`, `change_note`, `schema_summary`, `record_count`, `spatial_extent`, `source_crs`, `checksum`, `managed_path`, `imported_by`, `reviewed_by`, `imported_at`, `validated_at`, `published_at`, `retired_at`, `created_at`, `updated_at`.
- `data_asset_import_jobs`: `id`, `asset_id`, `asset_version_id`, `file_name`, `file_format`, `source_uri`, `file_size_bytes`, `raw_checksum`, `managed_path`, `status`, `validation_errors`, `validation_warnings`, `statistics`, `error_summary`, `requested_by`, `started_at`, `completed_at`, `created_at`.
- `data_asset_snapshots`: `id`, `run_id`, `asset_id`, `asset_version_id`, `asset_key`, `version`, `checksum`, `role`, `required`, `created_at`.
- `data_asset_audit_logs`: `id`, `asset_id`, `version_id`, `action`, `actor`, `reason`, `details`, `created_at`.
- `data_asset_records`: `id`, `version_id`, `row_number`, `business_key`, `properties`, `geom`, `created_at`.
- `data_asset_rasters`: `id`, `version_id`, `rast`, `band_manifest`, `checksum`, `width`, `height`, `srid`, `spatial_extent`, `created_at`.
- `assessment_runs`: add `data_asset_snapshot_fingerprint` and `data_asset_snapshot_result`. These columns are part of the initial `0012_data_asset_center` migration so the later run-snapshot task never modifies an already applied revision.

Create `backend/migrations/versions/0012_data_asset_center.py`:

```python
revision: str = "0012_data_asset_center"
down_revision: str | None = "0011_intensity_assessment"
```

Use `postgresql.UUID(as_uuid=True)`, `JSONB`, `Geometry`, and `Raster` exactly as the SQLAlchemy models declare. Add the partial unique index:

```python
op.create_index(
    "uq_data_asset_versions_published_asset",
    "data_asset_versions",
    ["asset_id"],
    unique=True,
    postgresql_where=sa.text("status = 'published'"),
)
```

Add all inverse drop operations in `downgrade()` in reverse dependency order. Register the model module in `backend/migrations/env.py`:

```python
from app.data_assets import models as data_asset_models  # noqa: F401
```

Update `backend/tests/test_migrations.py`:

```python
LATEST_REVISION = "0012_data_asset_center"
DATA_ASSET_PREVIOUS_REVISION = "0011_intensity_assessment"
```

Add `backend/tests/test_data_asset_schema.py` with a reversible migration test that downgrades to `0011_intensity_assessment`, asserts all seven tables and both run-snapshot columns are absent, upgrades to `0012_data_asset_center`, asserts all seven tables and both columns are present, checks the published partial index, and then returns to head.

- [ ] **Step 5: Run focused tests, lint, migration, and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_domain.py tests/test_data_asset_storage.py tests/test_data_asset_schema.py tests/test_migrations.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/data_assets migrations tests/test_data_asset_domain.py tests/test_data_asset_storage.py tests/test_data_asset_schema.py tests/test_migrations.py
```

Expected: PASS with Alembic head `0012_data_asset_center`.

```bash
git add backend/pyproject.toml backend/Dockerfile backend/app/config.py backend/app/assessment/models.py backend/app/data_assets backend/migrations/env.py backend/migrations/versions/0012_data_asset_center.py backend/tests/test_data_asset_domain.py backend/tests/test_data_asset_storage.py backend/tests/test_data_asset_schema.py backend/tests/test_migrations.py backend/tests/test_assessment_domain.py backend/tests/test_config.py .env.example infra/compose.yaml
git commit -m "feat: add data asset domain and schema"
```

### Task 2: GeoJSON Import and Managed Import Queue

**Files:**

- Create: `backend/app/data_assets/importer.py`
- Create: `backend/app/data_assets/geojson_importer.py`
- Create: `backend/app/data_assets/parameter_importer.py`
- Create: `backend/app/data_assets/import_jobs.py`
- Create: `backend/tests/test_data_asset_geojson_importer.py`
- Create: `backend/tests/test_data_asset_parameter_importer.py`
- Create: `backend/tests/test_data_asset_import_jobs.py`
- Modify: `backend/app/data_assets/models.py`
- Modify: `backend/app/data_assets/domain.py`

**Interfaces:**

- Consumes: `DataAssetDefinition`, `AssetContract`, `ManagedFileStore`, and the seven data asset tables.
- Produces:
  - `NormalizedRecord`
  - `NormalizedTableData`
  - `NormalizedRasterData`
  - `NormalizedAssetData`
  - `AssetImporter` protocol with `load(path: Path, definition: DataAssetDefinition) -> NormalizedAssetData`
  - `GeoJsonAssetImporter.load(path: Path, definition: DataAssetDefinition) -> NormalizedTableData`
  - `ParameterFileImporter.load(path: Path, definition: DataAssetDefinition) -> NormalizedTableData`
  - `UnsupportedImportFormat(ValueError)`
  - `queue_import_job(session, request: QueueImportRequest) -> DataAssetImportJob`
  - `claim_next_import_job(session) -> DataAssetImportJob | None`
  - `complete_import_job(session, job_id, version_id) -> None`
  - `reject_import_job(session, job_id, report: ValidationReport) -> None`
  - `fail_import_job(session, job_id, error: Exception) -> None`

- [ ] **Step 1: Write failing GeoJSON and queue tests**

Create `backend/tests/test_data_asset_geojson_importer.py`:

```python
import json
from pathlib import Path

import pytest

from app.data_assets.geojson_importer import GeoJsonAssetImporter
from app.data_assets.registry import get_asset_definition


def test_geojson_importer_normalizes_polygon_and_fields(tmp_path: Path) -> None:
    source = tmp_path / "town.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"ID": "310115001", "NAME": "陆家嘴街道"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[121.50, 31.22], [121.51, 31.22], [121.51, 31.23], [121.50, 31.22]]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = GeoJsonAssetImporter().load(
        source,
        get_asset_definition("shanghai.admin.town"),
    )

    assert result.record_count == 1
    assert result.records[0].business_key == "310115001"
    assert result.records[0].properties["NAME"] == "陆家嘴街道"
    assert result.records[0].geometry_wkt.startswith("MULTIPOLYGON")
    assert result.source_crs == "EPSG:4326"


def test_geojson_importer_rejects_duplicate_business_key(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.geojson"
    feature = {
        "type": "Feature",
        "properties": {"ID": "same", "NAME": "重复"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[121.5, 31.2], [121.51, 31.2], [121.51, 31.21], [121.5, 31.2]]],
        },
    }
    source.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature, feature]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="business key"):
        GeoJsonAssetImporter().load(
            source,
            get_asset_definition("shanghai.admin.town"),
        )
```

Create `backend/tests/test_data_asset_import_jobs.py`:

```python
from sqlalchemy import select

from app.data_assets.import_jobs import (
    QueueImportRequest,
    claim_next_import_job,
    queue_import_job,
)
from app.data_assets.models import DataAssetImportJob


async def test_import_queue_claim_is_single_consumer(session_factory) -> None:
    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key="shanghai.admin.town",
                    version="2022.1",
                    source_uri="https://example.gov.invalid/town.geojson",
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="initial import",
                    file_name="town.geojson",
                    file_format="geojson",
                    file_size_bytes=12,
                    checksum="a" * 64,
                    relative_path="aa/aa/aaaaaaaa-aa.geojson",
                    requested_by="tester",
                ),
            )
            job_id = job.id

    async with session_factory() as session:
        async with session.begin():
            claimed = await claim_next_import_job(session)
    async with session_factory() as session:
        async with session.begin():
            second = await claim_next_import_job(session)
            stored = await session.get(DataAssetImportJob, job_id)

    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.asset_version_id is not None
    assert second is None
    assert stored.status == "running"
```

Create `backend/tests/test_data_asset_parameter_importer.py`:

```python
from pathlib import Path

from app.data_assets.parameter_importer import ParameterFileImporter
from app.data_assets.registry import get_asset_definition


def test_parameter_yaml_importer_uses_version_as_business_key(tmp_path: Path) -> None:
    source = tmp_path / "loss-parameters.yaml"
    source.write_text(
        """
version: "loss-test-only-v1"
calibration_status: "reference_uncalibrated"
test_only: true
provenance:
  owner: "synthetic equation test"
models:
  building_damage:
    model_id: "building-structure-matrix-v1"
    formula_version: "building-structure-matrix-v1"
    source_requirements: []
    scenarios:
      low: {}
      central: {}
      high: {}
""".strip(),
        encoding="utf-8",
    )

    result = ParameterFileImporter().load(
        source,
        get_asset_definition("shanghai.loss.parameters"),
    )

    assert result.record_count == 1
    assert result.records[0].business_key == "loss-test-only-v1"
    assert result.records[0].properties["parameter_set_id"] == "loss-test-only-v1"
    assert result.records[0].properties["test_only"] is True
```

- [ ] **Step 2: Run the GeoJSON tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_geojson_importer.py tests/test_data_asset_import_jobs.py -v
```

Expected: FAIL because the importers, normalized records, and queue functions do not exist.

- [ ] **Step 3: Implement normalized import contracts and GeoJSON parsing**

Add to `backend/app/data_assets/domain.py`:

```python
@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    row_number: int
    business_key: str
    properties: dict[str, object]
    geometry_wkt: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedTableData:
    columns: tuple[str, ...]
    records: tuple[NormalizedRecord, ...]
    source_crs: str
    spatial_extent: tuple[float, float, float, float] | None

    @property
    def record_count(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class NormalizedRasterData:
    width: int
    height: int
    srid: int
    band_count: int
    dtype: str
    nodata: float | None
    resolution_x: float
    resolution_y: float
    spatial_extent: tuple[float, float, float, float]


type NormalizedAssetData = NormalizedTableData | NormalizedRasterData
```

Create `backend/app/data_assets/importer.py`:

```python
from pathlib import Path
from typing import Protocol

from app.data_assets.domain import DataAssetDefinition, NormalizedAssetData


class AssetImporter(Protocol):
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedAssetData: ...


class UnsupportedImportFormat(ValueError):
    pass
```

Create `backend/app/data_assets/geojson_importer.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import MultiPolygon, shape
from shapely.ops import transform
from shapely.validation import explain_validity

from app.data_assets.domain import (
    DataAssetDefinition,
    NormalizedRecord,
    NormalizedTableData,
)


class GeoJsonAssetImporter:
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedTableData:
        document = json.loads(path.read_text(encoding="utf-8"))
        features = self._features(document)
        contract = definition.contract
        transformer = (
            None
            if contract.source_crs == "EPSG:4326"
            else Transformer.from_crs(contract.source_crs, "EPSG:4326", always_xy=True).transform
        )
        records: list[NormalizedRecord] = []
        seen: set[str] = set()
        columns: set[str] = set()

        for row_number, feature in enumerate(features, start=1):
            properties = dict(feature.get("properties") or {})
            columns.update(properties)
            business_key = self._business_key(properties, contract.business_key_fields)
            if business_key in seen:
                raise ValueError(f"business key is duplicated at row {row_number}")
            seen.add(business_key)
            geometry = None
            geometry_wkt = None
            if feature.get("geometry") is not None:
                geometry = shape(feature["geometry"])
                if not geometry.is_valid:
                    raise ValueError(f"invalid geometry at row {row_number}: {explain_validity(geometry)}")
                if transformer is not None:
                    geometry = transform(transformer, geometry)
                if geometry.geom_type == "Polygon":
                    geometry = MultiPolygon([geometry])
                geometry_wkt = geometry.wkt
            records.append(
                NormalizedRecord(
                    row_number=row_number,
                    business_key=business_key,
                    properties=properties,
                    geometry_wkt=geometry_wkt,
                )
            )

        extent = None
        if any(record.geometry_wkt for record in records):
            geometries = [shape(record.geometry_wkt) for record in records if record.geometry_wkt]
            merged = geometries[0]
            for geometry in geometries[1:]:
                merged = merged.union(geometry)
            min_x, min_y, max_x, max_y = merged.bounds
            extent = (min_x, min_y, max_x, max_y)
        return NormalizedTableData(
            columns=tuple(sorted(columns)),
            records=tuple(records),
            source_crs="EPSG:4326",
            spatial_extent=extent,
        )

    @staticmethod
    def _features(document: object) -> list[dict]:
        if not isinstance(document, dict):
            raise ValueError("GeoJSON root must be an object")
        if document.get("type") == "FeatureCollection":
            features = document.get("features")
            if not isinstance(features, list):
                raise ValueError("GeoJSON features must be a list")
            return features
        if document.get("type") == "Feature":
            return [document]
        raise ValueError("GeoJSON must be a Feature or FeatureCollection")

    @staticmethod
    def _business_key(properties: dict, fields: tuple[str, ...]) -> str:
        values = [str(properties.get(field, "")).strip() for field in fields]
        if not values or any(not value for value in values):
            raise ValueError("business key fields are required")
        return "|".join(values)
```

Create `backend/app/data_assets/parameter_importer.py`:

```python
import json
from pathlib import Path

import yaml

from app.data_assets.domain import (
    DataAssetDefinition,
    NormalizedRecord,
    NormalizedTableData,
)


class ParameterFileImporter:
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedTableData:
        if path.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise ValueError("parameter file must be YAML or JSON")
        if not isinstance(payload, dict):
            raise ValueError("parameter file must contain one mapping")
        version = payload.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("parameter file requires a non-empty version")
        properties = {**payload, "parameter_set_id": version}
        record = NormalizedRecord(
            row_number=1,
            business_key=version,
            properties=properties,
        )
        return NormalizedTableData(
            columns=tuple(sorted(properties)),
            records=(record,),
            source_crs=definition.contract.source_crs,
            spatial_extent=None,
        )
```

- [ ] **Step 4: Implement durable import job claiming and failure recording**

Create `backend/app/data_assets/import_jobs.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_assets.models import (
    DataAsset,
    DataAssetAuditLog,
    DataAssetImportJob,
    DataAssetVersion,
)
from app.data_assets.domain import ValidationReport, validate_source_uri
from app.data_assets.registry import get_asset_definition


@dataclass(frozen=True, slots=True)
class QueueImportRequest:
    asset_key: str
    version: str
    source_uri: str
    license_name: str | None
    acquired_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    change_note: str
    file_name: str
    file_format: str
    file_size_bytes: int
    checksum: str
    relative_path: str
    requested_by: str


async def queue_import_job(
    session: AsyncSession,
    request: QueueImportRequest,
) -> DataAssetImportJob:
    source_uri = validate_source_uri(request.source_uri)
    definition = get_asset_definition(request.asset_key)
    asset = await _ensure_asset(session, definition)
    now = datetime.now(UTC)
    version = DataAssetVersion(
        asset_id=asset.id,
        version=request.version,
        status="imported",
        source_uri=source_uri,
        license_name=request.license_name,
        acquired_at=request.acquired_at,
        valid_from=request.valid_from,
        valid_to=request.valid_to,
        quality_grade=None,
        change_note=request.change_note,
        schema_summary={},
        record_count=0,
        spatial_extent=None,
        source_crs=definition.contract.source_crs,
        checksum=request.checksum,
        managed_path=request.relative_path,
        imported_by=request.requested_by,
        reviewed_by=None,
        imported_at=now,
        validated_at=None,
        published_at=None,
        retired_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(version)
    await session.flush()
    job = DataAssetImportJob(
        asset_id=asset.id,
        asset_version_id=version.id,
        file_name=request.file_name,
        file_format=request.file_format,
        source_uri=source_uri,
        file_size_bytes=request.file_size_bytes,
        raw_checksum=request.checksum,
        managed_path=request.relative_path,
        status="queued",
        validation_errors=[],
        validation_warnings=[],
        statistics={},
        error_summary=None,
        requested_by=request.requested_by,
        started_at=None,
        completed_at=None,
        created_at=now,
    )
    session.add(job)
    await session.flush()
    session.add(
        DataAssetAuditLog(
            asset_id=asset.id,
            version_id=version.id,
            action="import",
            actor=request.requested_by,
            reason=request.change_note,
            details={
                "file_name": request.file_name,
                "file_format": request.file_format,
                "import_job_id": str(job.id),
            },
            created_at=now,
        )
    )
    return job


async def claim_next_import_job(
    session: AsyncSession,
) -> DataAssetImportJob | None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('data-asset-import-worker'))"))
    job = await session.scalar(
        select(DataAssetImportJob)
        .where(DataAssetImportJob.status == "queued")
        .order_by(DataAssetImportJob.created_at, DataAssetImportJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    return job


async def complete_import_job(
    session: AsyncSession,
    job_id: UUID,
    version_id: UUID,
) -> None:
    job = await session.get(DataAssetImportJob, job_id, with_for_update=True)
    if job is None:
        raise LookupError("data asset import job not found")
    job.asset_version_id = version_id
    job.status = "completed"
    job.completed_at = datetime.now(UTC)


async def reject_import_job(
    session: AsyncSession,
    job_id: UUID,
    report: ValidationReport,
) -> None:
    job = await session.get(DataAssetImportJob, job_id, with_for_update=True)
    if job is None:
        raise LookupError("data asset import job not found")
    job.status = "rejected"
    job.validation_errors = [
        {
            "code": issue.code,
            "message": issue.message,
            "row_number": issue.row_number,
            "field_name": issue.field_name,
        }
        for issue in report.errors
    ]
    job.validation_warnings = [
        {
            "code": issue.code,
            "message": issue.message,
            "row_number": issue.row_number,
            "field_name": issue.field_name,
        }
        for issue in report.warnings
    ]
    job.error_summary = "; ".join(issue.message for issue in report.errors)[:1000]
    job.completed_at = datetime.now(UTC)
    await append_import_audit(
        session,
        job,
        action="import_rejected",
        details={"error_codes": [issue.code for issue in report.errors]},
    )


async def fail_import_job(
    session: AsyncSession,
    job_id: UUID,
    error: Exception,
) -> None:
    job = await session.get(DataAssetImportJob, job_id, with_for_update=True)
    if job is None:
        raise LookupError("data asset import job not found")
    job.status = "failed"
    job.error_summary = sanitize_error(error)[:1000]
    job.completed_at = datetime.now(UTC)
    await append_import_audit(
        session,
        job,
        action="import_failed",
        details={"error_category": type(error).__name__},
    )


async def append_import_audit(
    session: AsyncSession,
    job: DataAssetImportJob,
    *,
    action: str,
    details: dict,
) -> None:
    session.add(
        DataAssetAuditLog(
            asset_id=job.asset_id,
            version_id=job.asset_version_id,
            action=action,
            actor=job.requested_by,
            reason=None,
            details=details,
            created_at=datetime.now(UTC),
        )
    )


def sanitize_error(error: Exception) -> str:
    message = " ".join(str(error).replace("\x00", " ").split())
    if not message:
        return type(error).__name__
    patterns = (
        r"(?i)\b(?:driver|dbq|database|password|pwd|user|uid)=([^;]+)",
        r"(?i)\b(?:postgres(?:ql)?(?:\+asyncpg)?|mysql|mssql|odbc)://\S+",
        r"(?i)\b[A-Za-z]:\\[^\s;]+",
    )
    for pattern in patterns:
        message = re.sub(pattern, "[redacted]", message)
    return message[:1000]


async def _ensure_asset(session: AsyncSession, definition) -> DataAsset:
    asset = await session.scalar(
        select(DataAsset).where(
            DataAsset.asset_key == definition.asset_key,
            DataAsset.region_id == definition.region_id,
        ).with_for_update()
    )
    if asset is not None:
        return asset
    asset = DataAsset(
        asset_key=definition.asset_key,
        region_id=definition.region_id,
        name=definition.name,
        data_type=definition.data_type.value,
        spatial_granularity=definition.spatial_granularity,
        responsibility_unit=definition.responsibility_unit,
        update_interval_days=definition.update_interval_days,
        is_core=definition.is_core,
        contract={
            "business_key_fields": list(definition.contract.business_key_fields),
            "fields": [
                {
                    "name": field.name,
                    "python_type": field.python_type,
                    "required": field.required,
                    "nonnegative": field.nonnegative,
                    "minimum": field.minimum,
                    "maximum": field.maximum,
                }
                for field in definition.contract.fields
            ],
            "geometry_type": definition.contract.geometry_type,
            "source_crs": definition.contract.source_crs,
            "aggregate_of": definition.contract.aggregate_of,
            "expected_record_count": definition.contract.expected_record_count,
            "excluded_business_keys": list(
                definition.contract.excluded_business_keys
            ),
            "exclusion_reason": definition.contract.exclusion_reason,
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(asset)
    await session.flush()
    return asset
```

Do not create the worker in this task. The worker depends on
`DataAssetService.populate_candidate_version` and
`DataAssetService.validate_version`, which are introduced in Task 5. The
durable queue contract and failure recorder are complete and independently
testable here; Task 5 adds the worker after its service dependency exists.

Add the required imports to `backend/app/data_assets/import_jobs.py`:

```python
import re

from app.data_assets.domain import ValidationReport, validate_source_uri
from app.data_assets.registry import get_asset_definition
```

The exact worker dispatch table will be introduced in Task 5:

```python
IMPORTERS: dict[str, object] = {
    "geojson": GeoJsonAssetImporter(),
    "parameter_file": ParameterFileImporter(),
    "mdb": MdbAssetImporter(),
    "geotiff": GeoTiffAssetImporter(),
}
```

Do not parse files in the HTTP handler.

- [ ] **Step 5: Run tests, lint, and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_geojson_importer.py tests/test_data_asset_parameter_importer.py tests/test_data_asset_import_jobs.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/data_assets tests/test_data_asset_geojson_importer.py tests/test_data_asset_parameter_importer.py tests/test_data_asset_import_jobs.py
```

Expected: PASS.

```bash
git add backend/app/data_assets backend/tests/test_data_asset_geojson_importer.py backend/tests/test_data_asset_parameter_importer.py backend/tests/test_data_asset_import_jobs.py
git commit -m "feat: import geojson data assets"
```

### Task 2A: Shared Data-Asset Test Fixtures

**Files:**

- Create: `backend/tests/data_asset_helpers.py`
- Create: `backend/tests/test_data_asset_helpers.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**

- Consumes: `DataAssetService`, `DataAssetSnapshotService`, `queue_import_job`, `EventService`, and the data-asset ORM tables.
- Produces fixtures:
  - `candidate_factory(version: str) -> UUID`
  - `published_population_asset() -> UUID`
  - `seeded_assessment_run() -> UUID`
  - `data_asset_client() -> AsyncClient`
  - `geojson_town_file() -> Path`
  - `seeded_outbox() -> SeededOutbox`
  - `seeded_imported_version() -> SeededImportedVersion`
- Produces helper functions:
  - `publish_new_population_version(session_factory, version: str) -> UUID`
  - `wait_for_import_job(session_factory, job_id: UUID) -> UUID`

This task lands before Task 5, while `DataAssetService` and
`DataAssetSnapshotService` are introduced there. Implement
`data_asset_helpers.py` with only `dataclasses`, `pathlib`, and `pytest`
imported at module scope. Import service and ORM classes inside each fixture
or helper function. This keeps fixture registration valid before Task 5 while
the fixture bodies become executable as soon as Task 5 lands. Task 5 runs the
full helper smoke test.

- [ ] **Step 1: Create the shared fixtures and helper dataclasses**

```python
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from app.data_assets.domain import DataAssetDefinition


@dataclass(frozen=True, slots=True)
class SeededImportedVersion:
    version_id: UUID
    source_path: Path
    definition: DataAssetDefinition


@dataclass(frozen=True, slots=True)
class SeededOutbox:
    event_id: UUID
    revision_id: UUID
    outbox_id: UUID
```

Decorate `candidate_factory`, `published_population_asset`,
`seeded_assessment_run`, `data_asset_client`, `geojson_town_file`,
`seeded_outbox`, and `seeded_imported_version` with `@pytest.fixture`.
`publish_new_population_version` and `wait_for_import_job` are plain async
helper functions, not fixtures.

`candidate_factory` creates a 212-record synthetic `shanghai.population.town`
version through `queue_import_job`, calls
`DataAssetService.populate_candidate_version`, and returns the candidate
version UUID. Town codes are deterministic twelve-digit values derived from
`310115000001` through the required count; `total` is 100 for every row.

`published_population_asset` first publishes a matching synthetic
`shanghai.admin.town` version containing the same 212 town codes. It then calls
`candidate_factory("2022.1")`, validates the population version with
`DataAssetService.validate_version`, publishes it with
`DataAssetService.publish_version`, and returns the population version UUID.
The matching admin-town publication is mandatory because validation requires
every population business key to exist in a published town boundary version.

`seeded_outbox` uses `EventService.ingest_collected` with
`source_event_id=f"DATA-ASSET-TEST-{uuid4()}"`, a formal Shanghai event, and
the active test boundary. It returns the event, revision, and assessment
outbox UUIDs.

`seeded_assessment_run` calls
`AssessmentRepository.ensure_run_from_outbox` with the `seeded_outbox`
identifiers and returns the run UUID.

`data_asset_client` uses `httpx.AsyncClient` with
`ASGITransport(app=app)`, sets `base_url="http://testserver"`, and overrides
`get_current_user` with a superadmin before yielding. The override is removed
in `finally`.

`geojson_town_file` writes a valid FeatureCollection containing one
`MULTIPOLYGON` town feature to `tmp_path / "towns.geojson"`.

`seeded_imported_version` creates an imported `shanghai.gdp.raster` version and
a synthetic single-band GeoTIFF under `tmp_path`, then returns
`SeededImportedVersion(version_id, source_path, definition)`. Callers use the
named fields; it is not a tuple.

`publish_new_population_version` ensures the matching admin-town version is
published, then creates, validates, and publishes a synthetic population
version with the requested version string, retiring the previous published
population version in the same transaction.

`wait_for_import_job` does not require a separately running worker. It claims
the requested queued job with `process_import_job` in the test process, commits
the result, and returns `asset_version_id`. It raises `TimeoutError` with the
final job status if the job is not terminal after the processing attempt.

- [ ] **Step 2: Register fixture aliases**

Modify `backend/tests/conftest.py` with these exact imports. The imported
objects are pytest fixture functions already decorated with `@pytest.fixture`
in `tests.data_asset_helpers`; placing them in the conftest module namespace
registers them for collection under their original names. Keep the existing
`session_factory` fixture unchanged.

```python
from tests.data_asset_helpers import (  # noqa: F401
    candidate_factory,
    data_asset_client,
    geojson_town_file,
    published_population_asset,
    seeded_assessment_run,
    seeded_imported_version,
    seeded_outbox,
)
```

- [ ] **Step 3: Run the fixture registration smoke test and commit**

Create `backend/tests/test_data_asset_helpers.py`:

```python
def test_shared_data_asset_fixture_functions_are_registered(
    pytestconfig,
) -> None:
    fixture_manager = pytestconfig.pluginmanager.get_plugin(
        "fixturemanager"
    )
    registered = set(fixture_manager._arg2fixturedefs)
    assert {
        "candidate_factory",
        "published_population_asset",
        "seeded_assessment_run",
        "data_asset_client",
        "geojson_town_file",
        "seeded_outbox",
        "seeded_imported_version",
    } <= registered
```

This smoke test inspects pytest's fixture registry without requesting the
fixtures, so their service-backed bodies do not execute before Task 5 exists.

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_helpers.py -v
```

```bash
git add backend/tests/conftest.py backend/tests/data_asset_helpers.py backend/tests/test_data_asset_helpers.py
git commit -m "test: add shared data asset fixtures"
```

### Task 3: MDB Table and ESRI Geometry Import

**Files:**

- Create: `backend/app/data_assets/mdb_importer.py`
- Create: `backend/app/data_assets/mdb_cli.py`
- Create: `scripts/run-mdb-import.ps1`
- Create: `backend/tests/test_data_asset_mdb_importer.py`
- Create: `backend/tests/test_data_asset_mdb_host_integration.py`
- Modify: `backend/pyproject.toml`
- Modify: `infra/compose.yaml`
- Modify: `.env.example`

**Interfaces:**

- Consumes: `NormalizedTableData`, `NormalizedRecord`, `AssetContract`, and configured `data_asset_mdb_driver`.
- Produces:
  - `MdbConnectionFactory.connect(path: Path) -> Connection`
  - `MdbAssetImporter.load(path: Path, definition: DataAssetDefinition) -> NormalizedTableData`
  - `parse_esri_shape(blob: bytes) -> BaseGeometry`
  - `run_mdb_import(source_path: Path, asset_key: str, version: str, source_uri: str, actor: str, storage_root: Path) -> UUID`
  - CLI command `python -m app.data_assets.mdb_cli import`

- [ ] **Step 1: Write failing unit tests with synthetic ESRI shape blobs**

Create `backend/tests/test_data_asset_mdb_importer.py`:

```python
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.data_assets.mdb_importer import MdbAssetImporter, parse_esri_shape
from app.data_assets.registry import get_asset_definition


def _point_shape(x: float, y: float) -> bytes:
    return struct.pack("<i2d", 1, x, y)


def _polyline_shape(points: tuple[tuple[float, float], ...]) -> bytes:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    header = struct.pack(
        "<i4dii",
        3,
        min(xs),
        min(ys),
        max(xs),
        max(ys),
        1,
        len(points),
    )
    parts = struct.pack("<i", 0)
    coordinates = struct.pack(
        f"<{len(points) * 2}d",
        *(coordinate for point in points for coordinate in point),
    )
    return header + parts + coordinates


class FakeCursor:
    def __init__(self) -> None:
        self.description = [
            ("OBJECTID",),
            ("name",),
            ("strike",),
            ("DIP_ANGLE",),
            ("DIP_DIR",),
            ("LENGTH",),
            ("WIDTH",),
            ("SHAPE",),
        ]
        self.rows = [
            (
                1,
                "断裂 A",
                "120.5",
                "30.0",
                "90",
                "10.0",
                "0.5",
                _polyline_shape(((121.5, 31.2), (121.6, 31.3))),
            ),
            (
                2,
                "断裂 B",
                "121.5",
                "31.0",
                "95",
                "12.0",
                "0.7",
                _polyline_shape(((121.7, 31.4), (121.8, 31.5))),
            ),
        ]

    def execute(self, statement: str) -> None:
        assert statement == "SELECT * FROM [ACTIVEFAULT]"

    def fetchall(self):
        return self.rows


class FakeConnection:
    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def close(self) -> None:
        return None


def test_parse_esri_point_shape() -> None:
    geometry = parse_esri_shape(_point_shape(121.5, 31.2))

    assert geometry.geom_type == "Point"
    assert geometry.x == pytest.approx(121.5)
    assert geometry.y == pytest.approx(31.2)


def test_mdb_importer_normalizes_table_and_geometry(tmp_path: Path) -> None:
    source = tmp_path / "base.mdb"
    source.write_bytes(b"not-opened-by-fake")
    class FakeConnectionFactory:
        def connect(self, _path):
            return FakeConnection()

    importer = MdbAssetImporter(connection_factory=FakeConnectionFactory())

    result = importer.load(source, get_asset_definition("shanghai.fault"))

    assert result.record_count == 2
    assert result.records[0].business_key == "1"
    assert result.records[0].properties["name"] == "断裂 A"
    assert result.records[0].properties["strike"] == pytest.approx(120.5)
    assert result.records[0].geometry_wkt.startswith("MULTILINESTRING")
```

Create `backend/tests/test_data_asset_mdb_host_integration.py`:

```python
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
```

Add the marker in `backend/pyproject.toml`:

```toml
markers = [
  "performance: integration benchmarks that use the full configured grid",
  "windows_mdb: read-only Microsoft Access ODBC integration tests",
]
```

- [ ] **Step 2: Run the unit test to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_mdb_importer.py -v
```

Expected: FAIL because `MdbAssetImporter` and `parse_esri_shape` do not exist.

- [ ] **Step 3: Implement read-only MDB access and ESRI shape parsing**

Create `backend/app/data_assets/mdb_importer.py` with this connection path:

```python
class MdbConnectionFactory:
    def __init__(self, driver: str) -> None:
        self._driver = driver

    def connect(self, path: Path):
        import pyodbc

        connection_string = (
            f"DRIVER={{{self._driver}}};"
            f"DBQ={path.resolve()};"
            "ReadOnly=True;"
        )
        return pyodbc.connect(connection_string, autocommit=False)


class MdbAssetImporter:
    def __init__(self, connection_factory=None) -> None:
        self._connection_factory = connection_factory or MdbConnectionFactory(
            settings.data_asset_mdb_driver
        )

    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedTableData:
        if definition.source_table is None:
            raise ValueError("data asset does not define an MDB source table")
        connection = self._connection_factory.connect(path)
        try:
            cursor = connection.cursor()
            cursor.execute(f"SELECT * FROM [{definition.source_table}]")
            rows = cursor.fetchall()
            columns = tuple(item[0] for item in cursor.description)
            records_list: list[NormalizedRecord] = []
            for index, row in enumerate(rows, start=1):
                record = self._normalize_row(
                    row_number=index,
                    columns=columns,
                    row=row,
                    definition=definition,
                )
                if record.business_key in definition.contract.excluded_business_keys:
                    continue
                records_list.append(record)
            records = tuple(records_list)
            return NormalizedTableData(
                columns=columns,
                records=records,
                source_crs=definition.contract.source_crs,
                spatial_extent=_extent(records),
            )
        finally:
            connection.close()
```

`_normalize_row` must:

1. Build a case-insensitive property map while preserving source field names.
2. Convert Access `Decimal`, date, and bytes values into JSON-safe values.
3. Coerce string-valued numeric fields declared as `number` or `integer` in the contract to `float` or `int`, including the real `ACTIVEFAULT` angle and length fields.
4. Detect the `SHAPE` field and call `parse_esri_shape`.
5. Parse the business key using the contract's case-insensitive field mapping.
6. Reject duplicate business keys.

Implement `parse_esri_shape(blob: bytes)` for these exact ESRI shape type codes:

```python
SUPPORTED_SHAPE_TYPES = {
    1: "POINT",
    3: "POLYLINE",
    5: "POLYGON",
    8: "MULTIPOINT",
    11: "POINT_Z",
    13: "POLYLINE_Z",
    15: "POLYGON_Z",
    18: "MULTIPOINT_Z",
    21: "POINT_M",
    23: "POLYLINE_M",
    25: "POLYGON_M",
    28: "MULTIPOINT_M",
}
```

Reject null shapes, wrong byte order, truncated coordinate arrays, unsupported type codes, and invalid polygon rings with `ValueError`. Convert ESRI `PolyLine` to `MultiLineString` and polygon ring orientation to a valid `MultiPolygon`; do not silently accept invalid coordinates.

- [ ] **Step 4: Add the host-side CLI and synchronous Windows processing**

Create `backend/app/data_assets/mdb_cli.py`:

```python
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.data_assets.mdb_cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("import")
    command.add_argument("--asset-key", required=True)
    command.add_argument("--file", required=True, type=Path)
    command.add_argument("--version", required=True)
    command.add_argument("--source-uri", required=True)
    command.add_argument("--actor", required=True)
    command.add_argument("--storage-root", required=True, type=Path)
    return parser


async def run_mdb_import(
    source_path: Path,
    asset_key: str,
    version: str,
    source_uri: str,
    actor: str,
    storage_root: Path,
) -> UUID:
    validate_source_uri(source_uri)
    definition = get_asset_definition(asset_key)
    normalized = MdbAssetImporter().load(source_path, definition)
    store = ManagedFileStore(
        storage_root,
        max_upload_bytes=settings.data_asset_max_upload_bytes,
    )
    stored = copy_host_file(store, source_path, file_name=source_path.name)
    async with SessionFactory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key=asset_key,
                    version=version,
                    source_uri=source_uri,
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="MDB source imported from Windows host",
                    file_name=stored.file_name,
                    file_format="mdb",
                    file_size_bytes=stored.size_bytes,
                    checksum=stored.checksum,
                    relative_path=stored.relative_path,
                    requested_by=actor,
                ),
            )
            if job.asset_version_id is None:
                raise RuntimeError("MDB import job has no candidate version")
            service = DataAssetService()
            await service.populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {
                    "source_table": definition.source_table,
                    "record_count": normalized.record_count,
                    "source_crs": normalized.source_crs,
                    "importer": "mdb",
                },
            )
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor=actor,
            )
            if not report.publishable:
                await reject_import_job(session, job.id, report)
            else:
                await complete_import_job(
                    session,
                    job.id,
                    job.asset_version_id,
                )
            return job.id
```

Add `POSTGRES_HOST_PORT` to `infra/compose.yaml` and expose Postgres only on the loopback interface:

```yaml
    ports:
      - "127.0.0.1:${POSTGRES_HOST_PORT:-5432}:5432"
```

Add to `.env.example`:

```dotenv
POSTGRES_HOST_PORT=5432
```

Create `scripts/run-mdb-import.ps1` so the host CLI uses the same host
storage directory and connects through the published loopback port without
printing credentials:

```powershell
param(
  [Parameter(Mandatory = $true)][string]$AssetKey,
  [Parameter(Mandatory = $true)][string]$File,
  [Parameter(Mandatory = $true)][string]$Version,
  [Parameter(Mandatory = $true)][string]$SourceUri,
  [Parameter(Mandatory = $true)][string]$Actor
)

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
$values = @{}
Get-Content -LiteralPath $envFile | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') {
    $values[$matches[1].Trim()] = $matches[2].Trim()
  }
}
$hostPort = if ($values.ContainsKey("POSTGRES_HOST_PORT")) {
  $values["POSTGRES_HOST_PORT"]
} else {
  "5432"
}
$databaseUrl = $values["DATABASE_URL"] -replace "@postgres:5432", "@127.0.0.1:$hostPort"
$env:DATABASE_URL = $databaseUrl
$storageRoot = if ($values.ContainsKey("DATA_ASSET_STORAGE_HOST_DIR")) {
  [IO.Path]::GetFullPath(
    (Join-Path (Split-Path -Parent $envFile) $values["DATA_ASSET_STORAGE_HOST_DIR"])
  )
} else {
  Join-Path $root "data\data-assets"
}

Push-Location (Join-Path $root "backend")
try {
  python -m app.data_assets.mdb_cli import `
    --asset-key $AssetKey `
    --file $File `
    --version $Version `
    --source-uri $SourceUri `
    --actor $Actor `
    --storage-root $storageRoot
} finally {
  Pop-Location
}
```

Document the host command in the task verification output:

```powershell
.\scripts\run-mdb-import.ps1 `
  -AssetKey shanghai.population.town `
  -File "D:\地震应急辅助决策系统\基础数据\上海应急基础数据2022.mdb" `
  -Version "2022.1" `
  -SourceUri "https://example.gov.invalid/shanghai-base-data-2022" `
  -Actor "data-maintainer"
```

After the import, verify from inside the API container that the
`managed_path` recorded by the host CLI is readable and has the same SHA-256
checksum. This host-write/container-read check is part of the Task 3
acceptance, not an optional manual note.

The Windows host parses and populates the MDB in this CLI because the Linux
worker intentionally has no Microsoft Access ODBC driver. Keep the generic
worker dispatch entry for synthetic tests only:

```python
    "mdb": MdbAssetImporter(),
```

- [ ] **Step 5: Run unit tests, host integration tests, and commit**

Run in Compose:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_mdb_importer.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/data_assets tests/test_data_asset_mdb_importer.py
```

Run on the Z440 Windows host with Python 3.12 and installed project dependencies:
The host shell must set `DATABASE_URL` to the same database with host
`127.0.0.1:${POSTGRES_HOST_PORT}`; `scripts/run-mdb-import.ps1` performs the
equivalent rewrite for the production CLI.

```powershell
Push-Location backend
python -m pytest tests/test_data_asset_mdb_host_integration.py -v -m windows_mdb
Pop-Location
```

Expected: the first command passes with synthetic shape data; the host command reads 213 raw town-population rows, excludes the configured district aggregate, reports 212 publishable town-population records, and finds 23 active-fault records.

```bash
git add backend/app/data_assets backend/tests/test_data_asset_mdb_importer.py backend/tests/test_data_asset_mdb_host_integration.py backend/pyproject.toml infra/compose.yaml .env.example scripts/run-mdb-import.ps1
git commit -m "feat: import access mdb data assets"
```

### Task 4: GeoTIFF Import Into PostGIS Raster

**Files:**

- Create: `backend/app/data_assets/raster_importer.py`
- Create: `backend/app/data_assets/raster_repository.py`
- Create: `backend/tests/test_data_asset_raster_importer.py`
- Create: `backend/tests/test_data_asset_raster_repository.py`

**Interfaces:**

- Consumes: `NormalizedRasterData`, `DataAssetDefinition`, `ManagedFileStore`, and `data_asset_rasters`.
- Produces:
  - `GeoTiffAssetImporter.load(path: Path, definition: DataAssetDefinition) -> NormalizedRasterData`
  - `save_raster_version(session: AsyncSession, version_id: UUID, path: Path, descriptor: NormalizedRasterData) -> UUID`
  - `load_raster_version(session: AsyncSession, version_id: UUID) -> tuple[bytes, dict]`

- [ ] **Step 1: Write failing raster tests**

Create `backend/tests/test_data_asset_raster_importer.py`:

```python
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.data_assets.registry import get_asset_definition
from app.data_assets.raster_importer import GeoTiffAssetImporter


def test_geotiff_importer_reads_metadata(tmp_path: Path) -> None:
    source = tmp_path / "gdp.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=4,
        height=3,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        nodata=-9999.0,
        transform=from_origin(121.0, 31.5, 0.01, 0.01),
    ) as dataset:
        dataset.write(np.ones((1, 3, 4), dtype=np.float32))

    descriptor = GeoTiffAssetImporter().load(
        source,
        get_asset_definition("shanghai.gdp.raster"),
    )

    assert descriptor.width == 4
    assert descriptor.height == 3
    assert descriptor.srid == 4326
    assert descriptor.band_count == 1
    assert descriptor.nodata == -9999.0
    assert descriptor.resolution_x == pytest.approx(0.01)


def test_geotiff_importer_rejects_unknown_crs(tmp_path: Path) -> None:
    source = tmp_path / "bad.tif"
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=1,
        height=1,
        count=1,
        dtype="uint8",
        transform=from_origin(0, 1, 1, 1),
    ) as dataset:
        dataset.write(np.zeros((1, 1, 1), dtype=np.uint8))

    with pytest.raises(ValueError, match="CRS"):
        GeoTiffAssetImporter().load(
            source,
            get_asset_definition("shanghai.gdp.raster"),
        )
```

Create `backend/tests/test_data_asset_raster_repository.py`:

```python
import numpy as np
import rasterio
from rasterio.transform import from_origin
from sqlalchemy import select, text

from app.data_assets.models import DataAssetVersion
from app.data_assets.raster_importer import GeoTiffAssetImporter
from app.data_assets.raster_repository import save_raster_version


async def test_save_raster_version_can_read_postgis_metadata(
    session_factory,
    seeded_imported_version,
) -> None:
    source = seeded_imported_version
    descriptor = GeoTiffAssetImporter().load(
        source.source_path,
        source.definition,
    )
    async with session_factory() as session:
        async with session.begin():
            raster_id = await save_raster_version(
                session,
                source.version_id,
                source.source_path,
                descriptor,
            )
            row = (
                await session.execute(
                    text(
                        """
                        SELECT ST_Width(rast), ST_Height(rast), ST_SRID(rast),
                               checksum, band_manifest
                        FROM data_asset_rasters
                        WHERE id = :raster_id
                        """
                    ),
                    {"raster_id": raster_id},
                )
            ).one()
    assert row[0] == descriptor.width
    assert row[1] == descriptor.height
    assert row[2] == descriptor.srid
    assert len(row[3]) == 64
```

The `seeded_imported_version` fixture must create one `data_assets` row, one
`data_asset_versions` row in `imported`, and one synthetic GeoTIFF in
`tmp_path`.

- [ ] **Step 2: Run the raster tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_raster_importer.py tests/test_data_asset_raster_repository.py -v
```

Expected: FAIL because raster importer and repository do not exist.

- [ ] **Step 3: Implement GeoTIFF metadata normalization**

Create `backend/app/data_assets/raster_importer.py`:

```python
from pathlib import Path

import rasterio
from pyproj import CRS

from app.data_assets.domain import DataAssetDefinition, NormalizedRasterData


class GeoTiffAssetImporter:
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedRasterData:
        if definition.data_type.value != "raster":
            raise ValueError("GeoTIFF importer requires a raster asset")
        with rasterio.open(path) as dataset:
            if dataset.crs is None:
                raise ValueError("GeoTIFF CRS is required")
            epsg = CRS.from_user_input(dataset.crs).to_epsg()
            if epsg is None:
                raise ValueError("GeoTIFF CRS must map to an EPSG code")
            if dataset.width <= 0 or dataset.height <= 0 or dataset.count <= 0:
                raise ValueError("GeoTIFF dimensions and band count must be positive")
            if dataset.res[0] <= 0 or dataset.res[1] >= 0:
                raise ValueError("GeoTIFF resolution is invalid")
            if not dataset.bounds or not all(map(lambda value: value == value, dataset.bounds)):
                raise ValueError("GeoTIFF bounds are invalid")
            return NormalizedRasterData(
                width=dataset.width,
                height=dataset.height,
                srid=epsg,
                band_count=dataset.count,
                dtype=dataset.dtypes[0],
                nodata=dataset.nodata,
                resolution_x=abs(dataset.res[0]),
                resolution_y=abs(dataset.res[1]),
                spatial_extent=tuple(dataset.bounds),
            )
```

- [ ] **Step 4: Persist raster bytes with `ST_FromGDALRaster` and validate them**

Create `backend/app/data_assets/raster_repository.py`:

```python
async def save_raster_version(
    session: AsyncSession,
    version_id: UUID,
    path: Path,
    descriptor: NormalizedRasterData,
) -> UUID:
    payload = path.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    raster_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO data_asset_rasters (
                id, version_id, rast, band_manifest, checksum,
                width, height, srid, spatial_extent, created_at
            )
            VALUES (
                :id, :version_id, ST_FromGDALRaster(:payload),
                CAST(:manifest AS jsonb), :checksum,
                :width, :height, :srid,
                ST_Transform(
                    ST_Envelope(ST_FromGDALRaster(:payload))::geometry,
                    4326
                ),
                :created_at
            )
            """
        ),
        {
            "id": raster_id,
            "version_id": version_id,
            "payload": payload,
            "manifest": json.dumps(
                {
                    "band_count": descriptor.band_count,
                    "dtype": descriptor.dtype,
                    "nodata": descriptor.nodata,
                    "resolution_x": descriptor.resolution_x,
                    "resolution_y": descriptor.resolution_y,
                },
                sort_keys=True,
            ),
            "checksum": checksum,
            "width": descriptor.width,
            "height": descriptor.height,
            "srid": descriptor.srid,
            "created_at": datetime.now(UTC),
        },
    )
    row = (
        await session.execute(
            text(
                """
                SELECT ST_Width(rast), ST_Height(rast), ST_SRID(rast)
                FROM data_asset_rasters
                WHERE id = :id
                """
            ),
            {"id": raster_id},
        )
    ).one()
    if (int(row[0]), int(row[1]), int(row[2])) != (
        descriptor.width,
        descriptor.height,
        descriptor.srid,
    ):
        raise RuntimeError("persisted raster metadata does not match source")
    return raster_id


async def load_raster_version(
    session: AsyncSession,
    version_id: UUID,
) -> tuple[bytes, dict]:
    row = (
        await session.execute(
            text(
                """
                SELECT ST_AsGDALRaster(rast, 'GTiff') AS payload,
                       checksum, band_manifest
                FROM data_asset_rasters
                WHERE version_id = :version_id
                """
            ),
            {"version_id": version_id},
        )
    ).one_or_none()
    if row is None:
        raise LookupError("data asset raster not found")
    return bytes(row.payload), dict(row.band_manifest)
```

When Task 5 creates `backend/app/data_assets/worker.py`, its `IMPORTERS`
dispatch table must contain:

```python
    "geotiff": GeoTiffAssetImporter(),
```

- [ ] **Step 5: Run tests, lint, and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_raster_importer.py tests/test_data_asset_raster_repository.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/data_assets tests/test_data_asset_raster_importer.py tests/test_data_asset_raster_repository.py
```

Expected: PASS.

```bash
git add backend/app/data_assets backend/tests/test_data_asset_raster_importer.py backend/tests/test_data_asset_raster_repository.py
git commit -m "feat: import geotiff data assets"
```

### Task 5: Validation, Lifecycle, Repository, and Snapshot Service

**Files:**

- Create: `backend/app/data_assets/validators.py`
- Create: `backend/app/data_assets/repository.py`
- Create: `backend/app/data_assets/required_registry.py`
- Create: `backend/app/data_assets/service.py`
- Create: `backend/app/data_assets/snapshot_service.py`
- Create: `backend/app/data_assets/worker.py`
- Create: `config/data_assets/shanghai-required-assets.yaml`
- Create: `backend/tests/test_data_asset_validation.py`
- Create: `backend/tests/test_data_asset_lifecycle.py`
- Create: `backend/tests/test_data_asset_required_registry.py`
- Create: `backend/tests/test_data_asset_repository.py`
- Create: `backend/tests/test_data_asset_snapshot_service.py`
- Modify: `backend/app/data_assets/import_jobs.py`
- Modify: `backend/app/config.py`
- Modify: `infra/compose.yaml`
- Modify: `.env.example`

**Interfaces:**

- Consumes: normalized import data, all data asset tables, and existing `AssessmentRun`.
- Produces:
  - `DataAssetValidator.validate(definition: DataAssetDefinition, normalized: NormalizedAssetData) -> ValidationReport`
  - `DataAssetService.populate_candidate_version(session, version_id: UUID, normalized: NormalizedAssetData, schema_summary: dict) -> DataAssetVersion`
  - `DataAssetService.validate_version(session, version_id: UUID, *, actor: str = "system") -> ValidationReport`
  - `DataAssetService.publish_version(session, version_id: UUID, actor: str, reason: str) -> DataAssetVersion`
  - `DataAssetService.retire_version(session, version_id: UUID, actor: str, reason: str) -> DataAssetVersion`
  - `DataAssetService.rollback_version(session, version_id: UUID, actor: str, reason: str) -> DataAssetVersion`
  - `DataAssetRepository.get_published_version(session, asset_key: str, region_id: str) -> DataAssetVersion | None`
  - `DataAssetRepository.list_versions(session, asset_key: str | None = None, region_id: str | None = None) -> list[DataAssetVersion]`
  - `DataAssetRepository.list_records(session, version_id: UUID) -> list[AssetRecord]`
  - `RequiredAssetRegistry`
  - `load_required_asset_registry(path: str | Path) -> RequiredAssetRegistry`
  - `DataAssetSnapshotService.capture_required_assets(session, *, run_id: UUID, region_id: str, strict: bool) -> DataAssetSnapshotResult`
  - `DataAssetSnapshotService.get_locked_version(session, *, run_id: UUID, asset_key: str) -> DataAssetVersion | None`
  - `DataAssetSnapshotService.list_locked_records(session, *, run_id: UUID, asset_key: str) -> list[AssetRecord]`
  - `DataAssetSnapshotService.get_locked_raster(session, *, run_id: UUID, asset_key: str) -> AssetRaster | None`
  - `process_import_job(session: AsyncSession, job: DataAssetImportJob) -> None`
  - `python -m app.data_assets.worker` continuously claims queued jobs.

- [ ] **Step 1: Write failing validation, lifecycle, and snapshot tests**

Create `backend/tests/test_data_asset_validation.py`:

```python
from datetime import UTC, datetime

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
```

Create `backend/tests/test_data_asset_lifecycle.py`:

```python
import pytest
from sqlalchemy import select

from app.data_assets.import_jobs import fail_import_job
from app.data_assets.models import (
    DataAssetAuditLog,
    DataAssetImportJob,
    DataAssetVersion,
)
from app.data_assets.service import DataAssetService


async def test_candidate_validation_publish_and_single_published_version(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")

    async with session_factory() as session:
        async with session.begin():
            first = await service.validate_version(session, first_id)
            assert first.publishable
            await service.publish_version(session, first_id, "publisher", "initial")
            second = await service.validate_version(session, second_id)
            assert second.publishable
            await service.publish_version(session, second_id, "publisher", "annual update")
            rows = (
                await session.scalars(
                    select(DataAssetVersion).where(
                        DataAssetVersion.status == "published"
                    )
                )
            ).all()
            audit_rows = (await session.scalars(select(DataAssetAuditLog))).all()

    assert [str(row.id) for row in rows] == [str(second_id)]
    assert [row.action for row in audit_rows] == [
        "import",
        "validate",
        "publish",
        "import",
        "validate",
        "retire",
        "publish",
    ]


async def test_failed_import_writes_failed_job_and_audit(
    session_factory,
    candidate_factory,
) -> None:
    version_id = await candidate_factory("2022.3")
    async with session_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(DataAssetImportJob).where(
                    DataAssetImportJob.asset_version_id == version_id
                )
            )
            await fail_import_job(session, job.id, ValueError("synthetic parse failure"))

    async with session_factory() as session:
        failed_job = await session.get(DataAssetImportJob, job.id)
        audit_actions = (
            await session.scalars(
                select(DataAssetAuditLog).where(
                    DataAssetAuditLog.version_id == version_id
                )
            )
        ).all()
    assert failed_job.status == "failed"
    assert failed_job.error_summary == "synthetic parse failure"
    assert [row.action for row in audit_actions] == ["import", "import_failed"]


async def test_published_version_is_immutable_and_rollback_republishes(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, first_id)
            await service.publish_version(session, first_id, "publisher", "initial")
            await service.validate_version(session, second_id)
            await service.publish_version(session, second_id, "publisher", "update")
            await service.rollback_version(session, first_id, "publisher", "bad update")
            with pytest.raises(ValueError, match="transition"):
                await service.publish_version(session, first_id, "publisher", "duplicate")

    async with session_factory() as session:
        async with session.begin():
            first = await session.get(DataAssetVersion, first_id)

    assert first.status == "published"


async def test_published_version_rejects_metadata_mutation(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    version_id = await candidate_factory("2022.1")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, version_id)
            await service.publish_version(session, version_id, "publisher", "initial")
            with pytest.raises(ValueError, match="immutable"):
                await service.update_candidate_metadata(
                    session,
                    version_id,
                    change_note="changed after publication",
                    actor="publisher",
                )
```

Create `backend/tests/test_data_asset_repository.py`:

```python
from app.config import settings
from app.data_assets.repository import DataAssetRepository


async def test_repository_lists_published_versions_and_records(
    session_factory,
    published_population_asset,
) -> None:
    repository = DataAssetRepository()
    async with session_factory() as session:
        published = await repository.get_published_version(
            session,
            asset_key="shanghai.population.town",
            region_id=settings.data_asset_region_id,
        )
        versions = await repository.list_versions(
            session,
            asset_key="shanghai.population.town",
            region_id=settings.data_asset_region_id,
        )
        records = await repository.list_records(session, published.id)

    assert published.id == published_population_asset
    assert [version.id for version in versions] == [published_population_asset]
    assert len(records) == 212
    assert records[0].business_key


async def test_repository_returns_none_when_raster_is_not_loaded(
    session_factory,
    seeded_imported_version,
) -> None:
    async with session_factory() as session:
        raster = await DataAssetRepository().get_raster(
            session,
            seeded_imported_version.version_id,
        )
    assert raster is None
```

Create `backend/tests/test_data_asset_snapshot_service.py`:

```python
import pytest
from sqlalchemy import select

from app.assessment.models import AssessmentRun
from app.config import settings
from app.data_assets.models import DataAssetSnapshot
from app.data_assets.snapshot_service import DataAssetSnapshotService
from tests.data_asset_helpers import publish_new_population_version


async def test_snapshot_records_published_versions_and_missing_required_assets(
    session_factory,
    seeded_assessment_run,
    published_population_asset,
) -> None:
    run_id = seeded_assessment_run
    async with session_factory() as session:
        async with session.begin():
            result = await DataAssetSnapshotService().capture_required_assets(
                session,
                run_id=run_id,
                region_id=settings.data_asset_region_id,
                strict=False,
            )

    assert result.snapshot_count == 2
    assert "shanghai.building.town" in result.missing_required
    assert len(result.fingerprint) == 64
    async with session_factory() as session:
        snapshots = (await session.scalars(select(DataAssetSnapshot))).all()
    assert {snapshot.asset_key for snapshot in snapshots} == {
        "shanghai.admin.town",
        "shanghai.population.town",
    }


async def test_repeated_capture_reuses_locked_versions(
    session_factory,
    seeded_assessment_run,
    published_population_asset,
) -> None:
    run_id = seeded_assessment_run
    async with session_factory() as session:
        async with session.begin():
            first = await DataAssetSnapshotService().capture_required_assets(
                session,
                run_id=run_id,
                region_id=settings.data_asset_region_id,
                strict=False,
            )

    await publish_new_population_version(session_factory, "2022.2")

    async with session_factory() as session:
        async with session.begin():
            second = await DataAssetSnapshotService().capture_required_assets(
                session,
                run_id=run_id,
                region_id=settings.data_asset_region_id,
                strict=False,
            )
        snapshots = (
            await session.scalars(
                select(DataAssetSnapshot)
                .where(DataAssetSnapshot.run_id == run_id)
                .order_by(DataAssetSnapshot.asset_key)
            )
        ).all()

    assert second.fingerprint == first.fingerprint
    assert [snapshot.version for snapshot in snapshots] == [
        "2022.1",
        "2022.1",
    ]


async def test_strict_snapshot_fails_when_required_asset_is_missing(
    session_factory,
    seeded_assessment_run,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(LookupError, match="required data assets"):
                await DataAssetSnapshotService().capture_required_assets(
                    session,
                    run_id=seeded_assessment_run,
                    region_id=settings.data_asset_region_id,
                    strict=True,
                )
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_validation.py tests/test_data_asset_lifecycle.py tests/test_data_asset_required_registry.py tests/test_data_asset_repository.py tests/test_data_asset_snapshot_service.py -v
```

Expected: FAIL because validation, service, repository, and snapshot service do not exist.

- [ ] **Step 3: Implement contract validation and aggregate checks**

Create `backend/app/data_assets/validators.py`:

```python
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
```

Implement these exact validation codes:

```text
asset_type_mismatch
empty_dataset
required_field_missing
business_key_missing
business_key_duplicate
business_key_set_mismatch
field_type_invalid
field_nonnegative
field_below_minimum
field_above_maximum
geometry_missing
geometry_invalid
geometry_type_mismatch
spatial_extent_invalid
raster_dimension_invalid
raster_crs_missing
raster_resolution_invalid
raster_nodata_invalid
aggregate_difference_exceeded
record_count_outside_expected
```

Add aggregate checks in `DataAssetService.validate_version`:

1. For population, buildings, and economy contracts with `aggregate_of`, compare city/county/town totals after normalization.
2. Store both absolute and relative difference in `statistics["aggregate_checks"]`.
3. Treat a relative difference greater than `0.005` as an error and a difference greater than `0.001` as a warning.
4. Do not modify source values to force totals to match.
5. Before a town population or town building version can be published, compare its normalized business-key set with the published `shanghai.admin.town` version. Require exact equality and reject with `business_key_set_mismatch` when either dependent keys are missing or extra.

- [ ] **Step 4: Implement lifecycle, repository, and snapshot persistence**

Create `backend/app/data_assets/repository.py` with these signatures:

```python
@dataclass(frozen=True, slots=True)
class AssetRecord:
    row_number: int
    business_key: str
    properties: dict
    geometry_wkt: str | None


@dataclass(frozen=True, slots=True)
class AssetRaster:
    version_id: UUID
    width: int
    height: int
    srid: int
    checksum: str
    band_manifest: dict
    spatial_extent_wkt: str | None


class DataAssetRepository:
    async def get_asset(
        self,
        session: AsyncSession,
        *,
        asset_key: str,
        region_id: str,
    ) -> DataAsset | None: ...

    async def get_published_version(
        self,
        session: AsyncSession,
        *,
        asset_key: str,
        region_id: str,
    ) -> DataAssetVersion | None: ...

    async def list_versions(
        self,
        session: AsyncSession,
        *,
        asset_key: str | None = None,
        region_id: str | None = None,
    ) -> list[DataAssetVersion]: ...

    async def list_records(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> list[AssetRecord]: ...

    async def get_raster(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> AssetRaster | None: ...
```

Create `backend/app/data_assets/service.py`:

```python
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AssetSummaryView:
    asset_key: str
    region_id: str
    name: str
    data_type: str
    spatial_granularity: str
    responsibility_unit: str
    update_interval_days: int
    is_core: bool
    published_version: str | None
    published_at: datetime | None
    update_due_at: datetime | None
    is_update_overdue: bool


@dataclass(frozen=True, slots=True)
class AssetVersionView:
    version_id: UUID
    asset_key: str
    region_id: str
    version: str
    status: AssetVersionStatus
    source_uri: str
    license_name: str | None
    acquired_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    quality_grade: str | None
    change_note: str | None
    schema_summary: dict
    record_count: int
    checksum: str
    imported_by: str
    reviewed_by: str | None
    imported_at: datetime
    validated_at: datetime | None
    published_at: datetime | None
    retired_at: datetime | None
    validation_errors: tuple[ValidationIssue, ...]
    validation_warnings: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class AssetVersionDetailView:
    summary: AssetVersionView


ALLOWED_TRANSITIONS: dict[AssetVersionStatus, frozenset[AssetVersionStatus]] = {
    AssetVersionStatus.IMPORTED: frozenset({AssetVersionStatus.VALIDATED, AssetVersionStatus.REJECTED}),
    AssetVersionStatus.VALIDATED: frozenset({AssetVersionStatus.PUBLISHED}),
    AssetVersionStatus.PUBLISHED: frozenset({AssetVersionStatus.RETIRED}),
    AssetVersionStatus.RETIRED: frozenset({AssetVersionStatus.PUBLISHED}),
    AssetVersionStatus.REJECTED: frozenset(),
}


class DataAssetService:
    async def list_assets(
        self,
        session: AsyncSession,
        *,
        region_id: str | None = None,
    ) -> list[AssetSummaryView]: ...

    async def list_versions(
        self,
        session: AsyncSession,
        *,
        asset_key: str | None = None,
        region_id: str | None = None,
    ) -> list[AssetVersionView]: ...

    async def get_version_detail(
        self,
        session: AsyncSession,
        version_id: UUID,
    ) -> AssetVersionDetailView: ...

    async def populate_candidate_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        normalized: NormalizedAssetData,
        schema_summary: dict,
        *,
        source_path: Path | None = None,
    ) -> DataAssetVersion: ...

    async def validate_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        *,
        actor: str = "system",
    ) -> ValidationReport: ...

    async def update_candidate_metadata(
        self,
        session: AsyncSession,
        version_id: UUID,
        *,
        change_note: str,
        actor: str,
    ) -> DataAssetVersion: ...

    async def publish_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> DataAssetVersion: ...

    async def retire_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> DataAssetVersion: ...

    async def rollback_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        actor: str,
        reason: str,
    ) -> DataAssetVersion: ...
```

`publish_version`, `retire_version`, and `rollback_version` must:

1. Lock the `data_assets` row with `SELECT ... FOR UPDATE`.
2. Lock the target version row.
3. Verify the state transition from `ALLOWED_TRANSITIONS`.
4. Retire the currently published version in the same transaction.
5. Publish the target version.
6. Append a `data_asset_audit_logs` row with action, actor, reason, and
   old/new version identifiers under `details["old_version_id"]` and
   `details["new_version_id"]`.
7. Reject any attempt to update an already published version.

`update_candidate_metadata` must lock the version row, reject any version whose
status is not `imported`, update only the change note, and append a
`data_asset_audit_logs` row with action `update_candidate_metadata`.

`validate_version` must append a `data_asset_audit_logs` row with action
`validate`, the resulting status, and the validation error and warning codes.

`populate_candidate_version` must lock the version row, reject any version whose
status is not `imported`, persist `data_asset_records` or
`data_asset_rasters` for exactly that version, update `schema_summary`,
`record_count`, and `spatial_extent`, and leave the lifecycle status at
`imported`. For `NormalizedRasterData`, `source_path` is required and the
method calls
`save_raster_version(session, version_id, source_path, normalized)`. For table
or parameter data, reject a non-`None` `source_path`. Repeating population for
the same version and checksum is idempotent; a different checksum is rejected.

Create `backend/app/data_assets/snapshot_service.py`:

```python
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assessment.models import AssessmentRun
from app.config import settings
from app.data_assets.models import DataAssetSnapshot, DataAssetVersion
from app.data_assets.repository import AssetRaster, AssetRecord, DataAssetRepository
from app.data_assets.required_registry import (
    RequiredAssetRegistry,
    load_required_asset_registry,
)


@dataclass(frozen=True, slots=True)
class DataAssetSnapshotResult:
    snapshot_count: int
    missing_required: tuple[str, ...]
    fingerprint: str


class DataAssetSnapshotService:
    def __init__(
        self,
        *,
        registry: RequiredAssetRegistry | None = None,
        repository: DataAssetRepository | None = None,
    ) -> None:
        self._registry = registry or load_required_asset_registry(
            settings.data_asset_required_registry_path
        )
        self._repository = repository or DataAssetRepository()

    async def capture_required_assets(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        region_id: str,
        strict: bool,
    ) -> DataAssetSnapshotResult:
        if region_id != self._registry.region_id:
            raise ValueError("required asset registry does not match region_id")
        run_exists = await session.scalar(
            select(AssessmentRun.id)
            .where(AssessmentRun.id == run_id)
            .with_for_update()
        )
        if run_exists is None:
            raise LookupError("assessment run not found")
        existing = (
            await session.scalars(
                select(DataAssetSnapshot)
                .where(DataAssetSnapshot.run_id == run_id)
                .order_by(DataAssetSnapshot.asset_key)
            )
        ).all()
        if existing:
            locked = list(existing)
            locked_keys = {snapshot.asset_key for snapshot in locked}
            missing = tuple(
                sorted(set(self._registry.required) - locked_keys)
            )
            if strict and missing:
                raise LookupError(
                    f"required data assets are missing: {', '.join(missing)}"
                )
            fingerprint_items = sorted(
                (snapshot.asset_key, snapshot.version, snapshot.checksum)
                for snapshot in locked
            )
            payload = json.dumps(
                fingerprint_items,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return DataAssetSnapshotResult(
                snapshot_count=len(locked),
                missing_required=missing,
                fingerprint=hashlib.sha256(
                    payload.encode("utf-8")
                ).hexdigest(),
            )

        locked: list[DataAssetSnapshot] = []
        missing: list[str] = []
        assignments = (
            *((asset_key, "required") for asset_key in self._registry.required),
            *((asset_key, "optional") for asset_key in self._registry.optional),
        )
        now = datetime.now(UTC)
        for asset_key, role in assignments:
            version = await self._repository.get_published_version(
                session,
                asset_key=asset_key,
                region_id=region_id,
            )
            if version is None:
                if role == "required":
                    missing.append(asset_key)
                continue
            locked.append(
                DataAssetSnapshot(
                    run_id=run_id,
                    asset_id=version.asset_id,
                    asset_version_id=version.id,
                    asset_key=asset_key,
                    version=version.version,
                    checksum=version.checksum,
                    role=role,
                    required=role == "required",
                    created_at=now,
                )
            )
        missing_tuple = tuple(sorted(missing))
        if strict and missing_tuple:
            raise LookupError(
                f"required data assets are missing: {', '.join(missing_tuple)}"
            )
        for snapshot in locked:
            session.add(snapshot)
        fingerprint_items = sorted(
            (snapshot.asset_key, snapshot.version, snapshot.checksum)
            for snapshot in locked
        )
        payload = json.dumps(
            fingerprint_items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return DataAssetSnapshotResult(
            snapshot_count=len(locked),
            missing_required=missing_tuple,
            fingerprint=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )

    async def get_locked_version(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> DataAssetVersion | None:
        snapshot = await session.scalar(
            select(DataAssetSnapshot).where(
                DataAssetSnapshot.run_id == run_id,
                DataAssetSnapshot.asset_key == asset_key,
            )
        )
        if snapshot is None:
            return None
        return await session.get(DataAssetVersion, snapshot.asset_version_id)

    async def list_locked_records(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> list[AssetRecord]:
        version = await self.get_locked_version(
            session,
            run_id=run_id,
            asset_key=asset_key,
        )
        if version is None:
            return []
        return await self._repository.list_records(session, version.id)

    async def get_locked_raster(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        asset_key: str,
    ) -> AssetRaster | None:
        version = await self.get_locked_version(
            session,
            run_id=run_id,
            asset_key=asset_key,
        )
        if version is None:
            return None
        return await self._repository.get_raster(session, version.id)
```

Create `config/data_assets/shanghai-required-assets.yaml`:

```yaml
region_id: shanghai
required:
  - shanghai.admin.city
  - shanghai.admin.county
  - shanghai.admin.town
  - shanghai.population.town
  - shanghai.building.town
  - shanghai.economy.county
  - shanghai.loss.parameters
optional:
  - shanghai.fault
  - shanghai.gdp.raster
  - shanghai.dem.raster
```

Create `backend/app/data_assets/required_registry.py`:

```python
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RequiredAssetRegistry:
    region_id: str
    required: tuple[str, ...]
    optional: tuple[str, ...]


def load_required_asset_registry(path: str | Path) -> RequiredAssetRegistry:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("required asset registry must be a mapping")
    region_id = str(payload.get("region_id", "")).strip()
    required = tuple(str(item) for item in payload.get("required", ()))
    optional = tuple(str(item) for item in payload.get("optional", ()))
    if not region_id or not required:
        raise ValueError("required asset registry needs region_id and required keys")
    if set(required) & set(optional):
        raise ValueError("required and optional asset keys must not overlap")
    return RequiredAssetRegistry(region_id, required, optional)
```

`DataAssetSnapshotService.__init__` loads
`settings.data_asset_required_registry_path` by default and rejects a
`capture_required_assets` call whose `region_id` differs from the registry.
Tests may inject a temporary `RequiredAssetRegistry` so they do not depend on
the deployed `/config` path.

Create `backend/tests/test_data_asset_required_registry.py`:

```python
from pathlib import Path

import pytest

from app.data_assets.required_registry import load_required_asset_registry


def test_required_registry_distinguishes_required_and_optional(tmp_path: Path) -> None:
    path = tmp_path / "assets.yaml"
    path.write_text(
        "region_id: test\\nrequired: [a]\\noptional: [b]\\n",
        encoding="utf-8",
    )
    registry = load_required_asset_registry(path)
    assert registry.region_id == "test"
    assert registry.required == ("a",)
    assert registry.optional == ("b",)


def test_required_registry_rejects_overlap(tmp_path: Path) -> None:
    path = tmp_path / "assets.yaml"
    path.write_text(
        "region_id: test\\nrequired: [a]\\noptional: [a]\\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not overlap"):
        load_required_asset_registry(path)
```

Compute the fingerprint from sorted `(asset_key, version, checksum)` tuples:

```python
payload = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

If `strict=True` and required assets are missing, raise:

```python
raise LookupError(f"required data assets are missing: {', '.join(missing)}")
```

If `strict=False`, persist snapshots for all available published versions and return the missing list. The assessment run integration in Task 7 stores that list without failing the current intensity-only run.

Create `backend/app/data_assets/worker.py` only now that
`DataAssetService` exists. The file is executable without abbreviations:

```python
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.data_assets.geojson_importer import GeoJsonAssetImporter
from app.data_assets.import_jobs import (
    claim_next_import_job,
    complete_import_job,
    fail_import_job,
    reject_import_job,
)
from app.data_assets.mdb_importer import MdbAssetImporter
from app.data_assets.models import DataAsset, DataAssetImportJob
from app.data_assets.parameter_importer import ParameterFileImporter
from app.data_assets.raster_importer import GeoTiffAssetImporter
from app.data_assets.registry import get_asset_definition
from app.data_assets.service import DataAssetService
from app.data_assets.storage import ManagedFileStore
from app.db import SessionFactory

IMPORTERS = {
    "geojson": GeoJsonAssetImporter(),
    "parameter_file": ParameterFileImporter(),
    "mdb": MdbAssetImporter(),
    "geotiff": GeoTiffAssetImporter(),
}


async def process_import_job(
    session: AsyncSession,
    job: DataAssetImportJob,
) -> None:
    if job.asset_version_id is None:
        raise RuntimeError("data asset import job has no candidate version")
    asset = await session.get(DataAsset, job.asset_id)
    if asset is None:
        raise LookupError("data asset import job references a missing asset")
    definition = get_asset_definition(asset.asset_key)
    importer = IMPORTERS.get(job.file_format)
    if importer is None:
        await fail_import_job(
            session,
            job.id,
            ValueError(f"unsupported import format: {job.file_format}"),
        )
        return
    store = ManagedFileStore(
        settings.data_asset_storage_root,
        max_upload_bytes=settings.data_asset_max_upload_bytes,
    )
    path = store.resolve(job.managed_path)
    service = DataAssetService()
    try:
        async with session.begin_nested():
            normalized = importer.load(path, definition)
            await service.populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {
                    "file_format": job.file_format,
                    "record_count": getattr(normalized, "record_count", None),
                    "source_crs": getattr(normalized, "source_crs", None),
                    "importer": "data-asset-worker",
                },
                source_path=path,
            )
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor=job.requested_by,
            )
    except Exception as error:
        await fail_import_job(session, job.id, error)
        return
    if report.publishable:
        await complete_import_job(session, job.id, job.asset_version_id)
    else:
        await reject_import_job(session, job.id, report)


async def run_worker() -> None:
    while True:
        processed = False
        async with SessionFactory() as session:
            async with session.begin():
                job = await claim_next_import_job(session)
                if job is not None:
                    await process_import_job(session, job)
                    processed = True
        if not processed:
            await asyncio.sleep(settings.data_asset_worker_poll_seconds)


if __name__ == "__main__":
    asyncio.run(run_worker())
```

The nested transaction isolates failed importer or persistence writes while the
outer transaction remains usable for `fail_import_job`. `sanitize_error` keeps
the stored error summary free of connection strings, credentials, and absolute
host paths. A worker-level integration test is completed in Task 6 after the
API can enqueue a real upload.

Add `data_asset_worker_poll_seconds: float = 1.0` to
`backend/app/config.py`, reject non-positive values in the existing settings
validator, and add:

```dotenv
DATA_ASSET_WORKER_POLL_SECONDS=1.0
```

Add this service to `infra/compose.yaml`:

```yaml
  data-asset-worker:
    build:
      context: ../backend
    env_file:
      - ../.env
    command: ["python", "-m", "app.data_assets.worker"]
    restart: unless-stopped
    volumes:
      - ../backend:/app
      - ../config:/config
      - ${DATA_ASSET_STORAGE_HOST_DIR:-../data/data-assets}:/var/lib/data-assets
    depends_on:
      postgres:
        condition: service_healthy
```

- [ ] **Step 5: Run focused tests, concurrency tests, lint, and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_validation.py tests/test_data_asset_lifecycle.py tests/test_data_asset_required_registry.py tests/test_data_asset_repository.py tests/test_data_asset_snapshot_service.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/data_assets tests/test_data_asset_validation.py tests/test_data_asset_lifecycle.py tests/test_data_asset_required_registry.py tests/test_data_asset_repository.py tests/test_data_asset_snapshot_service.py
```

Expected: PASS, including one-published-version behavior under concurrent publish tests.

```bash
git add backend/app/data_assets backend/app/config.py backend/tests/test_data_asset_validation.py backend/tests/test_data_asset_lifecycle.py backend/tests/test_data_asset_required_registry.py backend/tests/test_data_asset_repository.py backend/tests/test_data_asset_snapshot_service.py infra/compose.yaml .env.example
git commit -m "feat: validate and publish data asset versions"
```

### Task 6: API, Permissions, and Audit Surface

**Files:**

- Create: `backend/app/data_assets/schemas.py`
- Create: `backend/app/data_assets/router.py`
- Create: `backend/tests/test_data_asset_api.py`
- Create: `backend/tests/test_data_asset_permissions.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/auth/models.py`
- Modify: `backend/app/data_assets/service.py`

**Interfaces:**

- Consumes: `DataAssetService`, `DataAssetRepository`, `DataAssetSnapshotService`, `ManagedFileStore`, and `require_role`.
- Produces:
  - `GET /api/v1/data-assets`
  - `POST /api/v1/data-assets/{asset_key}/import`
  - `GET /api/v1/data-asset-versions`
  - `GET /api/v1/data-asset-versions/{version_id}`
  - `POST /api/v1/data-asset-versions/{version_id}/validate`
  - `POST /api/v1/data-asset-versions/{version_id}/publish`
  - `POST /api/v1/data-asset-versions/{version_id}/retire`
  - `POST /api/v1/data-asset-versions/{version_id}/rollback`
  - `DataAssetService.list_assets(session: AsyncSession, *, region_id: str | None = None) -> list[AssetSummaryView]`
  - `DataAssetService.list_versions(session: AsyncSession, *, asset_key: str | None = None, region_id: str | None = None) -> list[AssetVersionView]`
  - `DataAssetService.get_version_detail(session: AsyncSession, version_id: UUID) -> AssetVersionDetailView`

- [ ] **Step 1: Write failing API and permission tests**

Create `backend/tests/test_data_asset_permissions.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.main import app


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("superadmin", 202),
        ("data_maintainer", 202),
        ("data_publisher", 202),
        ("viewer", 403),
        ("group_member", 403),
    ],
)
def test_import_role_matrix(role: str, status: int) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username=f"{role}-user",
        role=role,
        workgroup=None,
    )
    try:
        response = TestClient(app).post(
            "/api/v1/data-assets/shanghai.admin.town/import",
            data={
                "version": "2022.1",
                "source_uri": "https://example.gov.invalid/town.geojson",
                "change_note": "initial",
            },
            files={"file": ("town.geojson", b'{"type":"FeatureCollection","features":[]}', "application/geo+json")},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == status


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("superadmin", 200),
        ("data_publisher", 200),
        ("data_maintainer", 403),
        ("viewer", 403),
    ],
)
def test_publish_role_matrix(role: str, status: int) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username=f"{role}-user",
        role=role,
        workgroup=None,
    )
    try:
        response = TestClient(app).post(
            "/api/v1/data-asset-versions/00000000-0000-0000-0000-000000000001/publish",
            json={"reason": "approved"},
        )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code in {status, 404}
```

Create `backend/tests/test_data_asset_api.py` with real PostgreSQL/PostGIS:

```python
from tests.data_asset_helpers import wait_for_import_job


async def test_import_validate_publish_and_list(
    data_asset_client,
    geojson_town_file,
    session_factory,
) -> None:
    response = await data_asset_client.post(
        "/api/v1/data-assets/shanghai.admin.town/import",
        data={
            "version": "2022.1",
            "source_uri": "https://example.gov.invalid/town.geojson",
            "change_note": "initial import",
        },
        files={
            "file": (
                "town.geojson",
                geojson_town_file.read_bytes(),
                "application/geo+json",
            )
        },
    )
    assert response.status_code == 202
    version_id = await wait_for_import_job(
        session_factory,
        response.json()["job_id"],
    )

    validated = await data_asset_client.post(
        f"/api/v1/data-asset-versions/{version_id}/validate"
    )
    assert validated.status_code == 200
    assert validated.json()["status"] == "validated"

    published = await data_asset_client.post(
        f"/api/v1/data-asset-versions/{version_id}/publish",
        json={"reason": "verified against 2022 base data"},
    )
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    assets = await data_asset_client.get(
        "/api/v1/data-assets?region_id=shanghai"
    )
    assert assets.status_code == 200
    assert {item["region_id"] for item in assets.json()} == {"shanghai"}
    town = next(item for item in assets.json() if item["asset_key"] == "shanghai.admin.town")
    assert town["published_version"] == "2022.1"
```

- [ ] **Step 2: Run API tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_api.py tests/test_data_asset_permissions.py -v
```

Expected: FAIL because the data asset router and schemas do not exist.

- [ ] **Step 3: Implement response schemas and router**

Create `backend/app/data_assets/schemas.py` with:

```python
class AssetSummaryResponse(BaseModel):
    asset_key: str
    region_id: str
    name: str
    data_type: str
    spatial_granularity: str
    responsibility_unit: str
    update_interval_days: int
    is_core: bool
    published_version: str | None
    published_at: datetime | None
    update_due_at: datetime | None
    is_update_overdue: bool


class ImportAcceptedResponse(BaseModel):
    job_id: str
    asset_key: str
    version: str
    status: ImportJobStatus
    version_id: str


class ValidationIssueResponse(BaseModel):
    severity: str
    code: str
    message: str
    row_number: int | None
    field_name: str | None


class ValidationReportResponse(BaseModel):
    version_id: str
    status: AssetVersionStatus
    errors: list[ValidationIssueResponse]
    warnings: list[ValidationIssueResponse]
    statistics: dict
    checked_at: datetime


class AssetVersionResponse(BaseModel):
    id: str
    asset_key: str
    region_id: str
    version: str
    status: AssetVersionStatus
    source_uri: str
    license_name: str | None
    acquired_at: datetime | None
    valid_from: datetime | None
    valid_to: datetime | None
    quality_grade: str | None
    change_note: str | None
    schema_summary: dict
    record_count: int
    checksum: str
    imported_by: str
    reviewed_by: str | None
    imported_at: datetime
    validated_at: datetime | None
    published_at: datetime | None
    retired_at: datetime | None
    validation_errors: list[ValidationIssueResponse]
    validation_warnings: list[ValidationIssueResponse]


class LifecycleActionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
```

Create `backend/app/data_assets/router.py`:

```python
router = APIRouter(prefix="/api/v1", tags=["data-assets"])

_DATA_READ_ROLES = (
    "superadmin",
    "data_maintainer",
    "data_publisher",
    "group_leader",
    "group_deputy",
    "group_member",
    "viewer",
)
_DATA_WRITE_ROLES = ("superadmin", "data_maintainer", "data_publisher")
_DATA_PUBLISH_ROLES = ("superadmin", "data_publisher")


async def get_data_asset_service() -> DataAssetService:
    return DataAssetService()


def map_validation_report_response(
    report: ValidationReport,
) -> ValidationReportResponse:
    return ValidationReportResponse.model_validate(asdict(report))


def map_asset_version_response(
    view: AssetVersionView,
) -> AssetVersionResponse:
    payload = asdict(view)
    payload["id"] = str(payload.pop("version_id"))
    return AssetVersionResponse.model_validate(payload)


def map_asset_version_detail_response(
    view: AssetVersionDetailView,
) -> AssetVersionResponse:
    return map_asset_version_response(view.summary)


@router.get("/data-assets", response_model=list[AssetSummaryResponse])
async def list_data_assets(
    region_id: str = settings.data_asset_region_id,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    _current_user: object = Depends(require_role(*_DATA_READ_ROLES)),
) -> list[AssetSummaryResponse]:
    if not region_id.strip():
        raise HTTPException(status_code=422, detail="region_id must not be blank")
    views = await service.list_assets(session, region_id=region_id)
    return [AssetSummaryResponse.model_validate(asdict(view)) for view in views]


@router.post(
    "/data-assets/{asset_key}/import",
    response_model=ImportAcceptedResponse,
    status_code=202,
)
async def import_data_asset(
    asset_key: str,
    version: str = Form(...),
    source_uri: str = Form(...),
    license_name: str | None = Form(default=None),
    change_note: str = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_WRITE_ROLES)),
) -> ImportAcceptedResponse:
    definition = get_asset_definition(asset_key)
    normalized_source_uri = validate_source_uri(source_uri)
    upload_name = file.filename or "upload"
    file_format = source_format_for_file(upload_name, definition)
    store = ManagedFileStore(
        settings.data_asset_storage_root,
        max_upload_bytes=settings.data_asset_max_upload_bytes,
    )
    stored = store.store_upload(
        file.file,
        file_name=upload_name,
    )
    async with session.begin():
        job = await queue_import_job(
            session,
            QueueImportRequest(
                asset_key=asset_key,
                version=version,
                source_uri=normalized_source_uri,
                license_name=license_name,
                acquired_at=None,
                valid_from=None,
                valid_to=None,
                change_note=change_note,
                file_name=stored.file_name,
                file_format=file_format.value,
                file_size_bytes=stored.size_bytes,
                checksum=stored.checksum,
                relative_path=stored.relative_path,
                requested_by=current_user.username,
            ),
        )
        if job.asset_version_id is None:
            raise RuntimeError("import job has no candidate version")
        return ImportAcceptedResponse(
            job_id=str(job.id),
            asset_key=asset_key,
            version=version,
            status=job.status,
            version_id=str(job.asset_version_id),
        )


@router.get("/data-asset-versions", response_model=list[AssetVersionResponse])
async def list_data_asset_versions(
    asset_key: str | None = None,
    region_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    _current_user: object = Depends(require_role(*_DATA_READ_ROLES)),
) -> list[AssetVersionResponse]:
    views = await service.list_versions(
        session,
        asset_key=asset_key,
        region_id=region_id,
    )
    return [map_asset_version_response(view) for view in views]


@router.get("/data-asset-versions/{version_id}", response_model=AssetVersionResponse)
async def get_data_asset_version(
    version_id: UUID,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    _current_user: object = Depends(require_role(*_DATA_READ_ROLES)),
) -> AssetVersionResponse:
    view = await service.get_version_detail(session, version_id)
    return map_asset_version_detail_response(view)


@router.post("/data-asset-versions/{version_id}/validate", response_model=ValidationReportResponse)
async def validate_data_asset_version(
    version_id: UUID,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_WRITE_ROLES)),
) -> ValidationReportResponse:
    report = await service.validate_version(
        session,
        version_id,
        actor=current_user.username,
    )
    return map_validation_report_response(report)


@router.post("/data-asset-versions/{version_id}/publish", response_model=AssetVersionResponse)
async def publish_data_asset_version(
    version_id: UUID,
    request: LifecycleActionRequest,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_PUBLISH_ROLES)),
) -> AssetVersionResponse:
    version = await service.publish_version(
        session,
        version_id,
        current_user.username,
        request.reason,
    )
    return map_asset_version_response(version)


@router.post("/data-asset-versions/{version_id}/retire", response_model=AssetVersionResponse)
async def retire_data_asset_version(
    version_id: UUID,
    request: LifecycleActionRequest,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_PUBLISH_ROLES)),
) -> AssetVersionResponse:
    version = await service.retire_version(
        session,
        version_id,
        current_user.username,
        request.reason,
    )
    return map_asset_version_response(version)


@router.post("/data-asset-versions/{version_id}/rollback", response_model=AssetVersionResponse)
async def rollback_data_asset_version(
    version_id: UUID,
    request: LifecycleActionRequest,
    session: AsyncSession = Depends(get_session),
    service: DataAssetService = Depends(get_data_asset_service),
    current_user: object = Depends(require_role(*_DATA_PUBLISH_ROLES)),
) -> AssetVersionResponse:
    version = await service.rollback_version(
        session,
        version_id,
        current_user.username,
        request.reason,
    )
    return map_asset_version_response(version)
```

`source_format_for_file` is a router-private function that maps sanitized
filename suffixes to `SourceFormat`; it raises `UnsupportedImportFormat` for a
missing or unknown suffix. `map_asset_version_response`,
`map_asset_version_detail_response`, and `map_validation_report_response`
convert service DTOs to the Pydantic response models. The service layer must
not import or return the Pydantic models.

Add `data_asset_router` to `backend/app/main.py`:

```python
from app.data_assets.router import router as data_assets_router

app.include_router(data_assets_router)
```

- [ ] **Step 4: Implement status codes and error mapping**

Use these exact HTTP mappings:

```python
KeyError -> 404 {"detail": "data_asset_not_found"}
LookupError -> 404 {"detail": str(exc)}
ValueError -> 409 {"detail": str(exc)}
PermissionError -> 403 {"detail": "Insufficient permissions"}
SQLAlchemyError -> 503 {"detail": "data asset storage is unavailable"}
UnsupportedImportFormat -> 422 {"detail": str(exc)}
```

Validate `source_uri` as an absolute `http` or `https` URI with no embedded credentials. Sanitize all uploaded file names through `ManagedFileStore`. Ensure error responses never contain ODBC connection strings, database URLs, or local absolute file paths.

The `POST /import` path must:

1. Resolve the catalog definition by `asset_key`.
2. Stream the upload to `ManagedFileStore`.
3. Queue a `DataAssetImportJob`.
4. Return `202` immediately.
5. Never parse GeoJSON, MDB, or GeoTIFF inside the request handler.

- [ ] **Step 5: Run API tests, lint, and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_api.py tests/test_data_asset_permissions.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/data_assets app/main.py tests/test_data_asset_api.py tests/test_data_asset_permissions.py
```

Expected: PASS.

```bash
git add backend/app/data_assets backend/app/main.py backend/app/auth/models.py backend/tests/test_data_asset_api.py backend/tests/test_data_asset_permissions.py
git commit -m "feat: expose data asset management api"
```

The validate route passes `actor=current_user.username`. The import worker
passes `actor=job.requested_by`.

### Task 7: Assessment Run Snapshot Integration

**Files:**

- Modify: `config/data_assets/shanghai-required-assets.yaml`
- Create: `backend/tests/test_data_asset_run_snapshot.py`
- Modify: `backend/app/assessment/repository.py`
- Modify: `backend/app/assessment/schemas.py`
- Modify: `backend/app/assessment/router.py`
- Modify: `backend/app/data_assets/snapshot_service.py`
- Modify: `backend/app/config.py`
- Modify: `.env.example`
- Modify: `backend/tests/test_assessment_repository.py`
- Modify: `backend/tests/test_assessment_api.py`

**Interfaces:**

- Consumes: `AssessmentRepository.ensure_run_and_tasks()` and `DataAssetSnapshotService.capture_required_assets()`.
- Produces:
  - `AssessmentRepository(data_asset_snapshot_service: DataAssetSnapshotService | None = None)`
  - `assessment_runs.snapshot["data_asset_snapshot"]`
  - `assessment_runs.data_asset_snapshot_fingerprint`
  - `assessment_runs.data_asset_snapshot_result`

- [ ] **Step 1: Write failing run-snapshot tests**

Create `backend/tests/test_data_asset_run_snapshot.py`:

```python
from sqlalchemy import select

from app.assessment.repository import AssessmentRepository
from app.config import settings
from app.data_assets.models import DataAssetSnapshot
from tests.data_asset_helpers import publish_new_population_version


async def test_assessment_run_locks_available_assets_and_records_missing_required(
    session_factory,
    seeded_outbox,
    published_population_asset,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=seeded_outbox.event_id,
                revision_id=seeded_outbox.revision_id,
                outbox_id=seeded_outbox.outbox_id,
            )
            snapshots = (
                await session.scalars(
                    select(DataAssetSnapshot).where(DataAssetSnapshot.run_id == run.id)
                )
            ).all()

    assert len(snapshots) == 2
    assert {snapshot.asset_key for snapshot in snapshots} == {
        "shanghai.admin.town",
        "shanghai.population.town",
    }
    assert run.snapshot["region_id"] == settings.data_asset_region_id
    assert run.data_asset_snapshot_fingerprint
    assert "shanghai.building.town" in run.data_asset_snapshot_result["missing_required"]


async def test_data_update_after_run_does_not_change_locked_snapshot(
    session_factory,
    seeded_outbox,
    published_population_asset,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=seeded_outbox.event_id,
                revision_id=seeded_outbox.revision_id,
                outbox_id=seeded_outbox.outbox_id,
            )
            original = run.data_asset_snapshot_fingerprint

    await publish_new_population_version(session_factory, "2023.1")

    async with session_factory() as session:
        async with session.begin():
            stored = await session.get(type(run), run.id)
    assert stored.data_asset_snapshot_fingerprint == original
```

- [ ] **Step 2: Run the snapshot integration tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_run_snapshot.py -v
```

Expected: FAIL because assessment runs do not create data asset snapshots.

- [ ] **Step 3: Add the required-asset configuration and run fields**

Create `config/data_assets/shanghai-required-assets.yaml`:

```yaml
region_id: shanghai
required:
  - shanghai.admin.city
  - shanghai.admin.county
  - shanghai.admin.town
  - shanghai.population.town
  - shanghai.building.town
  - shanghai.economy.county
  - shanghai.loss.parameters
optional:
  - shanghai.fault
  - shanghai.gdp.raster
  - shanghai.dem.raster
```

The `data_asset_required_registry_path` setting and its non-empty validation
were added in Task 1.

The `AssessmentRun` columns and their migration operations were added in Task 1. Use them here; do not edit the already-applied `0012_data_asset_center` revision:

```python
data_asset_snapshot_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
data_asset_snapshot_result: Mapped[dict | None] = mapped_column(JSONB)
```

Task 1's migration adds these two columns to `assessment_runs`; its downgrade removes them.

- [ ] **Step 4: Capture snapshots inside the assessment run transaction**

Modify `AssessmentRepository.__init__`:

```python
from app.config import settings


def __init__(
    self,
    plan_builder: AssessmentPlanBuilder | None = None,
    data_asset_snapshot_service: DataAssetSnapshotService | None = None,
) -> None:
    self._plan_builder = plan_builder or AssessmentPlanBuilder()
    self._data_asset_snapshot_service = (
        data_asset_snapshot_service or DataAssetSnapshotService()
    )
```

After the new `AssessmentRun` and its tasks are flushed in `ensure_run_and_tasks`, call:

```python
snapshot_result = await self._data_asset_snapshot_service.capture_required_assets(
    session,
    run_id=run.id,
    region_id=settings.data_asset_region_id,
    strict=False,
)
run.data_asset_snapshot_fingerprint = snapshot_result.fingerprint
run.data_asset_snapshot_result = {
    "snapshot_count": snapshot_result.snapshot_count,
    "missing_required": list(snapshot_result.missing_required),
}
run.snapshot = {
    **dict(run.snapshot),
    "region_id": settings.data_asset_region_id,
    "data_asset_snapshot": run.data_asset_snapshot_result,
}
```

The call must remain in the same transaction as run creation. A repeated `ensure_run_and_tasks` for the same outbox must return the existing run without creating duplicate snapshots.

Modify `IntensityResultResponse` and `AssessmentRunStatusResponse` in
`backend/app/assessment/schemas.py`:

```python
data_asset_snapshot_fingerprint: str | None
data_asset_snapshot: dict | None
```

Populate the fields in `backend/app/assessment/router.py` rather than leaving
Pydantic defaults:

```python
# GET /api/v1/assessments/runs/{run_id}/intensity
data_asset_snapshot_fingerprint=run.data_asset_snapshot_fingerprint,
data_asset_snapshot=run.data_asset_snapshot_result,

# _run_response(...)
data_asset_snapshot_fingerprint=run.data_asset_snapshot_fingerprint,
data_asset_snapshot=run.data_asset_snapshot_result,

# The nested IntensityResultResponse in
# GET /api/v1/assessments/events/{event_id}/current
data_asset_snapshot_fingerprint=run.data_asset_snapshot_fingerprint,
data_asset_snapshot=run.data_asset_snapshot_result,
```

The nested `AssessmentRunStatusResponse.intensity` carries the same fingerprint
and snapshot payload as the parent run. Add API assertions for the direct
intensity route and the current-run route; both must return the persisted
fingerprint, and neither may synthesize an empty snapshot.

- [ ] **Step 5: Run assessment regression tests, snapshot tests, lint, and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_data_asset_run_snapshot.py tests/test_assessment_repository.py tests/test_assessment_api.py tests/test_assessment_temporal.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/assessment app/data_assets tests/test_data_asset_run_snapshot.py
```

Expected: PASS, with existing intensity behavior unchanged.

```bash
git add config/data_assets backend/app/assessment backend/app/data_assets/snapshot_service.py backend/app/config.py backend/tests/test_data_asset_run_snapshot.py backend/tests/test_assessment_repository.py backend/tests/test_assessment_api.py .env.example
git commit -m "feat: snapshot data assets for assessment runs"
```

### Task 8: Frontend Data Asset Management Page

**Files:**

- Create: `frontend/src/pages/DataAssetsPage.tsx`
- Create: `frontend/tests/data-assets.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/styles.css`

**Interfaces:**

- Consumes: data asset HTTP API and existing in-memory bearer token.
- Produces:
  - `listDataAssets(): Promise<DataAssetSummary[]>`
  - `listDataAssetVersions(assetKey?: string): Promise<DataAssetVersion[]>`
  - `importDataAsset(assetKey: string, input: DataAssetImportInput): Promise<DataAssetImportAccepted>`
  - `validateDataAssetVersion(versionId: string): Promise<DataAssetVersion>`
  - `publishDataAssetVersion(versionId: string, reason: string): Promise<DataAssetVersion>`
  - `retireDataAssetVersion(versionId: string, reason: string): Promise<DataAssetVersion>`
  - `rollbackDataAssetVersion(versionId: string, reason: string): Promise<DataAssetVersion>`
  - route `/data-assets`

- [ ] **Step 1: Write failing page tests**

Create `frontend/tests/data-assets.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import {
  importDataAsset,
  listDataAssetVersions,
  listDataAssets,
  publishDataAssetVersion,
} from "../src/api/client";
import { DataAssetsPage } from "../src/pages/DataAssetsPage";

vi.mock("../src/api/client", () => ({
  importDataAsset: vi.fn(),
  listDataAssetVersions: vi.fn(),
  listDataAssets: vi.fn(),
  publishDataAssetVersion: vi.fn(),
  validateDataAssetVersion: vi.fn(),
  retireDataAssetVersion: vi.fn(),
  rollbackDataAssetVersion: vi.fn(),
}));

const asset = {
  asset_key: "shanghai.admin.town",
  region_id: "shanghai",
  name: "上海市街镇边界",
  data_type: "vector",
  spatial_granularity: "town",
  responsibility_unit: "信息中心",
  update_interval_days: 365,
  is_core: true,
  published_version: "2022.1",
  published_at: "2026-09-28T00:00:00Z",
  update_due_at: "2027-09-28T00:00:00Z",
  is_update_overdue: false,
};

beforeEach(() => {
  vi.mocked(listDataAssets).mockReset();
  vi.mocked(listDataAssetVersions).mockReset();
  vi.mocked(importDataAsset).mockReset();
  vi.mocked(publishDataAssetVersion).mockReset();
});

test("renders assets and version lifecycle actions", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([
    {
      id: "version-1",
      asset_key: asset.asset_key,
      region_id: "shanghai",
      version: "2022.1",
      status: "published",
      source_uri: "https://example.gov.invalid/town.geojson",
      license_name: null,
      acquired_at: null,
      valid_from: null,
      valid_to: null,
      quality_grade: "L2",
      change_note: "initial",
      schema_summary: {},
      record_count: 212,
      checksum: "a".repeat(64),
      imported_by: "operator",
      reviewed_by: "reviewer",
      imported_at: "2026-09-28T00:00:00Z",
      validated_at: "2026-09-28T00:01:00Z",
      published_at: "2026-09-28T00:02:00Z",
      retired_at: null,
      validation_errors: [],
      validation_warnings: [],
    },
  ]);

  render(<DataAssetsPage userRole="data_publisher" />);

  expect(await screen.findByText("上海市街镇边界")).toBeInTheDocument();
  expect(screen.getByText("2022.1")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "停用" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "回滚" })).toBeInTheDocument();
});

test("submits a file and shows queued import", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([]);
  vi.mocked(importDataAsset).mockResolvedValue({
    job_id: "job-1",
    version_id: "version-1",
    asset_key: asset.asset_key,
    version: "2023.1",
    status: "queued",
  });

  render(<DataAssetsPage userRole="data_maintainer" />);
  await screen.findByText("上海市街镇边界");
  fireEvent.change(screen.getByLabelText("数据版本"), {
    target: { value: "2023.1" },
  });
  fireEvent.change(screen.getByLabelText("来源 URI"), {
    target: { value: "https://example.gov.invalid/town-2023.geojson" },
  });
  fireEvent.change(screen.getByLabelText("变更说明"), {
    target: { value: "2023 update" },
  });
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["{...}"], "town.geojson", { type: "application/geo+json" })] },
  });
  fireEvent.click(screen.getByRole("button", { name: "导入" }));

  await waitFor(() => expect(importDataAsset).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("导入任务已进入队列")).toBeInTheDocument();
});

test("hides publish actions from data maintainers", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([]);

  render(<DataAssetsPage userRole="data_maintainer" />);
  await screen.findByText("上海市街镇边界");

  expect(screen.queryByRole("button", { name: "发布" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "停用" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "回滚" })).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run the frontend test to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test -- tests/data-assets.test.tsx
```

Expected: FAIL because the page and API client functions do not exist.

- [ ] **Step 3: Implement types and authenticated API client methods**

Add to `frontend/src/types.ts`:

```ts
export type DataAssetType = "vector" | "table" | "raster" | "parameter";
export type DataAssetVersionStatus =
  | "imported"
  | "validated"
  | "published"
  | "retired"
  | "rejected";

export interface DataAssetSummary {
  asset_key: string;
  region_id: string;
  name: string;
  data_type: DataAssetType;
  spatial_granularity: string;
  responsibility_unit: string;
  update_interval_days: number;
  is_core: boolean;
  published_version: string | null;
  published_at: string | null;
  update_due_at: string | null;
  is_update_overdue: boolean;
}

export interface DataAssetVersion {
  id: string;
  asset_key: string;
  region_id: string;
  version: string;
  status: DataAssetVersionStatus;
  source_uri: string;
  license_name: string | null;
  acquired_at: string | null;
  valid_from: string | null;
  valid_to: string | null;
  quality_grade: string | null;
  change_note: string | null;
  schema_summary: Record<string, unknown>;
  record_count: number;
  checksum: string;
  imported_by: string;
  reviewed_by: string | null;
  imported_at: string;
  validated_at: string | null;
  published_at: string | null;
  retired_at: string | null;
  validation_errors: Array<{
    severity: string;
    code: string;
    message: string;
    row_number: number | null;
    field_name: string | null;
  }>;
  validation_warnings: Array<{
    severity: string;
    code: string;
    message: string;
    row_number: number | null;
    field_name: string | null;
  }>;
}

export interface DataAssetImportInput {
  version: string;
  source_uri: string;
  license_name?: string;
  change_note: string;
  file: File;
}
```

Add to `frontend/src/api/client.ts`:

```ts
function authHeaders(): Record<string, string> {
  if (!accessToken) {
    throw new ApiError("请先登录后管理数据资产", 401);
  }
  return { Authorization: `Bearer ${accessToken}` };
}

export async function listDataAssets(): Promise<DataAssetSummary[]> {
  return requestJson<DataAssetSummary[]>("/api/v1/data-assets", {
    headers: authHeaders(),
  });
}

export async function listDataAssetVersions(
  assetKey?: string,
): Promise<DataAssetVersion[]> {
  const query = assetKey ? `?asset_key=${encodeURIComponent(assetKey)}` : "";
  return requestJson<DataAssetVersion[]>(`/api/v1/data-asset-versions${query}`, {
    headers: authHeaders(),
  });
}

export async function importDataAsset(
  assetKey: string,
  input: DataAssetImportInput,
): Promise<DataAssetImportAccepted> {
  const body = new FormData();
  body.set("version", input.version);
  body.set("source_uri", input.source_uri);
  body.set("change_note", input.change_note);
  if (input.license_name) {
    body.set("license_name", input.license_name);
  }
  body.set("file", input.file);
  return requestJson<DataAssetImportAccepted>(
    `/api/v1/data-assets/${encodeURIComponent(assetKey)}/import`,
    {
      method: "POST",
      headers: authHeaders(),
      body,
    },
  );
}

export async function validateDataAssetVersion(
  versionId: string,
): Promise<ValidationReport> {
  return requestJson<ValidationReport>(
    `/api/v1/data-asset-versions/${encodeURIComponent(versionId)}/validate`,
    { method: "POST", headers: authHeaders() },
  );
}

export async function publishDataAssetVersion(
  versionId: string,
  reason: string,
): Promise<DataAssetVersion> {
  return requestJson<DataAssetVersion>(
    `/api/v1/data-asset-versions/${encodeURIComponent(versionId)}/publish`,
    {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
}

export async function retireDataAssetVersion(
  versionId: string,
  reason: string,
): Promise<DataAssetVersion> {
  return requestJson<DataAssetVersion>(
    `/api/v1/data-asset-versions/${encodeURIComponent(versionId)}/retire`,
    {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
}

export async function rollbackDataAssetVersion(
  versionId: string,
  reason: string,
): Promise<DataAssetVersion> {
  return requestJson<DataAssetVersion>(
    `/api/v1/data-asset-versions/${encodeURIComponent(versionId)}/rollback`,
    {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  );
}
```

Define `DataAssetImportAccepted` in `frontend/src/types.ts`:

```ts
export interface DataAssetImportAccepted {
  job_id: string;
  version_id: string;
  asset_key: string;
  version: string;
  status: "queued" | "running" | "completed" | "rejected" | "failed";
}

export interface ValidationIssue {
  severity: string;
  code: string;
  message: string;
  row_number: number | null;
  field_name: string | null;
}

export interface ValidationReport {
  version_id: string;
  status: string;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  statistics: Record<string, unknown>;
  checked_at: string;
}
```

- [ ] **Step 4: Implement the page, route, navigation, and restrained operational styling**

Create `frontend/src/pages/DataAssetsPage.tsx` with:

- Asset selector/list on the left.
- Selected asset facts on the right.
- Import form with labels `数据版本`, `来源 URI`, `许可证`, `变更说明`, `选择文件`.
- Version table with columns `版本`, `状态`, `质量`, `记录数`, `校验和`, `更新时间`, `操作`.
- Validation errors and warnings shown inline below the selected version.
- `data_publisher` and `superadmin` see `发布`, `停用`, and `回滚`.
- `data_maintainer` sees `导入`, `校验`, and version details only.
- Every irreversible lifecycle action opens a confirmation panel with a required `操作原因`.
- No online geometry editing, style editing, or ArcGIS publishing controls.

Use stable page structure:

```tsx
export function DataAssetsPage({ userRole }: { userRole: string }) {
  const canWrite = ["superadmin", "data_maintainer", "data_publisher"].includes(userRole);
  const canPublish = ["superadmin", "data_publisher"].includes(userRole);

  return (
    <section className="page-section" aria-labelledby="data-assets-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">数据治理</p>
          <h1 id="data-assets-title">数据资产中心</h1>
        </div>
      </header>
      {/* selected asset, import form, version table, lifecycle dialog */}
    </section>
  );
}
```

Modify `frontend/src/App.tsx`:

- Import `DataAssetsPage`.
- Add a `NavLink` to `/data-assets` labelled `数据资产`.
- Add `<Route path="/data-assets" element={<DataAssetsPage userRole={userRole} />} />`.
- Fetch `/api/v1/auth/me` after login and store `userRole` in component state. Use `viewer` only as a display fallback before `/me` resolves; never use the fallback to authorize an operation.

Add styles to `frontend/src/styles.css` using the existing variables:

```css
.data-asset-layout {
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  gap: 16px;
}

.data-asset-panel {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--surface);
  padding: 18px;
  box-shadow: var(--shadow);
}

.data-asset-status {
  display: inline-flex;
  border-radius: 3px;
  padding: 3px 7px;
  font-size: 12px;
  font-weight: 700;
}

.data-asset-status--published {
  background: var(--green-soft);
  color: var(--green);
}

.data-asset-status--validated {
  background: var(--blue-soft);
  color: var(--blue);
}

.data-asset-status--rejected,
.data-asset-status--retired {
  background: var(--red-soft);
  color: var(--red);
}
```

- [ ] **Step 5: Run frontend tests, typecheck, build, and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Expected: all frontend tests pass, TypeScript reports no errors, and Vite produces a production build.

```bash
git add frontend/src/pages/DataAssetsPage.tsx frontend/src/App.tsx frontend/src/api/client.ts frontend/src/types.ts frontend/src/styles.css frontend/tests/data-assets.test.tsx
git commit -m "feat: add data asset management page"
```

### Task 9: Runbook, Operational Verification, and Final Acceptance

**Files:**

- Create: `docs/runbooks/data-asset-center.md`
- Modify: `README.md`
- Modify: `docs/runbooks/assessment-orchestration.md`
- Modify: `backend/tests/test_migrations.py` only to keep the head assertion aligned.

**Interfaces:**

- Consumes: all earlier tasks.
- Produces: reproducible import, validation, publication, rollback, snapshot inspection, backup, and recovery procedures.

- [ ] **Step 1: Write the runbook with exact operating rules**

Create `docs/runbooks/data-asset-center.md` with these sections:

```markdown
# Data Asset Center Runbook

## Scope

## Required Configuration

## Starting The API And Import Worker

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres api data-asset-worker
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
```

## Importing GeoJSON

## Importing The Shanghai MDB On Windows

## Importing GDP And DEM GeoTIFF Files

## Validating A Candidate Version

## Publishing A Version

## Retiring And Rolling Back A Version

## Inspecting Import Failures

## Inspecting Evaluation Snapshots

## Update Overdue Detection

## Backup And Restore

## Verification Commands
```

Document these exact rules:

- The first catalog uses region `shanghai`, but all queries include `region_id`.
- MDB import runs on the Windows host because the Microsoft Access ODBC driver is not available in the Linux API container.
- The MDB is opened read-only and the importer never updates the source file.
- Raw files are content-addressed under `DATA_ASSET_STORAGE_ROOT`.
- Candidate versions cannot be used by an assessment until they are `published`.
- A correction or assessment run reads only versions in `data_asset_snapshots`.
- Retiring and rolling back do not modify historical snapshots.
- A failed validation retains the import job, validation errors, warnings, and source checksum.
- Backups must include PostgreSQL/PostGIS, especially `data_asset_records` and `data_asset_rasters`, and the host bind-mounted directory configured by `DATA_ASSET_STORAGE_HOST_DIR`.

Add these exact inspection queries:

```sql
SELECT da.asset_key, dav.version, dav.status, dav.quality_grade,
       dav.record_count, dav.checksum, dav.published_at
FROM data_assets da
JOIN data_asset_versions dav ON dav.asset_id = da.id
WHERE da.region_id = '<region-id>'
ORDER BY da.asset_key, dav.created_at DESC;

SELECT id, file_name, file_format, status, raw_checksum,
       validation_errors, validation_warnings, error_summary,
       started_at, completed_at
FROM data_asset_import_jobs
ORDER BY created_at DESC
LIMIT 20;

SELECT das.asset_key, das.version, das.checksum, das.role, das.required
FROM data_asset_snapshots das
WHERE das.run_id = '<run-id>'
ORDER BY das.asset_key;

SELECT COUNT(*) AS record_count
FROM data_asset_records
WHERE version_id = '<version-id>';

SELECT ST_Width(rast), ST_Height(rast), ST_SRID(rast), checksum
FROM data_asset_rasters
WHERE version_id = '<version-id>';
```

- [ ] **Step 2: Update README and assessment orchestration documentation**

Update `README.md` with:

- The data asset center’s implemented scope.
- The import format support and Windows MDB boundary.
- The roles `data_maintainer` and `data_publisher`.
- The fact that loss formulas remain future work even though asset snapshots are now captured.
- A link to `docs/runbooks/data-asset-center.md`.

Update `docs/runbooks/assessment-orchestration.md` with:

- `data_asset_snapshot_fingerprint` in the run query examples.
- The `data_asset_snapshot` JSON in the run response.
- A warning that missing required assets are recorded now and will become a loss-task failure when the loss implementation is activated.
- SQL that joins `assessment_runs` to `data_asset_snapshots`.

- [ ] **Step 3: Run the complete automated verification suite**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Expected:

- Compose configuration passes.
- Alembic head is `0012_data_asset_center`.
- All backend tests pass.
- Ruff passes.
- All frontend tests, typecheck, and production build pass.

Record the exact counts and command results in the runbook.

- [ ] **Step 4: Run real-data acceptance on the Z440 host**

Start Postgres and the API:

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres api data-asset-worker
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
```

Import the real MDB through the host-side CLI:

```powershell
.\scripts\run-mdb-import.ps1 `
  -AssetKey shanghai.population.town `
  -File "D:\地震应急辅助决策系统\基础数据\上海应急基础数据2022.mdb" `
  -Version "2022.1" `
  -SourceUri "https://example.gov.invalid/shanghai-base-data-2022" `
  -Actor "data-maintainer"
```

Verify the real expected counts:

```text
TOWN_CODE                  -> 212 records
TOWN_POPULATION            -> 213 raw records; 212 published town records after the configured district aggregate is excluded
TOWN_BUILDING              -> 212 records
COUNTY_POPULATION          -> 17 records
COUNTY_BUILDING            -> 17 records
economy                    -> 17 records
ACTIVEFAULT                -> 23 records
```

Use `GET /api/v1/data-asset-versions` to find each candidate version. Validate and publish the admin-town, population-town, building-town, and economy-county candidates. Confirm `GET /api/v1/data-assets` returns the published versions and update due dates.

Import GDP and DEM through the API or import worker, validate their raster dimensions and SRIDs, and publish them as background assets. They must not be marked as loss formula inputs.

- [ ] **Step 5: Run final spec coverage, placeholder scan, and commit**

Check the implementation against this coverage map:

```text
Spec 6 asset catalog/import/version storage -> Tasks 1-5
Spec 11.1 asset tables and constraints       -> Tasks 1, 5
Spec 13.1 asset APIs                         -> Task 6
Spec 14.1 management UI                      -> Task 8
Spec 17.3 data/version tests                 -> Tasks 1-5, 7
Spec 18 acceptance criteria                  -> Tasks 1-9
Spec 19 data asset deliverables              -> Tasks 1-9
```

Run:

```powershell
rg -n 'T[B]D|T[O]DO|implement[[:space:]]+later|fill[[:space:]]+in[[:space:]]+details|similar[[:space:]]+to[[:space:]]+Task' docs/superpowers/plans/2026-09-28-data-asset-center-implementation-plan.md
```

Expected: no matches.

```bash
git add README.md docs/runbooks/data-asset-center.md docs/runbooks/assessment-orchestration.md
git commit -m "docs: document data asset operations"
```
