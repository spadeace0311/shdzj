# 专业制图与报告生产中心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有评估链路上新增可追溯的专业制图与报告生产中心，在 `deadline_basis_at + 300 秒` 内形成 27 类图件和 12 类文档或演示稿，并提供版本化成果中心、重算和超级管理员原子覆盖能力。

**Architecture:** 新增 `backend/app/artifacts/` 垂直子系统，以 `ProductionRun -> ProductionTask -> GeneratedArtifact -> Publication` 为核心实体。首轮全量生产由 `AssessmentWorkflow` 以 `ParentClosePolicy=ABANDON` 启动 `ArtifactProductionWorkflow`，后续重算和覆盖以 `production_run_id` 顶层启动或同步事务执行；图件、DOCX 和 PPTX 使用可替换渲染器，所有运行固定模板、数据资产、底图和渲染器快照。

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, PostgreSQL 16/PostGIS 3.4, Alembic, Temporal Python 1.33, Playwright, MapLibre GL JS, Matplotlib, python-docx, python-pptx, Pytest, Ruff, React 19, TypeScript, Vitest。

**Spec:** `docs/superpowers/specs/2026-09-30-map-report-production-design.md`

## Global Constraints

- 专业版目录固定为 27 类图件、4 类核心文档或演示稿、8 类背景文档，共 39 项；不实现公众版、领导版、救援版、简图版、移动端和 PDF。
- 首期输出规格固定为 `a3v-professional`；图件使用 `4761 x 3369`、300 DPI，默认 JPEG，可由目录声明 PNG。
- 首轮全量生产的 5 分钟从 `AssessmentRun.deadline_basis_at` 起算，不得在评估结束后重新起算。
- `AssessmentRun.t1_at` 必须改为数据库、ORM、Pydantic 和前端类型均可空；人工、测试和演练事件不得伪造 `T1`。
- 自动生产只接受正式报、更正报、人工正式事件、测试事件和演练事件；自动速报不创建评估或生产运行。
- 真实、测试、演练和回放共用生产链路，但必须使用独立事件、状态、前缀和发布空间。
- 测试模式文件名前缀固定为 `【测试】`，演练为 `【演练】`，回放为 `【测试回放】`；标识必须写入文件名、图片像素和文档正文。
- A 类必需依赖缺失时任务失败且不得伪造数据；B、C 类只能按目录规则生成 `degraded` 成果并显示“待复核”。
- 运行期不得依赖 ArcGIS、互联网底图或在线模板；高德是主离线底图，天地图是备用离线底图。
- 任一生产运行创建后不得替换模板、底图、数据资产、模型参数或渲染器版本；变更必须创建新的 `production_run_id`。
- 所有成果使用内容寻址存储、SHA-256、临时写入和原子登记；数据库失败不得留下当前发布指针。
- 所有发布、重算和覆盖保留历史版本；超级管理员覆盖不得删除原始报文、已发布报告、成果文件或操作日志。
- 成果重算权限固定为 `superadmin`，或 `workgroup == "应急技术组"` 且角色属于 `group_leader`、`group_deputy`、`group_member`。前后端必须共用这一判定规则。
- 所有行为变更使用红绿 TDD，每个任务结束运行相关测试并创建独立提交。
- 不得读取、打印、提交、记录或暴露 `.env`、密码、Token、API Key、连接串或其他凭据。
- 只在执行者指定的 Git worktree 中工作，不得修改主工作区 `D:\地震应急辅助决策系统`。

---

## File Structure

```text
backend/app/artifacts/
  __init__.py
  catalog.py
  domain.py
  models.py
  permissions.py
  naming.py
  dependencies.py
  context.py
  basemap.py
  template_builder.py
  repository.py
  storage.py
  validation.py
  service.py
  workflow.py
  worker.py
  schemas.py
  router.py
  renderers/
    __init__.py
    base.py
    map_layers.py
    map_renderer.py
    chart_renderer.py
    docx_renderer.py
    pptx_renderer.py

backend/migrations/versions/0015_artifact_production.py
backend/tests/test_artifact_schema.py
backend/tests/test_artifact_catalog.py
backend/tests/test_artifact_naming.py
backend/tests/test_artifact_repository.py
backend/tests/test_artifact_storage.py
backend/tests/test_artifact_override.py
backend/tests/test_artifact_context.py
backend/tests/test_artifact_basemap.py
backend/tests/test_artifact_map_renderer.py
backend/tests/test_artifact_maps_a.py
backend/tests/test_artifact_maps_degraded.py
backend/tests/test_artifact_docx.py
backend/tests/test_artifact_core_documents.py
backend/tests/test_artifact_workflow.py
backend/tests/test_artifact_api.py
backend/tests/test_artifact_performance.py

frontend/src/pages/ArtifactCenterPage.tsx
frontend/src/components/ArtifactProgressCard.tsx
frontend/src/components/ArtifactList.tsx
frontend/src/components/ArtifactPreview.tsx
frontend/src/components/ArtifactOverrideDialog.tsx
frontend/tests/artifact-center.test.tsx
frontend/tests/artifact-progress.test.tsx
frontend/e2e/artifact-center.spec.ts

config/artifacts/catalog.yaml
config/artifacts/templates/
config/artifacts/map/
config/data_assets/shanghai-artifact-assets.yaml
docs/operations/artifact-production-runbook.md
```

`app/artifacts/domain.py` 不导入 FastAPI、Temporal、SQLAlchemy、Playwright 或 python-docx。`repository.py` 负责事务和行锁，`service.py` 负责用例，`renderers/` 只接收不可变上下文并返回待存储文件。

---

### Task 1: 依赖、迁移、ORM 与 T1 可空能力

**Files:**
- Create: `backend/migrations/versions/0015_artifact_production.py`
- Create: `backend/app/artifacts/__init__.py`
- Create: `backend/app/artifacts/models.py`
- Create: `backend/tests/test_artifact_schema.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/app/assessment/models.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/test_migrations.py`
- Modify: `backend/Dockerfile`
- Modify: `infra/compose.yaml`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `assessment_runs.id`, `assessment_tasks.id`, `earthquake_events.id`, `earthquake_revisions.id`, `data_asset_versions.id`, existing Alembic head `0014_loss_assessment`。
- Produces ORM classes: `ArtifactTemplate`, `ArtifactTemplateVersion`, `ProductionInputSnapshot`, `ProductionInputSnapshotItem`, `ProductionRun`, `ProductionTask`, `ArtifactTaskDependencyBinding`, `GeneratedArtifact`, `ArtifactPublication`, `ArtifactOverrideRequest`。
- Produces table names: `artifact_templates`, `artifact_template_versions`, `production_input_snapshots`, `production_input_snapshot_items`, `artifact_production_runs`, `artifact_production_tasks`, `artifact_task_dependency_bindings`, `generated_artifacts`, `artifact_publications`, `artifact_override_requests`。
- Produces nullable contract: `AssessmentRun.t1_at: Mapped[datetime | None]`。

- [ ] **Step 1: Write the failing schema and migration tests**

Create `backend/tests/test_artifact_schema.py`:

```python
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

ARTIFACT_TABLES = {
    "artifact_templates",
    "artifact_template_versions",
    "production_input_snapshots",
    "production_input_snapshot_items",
    "artifact_production_runs",
    "artifact_production_tasks",
    "artifact_task_dependency_bindings",
    "generated_artifacts",
    "artifact_publications",
    "artifact_override_requests",
}


async def test_artifact_tables_and_nullable_t1_exist() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            t1_column = await connection.run_sync(
                lambda sync: next(
                    column
                    for column in inspect(sync).get_columns("assessment_runs")
                    if column["name"] == "t1_at"
                )
            )
    finally:
        await engine.dispose()

    assert ARTIFACT_TABLES <= tables
    assert t1_column["nullable"] is True


async def test_artifact_partial_unique_indexes_exist() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT indexname, indexdef
                        FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND indexname IN (
                            'uq_artifact_run_current_scope',
                            'uq_artifact_publication_current',
                            'uq_artifact_dependency_product',
                            'uq_artifact_dependency_artifact'
                          )
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    definitions = {row["indexname"]: row["indexdef"] for row in rows}
    assert "WHERE" in definitions["uq_artifact_run_current_scope"]
    assert "is_current" in definitions["uq_artifact_run_current_scope"]
    assert "superseded_at IS NULL" in definitions["uq_artifact_publication_current"]
    assert "dependency_kind = 'assessment_product'" in (
        definitions["uq_artifact_dependency_product"]
    )
    assert "dependency_kind = 'artifact'" in definitions["uq_artifact_dependency_artifact"]
```

Modify `backend/tests/test_migrations.py` by changing `LATEST_REVISION` to `0015_artifact_production` and adding:

```python
ARTIFACT_PREVIOUS_REVISION = "0014_loss_assessment"
ARTIFACT_TABLES = {
    "artifact_templates",
    "artifact_template_versions",
    "production_input_snapshots",
    "production_input_snapshot_items",
    "artifact_production_runs",
    "artifact_production_tasks",
    "artifact_task_dependency_bindings",
    "generated_artifacts",
    "artifact_publications",
    "artifact_override_requests",
}


async def _artifact_tables_exist() -> bool:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
    finally:
        await engine.dispose()
    return ARTIFACT_TABLES <= names


async def test_0015_artifact_production_is_reversible() -> None:
    _set_revision(ARTIFACT_PREVIOUS_REVISION)
    assert await _artifact_tables_exist() is False
    try:
        _set_revision(LATEST_REVISION)
        assert await _artifact_tables_exist() is True
        _set_revision(ARTIFACT_PREVIOUS_REVISION)
        assert await _artifact_tables_exist() is False
        _set_revision(LATEST_REVISION)
    finally:
        _set_revision(LATEST_REVISION)
```

- [ ] **Step 2: Run the schema tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_schema.py tests/test_migrations.py -v
```

Expected: FAIL because `0015_artifact_production` and all artifact tables do not exist.

- [ ] **Step 3: Add runtime dependencies and settings**

Add these exact dependencies to `backend/pyproject.toml`:

```toml
"matplotlib==3.10.0",
"Pillow==11.1.0",
"playwright==1.49.1",
"python-docx==1.1.2",
"python-pptx==1.0.2",
```

Add `artifact_storage_root`, `artifact_template_root`, `artifact_catalog_path`, `artifact_max_override_bytes`, `artifact_render_concurrency`, `artifact_optional_dependency_reserve_seconds`, and `artifact_browser_pool_size` to `Settings`. Validate all paths are non-empty, override size is positive, render concurrency is between 1 and 6, browser pool size is between 1 and 8, and reserve seconds is positive and less than 300.

Add a named `artifact-data` volume mounted at `/var/lib/artifacts` to `api`, `temporal-worker`, and the new artifact worker. Keep the default render concurrency at 4 and browser pool size at 2 so Z440 does not oversubscribe. Add the new non-secret variables to `.env.example`, import `app.artifacts.models` in `backend/migrations/env.py`, and extend `backend/Dockerfile` with Chromium, Chinese fonts, MapLibre static assets, and the libraries required by Playwright, Matplotlib, python-docx, and python-pptx.

- [ ] **Step 4: Implement migration 0015 and the ORM models**

Create `0015_artifact_production.py` with `down_revision = "0014_loss_assessment"`. In `upgrade()`, first run:

```python
op.alter_column("assessment_runs", "t1_at", existing_type=sa.DateTime(timezone=True), nullable=True)
```

Create every table and constraint from spec section 11. At minimum include these database-enforced rules:

```python
UniqueConstraint("assessment_run_id", "generation_seq", name="uq_artifact_run_generation")
UniqueConstraint(
    "production_run_id",
    "artifact_key",
    "output_profile",
    name="uq_artifact_task_output",
)
UniqueConstraint(
    "event_id",
    "artifact_key",
    "output_profile",
    "artifact_version",
    name="uq_generated_artifact_version",
)
UniqueConstraint(
    "actor_id",
    "endpoint",
    "idempotency_key",
    name="uq_artifact_override_request",
)
UniqueConstraint("template_key", name="uq_artifact_template_key")
UniqueConstraint(
    "template_id",
    "version",
    name="uq_artifact_template_version",
)
UniqueConstraint(
    "production_run_id",
    name="uq_production_input_snapshot_run",
)
UniqueConstraint(
    "snapshot_id",
    "asset_key",
    "role",
    name="uq_production_input_snapshot_item",
)
CheckConstraint(
    "status IN ('pending','running','completed','partial','failed','canceled')",
    name="ck_artifact_run_status",
)
CheckConstraint(
    "production_mode IN ('live','manual','test','drill','replay')",
    name="ck_artifact_production_mode",
)
```

Create these partial unique indexes with `postgresql_where`:

```python
Index(
    "uq_artifact_run_current_scope",
    "event_id",
    "revision_id",
    "generation_scope",
    unique=True,
    postgresql_where=text("is_current AND superseded_at IS NULL"),
)
Index(
    "uq_artifact_publication_current",
    "event_id",
    "artifact_key",
    "output_profile",
    "production_mode",
    unique=True,
    postgresql_where=text("superseded_at IS NULL"),
)
Index(
    "uq_artifact_dependency_product",
    "production_task_id",
    "dependency_key",
    unique=True,
    postgresql_where=text("dependency_kind = 'assessment_product'"),
)
Index(
    "uq_artifact_dependency_artifact",
    "production_task_id",
    "dependency_key",
    "dependency_output_profile",
    unique=True,
    postgresql_where=text("dependency_kind = 'artifact'"),
)
Index(
    "uq_generated_artifact_final_task",
    "production_task_id",
    unique=True,
    postgresql_where=text("is_final"),
)
```

`downgrade()` drops the artifact tables in dependency order, then restores `assessment_runs.t1_at` to non-nullable. The migration test database must contain no null `t1_at` rows during downgrade.

Create SQLAlchemy models with typed columns and relationships. `ProductionRun.assessment_run_id` is not unique. `ProductionRun.required_outputs` is `JSONB`. `ProductionTask.depends_on` and `optional_depends_on` are `JSONB` arrays. `GeneratedArtifact.superseded_by_id` references `generated_artifacts.id` with `ondelete="SET NULL"`. `ArtifactOverrideRequest.lease_generation` and `attempt_count` default to `1` and `0`.

- [ ] **Step 5: Run the tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_schema.py tests/test_migrations.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/pyproject.toml backend/app/assessment/models.py backend/app/artifacts/__init__.py backend/app/artifacts/models.py backend/migrations/env.py backend/migrations/versions/0015_artifact_production.py backend/tests/test_artifact_schema.py backend/tests/test_migrations.py backend/Dockerfile infra/compose.yaml .env.example
git commit -m "feat: add artifact production schema"
```

