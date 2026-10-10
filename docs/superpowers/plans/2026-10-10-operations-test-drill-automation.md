# 运维、测试与演练自动化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有事件、评估、成果和协同链路上交付每日随机测试、手工测试、演练事件、真实事件优先保护、400 天/两年保留调度和运维展板，形成可审计、可恢复、可配置的运维闭环。

**Architecture:** 新增 `app.operations` 模块保存运维设置、每日测试计划、事件运行记录和审计；新增独立 `operations-worker` 负责每日计划、冲突判定、协作数据保留和状态对账。测试和演练仍使用现有 `earthquake_events`、评估、成果和协同链路，不复制业务数据；真实正式报或人工正式事件通过统一优先级守卫压过待执行测试和演练。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async、PostgreSQL/PostGIS、Alembic、Temporal、React 19、TypeScript、Vitest、Playwright、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-09-17-shanghai-earthquake-emergency-decision-system-design.md`

**Execution order:** 执行顺序为 Task 1、Task 2、Task 5、Task 3、Task 4、Task 6、Task 7、Task 8、Task 9。
Task 5 提前，是因为 Task 3 的触发服务必须注入 Task 5 产出的真实事件优先级守卫；任务编号保持不变，避免追踪标识漂移。

## Global Constraints

- 每日测试在上海时区 `02:00` 至 `04:00` 内随机选择一次执行时间，默认使用系统时区 `Asia/Shanghai`。
- 每日测试震级范围为 `5.0` 至 `5.9`，震源深度范围为 `5.00` 至 `20.00` 千米，震中必须落在当前激活的上海市行政边界内。
- 测试事件和成果保留 `400` 天；演练事件和成果保留 `2` 年；正式和人工事件长期保留。
- 保留清理删除测试或演练事件时，关联的运维计划通过 `ON DELETE CASCADE` 清理；运维运行记录同样级联清理，不保留指向已删除事件的计划。
- 所有测试成果的文件名、可见文字和元数据必须带不可移除的“测试”标识；演练成果必须带不可移除的“演练”标识。
- 真实正式报或人工正式事件优先。真实事件激活后，待执行测试继续延后，运行中的自动成果按现有 `real_event_priority` 逻辑终止；演练冲突策略可配置为 `pause` 或 `terminate`。
- 配置为 `pause` 时，尚未执行的演练保持暂停状态；已经进入成果工作流的演练按现有安全边界终止并记录 `resume_required=true`，不做不可靠的 Temporal 中途挂起。
- CENC 未发布但上海地震台网正式测定的地震继续使用现有手工正式事件入口，不伪造 CENC 报文。
- 每日计划必须幂等：同一上海本地日期最多创建一个计划、最多触发一个测试事件。
- 调度器不得通过重复触发补偿失败；失败必须记录原因、保留计划并告警。
- 测试、演练和正式事件继续使用现有评估与成果生产模式，不新增第二套损失或制图模型。
- 不新增手机端、短信、企业微信真实凭据、邮件账号或电话网关；现有通知适配接口继续作为后续渠道扩展点。
- 当前 Alembic head 为 `0025_qa_audit_provenance`，本计划新增唯一迁移 `0026_operations_automation`。
- 不在提交、日志、错误响应或测试夹具中写入 `.env`、密码、Token、API Key、连接串或真实凭据。
- 前端验收视口固定包含 `1920x1080`；新增页面必须满足无横向溢出、无关键文本遮挡和非空状态。

---

## File Structure

### 后端运维模块

- `backend/app/operations/__init__.py`：模块入口。
- `backend/app/operations/domain.py`：运维枚举、不可变场景、异常和结果类型。
- `backend/app/operations/models.py`：设置、每日计划、运行记录和审计表。
- `backend/app/operations/schemas.py`：运维 API 请求和响应。
- `backend/app/operations/scenario.py`：上海行政区内测试场景生成。
- `backend/app/operations/repository.py`：设置、计划、运行和审计持久化。
- `backend/app/operations/priority.py`：真实事件守卫、测试/演练冲突处理。
- `backend/app/operations/service.py`：手工触发、计划触发、状态对账和业务编排。
- `backend/app/operations/scheduler.py`：每日计划生成、领取、延后和触发。
- `backend/app/operations/worker.py`：独立运维工作进程。
- `backend/app/operations/health.py`：运维工作进程健康状态。
- `backend/app/operations/router.py`：运维状态、设置和触发 API。

### 现有模块接入

- `backend/app/events/service.py`：真实事件当前修订入库后通知运维优先级守卫。
- `backend/app/collaboration/service.py`：暂停或取消的运维运行禁止继续提交任务成果。
- `backend/app/collaboration/scheduler.py`：暂停或取消的测试/演练不再产生临期或超期提醒。
- `backend/app/config.py`：运维工作进程和默认计划参数。
- `backend/app/main.py`：注册运维 API。
- `backend/migrations/env.py`：注册运维模型。
- `backend/migrations/versions/0026_operations_automation.py`：运维表、约束、索引和升级/降级。
- `infra/compose.yaml`：新增 `operations-worker`。
- `.env.example`：新增非敏感配置键。

### 前端

- `frontend/src/pages/OperationsPage.tsx`：每日计划、运行状态、手工触发和设置。
- `frontend/src/api/client.ts`：运维 API 客户端。
- `frontend/src/types.ts`：运维类型和格式化函数。
- `frontend/src/App.tsx`：`/operations` 路由和导航。
- `frontend/src/styles.css`：运维页面响应式布局。
- `frontend/src/pages/OperationsPage.test.tsx`：页面行为测试。

### 验证和运行

- `backend/tests/test_operations_schema.py`：迁移与表结构。
- `backend/tests/test_operations_domain.py`：状态机、场景和幂等规则。
- `backend/tests/test_operations_repository.py`：设置、计划和运行持久化。
- `backend/tests/test_operations_service.py`：手工触发和真实事件冲突。
- `backend/tests/test_operations_scheduler.py`：每日随机计划与恢复。
- `backend/tests/test_operations_priority.py`：真实事件优先和任务冻结。
- `backend/tests/test_operations_api.py`：权限、幂等和错误契约。
- `backend/tests/test_operations_worker.py`：工作循环和保留调度。
- `frontend/e2e/operations.spec.ts`：真实后端运维闭环。
- `docs/runbooks/operations-test-drill.md`：部署、操作、恢复和故障处理。
- `README.md`：运行和验收入口。

---

## Task 1: 运维 schema、配置与 `0026` 迁移

**Files:**
- Create: `backend/app/operations/__init__.py`
- Create: `backend/app/operations/domain.py`
- Create: `backend/app/operations/models.py`
- Create: `backend/migrations/versions/0026_operations_automation.py`
- Modify: `backend/app/config.py`
- Modify: `backend/migrations/env.py`
- Modify: `backend/tests/test_migrations.py`
- Create: `backend/tests/test_operations_schema.py`

**Interfaces:**
- Consumes: 现有 `Base`、PostgreSQL UUID/JSONB、`earthquake_events`、`users`。
- Produces:
  - `TriggerMode`: `scheduled_test | manual_test | drill | manual_event`
  - `OperationState`: `queued | running | paused | completed | canceled | failed`
  - `ScheduleState`: `scheduled | due | deferred | triggered | skipped | failed`
  - `OperationsSettings`、`OperationsTestSchedule`、`OperationsRun`、`OperationsAuditLog`
  - `Settings.operations_worker_enabled: bool`
  - `Settings.operations_worker_poll_seconds: float`
  - `Settings.operations_daily_window_start: str`
  - `Settings.operations_daily_window_end: str`
  - `Settings.operations_retention_interval_seconds: int`

- [ ] **Step 1: 写迁移头和表结构失败测试**

在 `backend/tests/test_migrations.py` 将常量改为：

```python
LATEST_REVISION = "0026_operations_automation"
PREVIOUS_OPERATIONS_REVISION = "0025_qa_audit_provenance"
```

在 `backend/tests/test_operations_schema.py` 写：

```python
from sqlalchemy import inspect

