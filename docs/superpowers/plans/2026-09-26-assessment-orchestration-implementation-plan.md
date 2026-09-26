# 评估编排基础实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消费 `event_lifecycle_outbox` 中的正式报触发，通过 Temporal 幂等建立评估运行和评估任务骨架，并提供只读状态接口与前端展示。

**Architecture:** PostgreSQL Outbox 仍是事务边界和故障恢复来源。独立 `assessment-dispatcher` 使用 `FOR UPDATE SKIP LOCKED` 认领待发布记录，并以稳定 Workflow ID 启动 Temporal Workflow。Temporal Worker 执行幂等活动，在 PostgreSQL 中建立 `assessment_runs` 与 `assessment_tasks`。本期只编排任务，不实现烈度、损失、制图和报告算法。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、PostgreSQL 16、Temporal Python SDK 1.33.0、React 18、TypeScript、Vite、pytest、Vitest。

**Spec:** `docs/superpowers/specs/2026-09-17-shanghai-earthquake-emergency-decision-system-design.md`

## Global Constraints

- `T1` 是首次正式速报入库时刻，后续修订不重置原始 `T1`。
- 正式报和正式报修正触发评估；自动速报、测试、演练和普通人工事件在本期不自动触发。
- Outbox、评估运行和评估任务必须支持重复投递与 Worker 重启后的幂等恢复。
- 单个评估任务失败不得阻塞其他任务；失败、超时和重试状态必须持久化。
- 真实事件优先于测试和演练；本期只创建正式报评估，因此优先级固定为真实事件。
- 事件、修订、模型、数据和模板快照版本必须写入评估运行，不允许在运行中漂移。
- Temporal 不可用时，Outbox 保持 `pending` 或退避重试，不得伪报发布成功。
- 所有时间使用带时区 UTC 的 `datetime`；数据库列使用 `DateTime(timezone=True)`。
- 不记录密钥、访问令牌或完整敏感配置。
- 新增迁移的 `down_revision` 必须为当前头 `0009_non_cenc_lifecycle`。
- 每个任务先写失败测试，再写最小实现；提交前运行聚焦测试。

---

## 文件结构

### 新增

- `backend/app/assessment/__init__.py`
- `backend/app/assessment/domain.py`
- `backend/app/assessment/models.py`
- `backend/app/assessment/plan.py`
- `backend/app/assessment/repository.py`
- `backend/app/assessment/dispatcher.py`
- `backend/app/assessment/temporal.py`
- `backend/app/assessment/worker.py`
- `backend/app/assessment/schemas.py`
- `backend/app/assessment/router.py`
- `backend/migrations/versions/0010_assessment_orchestration.py`
- `backend/tests/test_assessment_domain.py`
- `backend/tests/test_assessment_schema.py`
- `backend/tests/test_assessment_plan.py`
- `backend/tests/test_assessment_repository.py`
- `backend/tests/test_assessment_dispatcher.py`
- `backend/tests/test_assessment_temporal.py`
- `backend/tests/test_assessment_api.py`
- `frontend/src/components/AssessmentProgressCard.tsx`
- `frontend/tests/assessment-progress.test.tsx`
- `docs/runbooks/assessment-orchestration.md`

### 修改

- `backend/pyproject.toml`
- `backend/app/config.py`
- `backend/app/main.py`
- `backend/app/events/models.py`
- `backend/app/events/repository.py`
- `backend/migrations/env.py`
- `backend/tests/conftest.py`
- `backend/tests/test_config.py`
- `infra/compose.yaml`
- `.env.example`
- `frontend/src/api/client.ts`
- `frontend/src/types.ts`
- `frontend/src/pages/EventDetailPage.tsx`
- `README.md`

---

### Task 1: 评估配置与持久化骨架

**Files:**
- Create: `backend/app/assessment/__init__.py`
- Create: `backend/app/assessment/domain.py`
- Create: `backend/app/assessment/models.py`
- Create: `backend/migrations/versions/0010_assessment_orchestration.py`
- Modify: `backend/app/config.py`
- Modify: `backend/migrations/env.py`
- Test: `backend/tests/test_assessment_domain.py`
- Test: `backend/tests/test_assessment_schema.py`
- Test: `backend/tests/test_config.py`

