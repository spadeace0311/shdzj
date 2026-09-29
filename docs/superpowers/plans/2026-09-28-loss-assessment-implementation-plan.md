# Loss Assessment Main Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved loss-assessment main chain from fused intensity through deterministic building, population, casualty, economic-loss, resource-demand, validation, persistence, Temporal orchestration, APIs, and frontend results.

**Architecture:** Keep the modular monolith. Add a pure `app/loss` domain package, use PostgreSQL/PostGIS for authoritative town exposure, spatial intersection, result storage, and derived 1 km rasters, and extend the existing `AssessmentWorkflow` instead of creating a second orchestration system. The completed data-asset center remains the only system that imports, validates, publishes, rolls back, and snapshots data assets.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, PostgreSQL 16/PostGIS 3.4 with PostGIS Raster, Alembic, Temporal Python 1.33, NumPy, PyProj, Rasterio, PyYAML, Pytest, Ruff, React 19, TypeScript, MapLibre GL JS, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-28-loss-assessment-design.md`

**Required prerequisite plan:** `docs/superpowers/plans/2026-09-28-data-asset-center-implementation-plan.md`

## Global Constraints

- The data-asset-center plan must be complete before Task 3 starts. Its migration `0013_data_asset_final_fixes` must already be the database head.
- Use the frozen `DataAssetService`, `DataAssetSnapshotService`, `data_asset_versions`, `data_asset_snapshots`, `data_asset_records`, and `data_asset_rasters` contracts. Do not create another importer, asset repository, publication workflow, record table, raster table, or snapshot table in `app/loss`.
- Keep the existing modular monolith, FastAPI, PostgreSQL/PostGIS, Temporal, and 1 km raster boundaries.
- The authoritative spatial result unit is the town. The 1 km result is a derived spatialized estimate and must be marked `spatialized_estimate=true`.
- Use the existing `AssessmentRun` and `AssessmentTask` records and the existing Temporal workflow.
- The five-minute acceptance clock starts at the latest formal or correction report ingestion time.
- The loss chain must complete within 300 seconds on the Z440 with real PostgreSQL/PostGIS; the normal target is within 180 seconds.
- Use EPSG:32651 for Shanghai area calculations and EPSG:4326 for external vector geometry.
- Core algorithms must not contain `if region == "shanghai"` or equivalent region branching.
- Production loss parameters may not be invented. The initial Shanghai parameter set must declare `reference_uncalibrated`, list source requirements for every numeric slot, and return unavailable values until a reviewed source supplies each coefficient.
- Tests that need arithmetic coefficients use `tests/fixtures/loss-test-parameters.yaml`, which must include `test_only: true`.
- Missing core exposure or a missing building vulnerability row makes the affected product unavailable and the run failed when the product is required.
- A missing resource coefficient makes only that resource kind `unavailable`; it does not set a zero quantity and does not fail other resource kinds.
- All published rows and versions are immutable. Recalculation creates a new run, product, or data-asset version.
- Data quality is `L1`, `L2`, `L3`, or `L0`; `L1` is prohibited until Shanghai historical-event calibration evidence exists.
- Low, central, and high values are parameter scenarios, not statistical confidence intervals.
- Money is stored in RMB yuan; area is stored in square metres; population and casualty estimates retain unrounded internal values.
- `LossModelType` contains only calculable loss models; `LossProductType` additionally contains `validation`.
- `loss_metric_values.numeric_value` is nullable. `value_status` distinguishes `available`, `zero`, `rounded_to_zero`, `unavailable`, and `not_applicable`; a missing resource is stored with `NULL`, never zero.
- `loss_model_definitions` and `loss_parameter_sets` are schema-only control-plane tables in this phase. Runtime model resolution uses `RegionLossProfile` plus the locked profile-selected loss-parameter data asset; no loss service writes these tables until a later registry-management plan.
- All behavior changes use red-green TDD and end in a focused commit.
- Do not read, print, commit, log, or expose values from `.env`, API keys, passwords, tokens, connection strings, or credentials.

---

## Prerequisite Contracts From the Data Asset Center

The completed `app/data_assets` subsystem exposes these frozen call contracts. The loss plan imports these functions and services and does not duplicate them:

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

Required asset keys are:

```text
shanghai.admin.city
shanghai.admin.county
shanghai.admin.town
shanghai.population.town
shanghai.building.town
shanghai.economy.county
shanghai.loss.parameters
```

Optional asset keys are:

```text
shanghai.fault
shanghai.gdp.raster
shanghai.dem.raster
```

## File Structure

```text
backend/app/loss/
  __init__.py
  domain.py
  models.py
  models_registry.py
  region.py
  asset_bridge.py
  exposure.py
  spatial.py
  buildings.py
  population.py
  casualties.py
  economic.py
  resources.py
  validation.py
  artifacts.py
  repository.py
  service.py
  schemas.py
  router.py
```

Domain modules never import FastAPI, Temporal, SQLAlchemy, GeoAlchemy, or PostGIS. The router never calculates losses. The Temporal activity layer calls `LossAssessmentService` only.

---

### Task 1: Loss Schema, ORM Models, and Migration

**Files:**
- Create: `backend/migrations/versions/0014_loss_assessment.py`
- Create: `backend/app/loss/__init__.py`
- Create: `backend/app/loss/models.py`
- Create: `backend/tests/test_loss_schema.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: existing `assessment_runs`, `assessment_tasks`, `data_asset_versions`, `data_asset_snapshots`, `data_asset_records`, and `data_asset_rasters`.
- Produces ORM classes:
  - `LossModelDefinition`
  - `LossParameterSet`
  - `LossProduct`
  - `LossMetricValue`
  - `LossProductRaster`
- Produces table names:
  - `loss_model_definitions`
  - `loss_parameter_sets`
  - `loss_products`
  - `loss_metric_values`
  - `loss_product_rasters`

- [ ] **Step 1: Write failing schema and migration tests**

Create `backend/tests/test_loss_schema.py`:

```python
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


async def test_loss_tables_and_constraints_exist() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            assert {
                "loss_model_definitions",
                "loss_parameter_sets",
                "loss_products",
                "loss_metric_values",
                "loss_product_rasters",
            } <= tables
            constraints = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conrelid IN (
                            'loss_model_definitions'::regclass,
                            'loss_parameter_sets'::regclass,
                            'loss_products'::regclass,
                            'loss_metric_values'::regclass,
                            'loss_product_rasters'::regclass
                        )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {
        row["conname"]: row["definition"]
        for row in constraints
    }
    assert "UNIQUE (model_id, formula_version)" in definitions[
        "uq_loss_model_formula"
    ]
    assert "UNIQUE (parameter_set_id, version)" in definitions[
        "uq_loss_parameter_version"
    ]
    assert "UNIQUE (run_id, product_type)" in definitions[
        "uq_loss_product_run_type"
    ]
    assert "UNIQUE (product_id, area_scope, area_code, metric_key, value_type)" in (
        definitions["uq_loss_metric_value"]
    )
    assert "UNIQUE (product_id, raster_version)" in definitions[
        "uq_loss_product_raster_version"
    ]


async def test_loss_product_uses_known_status_and_quality_values() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conname IN (
                            'ck_loss_product_status_value',
                            'ck_loss_product_quality_value'
                        )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {row["conname"]: row["definition"] for row in rows}
    status_definition = definitions["ck_loss_product_status_value"]
    for value in ("complete", "partial", "unavailable", "invalid"):
        assert f"'{value}'" in status_definition
    quality_definition = definitions["ck_loss_product_quality_value"]
    for value in ("L1", "L2", "L3", "L0"):
        assert f"'{value}'" in quality_definition
```

Modify `backend/tests/test_migrations.py`:

```python
LATEST_REVISION = "0014_loss_assessment"
LOSS_PREVIOUS_REVISION = "0013_data_asset_final_fixes"
LOSS_TABLES = {
    "loss_model_definitions",
    "loss_parameter_sets",
    "loss_products",
    "loss_metric_values",
    "loss_product_rasters",
}


async def _loss_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
    finally:
        await engine.dispose()
    return LOSS_TABLES <= names


async def test_0014_loss_assessment_is_reversible() -> None:
    _set_revision(LOSS_PREVIOUS_REVISION)
    assert await _loss_tables_exist() is False
    try:
        _set_revision(LATEST_REVISION)
        assert await _loss_tables_exist() is True
        _set_revision(LOSS_PREVIOUS_REVISION)
        assert await _loss_tables_exist() is False
        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)
```

- [ ] **Step 2: Run the migration and schema tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_schema.py tests/test_migrations.py -v
```

Expected: FAIL because migration `0014_loss_assessment` and the loss tables do not exist.

- [ ] **Step 3: Implement the migration and ORM models**

Create `backend/migrations/versions/0014_loss_assessment.py` with `revision = "0014_loss_assessment"` and `down_revision = "0013_data_asset_final_fixes"`.

Create the five tables with these required columns:

```python
loss_model_definitions:
    id, model_id, model_type, formula_version, applicable_region,
    applicable_admin_levels, input_contract, output_contract,
    source_citations, calibration_status, is_default, status,
    created_at, published_at

loss_parameter_sets:
    id, parameter_set_id, version, model_id, formula_version,
    region_id, calibration_status, quality_grade, scenarios,
    parameters, provenance, checksum, status, effective_at,
    created_at, published_at

loss_products:
    id, run_id, task_id, product_type, status, quality_grade,
    calibration_status, coverage_ratio, partial_scope,
    needs_review, spatialized_estimate, algorithm_version,
    parameter_version, region_profile_version, input_fingerprint,
    input_checksum, output_checksum, statistics, reason,
    created_at, completed_at, published_at

loss_metric_values:
    id, product_id, area_scope, area_code, area_name,
    metric_key, value_type, value_status, numeric_value, unit, precision,
    quality_grade, note, created_at

loss_product_rasters:
    id, product_id, raster_version, rast, band_manifest,
    checksum, width, height, srid, spatial_allocation_rule,
    coverage_ratio, created_at
```

Use check constraints:

```python
sa.CheckConstraint(
    "status IN ('complete','partial','unavailable','invalid')",
    name="ck_loss_product_status_value",
)
sa.CheckConstraint(
    "quality_grade IN ('L1','L2','L3','L0')",
    name="ck_loss_product_quality_value",
)
sa.CheckConstraint(
    "product_type IN "
    "('building_damage','population_impact','casualties',"
    "'economic_loss','resource_demand','validation')",
    name="ck_loss_product_type",
)
sa.CheckConstraint(
    "value_type IN ('low','central','high')",
    name="ck_loss_metric_value_type",
)
sa.CheckConstraint(
    "value_status IN "
    "('available','zero','rounded_to_zero','unavailable','not_applicable')",
    name="ck_loss_metric_value_status",
)
sa.CheckConstraint(
    """
    (value_status IN ('available','zero','rounded_to_zero')
     AND numeric_value IS NOT NULL AND numeric_value >= 0)
    OR
    (value_status IN ('unavailable','not_applicable')
     AND numeric_value IS NULL)
    """,
    name="ck_loss_metric_value_presence",
)
sa.CheckConstraint(
    "(value_status = 'zero' AND numeric_value = 0) "
    "OR value_status <> 'zero'",
    name="ck_loss_metric_zero_value",
)
```

Create `backend/app/loss/models.py` with SQLAlchemy mappings matching the migration and use `UUID(as_uuid=True)` primary keys, PostgreSQL `JSONB`, PostGIS `Geometry` for product extent if included, and PostGIS `Raster`.

Modify `backend/migrations/env.py` to import and register the loss metadata:

```python
from app.loss import models as loss_models  # noqa: F401
```

- [ ] **Step 4: Run the focused tests**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_schema.py tests/test_migrations.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss tests/test_loss_schema.py tests/test_migrations.py
```

Expected: PASS and Alembic head is `0014_loss_assessment`.

- [ ] **Step 5: Commit**

```bash
git add backend/migrations/versions/0014_loss_assessment.py backend/migrations/env.py backend/app/loss/__init__.py backend/app/loss/models.py backend/tests/test_loss_schema.py backend/tests/test_migrations.py
git commit -m "feat: add loss assessment schema"
```

### Task 2: Loss Domain Contracts, Model Registry, and Parameter Sets

**Files:**
- Create: `backend/app/loss/domain.py`
- Create: `backend/app/loss/models_registry.py`
- Create: `config/loss/shanghai-reference-uncalibrated.yaml`
- Create: `backend/tests/fixtures/loss-test-parameters.yaml`
- Create: `backend/tests/test_loss_domain.py`
- Create: `backend/tests/test_loss_models_registry.py`

**Interfaces:**
- Produces:
  - `LossModelType`
  - `LossProductType`
  - `LossProductStatus`
  - `LossQualityGrade`
  - `LossCalibrationStatus`
  - `LossValueType`
  - `LossMetricValueStatus`
  - `DamageState`
  - `BuildingStructure`
  - `ResourceKind`
  - `LossRunContext`
  - `ModelDefinition`
  - `ParameterSet`
  - `ScenarioParameters`
  - `LossMetric`
  - `LossValue`
  - `LossProductResult`
  - `LossModelRegistry.register(definition) -> None`
  - `LossModelRegistry.register_defaults(parameter_set: ParameterSet) -> None`
  - `LossModelRegistry.resolve(model_type: LossModelType, formula_version: str) -> ModelDefinition`
  - `LossParametersUnavailable(ValueError)`
  - `load_parameter_set(path: str | Path) -> ParameterSet`
  - `load_parameter_set_from_mapping(payload: Mapping[str, object], *, checksum: str) -> ParameterSet`
  - `canonical_intensity_bin(value: int | float | str) -> str`
  - `require_parameters(parameter_set: ParameterSet, model_type: LossModelType, scenario: LossValueType, names: tuple[str, ...]) -> dict[str, float]`

- [ ] **Step 1: Write failing contract and registry tests**

Create `backend/tests/test_loss_domain.py`:

```python
from app.loss.domain import (
    BuildingStructure,
    DamageState,
    LossCalibrationStatus,
    LossModelType,
    LossProductType,
    LossProductStatus,
    LossQualityGrade,
    LossValueType,
    LossMetricValueStatus,
    ResourceKind,
)
from app.loss.models_registry import canonical_intensity_bin


def test_loss_enums_have_stable_external_values() -> None:
    assert LossModelType.BUILDING_DAMAGE == "building_damage"
    assert LossProductType.VALIDATION == "validation"
    assert LossProductType.BUILDING_DAMAGE == "building_damage"
    assert LossProductStatus.COMPLETE == "complete"
    assert LossProductStatus.UNAVAILABLE == "unavailable"
    assert LossQualityGrade.L2 == "L2"
    assert LossCalibrationStatus.REFERENCE_UNCALIBRATED == "reference_uncalibrated"
    assert LossValueType.CENTRAL == "central"
    assert LossMetricValueStatus.UNAVAILABLE == "unavailable"
    assert LossMetricValueStatus.ROUNDED_TO_ZERO == "rounded_to_zero"
    assert DamageState.SEVERELY_DAMAGED == "severely_damaged"
    assert BuildingStructure.RC_FRAME == "rc_frame"
    assert ResourceKind.SICKBED == "sickbed"


def test_intensity_bin_has_one_canonical_key_format() -> None:
    assert canonical_intensity_bin(7) == "7"
    assert canonical_intensity_bin("7") == "7"
    assert canonical_intensity_bin("VII") == "7"
    assert canonical_intensity_bin("vii") == "7"
```

Create `backend/tests/test_loss_models_registry.py`:

```python
from pathlib import Path

import pytest

from app.loss.domain import LossModelType, LossValueType
from app.loss.models_registry import (
    LossModelRegistry,
    LossParametersUnavailable,
    load_parameter_set,
    require_parameters,
)


PRODUCTION_PARAMETERS = Path("/config/loss/shanghai-reference-uncalibrated.yaml")
TEST_PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")


def test_production_parameter_set_is_explicitly_uncalibrated() -> None:
    parameter_set = load_parameter_set(PRODUCTION_PARAMETERS)
    assert parameter_set.version == "shanghai-loss-reference-v1"
    assert parameter_set.calibration_status.value == "reference_uncalibrated"
    assert parameter_set.provenance["requires_review"] is True
    assert parameter_set.models[LossModelType.BUILDING_DAMAGE].source_requirements


def test_missing_numeric_parameter_is_unavailable_not_zero() -> None:
    parameter_set = load_parameter_set(PRODUCTION_PARAMETERS)
    with pytest.raises(LossParametersUnavailable):
        require_parameters(
            parameter_set,
            LossModelType.BUILDING_DAMAGE,
            LossValueType.CENTRAL,
            ("vulnerability.rc_frame.7.slightly_damaged",),
        )


def test_registry_resolves_default_and_version() -> None:
    registry = LossModelRegistry()
    registry.register_defaults(load_parameter_set(TEST_PARAMETERS))
    definition = registry.resolve(
        LossModelType.BUILDING_DAMAGE,
        "building-structure-matrix-v1",
    )
    assert definition.is_default is True
    assert definition.formula_version == "building-structure-matrix-v1"
```

- [ ] **Step 2: Run the focused tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_domain.py tests/test_loss_models_registry.py -v
```

Expected: FAIL because `app.loss.domain`, `app.loss.models_registry`, and the parameter files do not exist.

- [ ] **Step 3: Implement immutable contracts and parameter loading**

Create `backend/app/loss/domain.py` with `StrEnum` values:

```python
class LossModelType(StrEnum):
    BUILDING_DAMAGE = "building_damage"
    POPULATION_IMPACT = "population_impact"
    CASUALTIES = "casualties"
    ECONOMIC_LOSS = "economic_loss"
    RESOURCE_DEMAND = "resource_demand"


class LossProductType(StrEnum):
    BUILDING_DAMAGE = "building_damage"
    POPULATION_IMPACT = "population_impact"
    CASUALTIES = "casualties"
    ECONOMIC_LOSS = "economic_loss"
    RESOURCE_DEMAND = "resource_demand"
    VALIDATION = "validation"


class LossProductStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class LossQualityGrade(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L0 = "L0"


class LossCalibrationStatus(StrEnum):
    CALIBRATED = "calibrated"
    REFERENCE_UNCALIBRATED = "reference_uncalibrated"
    UNCALIBRATED = "uncalibrated"


class LossValueType(StrEnum):
    LOW = "low"
    CENTRAL = "central"
    HIGH = "high"


class LossMetricValueStatus(StrEnum):
    AVAILABLE = "available"
    ZERO = "zero"
    ROUNDED_TO_ZERO = "rounded_to_zero"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class DamageState(StrEnum):
    BASIC = "basic"
    SLIGHTLY_DAMAGED = "slightly_damaged"
    MODERATELY_DAMAGED = "moderately_damaged"
    SEVERELY_DAMAGED = "severely_damaged"
    COLLAPSED = "collapsed"


class BuildingStructure(StrEnum):
    HIGH_RISE = "high_rise"
    RC_FRAME = "rc_frame"
    MASONRY = "masonry"
    SINGLE_STOREY = "single_storey"
    OTHER = "other"


class ResourceKind(StrEnum):
    RESCUE_TEAM = "rescue_team"
    MEDICAL_TEAM = "medical_team"
    EPIDEMIC_TEAM = "epidemic_team"
    TENT = "tent"
    DRINKING_WATER = "drinking_water"
    TOILET = "toilet"
    CLOTHING = "clothing"
    QUILT = "quilt"
    FOOD = "food"
    BLANKET = "blanket"
    STRETCHER = "stretcher"
    SICKBED = "sickbed"
```

Add frozen dataclasses:

```python
@dataclass(frozen=True, slots=True)
class LossRunContext:
    run_id: str
    event_id: str
    revision_id: str
    report_ingested_at: datetime
    region_id: str
    region_profile_version: str
    minimum_town_coverage_ratio: float
    grid_residual_review_threshold: float
    fused_intensity_product_id: str
    fused_intensity_checksum: str
    data_asset_snapshot_checksum: str


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    model_id: str
    model_type: LossModelType
    formula_version: str
    applicable_region: str
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    source_citations: tuple[str, ...]
    calibration_status: LossCalibrationStatus
    is_default: bool


@dataclass(frozen=True, slots=True)
class ScenarioParameters:
    values: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ParameterSetModel:
    model_id: str
    formula_version: str
    applicable_region: str
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    source_citations: tuple[str, ...]
    source_requirements: tuple[str, ...]
    scenarios: Mapping[LossValueType, ScenarioParameters]


@dataclass(frozen=True, slots=True)
class ParameterSet:
    version: str
    calibration_status: LossCalibrationStatus
    provenance: Mapping[str, object]
    models: Mapping[LossModelType, ParameterSetModel]
    checksum: str
    test_only: bool


@dataclass(frozen=True, slots=True)
class LossValue:
    value_type: LossValueType
    value_status: LossMetricValueStatus
    numeric_value: float | None
    quality_grade: LossQualityGrade
    note: str | None = None


@dataclass(frozen=True, slots=True)
class LossMetric:
    area_scope: str
    area_code: str
    area_name: str | None
    metric_key: str
    unit: str
    precision: int | None
    values: tuple[LossValue, ...]


@dataclass(frozen=True, slots=True)
class LossProductResult:
    product_type: LossProductType
    status: LossProductStatus
    quality_grade: LossQualityGrade
    calibration_status: LossCalibrationStatus
    coverage_ratio: float
    partial_scope: bool
    needs_review: bool
    spatialized_estimate: bool
    algorithm_version: str
    parameter_version: str
    region_profile_version: str
    input_fingerprint: str
    input_checksum: str
    statistics: Mapping[str, object]
    metrics: tuple[LossMetric, ...]
    raster_checksum: str | None = None
    reason: str | None = None
```

Create `config/loss/shanghai-reference-uncalibrated.yaml` with no invented numeric coefficients:

```yaml
version: "shanghai-loss-reference-v1"
calibration_status: "reference_uncalibrated"
test_only: false
provenance:
  requires_review: true
  owner: "上海市地震局信息中心应急技术组"
  review_requirement: "每项系数必须登记来源、版本、适用震级烈度、单位、有效日期和审核人"
models:
  building_damage:
    model_id: "building-structure-matrix-v1"
    formula_version: "building-structure-matrix-v1"
    applicable_region: "shanghai"
    input_contract:
      - "exposure"
      - "town_intensity_shares"
      - "parameter_set"
    output_contract:
      - "building_damage"
    source_citations: []
    source_requirements:
      - "逐结构、逐烈度、逐破坏状态易损性比例"
      - "结构面积字段与年代结构映射"
      - "上海历史震例校核说明"
    scenarios:
      low: {}
      central: {}
      high: {}
  population_impact:
    model_id: "population-intensity-v1"
    formula_version: "population-intensity-v1"
    applicable_region: "shanghai"
    input_contract:
      - "exposure"
      - "town_intensity_shares"
      - "parameter_set"
    output_contract:
      - "population_impact"
    source_citations: []
    source_requirements:
      - "受灾人口起始烈度"
      - "逐烈度紧急安置比例"
      - "临时避难人口比例"
    scenarios:
      low: {}
      central: {}
      high: {}
  casualties:
    model_id: "casualty-building-intensity-v1"
    formula_version: "casualty-building-intensity-v1"
    applicable_region: "shanghai"
    input_contract:
      - "building_damage"
      - "population_impact"
      - "parameter_set"
    output_contract:
      - "casualties"
    source_citations: []
    source_requirements:
      - "死亡模型 a、b、c 系数"
      - "逐烈度死伤比"
      - "逐破坏状态压埋面积系数"
      - "城市类型压埋除数和时段系数"
    scenarios:
      low: {}
      central: {}
      high: {}
  economic_loss:
    model_id: "economic-building-loss-v1"
    formula_version: "economic-building-loss-v1"
    applicable_region: "shanghai"
    input_contract:
      - "building_damage"
      - "parameter_set"
    output_contract:
      - "economic_loss"
    source_citations: []
    source_requirements:
      - "逐结构重置单价"
      - "逐结构单位面积室内财产值"
      - "逐破坏状态损失比"
    scenarios:
      low: {}
      central: {}
      high: {}
  resource_demand:
    model_id: "resource-linear-demand-v1"
    formula_version: "resource-linear-demand-v1"
    applicable_region: "shanghai"
    input_contract:
      - "building_damage"
      - "population_impact"
      - "casualties"
      - "parameter_set"
    output_contract:
      - "resource_demand"
    source_citations: []
    source_requirements:
      - "每类资源的基础量或输入权重及单位"
      - "资源参数的适用场景和有效日期"
    scenarios:
      low: {}
      central: {}
      high: {}
```

Create the test fixture with synthetic values and:

```yaml
version: "loss-test-only-v1"
calibration_status: "uncalibrated"
test_only: true
provenance:
  purpose: "synthetic equation test"
  requires_review: false
x-building-low: &building-low
  vulnerability.rc_frame.7.basic: 0.70
  vulnerability.rc_frame.7.slightly_damaged: 0.15
  vulnerability.rc_frame.7.moderately_damaged: 0.10
  vulnerability.rc_frame.7.severely_damaged: 0.04
  vulnerability.rc_frame.7.collapsed: 0.01
x-building-central: &building-central
  vulnerability.rc_frame.7.basic: 0.60
  vulnerability.rc_frame.7.slightly_damaged: 0.20
  vulnerability.rc_frame.7.moderately_damaged: 0.12
  vulnerability.rc_frame.7.severely_damaged: 0.06
  vulnerability.rc_frame.7.collapsed: 0.02
x-building-high: &building-high
  vulnerability.rc_frame.7.basic: 0.45
  vulnerability.rc_frame.7.slightly_damaged: 0.25
  vulnerability.rc_frame.7.moderately_damaged: 0.16
  vulnerability.rc_frame.7.severely_damaged: 0.10
  vulnerability.rc_frame.7.collapsed: 0.04
x-resource-coefficients: &resource-coefficients
  rescue_team.rescuer_per_missing: 0.002
  medical_team.team_per_injury: 0.005
  epidemic_team.team_per_shelter_10000: 0.1
  tent.per_shelter_population: 0.01
  drinking_water.per_affected_population: 0.5
  toilet.per_shelter_population: 0.005
  clothing.per_affected_population: 0.1
  quilt.per_shelter_population: 0.1
  food.per_affected_population: 0.2
  blanket.per_affected_population: 0.1
  stretcher.per_injury: 0.02
  sickbed.per_injury: 0.05
x-resource-low: &resource-low
  <<: *resource-coefficients
  rescue_team.base: 1
  medical_team.base: 1
  epidemic_team.base: 1
  tent.base: 10
  drinking_water.base: 100
  toilet.base: 10
  clothing.base: 100
  quilt.base: 100
  food.base: 100
  blanket.base: 100
  stretcher.base: 10
  sickbed.base: 10
x-resource-central: &resource-central
  <<: *resource-coefficients
  rescue_team.base: 2
  medical_team.base: 2
  epidemic_team.base: 2
  tent.base: 20
  drinking_water.base: 200
  toilet.base: 20
  clothing.base: 200
  quilt.base: 200
  food.base: 200
  blanket.base: 200
  stretcher.base: 20
  sickbed.base: 20
x-resource-high: &resource-high
  <<: *resource-coefficients
  rescue_team.base: 3
  medical_team.base: 3
  epidemic_team.base: 3
  tent.base: 30
  drinking_water.base: 300
  toilet.base: 30
  clothing.base: 300
  quilt.base: 300
  food.base: 300
  blanket.base: 300
  stretcher.base: 30
  sickbed.base: 30
models:
  building_damage:
    model_id: "building-structure-matrix-v1"
    formula_version: "building-structure-matrix-v1"
    applicable_region: "shanghai"
    input_contract: ["exposure", "town_intensity_shares", "parameter_set"]
    output_contract: ["building_damage"]
    source_citations: ["synthetic_equation_test"]
    source_requirements: []
    scenarios:
      low: {values: *building-low}
      central: {values: *building-central}
      high: {values: *building-high}
  population_impact:
    model_id: "population-intensity-v1"
    formula_version: "population-intensity-v1"
    applicable_region: "shanghai"
    input_contract: ["exposure", "town_intensity_shares", "parameter_set"]
    output_contract: ["population_impact"]
    source_citations: ["synthetic_equation_test"]
    source_requirements: []
    scenarios:
      low:
        values:
          affected_population_min_intensity: 6
          shelter_ratio.6: 0.03
          shelter_ratio.7: 0.08
          temporary_shelter_ratio: 0.40
      central:
        values:
          affected_population_min_intensity: 6
          shelter_ratio.6: 0.05
          shelter_ratio.7: 0.10
          temporary_shelter_ratio: 0.50
      high:
        values:
          affected_population_min_intensity: 6
          shelter_ratio.6: 0.08
          shelter_ratio.7: 0.15
          temporary_shelter_ratio: 0.60
  casualties:
    model_id: "casualty-building-intensity-v1"
    formula_version: "casualty-building-intensity-v1"
    applicable_region: "shanghai"
    input_contract: ["building_damage", "population_impact", "parameter_set"]
    output_contract: ["casualties"]
    source_citations: ["synthetic_equation_test"]
    source_requirements: []
    scenarios:
      low:
        values:
          death.a: 0.020
          death.b: 0.00
          death.c: 0.00
          injury_to_death_ratio.7: 1.2
          buried.state.basic: 0.00
          buried.state.slightly_damaged: 0.00
          buried.state.moderately_damaged: 0.001
          buried.state.severely_damaged: 0.005
          buried.state.collapsed: 0.010
          buried.city_type_factor: 1.0
          buried.occupancy_factor: 1.0
      central:
        values:
          death.a: 0.050
          death.b: 0.00
          death.c: 0.00
          injury_to_death_ratio.7: 1.5
          buried.state.basic: 0.00
          buried.state.slightly_damaged: 0.00
          buried.state.moderately_damaged: 0.002
          buried.state.severely_damaged: 0.010
          buried.state.collapsed: 0.020
          buried.city_type_factor: 1.0
          buried.occupancy_factor: 1.0
      high:
        values:
          death.a: 0.080
          death.b: 0.00
          death.c: 0.00
          injury_to_death_ratio.7: 2.0
          buried.state.basic: 0.00
          buried.state.slightly_damaged: 0.00
          buried.state.moderately_damaged: 0.004
          buried.state.severely_damaged: 0.020
          buried.state.collapsed: 0.040
          buried.city_type_factor: 1.0
          buried.occupancy_factor: 1.0
  economic_loss:
    model_id: "economic-building-loss-v1"
    formula_version: "economic-building-loss-v1"
    applicable_region: "shanghai"
    input_contract: ["building_damage", "parameter_set"]
    output_contract: ["economic_loss"]
    source_citations: ["synthetic_equation_test"]
    source_requirements: []
    scenarios:
      low:
        values:
          replacement_cost_yuan_m2.rc_frame: 3500
          contents_value_yuan_m2.rc_frame: 600
          loss_ratio.rc_frame.basic: 0.00
          loss_ratio.rc_frame.slightly_damaged: 0.05
          loss_ratio.rc_frame.moderately_damaged: 0.20
          loss_ratio.rc_frame.severely_damaged: 0.55
          loss_ratio.rc_frame.collapsed: 0.90
          contents_ratio.rc_frame.basic: 0.00
          contents_ratio.rc_frame.slightly_damaged: 0.05
          contents_ratio.rc_frame.moderately_damaged: 0.20
          contents_ratio.rc_frame.severely_damaged: 0.50
          contents_ratio.rc_frame.collapsed: 0.80
      central:
        values:
          replacement_cost_yuan_m2.rc_frame: 4200
          contents_value_yuan_m2.rc_frame: 800
          loss_ratio.rc_frame.basic: 0.00
          loss_ratio.rc_frame.slightly_damaged: 0.08
          loss_ratio.rc_frame.moderately_damaged: 0.25
          loss_ratio.rc_frame.severely_damaged: 0.60
          loss_ratio.rc_frame.collapsed: 1.00
          contents_ratio.rc_frame.basic: 0.00
          contents_ratio.rc_frame.slightly_damaged: 0.08
          contents_ratio.rc_frame.moderately_damaged: 0.25
          contents_ratio.rc_frame.severely_damaged: 0.55
          contents_ratio.rc_frame.collapsed: 0.90
      high:
        values:
          replacement_cost_yuan_m2.rc_frame: 5000
          contents_value_yuan_m2.rc_frame: 1000
          loss_ratio.rc_frame.basic: 0.00
          loss_ratio.rc_frame.slightly_damaged: 0.10
          loss_ratio.rc_frame.moderately_damaged: 0.30
          loss_ratio.rc_frame.severely_damaged: 0.70
          loss_ratio.rc_frame.collapsed: 1.00
          contents_ratio.rc_frame.basic: 0.00
          contents_ratio.rc_frame.slightly_damaged: 0.10
          contents_ratio.rc_frame.moderately_damaged: 0.30
          contents_ratio.rc_frame.severely_damaged: 0.60
          contents_ratio.rc_frame.collapsed: 1.00
  resource_demand:
    model_id: "resource-linear-demand-v1"
    formula_version: "resource-linear-demand-v1"
    applicable_region: "shanghai"
    input_contract: [
      "building_damage",
      "population_impact",
      "casualties",
      "parameter_set",
    ]
    output_contract: ["resource_demand"]
    source_citations: ["synthetic_equation_test"]
    source_requirements: []
    scenarios:
      low: {values: *resource-low}
      central: {values: *resource-central}
      high: {values: *resource-high}
```

The YAML anchors above are deliberate: they keep the three scenarios explicit
while ensuring every required resource prefix exists. These values are
synthetic test values only and must never be copied into the production
parameter pack.

Implement `load_parameter_set` with SHA-256 over the raw file bytes, then delegate to:

```python
def load_parameter_set_from_mapping(
    payload: Mapping[str, object],
    *,
    checksum: str,
) -> ParameterSet:
    if len(checksum) != 64:
        raise ValueError("parameter set checksum must be a SHA-256 digest")
    return ParameterSet(
        version=str(payload["version"]),
        calibration_status=LossCalibrationStatus(str(payload["calibration_status"])),
        provenance=dict(payload["provenance"]),
        models={
            LossModelType(model_key): _parse_parameter_model(model_value)
            for model_key, model_value in dict(payload["models"]).items()
        },
        checksum=checksum,
        test_only=bool(payload.get("test_only", False)),
    )
```

Reject:

```python
if not parameter_set.test_only and parameter_set.calibration_status is LossCalibrationStatus.CALIBRATED:
    raise ValueError("production calibrated parameters require review evidence")
```

Implement `require_parameters` so the production empty scenario raises:

```python
missing = tuple(name for name in names if name not in scenario.values)
if missing:
    raise LossParametersUnavailable(
        f"{model_type.value} parameters unavailable: {', '.join(missing)}"
    )
return {name: float(scenario.values[name]) for name in names}
```

Implement one canonical intensity key formatter and use it everywhere a
parameter key contains `{intensity_bin}`:

```python
_ROMAN_INTENSITY = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
    "XI": 11,
    "XII": 12,
}


def canonical_intensity_bin(value: int | float | str) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in _ROMAN_INTENSITY:
            return str(_ROMAN_INTENSITY[normalized])
        value = normalized
    number = float(value)
    if not number.is_integer():
        raise ValueError("intensity bin must be an integer")
    integer = int(number)
    if integer < 1 or integer > 12:
        raise ValueError("intensity bin must be between I and XII")
    return str(integer)
```

All model modules must call `canonical_intensity_bin` before composing a
parameters key. The Roman form is accepted only at configuration boundaries;
the persisted and runtime key is always the decimal form (`"7"`).

Implement `_parse_parameter_model` so every field in `ParameterSetModel` is
read from YAML and converted to immutable tuples. Implement
`register_defaults` by reading each `ParameterSetModel` and creating a
`ModelDefinition` with the model id, model type, formula version,
`applicable_region`, `input_contract`, `output_contract`, `source_citations`,
`source_requirements`, calibration status, and `is_default=True`, then call
`register` once for each model type. Missing fields are configuration errors;
do not invent fallback metadata in code.

The YAML file is the reviewed source for creating the `shanghai.loss.parameters` candidate through the data-asset import pipeline; `DataAssetService.populate_candidate_version` persists its normalized record, and the loss runtime reads only the locked data-asset record through `load_locked_parameter_set`.

- [ ] **Step 4: Run the focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_domain.py tests/test_loss_models_registry.py tests/test_assessment_domain.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss tests/test_loss_domain.py tests/test_loss_models_registry.py
```

Expected: PASS. The production pack resolves model metadata but numeric parameter requests raise `LossParametersUnavailable`.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/domain.py backend/app/loss/models_registry.py config/loss/shanghai-reference-uncalibrated.yaml backend/tests/fixtures/loss-test-parameters.yaml backend/tests/test_loss_domain.py backend/tests/test_loss_models_registry.py
git commit -m "feat: add loss domain and parameter registry"
```

### Task 3: Data-Asset Bridge, Region Profile, and Exposure Preparation

**Files:**
- Create: `backend/app/loss/region.py`
- Create: `backend/app/loss/asset_bridge.py`
- Create: `backend/app/loss/exposure.py`
- Create: `config/loss/shanghai-region.yaml`
- Create: `backend/tests/test_loss_region.py`
- Create: `backend/tests/test_loss_exposure.py`
- Modify: `backend/app/config.py`

**Interfaces:**
- Consumes:
  - `DataAssetSnapshotService.capture_required_assets(session, run_id=..., region_id=..., strict=True)`
  - `DataAssetSnapshotService.get_locked_version(session, run_id=..., asset_key=...)`
  - `DataAssetSnapshotService.list_locked_records(session, run_id=..., asset_key=...)`
  - `DataAssetSnapshotService.get_locked_raster(session, run_id=..., asset_key=...)`
  - `data_asset_versions`
  - `data_asset_snapshots`
  - `data_asset_records`
  - `data_asset_rasters`
- Produces:
  - `RegionLossProfile`
  - `load_region_loss_profile(path: str | Path) -> RegionLossProfile`
  - `load_region_loss_profile_from_mapping(payload: Mapping[str, object]) -> RegionLossProfile`
  - `RegionLossProfile.to_snapshot() -> dict[str, object]`
  - `BuildingExposure`
  - `TownExposure`
  - `ExposureDataset`
  - `build_exposure_dataset(*, snapshot_checksum: str, towns: Sequence[AssetRecord | Mapping[str, object]], geometries: Sequence[AssetRecord | Mapping[str, object]], buildings: Sequence[AssetRecord | Mapping[str, object]]) -> ExposureDataset`
  - `LossAssetBridge.lock_and_load(session: AsyncSession, *, run_id: UUID, region_id: str) -> ExposureDataset`
  - `LossExposureService.prepare(session: AsyncSession, *, run_id: UUID) -> ExposureDataset`
  - `load_locked_parameter_set(session: AsyncSession, *, run_id: UUID, profile: RegionLossProfile) -> ParameterSet`

- [ ] **Step 1: Write failing region and exposure tests**

Create `backend/tests/test_loss_region.py`:

```python
from pathlib import Path

from app.loss.region import (
    load_region_loss_profile,
    load_region_loss_profile_from_mapping,
)


def test_loads_shanghai_loss_region_profile() -> None:
    profile = load_region_loss_profile(Path("/config/loss/shanghai-region.yaml"))
    assert profile.region_id == "shanghai"
    assert profile.area_crs == "EPSG:32651"
    assert profile.output_crs == "EPSG:4326"
    assert profile.population_field == "total"
    assert profile.affected_population_min_intensity == 6.0
    assert profile.spatial_allocation_rule == "town-uniform-v1"
    assert profile.grid_residual_review_threshold == 0.01
    assert profile.asset_keys.population_town == "shanghai.population.town"
    assert profile.asset_keys.loss_parameters == "shanghai.loss.parameters"
    frozen = load_region_loss_profile_from_mapping(profile.to_snapshot())
    assert frozen == profile
```

Create `backend/tests/test_loss_exposure.py`:

```python
from uuid import uuid4

import pytest

from app.data_assets.repository import AssetRecord
from app.loss.exposure import LossExposureService, build_exposure_dataset


class FakeSnapshots:
    async def capture_required_assets(
        self,
        session,
        *,
        run_id,
        region_id,
        strict,
    ):
        assert region_id == "shanghai"
        assert strict is True
        return type(
            "SnapshotResult",
            (),
            {
                "snapshot_count": 7,
                "missing_required": (),
                "fingerprint": "f" * 64,
            },
        )()

    async def get_locked_version(self, session, *, run_id, asset_key):
        return type(
            "LockedVersion",
            (),
            {"id": uuid4(), "version": "2022.1", "checksum": "a" * 64},
        )()


class FakeRun:
    snapshot = {"region_id": "shanghai"}


class FakeSession:
    async def get(self, model, run_id):
        return FakeRun()


async def _locked_records(session, *, run_id, asset_key):
    payloads = {
        "shanghai.population.town": (
            {"ID": "310101001", "NAME": "测试街道", "total": 10000.0},
        ),
        "shanghai.building.town": (
            {
                "id": "310101001",
                "name": "测试街道",
                "TOTAL_AREA": 50000.0,
                "HIGH_RISE": 0.0,
                "RCFRAME": 50000.0,
                "BRICK_STRUCTURE": 0.0,
                "SINGLE_AREA": 0.0,
                "OTHER_STRUCTURE": 0.0,
            },
        ),
    }
    return [
        type("Record", (), {"properties": payload})()
        for payload in payloads.get(asset_key, ())
    ]