from app.db import engine


OPERATIONS_TABLES = {
    "operations_settings",
    "operations_test_schedules",
    "operations_runs",
    "operations_audit_logs",
}


async def test_operations_tables_exist() -> None:
    async with engine.connect() as connection:
        names = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
    assert OPERATIONS_TABLES <= names
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_migrations.py::test_migration_head_includes_assessment_orchestration tests/test_operations_schema.py -v
```

Expected: FAIL，迁移 head 仍为 `0025_qa_audit_provenance`，运维表不存在。

- [ ] **Step 3: 添加配置字段和领域类型**

在 `backend/app/config.py` 的 `Settings` 中添加：

```python
operations_worker_enabled: bool = False
operations_worker_poll_seconds: float = 30.0
operations_daily_window_start: str = "02:00"
operations_daily_window_end: str = "04:00"
operations_retention_interval_seconds: int = 86_400
```

在配置校验中要求轮询间隔和保留间隔为正数，`operations_retention_interval_seconds >= 60`，并验证窗口格式为 `HH:MM` 且开始早于结束。

在 `backend/app/operations/domain.py` 定义：

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum


class TriggerMode(StrEnum):
    SCHEDULED_TEST = "scheduled_test"
    MANUAL_TEST = "manual_test"
    DRILL = "drill"
    MANUAL_EVENT = "manual_event"


class OperationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


class ScheduleState(StrEnum):
    SCHEDULED = "scheduled"
    DUE = "due"
    DEFERRED = "deferred"
    TRIGGERED = "triggered"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OperationsScenario:
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    depth_km: Decimal
    magnitude: Decimal
    place: str
    source_event_id: str
    random_seed: int
```

- [ ] **Step 4: 添加 SQLAlchemy 模型**

`OperationsSettings` 固定单例 `settings_key="default"`，必须包含：