**Interfaces:**
- Produces:
  - `AssessmentRunStatus(StrEnum)`: `pending`、`running`、`completed`、`failed`、`canceled`
  - `AssessmentTaskStatus(StrEnum)`: `pending`、`running`、`succeeded`、`failed`、`skipped`、`canceled`
  - `AssessmentRun`
  - `AssessmentTask`
  - `Settings.temporal_address: str`
  - `Settings.temporal_namespace: str`
  - `Settings.temporal_task_queue: str`
  - `Settings.assessment_dispatcher_enabled: bool`
  - `Settings.assessment_outbox_poll_seconds: float`
  - `Settings.assessment_outbox_batch_size: int`
  - `Settings.assessment_outbox_max_attempts: int`
  - `Settings.assessment_workflow_deadline_seconds: int`

- [x] **Step 1: 写配置和 ORM 契约失败测试**

```python
def test_assessment_settings_defaults() -> None:
    configured = Settings(
        _env_file=None,
        database_url=VALID_DATABASE_URL,
        jwt_secret=VALID_JWT_SECRET,
        superadmin_initial_password=VALID_SUPERADMIN_PASSWORD,
    )

    assert configured.temporal_address == "temporal:7233"
    assert configured.temporal_namespace == "default"
    assert configured.temporal_task_queue == "assessment"
    assert configured.assessment_dispatcher_enabled is False
    assert configured.assessment_outbox_poll_seconds == 1.0
    assert configured.assessment_outbox_batch_size == 20
    assert configured.assessment_outbox_max_attempts == 10
    assert configured.assessment_workflow_deadline_seconds == 300


def test_assessment_orm_metadata_contract() -> None:
    runs = AssessmentRun.__table__
    tasks = AssessmentTask.__table__

    assert {
        "event_id",
        "revision_id",
        "outbox_id",
        "run_no",
        "status",
        "priority",
        "t1_at",
        "deadline_at",
        "snapshot",
    } <= set(runs.c.keys())
    assert {
        "run_id",
        "task_key",
        "task_type",
        "component",
        "status",
        "priority",
        "deadline_at",
        "attempt_count",
        "max_attempts",
    } <= set(tasks.c.keys())
```

- [x] **Step 2: 运行测试确认失败**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_config.py tests/test_assessment_domain.py tests/test_assessment_schema.py
```

Expected: FAIL，原因是 `app.assessment`、`AssessmentRun` 和新增配置尚不存在。

- [x] **Step 3: 实现领域枚举和 SQLAlchemy 模型**

`AssessmentRun` 的关键约束：

```python
__table_args__ = (
    UniqueConstraint("revision_id", name="uq_assessment_runs_revision"),
    UniqueConstraint("outbox_id", name="uq_assessment_runs_outbox"),
    UniqueConstraint("event_id", "run_no", name="uq_assessment_runs_event_run_no"),
    Index("ix_assessment_runs_status_deadline", "status", "deadline_at"),
)
```

字段包含：

```python
id: UUID
event_id: UUID
revision_id: UUID
outbox_id: UUID
run_no: int
trigger_reason: str
status: str
priority: int
t1_at: datetime
deadline_at: datetime
started_at: datetime | None
completed_at: datetime | None
snapshot: dict
last_error: str | None
created_at: datetime
updated_at: datetime
```

`AssessmentTask` 的关键约束：

```python
__table_args__ = (
    UniqueConstraint("run_id", "task_key", name="uq_assessment_tasks_run_task_key"),
    Index("ix_assessment_tasks_status_deadline", "status", "deadline_at"),
)
```

字段包含：

```python
id: UUID
run_id: UUID
task_key: str
task_type: str
component: str
priority: int
sequence: int
status: str
deadline_at: datetime
started_at: datetime | None
completed_at: datetime | None
attempt_count: int
max_attempts: int
result: dict | None
last_error: str | None
created_at: datetime
updated_at: datetime
```

- [x] **Step 4: 增加迁移与模型注册**

迁移 `0010_assessment_orchestration.py` 创建两张表、外键和索引；`downgrade()` 按依赖逆序删除。`migrations/env.py` 导入 `app.assessment.models`。

- [x] **Step 5: 运行聚焦测试和迁移往返测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_domain.py tests/test_assessment_schema.py tests/test_config.py tests/test_migrations.py
```

Expected: PASS。

- [x] **Step 6: 提交**

```bash
git add backend/app/assessment backend/migrations/versions/0010_assessment_orchestration.py \
  backend/migrations/env.py backend/app/config.py \
  backend/tests/test_assessment_domain.py backend/tests/test_assessment_schema.py \
  backend/tests/test_config.py
git commit -m "feat: add assessment orchestration schema"
```

---

### Task 2: 幂等评估计划与仓储