async def _locked_features(session, *, run_id, asset_key):
    return [
        type(
            "Feature",
            (),
            {
                "properties": {
                    "ID": "310101001",
                    "NAME": "测试街道",
                },
                "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            },
        )()
    ]


async def test_exposure_combines_population_buildings_and_geometry() -> None:
    service = LossExposureService(
        snapshots=FakeSnapshots(),
        list_records=_locked_records,
        list_features=_locked_features,
    )
    dataset = await service.prepare(
        session=FakeSession(),
        run_id=uuid4(),
    )
    assert dataset.snapshot_checksum == "f" * 64
    assert len(dataset.towns) == 1
    assert dataset.towns[0].population_total == pytest.approx(10000.0)
    assert dataset.towns[0].buildings[0].area_m2 == pytest.approx(50000.0)


def test_exposure_accepts_locked_asset_records() -> None:
    dataset = build_exposure_dataset(
        snapshot_checksum="f" * 64,
        towns=(
            AssetRecord(
                row_number=1,
                business_key="310101001",
                properties={"ID": "310101001", "NAME": "测试街道", "total": 10000.0},
                geometry_wkt=None,
            ),
        ),
        geometries=(
            AssetRecord(
                row_number=1,
                business_key="310101001",
                properties={"ID": "310101001", "NAME": "测试街道"},
                geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
            ),
        ),
        buildings=(
            AssetRecord(
                row_number=1,
                business_key="310101001",
                properties={
                    "id": "310101001",
                    "name": "测试街道",
                    "TOTAL_AREA": 50000.0,
                    "HIGH_RISE": 0.0,
                    "RCFRAME": 50000.0,
                    "BRICK_STRUCTURE": 0.0,
                    "SINGLE_AREA": 0.0,
                    "OTHER_STRUCTURE": 0.0,
                },
                geometry_wkt=None,
            ),
        ),
    )

    assert dataset.towns[0].town_code == "310101001"
    assert dataset.towns[0].buildings[0].area_m2 == pytest.approx(50000.0)
```

- [ ] **Step 2: Run the focused tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_region.py tests/test_loss_exposure.py -v
```

Expected: FAIL because the loss region, bridge, and exposure modules do not exist.

- [ ] **Step 3: Implement the data-asset bridge and normalized exposure dataset**

Create `config/loss/shanghai-region.yaml`:

```yaml
version: "shanghai-loss-region-v1"
region_id: "shanghai"
name: "上海市"
administrative_level: "province"
area_crs: "EPSG:32651"
output_crs: "EPSG:4326"
population_field: "total"
population_fields_for_audit:
  - "resident"
  - "floating"
affected_population_min_intensity: 6.0
spatial_allocation_rule: "town-uniform-v1"
grid_resolution_m: 1000
minimum_town_coverage_ratio: 0.95
grid_residual_review_threshold: 0.01
assets:
  admin_city: "shanghai.admin.city"
  admin_county: "shanghai.admin.county"
  admin_town: "shanghai.admin.town"
  population_town: "shanghai.population.town"
  building_town: "shanghai.building.town"
  economy_county: "shanghai.economy.county"
  loss_parameters: "shanghai.loss.parameters"
  fault: "shanghai.fault"
  gdp_raster: "shanghai.gdp.raster"
  dem_raster: "shanghai.dem.raster"
default_model_versions:
  building_damage: "building-structure-matrix-v1"
  population_impact: "population-intensity-v1"
  casualties: "casualty-building-intensity-v1"
  economic_loss: "economic-building-loss-v1"
  resource_demand: "resource-linear-demand-v1"
```

Create `backend/app/loss/region.py` with a profile-owned asset map:

```python
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RegionAssetKeys:
    admin_city: str
    admin_county: str
    admin_town: str
    population_town: str
    building_town: str
    economy_county: str
    loss_parameters: str
    fault: str | None
    gdp_raster: str | None
    dem_raster: str | None


@dataclass(frozen=True, slots=True)
class RegionLossProfile:
    version: str
    region_id: str
    name: str
    administrative_level: str
    area_crs: str
    output_crs: str
    population_field: str
    population_fields_for_audit: tuple[str, ...]
    affected_population_min_intensity: float
    spatial_allocation_rule: str
    grid_resolution_m: int
    minimum_town_coverage_ratio: float
    grid_residual_review_threshold: float
    default_model_versions: dict[str, str]
    asset_keys: RegionAssetKeys


    def to_snapshot(self) -> dict[str, object]:
        return {
            "version": self.version,
            "region_id": self.region_id,
            "name": self.name,
            "administrative_level": self.administrative_level,
            "area_crs": self.area_crs,
            "output_crs": self.output_crs,
            "population_field": self.population_field,
            "population_fields_for_audit": list(
                self.population_fields_for_audit
            ),
            "affected_population_min_intensity": (
                self.affected_population_min_intensity
            ),
            "spatial_allocation_rule": self.spatial_allocation_rule,
            "grid_resolution_m": self.grid_resolution_m,
            "minimum_town_coverage_ratio": self.minimum_town_coverage_ratio,
            "grid_residual_review_threshold": (
                self.grid_residual_review_threshold
            ),
            "assets": asdict(self.asset_keys),
            "default_model_versions": dict(self.default_model_versions),
        }


def load_region_loss_profile(path: str | Path) -> RegionLossProfile:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("loss region profile must be a mapping")
    return load_region_loss_profile_from_mapping(payload)


def load_region_loss_profile_from_mapping(
    payload: Mapping[str, object],
) -> RegionLossProfile:
    if not isinstance(payload, dict):
        raise ValueError("loss region profile must be a mapping")
    assets = payload.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("loss region profile requires assets")
    required_asset_keys = (
        "admin_city",
        "admin_county",
        "admin_town",
        "population_town",
        "building_town",
        "economy_county",
        "loss_parameters",
    )
    optional_asset_keys = ("fault", "gdp_raster", "dem_raster")

    def required_asset_key(key: str) -> str:
        value = assets.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"loss region profile requires assets.{key}")
        return value.strip()

    def optional_asset_key(key: str) -> str | None:
        value = assets.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"loss region profile assets.{key} must be text or null")
        return value.strip()

    asset_keys = RegionAssetKeys(
        **{key: required_asset_key(key) for key in required_asset_keys},
        **{key: optional_asset_key(key) for key in optional_asset_keys},
    )
    return RegionLossProfile(
        version=str(payload["version"]),
        region_id=str(payload["region_id"]),
        name=str(payload["name"]),
        administrative_level=str(payload["administrative_level"]),
        area_crs=str(payload["area_crs"]),
        output_crs=str(payload["output_crs"]),
        population_field=str(payload["population_field"]),
        population_fields_for_audit=tuple(payload.get("population_fields_for_audit", ())),
        affected_population_min_intensity=float(payload["affected_population_min_intensity"]),
        spatial_allocation_rule=str(payload["spatial_allocation_rule"]),
        grid_resolution_m=int(payload["grid_resolution_m"]),
        minimum_town_coverage_ratio=float(payload["minimum_town_coverage_ratio"]),
        grid_residual_review_threshold=float(
            payload["grid_residual_review_threshold"]
        ),
        default_model_versions={
            str(key): str(value)
            for key, value in dict(payload["default_model_versions"]).items()
        },
        asset_keys=asset_keys,
    )
```

Add `loss_region_profile_path: str = "/config/loss/shanghai-region.yaml"` to
`backend/app/config.py` and reject a blank value in the existing settings
validator.

Create `backend/app/loss/asset_bridge.py`:

```python
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.loss.domain import ParameterSet
from app.loss.models_registry import load_parameter_set_from_mapping
from app.loss.region import RegionLossProfile, load_region_loss_profile

if TYPE_CHECKING:
    from app.loss.exposure import ExposureDataset


class LossAssetBridge:
    def __init__(
        self,
        *,
        snapshots: DataAssetSnapshotService,
        profile: RegionLossProfile | None = None,
        list_records=None,
        list_features=None,
    ) -> None:
        self._snapshots = snapshots
        self._profile = profile or load_region_loss_profile(
            settings.loss_region_profile_path
        )
        self._list_records = list_records or snapshots.list_locked_records
        self._list_features = list_features or snapshots.list_locked_records

    async def lock_and_load(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        region_id: str,
    ) -> "ExposureDataset":
        from app.loss.exposure import build_exposure_dataset

        if region_id != self._profile.region_id:
            raise ValueError("loss region profile does not match run region_id")
        snapshot = await self._snapshots.capture_required_assets(
            session,
            run_id=run_id,
            region_id=region_id,
            strict=True,
        )
        keys = self._profile.asset_keys
        town_version = await self._snapshots.get_locked_version(
            session,
            run_id=run_id,
            asset_key=keys.admin_town,
        )
        population_version = await self._snapshots.get_locked_version(
            session,
            run_id=run_id,
            asset_key=keys.population_town,
        )
        building_version = await self._snapshots.get_locked_version(
            session,
            run_id=run_id,
            asset_key=keys.building_town,
        )
        if town_version is None or population_version is None or building_version is None:
            raise LookupError("required loss data asset is not locked")
        towns = await self._list_records(
            session,
            run_id=run_id,
            asset_key=keys.population_town,
        )
        geometries = await self._list_features(
            session,
            run_id=run_id,
            asset_key=keys.admin_town,
        )
        buildings = await self._list_records(
            session,
            run_id=run_id,
            asset_key=keys.building_town,
        )
        return build_exposure_dataset(
            snapshot_checksum=snapshot.fingerprint,
            towns=towns,
            geometries=geometries,
            buildings=buildings,
        )


async def load_locked_parameter_set(
    session: AsyncSession,
    *,
    run_id: UUID,
    profile: RegionLossProfile,
) -> ParameterSet:
    snapshots = DataAssetSnapshotService()
    version = await snapshots.get_locked_version(
        session,
        run_id=run_id,
        asset_key=profile.asset_keys.loss_parameters,
    )
    if version is None:
        raise LookupError("loss parameter asset is not locked")
    records = await snapshots.list_locked_records(
        session,
        run_id=run_id,
        asset_key=profile.asset_keys.loss_parameters,
    )
    if len(records) != 1:
        raise ValueError("loss parameter asset requires exactly one record")
    return load_parameter_set_from_mapping(
        records[0].properties,
        checksum=version.checksum,
    )
```

Create `backend/app/loss/exposure.py` with frozen records:

```python
@dataclass(frozen=True, slots=True)
class BuildingExposure:
    town_code: str
    structure: BuildingStructure
    era: str | None
    area_m2: float


@dataclass(frozen=True, slots=True)
class TownExposure:
    town_code: str
    county_code: str
    town_name: str
    population_total: float
    geometry_wkt: str
    buildings: tuple[BuildingExposure, ...]


@dataclass(frozen=True, slots=True)
class ExposureDataset:
    snapshot_checksum: str
    towns: tuple[TownExposure, ...]


def build_exposure_dataset(
    *,
    snapshot_checksum: str,
    towns: Sequence[AssetRecord | Mapping[str, object]],
    geometries: Sequence[AssetRecord | Mapping[str, object]],
    buildings: Sequence[AssetRecord | Mapping[str, object]],
) -> ExposureDataset:
    if len(snapshot_checksum) != 64:
        raise ValueError("snapshot checksum must be a SHA-256 digest")

    town_rows = {_business_key(row, "town_code", "ID", "TOWN_CODE"): row for row in towns}
    geometry_rows = {
        _business_key(row, "town_code", "ID", "TOWN_CODE"): _geometry_wkt(row)
        for row in geometries
    }
    if len(town_rows) != len(towns):
        raise ValueError("duplicate town code in population asset")
    if set(town_rows) != set(geometry_rows):
        raise ValueError("town population and boundary business keys do not match")

    buildings_by_town: dict[str, list[BuildingExposure]] = {
        town_code: [] for town_code in town_rows
    }
    for row in buildings:
        town_code = _business_key(row, "town_code", "id", "ID")
        if town_code not in buildings_by_town:
            raise ValueError("building row references an unknown town code")
        for structure, area_m2 in _building_areas(row):
            if area_m2 < 0:
                raise ValueError("building area must not be negative")
            if area_m2 == 0:
                continue
            era_value = _optional_text(row, "era")
            buildings_by_town[town_code].append(
                BuildingExposure(
                    town_code=town_code,
                    structure=structure,
                    era=era_value,
                    area_m2=area_m2,
                )
            )

    result: list[TownExposure] = []
    for town_code in sorted(town_rows):
        row = town_rows[town_code]
        population = _number(row, "population_total", "total")
        if population < 0:
            raise ValueError("population must not be negative")
        result.append(
            TownExposure(
                town_code=town_code,
                county_code=_county_code(row, town_code),
                town_name=_required_text(row, "town_name", "NAME", "name"),
                population_total=population,
                geometry_wkt=geometry_rows[town_code],
                buildings=tuple(
                    sorted(
                        buildings_by_town[town_code],
                        key=lambda item: (item.structure.value, item.era or ""),
                    )
                ),
            )
        )
    return ExposureDataset(
        snapshot_checksum=snapshot_checksum,
        towns=tuple(result),
    )


class LossExposureService:
    def __init__(
        self,
        *,
        snapshots,
        profile: RegionLossProfile | None = None,
        list_records=None,
        list_features=None,
    ) -> None:
        self._profile = profile or load_region_loss_profile(
            settings.loss_region_profile_path
        )
        self._bridge = LossAssetBridge(
            snapshots=snapshots,
            profile=self._profile,
            list_records=list_records,
            list_features=list_features,
        )

    async def prepare(self, session, *, run_id) -> ExposureDataset:
        run = await session.get(AssessmentRun, run_id)
        if run is None:
            raise LookupError("assessment run not found")
        region_id = run.snapshot.get("region_id")
        if not isinstance(region_id, str) or not region_id.strip():
            raise ValueError("assessment run snapshot requires region_id")
        return await self._bridge.lock_and_load(
            session,
            run_id=run_id,
            region_id=region_id,
        )
```

The bridge maps the frozen data-asset records as follows:

```text
shanghai.population.town.properties:
  ID -> town_code
  NAME -> town_name
  total -> population_total

shanghai.admin.town.properties:
  ID or TOWN_CODE -> town_code
  geometry_wkt -> town boundary WKT

shanghai.building.town.properties:
  id -> town_code
  HIGH_RISE -> high_rise area_m2
  RCFRAME -> rc_frame area_m2
  BRICK_STRUCTURE -> masonry area_m2
  SINGLE_AREA -> single_storey area_m2
  OTHER_STRUCTURE -> other area_m2
  optional era -> era (absent in the first Shanghai MDB, so fallback is L3)
```

`build_exposure_dataset` accepts both `AssetRecord` instances and the mapping
fixtures above. Implement one `_properties(row)` helper returning
`row.properties` for `AssetRecord` and `dict(row)` for a mapping, and one
`_geometry_wkt(row)` helper returning `row.geometry_wkt` for `AssetRecord` or
the `geometry_wkt` key for a mapping. `_business_key`, `_required_text`,
`_optional_text`, `_number`, `_building_areas`, and `_county_code` then operate
only on `_properties(row)`. `_building_areas` expands the five real MDB
structure columns to `BuildingStructure` values; if a normalized test row
already contains `structure` and `area_m2`, it uses that row directly. County
code is the explicit `county_code` when present, otherwise the first nine
digits of the twelve-digit town code.

The function must reject duplicate town codes, negative population or area,
unknown structure names, unmatched population/boundary keys, building rows for
unknown towns, and missing geometry.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_region.py tests/test_loss_exposure.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/asset_bridge.py app/loss/exposure.py app/loss/region.py tests/test_loss_region.py tests/test_loss_exposure.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/region.py backend/app/loss/asset_bridge.py backend/app/loss/exposure.py config/loss/shanghai-region.yaml backend/tests/test_loss_region.py backend/tests/test_loss_exposure.py backend/app/config.py
git commit -m "feat: prepare versioned loss exposure"
```

### Task 4: Town-Intensity Distribution and 1 km Allocation

**Files:**
- Create: `backend/app/loss/spatial.py`
- Create: `backend/tests/test_loss_spatial.py`

**Interfaces:**
- Consumes: `ExposureDataset`, fused intensity raster, `region_boundaries`, and the active 1 km grid definition.
- Produces:
  - `TownIntensityShare`
  - `LossGridCell(cell_id: str, town_code: str, area_ratio: float, response_weight: float)`
  - `TownIntensityDistributionService.compute(session: AsyncSession, *, run_id: UUID) -> tuple[TownIntensityShare, ...]`
  - `TownIntensityDistributionService.load_cells(session: AsyncSession, *, run_id: UUID) -> tuple[LossGridCell, ...]`
  - `allocate_continuous(town_values: Mapping[str, float], cells: Sequence[LossGridCell]) -> dict[str, float]`
  - `allocate_integers(town_values: Mapping[str, int], cells: Sequence[LossGridCell]) -> dict[str, int]`

- [ ] **Step 1: Write failing spatial tests**

Create `backend/tests/test_loss_spatial.py`:

```python
import pytest

from app.loss.spatial import (
    LossGridCell,
    allocate_continuous,
    allocate_integers,
)


def test_continuous_allocation_preserves_town_total() -> None:
    cells = (
        LossGridCell("g1", "t1", 0.25, 4.0),
        LossGridCell("g2", "t1", 0.75, 8.0),
    )
    allocated = allocate_continuous({"t1": 100.0}, cells)
    assert sum(allocated.values()) == pytest.approx(100.0)
    assert allocated["g1"] == pytest.approx(14.2857142857, rel=1e-9)
    assert allocated["g2"] == pytest.approx(85.7142857143, rel=1e-9)


def test_integer_allocation_preserves_total_with_largest_remainder() -> None:
    cells = (
        LossGridCell("g1", "t1", 0.25, 4.0),
        LossGridCell("g2", "t1", 0.75, 8.0),
    )
    allocated = allocate_integers({"t1": 7}, cells)
    assert sum(allocated.values()) == 7
    assert allocated == {"g1": 2, "g2": 5}


def test_zero_weight_cells_receive_zero() -> None:
    cells = (
        LossGridCell("g1", "t1", 1.0, 0.0),
        LossGridCell("g2", "t1", 0.0, 10.0),
    )
    assert allocate_continuous({"t1": 5.0}, cells) == {"g1": 5.0, "g2": 0.0}
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_spatial.py -v
```

Expected: FAIL because `app.loss.spatial` does not exist.

- [ ] **Step 3: Implement PostGIS intersection and deterministic allocation**

Create `TownIntensityShare`:

```python
@dataclass(frozen=True, slots=True)
class TownIntensityShare:
    town_code: str
    intensity_bin: int
    area_ratio: float
    intensity_min: float
    intensity_max: float
```

Create `LossGridCell`:

```python
@dataclass(frozen=True, slots=True)
class LossGridCell:
    cell_id: str
    town_code: str
    area_ratio: float
    response_weight: float
```

Implement `TownIntensityDistributionService.compute` with one SQL statement using `ST_Intersection`, `ST_Area`, and the fused intensity raster. Use `ST_Clip`, `ST_Intersects`, and `ST_Value` or `ST_SummaryStats` only at the town-grid intersection; do not load the entire raster into Python when SQL can aggregate it.

Implement `load_cells` against the active fixed 1 km grid and the locked
administrative-town geometry for the run. Each returned `LossGridCell` must use
the stable grid cell ID, the intersecting town code, the fraction of the cell
inside that town, and the fused-intensity response weight. Reject duplicate
cell-town pairs and ensure every town with persisted metrics has at least one
cell. Order rows by `(cell_id, town_code)` for deterministic raster generation.

Implement allocation with deterministic ordering by `(town_code, cell_id)` and largest remainder for integers:

```python
def allocate_integers(
    town_values: Mapping[str, int],
    cells: Sequence[LossGridCell],
) -> dict[str, int]:
    result = {cell.cell_id: 0 for cell in cells}
    for town_code in sorted(town_values):
        town_cells = sorted(
            (cell for cell in cells if cell.town_code == town_code),
            key=lambda cell: cell.cell_id,
        )
        target = int(town_values[town_code])
        total_weight = sum(cell.response_weight for cell in town_cells)
        if target < 0 or total_weight <= 0:
            if target:
                raise ValueError("positive integer allocation requires positive weight")
            continue
        exact = [
            target * cell.response_weight / total_weight
            for cell in town_cells
        ]
        floors = [int(value) for value in exact]
        remainder = target - sum(floors)
        order = sorted(
            range(len(town_cells)),
            key=lambda index: (
                -(exact[index] - floors[index]),
                town_cells[index].cell_id,
            ),
        )
        for index in order[:remainder]:
            floors[index] += 1
        for cell, value in zip(town_cells, floors, strict=True):
            result[cell.cell_id] = value
    return result
```

Reject cells whose `area_ratio` is outside `[0, 1]`, negative response weights, or duplicate `cell_id`.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_spatial.py tests/test_intensity_grid.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/spatial.py tests/test_loss_spatial.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/spatial.py backend/tests/test_loss_spatial.py
git commit -m "feat: allocate town losses to kilometre grid"
```

### Task 5: Building Damage Model

**Files:**
- Create: `backend/app/loss/buildings.py`
- Create: `backend/tests/test_loss_buildings.py`

**Interfaces:**
- Consumes: `ExposureDataset`, `TownIntensityShare`, `ModelDefinition`, `ScenarioParameters`.
- Produces:
  - `BuildingDamageStateArea`
  - `TownBuildingDamage`
  - `BuildingDamageResult`
  - `assess_building_damage(exposure: ExposureDataset, intensity_shares: Sequence[TownIntensityShare], parameters: ScenarioParameters) -> BuildingDamageResult`

- [ ] **Step 1: Write failing building tests**

Create `backend/tests/test_loss_buildings.py`:

```python
from pathlib import Path

import pytest

from app.loss.buildings import assess_building_damage
from app.loss.domain import LossModelType, LossValueType
from app.loss.exposure import build_exposure_dataset
from app.loss.models_registry import load_parameter_set
from app.loss.spatial import TownIntensityShare


PARAMETERS = Path("tests/fixtures/loss-test-parameters.yaml")


def _exposure():
    return build_exposure_dataset(
        snapshot_checksum="a" * 64,
        towns=[
            {
                "town_code": "t1",
                "county_code": "c1",
                "town_name": "测试镇",
                "population_total": 1000.0,
            }
        ],
        geometries=[{"town_code": "t1", "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))"}],
        buildings=[
            {
                "town_code": "t1",
                "structure": "rc_frame",
                "era": None,
                "area_m2": 10000.0,
            }
        ],
    )


def test_building_damage_preserves_area_and_returns_all_states() -> None:
    parameters = load_parameter_set(PARAMETERS).models[
        LossModelType.BUILDING_DAMAGE
    ].scenarios[LossValueType.CENTRAL]
    result = assess_building_damage(
        _exposure(),
        (
            TownIntensityShare(
                town_code="t1",
                intensity_bin=7,
                area_ratio=1.0,
                intensity_min=6.5,
                intensity_max=7.5,
            ),
        ),
        parameters,
    )
    town = result.towns["t1"]
    assert sum(state.area_m2 for state in town.states) == pytest.approx(10000.0)
    assert {state.damage_state.value for state in town.states} == {
        "basic",
        "slightly_damaged",
        "moderately_damaged",
        "severely_damaged",
        "collapsed",
    }


def test_missing_matrix_row_is_an_error() -> None:
    parameters = load_parameter_set(PARAMETERS).models[
        LossModelType.BUILDING_DAMAGE
    ].scenarios[LossValueType.CENTRAL]
    with pytest.raises(KeyError):
        assess_building_damage(
            _exposure(),
            (
                TownIntensityShare(
                    town_code="t1",
                    intensity_bin=12,
                    area_ratio=1.0,
                    intensity_min=11.5,
                    intensity_max=12.5,
                ),
            ),
            parameters,
        )
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_buildings.py -v
```

Expected: FAIL because `assess_building_damage` does not exist.

- [ ] **Step 3: Implement the matrix model and fallback**

Create the exact immutable result records before implementing the formula:

```python
@dataclass(frozen=True, slots=True)
class BuildingDamageStateArea:
    structure: BuildingStructure
    intensity_bin: int
    damage_state: DamageState
    area_m2: float


@dataclass(frozen=True, slots=True)
class TownBuildingDamage:
    town_code: str
    total_area_m2: float
    states: tuple[BuildingDamageStateArea, ...]
    quality_grade: LossQualityGrade


@dataclass(frozen=True, slots=True)
class BuildingDamageResult:
    towns: Mapping[str, TownBuildingDamage]
    quality_grade: LossQualityGrade
    fallback_model_used: bool
```

Implement the formula:

```python
state_area = (
    structure_area
    * intensity_area_ratio
    * vulnerability[
        structure.value,
        canonical_intensity_bin(intensity_bin),
        state.value,
    ]
)
```

Use keys:

```text
vulnerability.{structure}.{intensity_bin}.{damage_state}
```

Rules:

- Prefer `vulnerability.{structure}.{era}.{intensity_bin}.{damage_state}` when
  `era` is present. When `era is None`, use
  `vulnerability.{structure}.{intensity_bin}.{damage_state}` and mark the town
  `L3`; this fallback is the expected path for the first Shanghai MDB.
- If neither row exists, raise `KeyError`.
- Clamp each state area to the structure-area share for that intensity bin.
- After calculating five states, scale only downward when the sum exceeds the available area.
- Calculate `collapsed_area_m2=state_area[COLLAPSED]`.
- Calculate `severe_or_collapsed_area_m2=severe+ collasped`.
- Keep per-intensity state areas for downstream casualty and economic calculations.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_buildings.py tests/test_loss_spatial.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/buildings.py tests/test_loss_buildings.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/buildings.py backend/tests/test_loss_buildings.py
git commit -m "feat: assess building damage"
```

### Task 5A: Shared Loss Test Factories

**Files:**
- Create: `backend/tests/loss_factories.py`
- Modify: `backend/tests/test_loss_buildings.py`

**Interfaces:**
- Consumes: the result records produced by Tasks 5-9. The functions for
  records not implemented yet use local imports so this file is importable
  after Task 5 and remains valid after each later task lands.
- Produces:
  - `building_damage_result(severe_fraction: float = 0.10) -> BuildingDamageResult`
  - `population_impact_result(emergency_shelter_population: float = 100.0, affected_population: float = 1000.0) -> PopulationImpactResult`
  - `casualty_result(deaths: float = 1.0, injuries: float = 2.0, buried: float = 3.0) -> CasualtyResult`
  - `economic_loss_result() -> EconomicLossResult`
  - `resource_demand_result() -> ResourceDemandResult`

- [ ] **Step 1: Create the factories with deterministic values**

Create `backend/tests/loss_factories.py`:

```python
from app.loss.domain import (
    DamageState,
    BuildingStructure,
    LossQualityGrade,
    ResourceKind,
)


def building_damage_result(severe_fraction: float = 0.10):
    from app.loss.buildings import (
        BuildingDamageResult,
        BuildingDamageStateArea,
        TownBuildingDamage,
    )

    total_area = 10000.0
    raw_fractions = {
        DamageState.BASIC: 0.70 - severe_fraction,
        DamageState.SLIGHTLY_DAMAGED: 0.15,
        DamageState.MODERATELY_DAMAGED: max(0.15 - severe_fraction, 0.0),
        DamageState.SEVERELY_DAMAGED: severe_fraction / 2.0,
        DamageState.COLLAPSED: severe_fraction / 2.0,
    }
    fraction_total = sum(raw_fractions.values())
    state_fractions = {
        state: fraction / fraction_total
        for state, fraction in raw_fractions.items()
    }
    states = tuple(
        BuildingDamageStateArea(
            structure=BuildingStructure.RC_FRAME,
            intensity_bin=7,
            damage_state=state,
            area_m2=total_area * fraction,
        )
        for state, fraction in state_fractions.items()
    )
    return BuildingDamageResult(
        towns={
            "t1": TownBuildingDamage(
                town_code="t1",
                total_area_m2=total_area,
                states=states,
                quality_grade=LossQualityGrade.L3,
            )
        },
        quality_grade=LossQualityGrade.L3,
        fallback_model_used=True,
    )


def population_impact_result(
    emergency_shelter_population: float = 100.0,
    affected_population: float = 1000.0,
):
    from app.loss.population import (
        IntensityPopulation,
        PopulationImpactResult,
        TownPopulationImpact,
    )

    return PopulationImpactResult(
        towns={
            "t1": TownPopulationImpact(
                town_code="t1",
                full_population=1200.0,
                affected_population=affected_population,
                emergency_shelter_population=emergency_shelter_population,
                temporary_shelter_population=500.0,
                by_intensity=(
                    IntensityPopulation(
                        town_code="t1",
                        intensity_bin=6,
                        population=600.0,
                        area_ratio=0.5,
                    ),
                    IntensityPopulation(
                        town_code="t1",
                        intensity_bin=7,
                        population=400.0,
                        area_ratio=0.5,
                    ),
                ),
            )
        },
        quality_grade=LossQualityGrade.L3,
    )


def casualty_result(
    deaths: float = 1.0,
    injuries: float = 2.0,
    buried: float = 3.0,
):
    from app.loss.casualties import CasualtyResult, TownCasualty

    return CasualtyResult(
        towns={
            "t1": TownCasualty(
                town_code="t1",
                deaths=deaths,
                injuries=injuries,
                buried=buried,
                quality_grade=LossQualityGrade.L3,
            )
        },
        quality_grade=LossQualityGrade.L3,
        fallback_model_used=True,
    )


def economic_loss_result():
    from app.loss.economic import EconomicLossResult, TownEconomicLoss

    town = TownEconomicLoss(
        town_code="t1",
        reconstruction_loss_yuan=1000000.0,
        contents_loss_yuan=100000.0,
        total_loss_yuan=1100000.0,
    )
    return EconomicLossResult(
        towns={"t1": town},
        partial_scope=True,
        excluded_scope=(
            "transport",
            "lifelines",
            "business_interruption",
            "indirect_loss",
            "recovery_duration",
        ),
    )


def resource_demand_result():
    from app.loss.resources import (
        ResourceDemandInputs,
        ResourceDemandResult,
        ResourceDemandValue,
    )

    return ResourceDemandResult(
        inputs=ResourceDemandInputs(
            affected_population=1000.0,
            emergency_shelter_population=100.0,
            deaths=1.0,
            injuries=2.0,
            buried=3.0,
        ),
        values={
            kind: ResourceDemandValue(
                quantity=index,
                status="available",
                reason=None,
            )
            for index, kind in enumerate(ResourceKind, start=1)
        },
    )
```

In `backend/tests/test_loss_buildings.py`, add this import so the factory is
exercised by the Task 5 test run:

```python
from tests.loss_factories import building_damage_result
```

Add:

```python
def test_shared_building_factory_preserves_total_area() -> None:
    result = building_damage_result()
    town = result.towns["t1"]
    assert sum(state.area_m2 for state in town.states) == pytest.approx(
        town.total_area_m2
    )
```

- [ ] **Step 2: Run the factory smoke test**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_buildings.py -v
```

Expected: PASS. The other factories are syntax-checked by Python import but
are not called until their result modules exist.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/loss_factories.py backend/tests/test_loss_buildings.py
git commit -m "test: add shared loss result factories"
```

### Task 6: Population Impact and Emergency Shelter

**Files:**
- Create: `backend/app/loss/population.py`
- Create: `backend/tests/test_loss_population.py`

**Interfaces:**
- Consumes: `ExposureDataset`, `TownIntensityShare`, `ScenarioParameters`.
- Produces:
  - `IntensityPopulation`
  - `TownPopulationImpact`
  - `PopulationImpactResult`
  - `assess_population_impact(exposure: ExposureDataset, intensity_shares: Sequence[TownIntensityShare], parameters: ScenarioParameters) -> PopulationImpactResult`

- [ ] **Step 1: Write failing population tests**

Create `backend/tests/test_loss_population.py`:

```python
from pathlib import Path

import pytest

from app.loss.domain import LossModelType, LossValueType
from app.loss.exposure import build_exposure_dataset
from app.loss.models_registry import load_parameter_set
from app.loss.population import assess_population_impact
from app.loss.spatial import TownIntensityShare


def test_population_threshold_and_shelter_totals() -> None:
    exposure = build_exposure_dataset(
        snapshot_checksum="a" * 64,
        towns=[
            {
                "town_code": "t1",
                "county_code": "c1",
                "town_name": "测试镇",
                "population_total": 10000.0,
            }
        ],
        geometries=[{"town_code": "t1", "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))"}],
        buildings=[],
    )
    parameters = load_parameter_set(
        Path("tests/fixtures/loss-test-parameters.yaml")
    ).models[LossModelType.POPULATION_IMPACT].scenarios[LossValueType.CENTRAL]
    result = assess_population_impact(
        exposure,
        (
            TownIntensityShare("t1", 5, 0.2, 4.5, 5.5),
            TownIntensityShare("t1", 6, 0.3, 5.5, 6.5),
            TownIntensityShare("t1", 7, 0.5, 6.5, 7.5),
        ),
        parameters,
    )
    town = result.towns["t1"]
    assert town.full_population == pytest.approx(10000.0)
    assert town.affected_population == pytest.approx(8000.0)
    assert town.emergency_shelter_population <= town.affected_population
    assert town.temporary_shelter_population <= town.affected_population
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_population.py -v
```

Expected: FAIL because `app.loss.population` does not exist.

- [ ] **Step 3: Implement per-intensity population and shelter calculations**

Create the exact immutable result records:

```python
@dataclass(frozen=True, slots=True)
class IntensityPopulation:
    town_code: str
    intensity_bin: int
    population: float
    area_ratio: float


@dataclass(frozen=True, slots=True)
class TownPopulationImpact:
    town_code: str
    full_population: float
    affected_population: float
    emergency_shelter_population: float
    temporary_shelter_population: float
    by_intensity: tuple[IntensityPopulation, ...]


@dataclass(frozen=True, slots=True)
class PopulationImpactResult:
    towns: Mapping[str, TownPopulationImpact]
    quality_grade: LossQualityGrade
```

Use keys:

```text
affected_population_min_intensity
shelter_ratio.{intensity_bin}
temporary_shelter_ratio
```

Calculate:

```python
by_intensity = tuple(
    (share, town.population_total * share.area_ratio)
    for share in shares
)
affected = tuple(
    (share, value)
    for share, value in by_intensity
    if share.intensity_bin >= math.ceil(affected_min)
)
affected_population = sum(value for _, value in affected)
emergency_shelter = sum(
    value
    * parameters.values[
        f"shelter_ratio.{canonical_intensity_bin(share.intensity_bin)}"
    ]
    for share, value in affected
)
temporary_shelter = (
    emergency_shelter * parameters.values["temporary_shelter_ratio"]
)
```

Reject ratios outside `[0, 1]`, `emergency_shelter > affected`, and `temporary_shelter > affected`.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_population.py tests/test_loss_buildings.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/population.py tests/test_loss_population.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/population.py backend/tests/test_loss_population.py
git commit -m "feat: assess population impact"
```

### Task 7: Death, Injury, and Buried Casualties

**Files:**
- Create: `backend/app/loss/casualties.py`
- Create: `backend/tests/test_loss_casualties.py`

**Interfaces:**
- Consumes: `BuildingDamageResult`, `PopulationImpactResult`, `ScenarioParameters`.
- Produces:
  - `TownCasualty`
  - `CasualtyResult`
  - `assess_casualties(buildings: BuildingDamageResult, population: PopulationImpactResult, parameters: ScenarioParameters) -> CasualtyResult`

- [ ] **Step 1: Write failing casualty tests**

Create `backend/tests/test_loss_casualties.py`:

```python
from pathlib import Path

import pytest

from app.loss.casualties import assess_casualties
from app.loss.domain import LossModelType, LossValueType
from app.loss.models_registry import load_parameter_set
from tests.loss_factories import (
    building_damage_result as _building_result,
    population_impact_result as _population_result,
)


def test_casualties_are_non_negative_and_below_exposure() -> None:
    parameters = load_parameter_set(
        Path("tests/fixtures/loss-test-parameters.yaml")
    ).models[LossModelType.CASUALTIES].scenarios[LossValueType.CENTRAL]
    buildings = _building_result()
    population = _population_result()
    result = assess_casualties(buildings, population, parameters)
    town = result.towns["t1"]
    assert town.deaths >= 0
    assert town.injuries >= town.deaths
    assert town.buried >= 0
    assert town.deaths + town.injuries <= population.towns["t1"].affected_population


def test_death_rate_is_monotonic_when_damage_fraction_increases() -> None:
    parameters = load_parameter_set(
        Path("tests/fixtures/loss-test-parameters.yaml")
    ).models[LossModelType.CASUALTIES].scenarios[LossValueType.CENTRAL]
    low = assess_casualties(
        _building_result(severe_fraction=0.05),
        _population_result(),
        parameters,
    )
    high = assess_casualties(
        _building_result(severe_fraction=0.20),
        _population_result(),
        parameters,
    )
    assert high.towns["t1"].deaths >= low.towns["t1"].deaths
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_casualties.py -v
```

Expected: FAIL because `assess_casualties` and its fixture records do not exist.

- [ ] **Step 3: Implement the deterministic formulas**

Create the exact immutable result records:

```python
@dataclass(frozen=True, slots=True)
class TownCasualty:
    town_code: str
    deaths: float
    injuries: float
    buried: float
    quality_grade: LossQualityGrade


@dataclass(frozen=True, slots=True)
class CasualtyResult:
    towns: Mapping[str, TownCasualty]
    quality_grade: LossQualityGrade
    fallback_model_used: bool
```

Use the legacy structure only as the formula boundary:

```python
severe_fraction = min(
    max(severe_or_collapsed_area_m2 / total_building_area_m2, 0.0),
    1.0,
)
death_rate = a * math.exp(b * (intensity + c)) * severe_fraction
death_rate = min(max(death_rate, 0.0), 1.0)
deaths = affected_population * death_rate
injuries = deaths * injury_to_death_ratio[canonical_intensity_bin(intensity_bin)]
buried = (
    sum(
        building_coefficient[state] * area_by_state[state]
        for state in DamageState
    )
    / city_type_factor
    * occupancy_factor
)
```

Parameter keys:

```text
death.a
death.b
death.c
injury_to_death_ratio.{intensity_bin}
buried.state.{damage_state}
buried.city_type_factor
buried.occupancy_factor
```

Do not hardcode `0.000971`, `0.5`, `-7`, or any other numeric coefficient in production code. Those values may appear only as explicitly synthetic values in `backend/tests/fixtures/loss-test-parameters.yaml` with provenance `synthetic equation test`.

Clamp injuries so `deaths + injuries <= affected_population`; clamp buried to `affected_population - deaths`.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_casualties.py tests/test_loss_buildings.py tests/test_loss_population.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/casualties.py tests/test_loss_casualties.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/casualties.py backend/tests/test_loss_casualties.py
git commit -m "feat: assess death injury and buried casualties"
```

### Task 8: Direct Economic Loss

**Files:**
- Create: `backend/app/loss/economic.py`
- Create: `backend/tests/test_loss_economic.py`

**Interfaces:**
- Consumes: `BuildingDamageResult`, `ScenarioParameters`.
- Produces:
  - `TownEconomicLoss`
  - `EconomicLossResult`
  - `assess_economic_loss(buildings: BuildingDamageResult, parameters: ScenarioParameters) -> EconomicLossResult`

- [ ] **Step 1: Write failing economic tests**

Create `backend/tests/test_loss_economic.py`:

```python
from pathlib import Path

import pytest

from app.loss.domain import LossModelType, LossValueType
from app.loss.economic import assess_economic_loss
from app.loss.models_registry import load_parameter_set
from tests.loss_factories import building_damage_result as _building_result


def test_economic_loss_is_partial_and_equals_components() -> None:
    parameters = load_parameter_set(
        Path("tests/fixtures/loss-test-parameters.yaml")
    ).models[LossModelType.ECONOMIC_LOSS].scenarios[LossValueType.CENTRAL]
    result = assess_economic_loss(_building_result(), parameters)
    town = result.towns["t1"]
    assert result.partial_scope is True
    assert town.reconstruction_loss_yuan >= 0
    assert town.contents_loss_yuan >= 0
    assert town.total_loss_yuan == pytest.approx(
        town.reconstruction_loss_yuan + town.contents_loss_yuan
    )


def test_higher_damage_increases_economic_loss() -> None:
    parameters = load_parameter_set(
        Path("tests/fixtures/loss-test-parameters.yaml")
    ).models[LossModelType.ECONOMIC_LOSS].scenarios[LossValueType.CENTRAL]
    low = assess_economic_loss(_building_result(severe_fraction=0.05), parameters)
    high = assess_economic_loss(_building_result(severe_fraction=0.20), parameters)
    assert high.towns["t1"].total_loss_yuan > low.towns["t1"].total_loss_yuan
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_economic.py -v
```

Expected: FAIL because `assess_economic_loss` does not exist.

- [ ] **Step 3: Implement reconstruction and contents loss**

Create the exact immutable result records:

```python
@dataclass(frozen=True, slots=True)
class TownEconomicLoss:
    town_code: str
    reconstruction_loss_yuan: float
    contents_loss_yuan: float
    total_loss_yuan: float


@dataclass(frozen=True, slots=True)
class EconomicLossResult:
    towns: Mapping[str, TownEconomicLoss]
    partial_scope: bool
    excluded_scope: tuple[str, ...]
```

For each structure, intensity bin, and damage state:

```python
reconstruction = (
    area_m2
    * loss_ratio[structure, state]
    * replacement_cost_yuan_m2[structure]
)
contents = (
    area_m2
    * contents_ratio[structure, state]
    * contents_value_yuan_m2[structure]
)
```

Parameter keys:

```text
loss_ratio.{structure}.{damage_state}
contents_ratio.{structure}.{damage_state}
replacement_cost_yuan_m2.{structure}
contents_value_yuan_m2.{structure}
```

Replacement and contents values are reviewed economic parameters, not fields
invented in the building asset. Set `partial_scope=True` for every result and
include the explicit excluded scope:

```python
excluded_scope = (
    "transport",
    "lifelines",
    "business_interruption",
    "indirect_loss",
    "recovery_duration",
)
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_economic.py tests/test_loss_buildings.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/economic.py tests/test_loss_economic.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/economic.py backend/tests/test_loss_economic.py
git commit -m "feat: assess direct economic loss"
```

### Task 9: Emergency Resource Demand

**Files:**
- Create: `backend/app/loss/resources.py`
- Create: `backend/tests/test_loss_resources.py`

**Interfaces:**
- Consumes: `BuildingDamageResult`, `PopulationImpactResult`, `CasualtyResult`, `ScenarioParameters`.
- Produces:
  - `ResourceDemandInputs`
  - `ResourceDemandValue`
  - `ResourceDemandResult`
  - `assess_resource_demand(buildings: BuildingDamageResult, population: PopulationImpactResult, casualties: CasualtyResult, parameters: ScenarioParameters) -> ResourceDemandResult`

- [ ] **Step 1: Write failing resource tests**

Create `backend/tests/test_loss_resources.py`:

```python
from pathlib import Path

import pytest

from app.loss.domain import LossModelType, LossValueType, ResourceKind
from app.loss.models_registry import load_parameter_set
from app.loss.resources import assess_resource_demand
from tests.loss_factories import (
    building_damage_result as _building_result,
    casualty_result as _casualty_result,
    population_impact_result as _population_result,
)


def test_all_resource_kinds_are_present_and_integral() -> None:
    parameters = load_parameter_set(
        Path("tests/fixtures/loss-test-parameters.yaml")
    ).models[LossModelType.RESOURCE_DEMAND].scenarios[LossValueType.CENTRAL]
    result = assess_resource_demand(
        _building_result(),
        _population_result(),
        _casualty_result(),
        parameters,
    )
    assert set(result.values) == set(ResourceKind)
    assert all(value.quantity is None or value.quantity == int(value.quantity) for value in result.values.values())


def test_missing_coefficient_makes_only_that_resource_unavailable() -> None:
    parameters = load_parameter_set(
        Path("tests/fixtures/loss-test-parameters.yaml")
    ).models[LossModelType.RESOURCE_DEMAND].scenarios[LossValueType.CENTRAL]
    incomplete = type(parameters)(values={
        key: value for key, value in parameters.values.items() if not key.startswith("tent.")
    })
    result = assess_resource_demand(
        _building_result(),
        _population_result(),
        _casualty_result(),
        incomplete,
    )
    assert result.values[ResourceKind.TENT].quantity is None
    assert result.values[ResourceKind.TENT].status == "unavailable"
    assert result.values[ResourceKind.DRINKING_WATER].quantity is not None
```

The exact public records are fixed before the formulas are implemented:

```python
@dataclass(frozen=True, slots=True)
class ResourceDemandInputs:
    affected_population: float
    emergency_shelter_population: float
    deaths: float
    injuries: float
    buried: float


@dataclass(frozen=True, slots=True)
class ResourceDemandValue:
    quantity: int | None
    status: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceDemandResult:
    inputs: ResourceDemandInputs
    values: Mapping[ResourceKind, ResourceDemandValue]
```

`status` is either `available` or `unavailable`. `reason` is `None` for an
available value. For every missing required coefficient, the function returns
`ResourceDemandValue(quantity=None, status="unavailable",
reason=f"missing_parameter:{missing_key}")`; it must never substitute zero.

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_resources.py -v
```

Expected: FAIL because `app.loss.resources` does not exist.

- [ ] **Step 3: Implement independent resource formulas**

Each resource uses a scenario-driven linear expression. `coefficients` is the
validated tuple returned by `require_parameters` for the resource:

```python
base = coefficients[f"{kind.value}.base"]
raw = {
    ResourceKind.RESCUE_TEAM: (
        base
        + coefficients["rescue_team.rescuer_per_missing"] * buried
    ),
    ResourceKind.MEDICAL_TEAM: (
        base
        + coefficients["medical_team.team_per_injury"] * injuries
    ),
    ResourceKind.EPIDEMIC_TEAM: (
        base
        + coefficients["epidemic_team.team_per_shelter_10000"]
        * emergency_shelter_population
        / 10000.0
    ),
    ResourceKind.TENT: (
        base
        + coefficients["tent.per_shelter_population"]
        * emergency_shelter_population
    ),
    ResourceKind.DRINKING_WATER: (
        base
        + coefficients["drinking_water.per_affected_population"]
        * affected_population
    ),
    ResourceKind.TOILET: (
        base
        + coefficients["toilet.per_shelter_population"]
        * emergency_shelter_population
    ),
    ResourceKind.CLOTHING: (
        base
        + coefficients["clothing.per_affected_population"]
        * affected_population
    ),
    ResourceKind.QUILT: (
        base
        + coefficients["quilt.per_shelter_population"]
        * emergency_shelter_population
    ),
    ResourceKind.FOOD: (
        base
        + coefficients["food.per_affected_population"]
        * affected_population
    ),
    ResourceKind.BLANKET: (
        base
        + coefficients["blanket.per_affected_population"]
        * affected_population
    ),
    ResourceKind.STRETCHER: (
        base
        + coefficients["stretcher.per_injury"] * injuries
    ),
    ResourceKind.SICKBED: (
        base
        + coefficients["sickbed.per_injury"] * injuries
    ),
}[kind]
quantity = ceil(max(raw, 0.0))
```

Parameter key prefixes:

```text
rescue_team.*
medical_team.*
epidemic_team.*
tent.*
drinking_water.*
toilet.*
clothing.*
quilt.*
food.*
blanket.*
stretcher.*
sickbed.*
```

Each prefix has a `base` value and the subset of input coefficients required
by that resource. Missing keys produce the exact unavailable record described
above without raising for the other resources. The coefficient map is:

```python
RESOURCE_COEFFICIENTS = {
    ResourceKind.RESCUE_TEAM: (
        "rescue_team.base",
        "rescue_team.rescuer_per_missing",
    ),
    ResourceKind.MEDICAL_TEAM: (
        "medical_team.base",
        "medical_team.team_per_injury",
    ),
    ResourceKind.EPIDEMIC_TEAM: (
        "epidemic_team.base",
        "epidemic_team.team_per_shelter_10000",
    ),
    ResourceKind.TENT: (
        "tent.base",
        "tent.per_shelter_population",
    ),
    ResourceKind.DRINKING_WATER: (
        "drinking_water.base",
        "drinking_water.per_affected_population",
    ),
    ResourceKind.TOILET: (
        "toilet.base",
        "toilet.per_shelter_population",
    ),
    ResourceKind.CLOTHING: (
        "clothing.base",
        "clothing.per_affected_population",
    ),
    ResourceKind.QUILT: (
        "quilt.base",
        "quilt.per_shelter_population",
    ),
    ResourceKind.FOOD: (
        "food.base",
        "food.per_affected_population",
    ),
    ResourceKind.BLANKET: (
        "blanket.base",
        "blanket.per_affected_population",
    ),
    ResourceKind.STRETCHER: (
        "stretcher.base",
        "stretcher.per_injury",
    ),
    ResourceKind.SICKBED: (
        "sickbed.base",
        "sickbed.per_injury",
    ),
}
```

`assess_resource_demand` must first call `require_parameters` for the complete
coefficient tuple for each resource inside its own `try` block. A
`LossParametersUnavailable` for one resource records that resource as
unavailable and continues with the remaining resources.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_resources.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/resources.py tests/test_loss_resources.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/resources.py backend/tests/test_loss_resources.py
git commit -m "feat: assess emergency resource demand"
```

### Task 10: Result Validation and Quality Rules

**Files:**
- Create: `backend/app/loss/validation.py`
- Create: `backend/tests/test_loss_validation.py`

**Interfaces:**
- Consumes: all loss result records and the persisted town/grid relationships.
- Produces:
  - `ValidationIssue`
  - `ValidationResult`
  - `GridResidual`
  - `ValidationContext`
  - `LossValidationSnapshot`
  - `validate_loss_assessment(buildings, population, casualties, economic, resources, *, coverage_ratio: float, grid_residuals: Sequence[GridResidual], context: ValidationContext) -> ValidationResult`

- [ ] **Step 1: Write failing validation tests**

Create `backend/tests/test_loss_validation.py`:

```python
import pytest

from app.loss.domain import LossProductType
from app.loss.validation import validate_loss_assessment
from tests.loss_factories import (
    building_damage_result as _building_result,
    casualty_result as _casualty_result,
    economic_loss_result as _economic_result,
    population_impact_result as _population_result,
    resource_demand_result as _resource_result,
)


CONTEXT = ValidationContext(
    region_profile_version="shanghai-loss-region-v1",
    minimum_town_coverage_ratio=0.95,
    grid_residual_review_threshold=0.01,
    data_asset_snapshot_fingerprint="f" * 64,
    product_snapshot_fingerprints={
        product_type: "f" * 64
        for product_type in (
            LossProductType.BUILDING_DAMAGE,
            LossProductType.POPULATION_IMPACT,
            LossProductType.CASUALTIES,
            LossProductType.ECONOMIC_LOSS,
            LossProductType.RESOURCE_DEMAND,
        )
    },
)


def test_valid_results_have_no_blocking_issues() -> None:
    result = validate_loss_assessment(
        _building_result(),
        _population_result(),
        _casualty_result(),
        _economic_result(),
        _resource_result(),
        coverage_ratio=0.99,
        grid_residuals=(
            GridResidual("town", "t1", 0.0),
            GridResidual("town", "t2", 0.0),
        ),
        context=CONTEXT,
    )
    assert result.valid is True
    assert result.blocking_issues == ()


def test_shelter_above_affected_population_is_invalid() -> None:
    result = validate_loss_assessment(
        _building_result(),
        _population_result(emergency_shelter_population=1000.0, affected_population=900.0),
        _casualty_result(),
        _economic_result(),
        _resource_result(),
        coverage_ratio=1.0,
        grid_residuals=(GridResidual("town", "t1", 0.0),),
        context=CONTEXT,
    )
    assert result.valid is False
    assert result.blocking_issues[0].code == "shelter_exceeds_affected"


def test_maximum_grid_residual_marks_review_without_rewriting_values() -> None:
    result = validate_loss_assessment(
        _building_result(),
        _population_result(),
        _casualty_result(),
        _economic_result(),
        _resource_result(),
        coverage_ratio=1.0,
        grid_residuals=(
            GridResidual("town", "t1", 0.0),
            GridResidual("town", "t2", 0.02),
        ),
        context=CONTEXT,
    )
    assert result.valid is True
    assert result.needs_review is True
    assert result.review_issues[0].code == "grid_residual_threshold"
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_validation.py -v
```

Expected: FAIL because `validate_loss_assessment` does not exist.

- [ ] **Step 3: Implement blocking and review checks**

Create the exact immutable validation records:

```python
@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    blocking: bool
    context: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    blocking_issues: tuple[ValidationIssue, ...]
    review_issues: tuple[ValidationIssue, ...]
    needs_review: bool
    quality_grade: LossQualityGrade


@dataclass(frozen=True, slots=True)
class GridResidual:
    area_scope: str
    area_code: str
    residual: float


@dataclass(frozen=True, slots=True)
class ValidationContext:
    region_profile_version: str
    minimum_town_coverage_ratio: float
    grid_residual_review_threshold: float
    data_asset_snapshot_fingerprint: str | None
    product_snapshot_fingerprints: Mapping[LossProductType, str | None]


@dataclass(frozen=True, slots=True)
class LossValidationSnapshot:
    buildings: BuildingDamageResult
    population: PopulationImpactResult
    casualties: CasualtyResult
    economic: EconomicLossResult
    resources: ResourceDemandResult
    coverage_ratio: float
    grid_residuals: tuple[GridResidual, ...]
    product_snapshot_fingerprints: Mapping[LossProductType, str | None]
```

Blocking checks:

```text
negative_metric
building_damage_exceeds_stock
scenario_order_invalid
shelter_exceeds_affected
sum_scope_mismatch
grid_total_mismatch
coverage_below_minimum
required_parameter_unavailable
```

Review checks:

```text
grid_residual_threshold
order_of_magnitude
stale_data_asset
fallback_model_used
partial_scope
```

Use the region profile thresholds stored in the run snapshot:

```python
if coverage_ratio < context.minimum_town_coverage_ratio:
    blocking.append(
        _issue(
            "coverage_below_minimum",
            threshold=context.minimum_town_coverage_ratio,
            actual=coverage_ratio,
        )
    )

for residual in grid_residuals:
    if abs(residual.residual) > context.grid_residual_review_threshold:
        review.append(
            _issue(
                "grid_residual_threshold",
                area_scope=residual.area_scope,
                area_code=residual.area_code,
                threshold=context.grid_residual_review_threshold,
                actual=residual.residual,
            )
        )

if (
    context.data_asset_snapshot_fingerprint is None
    or any(
        fingerprint != context.data_asset_snapshot_fingerprint
        for fingerprint in context.product_snapshot_fingerprints.values()
    )
    or set(context.product_snapshot_fingerprints)
    != {
        LossProductType.BUILDING_DAMAGE,
        LossProductType.POPULATION_IMPACT,
        LossProductType.CASUALTIES,
        LossProductType.ECONOMIC_LOSS,
        LossProductType.RESOURCE_DEMAND,
    }
):
    review.append(_issue("stale_data_asset"))
```

`grid_total_mismatch` is blocking only when the town and grid scopes cannot
be reconciled structurally, a required residual is missing, or a residual is
non-finite. A finite numeric residual is handled by
`grid_residual_threshold` as a review issue so the original estimates remain
visible.

A blocking issue prevents publication. A review issue preserves the original
numbers, sets `needs_review=true`, and lowers quality to `L3`. The service must
construct `product_snapshot_fingerprints` from the locked data-asset snapshot
stored in each persisted product's `statistics`; it must not infer equality
from product type or from a newly published asset version.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_validation.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/validation.py tests/test_loss_validation.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/validation.py backend/tests/test_loss_validation.py
git commit -m "feat: validate loss assessment results"
```

### Task 11: Loss Repository, Artifact Checksums, and Raster Persistence

**Files:**
- Create: `backend/app/loss/artifacts.py`
- Create: `backend/app/loss/repository.py`
- Create: `backend/tests/test_loss_repository.py`
- Modify: `backend/app/intensity/artifacts.py`
- Modify: `backend/tests/test_intensity_artifacts.py`

**Interfaces:**
- Consumes: existing `RasterCodec`, GridDefinition, database session, loss ORM models.
- Produces:
  - `RasterCodec.content_checksum(definition, bands, manifest, *, checksum_namespace=...) -> str`
  - `LossArtifactCodec.checksum(product: LossProductResult) -> str`
  - `LossArtifactCodec.encode_validation_payload(product_type, result) -> dict[str, object]`
  - `LossArtifactCodec.decode_validation_payload(product_type, payload) -> object`
  - `LossRepository.write_product(session, write: LossProductWrite) -> LossProduct`
  - `LossRepository.get_product(session, run_id, product_type: LossProductType) -> LossProduct | None`
  - `LossRepository.list_products(session, run_id) -> list[LossProduct]`
  - `LossRepository.load_raster(session, product_id) -> tuple[list[np.ndarray], dict]`
  - `LossRepository.load_validation_inputs(session, run_id) -> LossValidationSnapshot`

- [ ] **Step 1: Write failing artifact and repository tests**

Add to `backend/tests/test_intensity_artifacts.py`:

```python
def test_raster_content_checksum_supports_loss_namespace() -> None:
    definition = GridDefinition("g", "EPSG:32651", 1000, 0, 1000, 1, 1)
    bands = [("population", np.array([[1.0]], dtype=np.float64))]
    manifest = {"bands": [{"number": 1, "name": "population"}]}
    intensity = RasterCodec.content_checksum(definition, bands, manifest)
    loss = RasterCodec.content_checksum(
        definition,
        bands,
        manifest,
        checksum_namespace="loss-raster-content-v1",
    )
    assert loss != intensity
```

Create `backend/tests/test_loss_repository.py`:

```python
import numpy as np
import pytest
from sqlalchemy import select

from app.assessment.models import AssessmentTask
from app.loss.domain import (
    LossCalibrationStatus,
    LossMetricValueStatus,
    LossProductType,
    LossProductStatus,
    LossQualityGrade,
    LossValueType,
)
from app.loss.repository import (
    LossMetricValueWrite,
    LossProductWrite,
    LossRepository,
)
from app.intensity.domain import GridDefinition


async def _loss_product_write(
    session_factory,
    run_id,
    *,
    output_checksum: str = "a" * 64,
) -> LossProductWrite:
    async with session_factory() as session:
        task_id = await session.scalar(
            select(AssessmentTask.id)
            .where(AssessmentTask.run_id == run_id)
            .order_by(AssessmentTask.sequence, AssessmentTask.id)
        )
    if task_id is None:
        raise LookupError("seeded run has no assessment task")
    return LossProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=LossProductType.BUILDING_DAMAGE,
        status=LossProductStatus.COMPLETE,
        quality_grade=LossQualityGrade.L3,
        calibration_status=LossCalibrationStatus.UNCALIBRATED,
        coverage_ratio=1.0,
        partial_scope=False,
        needs_review=False,
        spatialized_estimate=False,
        algorithm_version="building-structure-matrix-v1",
        parameter_version="loss-test-only-v1",
        region_profile_version="shanghai-loss-region-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        output_checksum=output_checksum,
        statistics={"town_count": 1},
        metrics=(
            LossMetricValueWrite(
                area_scope="city",
                area_code="310000",
                area_name="上海市",
                metric_key="building.damaged_area",
                value_type=LossValueType.CENTRAL,
                value_status=LossMetricValueStatus.AVAILABLE,
                numeric_value=10.0,
                unit="m2",
                precision=2,
                quality_grade=LossQualityGrade.L3,
                note=None,
            ),
        ),
        raster=None,
        reason=None,
    )


async def test_product_write_is_idempotent_and_immutable(
    session_factory,
    seeded_assessment_run,
) -> None:
    write = await _loss_product_write(session_factory, seeded_assessment_run)
    repository = LossRepository()
    async with session_factory() as session:
        async with session.begin():
            first = await repository.write_product(session, write)
            second = await repository.write_product(session, write)
        assert first.id == second.id


async def test_changed_checksum_cannot_overwrite_existing_product(
    session_factory,
    seeded_assessment_run,
) -> None:
    repository = LossRepository()
    async with session_factory() as session:
        async with session.begin():
            write = await _loss_product_write(session_factory, seeded_assessment_run)
            await repository.write_product(session, write)
        with pytest.raises(ValueError, match="cannot be overwritten"):
            async with session.begin():
                await repository.write_product(
                    session,
                    await _loss_product_write(
                        session_factory,
                        seeded_assessment_run,
                        output_checksum="b" * 64,
                    ),
                )
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_artifacts.py tests/test_loss_repository.py -v
```

Expected: FAIL because the namespace parameter and loss repository do not exist.

- [ ] **Step 3: Implement namespaced checksums and idempotent persistence**

Modify `RasterCodec.content_checksum`:

```python
@staticmethod
def content_checksum(
    definition: GridDefinition,
    bands: Sequence[BandInput],
    band_manifest: dict,
    *,
    checksum_namespace: str = "intensity-raster-content-v1",
) -> str:
    digest = hashlib.sha256(checksum_namespace.encode("ascii") + b"\0")
    return _content_checksum_with_digest(
        digest,
        definition,
        bands,
        band_manifest,
    )
```

Move the existing canonical grid/manifest/band serialization into
`_content_checksum_with_digest`; the public method only chooses the namespace.
Keep `checksum_namespace` ASCII-only and reject an empty namespace.

Create the write contracts with:

```python
from dataclasses import asdict, dataclass
from uuid import UUID
import hashlib
import json

import numpy as np

from app.loss.domain import (
    LossCalibrationStatus,
    LossMetricValueStatus,
    LossProductResult,
    LossProductStatus,
    LossProductType,
    LossQualityGrade,
    LossValueType,
)


@dataclass(frozen=True, slots=True)
class LossMetricValueWrite:
    area_scope: str
    area_code: str
    area_name: str | None
    metric_key: str
    value_type: LossValueType
    value_status: LossMetricValueStatus
    numeric_value: float | None
    unit: str
    precision: int | None
    quality_grade: LossQualityGrade
    note: str | None


@dataclass(frozen=True, slots=True)
class LossRasterBandWrite:
    name: str
    values: np.ndarray
    unit: str
    precision: int | None


@dataclass(frozen=True, slots=True)
class LossRasterWrite:
    raster_version: str
    definition: GridDefinition
    bands: tuple[LossRasterBandWrite, ...]
    band_manifest: dict
    checksum: str
    spatial_allocation_rule: str
    coverage_ratio: float


@dataclass(frozen=True, slots=True)
class LossProductWrite:
    run_id: UUID
    task_id: UUID
    product_type: LossProductType
    status: LossProductStatus
    quality_grade: LossQualityGrade
    calibration_status: LossCalibrationStatus
    coverage_ratio: float
    partial_scope: bool
    needs_review: bool
    spatialized_estimate: bool
    algorithm_version: str
    parameter_version: str
    region_profile_version: str
    input_fingerprint: str
    input_checksum: str
    output_checksum: str
    statistics: dict
    metrics: tuple[LossMetricValueWrite, ...]
    raster: LossRasterWrite | None
    reason: str | None
```

Implement the result checksum without relying on `repr()`:

```python
class LossArtifactCodec:
    CHECKSUM_NAMESPACE = "loss-product-result-v1"

    @classmethod
    def checksum(cls, product: LossProductResult) -> str:
        payload = json.dumps(
            {
                "namespace": cls.CHECKSUM_NAMESPACE,
                "product": asdict(product),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Each successful model product stores two audit fields inside `statistics`:

```python
{
    "data_asset_snapshot_fingerprint": snapshot_checksum,
    "validation_payload": LossArtifactCodec.encode_validation_payload(
        product_type,
        domain_result,
    ),
}
```

`encode_validation_payload` uses dataclass field names, enum values, sorted
mapping keys, and JSON primitives only. `decode_validation_payload` performs
the inverse, rejects unknown or missing fields, and returns the exact result
record from Tasks 5-9. `load_validation_inputs` requires exactly one current
product for each of the five model types, decodes all payloads, reads each
product's `data_asset_snapshot_fingerprint`, and derives `coverage_ratio` and
`grid_residuals` from persisted town metrics plus the building-damage grid
raster manifest.
The manifest uses `reconciliation.<area_scope>.<area_code>.residual` for each
town residual; `load_validation_inputs` validates that every residual is
finite and that the town set matches the persisted town metrics. It also
returns the exact `product_snapshot_fingerprints` mapping stored by the five
products.

Use a transaction and `SELECT ... FOR UPDATE` on the product key before insert. On an existing immutable product, compare every immutable field and return the existing row when equal; raise `ValueError("published loss product cannot be overwritten")` when different.

Persist large rasters in `loss_product_rasters.rast`; do not store cell values in JSONB.

Enforce the numeric contract before writing:

```python
if write.numeric_value is None:
    if write.value_status not in {
        LossMetricValueStatus.UNAVAILABLE,
        LossMetricValueStatus.NOT_APPLICABLE,
    }:
        raise ValueError("null metric requires unavailable or not_applicable status")
elif write.value_status == LossMetricValueStatus.ZERO and write.numeric_value != 0:
    raise ValueError("zero status requires a zero value")
elif write.value_status in {
    LossMetricValueStatus.UNAVAILABLE,
    LossMetricValueStatus.NOT_APPLICABLE,
}:
    raise ValueError("unavailable metric must not carry a numeric value")
```

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_intensity_artifacts.py tests/test_loss_repository.py tests/test_intensity_repository.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/artifacts.py app/loss/repository.py app/intensity/artifacts.py tests/test_loss_repository.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/artifacts.py backend/app/loss/repository.py backend/app/intensity/artifacts.py backend/tests/test_loss_repository.py backend/tests/test_intensity_artifacts.py
git commit -m "feat: persist loss products and rasters"
```

### Task 12: Loss Assessment Service

**Files:**
- Create: `backend/app/loss/service.py`
- Create: `backend/tests/test_loss_service.py`
- Create: `backend/tests/test_loss_service_execution.py`

**Interfaces:**
- Consumes: `LossModelRegistry`, `ParameterSet`, `LossExposureService`, `TownIntensityDistributionService`, all model modules, `LossRepository`, and `AssessmentRepository`.
- Produces:
  - `LossTaskOutcome`
  - `LossChainPerformanceResult`
  - `LossAssessmentService(session_factory, *, exposure_service=None, parameter_loader=None, repository=None, assessment_repository=None, region_profile=None, intensity_repository=None)`
  - `LossAssessmentService.run_buildings(run_id: str) -> LossTaskOutcome`
  - `LossAssessmentService.run_population(run_id: str) -> LossTaskOutcome`
  - `LossAssessmentService.run_casualties(run_id: str) -> LossTaskOutcome`
  - `LossAssessmentService.run_economic(run_id: str) -> LossTaskOutcome`
  - `LossAssessmentService.run_resources(run_id: str) -> LossTaskOutcome`
  - `LossAssessmentService.run_validate(run_id: str) -> LossTaskOutcome`
  - `LossAssessmentService._load_context(session, run_id: UUID, exposure: ExposureDataset) -> LossRunContext`

- [ ] **Step 1: Write failing service tests**

Create `backend/tests/test_loss_service.py`:

```python
from app.loss.domain import LossModelType
from app.loss.service import LossAssessmentService


def test_algorithm_versions_are_stable() -> None:
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.BUILDING_DAMAGE] == (
        "building-structure-matrix-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.POPULATION_IMPACT] == (
        "population-intensity-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.CASUALTIES] == (
        "casualty-building-intensity-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.ECONOMIC_LOSS] == (
        "economic-building-loss-v1"
    )
    assert LossAssessmentService.ALGORITHM_VERSIONS[LossModelType.RESOURCE_DEMAND] == (
        "resource-linear-demand-v1"
    )
```

Create `backend/tests/test_loss_service_execution.py`. This is an
orchestration test, not a database integration test: it injects a deterministic
context and repository while exercising the real four scenario models and the
real six service methods. The Task 17 performance suite remains responsible
for real PostgreSQL/PostGIS and HTTP evidence.

The service test suite must also include
`test_load_context_freezes_region_profile_and_thresholds`: it calls the real
`_load_context` once, changes the service's in-memory profile thresholds,
calls `_load_context` again, and asserts both the returned values and
`run.snapshot["region_profile"]` still equal the first profile version,
coverage threshold, and residual threshold.

```python
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.loss.domain import LossModelType, LossProductType, LossRunContext
from app.loss.exposure import build_exposure_dataset
from app.loss.artifacts import LossArtifactCodec
from app.loss.models_registry import load_parameter_set
from app.loss.region import load_region_loss_profile
from app.loss.service import LossAssessmentService
from app.loss.spatial import LossGridCell, TownIntensityDistributionService


class _FakeSession:
    @asynccontextmanager
    async def begin(self):
        yield self


class _FakeSessionFactory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return _FakeSession()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakeAssessmentRepository:
    def __init__(self):
        self.completed = []
        self.tasks = {}

    async def start_task(
        self,
        session,
        run_id,
        task_key,
        algorithm_version,
        input_fingerprint,
    ):
        task = self.tasks.get(task_key)
        if task is None:
            task = SimpleNamespace(
                id=uuid4(),
                status="running",
                task_key=task_key,
            )
            self.tasks[task_key] = task
        return task

    async def complete_task(
        self,
        session,
        task_id,
        output_checksum,
        result,
    ):
        task = next(
            task
            for task in self.tasks.values()
            if task.id == task_id
        )
        task.status = "succeeded"
        task.output_checksum = output_checksum
        self.completed.append(
            (task_id, output_checksum, dict(result))
        )
        return task

    async def record_task_failure_audit(self, *args, **kwargs):
        return None


class _FakeLossRepository:
    def __init__(self):
        self.rows: dict[LossProductType, SimpleNamespace] = {}
        self.writes = []

    async def get_product(self, session, run_id, product_type):
        return self.rows.get(product_type)

    async def list_products(self, session, run_id):
        return [
            row
            for product_type, row in self.rows.items()
            if product_type is not LossProductType.VALIDATION
        ]

    async def load_validation_inputs(self, session, run_id):
        from app.loss.validation import (
            GridResidual,
            LossValidationSnapshot,
        )

        decoded = {
            product_type: LossArtifactCodec.decode_validation_payload(
                product_type,
                row.write.statistics["validation_payload"],
            )
            for product_type, row in self.rows.items()
            if product_type is not LossProductType.VALIDATION
        }
        return LossValidationSnapshot(
            buildings=decoded[LossProductType.BUILDING_DAMAGE],
            population=decoded[LossProductType.POPULATION_IMPACT],
            casualties=decoded[LossProductType.CASUALTIES],
            economic=decoded[LossProductType.ECONOMIC_LOSS],
            resources=decoded[LossProductType.RESOURCE_DEMAND],
            coverage_ratio=1.0,
            grid_residuals=(GridResidual("town", "t1", 0.0),),
            product_snapshot_fingerprints={
                product_type: "f" * 64
                for product_type in decoded
            },
        )

    async def write_product(self, session, write):
        existing = self.rows.get(write.product_type)
        if existing is not None:
            return existing
        row = SimpleNamespace(
            id=uuid4(),
            status=write.status,
            output_checksum=write.output_checksum,
            write=write,
        )
        self.rows[write.product_type] = row
        self.writes.append(write)
        return row


class _FakeExposureService:
    async def prepare(self, session, *, run_id):
        return build_exposure_dataset(
            snapshot_checksum="f" * 64,
            towns=[
                {
                    "town_code": "t1",
                    "county_code": "c1",
                    "town_name": "测试镇",
                    "population_total": 10000.0,
                }
            ],
            geometries=[
                {
                    "town_code": "t1",
                    "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
                }
            ],
            buildings=[
                {
                    "town_code": "t1",
                    "structure": "rc_frame",
                    "era": "1990_1999",
                    "area_m2": 10000.0,
                }
            ],
        )


class _FakeSpatialService:
    async def load_cells(self, session, *, run_id):
        return (LossGridCell("g1", "t1", 1.0, 1.0),)


async def test_loss_service_executes_all_six_tasks_idempotently(
    monkeypatch,
) -> None:
    run_id = uuid4()
    repository = _FakeLossRepository()
    profile = load_region_loss_profile(
        Path("/config/loss/shanghai-region.yaml")
    )
    parameters = load_parameter_set(
        Path("/app/tests/fixtures/loss-test-parameters.yaml")
    )

    async def load_parameters(session, *, run_id, profile):
        return parameters

    service = LossAssessmentService(
        _FakeSessionFactory(),
        exposure_service=_FakeExposureService(),
        parameter_loader=load_parameters,
        repository=repository,
        assessment_repository=_FakeAssessmentRepository(),
        region_profile=profile,
        spatial_service=_FakeSpatialService(),
    )

    async def load_context(self, session, run_id, exposure):
        return LossRunContext(
            run_id=str(run_id),
            event_id="e1",
            revision_id="r1",
            report_ingested_at=datetime(2026, 9, 28, tzinfo=UTC),
            region_id="shanghai",
            region_profile_version=profile.version,
            minimum_town_coverage_ratio=profile.minimum_town_coverage_ratio,
            grid_residual_review_threshold=(
                profile.grid_residual_review_threshold
            ),
            fused_intensity_product_id="i1",
            fused_intensity_checksum="a" * 64,
            data_asset_snapshot_checksum=exposure.snapshot_checksum,
        )

    monkeypatch.setattr(
        LossAssessmentService,
        "_load_context",
        load_context,
    )

    first = await service.run_buildings(str(run_id))
    second = await service.run_buildings(str(run_id))
    population = await service.run_population(str(run_id))
    casualties = await service.run_casualties(str(run_id))
    economic = await service.run_economic(str(run_id))
    resources = await service.run_resources(str(run_id))
    validation = await service.run_validate(str(run_id))

    assert first.product_id == second.product_id
    assert first.status == "succeeded"
    assert population.status == "succeeded"
    assert casualties.status == "succeeded"
    assert economic.status == "succeeded"
    assert resources.status == "succeeded"
    assert validation.status == "succeeded"
    assert len(service._assessment_repository.completed) == 6
    assert {
        result["task_key"]
        for _, _, result in service._assessment_repository.completed
    } == {
        "loss.buildings",
        "loss.population",
        "loss.casualties",
        "loss.economic",
        "loss.resources",
        "loss.validate",
    }
```

- [ ] **Step 2: Run the service tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_service.py tests/test_loss_service_execution.py -v
```

Expected: FAIL because `LossAssessmentService` does not exist.

- [ ] **Step 3: Implement service orchestration and fingerprints**

Define:

```python
from app.assessment.models import AssessmentRun
from app.assessment.repository import AssessmentRepository
from app.intensity.artifacts import RasterCodec
from app.intensity.domain import ProductType
from app.intensity.repository import IntensityRepository
from app.loss.repository import (
    LossProductWrite,
    LossRasterBandWrite,
    LossRasterWrite,
    LossRepository,
)
from app.loss.region import load_region_loss_profile_from_mapping
from app.loss.spatial import (
    TownIntensityDistributionService,
    allocate_continuous,
    allocate_integers,
)


@dataclass(frozen=True, slots=True)
class LossTaskOutcome:
    product_id: UUID
    task_key: str
    status: str
    started_at: datetime
    completed_at: datetime
    stage_seconds: dict[str, float]


@dataclass(frozen=True, slots=True)
class LossChainPerformanceResult:
    status: str
    report_ingested_at: datetime
    query_ready_at: datetime
    elapsed_seconds: float
    stage_seconds: dict[str, float]


class LossAssessmentService:
    ALGORITHM_VERSIONS = {
        LossModelType.BUILDING_DAMAGE: "building-structure-matrix-v1",
        LossModelType.POPULATION_IMPACT: "population-intensity-v1",
        LossModelType.CASUALTIES: "casualty-building-intensity-v1",
        LossModelType.ECONOMIC_LOSS: "economic-building-loss-v1",
        LossModelType.RESOURCE_DEMAND: "resource-linear-demand-v1",
    }

    def __init__(
        self,
        session_factory,
        *,
        exposure_service=None,
        parameter_loader=None,
        repository=None,
        assessment_repository=None,
        region_profile=None,
        intensity_repository=None,
        spatial_service=None,
    ) -> None:
        self._session_factory = session_factory
        self._exposure_service = exposure_service or LossExposureService(
            snapshots=DataAssetSnapshotService()
        )
        self._parameter_loader = parameter_loader or load_locked_parameter_set
        self._repository = repository or LossRepository()
        self._assessment_repository = assessment_repository or AssessmentRepository()
        self._region_profile = region_profile or load_region_loss_profile(
            settings.loss_region_profile_path
        )
        self._intensity_repository = intensity_repository or IntensityRepository()
        self._spatial_service = spatial_service or TownIntensityDistributionService()

VALIDATION_ALGORITHM_VERSION = "loss-validation-v1"
```

`_load_context` is the only database lookup needed by the service tests. It
must load `AssessmentRun` and the latest succeeded `intensity.fusion` product,
reject a missing run or fusion product, and return the exact `LossRunContext`
used by every task:

The first loss task locks the run and writes the complete
`RegionLossProfile.to_snapshot()` payload under
`run.snapshot["region_profile"]` before any task fingerprint is calculated.
Later tasks, retries, and validation reuse that frozen payload instead of
reloading `/config/loss/shanghai-region.yaml`.

```python
async def _load_context(
    self,
    session: AsyncSession,
    run_id: UUID,
    exposure: ExposureDataset,
) -> LossRunContext:
    run = await session.get(
        AssessmentRun,
        run_id,
        with_for_update=True,
    )
    if run is None:
        raise LookupError("assessment run not found")
    fusion = await self._intensity_repository.get_product(
        session,
        run_id=run_id,
        product_type=ProductType.FUSION,
    )
    if (
        fusion is None
        or fusion["status"] != "available"
        or not fusion["output_checksum"]
    ):
        raise LookupError("succeeded intensity fusion product not found")
    region_id = run.snapshot.get("region_id")
    if not isinstance(region_id, str) or not region_id:
        raise ValueError("assessment run snapshot requires region_id")
    run_snapshot = dict(run.snapshot or {})
    raw_profile = run_snapshot.get("region_profile")
    if raw_profile is None:
        frozen_profile = self._region_profile
        raw_profile = frozen_profile.to_snapshot()
        run_snapshot["region_profile"] = raw_profile
        run.snapshot = run_snapshot
    elif isinstance(raw_profile, dict):
        frozen_profile = load_region_loss_profile_from_mapping(raw_profile)
    else:
        raise ValueError("assessment run region_profile snapshot is invalid")
    if frozen_profile.region_id != region_id:
        raise ValueError("assessment run region profile does not match region_id")
    return LossRunContext(
        run_id=str(run.id),
        event_id=str(run.event_id),
        revision_id=str(run.revision_id),
        report_ingested_at=run.report_ingested_at,
        region_id=region_id,
        region_profile_version=frozen_profile.version,
        minimum_town_coverage_ratio=(
            frozen_profile.minimum_town_coverage_ratio
        ),
        grid_residual_review_threshold=(
            frozen_profile.grid_residual_review_threshold
        ),
        fused_intensity_product_id=str(fusion["id"]),
        fused_intensity_checksum=fusion["output_checksum"],
        data_asset_snapshot_checksum=exposure.snapshot_checksum,
    )
```

Inject `intensity_repository` into the constructor with
`IntensityRepository()` as the default. It is a test seam only; production
always uses the repository backed by the same database session.

Every `LossTaskOutcome.stage_seconds` keys its measured internal stages. The
fixed performance helper aggregates these keys into
`LossChainPerformanceResult.stage_seconds` using the four budget names from
Task 17; no caller infers timing from log timestamps.

For every run method:

1. In one database transaction, create or load the locked data-asset snapshot
   and prepare the `ExposureDataset` before any task fingerprint is written.
2. Load `AssessmentRun` and the latest succeeded `intensity.fusion` product.
3. Resolve the region profile and parameter set.
4. Build the deterministic `input_fingerprint` from:

```python
{
    "run_id": str(run.id),
    "revision_id": str(run.revision_id),
    "fusion_product_id": str(fusion["id"]),
    "fusion_checksum": fusion.output_checksum,
    "data_snapshot_checksum": exposure.snapshot_checksum,
    "region_profile_version": profile.version,
    "model_version": model.formula_version,
    "parameter_version": parameter_set.version,
    "scenario_versions": ["low", "central", "high"],
}
```

5. Call `AssessmentRepository.start_task`.
6. If the task already succeeded, load the existing product and return its
   outcome without writing anything.
7. Run the deterministic model for all three scenarios.
8. Build one derived 1 km raster for every model product that has at least one
   numeric town-scope metric:
   - Load `grid_cells` through
     `TownIntensityDistributionService.load_cells(session, run_id=run.id)`.
   - Allocate each numeric `(metric_key, value_type)` town series with
     `allocate_continuous`; use `allocate_integers` for resource quantities.
     Preserve exact totals and deterministic cell ordering.
   - Build `LossRasterBandWrite` rows and a `band_manifest` containing band
     number, name, metric key, scenario, unit, and precision. Omit bands whose
     town value is `None`; never encode an unavailable value as zero.
   - For the building-damage raster only, add
     `reconciliation.town.<town_code>.residual` for the central
     `collapsed_area_m2` metric. Each residual equals the sum of allocated cells
     minus the persisted town value.
   - Compute the raster checksum with
     `RasterCodec.content_checksum(definition, bands, band_manifest,
     checksum_namespace="loss-raster-content-v1")`, set
     `spatialized_estimate=true`, and attach the resulting `LossRasterWrite` to
     the product write. A product with no numeric town-scope metric has
     `raster=None` and `spatialized_estimate=false`.
9. Persist low, central, and high values and the derived raster in one product. Include
   `data_asset_snapshot_fingerprint`, `validation_payload`, and
   `stage_seconds` in `statistics`.
10. Convert every result value to `LossMetricValueWrite`: use `zero` for an
   exact zero, `available` for a positive value, and `unavailable` with
   `numeric_value=None` for a missing resource coefficient. Never write a
   missing resource as zero.
11. Call
    `AssessmentRepository.complete_task(session, task.id,
    product.output_checksum,
    {"product_id": str(product.id), "task_key": task.task_key,
    "stage_seconds": stage_seconds})` in the same transaction as the product
    write.
12. On failure, call `AssessmentRepository.fail_task` with a sanitized error
   category, then record the same sanitized summary through
   `AssessmentRepository.record_task_failure_audit`; do not leave the task in
   `running`.

`run_validate` calls `LossRepository.load_validation_inputs(session, run_id)`,
constructs `ValidationContext` from the five persisted products' snapshot
fingerprints, the trusted `LossRunContext.data_asset_snapshot_checksum`,
`LossRunContext.region_profile_version`,
`LossRunContext.minimum_town_coverage_ratio`, and
`LossRunContext.grid_residual_review_threshold`,
invokes `validate_loss_assessment`, writes a
`LossProductType.VALIDATION` product, and calls `complete_task` with output
checksum from the validation product using
`AssessmentRepository.complete_task(session, task.id, output_checksum,
result)`. The result contains `valid`, `needs_review`, `quality_grade`, and
`stage_seconds`. It sets `needs_review=true` and quality `L3` for review issues
but does not change the underlying metric values.

- [ ] **Step 4: Run focused tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_service.py tests/test_loss_service_execution.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/service.py tests/test_loss_service.py tests/test_loss_service_execution.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/service.py backend/tests/test_loss_service.py backend/tests/test_loss_service_execution.py
git commit -m "feat: orchestrate loss assessment service"
```

### Task 13: Temporal Workflow Integration and Completion Semantics

**Files:**
- Modify: `backend/app/assessment/plan.py`
- Modify: `backend/app/assessment/temporal.py`
- Modify: `backend/app/assessment/worker.py`
- Modify: `backend/app/assessment/repository.py`
- Modify: `backend/tests/test_assessment_plan.py`
- Modify: `backend/tests/test_assessment_temporal.py`
- Modify: `backend/tests/test_assessment_schema.py`

**Interfaces:**
- Consumes: `LossAssessmentService` methods from Task 12.
- Produces:
  - planned tasks `loss.resources` and `loss.validate`
  - loss activity names
  - completion semantics including all required loss tasks
  - worker registration for loss activities

- [ ] **Step 1: Write failing plan, temporal, and completion tests**

Update `backend/tests/test_assessment_plan.py`:

```python
assert [task.task_key for task in plan] == [
    "intensity.model",
    "intensity.instrument",
    "intensity.fusion",
    "loss.population",
    "loss.casualties",
    "loss.buildings",
    "loss.economic",
    "loss.resources",
    "loss.validate",
    "report.rapid_assessment",
    "workgroup.response_tasks",
]
```

Update `backend/tests/test_assessment_temporal.py` to assert:

```python
assert statuses["loss.buildings"] == "succeeded"
assert statuses["loss.population"] == "succeeded"
assert statuses["loss.casualties"] == "succeeded"
assert statuses["loss.economic"] == "succeeded"
assert statuses["loss.resources"] == "succeeded"
assert statuses["loss.validate"] == "succeeded"
assert statuses["report.rapid_assessment"] == "skipped"
```

Add a failure test:

```python
async def test_building_failure_prevents_casualty_and_economic_activities(
    session_factory,
) -> None:
    calls = []

    class FailingLossService:
        async def run_buildings(self, run_id):
            calls.append("buildings")
            raise ValueError("vulnerability row unavailable")

        async def run_population(self, run_id):
            calls.append("population")
            return _loss_outcome(run_id, "loss.population")

    result = await _execute_loss_workflow(session_factory, FailingLossService())
    assert result.status == "failed"
    assert "casualties" not in calls
    assert "economic" not in calls
```

Add these exact helpers to the same test module:

```python
from uuid import uuid4

from app.loss.service import LossTaskOutcome


def _loss_outcome(run_id: str, task_key: str) -> LossTaskOutcome:
    now = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
    return LossTaskOutcome(
        product_id=uuid4(),
        task_key=task_key,
        status="succeeded",
        started_at=now,
        completed_at=now,
        stage_seconds={},
    )


async def _execute_loss_workflow(session_factory, loss_service):
    request = await _create_workflow_input(session_factory)
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _test_intensity_service(session_factory),
        loss_service_factory=lambda: loss_service,
    )
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue="assessment-loss-failure-test",
            workflows=[AssessmentWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
            ],
        ):
            return await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=f"assessment-loss-failure:{request.event_id}",
                task_queue="assessment-loss-failure-test",
            )
```

- [ ] **Step 2: Run focused tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_plan.py tests/test_assessment_temporal.py tests/test_assessment_schema.py -v
```

Expected: FAIL because `loss.resources` and `loss.validate` are not planned or executed.

- [ ] **Step 3: Extend the plan, workflow, activities, worker, and completion policy**

Add tasks:

```python
PlannedAssessmentTask(
    task_key="loss.resources",
    task_type="loss",
    component="resource_demand",
    priority=86,
    sequence=8,
    deadline_offset_seconds=210,
)
PlannedAssessmentTask(
    task_key="loss.validate",
    task_type="loss",
    component="loss_validation",
    priority=85,
    sequence=9,
    deadline_offset_seconds=220,
)
```

Move report and workgroup sequences to 10 and 11.

In `AssessmentWorkflow.run`, after fusion succeeds:

```python
outcome = "completed"
try:
    await workflow.execute_activity(
        "run_intensity_fusion",
        IntensityActivityInput(prepared.run_id, "intensity.fusion"),
        start_to_close_timeout=timedelta(seconds=1800),
        retry_policy=_retry_policy(),
    )

    building_handle = workflow.start_activity(
        "run_loss_buildings",
        IntensityActivityInput(prepared.run_id, "loss.buildings"),
        start_to_close_timeout=timedelta(seconds=1800),
        retry_policy=_retry_policy(),
    )
    population_handle = workflow.start_activity(
        "run_loss_population",
        IntensityActivityInput(prepared.run_id, "loss.population"),
        start_to_close_timeout=timedelta(seconds=1800),
        retry_policy=_retry_policy(),
    )
    first_stage = await asyncio.gather(
        building_handle,
        population_handle,
        return_exceptions=True,
    )
    if any(isinstance(value, BaseException) for value in first_stage):
        outcome = "failed"

    if outcome == "completed":
        casualty_handle = workflow.start_activity(
            "run_loss_casualties",
            IntensityActivityInput(prepared.run_id, "loss.casualties"),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=_retry_policy(),
        )
        economic_handle = workflow.start_activity(
            "run_loss_economic",
            IntensityActivityInput(prepared.run_id, "loss.economic"),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=_retry_policy(),
        )
        second_stage = await asyncio.gather(
            casualty_handle,
            economic_handle,
            return_exceptions=True,
        )
        if any(isinstance(value, BaseException) for value in second_stage):
            outcome = "failed"

    if outcome == "completed":
        await workflow.execute_activity(
            "run_loss_resources",
            IntensityActivityInput(prepared.run_id, "loss.resources"),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=_retry_policy(),
        )
        await workflow.execute_activity(
            "run_loss_validate",
            IntensityActivityInput(prepared.run_id, "loss.validate"),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=_retry_policy(),
        )
except ActivityError:
    outcome = "failed"

return await self._finalize(prepared, outcome=outcome)
```

The existing earlier fusion `try/except` is folded into this single loss
chain so every required activity failure reaches `_finalize`. If either
parallel first-stage activity fails, the workflow does not start casualties,
economic loss, resources, or validation. If casualties or economic loss
fails, resources and validation are skipped. `ActivityError` is the expected
final activity failure after retry exhaustion; it must not escape the workflow
without calling `_finalize`.

Add activity methods:

```python
@activity.defn(name="run_loss_buildings")
async def run_loss_buildings(self, request: IntensityActivityInput):
    service = self._loss_service_factory()
    return await service.run_buildings(request.run_id)

@activity.defn(name="run_loss_population")
async def run_loss_population(self, request: IntensityActivityInput):
    service = self._loss_service_factory()
    return await service.run_population(request.run_id)

@activity.defn(name="run_loss_casualties")
async def run_loss_casualties(self, request: IntensityActivityInput):
    service = self._loss_service_factory()
    return await service.run_casualties(request.run_id)

@activity.defn(name="run_loss_economic")
async def run_loss_economic(self, request: IntensityActivityInput):
    service = self._loss_service_factory()
    return await service.run_economic(request.run_id)

@activity.defn(name="run_loss_resources")
async def run_loss_resources(self, request: IntensityActivityInput):
    service = self._loss_service_factory()
    return await service.run_resources(request.run_id)

@activity.defn(name="run_loss_validate")
async def run_loss_validate(self, request: IntensityActivityInput):
    service = self._loss_service_factory()
    return await service.run_validate(request.run_id)
```

Extend `AssessmentActivities.__init__` with
`loss_service_factory: Callable[[], LossAssessmentService] | None = None`,
store it as `self._loss_service_factory`, and add:

```python
def _default_loss_service_factory(self) -> LossAssessmentService:
    from app.loss.service import LossAssessmentService

    return LossAssessmentService(session_factory=self._session_factory)
```

Use `self._loss_service_factory()` in all six activities, matching the
existing injectable intensity-service pattern. Do not construct the service
inside the workflow.

Register all six activities in `build_worker`.

Change `AssessmentRepository.complete_run` to require:

```python
required_task_keys = {
    "intensity.model",
    "intensity.fusion",
    "loss.population",
    "loss.buildings",
    "loss.casualties",
    "loss.economic",
    "loss.resources",
    "loss.validate",
}
```

Use `algorithm_bundle_version="intensity-loss-v1"` in `finalize_assessment`.

- [ ] **Step 4: Run focused tests, lifecycle tests, and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_plan.py tests/test_assessment_temporal.py tests/test_assessment_task_lifecycle.py tests/test_assessment_schema.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/assessment tests/test_assessment_plan.py tests/test_assessment_temporal.py tests/test_assessment_task_lifecycle.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/assessment/plan.py backend/app/assessment/temporal.py backend/app/assessment/worker.py backend/app/assessment/repository.py backend/tests/test_assessment_plan.py backend/tests/test_assessment_temporal.py backend/tests/test_assessment_schema.py
git commit -m "feat: execute loss tasks in assessment workflow"
```

### Task 14: Loss Result API

**Files:**
- Create: `backend/app/loss/schemas.py`
- Create: `backend/app/loss/router.py`
- Create: `backend/tests/test_loss_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/assessment/schemas.py`
- Modify: `backend/app/assessment/router.py`

**Interfaces:**
- Produces endpoints:
  - `GET /api/v1/assessments/runs/{run_id}/loss`
  - `GET /api/v1/assessments/runs/{run_id}/loss/products/{product_type}`
  - `GET /api/v1/assessments/runs/{run_id}/loss/areas?scope=city|county|town`
  - `GET /api/v1/assessments/runs/{run_id}/loss/artifact?product_id={product_id}&band={band}`
  - `GET /api/v1/assessments/runs/{run_id}/loss/artifact/{product_id}/{band}/{z}/{x}/{y}.png`
- Extends `AssessmentRunStatusResponse` with `loss: LossResultResponse | None`.

- [ ] **Step 1: Write failing API tests**

Create `backend/tests/test_loss_api.py`:

```python
from dataclasses import dataclass
from uuid import UUID, uuid4

import numpy as np
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.assessment.models import AssessmentTask
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.intensity.domain import GridDefinition
from app.loss.domain import (
    LossCalibrationStatus,
    LossMetricValueStatus,
    LossProductStatus,
    LossProductType,
    LossQualityGrade,
    LossValueType,
)
from app.loss.repository import (
    LossMetricValueWrite,
    LossProductWrite,
    LossRasterBandWrite,
    LossRasterWrite,
    LossRepository,
)
from app.main import app


@dataclass(frozen=True, slots=True)
class SeededLossApiProducts:
    run_id: UUID
    building_product_id: UUID


async def _seed_loss_products(
    session_factory,
    run_id: UUID,
) -> SeededLossApiProducts:
    async with session_factory() as session:
        async with session.begin():
            task_ids = dict(
                (
                    await session.execute(
                        select(
                            AssessmentTask.task_key,
                            AssessmentTask.id,
                        ).where(AssessmentTask.run_id == run_id)
                    )
                ).all()
            )
            repository = LossRepository()
            building = await repository.write_product(
                session,
                _building_write(run_id, task_ids["loss.buildings"]),
            )
            for product_type, task_key in (
                (LossProductType.POPULATION_IMPACT, "loss.population"),
                (LossProductType.CASUALTIES, "loss.casualties"),
                (LossProductType.ECONOMIC_LOSS, "loss.economic"),
                (LossProductType.RESOURCE_DEMAND, "loss.resources"),
                (LossProductType.VALIDATION, "loss.validate"),
            ):
                await repository.write_product(
                    session,
                    _metric_write(run_id, task_ids[task_key], product_type),
                )
    return SeededLossApiProducts(
        run_id=run_id,
        building_product_id=building.id,
    )


def _metric(
    metric_key: str = "building.damaged_area",
    numeric_value: float | None = 10.0,
) -> LossMetricValueWrite:
    status = (
        LossMetricValueStatus.AVAILABLE
        if numeric_value is not None
        else LossMetricValueStatus.UNAVAILABLE
    )
    return LossMetricValueWrite(
        area_scope="city",
        area_code="310000",
        area_name="上海市",
        metric_key=metric_key,
        value_type=LossValueType.CENTRAL,
        value_status=status,
        numeric_value=numeric_value,
        unit="m2",
        precision=2,
        quality_grade=LossQualityGrade.L2,
        note=None,
    )


def _building_write(run_id: UUID, task_id: UUID) -> LossProductWrite:
    definition = GridDefinition(
        "loss-api-grid",
        "EPSG:32651",
        1000,
        0,
        1000,
        1,
        1,
    )
    values = np.asarray([[1.0]], dtype=np.float64)
    raster = LossRasterWrite(
        raster_version="loss-api-grid-v1",
        definition=definition,
        bands=(
            LossRasterBandWrite(
                name="buildings_collapsed_area_m2",
                values=values,
                unit="m2",
                precision=2,
            ),
        ),
        band_manifest={
            "bands": [
                {
                    "number": 1,
                    "name": "buildings_collapsed_area_m2",
                    "unit": "m2",
                }
            ]
        },
        checksum="b" * 64,
        spatial_allocation_rule="town-uniform-v1",
        coverage_ratio=1.0,
    )
    return LossProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=LossProductType.BUILDING_DAMAGE,
        status=LossProductStatus.COMPLETE,
        quality_grade=LossQualityGrade.L2,
        calibration_status=LossCalibrationStatus.REFERENCE_UNCALIBRATED,
        coverage_ratio=1.0,
        partial_scope=False,
        needs_review=False,
        spatialized_estimate=True,
        algorithm_version="building-structure-matrix-v1",
        parameter_version="shanghai-loss-reference-v1",
        region_profile_version="shanghai-loss-region-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        output_checksum="e" * 64,
        statistics={"town_count": 1},
        metrics=(_metric(),),
        raster=raster,
        reason=None,
    )


def _metric_write(
    run_id: UUID,
    task_id: UUID,
    product_type: LossProductType,
) -> LossProductWrite:
    return LossProductWrite(
        run_id=run_id,
        task_id=task_id,
        product_type=product_type,
        status=LossProductStatus.COMPLETE,
        quality_grade=LossQualityGrade.L2,
        calibration_status=LossCalibrationStatus.REFERENCE_UNCALIBRATED,
        coverage_ratio=1.0,
        partial_scope=product_type is LossProductType.ECONOMIC_LOSS,
        needs_review=False,
        spatialized_estimate=False,
        algorithm_version=f"{product_type.value}-v1",
        parameter_version="shanghai-loss-reference-v1",
        region_profile_version="shanghai-loss-region-v1",
        input_fingerprint="c" * 64,
        input_checksum="d" * 64,
        output_checksum=f"{product_type.value:0<64}"[:64],
        statistics={},
        metrics=(_metric(f"{product_type.value}.total"),),
        raster=None,
        reason=None,
    )


async def _request(
    path: str,
    *,
    role: str = "group_member",
    method: str = "GET",
):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="operator",
        role=role,
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.request(method, path)
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


async def _request_loss(run_id: UUID):
    return await _request(f"/api/v1/assessments/runs/{run_id}/loss")


async def _request_loss_areas(run_id: UUID, scope: str):
    return await _request(
        f"/api/v1/assessments/runs/{run_id}/loss/areas?scope={scope}"
    )


async def _request_loss_artifact(run_id: UUID, product_id: UUID):
    return await _request(
        f"/api/v1/assessments/runs/{run_id}/loss/artifact"
        f"?product_id={product_id}&band=buildings_collapsed_area_m2"
    )


async def _request_data_asset_publish_as_viewer():
    return await _request(
        f"/api/v1/data-asset-versions/{uuid4()}/publish",
        role="viewer",
        method="POST",
    )


async def test_get_loss_summary_returns_versions_and_quality(
    session_factory,
    seeded_assessment_run,
) -> None:
    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    response = await _request_loss(seeded.run_id)
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(seeded.run_id)
    assert body["products"][0]["status"] == "complete"
    assert body["products"][0]["quality_grade"] == "L2"
    assert body["products"][0]["calibration_status"] == "reference_uncalibrated"
    assert body["products"][0]["metrics"][0]["value_type"] == "central"
    assert body["products"][0]["metrics"][0]["value_status"] == "available"


async def test_get_loss_areas_rejects_invalid_scope(session_factory) -> None:
    response = await _request_loss_areas(uuid4(), "not-a-scope")
    assert response.status_code == 422


async def test_get_loss_artifact_returns_metadata_not_cell_json(
    session_factory,
    seeded_assessment_run,
) -> None:
    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    response = await _request_loss_artifact(
        seeded.run_id,
        seeded.building_product_id,
    )
    assert response.status_code == 200
    assert response.json()["product_id"] == str(seeded.building_product_id)
    assert response.json()["bands"][0]["name"] == "buildings_collapsed_area_m2"
    assert "cells" not in response.json()
    assert response.json()["tile_template"].endswith("/{z}/{x}/{y}.png")


async def test_get_town_loss_areas_returns_geometry_and_metrics(
    session_factory,
    seeded_assessment_run,
) -> None:
    seeded = await _seed_loss_products(session_factory, seeded_assessment_run)
    response = await _request_loss_areas(seeded.run_id, "town")
    assert response.status_code == 200
    feature = response.json()["features"][0]
    assert feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}
    assert feature["metrics"]


async def test_reader_role_cannot_import_or_publish_data_assets() -> None:
    response = await _request_data_asset_publish_as_viewer()
    assert response.status_code == 403
```

- [ ] **Step 2: Run API tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_api.py -v
```

Expected: FAIL because the loss router and schemas do not exist.

- [ ] **Step 3: Implement read APIs and assessment integration**

Create Pydantic response models with the exact fields:

```python
class LossValueResponse(BaseModel):
    area_scope: str
    area_code: str
    area_name: str | None
    metric_key: str
    value_type: str
    value_status: str
    numeric_value: float | None
    unit: str
    precision: int | None
    quality_grade: str
    note: str | None


class LossProductResponse(BaseModel):
    product_id: str
    product_type: str
    status: str
    quality_grade: str
    calibration_status: str
    coverage_ratio: float
    partial_scope: bool
    needs_review: bool
    spatialized_estimate: bool
    algorithm_version: str
    parameter_version: str
    region_profile_version: str
    output_checksum: str | None
    statistics: dict
    metrics: list[LossValueResponse]
    reason: str | None


class LossResultResponse(BaseModel):
    run_id: str
    event_id: str
    revision_id: str
    effective_run_id: str | None
    is_fallback: bool
    products: list[LossProductResponse]


class LossAreaFeatureResponse(BaseModel):
    area_scope: str
    area_code: str
    area_name: str
    geometry: dict
    metrics: list[LossValueResponse]


class LossAreaResponse(BaseModel):
    run_id: str
    scope: str
    features: list[LossAreaFeatureResponse]


class LossGridBandResponse(BaseModel):
    name: str
    unit: str
    precision: int | None


class LossGridArtifactResponse(BaseModel):
    product_id: str
    checksum: str
    width: int
    height: int
    srid: int
    bbox: tuple[float, float, float, float]
    spatial_allocation_rule: str
    coverage_ratio: float
    spatialized_estimate: bool
    bands: list[LossGridBandResponse]
    tile_template: str
```

`GET /loss` and `GET /loss/products/{product_type}` must attach the persisted
metric rows to each product. A metric whose `numeric_value` is `NULL` returns
`value_status=unavailable` or `not_applicable` and must not be coerced to zero.

`GET /loss/areas` uses `ST_AsGeoJSON` on the locked/admin boundary matched to
each town or county metric and returns coordinate geometry plus the same
metric rows. The town scope is the GeoJSON source consumed by `LossMap`.

`GET /loss/artifact` requires `product_id` and accepts an optional `band`
selector. It selects exactly the requested raster for the run, never the first
available raster. An unknown product, product without a raster, or unknown band
returns 404. It returns raster metadata and a relative `tile_template`
such as `/api/v1/assessments/runs/{run_id}/loss/artifact/{product_id}/{band}/{z}/{x}/{y}.png`.
The PNG route renders the selected PostGIS Raster band into Mapbox raster
tiles with a deterministic color ramp and must not query or return a cell
matrix as ordinary JSON. Reject an unknown band, product, or non-loss product
with 404 and reject disallowed zoom ranges with 422.

Use `_ASSESSMENT_READ_ROLES` for all loss result endpoints.

Include the loss router in `backend/app/main.py`.

In `get_current_assessment`, load loss products from the effective run and attach them to `AssessmentRunStatusResponse.loss`. If no effective loss product exists, return `None` without fabricating an empty complete product.

- [ ] **Step 4: Run API tests and lint**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_api.py tests/test_assessment_api.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss/schemas.py app/loss/router.py app/assessment/router.py app/assessment/schemas.py tests/test_loss_api.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/loss/schemas.py backend/app/loss/router.py backend/app/main.py backend/app/assessment/schemas.py backend/app/assessment/router.py backend/tests/test_loss_api.py
git commit -m "feat: expose loss assessment results"
```

### Task 15: Frontend Loss Summary and Product Views

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Create: `frontend/src/components/LossAssessmentPanel.tsx`
- Create: `frontend/tests/loss-assessment.test.tsx`
- Modify: `frontend/src/pages/EventDetailPage.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Produces:
  - `getLossAssessment(runId: string): Promise<LossResult>`
  - TypeScript `LossResult`, `LossProductSummary`, `LossValue`
  - `LossAssessmentPanel` with `{runId: string, result: LossResult}` rendering product summaries, low/central/high values, quality, calibration, coverage, and review reasons.

- [ ] **Step 1: Write failing React tests**

Create `frontend/tests/loss-assessment.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";

import { LossAssessmentPanel } from "../src/components/LossAssessmentPanel";
import type { LossResult } from "../src/types";


const result: LossResult = {
  run_id: "run-1",
  event_id: "event-1",
  revision_id: "revision-1",
  effective_run_id: "run-1",
  is_fallback: false,
  products: [
    {
      product_id: "product-1",
      product_type: "building_damage",
      status: "complete",
      quality_grade: "L2",
      calibration_status: "reference_uncalibrated",
      coverage_ratio: 0.99,
      partial_scope: false,
      needs_review: false,
      spatialized_estimate: false,
      algorithm_version: "building-structure-matrix-v1",
      parameter_version: "test-only-v1",
      region_profile_version: "shanghai-loss-region-v1",
      output_checksum: "a".repeat(64),
      statistics: {
        collapsed_area_m2: { low: 10, central: 20, high: 30 },
      },
      metrics: [
        {
          area_scope: "city",
          area_code: "310000",
          area_name: "上海市",
          metric_key: "collapsed_area_m2",
          value_type: "central",
          value_status: "available",
          numeric_value: 20,
          unit: "m2",
          precision: 2,
          quality_grade: "L2",
          note: null,
        },
      ],
      reason: null,
    },
  ],
};


it("renders loss values and quality evidence", () => {
  render(<LossAssessmentPanel runId="run-1" result={result} />);
  expect(screen.getByText("建筑破坏")).toBeInTheDocument();
  expect(screen.getByText("L2")).toBeInTheDocument();
  expect(screen.getByText("参考参数未本地校准")).toBeInTheDocument();
  expect(screen.getByText("20.00")).toBeInTheDocument();
});


it("does not render an empty success state when no product exists", () => {
  render(
    <LossAssessmentPanel
      runId="run-1"
      result={{ ...result, products: [] }}
    />,
  );
  expect(screen.getByText("损失评估结果尚未发布")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run frontend tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test -- loss-assessment.test.tsx
```

Expected: FAIL because `LossAssessmentPanel` and `LossResult` do not exist.

- [ ] **Step 3: Implement types, API call, and panel**

Add TypeScript types matching `LossResultResponse`.

Add:

```typescript
export async function getLossAssessment(runId: string): Promise<LossResult> {
  if (!accessToken) {
    throw new ApiError("请先登录后查看损失评估结果", 401);
  }
  return requestJson<LossResult>(
    `/api/v1/assessments/runs/${encodeURIComponent(runId)}/loss`,
    {
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    },
  );
}
```

The null check mirrors `getCurrentAssessment`; do not send an empty bearer
token. Extract the repeated authenticated-header construction into a private
`authenticatedHeaders()` helper and use it from this function and the existing
assessment/collector calls in the same edit.

Render product labels:

```typescript
const PRODUCT_LABELS = {
  building_damage: "建筑破坏",
  population_impact: "受灾人口与安置",
  casualties: "人员伤亡与压埋",
  economic_loss: "直接经济损失",
  resource_demand: "应急资源需求",
  validation: "结果校验",
} as const;
```

`LossAssessmentPanel` must:

- Show `值班结果`, `部分可用`, `待复核`, or `不可计算` based on status and quality.
- Render `product.metrics`; show low, central, and high values with units and
  use `value_status` to distinguish `0`, `unavailable`, and `not_applicable`.
- Show `参考参数未本地校准` for `reference_uncalibrated`.
- Show `空间化估算` for derived grid products.
- Render reason text for unavailable or invalid products.

Load the loss result only after an assessment run ID is available in `EventDetailPage`. A 404 leaves the panel hidden; other errors render a retry state.

- [ ] **Step 4: Run frontend tests, typecheck, and build**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/client.ts frontend/src/types.ts frontend/src/components/LossAssessmentPanel.tsx frontend/src/pages/EventDetailPage.tsx frontend/src/styles.css frontend/tests/loss-assessment.test.tsx
git commit -m "feat: display loss assessment results"
```

### Task 16: Loss Map Linkage for Town and 1 km Results

**Files:**
- Create: `frontend/src/components/LossMap.tsx`
- Create: `frontend/tests/loss-map.test.tsx`
- Create: `frontend/e2e/loss-map.spec.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/src/components/LossAssessmentPanel.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Produces:
  - `LossMap` with fused-intensity, town-loss, and 1 km derived layers.
  - layer visibility controls and town selection linked to product metrics.
  - typed authenticated clients for town GeoJSON and raster tile metadata.
- Consumes `VITE_AMAP_TILE_URL_TEMPLATE`, which is supplied by deployment and never logged or committed.
- Consumes authenticated town GeoJSON from `/loss/areas?scope=town` and raster tile metadata from `/loss/artifact`.

- [ ] **Step 1: Write failing map tests**

Create `frontend/tests/loss-map.test.tsx`:

```tsx
import { render, screen, fireEvent } from "@testing-library/react";

import { LossMap } from "../src/components/LossMap";


it("toggles fused intensity and town loss layers", () => {
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={null}
    />,
  );
  const townToggle = screen.getByLabelText("街镇损失");
  fireEvent.click(townToggle);
  expect(townToggle).not.toBeChecked();
});


it("labels the derived layer as spatialized estimate", () => {
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={{
        product_id: "p1",
        checksum: "a".repeat(64),
        width: 1,
        height: 1,
        srid: 32651,
        bbox: [121.4, 31.1, 121.6, 31.3],
        spatial_allocation_rule: "town-uniform-v1",
        coverage_ratio: 1,
        bands: [
          {
            name: "collapsed_area_m2",
            unit: "m2",
            precision: 2,
          },
        ],
        spatialized_estimate: true,
        tile_template: "/api/v1/assessments/runs/r1/loss/artifact/p1/{band}/{z}/{x}/{y}.png",
      }}
    />,
  );
  expect(screen.getByText("公里格网为空间化估算")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run frontend tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test -- loss-map.test.tsx
```

Expected: FAIL because `LossMap` does not exist.

- [ ] **Step 3: Implement the map with GaoDe-compatible raster tiles**

Add:

```json
"maplibre-gl": "5.6.0"
```

Add these TypeScript types and authenticated request functions before
creating the component:

```typescript
export type LossAreaScope = "city" | "county" | "town";

export interface LossAreaFeature {
  area_scope: LossAreaScope;
  area_code: string;
  area_name: string;
  geometry: Record<string, unknown>;
  metrics: LossValue[];
}

export interface LossAreaResponse {
  run_id: string;
  scope: LossAreaScope;
  features: LossAreaFeature[];
}

export interface LossGridArtifact {
  product_id: string;
  checksum: string;
  width: number;
  height: number;
  srid: number;
  bbox: [number, number, number, number];
  spatial_allocation_rule: string;
  coverage_ratio: number;
  bands: Array<{
    name: string;
    unit: string;
    precision: number | null;
  }>;
  spatialized_estimate: boolean;
  tile_template: string;
}


export async function getLossAreas(
  runId: string,
  scope: LossAreaScope,
): Promise<LossAreaResponse> {
  if (!accessToken) {
    throw new ApiError("请先登录后查看损失空间结果", 401);
  }
  return requestJson<LossAreaResponse>(
    `/api/v1/assessments/runs/${encodeURIComponent(runId)}/loss/areas?scope=${scope}`,
    {
      headers: authenticatedHeaders(),
    },
  );
}


export async function getLossArtifact(
  runId: string,
  productId: string,
): Promise<LossGridArtifact> {
  if (!accessToken) {
    throw new ApiError("请先登录后查看损失格网", 401);
  }
  return requestJson<LossGridArtifact>(
    `/api/v1/assessments/runs/${encodeURIComponent(runId)}/loss/artifact?product_id=${encodeURIComponent(productId)}`,
    {
      headers: authenticatedHeaders(),
    },
  );
}
```

Create a component that:

- Uses a MapLibre `Map` instance with `RasterSource`, `GeoJSONSource`, `addLayer`, and map event handlers.
- Uses `VITE_AMAP_TILE_URL_TEMPLATE` when present.
- Selects `gridArtifact.bands[0].name` as the initial layer band and expands
  `gridArtifact.tile_template` by replacing `{band}`, `{z}`, `{x}`, and `{y}`
  for MapLibre. Passes the expanded URL template to the loss raster source and
  passes a
  MapLibre `transformRequest` callback that adds
  `Authorization: Bearer ${getAccessToken()}` to same-origin loss tile
  requests. If no token exists, do not load the tile source.
- Falls back to a neutral local background and still displays GeoJSON overlays when the tile template is absent.
- Provides layer toggles for `融合烈度`, `街镇损失`, and `公里格网`.
- Marks the 1 km layer with `aria-label="公里格网为空间化估算"`.
- Emits `onTownSelect(townCode)` when a town feature is clicked.
- Does not render or log the tile URL template.
- Destroys the MapLibre map instance on component unmount.

`LossAssessmentPanel` owns `selectedProductId` and `selectedTownCode`.
`EventDetailPage` only passes `runId` and the loss result to the panel. The
panel initializes `selectedProductId` deterministically by sorting completed
products with `spatialized_estimate=true` by `product_type`, calls
`getLossArtifact(runId, selectedProductId)`, and refetches whenever the user
selects a different raster product. `gridArtifact` and `townFeatures` are
passed to `LossMap`; `onTownSelect` updates `selectedTownCode` without
refetching the assessment run. The map may switch among bands from the selected
product, but must not infer or silently switch to a different product.

Mock `maplibre-gl` in Vitest so unit tests verify layer toggles and emitted
town-selection events without creating WebGL contexts.

Create `frontend/e2e/loss-map.spec.ts` with a real MapLibre smoke check:

```typescript
import { expect, test } from "@playwright/test";


const eventId = process.env.E2E_LOSS_EVENT_ID;
const password = process.env.E2E_SUPERADMIN_PASSWORD;


test("renders town and grid loss layers with the real MapLibre bundle", async ({
  page,
}) => {
  test.skip(!eventId || !password, "loss E2E fixture is not configured");

  await page.goto("/");
  await page.getByLabel("用户名").fill(
    process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin",
  );
  await page.getByLabel("密码").fill(password as string);
  await page.getByRole("button", { name: "登录" }).click();
  await page.goto(`/events/${eventId}`);

  const map = page.locator(".maplibregl-map");
  await expect(map).toBeVisible();
  await expect(map.locator("canvas.maplibregl-canvas")).toBeVisible();
  await expect(page.getByLabel("公里格网为空间化估算")).toBeVisible();
  await page.getByLabel("街镇损失").uncheck();
  await expect(map.locator(".maplibregl-canvas")).toBeVisible();
});
```

The real smoke check runs after the fixed Shanghai loss fixture is ready:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run test:e2e -- e2e/loss-map.spec.ts
```

`E2E_LOSS_EVENT_ID` and `E2E_SUPERADMIN_PASSWORD` are runtime environment
values and must not be written into source or logs.

- [ ] **Step 4: Run frontend tests, typecheck, and build**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/api/client.ts frontend/src/types.ts frontend/src/components/LossMap.tsx frontend/src/components/LossAssessmentPanel.tsx frontend/src/styles.css frontend/tests/loss-map.test.tsx frontend/e2e/loss-map.spec.ts
git commit -m "feat: link loss products to map layers"
```

### Task 17: Fixed Scenario, Failure Injection, and Z440 Performance Evidence

**Files:**
- Create: `backend/tests/loss_helpers.py`
- Create: `backend/tests/test_loss_end_to_end.py`
- Create: `backend/tests/test_loss_failure_modes.py`
- Create: `backend/tests/test_loss_performance.py`
- Modify: `backend/pyproject.toml`

**Interfaces:**
- Consumes: all previous tasks.
- Produces:
  - a real PostgreSQL/PostGIS fixed Shanghai scenario with test-only parameters.
  - failure-injection and idempotency coverage.
  - a Z440 performance benchmark with a five-minute hard assertion.

- [ ] **Step 1: Write failing end-to-end, failure, and performance tests**

Create `backend/tests/test_loss_end_to_end.py` with a fixture that:

1. Inserts a formal event and assessment run.
2. Creates a fused intensity raster with a deterministic synthetic field around `121.5, 31.2`.
3. Creates test-only data-asset versions and snapshots.
4. Executes the real `AssessmentWorkflow`, including all six loss activities.
5. Queries the loss products, metrics, and raster.

Assert:

```python
async with session_factory() as session:
    validation_inputs = await LossRepository().load_validation_inputs(
        session,
        run_id,
    )

assert products["building_damage"].status == "complete"
assert products["population_impact"].status == "complete"
assert products["casualties"].status == "complete"
assert products["economic_loss"].status == "complete"
assert products["resource_demand"].status == "complete"
assert products["validation"].status == "complete"
assert products["building_damage"].quality_grade in {"L2", "L3"}
assert products["building_damage"].calibration_status == "uncalibrated"
assert raster.spatial_allocation_rule == "town-uniform-v1"
assert all(
    abs(residual.residual) <= 1e-6
    for residual in validation_inputs.grid_residuals
)
assert {
    metric.value_status
    for metric in products["resource_demand"].metrics
    if metric.metric_key == "tent.quantity"
} == {"available"}
```

This fixed fixture intentionally uses
`backend/tests/fixtures/loss-test-parameters.yaml`; its expected calibration
status is therefore `uncalibrated`, not `reference_uncalibrated`. The
production parameter pack remains `reference_uncalibrated` and cannot produce
this test until reviewed coefficients are supplied.

Create `backend/tests/test_loss_failure_modes.py`:

```python
from app.loss.domain import ResourceKind
from tests.loss_helpers import (
    run_buildings_twice,
    run_correction,
    run_formal,
    run_partial_asset_publish_recovery,
    run_resources_with_missing,
    run_without_vulnerability_row,
)


async def test_missing_resource_parameter_does_not_block_other_resources(
    session_factory,
) -> None:
    result = await run_resources_with_missing(session_factory, "tent.base")
    assert result.values[ResourceKind.TENT].status == "unavailable"
    assert result.values[ResourceKind.FOOD].status == "available"
    assert result.persisted_values["tent.quantity"].numeric_value is None
    assert result.persisted_values["tent.quantity"].value_status == "unavailable"
    assert result.persisted_values["food.quantity"].numeric_value is not None


async def test_missing_vulnerability_row_fails_run(session_factory) -> None:
    result = await run_without_vulnerability_row(session_factory)
    assert result.status == "failed"


async def test_duplicate_activity_delivery_publishes_once(session_factory) -> None:
    first, second = await run_buildings_twice(session_factory)
    assert first.product_id == second.product_id


async def test_correction_creates_independent_loss_version(
    session_factory,
) -> None:
    first = await run_formal(session_factory)
    second = await run_correction(session_factory)
    assert first.product_id != second.product_id
    assert first.run_id != second.run_id


async def test_partial_asset_publish_restores_previous_published_versions(
    session_factory,
) -> None:
    await run_partial_asset_publish_recovery(session_factory)
```

Create `backend/tests/test_loss_performance.py`:

```python
import pytest

from tests.loss_helpers import execute_fixed_loss_chain


@pytest.mark.performance
async def test_fixed_shanghai_loss_chain_meets_five_minute_budget(
    session_factory,
) -> None:
    result = await execute_fixed_loss_chain(session_factory)
    assert result.status == "completed"
    assert result.query_ready_at > result.report_ingested_at
    assert result.elapsed_seconds <= 300.0
    assert result.stage_seconds["snapshot_and_exposure"] <= 20.0
    assert result.stage_seconds["buildings_and_population"] <= 60.0
    assert result.stage_seconds["casualties_economic_resources"] <= 60.0
    assert result.stage_seconds["validate_and_persist"] <= 20.0
```

`execute_fixed_loss_chain` must start with a formal report inserted through
the normal ingestion path, capture the persisted
`AssessmentRun.report_ingested_at`, execute the workflow through query-ready
publication, then issue the authenticated `GET /loss` request. `query_ready_at`
is captured only after that request returns HTTP 200; it is not derived from
an in-process service return. The 180-second value is the normal operating target
and the 300-second assertion is the hard acceptance limit, both measured from
report ingestion.

- [ ] **Step 2: Run the new tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_end_to_end.py tests/test_loss_failure_modes.py tests/test_loss_performance.py -v
```

Expected: FAIL until the helpers, integrated service wiring, and resource artifact path are complete.

- [ ] **Step 3: Implement fixtures and fix only verified integration gaps**

Create `backend/tests/loss_helpers.py` with explicit cleanup and seed helpers.
Prefix fixture records with `LOSS-TEST-`. The helper returns the
`LossChainPerformanceResult` from Task 12 and records the four named stage
timers; it must not replace the hard query-ready measurement with a mocked
database call.

Expose these exact helpers:

```python
@dataclass(frozen=True, slots=True)
class SeededLossProduct:
    run_id: UUID
    product_id: UUID | None
    status: str


@dataclass(frozen=True, slots=True)
class SeededLossRun:
    run_id: UUID
    workflow_request: AssessmentWorkflowInput


@dataclass(frozen=True, slots=True)
class PersistedResourceCheck:
    values: Mapping[ResourceKind, ResourceDemandValue]
    persisted_values: Mapping[str, LossMetricValue]


@dataclass(frozen=True, slots=True)
class BaselineAssetState:
    version_id: UUID
    created: bool
    previous_version_id: UUID | None


async def execute_fixed_loss_chain(
    session_factory,
) -> LossChainPerformanceResult:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        async with session_factory() as session:
            run = await session.get(AssessmentRun, seeded.run_id)
            if run is None:
                raise LookupError("loss fixture run not found")
            report_ingested_at = run.report_ingested_at

        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "completed":
            raise AssertionError(
                f"assessment workflow failed: {workflow_result.status}"
            )

        response = await _request_loss_api(seeded.run_id)
        query_ready_at = datetime.now(UTC)
        if response.status_code != 200:
            raise AssertionError(
                f"loss API did not become query-ready: {response.status_code}"
            )
        products = response.json()["products"]
        if len(products) != 6:
            raise AssertionError("loss API did not return all six products")
        stage_seconds = await _load_stage_seconds(
            session_factory,
            seeded.run_id,
        )
        elapsed = (query_ready_at - report_ingested_at).total_seconds()
        return LossChainPerformanceResult(
            status="completed",
            report_ingested_at=report_ingested_at,
            query_ready_at=query_ready_at,
            elapsed_seconds=elapsed,
            stage_seconds=stage_seconds,
        )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_resources_with_missing(
    session_factory,
    missing_parameter: str,
) -> PersistedResourceCheck:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )

        async def load_parameters(session, *, run_id, profile):
            parameter_set = await load_locked_parameter_set(
                session,
                run_id=run_id,
                profile=profile,
            )
            model = parameter_set.models[LossModelType.RESOURCE_DEMAND]
            scenarios = {
                scenario: ScenarioParameters(
                    values={
                        key: value
                        for key, value in parameters.values.items()
                        if key != missing_parameter
                    }
                )
                for scenario, parameters in model.scenarios.items()
            }
            return replace(
                parameter_set,
                models={
                    **parameter_set.models,
                    LossModelType.RESOURCE_DEMAND: replace(
                        model,
                        scenarios=scenarios,
                    ),
                },
            )

        service = LossAssessmentService(
            session_factory,
            parameter_loader=load_parameters,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
            loss_service_factory=lambda: service,
        )
        if workflow_result.status != "completed":
            raise AssertionError(
                f"assessment workflow failed: {workflow_result.status}"
            )

        async with session_factory() as session:
            product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.RESOURCE_DEMAND,
            )
        if product is None:
            raise LookupError("resource product not found")
        values = {
            ResourceKind(metric.metric_key.split(".", 1)[0]): ResourceDemandValue(
                quantity=(
                    int(metric.numeric_value)
                    if metric.numeric_value is not None
                    else None
                ),
                status=str(
                    getattr(metric.value_status, "value", metric.value_status)
                ),
                reason=metric.note,
            )
            for metric in product.metrics
        }
        persisted = {
            metric.metric_key: metric
            for metric in product.metrics
        }
        return PersistedResourceCheck(
            values=values,
            persisted_values=persisted,
        )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_without_vulnerability_row(
    session_factory,
) -> SeededLossProduct:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(
            session_factory,
            include_buildings=False,
        )
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "failed":
            raise AssertionError("workflow should fail without vulnerability data")
        async with session_factory() as session:
            run = await session.get(AssessmentRun, seeded.run_id)
            if run is None:
                raise LookupError("loss fixture run not found")
            failed_task = await session.scalar(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == seeded.run_id,
                    AssessmentTask.task_key == "loss.buildings",
                )
            )
            if (
                failed_task is None
                or failed_task.status != "failed"
                or not failed_task.last_error
            ):
                raise AssertionError("failed building task was not audited")
            return SeededLossProduct(
                run_id=seeded.run_id,
                product_id=None,
                status=run.status,
            )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_buildings_twice(
    session_factory,
) -> tuple[SeededLossProduct, SeededLossProduct]:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        first_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
            workflow_id_suffix="first",
        )
        second_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
            workflow_id_suffix="second",
        )
        if first_result.status != "completed" or second_result.status != "completed":
            raise AssertionError("duplicate workflow did not complete")
        async with session_factory() as session:
            first_product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.BUILDING_DAMAGE,
            )
        if first_product is None:
            raise LookupError("building product not found")
        return (
            SeededLossProduct(
                seeded.run_id,
                first_product.id,
                first_product.status,
            ),
            SeededLossProduct(
                seeded.run_id,
                first_product.id,
                first_product.status,
            ),
        )
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_formal(session_factory) -> SeededLossProduct:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "completed":
            raise AssertionError("formal workflow did not complete")
        async with session_factory() as session:
            product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.BUILDING_DAMAGE,
            )
        if product is None:
            raise LookupError("building product not found")
        return SeededLossProduct(seeded.run_id, product.id, product.status)
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_correction(session_factory) -> SeededLossProduct:
    boundary_version = await _seed_loss_boundary(session_factory)
    asset_version_ids: tuple[UUID, ...] = ()
    try:
        published = await _publish_fixed_assets(session_factory)
        asset_version_ids = tuple(published.values())
        seeded = await _seed_loss_run(
            session_factory,
            boundary_version=boundary_version,
            event_kind=EventKind.CORRECTION,
        )
        workflow_result = await _execute_loss_workflow(
            session_factory,
            seeded.workflow_request,
        )
        if workflow_result.status != "completed":
            raise AssertionError("correction workflow did not complete")
        async with session_factory() as session:
            product = await LossRepository().get_product(
                session,
                seeded.run_id,
                LossProductType.BUILDING_DAMAGE,
            )
        if product is None:
            raise LookupError("building product not found")
        return SeededLossProduct(seeded.run_id, product.id, product.status)
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            boundary_version,
            asset_version_ids=asset_version_ids,
        )


async def run_partial_asset_publish_recovery(session_factory) -> None:
    asset_key = "shanghai.admin.city"
    baseline_state = await _publish_baseline_asset(
        session_factory,
        asset_key=asset_key,
    )
    try:
        try:
            await _publish_fixed_assets(
                session_factory,
                fail_after=1,
            )
        except InjectedPublicationFailure:
            pass
        else:
            raise AssertionError("partial publication failure was not raised")
    finally:
        await _cleanup_loss_fixture(
            session_factory,
            f"loss-cleanup-{uuid4()}",
        )

    try:
        async with session_factory() as session:
            baseline_row = await session.get(
                DataAssetVersion,
                baseline_state.version_id,
            )
            remaining = await session.scalar(
                select(DataAssetVersion.id).where(
                    DataAssetVersion.version.like(
                        f"{ASSET_VERSION_PREFIX}%"
                    )
                )
            )
        if baseline_row is None or baseline_row.status != "published":
            raise AssertionError("previous published version was not restored")
        if remaining is not None:
            raise AssertionError("fixture asset version was not removed")
    finally:
        await _delete_baseline_asset(
            session_factory,
            asset_key=asset_key,
            baseline=baseline_state,
        )
```

The imports and private fixtures used above must be present in
`backend/tests/loss_helpers.py`:

```python
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from geoalchemy2.elements import WKTElement
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.assessment.temporal import (
    AssessmentActivities,
    AssessmentWorkflow,
    AssessmentWorkflowInput,
)
from app.assessment.models import AssessmentRun, AssessmentTask
from app.assessment.repository import AssessmentRepository
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.data_assets.domain import NormalizedRecord, NormalizedTableData
from app.data_assets.import_jobs import QueueImportRequest, queue_import_job
from app.data_assets.models import (
    DataAssetAuditLog,
    DataAssetImportJob,
    DataAssetRecord,
    DataAssetRaster,
    DataAssetSnapshot,
    DataAssetVersion,
)
from app.data_assets.service import DataAssetService
from app.db import engine
from app.events.domain import EventKind, NormalizedEvent
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.response_rules import ResponseInput
from app.events.service import EventService
from app.intensity.domain import GridDefinition
from app.intensity.models import IntensityFieldProduct, IntensityRaster
from app.intensity.parameters import load_parameter_bundle
from app.intensity.service import IntensityService
from app.loss.asset_bridge import load_locked_parameter_set
from app.loss.domain import (
    LossModelType,
    LossProductType,
    ResourceDemandValue,
    ResourceKind,
    ScenarioParameters,
)
from app.loss.models import LossMetricValue, LossProduct, LossProductRaster
from app.loss.repository import LossRepository
from app.loss.service import LossAssessmentService, LossChainPerformanceResult
from app.main import app
from app.regions.domain import RegionContext
from app.regions.models import RegionBoundary
```

`_seed_loss_boundary`, `_publish_fixed_assets`, `_seed_loss_run`,
`_request_loss_api`, and `_cleanup_loss_fixture` are implemented immediately
below the public helpers. They use only production service seams:

```python
LOSS_TEST_PREFIX = "LOSS-TEST-"
ASSET_VERSION = "loss-e2e-v1"
ASSET_VERSION_PREFIX = f"{ASSET_VERSION}-"


class InjectedPublicationFailure(RuntimeError):
    pass


def _fixed_intensity_service(session_factory) -> IntensityService:
    return IntensityService(
        session_factory=session_factory,
        parameters=load_parameter_bundle(
            "/config/intensity/shanghai-2019.yaml"
        ),
        fixed_grid_definition=GridDefinition(
            "loss-e2e-grid",
            "EPSG:32651",
            1000,
            0,
            2000,
            2,
            2,
        ),
    )


async def _execute_loss_workflow(
    session_factory,
    request: AssessmentWorkflowInput,
    *,
    loss_service_factory=None,
    workflow_id_suffix: str = "main",
):
    activities = AssessmentActivities(
        session_factory,
        intensity_service_factory=lambda: _fixed_intensity_service(
            session_factory
        ),
        loss_service_factory=loss_service_factory,
    )
    task_queue = f"loss-e2e-{uuid4()}"
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[AssessmentWorkflow],
            activities=[
                activities.prepare_assessment,
                activities.run_intensity_model,
                activities.run_intensity_instrument,
                activities.run_intensity_fusion,
                activities.run_loss_buildings,
                activities.run_loss_population,
                activities.run_loss_casualties,
                activities.run_loss_economic,
                activities.run_loss_resources,
                activities.run_loss_validate,
                activities.mark_deadline_exceeded,
                activities.observe_task_deadlines,
                activities.finalize_assessment,
            ],
        ):
            return await environment.client.execute_workflow(
                AssessmentWorkflow.run,
                request,
                id=(
                    f"loss-e2e:{request.event_id}:"
                    f"{request.revision_id}:{workflow_id_suffix}"
                ),
                task_queue=task_queue,
            )


async def _load_stage_seconds(
    session_factory,
    run_id: UUID,
) -> dict[str, float]:
    async with session_factory() as session:
        tasks = (
            await session.scalars(
                select(AssessmentTask).where(
                    AssessmentTask.run_id == run_id
                )
            )
        ).all()
    by_key = {task.task_key: task for task in tasks}

    def loss_stage(task_key: str, stage_name: str) -> float:
        task = by_key[task_key]
        if task.result is None:
            raise LookupError(f"{task_key} has no result")
        return float(task.result["stage_seconds"][stage_name])

    def task_duration(task_key: str) -> float:
        task = by_key[task_key]
        if task.started_at is None or task.completed_at is None:
            raise LookupError(f"{task_key} has no execution window")
        return (task.completed_at - task.started_at).total_seconds()

    intensity_seconds = sum(
        task_duration(key)
        for key in (
            "intensity.model",
            "intensity.instrument",
            "intensity.fusion",
        )
    )
    return {
        "snapshot_and_exposure": (
            intensity_seconds
            + loss_stage("loss.buildings", "snapshot_and_exposure")
        ),
        "buildings_and_population": (
            loss_stage("loss.buildings", "buildings_and_population")
            + loss_stage("loss.population", "buildings_and_population")
        ),
        "casualties_economic_resources": (
            loss_stage("loss.casualties", "casualties_economic_resources")
            + loss_stage("loss.economic", "casualties_economic_resources")
            + loss_stage("loss.resources", "casualties_economic_resources")
        ),
        "validate_and_persist": loss_stage(
            "loss.validate",
            "validate_and_persist",
        ),
    }


async def _seed_loss_boundary(session_factory) -> str:
    version = f"loss-test-boundary-{uuid4()}"
    geometry = WKTElement(
        "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
        "121.60 31.30, 121.40 31.30, 121.40 31.15)))",
        srid=4326,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                RegionBoundary(
                    version=version,
                    name="loss test boundary",
                    local_buffer_km=50,
                    geom=geometry,
                    maritime_geom=geometry,
                    source_uri="https://example.gov.invalid/loss-e2e/boundary",
                    checksum=hashlib.sha256(version.encode()).hexdigest(),
                    is_active=False,
                )
            )
    return version


async def _publish_fixed_assets(
    session_factory,
    *,
    include_buildings: bool = True,
    fail_after: int | None = None,
) -> dict[str, UUID]:
    town_codes = [f"310115{i:06d}" for i in range(1, 213)]
    town_records = [
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "ID": town_code,
                "NAME": f"测试街镇{index}",
            },
            geometry_wkt=(
                "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
                "121.60 31.30, 121.40 31.30, 121.40 31.15)))"
            ),
        )
        for index, town_code in enumerate(town_codes, start=1)
    ]
    population_records = [
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "ID": town_code,
                "NAME": f"测试街镇{index}",
                "total": 10000.0,
                "resident": 8000.0,
                "floating": 2000.0,
                "family": 3200.0,
                "under14": 1200.0,
                "over65": 1400.0,
            },
        )
        for index, town_code in enumerate(town_codes, start=1)
    ]
    building_records = [
        NormalizedRecord(
            row_number=index,
            business_key=town_code,
            properties={
                "id": town_code,
                "name": f"测试街镇{index}",
                "TOTAL_AREA": 500000.0,
                "HIGH_RISE": 100000.0,
                "RCFRAME": 300000.0,
                "BRICK_STRUCTURE": 50000.0,
                "SINGLE_AREA": 30000.0,
                "OTHER_STRUCTURE": 20000.0,
            },
        )
        for index, town_code in enumerate(town_codes, start=1)
    ]
    assets = {
        "shanghai.admin.town": _table(
            ("ID", "NAME", "geometry_wkt"),
            town_records,
        ),
        "shanghai.population.town": _table(
            (
                "ID",
                "NAME",
                "total",
                "resident",
                "floating",
                "family",
                "under14",
                "over65",
            ),
            population_records,
        ),
    }
    if include_buildings:
        assets["shanghai.building.town"] = _table(
            (
                "id",
                "name",
                "TOTAL_AREA",
                "HIGH_RISE",
                "RCFRAME",
                "BRICK_STRUCTURE",
                "SINGLE_AREA",
                "OTHER_STRUCTURE",
            ),
            building_records,
        )
    for asset_key in ("shanghai.admin.city", "shanghai.admin.county"):
        assets[asset_key] = _table(
            ("ID", "NAME", "geometry_wkt"),
            (
                NormalizedRecord(
                    row_number=1,
                    business_key="310000",
                    properties={
                        "ID": "310000",
                        "NAME": "上海市",
                    },
                    geometry_wkt=(
                        "MULTIPOLYGON (((121.40 31.15, 121.60 31.15, "
                        "121.60 31.30, 121.40 31.30, 121.40 31.15)))"
                    ),
                ),
            ),
        )
    assets["shanghai.economy.county"] = _table(
        (
            "id",
            "name",
            "gdp",
            "industry_value",
            "agri_value",
            "service_value",
            "income",
        ),
        (
            NormalizedRecord(
                row_number=1,
                business_key="310115",
                properties={
                    "id": "310115",
                    "name": "浦东新区",
                    "gdp": 1.0,
                    "industry_value": 1.0,
                    "agri_value": 1.0,
                    "service_value": 1.0,
                    "income": 1.0,
                },
            ),
        ),
    )
    loss_document = yaml.safe_load(
        Path("/app/tests/fixtures/loss-test-parameters.yaml").read_text(
            encoding="utf-8"
        )
    )
    loss_document["parameter_set_id"] = loss_document["version"]
    assets["shanghai.loss.parameters"] = NormalizedTableData(
        columns=tuple(sorted(loss_document)),
        records=(
            NormalizedRecord(
                row_number=1,
                business_key=str(loss_document["parameter_set_id"]),
                properties=loss_document,
            ),
        ),
        source_crs="EPSG:4326",
        spatial_extent=None,
    )
    published: dict[str, UUID] = {}
    for index, (asset_key, normalized) in enumerate(
        sorted(assets.items()),
        start=1,
    ):
        published[asset_key] = await _publish_table_asset(
            session_factory,
            asset_key=asset_key,
            normalized=normalized,
        )
        if fail_after is not None and index == fail_after:
            raise InjectedPublicationFailure(
                "injected loss fixture publication failure"
            )
    return published


def _table(columns, records) -> NormalizedTableData:
    return NormalizedTableData(
        columns=tuple(columns),
        records=tuple(records),
        source_crs="EPSG:4326",
        spatial_extent=(121.40, 31.15, 121.60, 31.30),
    )


async def _publish_table_asset(session_factory, *, asset_key, normalized):
    service = DataAssetService()
    checksum = hashlib.sha256(
        repr((asset_key, ASSET_VERSION)).encode("utf-8")
    ).hexdigest()
    async with session_factory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key=asset_key,
                    version=f"{ASSET_VERSION_PREFIX}{uuid4()}",
                    source_uri=(
                        "https://example.gov.invalid/loss-e2e/"
                        f"{asset_key.replace('.', '-')}"
                    ),
                    license_name="test-only",
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="loss end-to-end fixture",
                    file_name=f"{asset_key}.json",
                    file_format="parameter_file",
                    file_size_bytes=1,
                    checksum=checksum,
                    relative_path=f"loss-e2e/{checksum}.json",
                    requested_by="loss-test",
                ),
            )
            await service.populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {"source": "loss-test"},
            )
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor="loss-test",
            )
            if not report.publishable:
                raise AssertionError(
                    f"loss fixture asset failed validation: {asset_key}"
                )
            version = await service.publish_version(
                session,
                job.asset_version_id,
                "loss-test",
                "loss end-to-end fixture",
            )
            return version.id


async def _seed_loss_run(
    session_factory,
    *,
    boundary_version: str,
    event_kind: EventKind = EventKind.FORMAL,
) -> SeededLossRun:
    source_event_id = f"{LOSS_TEST_PREFIX}{uuid4()}"
    event = NormalizedEvent(
        kind=event_kind,
        source="cenc",
        source_event_id=source_event_id,
        origin_time=datetime(2026, 9, 28, 1, 0, tzinfo=UTC),
        longitude=Decimal("121.500000"),
        latitude=Decimal("31.200000"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.2"),
        place="loss test",
        report_time=datetime(2026, 9, 28, 1, 2, tzinfo=UTC),
    )
    received = datetime(2026, 9, 28, 1, 3, tzinfo=UTC)
    outcome = await EventService(session_factory).ingest_collected(
        raw_payload={"EventID": source_event_id, "type": "reviewed"},
        event=event,
        provider="fan",
        lane="websocket",
        received_at=received,
        response_input=ResponseInput(
            event.magnitude,
            event.depth_km,
            True,
            0,
            None,
            None,
        ),
        region_context=RegionContext(
            True,
            0,
            boundary_version,
            received,
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            outbox = await session.scalar(
                select(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.revision_id == outcome.revision_id
                )
            )
            if outbox is None:
                raise LookupError("loss fixture outbox not found")
            run = await AssessmentRepository().ensure_run_from_outbox(
                session,
                event_id=outcome.event_id,
                revision_id=outcome.revision_id,
                outbox_id=str(outbox.id),
            )
            return SeededLossRun(
                run_id=run.id,
                workflow_request=AssessmentWorkflowInput(
                    event_id=outcome.event_id,
                    revision_id=outcome.revision_id,
                    outbox_id=str(outbox.id),
                ),
            )


async def _request_loss_api(run_id: UUID):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="loss-test",
        role="superadmin",
        workgroup="震害评估组",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(
                f"/api/v1/assessments/runs/{run_id}/loss"
            )
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


async def _cleanup_loss_fixture(
    session_factory,
    boundary_version: str,
    *,
    asset_version_ids: tuple[UUID, ...] = (),
) -> None:
    await engine.dispose()
    async with session_factory() as session:
        async with session.begin():
            event_ids = select(EarthquakeEvent.id).where(
                EarthquakeEvent.source_event_id.like(f"{LOSS_TEST_PREFIX}%")
            )
            raw_message_ids = tuple(
                (
                    await session.scalars(
                        select(EarthquakeRevision.raw_message_id).where(
                            EarthquakeRevision.event_id.in_(event_ids)
                        )
                    )
                ).all()
            )
            run_ids = select(AssessmentRun.id).where(
                AssessmentRun.event_id.in_(event_ids)
            )
            product_ids = select(LossProduct.id).where(
                LossProduct.run_id.in_(run_ids)
            )
            await session.execute(
                delete(LossProductRaster).where(
                    LossProductRaster.product_id.in_(product_ids)
                )
            )
            await session.execute(
                delete(LossMetricValue).where(
                    LossMetricValue.product_id.in_(product_ids)
                )
            )
            await session.execute(
                delete(LossProduct).where(
                    LossProduct.run_id.in_(run_ids)
                )
            )
            await session.execute(
                delete(IntensityRaster).where(
                    IntensityRaster.product_id.in_(
                        select(IntensityFieldProduct.id).where(
                            IntensityFieldProduct.run_id.in_(run_ids)
                        )
                    )
                )
            )
            await session.execute(
                delete(IntensityFieldProduct).where(
                    IntensityFieldProduct.run_id.in_(run_ids)
                )
            )
            await session.execute(
                delete(AssessmentTask).where(
                    AssessmentTask.run_id.in_(run_ids)
                )
            )
            discovered_version_ids = await _restore_fixture_asset_publications(session)
            fixture_asset_version_ids = tuple(
                dict.fromkeys((*asset_version_ids, *discovered_version_ids))
            )
            if fixture_asset_version_ids:
                await session.execute(
                    delete(DataAssetSnapshot).where(
                        DataAssetSnapshot.asset_version_id.in_(
                            fixture_asset_version_ids
                        )
                    )
                )
            await session.execute(
                delete(AssessmentRun).where(
                    AssessmentRun.id.in_(run_ids)
                )
            )
            await session.execute(
                delete(EventLifecycleOutbox).where(
                    EventLifecycleOutbox.event_id.in_(event_ids)
                )
            )
            await session.execute(
                delete(EarthquakeRevision).where(
                    EarthquakeRevision.event_id.in_(event_ids)
                )
            )
            await session.execute(
                delete(EarthquakeEvent).where(
                    EarthquakeEvent.id.in_(event_ids)
                )
            )
            await session.execute(
                delete(RawMessage).where(RawMessage.id.in_(raw_message_ids))
            )
            await session.execute(
                delete(RegionBoundary).where(
                    RegionBoundary.version == boundary_version
                )
            )
            if fixture_asset_version_ids:
                await session.execute(
                    delete(DataAssetRecord).where(
                        DataAssetRecord.version_id.in_(fixture_asset_version_ids)
                    )
                )
                await session.execute(
                    delete(DataAssetRaster).where(
                        DataAssetRaster.version_id.in_(fixture_asset_version_ids)
                    )
                )
                await session.execute(
                    delete(DataAssetImportJob).where(
                        DataAssetImportJob.asset_version_id.in_(
                            fixture_asset_version_ids
                        )
                    )
                )
                await session.execute(
                    delete(DataAssetAuditLog).where(
                        DataAssetAuditLog.version_id.in_(
                            fixture_asset_version_ids
                        )
                    )
                )
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.id.in_(fixture_asset_version_ids)
                    )
                )
    await engine.dispose()
```

`_publish_baseline_asset` first checks for an existing published
`shanghai.admin.city` version and reuses it with `created=false`. Only when no
version is published does it create one whose version starts with
`LOSS-BASELINE-`, record any retired predecessor, and return
`BaselineAssetState(created=true)`. `_delete_baseline_asset` restores the
recorded predecessor when needed, then removes only the created baseline
version, its records/rasters, import job, and audit rows. Reused production
versions are not deleted. These helpers must use the production
`DataAssetService` lifecycle for publication and direct deletion only for final
test cleanup.

Implement `_restore_fixture_asset_publications` as the first cleanup step for
asset versions:

```python
async def _restore_fixture_asset_publications(
    session,
) -> tuple[UUID, ...]:
    version_ids = tuple(
        (
            await session.scalars(
                select(DataAssetVersion.id).where(
                    DataAssetVersion.version.like(
                        f"{ASSET_VERSION_PREFIX}%"
                    )
                )
            )
        ).all()
    )
    if not version_ids:
        return ()

    audits = (
        await session.scalars(
            select(DataAssetAuditLog).where(
                DataAssetAuditLog.version_id.in_(version_ids),
                DataAssetAuditLog.action == "publish",
            )
        )
    ).all()
    fixture_version_id_set = set(version_ids)
    previous_version_ids = tuple(
        dict.fromkeys(
            previous_id
            for audit in audits
            if audit.details and audit.details.get("old_version_id")
            if (previous_id := UUID(str(audit.details["old_version_id"])))
            not in fixture_version_id_set
        )
    )

    await session.execute(
        update(DataAssetVersion)
        .where(DataAssetVersion.id.in_(version_ids))
        .values(status="retired", retired_at=datetime.now(UTC))
    )
    if previous_version_ids:
        await session.execute(
            update(DataAssetVersion)
            .where(DataAssetVersion.id.in_(previous_version_ids))
            .values(status="published", retired_at=None)
        )
    await session.execute(
        delete(DataAssetAuditLog).where(
            DataAssetAuditLog.actor == "loss-test"
        )
    )
    return version_ids
```

`execute_fixed_loss_chain` inserts the report with
`report_ingested_at`, executes the real workflow through its dependency graph,
requests `GET /loss` with a superadmin token, and returns only after the
response contains all six products. Every public helper executes the real
`AssessmentWorkflow` through a `WorkflowEnvironment` and `Worker`; no helper
advances run or task state by calling repository completion methods directly.
The failure helpers insert isolated `LOSS-TEST-*` events in their `finally`
blocks.

`_cleanup_loss_fixture` must not rely only on the version IDs returned by a
successful `_publish_fixed_assets` call. Before deleting asset data, it must:

1. Discover every `DataAssetVersion` whose version starts with
   `ASSET_VERSION_PREFIX`; this covers a partial failure before the publication
   helper returns.
2. Read the fixture versions' `publish` audit rows and collect
   `details["old_version_id"]`.
3. Mark fixture versions `retired`, then restore each recorded previous version
   to `published` with `retired_at=None`. This reverses the production-version
   retirement performed by the fixture and preserves the one-published-version
   constraint.
4. Delete fixture-created audit rows where `actor="loss-test"`, snapshots,
   records, rasters, import jobs, and finally fixture versions.

Cleanup may use the exact IDs passed from the caller as a fast path, but the
prefix-based discovery and previous-version restoration are mandatory. A test
must leave the pre-test published version set unchanged, including after
`_publish_fixed_assets` raises partway through its loop.

The helper must use the existing `DataAssetService` and `DataAssetSnapshotService` interfaces. It must not insert directly into `data_asset_versions` or `data_asset_snapshots` unless the data-asset center plan documents those tables as its supported test seam.

For performance failures, profile only the slow path. Do not weaken the four
stage budgets, the 180-second normal target, or the 300-second hard assertion.

- [ ] **Step 4: Run focused suites and the benchmark**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_loss_end_to_end.py tests/test_loss_failure_modes.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_loss_performance.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app/loss tests/loss_helpers.py tests/test_loss_end_to_end.py tests/test_loss_failure_modes.py tests/test_loss_performance.py
```

Expected: PASS. Record the observed elapsed time and stage times for the runbook.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/tests/loss_helpers.py backend/tests/test_loss_end_to_end.py backend/tests/test_loss_failure_modes.py backend/tests/test_loss_performance.py
git commit -m "test: verify loss chain failure and performance behavior"
```

### Task 18: Runbook, Documentation, and Final Verification

**Files:**
- Create: `docs/runbooks/loss-assessment.md`
- Modify: `README.md`
- Modify: `docs/runbooks/assessment-orchestration.md`

**Interfaces:**
- Consumes: all prior tasks.
- Produces operating, calibration, recovery, backup, and verification instructions.

- [ ] **Step 1: Write the runbook and update project documentation**

Create `docs/runbooks/loss-assessment.md` with these sections:

```markdown
# Loss Assessment Runbook

## Scope

## Required Data Asset Versions

## Required Parameter Sources

## Starting Services

## Running a Fixed Shanghai Scenario

## Reading Building, Population, Casualty, Economic, and Resource Products

## Interpreting Quality, Calibration, and Coverage

## Interpreting Spatialized 1 km Results

## Handling Missing Parameters

## Handling Missing Town Data

## Correcting a Formal Report

## Recovering a Failed Workflow

## Inspecting PostgreSQL and PostGIS Raster Results

## Backup and Restore

## Verification Commands
```

Document these exact operational rules:

- Loss work starts only after `intensity.fusion` succeeds.
- `loss.resources` succeeds when every resource kind has a final `available` or `unavailable` status.
- A missing resource coefficient returns `unavailable`, not zero.
- Missing core town exposure or vulnerability data fails the required run.
- The town result is authoritative; the 1 km result is derived with `town-uniform-v1`.
- The production parameter set remains `reference_uncalibrated` until each coefficient has a reviewed source and Shanghai calibration evidence.
- A correction creates a new run and does not overwrite prior products.
- The five-minute deadline is an alert threshold; the technical safety timeout remains 1800 seconds.
- PostgreSQL and PostGIS Raster must be included in backup and restore verification.

Update `README.md` with the implemented loss-assessment behavior and the exact test commands.

Update `docs/runbooks/assessment-orchestration.md` with loss task names, completion semantics, and SQL examples that query `loss_products`, `loss_metric_values`, and `loss_product_rasters`.

- [ ] **Step 2: Run the complete verification suite**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_loss_performance.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Expected:

- Compose configuration passes.
- Alembic head is `0014_loss_assessment`.
- All backend and frontend tests pass.
- The fixed Shanghai loss benchmark meets every stage budget and the 300-second hard limit.
- Ruff passes.
- Frontend typecheck and production build pass.

- [ ] **Step 3: Verify real service integration**

Start the stack:

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal temporal-ui
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker
```

Submit the fixed formal event through the existing event ingestion path, then verify:

```sql
SELECT run_no, status, report_ingested_at, deadline_at,
       deadline_exceeded_at, algorithm_bundle_version
FROM assessment_runs
ORDER BY created_at DESC
LIMIT 5;

SELECT task_key, status, attempt_count, input_fingerprint,
       output_checksum, last_error
FROM assessment_tasks
WHERE run_id = '<run-id>'
ORDER BY sequence;

SELECT product_type, status, quality_grade, calibration_status,
       coverage_ratio, partial_scope, needs_review,
       spatialized_estimate, output_checksum
FROM loss_products
WHERE run_id = '<run-id>'
ORDER BY product_type;

SELECT area_scope, area_code, metric_key, value_type,
       numeric_value, unit, quality_grade
FROM loss_metric_values
WHERE product_id = '<product-id>'
ORDER BY area_scope, area_code, metric_key, value_type;

SELECT product_id, raster_version, ST_Width(rast), ST_Height(rast),
       ST_SRID(rast), spatial_allocation_rule, coverage_ratio
FROM loss_product_rasters
WHERE product_id = '<product-id>';
```

Expected: required loss tasks succeed, resource gaps remain explicit if the production parameter set does not contain verified coefficients, and the effective run points to the latest completed loss revision.

- [ ] **Step 4: Run the final spec coverage check**

Record evidence against the specification:

```text
7. Region and spatial units       -> Tasks 3, 4
8. Model registry and parameters  -> Task 2
9. Building damage                -> Task 5
9. Population and shelter         -> Task 6
9. Casualties                     -> Task 7
9. Economic loss                  -> Task 8
9. Resource demand                -> Task 9
9. Validation                     -> Task 10
10. Product status and quality    -> Tasks 2, 10, 11
11.2 Model and parameter tables   -> Tasks 1, 2
11.3 Result tables                -> Tasks 1, 11
12. Temporal and completion       -> Task 13
13. APIs                          -> Task 14
14. UI, map, and permissions      -> Tasks 14, 15, 16
15. Exception handling            -> Tasks 10, 12, 17
16. Performance budget            -> Task 17
17. Test design                   -> Tasks 2-17
18. Acceptance criteria           -> Tasks 17, 18
19. Deliverables                  -> Tasks 1-18
```

Fix any uncovered acceptance criterion before committing.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/runbooks/loss-assessment.md docs/runbooks/assessment-orchestration.md
git commit -m "docs: document loss assessment operations"
```