```text
id UUID PK
settings_key VARCHAR(32) UNIQUE
daily_test_enabled BOOLEAN NOT NULL DEFAULT true
timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai'
daily_window_start TIME NOT NULL
daily_window_end TIME NOT NULL
test_magnitude_min NUMERIC(4,1) NOT NULL DEFAULT 5.0
test_magnitude_max NUMERIC(4,1) NOT NULL DEFAULT 5.9
test_depth_min_km NUMERIC(6,2) NOT NULL DEFAULT 5.00
test_depth_max_km NUMERIC(6,2) NOT NULL DEFAULT 20.00
drill_conflict_policy VARCHAR(16) NOT NULL DEFAULT 'pause'
row_version INTEGER NOT NULL DEFAULT 1
updated_by VARCHAR(64) NULL
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
```

检查约束必须保证震级最小值和最大值位于 `[-2.0, 12.0]`、深度位于 `[0, 1000]`、最小值不大于最大值、窗口开始早于结束、演练策略只能为 `pause|terminate`。

`OperationsTestSchedule` 必须包含：

```text
id UUID PK
local_date DATE UNIQUE
window_start TIMESTAMPTZ
window_end TIMESTAMPTZ
scheduled_at TIMESTAMPTZ
state VARCHAR(16)
scenario JSONB NULL
random_seed BIGINT
event_id UUID NULL FK earthquake_events ON DELETE CASCADE
failure_reason TEXT NULL
triggered_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
```

检查约束必须限制状态值；`triggered` 状态要求 `event_id` 和 `triggered_at` 非空。事件删除采用
`ON DELETE CASCADE`，由数据库同时清理关联计划，避免 `SET NULL` 与 `triggered` 检查约束冲突；
该行为与测试/演练事件保留清理一致。

`OperationsRun` 必须包含：

```text
id UUID PK
event_id UUID UNIQUE FK earthquake_events ON DELETE CASCADE
schedule_id UUID NULL UNIQUE FK operations_test_schedules ON DELETE SET NULL
trigger_mode VARCHAR(32)
operation_state VARCHAR(16)
priority SMALLINT
idempotency_key VARCHAR(160) UNIQUE
triggered_by VARCHAR(64)
resume_required BOOLEAN NOT NULL DEFAULT false
detail JSONB NOT NULL DEFAULT '{}'
started_at TIMESTAMPTZ NULL
paused_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
canceled_at TIMESTAMPTZ NULL
created_at TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
```

`OperationsAuditLog` 包含 `action`、`actor`、`resource_type`、`resource_id`、`before_state`、`after_state`、`detail` 和 `created_at`。

- [ ] **Step 5: 创建 `0026` 迁移并注册模型**

迁移头部固定为：

```python
revision: str = "0026_operations_automation"
down_revision: str | None = "0025_qa_audit_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
```

`upgrade()` 创建四张表、检查约束和索引，并插入唯一默认设置行。`downgrade()` 按外键逆序删除四张表。迁移不得修改现有事件、评估、成果或协同表。

在 `backend/migrations/env.py` 添加：

```python
from app.operations import models as operations_models  # noqa: F401
```

- [ ] **Step 6: 运行迁移和结构测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_migrations.py tests/test_operations_schema.py -v
```

Expected: PASS；head 为 `0026_operations_automation`，四张表、检查约束和唯一索引存在。

- [ ] **Step 7: 提交**

```powershell
git add backend/app/config.py backend/app/operations backend/migrations/env.py backend/migrations/versions/0026_operations_automation.py backend/tests/test_migrations.py backend/tests/test_operations_schema.py
git commit -m "feat: add operations automation persistence"
```

---

## Task 2: 上海测试场景生成与计划设置

**Files:**
- Create: `backend/app/operations/scenario.py`
- Create: `backend/app/operations/repository.py`
- Create: `backend/tests/test_operations_domain.py`
- Create: `backend/tests/test_operations_repository.py`

**Interfaces:**
- Consumes: Task 1 模型、`RegionBoundary`、`RegionContextResolver`、`NormalizedEvent`、`EventKind.TEST`。
- Produces:
  - `ScenarioGenerator.generate_test(session, settings, *, observed_at, seed) -> OperationsScenario`
  - `ScenarioGenerator.scenario_to_event(scenario) -> NormalizedEvent`
  - `ScenarioGenerator.region_contains(session, scenario) -> bool`
  - `OperationsRepository.get_settings(session) -> OperationsSettings`
  - `OperationsRepository.update_settings(session, actor, expected_version, patch) -> OperationsSettings`
  - `OperationsRepository.get_schedule(session, local_date) -> OperationsTestSchedule | None`
  - `OperationsRepository.get_run_for_event(session, event_id) -> OperationsRun | None`

- [ ] **Step 1: 写场景生成失败测试**

新增测试：

```python
from datetime import UTC, datetime
from datetime import time
from decimal import Decimal

from geoalchemy2.elements import WKTElement

from app.events.domain import EventKind
from app.operations.models import OperationsSettings
from app.operations.scenario import ScenarioGenerator
from app.regions.models import RegionBoundary