**Files:**
- Create: `backend/app/assessment/plan.py`
- Create: `backend/app/assessment/repository.py`
- Create: `backend/tests/test_assessment_plan.py`
- Create: `backend/tests/test_assessment_repository.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class PlannedAssessmentTask:
    task_key: str
    task_type: str
    component: str
    priority: int
    sequence: int
    deadline_offset_seconds: int
    max_attempts: int = 3


class AssessmentPlanBuilder:
    def build(
        self,
        *,
        event_kind: EventKind,
        institutional_level: str | None,
        service_level: int | None,
    ) -> tuple[PlannedAssessmentTask, ...]: ...


class AssessmentRepository:
    async def ensure_run_and_tasks(
        self,
        session: AsyncSession,
        *,
        event: EarthquakeEvent,
        revision: EarthquakeRevision,
        outbox: EventLifecycleOutbox,
    ) -> AssessmentRun: ...

    async def get_current_run(
        self,
        session: AsyncSession,
        *,
        event_id: str,
    ) -> AssessmentRun | None: ...
```

- [x] **Step 1: 写确定性计划失败测试**

首批任务必须为：

```python
expected = [
    "intensity.model",
    "intensity.instrument",
    "intensity.fusion",
    "loss.population",
    "loss.casualties",
    "loss.buildings",
    "loss.economic",
    "report.rapid_assessment",
    "workgroup.response_tasks",
]
assert [task.task_key for task in plan] == expected
```

优先级顺序：

```python
assert [task.priority for task in plan[:3]] == [100, 99, 98]
assert plan[-1].priority == 10
```

- [x] **Step 2: 写幂等仓储失败测试**

同一 Outbox 连续执行两次 `ensure_run_and_tasks()`：

```python
first = await repository.ensure_run_and_tasks(session, event=event, revision=revision, outbox=outbox)
second = await repository.ensure_run_and_tasks(session, event=event, revision=revision, outbox=outbox)

assert first.id == second.id
assert await count_tasks(session, first.id) == 9
```

- [x] **Step 3: 运行测试确认失败**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_plan.py tests/test_assessment_repository.py
```

Expected: FAIL，原因是计划和仓储尚不存在。

- [x] **Step 4: 实现计划构建器**

本任务固定输出 9 个任务。`deadline_offset_seconds` 分别为：

```python
{
    "intensity.model": 60,
    "intensity.instrument": 60,
    "intensity.fusion": 90,
    "loss.population": 120,
    "loss.casualties": 120,
    "loss.buildings": 150,
    "loss.economic": 180,
    "report.rapid_assessment": 240,
    "workgroup.response_tasks": 300,
}
```

- [x] **Step 5: 实现仓储幂等写入**

在同一事务中：

1. 锁定事件行。
2. 查询 `outbox_id` 的现有运行；存在则直接返回。
3. 计算 `run_no = max(event runs) + 1`。
4. 读取 `event.t1_at`；为空时抛出 `ValueError`。
5. 写入运行和 9 个任务。
6. 冲突回滚后重新读取同 `outbox_id` 的运行并返回。

`snapshot` 至少包含：

```python
{
    "event_id": str(event.id),
    "revision_id": str(revision.id),
    "revision_no": revision.revision_no,
    "t1_at": event.t1_at.isoformat(),
    "response_rule_version": revision.response_rule_version,
    "region_boundary_version": revision.region_boundary_version,
}
```

- [x] **Step 6: 运行测试并提交**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_plan.py tests/test_assessment_repository.py
```

Expected: PASS。

```bash
git add backend/app/assessment/plan.py backend/app/assessment/repository.py \
  backend/tests/test_assessment_plan.py backend/tests/test_assessment_repository.py
git commit -m "feat: build idempotent assessment plans"
```

---

### Task 3: Outbox 调度器

**Files:**
- Create: `backend/app/assessment/dispatcher.py`
- Create: `backend/tests/test_assessment_dispatcher.py`

**Interfaces:**

```python
class WorkflowStarter(Protocol):
    async def start_assessment(
        self,
        *,
        workflow_id: str,
        payload: dict[str, object],
    ) -> None: ...


class AssessmentDispatcher:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        starter: WorkflowStarter,
        batch_size: int,
        max_attempts: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None: ...

    async def dispatch_once(self) -> int: ...
```

- [x] **Step 1: 写认领、成功发布和重试失败测试**

必须证明：

```python
assert dispatched == 1
assert outbox.status == "published"
assert outbox.published_at == now()
assert outbox.attempt_count == 1
```