---

### Task 2: 成果目录、领域类型、命名和依赖图

**Files:**
- Create: `backend/app/artifacts/domain.py`
- Create: `backend/app/artifacts/catalog.py`
- Create: `backend/app/artifacts/naming.py`
- Create: `backend/app/artifacts/dependencies.py`
- Create: `backend/tests/test_artifact_catalog.py`
- Create: `backend/tests/test_artifact_naming.py`
- Create: `config/artifacts/catalog.yaml`

**Interfaces:**
- Consumes: no database or workflow dependency.
- Produces enums: `ArtifactKind`, `ProductionMode`, `LaunchMode`, `ProductionRunStatus`, `ProductionTaskStatus`, `PublicationMode`, `DependencyKind`, `ResolutionStatus`。
- Produces dataclasses: `ArtifactDefinition`, `DependencySpec`, `ArtifactCatalog`, `ArtifactNameContext`。
- Produces functions:
  - `ArtifactCatalog.load(path: str | Path) -> ArtifactCatalog`
  - `ArtifactCatalog.get(artifact_key: str, output_profile: str) -> ArtifactDefinition`
  - `ArtifactCatalog.full_required_outputs() -> tuple[tuple[str, str], ...]`
  - `ArtifactCatalog.scope_required_outputs(scope: str, default_profile: str) -> tuple[tuple[str, str], ...]`
  - `build_artifact_file_name(context: ArtifactNameContext, *, extension: str) -> str`
  - `ArtifactDependencyGraph(catalog).ready_keys(task_keys: set[str], resolved: set[str]) -> tuple[str, ...]`

- [ ] **Step 1: Write the failing catalog, dependency, and naming tests**

Create `backend/tests/test_artifact_catalog.py`:

```python
from pathlib import Path

import pytest

from app.artifacts.catalog import ArtifactCatalog
from app.artifacts.domain import ArtifactKind, DependencyKind, ProductionMode

CATALOG_PATH = Path("/config/artifacts/catalog.yaml")


def test_catalog_contains_exactly_39_professional_outputs() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    outputs = catalog.full_required_outputs()

    assert len(outputs) == 39
    assert sum(catalog.get(key, "a3v-professional").kind == ArtifactKind.MAP for key, _ in outputs) == 27
    assert sum(catalog.get(key, "a3v-professional").kind == ArtifactKind.DOCX for key, _ in outputs) == 11
    assert sum(catalog.get(key, "a3v-professional").kind == ArtifactKind.PPTX for key, _ in outputs) == 1
    assert all(profile == "a3v-professional" for _, profile in outputs)
    assert all(
        catalog.get(key, profile).format in {"jpg", "png", "docx", "pptx"}
        for key, profile in outputs
    )


def test_catalog_dependencies_are_typed_and_acyclic() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    decision = catalog.get("doc.decision_report", "a3v-professional")

    assert DependencySpecExample(
        kind=DependencyKind.ARTIFACT,
        key="doc.rapid_report",
        output_profile="a3v-professional",
    ).to_dict() in decision.depends_on
    assert catalog.assert_acyclic() is None


def test_scope_parser_rejects_ambiguous_artifact_scope() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    assert catalog.scope_required_outputs("full", "a3v-professional") == (
        catalog.full_required_outputs()
    )
    assert catalog.scope_required_outputs(
        "artifact:map.epicenter:a3v-professional",
        "a3v-professional",
    ) == (("map.epicenter", "a3v-professional"),)
    with pytest.raises(ValueError):
        catalog.scope_required_outputs("artifact:map.epicenter", "a3v-professional")
```

Import `DependencySpec as DependencySpecExample` at the top of the test rather than creating a duplicate test-only class.

Create `backend/tests/test_artifact_naming.py`:

```python
from app.artifacts.domain import ArtifactNameContext, ProductionMode
from app.artifacts.naming import build_artifact_file_name


def test_file_name_contains_required_mode_marker_and_version() -> None:
    context = ArtifactNameContext(
        place="浦东新区",
        magnitude=5.1,
        display_name="震中位置分布图",
        version=1,
        generated_at="2026-09-30T15:30:00+08:00",
        production_mode=ProductionMode.TEST,
    )

    assert build_artifact_file_name(context, extension="jpg") == (
        "【测试】浦东新区_5.1级地震_震中位置分布图_V001_20260930-153000.jpg"
    )


def test_file_name_sanitizes_windows_illegal_characters() -> None:
    context = ArtifactNameContext(
        place='浦东<新区>:"/\\|?*',
        magnitude=4.0,
        display_name="震中位置分布图",
        version=2,
        generated_at="2026-09-30T15:30:00+08:00",
        production_mode=ProductionMode.LIVE,
    )

    assert build_artifact_file_name(context, extension="png") == (
        "浦东_新区_4.0级地震_震中位置分布图_V002_20260930-153000.png"
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_catalog.py tests/test_artifact_naming.py -v
```

Expected: FAIL because the domain, catalog, naming modules, and YAML do not exist.

- [ ] **Step 3: Implement domain and catalog**

Create `config/artifacts/catalog.yaml` with `catalog_version: "2026.09.30-professional-v1"` and exactly these keys:

```text
map.shelter_emergency
map.intensity
map.economic_loss
map.rescue_demand
map.deaths
map.injuries
map.buried
map.material_demand
map.gdp
map.pga_zoning
map.transport
map.historical_earthquakes
map.population
map.reservoirs
map.hazard_sources
map.schools
map.hospitals
map.metro
map.seismic_stations
map.active_faults
map.building_damage
map.building_grid
map.rescue_teams
map.cultural_relics
map.key_targets
map.epicenter
map.city_distances
doc.background
doc.housing
doc.economy
doc.population
doc.key_targets
doc.spatial_distances
doc.area_overview
doc.historical_catalog
doc.rapid_brief
doc.rapid_report
doc.decision_report
deck.decision_report
```

Use `legacy_code` `M01` through `M27` and `D01` through `D12` in the displayed order. Set `output_profile: a3v-professional`, `format: jpg`, `dimensions: [4761, 3369]`, `dpi: 300` for every map. For maps, set the product dependencies exactly as follows:

```text
map.intensity: assessment_product(intensity.fusion)
map.economic_loss: assessment_product(loss.economic)
map.rescue_demand: assessment_product(loss.resources)
map.deaths: assessment_product(loss.casualties)
map.injuries: assessment_product(loss.casualties)
map.buried: assessment_product(loss.casualties)
map.material_demand: assessment_product(loss.resources)
map.building_damage: assessment_product(loss.buildings)
map.building_grid: assessment_product(loss.buildings)
all other maps: []
```

For documents, encode these exact hard dependencies:

```text
doc.background: []
doc.housing: assessment_product(loss.buildings), assessment_product(loss.population)
doc.economy: assessment_product(loss.economic)
doc.population: assessment_product(loss.population)
doc.key_targets: []
doc.spatial_distances: []
doc.area_overview: artifact(doc.background), artifact(doc.housing), artifact(doc.economy), artifact(doc.population), artifact(doc.key_targets), artifact(doc.spatial_distances)
doc.historical_catalog: []
doc.rapid_brief: assessment_product(intensity.fusion), assessment_product(loss.buildings), assessment_product(loss.population), assessment_product(loss.casualties), assessment_product(loss.economic), assessment_product(loss.resources), assessment_product(loss.validate), artifact(map.intensity), artifact(map.deaths), artifact(map.injuries), artifact(map.population), artifact(map.building_damage), artifact(map.epicenter)
doc.rapid_report: artifact(doc.background), artifact(doc.housing), artifact(doc.economy), artifact(doc.population), artifact(doc.key_targets), artifact(doc.spatial_distances), artifact(doc.area_overview), artifact(doc.historical_catalog), plus all A-class maps from M02-M27
doc.decision_report: artifact(doc.rapid_brief), artifact(doc.rapid_report), artifact(map.intensity), artifact(map.economic_loss), artifact(map.rescue_demand), artifact(map.deaths), artifact(map.injuries), artifact(map.buried), artifact(map.material_demand), artifact(map.active_faults), artifact(map.key_targets), artifact(map.epicenter), artifact(map.city_distances)
deck.decision_report: artifact(doc.decision_report)
```

Put B and C class maps in `optional_depends_on` for `doc.rapid_report`. Every A-class map has `failure_policy: block`; B maps use `degrade`; `map.building_grid` uses `degrade` only when `spatialized_estimate=true`, otherwise it fails.

- [ ] **Step 4: Implement naming and dependency graph**

`build_artifact_file_name` must use `Asia/Shanghai`, format magnitude with one decimal, version with three digits, remove Windows-illegal characters, collapse repeated underscores, trim the place to 80 characters, and allow only `jpg`, `png`, `docx`, and `pptx`.

`ArtifactDependencyGraph` must resolve only keys present in the run's required outputs and fail if a hard dependency is absent from the catalog. It must never treat a failed or timed-out dependency as ready.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_catalog.py tests/test_artifact_naming.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/domain.py backend/app/artifacts/catalog.py backend/app/artifacts/naming.py backend/app/artifacts/dependencies.py backend/tests/test_artifact_catalog.py backend/tests/test_artifact_naming.py config/artifacts/catalog.yaml
git commit -m "feat: define artifact production catalog"
```

---

### Task 3: 生产运行仓储、任务生命周期和发布事务

**Files:**
- Create: `backend/app/artifacts/repository.py`
- Create: `backend/app/artifacts/service.py`
- Create: `backend/tests/test_artifact_repository.py`

**Interfaces:**
- Consumes: Task 1 ORM models, Task 2 catalog and dependency graph。
- Produces dataclasses: `CreateProductionRunCommand`, `PreparedProductionRun`, `TaskCompletion`, `ArtifactGenerationResult`, `ArtifactQuality`, `ProductionDependencyBinding`。
- Produces methods on `ArtifactProductionRepository`:
  - `create_run(session, command) -> ProductionRun`
  - `prepare_dependencies(session, production_run_id) -> tuple[ProductionTask, ...]`
  - `bind_dependency(session, production_task_id, binding) -> ArtifactTaskDependencyBinding`
  - `freeze_task_fingerprint(session, production_task_id, fingerprint) -> ProductionTask`
  - `start_task(session, production_task_id, activity_idempotency_key) -> ProductionTask`
  - `complete_task(session, task_id, artifact_result, task_status) -> GeneratedArtifact`
  - `fail_task(session, task_id, error_category, error_summary) -> ProductionTask`
  - `timeout_run(session, production_run_id, observed_at) -> ProductionRun`
  - `finalize_run(session, production_run_id, observed_at) -> ProductionRun`
  - `publish_artifact(session, artifact_id, published_by, forced) -> ArtifactPublication`
  - `create_override_run(session, command) -> tuple[ProductionRun, ProductionTask, GeneratedArtifact, ArtifactPublication]`
  - `supersede_runs_for_revision(session, event_id, new_revision_id, new_revision_no) -> tuple[UUID, ...]`
  - `cancel_non_live_runs_for_real_event(session, event_id) -> tuple[UUID, ...]`

- [ ] **Step 1: Write the failing lifecycle tests**

Create `backend/tests/test_artifact_repository.py` with focused tests:

```python
from datetime import UTC, datetime, timedelta

import pytest