async def test_generated_test_scenario_is_shanghai_5_to_6(
    session,
) -> None:
    session.add(
        RegionBoundary(
            version="operations-test-boundary-v1",
            name="上海市测试边界",
            local_buffer_km=Decimal("50"),
            geom=WKTElement(
                "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, "
                "121.0 31.8, 121.0 30.8)))",
                srid=4326,
            ),
            maritime_geom=WKTElement(
                "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, "
                "121.0 31.8, 121.0 30.8)))",
                srid=4326,
            ),
            source_uri="test://operations-boundary",
            checksum="a" * 64,
            is_active=True,
        )
    )
    settings_record = OperationsSettings(
        settings_key="default",
        daily_test_enabled=True,
        timezone="Asia/Shanghai",
        daily_window_start=time(2, 0),
        daily_window_end=time(4, 0),
        test_magnitude_min=Decimal("5.0"),
        test_magnitude_max=Decimal("5.9"),
        test_depth_min_km=Decimal("5.00"),
        test_depth_max_km=Decimal("20.00"),
        drill_conflict_policy="pause",
    )
    session.add(settings_record)
    await session.flush()

    generator = ScenarioGenerator()
    scenario = await generator.generate_test(
        session,
        settings_record,
        observed_at=datetime(2026, 10, 10, 2, tzinfo=UTC),
        seed=20261010,
    )
    event = generator.scenario_to_event(scenario)

    assert event.kind is EventKind.TEST
    assert Decimal("5.0") <= event.magnitude <= Decimal("5.9")
    assert Decimal("5.00") <= event.depth_km <= Decimal("20.00")
    assert scenario.place == "上海市（系统测试）"
    assert await generator.region_contains(session, scenario) is True
```

Expected: FAIL because `app.operations.scenario` does not exist.

- [ ] **Step 2: 实现确定性随机场景**

`generate_test()` 使用 `random.Random(seed)` 生成震级和深度，并用 PostGIS 查询当前激活边界：

```sql
SELECT
    ST_X(point) AS longitude,
    ST_Y(point) AS latitude
FROM (
    SELECT ST_PointOnSurface(geom) AS point
    FROM region_boundaries
    WHERE is_active
) AS active_boundary
```

震级按 `0.1` 步长，深度按 `0.01` 步长；地点固定为 `上海市（系统测试）`；事件 `source` 固定为
`operations`，`source_event_id` 为 `operations-test:{YYYYMMDD}:{seed}`，由 canonical source
组合为 `operations:operations-test:{YYYYMMDD}:{seed}`，不得把 `operations-test` 同时放进两段。
没有激活边界时抛出 `OperationsConfigurationError("active_shanghai_boundary_required")`，
不得回退到硬编码坐标。

实现 `region_contains()` 使用同一激活边界执行 `ST_Covers(geom, point)`，测试可注入查询结果。

- [ ] **Step 3: 写设置并发更新失败测试**

测试覆盖：

- 首次读取自动返回默认设置。
- `row_version` 不匹配抛出 `StaleOperationsSettings`。
- 修改窗口、震级和演练冲突策略后写审计记录。
- `daily_window_start >= daily_window_end`、`magnitude_min > magnitude_max`、非法策略被拒绝。
- 震级和深度必须是 `Decimal`，分别位于 `[-2.0, 12.0]` 与 `[0, 1000]`；更新前按数据库列精度量化，
  保证审计快照与最终持久化值一致。
- 同日 schedule 可读取且返回同一记录；不存在的日期返回 `None`。
- 按 event_id 可读取唯一 run；不存在的 event_id 返回 `None`。

Expected: FAIL，因为 repository 尚未实现。

- [ ] **Step 4: 实现设置和运行 repository**

更新设置必须在事务中锁定单例行，并执行：

```python
settings.row_version += 1
settings.updated_by = actor
settings.updated_at = observed_at
session.add(
    OperationsAuditLog(
        action="settings.updated",
        actor=actor,
        resource_type="operations_settings",
        resource_id=str(settings.id),
        before_state=before,
        after_state=after,
        detail={"expected_version": expected_version},
        created_at=observed_at,
    )
)
```

所有 repository 查询必须使用显式排序，避免依赖数据库无序返回。

- [ ] **Step 5: 运行聚焦测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_domain.py tests/test_operations_repository.py -v
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add backend/app/operations/scenario.py backend/app/operations/repository.py backend/tests/test_operations_domain.py backend/tests/test_operations_repository.py
git commit -m "feat: generate shanghai test scenarios"
```

---

## Task 3: 运维服务和手工触发 API

**Files:**
- Create: `backend/app/operations/service.py`
- Create: `backend/app/operations/schemas.py`
- Create: `backend/app/operations/router.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_operations_service.py`
- Create: `backend/tests/test_operations_api.py`