首次失败后：

```python
assert outbox.status == "pending"
assert outbox.attempt_count == 1
assert outbox.available_at == now() + timedelta(seconds=2)
assert "RuntimeError" in outbox.last_error
```

- [x] **Step 2: 写并发认领测试**

两个 dispatcher 并发处理同一批时，只允许一个实例认领每条 Outbox；另一个返回 `0`。测试使用真实 PostgreSQL 事务和 `FOR UPDATE SKIP LOCKED`。

- [x] **Step 3: 运行测试确认失败**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_dispatcher.py
```

Expected: FAIL，原因是 dispatcher 尚不存在。

- [x] **Step 4: 实现调度器**

Workflow ID 固定为：

```python
f"assessment:{outbox.event_id}:{outbox.revision_id}"
```

退避函数固定为：

```python
delay_seconds = min(2 ** max(outbox.attempt_count - 1, 0), 300)
```

达到 `max_attempts` 时状态改为 `dead_letter`，保留 `last_error`，不再自动发布。

- [x] **Step 5: 运行聚焦测试并提交**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_dispatcher.py
```

Expected: PASS。

```bash
git add backend/app/assessment/dispatcher.py backend/tests/test_assessment_dispatcher.py
git commit -m "feat: dispatch assessment outbox"
```

---

### Task 4: Temporal Workflow 与 Activity

**Files:**
- Modify: `backend/pyproject.toml`
- Create: `backend/app/assessment/temporal.py`
- Create: `backend/tests/test_assessment_temporal.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class AssessmentWorkflowInput:
    event_id: str
    revision_id: str
    outbox_id: str


@dataclass(frozen=True, slots=True)
class AssessmentWorkflowResult:
    run_id: str
    task_count: int


@workflow.defn
class AssessmentWorkflow:
    @workflow.run
    async def run(self, request: AssessmentWorkflowInput) -> AssessmentWorkflowResult: ...


class AssessmentActivities:
    @activity.defn
    async def prepare_assessment(
        self,
        request: AssessmentWorkflowInput,
    ) -> AssessmentWorkflowResult: ...
```

- [x] **Step 1: 写 Temporal 测试失败用例**

使用 `WorkflowEnvironment.start_time_skipping()`：

```python
async with await WorkflowEnvironment.start_time_skipping() as env:
    async with Worker(
        env.client,
        task_queue="assessment-test",
        workflows=[AssessmentWorkflow],
        activities=[activities.prepare_assessment],
    ):
        result = await env.client.execute_workflow(
            AssessmentWorkflow.run,
            AssessmentWorkflowInput(...),
            id="assessment:event:revision",
            task_queue="assessment-test",
        )

assert result.task_count == 9
```

- [x] **Step 2: 运行测试确认失败**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_temporal.py
```

Expected: FAIL，原因是 `temporalio` 和 workflow 尚不存在。

- [x] **Step 3: 增加固定依赖**

在 `backend/pyproject.toml` 增加：

```toml
"temporalio==1.33.0",
```

更新 lock 文件或镜像依赖安装方式后重建 API 镜像。

- [x] **Step 4: 实现 Workflow、Activity 和失败重试策略**

Activity 使用：

```python
@activity.defn
async def prepare_assessment(
    self,
    request: AssessmentWorkflowInput,
) -> AssessmentWorkflowResult:
    async with self._session_factory() as session:
        async with session.begin():
            run = await self._repository.ensure_run_from_outbox(
                session,
                event_id=request.event_id,
                revision_id=request.revision_id,
                outbox_id=request.outbox_id,
            )
            count = await self._repository.count_tasks(session, run.id)
    return AssessmentWorkflowResult(run_id=str(run.id), task_count=count)
```

RetryPolicy 固定为最多 3 次、初始间隔 1 秒、最大间隔 10 秒。

- [x] **Step 5: 运行测试并提交**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_temporal.py tests/test_assessment_repository.py
```

Expected: PASS。

```bash
git add backend/pyproject.toml backend/app/assessment/temporal.py \
  backend/tests/test_assessment_temporal.py
git commit -m "feat: orchestrate assessment in temporal"
```

---

### Task 5: Dispatcher 与 Worker 进程

**Files:**
- Create: `backend/app/assessment/worker.py`
- Modify: `backend/app/assessment/dispatcher.py`
- Modify: `infra/compose.yaml`
- Modify: `.env.example`
- Test: `backend/tests/test_assessment_dispatcher.py`
- Test: `backend/tests/test_config.py`