async def test_full_run_creates_exactly_39_tasks(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    tasks = await artifact_repository.list_tasks(session, run.id)

    assert len(tasks) == 39
    assert len({(task.artifact_key, task.output_profile) for task in tasks}) == 39
    assert all(task.status == "pending" for task in tasks)


async def test_single_artifact_rebuild_does_not_replace_full_progress(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    full = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    rebuild = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.rebuild_command("map.epicenter"),
    )

    assert full.id != rebuild.id
    assert rebuild.generation_scope == "artifact:map.epicenter:a3v-professional"
    assert len(await artifact_repository.list_tasks(session, rebuild.id)) == 1
    assert await artifact_repository.get_current_full_run(
        session,
        event_id=full.event_id,
        revision_id=full.revision_id,
    ) == full


async def test_finalize_run_partial_keeps_successful_artifacts(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    run = await artifact_repository.create_run(
        session,
        seeded_artifact_assessment.full_run_command(),
    )
    tasks = await artifact_repository.list_tasks(session, run.id)
    await artifact_repository.mark_task_succeeded_for_test(session, tasks[0].id)
    await artifact_repository.mark_task_failed_for_test(
        session,
        tasks[1].id,
        observed_at=run.deadline_at + timedelta(seconds=1),
    )

    finalized = await artifact_repository.finalize_run(
        session,
        run.id,
        observed_at=run.deadline_at + timedelta(seconds=1),
    )

    assert finalized.status == "partial"
    assert finalized.deadline_exceeded_at is not None
    assert finalized.last_artifact_committed_at is not None


async def test_publish_artifact_atomically_supersedes_previous(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    first = await seeded_artifact_assessment.published_artifact("map.epicenter", version=1)
    second = await seeded_artifact_assessment.generated_artifact("map.epicenter", version=2)
    old_publication = await artifact_repository.get_publication(
        session,
        event_id=first.event_id,
        artifact_key=first.artifact_key,
        output_profile=first.output_profile,
        production_mode=first.production_mode,
    )

    publication = await artifact_repository.publish_artifact(
        session,
        second.id,
        published_by="superadmin",
        forced=False,
    )
    previous = await artifact_repository.get_publication(
        session,
        event_id=first.event_id,
        artifact_key=first.artifact_key,
        output_profile=first.output_profile,
        production_mode=first.production_mode,
    )

    assert publication.artifact_id == second.id
    assert publication.is_forced is False
    assert old_publication is not None
    await session.refresh(old_publication)
    assert old_publication.superseded_at is not None
    assert previous is not None
    assert previous.artifact_id == second.id


async def test_correction_supersedes_old_revision_and_requests_cancel(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    old = await seeded_artifact_assessment.create_full_run(revision_no=1)
    correction = await seeded_artifact_assessment.create_correction_revision(revision_no=2)

    canceled_ids = await artifact_repository.supersede_runs_for_revision(
        session,
        event_id=old.event_id,
        new_revision_id=correction.id,
        new_revision_no=2,
    )
    await session.refresh(old)

    assert old.id in canceled_ids
    assert old.is_current is False
    assert old.superseded_at is not None
    assert old.cancel_reason == "revision_superseded"


async def test_real_event_cancels_test_and_drill_runs(
    seeded_artifact_assessment,
    artifact_repository,
    session,
) -> None:
    test_run = await seeded_artifact_assessment.create_full_run(production_mode="test")
    drill_run = await seeded_artifact_assessment.create_full_run(production_mode="drill")

    canceled_ids = await artifact_repository.cancel_non_live_runs_for_real_event(
        session,
        event_id=test_run.event_id,
    )

    assert set(canceled_ids) == {test_run.id, drill_run.id}
    assert (await session.get(type(test_run), test_run.id)).cancel_reason == "real_event_priority"
    assert (await session.get(type(drill_run), drill_run.id)).cancel_reason == "real_event_priority"
```

Add fixture methods to `backend/tests/artifact_helpers.py` so tests seed real `EarthquakeEvent`, `EarthquakeRevision`, `AssessmentRun`, `ProductionRun`, `ProductionTask`, and `GeneratedArtifact` rows. Do not mock SQLAlchemy transactions.

- [ ] **Step 2: Run the lifecycle tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_repository.py -v
```

Expected: FAIL because the repository and fixtures do not exist.

- [ ] **Step 3: Implement transactional repository behavior**

Every mutating method uses one `AsyncSession` transaction supplied by the caller. Lock order is always `event -> assessment_run -> production_run -> production_task -> generated_artifact -> artifact_publications`. Use `SELECT ... FOR UPDATE` for current-run and publication changes.

`complete_task` behavior:

```python
async def complete_task(
    self,
    session: AsyncSession,
    task_id: UUID,
    artifact_result: ArtifactGenerationResult,
    task_status: str,
) -> GeneratedArtifact:
    if task_status not in {"succeeded", "degraded"}:
        raise ValueError("completed task status must be succeeded or degraded")
    # Lock task and run.
    # Reject writes after deadline_at.
    # Allocate event+artifact+profile+version under event advisory lock.
    # Insert immutable GeneratedArtifact.
    # Set task final_artifact_id, output_checksum, completed_at, status.
    # Update run.last_artifact_committed_at using max(existing, now).
    ...
```

`publish_artifact` behavior:

```python
async def publish_artifact(
    self,
    session: AsyncSession,
    artifact_id: UUID,
    published_by: str | None,
    forced: bool,
) -> ArtifactPublication:
    # Lock artifact, production run, old publication row.
    # Reject non-current run, superseded revision, or lower generation_seq.
    # Set old publication.superseded_at.
    # Insert new current publication with generation_seq.
    # Mark artifact is_final=true and published_at.
    ...
```

`finalize_run` rules:

```text
all required tasks succeeded/degraded and all committed <= deadline_at:
    completed
at least one succeeded/degraded:
    partial
none:
    failed
```

Set `deadline_exceeded_at` when any valid artifact commits after `deadline_at` or when unfinished tasks are timed out.

For tasks with document-level `optional_depends_on`, set:

```python
optional_dependency_wait_cutoff_at = run.deadline_at - timedelta(
    seconds=settings.artifact_optional_dependency_reserve_seconds
)
```

The cutoff must be earlier than `deadline_at` and must leave enough time for file validation, immutable storage, and the publication transaction. Tasks with no optional artifact or assessment-product dependencies leave this field `None`.

- [ ] **Step 4: Implement service orchestration boundaries**

Create `ArtifactProductionService` with these methods:

```python
async def create_initial_run(
    self,
    *,
    assessment_run_id: UUID,
    event_id: UUID,
    revision_id: UUID,
) -> PreparedProductionRun: ...

async def create_rebuild_run(
    self,
    *,
    event_id: UUID,
    revision_id: UUID,
    artifact_key: str,
    output_profile: str,
    requested_by: str,
) -> PreparedProductionRun: ...

async def prepare_task(
    self,
    production_task_id: UUID,
) -> ArtifactRenderInput: ...

async def commit_task_result(
    self,
    task_id: UUID,
    result: ArtifactGenerationResult,
) -> GeneratedArtifact: ...
```

`create_initial_run` uses the assessment run's `deadline_basis_at` and `deadline_at` unchanged and creates exactly 39 tasks. `create_rebuild_run` uses `now + 300 seconds` and creates exactly one task.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_repository.py tests/test_artifact_catalog.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/repository.py backend/app/artifacts/service.py backend/tests/artifact_helpers.py backend/tests/test_artifact_repository.py
git commit -m "feat: add artifact production lifecycle"
```

---

### Task 4: 内容寻址存储、格式校验和覆盖幂等

**Files:**
- Create: `backend/app/artifacts/storage.py`
- Create: `backend/app/artifacts/validation.py`
- Create: `backend/tests/test_artifact_storage.py`
- Create: `backend/tests/test_artifact_override.py`
- Modify: `backend/app/artifacts/service.py`

**Interfaces:**
- Consumes: Task 1 `artifact_override_requests`, Task 3 publication transaction。
- Produces dataclass `StoredArtifactFile` with `file_name`, `relative_path`, `managed_path`, `size_bytes`, `checksum`。
- Produces methods on `ArtifactStore`:
  - `stage(source: BinaryIO, *, file_name: str) -> Path`
  - `store_immutable(path: Path, *, file_name: str) -> StoredArtifactFile`
  - `store_immutable_stream(source: BinaryIO, *, file_name: str) -> StoredArtifactFile`
  - `resolve(relative_path: str) -> Path`
  - `delete_unreferenced(stored: StoredArtifactFile) -> None`
- Produces `ArtifactValidator.validate(path, definition, production_mode) -> ValidationResult`。
- Produces dataclass `ArtifactOverrideResponse`。
- Produces `ArtifactOverrideService.override(...) -> ArtifactOverrideResponse`。

- [ ] **Step 1: Write failing storage and override tests**

Create `backend/tests/test_artifact_storage.py`:

```python
from io import BytesIO

import pytest

from app.artifacts.domain import ArtifactDefinition, ArtifactKind, ProductionMode
from app.artifacts.storage import ArtifactStore
from app.artifacts.validation import ArtifactValidator


def test_store_is_content_addressed_and_rejects_path_escape(tmp_path) -> None:
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    stored = store.store_immutable_stream(
        BytesIO(b"artifact-bytes"),
        file_name="map.jpg",
    )

    assert stored.checksum == "6521df166eb07efaf36eba5b6bedefd9d6a252e9c80bab1c99653700ec71473c"
    assert stored.relative_path.endswith("-map.jpg")
    with pytest.raises(ValueError):
        store.resolve("../outside.jpg")


def test_validator_rejects_extension_mismatch(tmp_path) -> None:
    path = tmp_path / "fake.jpg"
    path.write_bytes(b"not-an-image")
    definition = ArtifactDefinition(
        artifact_key="map.epicenter",
        display_name="震中位置分布图",
        kind=ArtifactKind.MAP,
        output_profile="a3v-professional",
        format="jpg",
        legacy_code="M26",
        priority=80,
        depends_on=(),
        optional_depends_on=(),
        required_assets=(),
        optional_assets=(),
        template_key="map.epicenter",
        marker_policy="mode",
        failure_policy="block",
        quality_policy="A",
    )

    result = ArtifactValidator().validate(
        path,
        definition,
        production_mode=ProductionMode.LIVE,
    )
    assert result.valid is False
    assert result.error_category == "format_mismatch"
```

Replace the checksum assertion with the exact SHA-256 literal; do not leave the sample string.

Create `backend/tests/test_artifact_override.py`:

```python
async def test_same_idempotency_key_and_fingerprint_returns_same_result(
    artifact_override_service,
    seeded_event,
    valid_override_jpeg,
) -> None:
    first = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="替换错误图件",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000001",
        upload=valid_override_jpeg,
    )
    second = await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="替换错误图件",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000001",
        upload=valid_override_jpeg,
    )

    assert second.artifact_id == first.artifact_id
    assert second.production_run_id == first.production_run_id


async def test_same_idempotency_key_with_different_fingerprint_conflicts(
    artifact_override_service,
    seeded_event,
    valid_override_jpeg,
    other_override_jpeg,
) -> None:
    await artifact_override_service.override(
        actor_id="admin-id",
        event_id=seeded_event.id,
        artifact_key="map.epicenter",
        output_profile="a3v-professional",
        revision_id=seeded_event.current_revision_id,
        reason="第一次原因",
        expected_current_artifact_id=None,
        idempotency_key="00000000-0000-0000-0000-000000000002",
        upload=valid_override_jpeg,
    )

    with pytest.raises(IdempotencyConflictError):
        await artifact_override_service.override(
            actor_id="admin-id",
            event_id=seeded_event.id,
            artifact_key="map.epicenter",
            output_profile="a3v-professional",
            revision_id=seeded_event.current_revision_id,
            reason="第二次原因",
            expected_current_artifact_id=None,
            idempotency_key="00000000-0000-0000-0000-000000000002",
            upload=other_override_jpeg,
        )
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_storage.py tests/test_artifact_override.py -v
```

Expected: FAIL because storage, validation, and override services do not exist.

- [ ] **Step 3: Implement staging, immutable storage, and validation**

Use `ARTIFACT_STORAGE_ROOT/staging` for temporary files and `ARTIFACT_STORAGE_ROOT/objects/<aa>/<bb>/<sha256>-<safe-name>` for immutable objects. Copy from staging only after validation. Flush and `os.fsync` before `Path.replace`.

`ArtifactValidator` checks:

```text
file exists, regular file, non-zero, <= max bytes
extension in jpg/png/docx/pptx
magic bytes and parser result match extension
map dimensions 4761 x 3369 and DPI 300
map non-empty pixel ratio >= configured threshold
test/drill/replay text pixels or embedded metadata present
DOCX is readable by python-docx and has no unresolved {{...}} marker
PPTX is readable by python-pptx and has no unresolved {{...}} marker
```

Return `ValidationResult(valid, error_category, summary, checksum, dimensions, page_count)` without mutating the source.

- [ ] **Step 4: Implement the override claim and publication protocol**

Compute the request fingerprint before writing the database claim:

```python
request_fingerprint = sha256_json(
    {
        "event_id": str(event_id),
        "revision_id": str(revision_id),
        "artifact_key": artifact_key,
        "output_profile": output_profile,
        "expected_current_artifact_id": (
            str(expected_current_artifact_id) if expected_current_artifact_id else None
        ),
        "file_name": file_name,
        "size_bytes": size_bytes,
        "checksum": checksum,
        "reason": reason,
    }
)
```

Implement the exact lease protocol from spec section 11.8. A loser never creates a run. An expired `processing` row can be retaken only with an atomic update that increments `lease_generation`; the old worker must check its in-memory generation in the final transaction.

On successful validation and publication, create one synthetic `ProductionRun`, one `ProductionTask`, one `GeneratedArtifact`, and one `ArtifactPublication` in the same transaction that sets the override request to `succeeded`. On any failure after staging, remove only the temporary or newly stored object if no committed artifact row references it.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_storage.py tests/test_artifact_override.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS, including concurrent request and stale-lease tests.

```bash
git add backend/app/artifacts/storage.py backend/app/artifacts/validation.py backend/app/artifacts/service.py backend/tests/test_artifact_storage.py backend/tests/test_artifact_override.py
git commit -m "feat: store and validate artifact versions"
```

---

### Task 5: 生产输入快照、数据资产契约和上下文指纹

**Files:**
- Create: `backend/app/artifacts/context.py`
- Create: `backend/tests/test_artifact_context.py`
- Create: `config/data_assets/shanghai-artifact-assets.yaml`
- Modify: `backend/app/data_assets/required_registry.py`
- Modify: `backend/app/data_assets/registry.py`
- Modify: `backend/app/config.py`

**Interfaces:**
- Consumes: `DataAssetVersion`, `DataAssetRecord`, `DataAssetRaster`, assessment products, Task 1 snapshot tables。
- Produces dataclasses: `StaticProductionContext`, `ArtifactTaskContext`, `FrozenAssetVersion`。
- Produces `StaticProductionContext.item(asset_key: str) -> FrozenAssetVersion`。
- Produces methods on `ProductionContextService`:
  - `freeze_static_context(session, production_run_id, catalog) -> StaticProductionContext`
  - `build_task_context(session, production_task_id) -> ArtifactTaskContext`
  - `build_map_context(session, production_task_id) -> MapRenderContext`
  - `build_document_context(session, production_task_id, document_type: str) -> DocumentRenderContext`
- Produces stable `sha256_json(value: object) -> str` used by context, dependency, request, and render fingerprints。

- [ ] **Step 1: Write failing context and snapshot tests**

Create `backend/tests/test_artifact_context.py`:

```python
async def test_static_context_is_deterministic_and_does_not_drift(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    first = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )
    seeded_artifact_assessment.publish_new_template_version("map.epicenter", "v2")
    second = await production_context_service.build_task_context(
        session,
        (await seeded_artifact_assessment.first_task(run.id)).id,
    )

    assert len(first.catalog_version) == len("2026.09.30-professional-v1")
    assert second.context_fingerprint == first.context_fingerprint
    assert second.template_versions["map.epicenter"]["version"] == "v1"


async def test_missing_but_optional_asset_is_recorded_not_invented(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run(
        missing_optional_assets={"shanghai.reservoir"},
    )
    snapshot = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )

    item = snapshot.item("shanghai.reservoir")
    assert item.resolution_status == "missing"
    assert item.asset_version_id is None
    assert item.checksum is None


async def test_t1_none_is_serialized_as_null(
    seeded_artifact_assessment,
    production_context_service,
    session,
) -> None:
    run = await seeded_artifact_assessment.create_full_run(t1_at=None)
    context = await production_context_service.freeze_static_context(
        session,
        run.id,
        seeded_artifact_assessment.catalog,
    )

    assert context.manifest["t1_at"] is None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_context.py -v
```

Expected: FAIL because context freezing and artifact asset contracts do not exist.

- [ ] **Step 3: Add artifact asset contracts**

Create `config/data_assets/shanghai-artifact-assets.yaml` with the 15 keys from spec section 10.1 and the basemap keys `basemap.gaode.offline` and `basemap.tianditu.offline`. Each entry declares:

```yaml
- asset_key: shanghai.shelter.emergency
  data_type: vector
  geometry_type: point-or-polygon
  required_fields: [id, name, address, capacity, level, authority, updated_at]
  crs: EPSG:4326
  artifact_usage: [map.shelter_emergency, doc.key_targets]
  classification: B
```

Update `required_registry.py` so the original seven core assets remain required and the artifact-only assets are loaded from this contract catalog. Extend `registry.py` with a function that returns all registered artifact asset keys without weakening existing data-asset import validation.

- [ ] **Step 4: Implement immutable production snapshots**

`freeze_static_context` runs in one transaction and writes exactly one `production_input_snapshots` row plus one item per required or optional asset. Select only published `DataAssetVersion` rows. Copy `asset_version_id`, `checksum`, `coverage`, and `role`; never copy mutable asset rows.

The manifest is normalized before hashing:

```python
manifest = {
    "catalog_version": catalog.catalog_version,
    "event": {
        "event_id": str(event.id),
        "revision_id": str(revision.id),
        "revision_no": revision.revision_no,
        "event_kind": revision.revision_kind,
        "t1_at": event.t1_at.isoformat() if event.t1_at else None,
        "deadline_basis_at": assessment_run.deadline_basis_at.isoformat(),
    },
    "assets": sorted_asset_versions,
    "templates": sorted_template_versions,
    "basemaps": selected_basemap_manifest,
    "fonts": font_bundle_manifest,
    "renderer_versions": {
        "maplibre": "5.6.0",
        "playwright": "1.49.1",
        "python-docx": "1.1.2",
        "python-pptx": "1.0.2",
    },
    "naming_version": "artifact-name-v1",
}
context_fingerprint = sha256_json(manifest)
```

Use `datetime.isoformat()` only behind the `if value is not None` guard. A later template or asset publication must not modify an existing snapshot.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_context.py tests/test_data_asset_required_registry.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/context.py backend/tests/test_artifact_context.py config/data_assets/shanghai-artifact-assets.yaml backend/app/data_assets/required_registry.py backend/app/data_assets/registry.py backend/app/config.py
git commit -m "feat: freeze artifact production inputs"
```

---

### Task 6: 离线底图清单和精确瓦片校验

**Files:**
- Create: `backend/app/artifacts/basemap.py`
- Create: `backend/tests/test_artifact_basemap.py`
- Modify: `backend/app/artifacts/context.py`
- Modify: `backend/app/config.py`
- Modify: `infra/compose.yaml`

**Interfaces:**
- Consumes: `MapViewportTileManifest`, published offline basemap packages, event center and map profiles。
- Produces dataclasses: `TileKey`, `MapViewportTileManifest`, `OfflineBasemapPackage`, `BasemapValidationResult`, `SelectedBasemap`。
- Produces methods:
  - `MapViewportTileManifest.build(center_lon, center_lat, radius_km, output_width, output_height, zoom_levels) -> MapViewportTileManifest`
  - `OfflineBasemapValidator.validate_package(package, manifests, *, observed_at) -> BasemapValidationResult`
  - `BasemapSelector.select(gaode, tianditu, manifests, observed_at) -> SelectedBasemap`

- [ ] **Step 1: Write failing exact-tile and fallback tests**

Create `backend/tests/test_artifact_basemap.py`:

```python
from datetime import UTC, datetime, timedelta

import pytest


def test_manifest_contains_exact_required_xyz_without_duplicates(
    tile_manifest_factory,
) -> None:
    manifest = tile_manifest_factory(center=(121.5, 31.2), zoom_levels=(9, 10))

    keys = [(tile.z, tile.x, tile.y) for tile in manifest.tiles]
    assert len(keys) == len(set(keys))
    assert manifest.required_count == len(keys)
    assert (-1, 0, 10) not in keys


def test_one_missing_exact_tile_fails_package_even_if_200_samples_pass(
    offline_basemap_package,
    tile_manifest,
) -> None:
    offline_basemap_package.remove_tile(tile_manifest.tiles[137])

    result = OfflineBasemapValidator().validate_package(
        offline_basemap_package,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert result.valid is False
    assert result.error_category == "required_tile_missing"
    assert result.missing_tiles == ((tile_manifest.tiles[137].z, tile_manifest.tiles[137].x, tile_manifest.tiles[137].y),)


def test_selector_uses_tianditu_only_after_gaode_fails(
    offline_basemap_package,
    tile_manifest,
) -> None:
    gaode = offline_basemap_package.with_missing_tile(tile_manifest.tiles[0])
    tianditu = offline_basemap_package.valid_copy()

    selected = BasemapSelector().select(
        gaode,
        tianditu,
        [tile_manifest],
        observed_at=datetime(2026, 9, 30, tzinfo=UTC),
    )

    assert selected.provider == "tianditu"
    assert selected.package_id == tianditu.package_id


def test_expired_package_is_rejected_without_internet_fallback(
    offline_basemap_package,
    tile_manifest,
) -> None:
    with pytest.raises(AllBasemapsUnavailableError):
        BasemapSelector().select(
            offline_basemap_package.expired(),
            offline_basemap_package.expired(),
            [tile_manifest],
            observed_at=datetime(2026, 9, 30, tzinfo=UTC),
        )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_basemap.py -v
```

Expected: FAIL because manifests, validators, and selectors do not exist.

- [ ] **Step 3: Implement provider-independent tile manifests**

Build the manifest from viewport, radius, output dimensions, padding, and zoom levels before selecting a provider. Include all zoom levels required by the five viewport variants, `2.5`, `5`, `10`, `30`, and `50` kilometers. Use `EPSG:3857` for tile indexing and `EPSG:4326` for the center.

Persist the resulting manifests in the static context as a sorted list of:

```json
{
  "viewport_radius_km": 10,
  "zoom_level": 12,
  "required_tiles": [[12, 3421, 1678], [12, 3422, 1678]],
  "buffer_pixels": 256,
  "manifest_checksum": "lowercase-sha256-hex"
}
```

- [ ] **Step 4: Implement exact package validation and selection**

Validation order is fixed:

```text
package manifest checksum
package file count and record count
coverage boundary
package max age
exact required (z, x, y) set
at least 200 deterministic sampled tile decodes
```

The 200-tile sample is an additional health check only. A package succeeds only if the exact tile set is complete and all health checks pass. `BasemapSelector.select` freezes `provider`, `package_id`, `version`, `checksum`, and `selection_reason` into the selected basemap object. It must not perform an HTTP request at any point.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_basemap.py tests/test_artifact_context.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/basemap.py backend/tests/test_artifact_basemap.py backend/app/artifacts/context.py backend/app/config.py infra/compose.yaml
git commit -m "feat: validate offline artifact basemaps"
```

---

### Task 7: MapLibre/Playwright 渲染链路和代表性图件

**Files:**
- Create: `backend/app/artifacts/renderers/__init__.py`
- Create: `backend/app/artifacts/renderers/base.py`
- Create: `backend/app/artifacts/renderers/map_renderer.py`
- Create: `backend/tests/test_artifact_map_renderer.py`
- Create: `backend/tests/fixtures/artifact_maps/epicenter.geojson`
- Create: `config/artifacts/map/base-style.json`

**Interfaces:**
- Consumes: `MapRenderContext`, selected offline basemap, local MapLibre assets, output profile。
- Produces `MapRenderSpec` and `RenderResult`。
- Produces:
  - `MapSpecBuilder.build(context) -> MapRenderSpec`
  - `MapRenderer.render(spec, output_path: Path) -> RenderResult`
  - `BrowserPool.start() -> None`
  - `BrowserPool.close() -> None`
- `RenderResult` contains `path`, `format`, `width`, `height`, `dpi`, `checksum`, `quality`, `task_status`, `file_name`, `render_manifest`。

- [ ] **Step 1: Write the failing renderer tests**

Create `backend/tests/test_artifact_map_renderer.py`:

```python
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.artifacts.renderers.map_renderer import MapLayer, MapSpecBuilder, RemoteAssetForbiddenError


async def test_epicenter_map_renders_a3v_professional(
    seeded_artifact_assessment,
    map_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.map_context("map.epicenter")
    spec = MapSpecBuilder().build(context)
    result = await map_renderer.render(spec, tmp_path / "epicenter.jpg")

    assert spec.title == "震中位置分布图"
    assert spec.output.width == 4761
    assert spec.output.height == 3369
    assert result.width == 4761
    assert result.height == 3369
    assert result.dpi == 300
    assert result.non_empty_ratio > 0.2
    assert Image.open(result.path).format == "JPEG"


async def test_renderer_never_loads_remote_assets(
    map_renderer,
    seeded_artifact_assessment,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.map_context(
        "map.epicenter",
        base_style="gaode-local-v1",
    )
    spec = replace(
        MapSpecBuilder().build(context),
        layers=(MapLayer(url="https://example.invalid/tile.png"),),
    )
    with pytest.raises(RemoteAssetForbiddenError):
        await map_renderer.render(spec, tmp_path / "rejected.jpg")


async def test_cancel_closes_page_and_releases_browser_slot(
    browser_pool,
    seeded_artifact_assessment,
) -> None:
    spec = MapSpecBuilder().build(
        await seeded_artifact_assessment.map_context("map.epicenter")
    )
    token = browser_pool.reserve_slot(priority=100)
    await browser_pool.cancel(token)

    assert browser_pool.active_pages == 0
    assert browser_pool.available_slots == browser_pool.max_slots
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_map_renderer.py -v
```

Expected: FAIL because renderer, browser pool, map style, and fixture do not exist.

- [ ] **Step 3: Implement the declarative map specification**

`MapSpecBuilder` accepts only immutable context data. It must create this shape before rendering:

```python
MapRenderSpec(
    artifact_key=context.artifact_key,
    title=context.display_name,
    event=MapEvent(
        event_id=str(context.event.id),
        revision_id=str(context.revision.id),
        place=context.event.place,
        magnitude=float(context.event.magnitude),
        origin_time=context.event.origin_time,
        longitude=float(context.event.longitude),
        latitude=float(context.event.latitude),
        depth_km=float(context.event.depth_km),
    ),
    viewport=MapViewport(center=(float(context.event.longitude), float(context.event.latitude)), radius_km=50, padding=40),
    base_style=context.selected_basemap.provider_style_key,
    layers=context.layers,
    legend=context.legend,
    source_notes=context.source_notes,
    quality=context.quality,
    marker=context.marker,
    output=MapOutput(format="jpg", width=4761, height=3369, dpi=300),
)
```

Use the same normalized object for `context_fingerprint`, `input_fingerprint`, `render_manifest`, and validation. Reject any URL not starting with `local://` or a path under `ARTIFACT_STORAGE_ROOT`.

- [ ] **Step 4: Implement browser pool and screenshot validation**

Preload Chromium, MapLibre static assets, Chinese font glyphs, base style JSON, and both basemap package indexes at worker startup. For each render:

1. Reserve a browser slot with priority.
2. Create or reuse a page.
3. Load the local renderer HTML and local resources.
4. Wait for `map.loaded()`, `document.fonts.ready`, and a stable `requestAnimationFrame`.
5. Screenshot at `1587 x 1123` CSS pixels with `deviceScaleFactor=3`.
6. Verify dimensions, format, DPI, and non-empty ratio.
7. Emit `RenderResult` with a manifest containing the selected basemap package, layer versions, template version, output coordinates, and renderer version.

On cancellation, close the page, kill a hung browser context if needed, and release the slot in `finally`.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_map_renderer.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/renderers/__init__.py backend/app/artifacts/renderers/base.py backend/app/artifacts/renderers/map_renderer.py backend/tests/test_artifact_map_renderer.py backend/tests/fixtures/artifact_maps/epicenter.geojson config/artifacts/map/base-style.json
git commit -m "feat: render professional map artifacts"
```

---

### Task 8: 19 类 A 级专业图件

**Files:**
- Create: `backend/app/artifacts/renderers/map_layers.py`
- Create: `backend/tests/test_artifact_maps_a.py`
- Modify: `backend/app/artifacts/renderers/map_renderer.py`
- Modify: `config/artifacts/catalog.yaml`

**Interfaces:**
- Consumes: `ArtifactTaskContext`, `MapSpecBuilder`, published vector/raster assets, evaluation products。
- Produces `MapLayerRegistry.build(artifact_key, context) -> tuple[MapLayer, ...]`。
- Produces `MapQualityPolicy.evaluate(artifact_key, layers) -> ArtifactQuality`。

- [ ] **Step 1: Write failing tests for every A-class output**

Create `backend/tests/test_artifact_maps_a.py`:

```python
import pytest

A_CLASS_ARTIFACTS = (
    "map.intensity",
    "map.economic_loss",
    "map.rescue_demand",
    "map.deaths",
    "map.injuries",
    "map.buried",
    "map.material_demand",
    "map.gdp",
    "map.transport",
    "map.historical_earthquakes",
    "map.population",
    "map.hazard_sources",
    "map.schools",
    "map.hospitals",
    "map.active_faults",
    "map.building_damage",
    "map.key_targets",
    "map.epicenter",
    "map.city_distances",
)


@pytest.mark.parametrize("artifact_key", A_CLASS_ARTIFACTS)
async def test_a_class_artifacts_are_rendered_with_required_layers(
    artifact_key,
    seeded_artifact_assessment,
    map_renderer,
    tmp_path,
) -> None:
    context = await seeded_artifact_assessment.map_context(artifact_key)
    spec = MapSpecBuilder().build(context)
    result = await map_renderer.render(spec, tmp_path / f"{artifact_key}.jpg")

    assert result.width == 4761
    assert result.height == 3369
    assert result.quality.grade == "A"
    assert result.quality.needs_review is False
    assert result.render_manifest["base_provider"] in {"gaode", "tianditu"}


@pytest.mark.parametrize(
    "artifact_key,product_key",
    (
        ("map.intensity", "intensity.fusion"),
        ("map.economic_loss", "loss.economic"),
        ("map.rescue_demand", "loss.resources"),
        ("map.deaths", "loss.casualties"),
        ("map.injuries", "loss.casualties"),
        ("map.buried", "loss.casualties"),
        ("map.material_demand", "loss.resources"),
        ("map.building_damage", "loss.buildings"),
    ),
)
async def test_a_class_product_dependencies_are_persisted(
    artifact_key,
    product_key,
    seeded_artifact_assessment,
    session,
) -> None:
    task = await seeded_artifact_assessment.production_task(artifact_key)
    bindings = await seeded_artifact_assessment.dependency_bindings(task.id)

    binding = next(
        item
        for item in bindings
        if item.dependency_kind == "assessment_product"
        and item.dependency_key == product_key
    )
    assert binding.resolution_status == "bound"
    assert binding.bound_entity_id is not None
    assert binding.bound_checksum is not None
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_maps_a.py -v
```

Expected: FAIL because the layer registry and A-class rendering paths do not exist.

- [ ] **Step 3: Implement one declarative layer entry per artifact**

Implement these mappings in `map_layers.py`. Every layer has `layer_id`, `source_key`, `geometry_type`, `style_id`, `legend`, `minimum_zoom`, and `attribute_bindings`.

```text
map.intensity:
    intensity product raster and intensity vector boundary
map.economic_loss:
    loss.economic town geometry plus metric styling
map.rescue_demand:
    loss.resources town geometry plus resource demand styling
map.deaths, map.injuries, map.buried:
    loss.casualties town geometry plus corresponding metric styling
map.material_demand:
    loss.resources town geometry plus material demand styling
map.gdp:
    shanghai.gdp.raster
map.transport:
    shanghai.road.network plus selected offline basemap
map.historical_earthquakes:
    shanghai.historical.earthquakes plus radius filtering
map.population:
    shanghai.population.town
map.hazard_sources:
    shanghai.hazard_source
map.schools:
    shanghai.education.school
map.hospitals:
    shanghai.health.hospital
map.active_faults:
    shanghai.fault plus distance annotation
map.building_damage:
    loss.buildings and shanghai.building.town
map.key_targets:
    shanghai.key_target plus shanghai.lifeline
map.epicenter:
    event point, administrative boundaries, offline basemap
map.city_distances:
    shanghai.distance.reference_points and PostGIS distance output
```

The renderer must read product values from persisted product records and rasters, not recalculate loss metrics. Spatial distances may be computed live, but only from the snapshot-bound input coordinates.

- [ ] **Step 4: Add quality checks and source annotations**

For every A-class output, require:

```text
title and event summary visible
legend visible when a data layer has a scale
scale bar, north arrow, source line, generation time, version and quality marker visible
at least one non-empty map layer or explicit verified-empty statement
no placeholder text
no unexpected zero substitution
```

For `map.transport`, allow only `shanghai.road.network` to degrade because it is the explicitly declared optional enhancement. For `map.active_faults`, the valid empty result is displayed as `检索范围内无活动断裂记录`; this is different from an unavailable asset.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_maps_a.py tests/test_artifact_map_renderer.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/renderers/map_layers.py backend/tests/test_artifact_maps_a.py backend/app/artifacts/renderers/map_renderer.py config/artifacts/catalog.yaml
git commit -m "feat: render A-class map artifacts"
```

---

### Task 9: 7 类 B 级和 1 类 C 级图件的降级规则

**Files:**
- Modify: `backend/app/artifacts/renderers/map_layers.py`
- Create: `backend/tests/test_artifact_maps_degraded.py`
- Modify: `backend/app/artifacts/validation.py`
- Modify: `config/artifacts/catalog.yaml`

**Interfaces:**
- Consumes: Task 8 `MapLayerRegistry`, optional asset resolution, Task 4 validation。
- Produces `DegradeDecision` with `status`, `needs_review`, `missing_assets`, `reason`, `spatialized_estimate`。
- Produces `MapDegradePolicy.evaluate(artifact_key, layers, asset_resolutions) -> DegradeDecision`。

- [ ] **Step 1: Write failing degradation tests**

Create `backend/tests/test_artifact_maps_degraded.py`:

```python
import pytest

B_CLASS = (
    "map.shelter_emergency",
    "map.pga_zoning",
    "map.reservoirs",
    "map.metro",
    "map.seismic_stations",
    "map.rescue_teams",
    "map.cultural_relics",
)


@pytest.mark.parametrize("artifact_key", B_CLASS)
async def test_b_class_missing_optional_data_creates_degraded_file(
    artifact_key,
    seeded_artifact_assessment,
    map_renderer,
    tmp_path,
) -> None:
    context = await seeded_artifact_assessment.map_context_without_optional_assets(
        artifact_key
    )
    result = await map_renderer.render(
        MapSpecBuilder().build(context),
        tmp_path / f"{artifact_key}.jpg",
    )

    assert result.task_status == "degraded"
    assert result.quality.needs_review is True
    assert result.file_name.endswith(".jpg")
    assert result.render_manifest["missing_assets"]


async def test_c_class_grid_requires_traceable_spatialized_input(
    seeded_artifact_assessment,
    map_renderer,
    tmp_path,
) -> None:
    traceable = await seeded_artifact_assessment.map_context("map.building_grid")
    result = await map_renderer.render(
        MapSpecBuilder().build(traceable),
        tmp_path / "grid.jpg",
    )
    assert result.task_status == "degraded"
    assert result.quality.spatialized_estimate is True

    missing_model = await seeded_artifact_assessment.map_context_without_spatial_model(
        "map.building_grid"
    )
    with pytest.raises(RequiredDependencyMissingError):
        await map_renderer.render(missing_model, tmp_path / "failed.jpg")


async def test_b_class_never_fabricates_zero_values(
    seeded_artifact_assessment,
) -> None:
    context = await seeded_artifact_assessment.map_context_without_optional_assets(
        "map.hazard_sources"
    )
    with pytest.raises(NoZeroSubstitutionError):
        await MapSpecBuilder().build(context).with_zero_substitution()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_maps_degraded.py -v
```

Expected: FAIL because B、C 类降级策略尚未实现。

- [ ] **Step 3: Implement B-class degradation**

For B-class maps, require at least the event point and administrative boundary. An unavailable optional layer produces `degraded`, not `failed`, with a visible high-contrast “待复核” label and a missing data note. Apply this list exactly:

```text
map.shelter_emergency -> 疏散场地数据待复核
map.pga_zoning -> 区划数据待复核
map.reservoirs -> 水库数据待复核
map.metro -> 轨道交通数据待复核
map.seismic_stations -> 台站数据待复核
map.rescue_teams -> 救援队伍数据待复核
map.cultural_relics -> 文物数据待复核
```

If the event point or administrative boundary is unavailable, fail the task. Empty-but-successful queries display “检索范围内无记录” and do not set `needs_review`.

- [ ] **Step 4: Implement C-class grid behavior**

`map.building_grid` may render only when the context contains:

```text
shanghai.building.town
loss.buildings
a declared spatial allocation rule
input checksum for each allocation source
```

Set `spatialized_estimate=true`, add the visible note “模型分配，待复核”, and record the allocation rule in `render_manifest`. If any prerequisite is missing, raise `RequiredDependencyMissingError`; never render a zero grid.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_maps_degraded.py tests/test_artifact_maps_a.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/renderers/map_layers.py backend/tests/test_artifact_maps_degraded.py backend/app/artifacts/validation.py config/artifacts/catalog.yaml
git commit -m "feat: degrade incomplete map artifacts explicitly"
```

---

### Task 10: 统计图、DOCX 渲染器和 8 类背景文档

**Files:**
- Create: `backend/app/artifacts/renderers/chart_renderer.py`
- Create: `backend/app/artifacts/renderers/docx_renderer.py`
- Create: `backend/app/artifacts/template_builder.py`
- Create: `config/artifacts/templates/background-template.docx`
- Create: `backend/tests/test_artifact_docx.py`
- Modify: `backend/app/artifacts/context.py`
- Modify: `backend/app/artifacts/renderers/base.py`
- Modify: `config/artifacts/catalog.yaml`

**Interfaces:**
- Consumes: `DocumentRenderContext`, template package, chart data, referenced map artifacts。
- Produces `ChartRenderer.render(spec, output_path: Path) -> RenderResult`。
- Produces `DocxRenderer.render(spec, output_path: Path) -> RenderResult`。
- Produces dataclass `DocumentRenderSpec`。
- Produces `build_background_document_spec(context, artifact_key: str) -> DocumentRenderSpec` for D01-D08。
- Produces `build_background_templates(output_dir: Path) -> dict[str, Path]`。

- [ ] **Step 1: Write failing DOCX tests**

Create `backend/tests/test_artifact_docx.py`:

```python
from pathlib import Path

import pytest
from docx import Document

BACKGROUND_DOCS = (
    "doc.background",
    "doc.housing",
    "doc.economy",
    "doc.population",
    "doc.key_targets",
    "doc.spatial_distances",
    "doc.area_overview",
    "doc.historical_catalog",
)


@pytest.mark.parametrize("artifact_key", BACKGROUND_DOCS)
async def test_background_documents_are_valid_docx(
    artifact_key,
    seeded_artifact_assessment,
    docx_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(artifact_key)
    result = await docx_renderer.render(
        build_background_document_spec(context, artifact_key),
        tmp_path / f"{artifact_key}.docx",
    )
    document = Document(result.path)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)

    assert result.page_count >= 1
    assert "{{" not in text
    assert "事件名称" in text
    assert "数据来源" in text
    assert result.control_fields["template_version"] == "v1"


async def test_docx_contains_mode_marker_in_pixels_and_metadata(
    seeded_artifact_assessment,
    docx_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.background",
        production_mode="test",
    )
    result = await docx_renderer.render(
        build_background_document_spec(context, "doc.background"),
        tmp_path / "test.docx",
    )
    document = Document(result.path)

    assert "【测试】" in result.file_name
    assert "【测试】" in document.paragraphs[0].text
    assert result.render_manifest["marker"] == "【测试】"


async def test_docx_reuses_the_same_map_checksum_for_repeated_reference(
    seeded_artifact_assessment,
    docx_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("doc.area_overview")
    spec = build_background_document_spec(context, "doc.area_overview")
    result = await docx_renderer.render(spec, tmp_path / "overview.docx")

    assert result.render_manifest["image_checksums"]["map.epicenter"] == (
        context.artifacts["map.epicenter"].checksum
    )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_docx.py -v
```

Expected: FAIL because the chart renderer, DOCX renderer, template builder, and background documents do not exist.

- [ ] **Step 3: Build controlled templates from code**

Implement `build_background_templates` with python-docx only. Generate one `background-template.docx` containing:

```text
cover title placeholder {{title}}
event control table placeholders
section placeholders {{section_1_title}}, {{section_1_body}}
optional image placeholder {{section_1_image}}
footer placeholders {{file_name}}, {{page_number}}, {{version}}, {{generated_by}}
```

Use deterministic styles and page dimensions but do not use `python-docx` to add dynamic content that the renderer cannot reproduce. The template generation command is:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -c "from pathlib import Path; from app.artifacts.template_builder import build_background_templates; build_background_templates(Path('/config/artifacts/templates'))"
```

Commit the generated DOCX as the first controlled template version.

- [ ] **Step 4: Implement background document specs**

Build these document-specific fields exactly:

```text
doc.background: event, place, origin time, coordinate, depth, historical earthquakes, nearby faults, geographic notes
doc.housing: town totals, structure type, damage statistics, coverage and quality
doc.economy: GDP, industry structure, economic loss, source and scenario
doc.population: resident, floating, household, age structure and affected population
doc.key_targets: shelter, school, hospital, hazard source, rescue team, cultural relic and key target lists
doc.spatial_distances: city, county, town, major city, key target and fault distances
doc.area_overview: geography, administration, intensity, population, buildings, economy and key risks
doc.historical_catalog: radius, magnitude threshold, historical and disaster earthquakes, statistics
```

Every document must include the control fields from spec section 9.2. Use `T1: 不适用（人工/测试/演练）` when `t1_at` is `None` and never call `.isoformat()` on a null value.

For tables and charts, generate a local deterministic asset first, then embed the same path and checksum in every document that references it. Do not recalculate loss values during DOCX composition.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_docx.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/renderers/chart_renderer.py backend/app/artifacts/renderers/docx_renderer.py backend/app/artifacts/template_builder.py config/artifacts/templates/background-template.docx backend/tests/test_artifact_docx.py backend/app/artifacts/context.py backend/app/artifacts/renderers/base.py config/artifacts/catalog.yaml
git commit -m "feat: compose background document artifacts"
```

---

### Task 11: 4 类核心文档和辅助决策 PPTX

**Files:**
- Create: `backend/app/artifacts/renderers/pptx_renderer.py`
- Create: `config/artifacts/templates/decision-template.pptx`
- Create: `backend/tests/test_artifact_core_documents.py`
- Modify: `backend/app/artifacts/renderers/docx_renderer.py`
- Modify: `backend/app/artifacts/context.py`

**Interfaces:**
- Consumes: Task 10 `DocxRenderer`, background documents, maps, evaluation products and quality metadata。
- Produces `PptxRenderer.render(spec, output_path: Path) -> RenderResult`。
- Produces `build_core_document_spec(context, artifact_key: str) -> DocumentRenderSpec` for `doc.rapid_brief`, `doc.rapid_report`, `doc.decision_report` and `deck.decision_report`。

- [ ] **Step 1: Write failing core document tests**

Create `backend/tests/test_artifact_core_documents.py`:

```python
from pathlib import Path

import pytest
from docx import Document
from pptx import Presentation


async def test_rapid_brief_contains_t1_not_applicable_for_manual_event(
    seeded_artifact_assessment,
    docx_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.rapid_brief",
        t1_at=None,
    )
    result = await docx_renderer.render(
        build_core_document_spec(context, "doc.rapid_brief"),
        tmp_path / "brief.docx",
    )
    text = "\n".join(paragraph.text for paragraph in Document(result.path).paragraphs)

    assert "T1" in text
    assert "不适用（人工/测试/演练）" in text


async def test_rapid_report_omits_failed_optional_map_but_marks_degraded(
    seeded_artifact_assessment,
    docx_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.rapid_report",
        failed_optional_artifacts={"map.reservoirs"},
    )
    result = await docx_renderer.render(
        build_core_document_spec(context, "doc.rapid_report"),
        tmp_path / "report.docx",
    )
    text = "\n".join(paragraph.text for paragraph in Document(result.path).paragraphs)

    assert result.task_status == "degraded"
    assert "map.reservoirs" not in result.render_manifest["images"]
    assert "水库数据待复核" in text


async def test_pptx_replaces_all_placeholders_and_uses_fixed_pages(
    seeded_artifact_assessment,
    pptx_renderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("deck.decision_report")
    result = await pptx_renderer.render(
        build_core_document_spec(context, "deck.decision_report"),
        tmp_path / "decision.pptx",
    )
    presentation = Presentation(result.path)
    all_text = "\n".join(
        shape.text
        for slide in presentation.slides
        for shape in slide.shapes
        if hasattr(shape, "text")
    )

    assert len(presentation.slides) == context.pptx_slide_count
    assert "{{" not in all_text
    assert "辅助决策报告" in all_text
    assert result.render_manifest["marker"] in {"", "【测试】", "【演练】", "【测试回放】"}
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_core_documents.py -v
```

Expected: FAIL because core document specification and PPTX renderer do not exist.

- [ ] **Step 3: Implement the four core document specifications**

Use these exact hard dependencies:

```text
doc.rapid_brief:
    intensity.fusion, loss.buildings, loss.population, loss.casualties,
    loss.economic, loss.resources, loss.validate,
    map.intensity, map.deaths, map.injuries, map.population,
    map.building_damage, map.epicenter

doc.rapid_report:
    doc.background, doc.housing, doc.economy, doc.population,
    doc.key_targets, doc.spatial_distances, doc.area_overview,
    doc.historical_catalog, every A-class map

doc.decision_report:
    doc.rapid_brief, doc.rapid_report, map.intensity, map.economic_loss,
    map.rescue_demand, map.deaths, map.injuries, map.buried,
    map.material_demand, map.active_faults, map.key_targets,
    map.epicenter, map.city_distances

deck.decision_report:
    doc.decision_report
```

For every core document, include source/product versions, quality grade, degradation reasons, and a `needs_review` section. The PPTX uses a fixed slide count and a fixed set of slide layouts:

```text
1 cover
2 event facts
3 response recommendation
4 fused intensity
5 casualties and building damage
6 economic and resource demand
7 key targets and faults
8 spatial distance and conclusions
```

- [ ] **Step 4: Implement the PPTX template and validation**

Generate `decision-template.pptx` with eight slide layouts, placeholder names listed in `config/artifacts/catalog.yaml`, fixed page dimensions, no linked media, and no external chart references. `PptxRenderer` must:

1. Open the template.
2. Replace only declared placeholders.
3. Embed map images by absolute local path and checksum.
4. Validate that all declared slide layouts exist.
5. Fail if a hard dependency image is unavailable.
6. Return page count, template version, image checksums, and unresolved-placeholder count.

`LibreOffice` may be used for an optional compatibility check, but it must not be a runtime dependency and the test must pass without it.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_core_documents.py tests/test_artifact_docx.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/renderers/pptx_renderer.py config/artifacts/templates/decision-template.pptx backend/tests/test_artifact_core_documents.py backend/app/artifacts/renderers/docx_renderer.py backend/app/artifacts/context.py
git commit -m "feat: compose core decision documents"
```

---

### Task 12: Temporal 生产编排、评估阶段信号和人工测试演练触发

**Files:**
- Create: `backend/app/artifacts/workflow.py`
- Create: `backend/app/artifacts/worker.py`
- Create: `backend/tests/test_artifact_workflow.py`
- Modify: `backend/app/assessment/temporal.py`
- Modify: `backend/app/assessment/worker.py`
- Modify: `backend/app/assessment/plan.py`
- Modify: `backend/app/assessment/repository.py`
- Modify: `backend/app/assessment/schemas.py`
- Modify: `backend/app/events/service.py`
- Modify: `backend/tests/test_assessment_plan.py`
- Modify: `backend/tests/test_assessment_repository.py`
- Modify: `backend/tests/test_assessment_temporal.py`

**Interfaces:**
- Consumes: Task 3 lifecycle, Task 5 context, Task 6 basemap selection, Tasks 7-11 renderers。
- Produces workflow dataclasses from spec section 7.5:
  - `ArtifactProductionWorkflowInput`
  - `ArtifactProductionWorkflowResult`
  - `ArtifactTaskActivityInput`
  - `ArtifactDependencyWaitInput`
  - `ArtifactValidationInput`
  - `ArtifactPublicationInput`
- Produces Activities:
  - `prepare_artifact_production`
  - `wait_for_artifact_dependencies`
  - `render_map_artifact`
  - `compose_docx_artifact`
  - `compose_pptx_artifact`
  - `validate_artifact_production`
  - `publish_artifact_production`
  - `mark_production_deadline_exceeded`
- Produces `AssessmentPrepared` fields: `production_run_id: str | None`, `artifact_workflow_input: ArtifactProductionWorkflowInput | None`。

- [ ] **Step 1: Write failing orchestration and event-trigger tests**

Add to `backend/tests/test_assessment_plan.py`:

```python
@pytest.mark.parametrize(
    "event_kind",
    (EventKind.FORMAL, EventKind.CORRECTION, EventKind.MANUAL, EventKind.TEST, EventKind.DRILL),
)
def test_plan_builder_accepts_all_production_event_kinds(event_kind) -> None:
    plan = AssessmentPlanBuilder().build(
        event_kind=event_kind,
        institutional_level=None,
        service_level=None,
    )
    assert "artifact.production" in {task.task_key for task in plan}
    assert "report.rapid_assessment" not in {task.task_key for task in plan}
```

Add to `backend/tests/test_assessment_repository.py`:

```python
async def test_manual_event_without_t1_creates_run_using_ingested_at(
    session,
    seeded_manual_event_without_t1,
) -> None:
    run = await AssessmentRepository().ensure_run_and_tasks(
        session,
        event=seeded_manual_event_without_t1.event,
        revision=seeded_manual_event_without_t1.revision,
        outbox=seeded_manual_event_without_t1.outbox,
    )

    assert run.t1_at is None
    assert run.deadline_basis_at == seeded_manual_event_without_t1.revision.ingested_at
    assert run.snapshot["t1_at"] is None
```

Add to `backend/tests/test_artifact_workflow.py`:

```python
async def test_assessment_child_is_abandoned_when_parent_finishes(
    temporal_env,
    seeded_assessment_workflow,
) -> None:
    handle = await temporal_env.client.start_workflow(
        AssessmentWorkflow.run,
        seeded_assessment_workflow.input,
        id=seeded_assessment_workflow.workflow_id,
        task_queue="assessment-test",
    )
    result = await handle.result()
    artifact_handle = temporal_env.client.get_workflow_handle(
        f"artifact-production:{result.production_run_id}"
    )

    await artifact_handle.signal(ArtifactProductionWorkflow.intensity_ready)
    assert await artifact_handle.query(ArtifactProductionWorkflow.status) in {
        "running",
        "completed",
        "partial",
    }


async def test_standalone_rebuild_waits_for_persisted_dependencies(
    temporal_env,
    seeded_artifact_assessment,
) -> None:
    run = await seeded_artifact_assessment.create_rebuild_run("map.intensity")
    result = await temporal_env.client.execute_workflow(
        ArtifactProductionWorkflow.run,
        seeded_artifact_assessment.standalone_input(run),
        id=f"artifact-production:{run.id}",
        task_queue="assessment-test",
    )

    assert result.production_run_id == str(run.id)
    assert result.status in {"completed", "partial", "failed"}
    assert result.required_outputs == (("map.intensity", "a3v-professional"),)
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_plan.py tests/test_assessment_repository.py tests/test_assessment_temporal.py tests/test_artifact_workflow.py -v
```

Expected: FAIL because assessment plans reject manual/test/drill, T1 is still required, and the artifact workflow does not exist.

- [ ] **Step 3: Make assessment preparation event-kind and T1 compatible**

In `AssessmentPlanBuilder.build`, accept `formal`, `correction`, `manual`, `test`, and `drill`; retain rejection for `auto`. Replace `report.rapid_assessment` with:

```python
PlannedAssessmentTask(
    task_key="artifact.production",
    task_type="artifact",
    component="artifact_production",
    priority=30,
    sequence=10,
    deadline_offset_seconds=240,
)
```

Keep `workgroup.response_tasks` at sequence 11 for the later coordination subsystem.

In `AssessmentRepository.ensure_run_and_tasks`:

```python
if canonical.current_revision_id != revision.id:
    raise ValueError("assessment trigger revision must be current")
basis_at = revision.ingested_at
if basis_at is None:
    raise ValueError("assessment revision ingested_at is required")
snapshot_t1 = canonical.t1_at.isoformat() if canonical.t1_at else None
```

Do not condition on `canonical.t1_at`. Store `t1_at=canonical.t1_at` and use `basis_at + timedelta(seconds=300)` for the assessment deadline. Update `AssessmentRunStatusResponse.t1_at` and its serializers to `datetime | None`.

Expand `EventService` assessment-outbox creation to `formal`, `correction`, `manual`, `test`, and `drill`, but only for a new, current revision. Keep automatic reports out of assessment. The outbox payload remains `event_id`, `revision_id`, and `revision_no`.

Reuse the existing assessment applicability resolver and boundary rules before enqueueing. Events outside the Shanghai service area, outside the 20-kilometer boundary buffer, and events that do not meet the configured magnitude/intensity threshold are still recorded and notified, but must not create an assessment or production run. Do not duplicate the geographic rule in the artifact subsystem.

- [ ] **Step 4: Implement `ArtifactProductionWorkflow`**

Use `ArtifactProductionWorkflowInput` exactly as specified in spec section 7.5. The workflow has these internal signals:

```python
@workflow.signal
async def intensity_ready(self) -> None: ...

@workflow.signal
async def loss_core_ready(self) -> None: ...

@workflow.signal
async def loss_final_ready(self) -> None: ...

@workflow.signal
async def assessment_failed(self) -> None: ...

@workflow.signal
async def cancel_requested(self, reason: str) -> None: ...
```

The child workflow behavior is:

```text
1. run prepare_artifact_production
2. schedule background tasks immediately
3. on intensity_ready, schedule map.intensity tasks
4. on loss_core_ready, schedule building/population maps and documents
5. on loss_final_ready, schedule casualty/economic/resource maps and documents
6. on assessment_failed, fail product-dependent tasks and finish background-only tasks
7. run validate_artifact_production and publish_artifact_production
8. return status, counts, required outputs and deadline evidence
```

In `AssessmentWorkflow.run`, after `prepare_assessment` returns, start the child with:

```python
child = await workflow.start_child_workflow(
    ArtifactProductionWorkflow.run,
    prepared.artifact_workflow_input,
    id=f"artifact-production:{prepared.production_run_id}",
    parent_close_policy=ParentClosePolicy.ABANDON,
)
```

Send `intensity_ready` after fusion completes, `loss_core_ready` after building and population complete, and `loss_final_ready` after resources and validation complete. Send `assessment_failed` before finalizing a failed assessment. Do not await the child before finishing the assessment. Mark the assessment task `artifact.production` succeeded only after the child workflow start call succeeds; the actual run status remains authoritative in `ProductionRun`.

- [ ] **Step 5: Implement worker registration and Activity idempotency**

Create `build_artifact_worker`, `run_artifact_worker`, and `run_artifact_dispatcher` in `backend/app/artifacts/worker.py`. Register `ArtifactProductionWorkflow` and all Activities in the existing assessment Temporal worker or a dedicated worker sharing the same task queue. Activity idempotency keys must be exactly:

```text
artifact:{production_run_id}:{artifact_key}:{output_profile}:{task_input_fingerprint}
```

`wait_for_artifact_dependencies` reads persisted bindings using a fixed backoff and never queries the parent workflow. Activity cancellation must call `browser_pool.cancel`, close temporary files, release slots, and only then update the task to `canceled`.

- [ ] **Step 6: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_assessment_plan.py tests/test_assessment_repository.py tests/test_assessment_temporal.py tests/test_artifact_workflow.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/workflow.py backend/app/artifacts/worker.py backend/tests/test_artifact_workflow.py backend/app/assessment/temporal.py backend/app/assessment/worker.py backend/app/assessment/plan.py backend/app/assessment/repository.py backend/app/assessment/schemas.py backend/app/events/service.py backend/tests/test_assessment_plan.py backend/tests/test_assessment_repository.py backend/tests/test_assessment_temporal.py
git commit -m "feat: orchestrate artifact production workflows"
```

---

### Task 13: 后端读取、重算、取消和超级管理员覆盖 API

**Files:**
- Create: `backend/app/artifacts/permissions.py`
- Create: `backend/app/artifacts/schemas.py`
- Create: `backend/app/artifacts/router.py`
- Create: `backend/tests/test_artifact_api.py`
- Create: `backend/tests/test_artifact_permissions.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/artifacts/service.py`

**Interfaces:**
- Consumes: `get_current_user`, `require_role`, Task 3 repository, Task 4 override service, Temporal starter。
- Produces read routes from spec section 13:
  - `GET /api/v1/assessments/runs/{assessment_run_id}/production`
  - `GET /api/v1/artifact-production-runs/{production_run_id}`
  - `GET /api/v1/events/{event_id}/artifacts`
  - `GET /api/v1/artifacts/{artifact_id}`
  - `GET /api/v1/artifacts/{artifact_id}/download`
  - `GET /api/v1/artifacts/{artifact_id}/thumbnail`
  - `GET /api/v1/events/{event_id}/artifact-versions`
- Produces write routes:
  - `POST /api/v1/events/{event_id}/artifacts/{artifact_key}/rebuild`
  - `POST /api/v1/events/{event_id}/artifacts/{artifact_key}/override`
- Produces `can_rebuild_artifacts(user: AuthUser) -> bool`。

- [ ] **Step 1: Write failing API and permission tests**

Create `backend/tests/test_artifact_permissions.py`:

```python
from app.artifacts.permissions import can_rebuild_artifacts
from app.auth.service import AuthUser


def test_superadmin_can_always_rebuild() -> None:
    user = AuthUser(username="admin", role="superadmin", workgroup=None)
    assert can_rebuild_artifacts(user) is True


def test_emergency_tech_members_can_rebuild() -> None:
    user = AuthUser(username="tech", role="group_member", workgroup="应急技术组")
    assert can_rebuild_artifacts(user) is True


def test_other_workgroup_cannot_rebuild() -> None:
    user = AuthUser(username="member", role="group_member", workgroup="综合协调组")
    assert can_rebuild_artifacts(user) is False
```

Create `backend/tests/test_artifact_api.py`:

```python
async def test_artifact_read_api_returns_thumbnail_and_download_urls(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    event = await seeded_artifact_assessment.publish_epicenter_artifact()
    response = await artifact_client.get(f"/api/v1/events/{event.event_id}/artifacts")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["artifact_key"] == "map.epicenter"
    assert body[0]["thumbnail_url"].endswith("/thumbnail")
    assert body[0]["download_url"].endswith("/download")


async def test_rebuild_returns_202_and_does_not_change_full_progress(
    artifact_client,
    seeded_artifact_assessment,
) -> None:
    event = await seeded_artifact_assessment.publish_full_progress()
    response = await artifact_client.post(
        f"/api/v1/events/{event.event_id}/artifacts/map.epicenter/rebuild"
        "?output_profile=a3v-professional",
        json={"reason": "底图版本更新后重新生成"},
    )

    assert response.status_code == 202
    assert response.json()["generation_scope"] == (
        "artifact:map.epicenter:a3v-professional"
    )
    assert await seeded_artifact_assessment.full_progress(event.event_id) == "39/39"


async def test_override_requires_superadmin(
    artifact_client_as_group_member,
    seeded_artifact_assessment,
) -> None:
    response = await artifact_client_as_group_member.post(
        f"/api/v1/events/{seeded_artifact_assessment.event.id}/artifacts/map.epicenter/override",
        files={"file": ("map.jpg", b"not-a-jpeg", "image/jpeg")},
        data={
            "revision_id": str(seeded_artifact_assessment.revision.id),
            "reason": "test",
        },
        headers={"Idempotency-Key": "00000000-0000-0000-0000-000000000003"},
    )

    assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_permissions.py tests/test_artifact_api.py -v
```

Expected: FAIL because the artifacts API does not exist.

- [ ] **Step 3: Implement permissions and response schemas**

Implement the shared permission rule:

```python
EMERGENCY_TECH_WORKGROUP = "应急技术组"
REBUILD_ROLES = {"group_leader", "group_deputy", "group_member"}


def can_rebuild_artifacts(user: AuthUser) -> bool:
    if user.role == "superadmin":
        return True
    return (
        user.workgroup == EMERGENCY_TECH_WORKGROUP
        and user.role in REBUILD_ROLES
    )
```

Read roles are `superadmin`, `group_leader`, `group_deputy`, `group_member`, `viewer`. Override is `superadmin` only.

Response schemas include:

```python
class ProductionRunResponse(BaseModel):
    production_run_id: UUID
    assessment_run_id: UUID
    status: str
    production_mode: str
    launch_mode: str
    generation_seq: int
    generation_scope: str
    deadline_basis_at: datetime
    deadline_at: datetime
    required_output_count: int
    complete_count: int
    degraded_count: int
    failed_count: int
    timeout_count: int
    needs_review_count: int
    is_current: bool
    artifacts: list[ArtifactSummaryResponse]
    context_fingerprint: str
    catalog_version: str
    template_versions: dict[str, str]
    data_asset_versions: dict[str, str]
    renderer_versions: dict[str, str]
    marker: str | None

class ArtifactSummaryResponse(BaseModel):
    artifact_id: UUID
    artifact_key: str
    output_profile: str
    display_name: str
    artifact_version: int
    status: str
    quality_grade: str
    needs_review: bool
    production_mode: str
    publication_mode: str
    file_name: str
    format: str
    size_bytes: int
    generated_at: datetime
    download_url: str
    thumbnail_url: str | None

class ArtifactRebuildRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
```

- [ ] **Step 4: Implement routes and binary responses**

Use `FileResponse` for download with `media_type` from the artifact format and a safe `Content-Disposition` filename. Use `FileResponse` or a generated image response for thumbnails. Never expose the managed path or arbitrary filesystem path.

`rebuild` behavior:

1. Resolve the current event revision and catalog default `output_profile`.
2. Call `can_rebuild_artifacts` before touching data.
3. Create one standalone run with `generation_scope=artifact:<key>:<profile>`.
4. Start `ArtifactProductionWorkflow` with `artifact-production:{production_run_id}`.
5. Return `202` with production run ID, deadline, scope, and status.

Persist the operator, timestamp, artifact key, output profile, old current artifact ID, new run ID, and reason in `ProductionRun.snapshot` before returning `202`. This is the immutable rebuild audit record; do not require a separate generic audit table.

`override` behavior:

1. Require `superadmin`.
2. Validate `Idempotency-Key` as a UUID header.
3. Call the Task 4 override service with the upload, revision, reason, and expected current artifact ID.
4. Return `201` for a new override or the stored response for an identical retry.
5. Map `IdempotencyConflictError` to `409`, invalid file to `422`, and storage failures to `503`.

Register the router in `backend/app/main.py` and ensure its `GET` endpoints are covered by the existing CORS and dependency injection setup.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_permissions.py tests/test_artifact_api.py tests/test_auth.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

Expected: PASS.

```bash
git add backend/app/artifacts/permissions.py backend/app/artifacts/schemas.py backend/app/artifacts/router.py backend/tests/test_artifact_api.py backend/tests/test_artifact_permissions.py backend/app/main.py backend/app/artifacts/service.py
git commit -m "feat: expose artifact production APIs"
```

---

### Task 14: 前端成果中心和鉴权下载

**Files:**
- Create: `frontend/src/pages/ArtifactCenterPage.tsx`
- Create: `frontend/src/components/ArtifactProgressCard.tsx`
- Create: `frontend/src/components/ArtifactList.tsx`
- Create: `frontend/src/components/ArtifactPreview.tsx`
- Create: `frontend/src/components/ArtifactOverrideDialog.tsx`
- Create: `frontend/tests/artifact-center.test.tsx`
- Create: `frontend/tests/artifact-progress.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/EventDetailPage.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: Task 13 API routes and `CurrentUser.workgroup`。
- Produces React types `ProductionRun`, `ArtifactSummary`, `ProductionFilters`, `ArtifactPreviewState`。
- Produces client functions:
  - `getAssessmentProduction(assessmentRunId: string): Promise<ProductionRun>`
  - `getArtifactProductionRun(productionRunId: string): Promise<ProductionRun>`
  - `listEventArtifacts(eventId: string, filters: ProductionFilters): Promise<ArtifactSummary[]>`
  - `getArtifactVersions(eventId: string): Promise<ArtifactSummary[]>`
  - `rebuildArtifact(eventId: string, artifactKey: string, outputProfile: string, reason: string): Promise<ProductionRun>`
  - `overrideArtifact(eventId: string, artifactKey: string, input: ArtifactOverrideInput): Promise<ArtifactSummary>`
  - `fetchArtifactBlob(artifactId: string, kind: "download" | "thumbnail"): Promise<Blob>`

- [ ] **Step 1: Write failing frontend tests**

Create `frontend/tests/artifact-center.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ArtifactCenterPage } from "../src/pages/ArtifactCenterPage";
import { setAccessToken } from "../src/api/client";

test("lists grouped artifacts and downloads with an authenticated blob", async () => {
  setAccessToken("test-token");
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      jsonResponse({
        production_run_id: "run-1",
        status: "partial",
        required_output_count: 39,
        complete_count: 37,
        degraded_count: 1,
        failed_count: 1,
        timeout_count: 0,
        needs_review_count: 1,
        artifacts: [
          {
            artifact_id: "artifact-1",
            artifact_key: "map.epicenter",
            display_name: "震中位置分布图",
            status: "complete",
            quality_grade: "A",
            needs_review: false,
            artifact_version: 1,
            file_name: "浦东新区_5.1级地震_震中位置分布图_V001_20260930-153000.jpg",
            download_url: "/api/v1/artifacts/artifact-1/download",
            thumbnail_url: "/api/v1/artifacts/artifact-1/thumbnail",
          },
        ],
      }),
    )
    .mockResolvedValueOnce(new Response(new Blob(["image"]), { status: 200 }));

  render(<ArtifactCenterPage eventId="event-1" userRole="group_member" workgroup="应急技术组" />);
  expect(await screen.findByText("震中位置分布图")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下载原文件" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({
    Authorization: "Bearer test-token",
  });
});

test("shows override only to superadmin", () => {
  const { rerender } = render(
    <ArtifactCenterPage eventId="event-1" userRole="group_member" workgroup="应急技术组" />,
  );
  expect(screen.queryByRole("button", { name: "强制覆盖" })).not.toBeInTheDocument();

  rerender(<ArtifactCenterPage eventId="event-1" userRole="superadmin" workgroup={null} />);
  expect(screen.getByRole("button", { name: "强制覆盖" })).toBeInTheDocument();
});
```

Create `frontend/tests/artifact-progress.test.tsx`:

```tsx
test("progress card uses latest full run and shows grouped counts", () => {
  render(
    <ArtifactProgressCard
      production={{
        productionRunId: "run-1",
        status: "running",
        completeCount: 37,
        degradedCount: 1,
        failedCount: 1,
        timeoutCount: 0,
        needsReviewCount: 1,
        requiredOutputCount: 39,
        mapCount: 26,
        backgroundDocumentCount: 8,
        coreDocumentCount: 3,
        progressText: "37/39",
      }}
    />,
  );

  expect(screen.getByText("37/39")).toBeInTheDocument();
  expect(screen.getByText("图件")).toBeInTheDocument();
  expect(screen.getByText("背景文档")).toBeInTheDocument();
  expect(screen.getByText("核心文档")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test -- artifact-center.test.tsx artifact-progress.test.tsx
```

Expected: FAIL because the artifact center, progress card, and client functions do not exist.

- [ ] **Step 3: Store current user workgroup and add navigation**

Update `App.tsx` to store both `userRole` and `userWorkgroup` from `getCurrentUser()`. Pass them to `ConsoleShell` and relevant pages. Add the first-level navigation item `成果中心` routing to `/artifacts`; the page shows an event selector when no event is selected and uses `/artifacts/:eventId` when opened from the event detail. Add a production progress card to `EventDetailPage` that loads the latest full run and links to the scoped artifact center. Do not use `@tanstack/react-query`; keep the existing `useState/useEffect/useCallback` pattern.

Change `AssessmentRunStatus.t1_at` in `frontend/src/types.ts` to `string | null`. In assessment and artifact views, render a null T1 as `不适用（人工/测试/演练）`; do not reuse the generic `-` placeholder for this case.

- [ ] **Step 4: Implement authenticated Blob preview and download**

Extend `frontend/src/api/client.ts` with:

```typescript
export async function fetchArtifactBlob(
  artifactId: string,
  kind: "download" | "thumbnail",
): Promise<Blob> {
  const response = await fetchWithTimeout(
    `/api/v1/artifacts/${encodeURIComponent(artifactId)}/${kind}`,
    {
      headers: authenticatedHeaders("请先登录后查看成果"),
    },
    null,
  );
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  return response.blob();
}
```

In `ArtifactPreview`, create an object URL for thumbnails and revoke it on unmount or artifact change. DOCX and PPTX use the server thumbnail and show a download button; do not preview Office ZIP contents in the browser. Failed artifacts show the error summary and no fake thumbnail.

- [ ] **Step 5: Implement the artifact center interactions**

The page must:

```text
show live/test/drill/replay status without an off switch
filter by kind, status, quality, and publication mode
show three groups: map, background document, core document/deck
show current 39-item progress from the latest generation_scope=full run
show artifact versions separately from current publications
show needs_review in list and preview
show rebuild only for canRebuild(user)
show override upload only for superadmin
reason is required for rebuild and override
```

`ArtifactOverrideDialog` sends `file`, `revision_id`, `reason`, optional `expected_current_artifact_id`, and a generated UUID `Idempotency-Key`. It must not send arbitrary JSON for the file.

- [ ] **Step 6: Run tests and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test -- artifact-center.test.tsx artifact-progress.test.tsx event-detail.test.tsx app-role-resolution.test.tsx
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
```

Expected: PASS.

```bash
git add frontend/src/pages/ArtifactCenterPage.tsx frontend/src/components/ArtifactProgressCard.tsx frontend/src/components/ArtifactList.tsx frontend/src/components/ArtifactPreview.tsx frontend/src/components/ArtifactOverrideDialog.tsx frontend/tests/artifact-center.test.tsx frontend/tests/artifact-progress.test.tsx frontend/src/App.tsx frontend/src/types.ts frontend/src/api/client.ts frontend/src/pages/EventDetailPage.tsx frontend/src/styles.css
git commit -m "feat: add artifact production console"
```

---

### Task 15: 端到端验收、性能门槛、保留策略和运维手册

**Files:**
- Create: `frontend/e2e/artifact-center.spec.ts`
- Create: `backend/tests/test_artifact_performance.py`
- Create: `backend/app/artifacts/retention.py`
- Create: `backend/tests/test_artifact_retention.py`
- Create: `docs/operations/artifact-production-runbook.md`
- Modify: `backend/tests/test_migrations.py`
- Modify: `infra/compose.yaml`
- Modify: `backend/Dockerfile`

**Interfaces:**
- Consumes: all previous tasks and the Z440-local PostgreSQL/PostGIS, Temporal, offline basemap packages, template packages, and synthetic test event fixtures。
- Produces an executable acceptance command and a documented operational recovery procedure。

- [ ] **Step 1: Write the failing end-to-end and performance tests**

Create `backend/tests/test_artifact_performance.py`:

```python
import pytest


@pytest.mark.performance
async def test_test_event_completes_39_outputs_within_five_minutes(
    artifact_acceptance_environment,
) -> None:
    result = await artifact_acceptance_environment.run_test_event(
        magnitude=5.1,
        production_mode="test",
    )

    assert result.required_output_count == 39
    assert result.complete_count + result.degraded_count == 39
    assert result.failed_count == 0
    assert result.timeout_count == 0
    assert result.elapsed_seconds <= 300
    assert all("【测试】" in artifact.file_name for artifact in result.artifacts)
    assert len({artifact.context_fingerprint for artifact in result.artifacts}) == 1


@pytest.mark.performance
async def test_three_preheated_runs_take_worst_case(
    artifact_acceptance_environment,
) -> None:
    results = [
        await artifact_acceptance_environment.run_test_event(
            magnitude=5.1,
            production_mode="test",
        )
        for _ in range(3)
    ]

    assert max(result.elapsed_seconds for result in results) <= 300


@pytest.mark.performance
async def test_formal_replay_uses_t1_as_deadline_basis(
    artifact_acceptance_environment,
) -> None:
    result = await artifact_acceptance_environment.run_replay_event(
        t1_at="2026-09-30T07:00:00+00:00",
        production_mode="replay",
    )

    assert result.deadline_basis_at == "2026-09-30T07:00:00+00:00"
    assert result.deadline_at == "2026-09-30T07:05:00+00:00"
    assert all("【测试回放】" in artifact.file_name for artifact in result.artifacts)
```

Create `backend/tests/test_artifact_retention.py`:

```python
async def test_retention_removes_expired_test_and_drill_but_not_live(
    seeded_artifact_assessment,
    artifact_retention_service,
    session,
) -> None:
    test_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="test",
        age_days=401,
    )
    drill_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="drill",
        age_days=731,
    )
    live_artifact = await seeded_artifact_assessment.expired_artifact(
        production_mode="live",
        age_days=10_000,
    )

    removed = await artifact_retention_service.retain_expired(
        session,
        observed_at=seeded_artifact_assessment.now,
    )

    assert removed.test_deleted == 1
    assert removed.drill_deleted == 1
    assert removed.live_deleted == 0
    assert await session.get(type(test_artifact), test_artifact.id) is None
    assert await session.get(type(drill_artifact), drill_artifact.id) is None
    assert await session.get(type(live_artifact), live_artifact.id) is not None
```

Create `frontend/e2e/artifact-center.spec.ts`:

```ts
import { expect, test } from "@playwright/test";

test("artifact center opens a completed test event and downloads the original file", async ({ page }) => {
  await page.goto("/events/test-artifact-event");
  await page.getByRole("link", { name: "成果中心" }).click();
  await expect(page.getByText("39/39")).toBeVisible();
  await expect(page.getByText("【测试】")).toBeVisible();
  await page.getByRole("button", { name: "下载原文件" }).first().click();
  await expect(page.getByText("下载已开始")).toBeVisible();
});
```

- [ ] **Step 2: Run the tests to verify failure**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_performance.py -v -m performance
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run test:e2e -- artifact-center.spec.ts
```

Expected: FAIL until all production components, fixtures, and worker services are wired end to end.

- [ ] **Step 3: Add the acceptance fixture and runtime containers**

Create a deterministic fixture that seeds:

```text
one test event
one formal test revision with ingested_at as deadline_basis_at
one assessment run with all nine assessment products
published administrative, population, building, economic, historical, fault, facility, and basemap assets
v1 map and document templates
```

Add `artifact-worker` and `artifact-dispatcher` services to `infra/compose.yaml` with the artifact storage volume, template/basemap/font mounts, and health checks. Add Chromium, fonts, MapLibre static files, and required native libraries to `backend/Dockerfile`. Keep the render concurrency configurable and default to 4.

Implement `ArtifactRetentionService.retain_expired` so:

```text
test artifacts are removable after 400 days
drill artifacts are removable after 2 years
live and manual artifacts are never removed by this job
referenced template or publication rows are never silently deleted
```

Run the retention job once per day from the artifact worker and record counts by production mode. A failed object deletion leaves the database record intact for the next retry.

The acceptance command must run against PostgreSQL/PostGIS and Temporal, not mocks:

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal artifact-worker artifact-dispatcher
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest tests/test_artifact_performance.py -v -m performance
```

- [ ] **Step 4: Implement the runbook and recovery checks**

Write `docs/operations/artifact-production-runbook.md` with exact procedures for:

```text
checking a timed-out production run
identifying a missing tile package
rebuilding one artifact
rebuilding a full run
recovering an expired override lease
checking old publication remains current after a failed rebuild
clearing unreferenced temporary objects
rotating test and drill artifacts
verifying storage checksum and disk capacity
```

Include SQL queries that inspect `artifact_production_runs`, `artifact_production_tasks`, `generated_artifacts`, and `artifact_publications`. Do not include credentials or connection strings.

- [ ] **Step 5: Run the complete acceptance suite and commit**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run test:e2e
```

Expected: PASS. The performance test must show a worst-case result of no more than 300 seconds after three preheated runs.

```bash
git add frontend/e2e/artifact-center.spec.ts backend/tests/test_artifact_performance.py backend/app/artifacts/retention.py backend/tests/test_artifact_retention.py docs/operations/artifact-production-runbook.md backend/tests/test_migrations.py infra/compose.yaml backend/Dockerfile
git commit -m "test: verify artifact production acceptance"
```

---

## Spec Coverage Check

| 规格区域 | 实施任务 | 覆盖结果 |
| --- | --- | --- |
| 39 项专业成果目录、编号、命名 | Task 2、8、9、10、11 | 固定键、A/B/C 规则和文件命名均有测试 |
| 生产运行、任务、依赖和状态机 | Task 1、3、12 | 数据模型、生命周期、Temporal 分支和超时均有覆盖 |
| `T1` 可空、人工/测试/演练触发 | Task 1、12 | 迁移、仓储、Schema、评估计划和事件 Outbox 均有覆盖 |
| 数据资产、模型、模板和上下文快照 | Task 1、5 | 独立快照表和稳定指纹 |
| 离线高德/天地图主备选择 | Task 6、7 | 精确瓦片集校验、缓存冻结、禁止在线回退 |
| 27 类图件 | Task 7、8、9 | 代表性渲染、19 A、7 B、1 C |
| 8 类背景文档 | Task 10 | 模板生成、控制信息、图片复用 |
| 4 类核心文档和 PPTX | Task 11 | 硬依赖、降级、固定版式、控制信息 |
| Temporal 子工作流、信号和独立重算 | Task 12 | `assessment_child`、`standalone`、Activity 幂等和取消 |
| API、权限、重算、覆盖 | Task 13 | 读取、下载、缩略图、重算和原子覆盖 |
| 前端成果中心 | Task 14 | 一级导航、进度、筛选、预览、下载和权限门控 |
| 5 分钟验收、性能、保留和运维 | Task 15 | 39 项端到端、三次预热、回放边界、保留策略、E2E 和运维手册 |
| 历史版本和超级管理员覆盖保留 | Task 3、4、13 | 新版本、发布指针替换、历史记录不删除 |

## Placeholder Scan

执行前必须运行以下命令，并确保输出为空：

```powershell
rg -n "TBD|TODO|FIXME|implement later|待补充|稍后补充" docs/superpowers/plans/2026-09-30-map-report-production-implementation-plan.md |
  Where-Object { $_ -notmatch "rg -n" }
```

计划中没有未指定接口、表名、路径、测试命令或提交命令。实现者不得用“按需处理”“类似上一任务”“补充测试”替代任务中的具体步骤。

## Type and Naming Consistency

计划统一使用以下机器标识，不在实现中创建同义别名：

```text
artifact_key
output_profile
production_run_id
production_task_id
input_fingerprint
context_fingerprint
generation_scope
generation_seq
launch_mode
deadline_basis_at
deadline_at
published_at
superseded_at
```

依赖类型只允许 `assessment_product` 和 `artifact`。生产模式只允许 `live`、`manual`、`test`、`drill`、`replay`。生产运行状态只允许 `pending`、`running`、`completed`、`partial`、`failed`、`canceled`。生产任务状态只允许 `pending`、`ready`、`running`、`succeeded`、`degraded`、`failed`、`timed_out`、`canceled`。

## Final Repository Checks

在提交计划前运行：

```powershell
git diff --check
Test-Path docs/superpowers/plans/2026-09-30-map-report-production-implementation-plan.md
rg -n "TBD|TODO|FIXME|implement later|待补充|稍后补充" docs/superpowers/plans/2026-09-30-map-report-production-implementation-plan.md |
  Where-Object { $_ -notmatch "rg -n" }
```

Expected: `git diff --check` returns no output, `Test-Path` returns `True`, and the scan returns no matches.

**计划状态:** READY FOR EXECUTION