**Interfaces:**
- Consumes: Task 1/2、`EventService.ingest_collected()`、`RegionContextResolver`、`ResponseRuleEngine`、`get_current_user`、`require_role`。
- Produces:
  - `OperationsService.trigger_manual_test(request, actor) -> OperationsTriggerResult`
  - `OperationsService.trigger_drill(request, actor) -> OperationsTriggerResult`
  - `OperationsService.trigger_scheduled_test(schedule_id, actor) -> OperationsTriggerResult`
  - `OperationsService.list_schedules(limit) -> list[OperationsScheduleRecord]`
  - `OperationsService.list_runs(limit) -> list[OperationsRunRecord]`
  - `OperationsService.get_overview(observed_at) -> OperationsOverviewRecord`
  - `OperationsService.update_settings(request, actor, expected_version)`

- [ ] **Step 1: 写手工触发失败测试**

测试必须证明：

- 手工测试默认生成上海行政区内 `5.0-5.9` 级事件。
- 手工测试和演练调用现有 `EventService`，事件 `event_type` 分别为 `test|drill`。
- 同一 `Idempotency-Key` 重放返回同一 `event_id`、`run_id`，不创建第二个事件。
- 不同幂等键创建不同事件。
- 正式事件活动期间手工测试返回 `real_event_priority_active`。
- 真实事件活动期间演练按 `pause` 策略返回 `deferred` 且不创建事件；按 `terminate` 策略返回 `canceled` 且不创建事件。
- 事件创建成功但运行记录写入失败时，下一次相同幂等键可以补建运行记录。

Expected: FAIL，因为 service 和 API 不存在。

- [ ] **Step 2: 实现触发编排**

`OperationsService` 构造参数固定为：

```python
def __init__(
    self,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    *,
    event_service: EventService | None = None,
    repository: OperationsRepository | None = None,
    scenario_generator: ScenarioGenerator | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    ...
```

手工测试流程：

1. 在只读事务中读取设置并检查真实事件守卫。
2. 用 `request.idempotency_key` 派生稳定 `source_event_id`。
3. 生成场景并解析 `RegionContext`。
4. 调用 `EventService.ingest_collected()`，参数固定为 `provider="api"`、`lane="http"`、`trigger_reason="live"`。
5. 在数据库事务中 upsert `OperationsRun`。
6. 写 `operations.manual_test.triggered` 审计。

API 幂等键通过 `Idempotency-Key` 请求头传入，缺失时仍可触发一次，但服务端使用随机键并将返回值写入响应；只有显式幂等键保证重放。

- [ ] **Step 3: 实现 API 和权限**

路由固定为：

```text
GET  /api/v1/operations/overview
GET  /api/v1/operations/settings
PUT  /api/v1/operations/settings
POST /api/v1/operations/test-trigger
POST /api/v1/operations/drill-trigger
GET  /api/v1/operations/schedules
GET  /api/v1/operations/runs
```

权限：

- 查看：任意已登录用户。
- 手工测试、演练：`superadmin | group_leader | group_deputy`。
- 修改设置：`superadmin`。

错误映射：

```text
StaleOperationsSettings -> 409 settings_version_conflict
RealEventPriorityActive -> 409 real_event_priority_active
OperationsConfigurationError -> 503 operations_configuration_unavailable
LookupError -> 404
ValueError/TypeError -> 422
SQLAlchemyError -> 503 operations_storage_unavailable
```

- [ ] **Step 4: 运行服务和 API 测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_service.py tests/test_operations_api.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/operations backend/app/main.py backend/tests/test_operations_service.py backend/tests/test_operations_api.py
git commit -m "feat: add operations trigger api"
```

---

## Task 4: 每日随机测试调度

**Files:**
- Create: `backend/app/operations/scheduler.py`
- Create: `backend/tests/test_operations_scheduler.py`

**Interfaces:**
- Consumes: Task 1/2/3、`ZoneInfo("Asia/Shanghai")`、`OperationsService.trigger_scheduled_test()`。
- Produces:
  - `DailyTestScheduler.ensure_schedule(session, observed_at) -> OperationsTestSchedule`
  - `DailyTestScheduler.dispatch_due(observed_at, trigger) -> OperationsSchedulerResult`
  - `OperationsSchedulerResult(created, due, deferred, triggered, failed)`

- [ ] **Step 1: 写调度失败测试**

测试覆盖：

- 同一上海本地日期并发执行 `ensure_schedule()` 只创建一条计划。
- 计划时间位于当天配置窗口内。
- 相同随机种子计划时间相同；不同日期计划可不同。
- `scheduled_at <= observed_at` 时只触发一次。
- 正式事件活动期间计划状态为 `deferred`，事件结束后下一轮触发同一计划。
- 调度关闭时计划状态为 `skipped`。
- 无激活边界时状态为 `failed`，`failure_reason` 非空，后续不会无限重试同一事务内。

Expected: FAIL，因为 scheduler 不存在。

- [ ] **Step 2: 实现计划生成和领取**

使用上海本地日期作为唯一键：

```python
local_now = observed_at.astimezone(ZoneInfo(settings_record.timezone))
local_date = local_now.date()
window_start = datetime.combine(local_date, settings_record.daily_window_start, tzinfo=zone)
window_end = datetime.combine(local_date, settings_record.daily_window_end, tzinfo=zone)
```

随机秒数使用 `secrets.randbelow(int((window_end - window_start).total_seconds()))`，第一次创建后以数据库中的 `scheduled_at` 为准。

领取计划时使用 `SELECT ... FOR UPDATE SKIP LOCKED`，将 `scheduled|deferred` 且到期状态改为 `due`，再调用触发函数。触发成功后更新 `triggered`、`event_id` 和 `triggered_at`；触发异常更新 `failed` 并保留错误摘要。

- [ ] **Step 3: 运行 scheduler 测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_scheduler.py -v
```