**Interfaces:**

```python
async def run_dispatcher(stop_event: asyncio.Event | None = None) -> None: ...
async def run_worker(stop_event: asyncio.Event | None = None) -> None: ...
```

- [x] **Step 1: 写进程工厂失败测试**

使用假的 Temporal client、session factory 和 sleep 函数，断言：

```python
assert dispatcher.batch_size == configured.assessment_outbox_batch_size
assert dispatcher.max_attempts == configured.assessment_outbox_max_attempts
assert worker_task_queue == configured.temporal_task_queue
```

- [x] **Step 2: 实现连接工厂和独立进程**

Dispatcher 循环：

```python
while not stop_event.is_set():
    dispatched = await dispatcher.dispatch_once()
    if dispatched == 0:
        await asyncio.sleep(settings.assessment_outbox_poll_seconds)
```

Worker：

```python
client = await Client.connect(
    settings.temporal_address,
    namespace=settings.temporal_namespace,
)
worker = Worker(
    client,
    task_queue=settings.temporal_task_queue,
    workflows=[AssessmentWorkflow],
    activities=[assessment_activities.prepare_assessment],
)
await worker.run()
```

- [x] **Step 3: 在 Compose 中增加独立服务**

新增：

- `temporal`：自托管 Temporal Server，持久化到 PostgreSQL 的独立数据库。
- `temporal-ui`：本地管理界面，仅绑定 `127.0.0.1`。
- `assessment-dispatcher`：运行 `python -m app.assessment.worker dispatcher`。
- `temporal-worker`：运行 `python -m app.assessment.worker worker`。

API 服务默认 `ASSESSMENT_DISPATCHER_ENABLED=false`；仅 dispatcher 容器显式设为 `true`。

- [x] **Step 4: 运行配置和进程测试**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_dispatcher.py tests/test_config.py
```

Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/app/assessment/worker.py backend/app/assessment/dispatcher.py \
  infra/compose.yaml .env.example backend/tests/test_assessment_dispatcher.py \
  backend/tests/test_config.py
git commit -m "feat: run assessment dispatcher and worker"
```

---

### Task 6: 评估状态只读 API

**Files:**
- Create: `backend/app/assessment/schemas.py`
- Create: `backend/app/assessment/router.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/assessment/repository.py`
- Test: `backend/tests/test_assessment_api.py`

**Interfaces:**

```text
GET /api/v1/assessments/events/{event_id}/current
```

Response:

```json
{
  "run_id": "uuid",
  "event_id": "uuid",
  "revision_id": "uuid",
  "run_no": 1,
  "status": "pending",
  "t1_at": "2026-09-26T01:00:00Z",
  "deadline_at": "2026-09-26T01:05:00Z",
  "completed_task_count": 0,
  "failed_task_count": 0,
  "total_task_count": 9,
  "tasks": []
}
```

- [x] **Step 1: 写 API 失败测试**

覆盖：

```python
response = await client.get(
    f"/api/v1/assessments/events/{event_id}/current",
    headers={"Authorization": f"Bearer {token}"},
)
assert response.status_code == 200
assert response.json()["total_task_count"] == 9
```

再覆盖无运行返回 `404 assessment_run_not_found` 和非法 UUID 返回 `422`。

- [x] **Step 2: 运行测试确认失败**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_api.py
```

Expected: FAIL，原因是路由尚不存在。

- [x] **Step 3: 实现只读仓储查询和认证路由**

沿用现有 `require_role`，允许 `superadmin`、`group_leader`、`group_deputy`、普通组员和只读用户。未知角色沿用项目现有拒绝策略。

- [x] **Step 4: 运行测试并提交**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api `
  pytest -q tests/test_assessment_api.py
```

Expected: PASS。

```bash
git add backend/app/assessment/schemas.py backend/app/assessment/router.py \
  backend/app/main.py backend/app/assessment/repository.py \
  backend/tests/test_assessment_api.py
git commit -m "feat: expose assessment run status"
```

---

### Task 7: 前端评估进度卡

**Files:**
- Create: `frontend/src/components/AssessmentProgressCard.tsx`
- Create: `frontend/tests/assessment-progress.test.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/EventDetailPage.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**

```typescript
export interface AssessmentRunStatus {
  run_id: string;
  event_id: string;
  revision_id: string;
  run_no: number;
  status: "pending" | "running" | "completed" | "failed" | "canceled";
  t1_at: string;
  deadline_at: string;
  completed_task_count: number;
  failed_task_count: number;
  total_task_count: number;
  tasks: AssessmentTaskStatus[];
}
```