Expected: PASS。

- [ ] **Step 4: 提交**

```powershell
git add backend/app/operations/scheduler.py backend/tests/test_operations_scheduler.py
git commit -m "feat: schedule daily random test earthquakes"
```

---

## Task 5: 真实事件优先与任务冻结

**Files:**
- Create: `backend/app/operations/priority.py`
- Modify: `backend/app/events/service.py`
- Modify: `backend/app/collaboration/service.py`
- Modify: `backend/app/collaboration/scheduler.py`
- Create: `backend/tests/test_operations_priority.py`

**Interfaces:**
- Consumes: `OperationsRun`、`OperationsTestSchedule`、`EarthquakeEvent`、`WorkgroupTask`。
- Produces:
  - `OperationsPriorityGuard.has_active_real_event(session, observed_at) -> bool`
  - `OperationsPriorityGuard.apply_real_event_start(session, event_id, observed_at) -> PriorityResult`
  - `OperationsPriorityGuard.ensure_event_work_allowed(session, event_id) -> None`
  - `OperationsPriorityGuard.release_deferred_runs(session, observed_at) -> int`

- [ ] **Step 1: 写真实事件优先失败测试**

测试必须证明：

- `formal` 或 `manual` 事件当前修订入库后，守卫将同日 `scheduled|deferred` 测试计划保持为 `deferred`。
- 运行中的 `test` 运行记录变为 `canceled`，`detail["reason"] == "real_event_priority"`。
- `drill_conflict_policy=pause` 时，运行中的演练记录变为 `paused`，`resume_required=true`。
- `drill_conflict_policy=terminate` 时，运行中的演练记录变为 `canceled`。
- 暂停或取消运行的事件禁止继续提交协同成果，抛出 `OperationsWorkBlocked`。
- 暂停或取消运行的测试/演练不再产生临期、超期或外部渠道提醒。

Expected: FAIL，因为优先级守卫尚未接入。

- [ ] **Step 2: 实现守卫并接入事件服务**

在 `EventService` 构造函数增加可选 `priority_guard`。正式报通过
`ingest_collected()` 入库，手工正式事件通过 `ingest()` 入库；两条路径都要在
真实事件首次成为当前修订后，于同一事务内调用以下守卫，不能只接入其中一条：

```python
if result.is_new and result.is_current and result.event_kind in {
    EventKind.FORMAL,
    EventKind.MANUAL,
}:
    await self._priority_guard.apply_real_event_start(
        session,
        event_id=result.event_id,
        observed_at=normalized_received_at,
    )
```

守卫只处理 `event_type IN ('test', 'drill')` 的运行和计划，不修改正式/人工事件数据。

- [ ] **Step 3: 实现协同和通知冻结**

在任务提交、退回、取消、成果上传和发布前调用 `ensure_event_work_allowed()`；被冻结时返回 409 `operations_work_blocked`。

在 `DeadlineScheduler.run_once()` 查询非终态任务时排除关联运行状态为 `paused|canceled` 的事件，避免冻结任务继续产生提醒。已存在待发送通知不再新建，已有通知由现有通知服务决定是否保留为审计记录。

- [ ] **Step 4: 运行优先级和协同回归**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_priority.py tests/test_collaboration_notifications.py tests/test_collaboration_tasks_api.py tests/test_artifact_cancellation.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/operations/priority.py backend/app/events/service.py backend/app/collaboration/service.py backend/app/collaboration/scheduler.py backend/tests/test_operations_priority.py
git commit -m "feat: enforce real event priority for operations"
```

---

## Task 6: 运维工作进程与保留调度

**Files:**
- Create: `backend/app/operations/health.py`
- Create: `backend/app/operations/worker.py`
- Create: `backend/tests/test_operations_worker.py`
- Modify: `infra/compose.yaml`
- Modify: `.env.example`
- Modify: `backend/app/process_health.py`

**Interfaces:**
- Consumes: `DailyTestScheduler`、`OperationsService`、`CollaborationRetentionService`、`Settings`。
- Produces:
  - `OperationsWorkerCycleResult`
  - `run_operations_cycle(session_factory, observed_at, configured=settings) -> OperationsWorkerCycleResult`
  - `run_operations_worker(stop_event=None, ...)`
  - `python -m app.operations.worker`

- [ ] **Step 1: 写工作循环失败测试**

测试覆盖：

- 工作循环先创建/领取每日计划，再触发到期测试。
- 到达保留间隔时调用 `CollaborationRetentionService.purge_due()`。
- 未到保留间隔时不重复清理。
- 单个调度任务失败不会阻止保留清理，但工作循环结果记录失败数。
- 停止事件触发后循环退出。
- `/health` 在最近一次循环成功后返回 `ok`，超过陈旧窗口返回 `unavailable`。

Expected: FAIL，因为 worker 不存在。

- [ ] **Step 2: 实现工作循环**

循环顺序固定为：

1. `ensure_schedule`
2. `dispatch_due`
3. `release_deferred_runs`
4. 到期时执行协作保留
5. 刷新运行状态并更新健康状态

`OperationsWorkerCycleResult` 至少包含：

```python
schedules_created: int
schedules_due: int
tests_triggered: int
tests_deferred: int
schedules_failed: int
retention_ran: bool
retention_test_deleted: int
retention_drill_deleted: int
```

- [ ] **Step 3: 增加 Compose 服务和配置**

在 `backend/app/process_health.py` 的 `process_modes` 增加
`"operations": ("app.operations.worker",)`，并把 `choices` 与错误信息更新为
`worker|dispatcher|collaboration|operations`。

在 `infra/compose.yaml` 添加：

```yaml
operations-worker:
  build:
    context: ../backend
  env_file:
    - ../.env
  environment:
    OPERATIONS_WORKER_ENABLED: "true"
  command: ["python", "-m", "app.operations.worker"]
  restart: unless-stopped
  volumes:
    - ../backend:/app
    - ../config:/config
    - artifact-data:/var/lib/artifacts
  depends_on:
    postgres:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "python", "-m", "app.process_health", "operations"]
```

在 `.env.example` 添加：

```text
OPERATIONS_WORKER_ENABLED=false
OPERATIONS_WORKER_POLL_SECONDS=30
OPERATIONS_DAILY_WINDOW_START=02:00
OPERATIONS_DAILY_WINDOW_END=04:00
OPERATIONS_RETENTION_INTERVAL_SECONDS=86400
```

- [ ] **Step 4: 运行 worker 和 Compose 测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations config --quiet
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_operations_worker.py tests/test_process_health.py tests/test_collaboration_retention.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/operations/health.py backend/app/operations/worker.py backend/tests/test_operations_worker.py infra/compose.yaml .env.example backend/app/process_health.py
git commit -m "feat: run operations automation worker"
```

---

## Task 7: 运维前端与手工入口

**Files:**
- Create: `frontend/src/pages/OperationsPage.tsx`
- Create: `frontend/src/pages/OperationsPage.test.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: Task 3 运维 API；现有 `/manual` 手工正式事件页面。
- Produces:
  - `getOperationsOverview()`
  - `getOperationsSettings()`
  - `updateOperationsSettings()`
  - `triggerOperationsTest()`
  - `triggerOperationsDrill()`

- [ ] **Step 1: 写页面失败测试**

测试必须覆盖：

- 页面显示下一次每日测试、最近计划和最近运行。
- 手工测试按钮显示随机上海 `5.0-5.9` 场景说明并调用 API。
- 演练按钮调用 API，并显示“演练”不可移除标识。
- 正式事件活动时测试按钮禁用并显示优先保护原因。
- 超级管理员可以修改窗口、震级、深度和冲突策略。
- 非超级管理员看不到设置编辑控件。
- 错误和空状态均有稳定文本，不显示无限加载。

Expected: FAIL，因为页面不存在。

- [ ] **Step 2: 实现类型和客户端**

新增类型：

```typescript
export type OperationsTriggerMode =
  | "scheduled_test"
  | "manual_test"
  | "drill"
  | "manual_event";

export type OperationsRunState =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "canceled"
  | "failed";
```

手工触发请求必须支持 `Idempotency-Key`，前端在用户点击后生成一次 UUID，同一请求重试复用它，重新点击时生成新键。

- [ ] **Step 3: 实现页面**

页面采用工作台布局，不新增营销式大标题。首屏包含：

- 当前真实事件保护状态。
- 下一次每日测试时间和剩余时间。
- 今日计划状态和最近五次运行。
- “手工测试”“演练测试”“手工正式事件”三个明确操作区。
- 超级管理员设置面板。

手工正式事件按钮链接到现有 `/manual`，不重复实现坐标表单。测试和演练按钮必须调用专用运维 API，不直接调用 `/events/manual`，确保运行记录和审计完整。

- [ ] **Step 4: 运行前端聚焦测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test -- OperationsPage.test.tsx
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run build
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add frontend/src/pages/OperationsPage.tsx frontend/src/pages/OperationsPage.test.tsx frontend/src/api/client.ts frontend/src/types.ts frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: add operations automation console"
```

---

## Task 8: E2E、运行手册和交接说明