- [ ] **Step 1: 写组件失败测试**

测试断言：

```tsx
expect(screen.getByText("评估编排")).toBeInTheDocument();
expect(screen.getByText("3 / 9")).toBeInTheDocument();
expect(screen.getByText("距 T1 +5 分钟截止")).toBeInTheDocument();
```

无运行数据时断言：

```tsx
expect(screen.queryByText("评估编排")).not.toBeInTheDocument();
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
cd frontend
npm test -- --run tests/assessment-progress.test.tsx
```

Expected: FAIL，原因是组件和类型尚不存在。

- [ ] **Step 3: 实现卡片和事件详情接入**

卡片以紧凑的运营界面展示：

- 运行版本 `V{run_no}`。
- 总状态。
- `完成任务数 / 总任务数`。
- `T1`、截止时间、剩余或超时分钟数。
- 最多显示 9 个任务状态点。

不使用大标题、渐变或装饰性卡片嵌套。

- [ ] **Step 4: 运行前端测试并提交**

Run:

```powershell
cd frontend
npm test -- --run tests/assessment-progress.test.tsx
npm run typecheck
npm run build
```

Expected: PASS。

```bash
git add frontend/src/components/AssessmentProgressCard.tsx \
  frontend/tests/assessment-progress.test.tsx frontend/src/types.ts \
  frontend/src/api/client.ts frontend/src/pages/EventDetailPage.tsx \
  frontend/src/styles.css
git commit -m "feat: show assessment orchestration progress"
```

---

### Task 8: 运行手册与全量验收

**Files:**
- Create: `docs/runbooks/assessment-orchestration.md`
- Modify: `README.md`
- Test: `backend/tests/test_assessment_dispatcher.py`
- Test: `backend/tests/test_assessment_temporal.py`
- Test: `backend/tests/test_assessment_api.py`

**Interfaces:**

- Produces operational instructions for:
  - Temporal server, dispatcher, worker health.
  - Outbox pending/processing/dead-letter inspection.
  - Safe replay of failed Outbox rows.
  - Workflow history and task status inspection.
  - Worker restart and database recovery.

- [ ] **Step 1: 编写运行手册**

手册必须包含以下可执行 SQL：

```sql
SELECT status, count(*)
FROM event_lifecycle_outbox
GROUP BY status
ORDER BY status;

SELECT id, event_id, revision_id, status, attempt_count, available_at, last_error
FROM event_lifecycle_outbox
WHERE status IN ('pending', 'processing', 'dead_letter')
ORDER BY available_at;

SELECT id, event_id, revision_id, status, deadline_at, last_error
FROM assessment_runs
ORDER BY created_at DESC
LIMIT 20;
```

- [ ] **Step 2: 更新 README 的能力边界**

README 必须明确：

- 已实现评估运行和任务骨架。
- 烈度、损失、制图和报告算法仍未实现。
- Temporal 不可用时会阻塞 Outbox 发布，不会影响已接收事件。

- [ ] **Step 3: 运行全量验证**

Run:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -q
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

Expected:

- Alembic 到 `0010_assessment_orchestration`。
- 后端全部测试通过。
- Ruff 无错误。
- 前端全部测试、类型检查和构建通过。

- [ ] **Step 4: 验证真实本地 Temporal 回路**

启动 Compose 后：

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal temporal-ui
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker
```

向 API 提交一条正式报，确认：

1. Outbox 变为 `published`。
2. Temporal Workflow 执行成功。
3. `assessment_runs` 有一条记录。
4. `assessment_tasks` 有 9 条记录。
5. 前端事件详情显示 `0 / 9` 或实际完成进度。

- [ ] **Step 5: 最终提交**

```bash
git add docs/runbooks/assessment-orchestration.md README.md
git commit -m "docs: document assessment orchestration"
```

---

## 自检结果

- 规格覆盖：Outbox 消费、T1、修订幂等、评估运行、任务状态、时限、错误恢复、状态展示均有对应任务。
- 明确排除：烈度、损失、制图、报告算法、AI、通知和任务协同页面不在本期实现。
- 类型一致性：`outbox_id`、`revision_id`、`event_id`、`run_id`、`task_key` 在模型、仓储、Temporal 和 API 中命名一致。
- 迁移头：`0010_assessment_orchestration` 的 `down_revision` 固定为 `0009_non_cenc_lifecycle`。
- 无占位符：所有任务均给出行为、接口、测试和提交边界。