**Files:**
- Create: `frontend/e2e/operations.spec.ts`
- Create: `docs/runbooks/operations-test-drill.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: 全部后端和前端接口。
- Produces: 可复现的运维验收、故障恢复和日常操作文档。

- [ ] **Step 1: 写真实 E2E**

E2E 使用真实登录、真实后端数据库和真实 API：

1. 超级管理员打开 `/operations`。
2. 手工触发 5.0 至 5.9 级测试。
3. 验证返回事件类型为 `test`。
4. 验证列表、详情和成果带“测试”标识。
5. 验证 27 类图件和全部报告生产任务被创建，最终状态结果从真实 API 读取，不 mock。
6. 创建活动正式事件后，验证手工测试返回 `409 real_event_priority_active`。
7. 按测试事件日期验证保留策略查询结果，不删除未到期数据。

如果没有可用的真实凭据、数据库或浏览器运行环境，测试必须 skip 并报告原因，不得伪造通过。

- [ ] **Step 2: 编写运行手册**

手册必须包含：

- 每日计划的创建、查看、延后和失败恢复。
- 手工测试和演练触发。
- 上海时区窗口和随机位置规则。
- 真实事件优先状态和冲突策略。
- `pause` 与 `terminate` 的行为差异。
- 测试 400 天、演练两年保留核验。
- 运维 worker 健康检查、重启、死信和运行对账。
- 从数据库核验计划、运行和审计的只读 SQL。
- 不允许重新打开或移除测试/演练标识。

- [ ] **Step 3: 更新 README**

增加：

- 运维子系统启动命令。
- 运维页面地址。
- `operations-worker` 健康检查。
- 每日测试、手工测试和演练验收命令。
- 测试/演练保留边界说明。

- [ ] **Step 4: 运行文档和静态检查**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations config --quiet
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api ruff check app tests
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run build
git diff --check
```

Expected: 全部通过。

- [ ] **Step 5: 提交**

```powershell
git add frontend/e2e/operations.spec.ts docs/runbooks/operations-test-drill.md README.md
git commit -m "docs: document operations test and drill workflows"
```

---

## Task 9: 子系统全量验证与独立复审

**Files:**
- Create: `.superpowers/sdd/2026-10-10-operations-test-drill-automation/progress.md`
- Create: `.superpowers/sdd/2026-10-10-operations-test-drill-automation/final-report.md`

**Interfaces:**
- Consumes: Task 1-8 的所有提交和验收结果。
- Produces: 可追踪的验证证据、残余风险和合并建议。

- [ ] **Step 1: 运行后端全量测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest -q
```

Expected: 所有测试通过；如果存在因本机缺少外部凭据而 skip 的测试，必须逐项列出。

- [ ] **Step 2: 运行前端全量测试和 E2E**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm frontend npm run test:e2e -- operations.spec.ts
```

Expected: PASS；真实凭据缺失导致的 skip 不得写成通过。

- [ ] **Step 3: 运行迁移升级/降级和恢复测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api pytest tests/test_migrations.py tests/test_operations_schema.py tests/test_operations_worker.py -v
```

Expected: `0026_operations_automation` 可升级、降级、重新升级，且不影响既有数据。

- [ ] **Step 4: 独立复审**

复审必须检查：

- 每日计划是否可能因并发或重启重复触发。
- 随机场景是否始终落在当前上海行政边界内。
- 真实事件激活后是否可能继续创建测试或演练。
- 暂停/取消运行是否仍能提交协同成果或发送提醒。
- 保留清理是否可能删除正式、人工或未到期测试/演练成果。
- 所有测试/演练标识是否在数据库、命名、图件和文档中不可移除。
- 文档命令是否使用唯一 Compose project 且不输出敏感变量。

发现问题时按 Critical、Important、Minor 分类修复；Critical 和 Important 未关闭前不得完成本计划。

- [ ] **Step 5: 记录最终报告并提交**

`final-report.md` 必须记录提交范围、测试命令、通过数量、skip 原因、E2E 证据、残余风险和合并建议。

```powershell
git add .superpowers/sdd/2026-10-10-operations-test-drill-automation
git commit -m "docs: record operations automation verification"
```

---

## Final Acceptance Checklist

- [ ] 每日测试在上海时区配置窗口内随机触发一次。
- [ ] 同一本地日期不会重复创建或触发测试。
- [ ] 手工测试、演练和手工正式事件入口彼此独立。
- [ ] 上海行政区内 `5.0-5.9` 测试事件创建完整评估和成果生产。
- [ ] 真实事件活动期间测试延后，运行中非正式成果遵守现有优先终止规则。
- [ ] 演练冲突策略支持暂停和终止。
- [ ] 冻结或取消的测试/演练不能继续提交成果或产生截止提醒。
- [ ] 测试成果保留 400 天，演练成果保留两年。
- [ ] 测试和演练标识不可移除。
- [ ] 运维 API、页面、worker、健康检查和审计记录完整。
- [ ] 迁移、后端、前端、E2E、Ruff、类型检查、构建和 Compose 校验有真实证据。
