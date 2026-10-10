# 运维、测试与演练运行手册

本文面向值守、运维和交接人员，说明运维子系统的启动、手工触发、每日计划、真实事件优先、保留清理、故障恢复和只读核验方法。文中命令固定使用 Compose project `codex-operations`，不要复用其他任务的容器或卷，也不要把 `.env`、密码、Token、API Key 或连接串写入日志、提交或回复。

本文只描述仓库中已经实现的能力。运维 API、`/operations` 页面、`operations-worker`、每日计划、手工测试、演练、真实事件优先和保留清理均已落地；未实现的自动执行入口会明确标注。

## 1. 子系统范围

运维子系统由以下部分组成：

- FastAPI 运维路由：`GET /api/v1/operations/overview`、`GET/PUT /api/v1/operations/settings`、`GET /api/v1/operations/schedules`、`GET /api/v1/operations/runs`、`POST /api/v1/operations/test-trigger`、`POST /api/v1/operations/drill-trigger`。
- React 页面：`http://localhost:5173/operations`。
- 独立 `operations-worker`：维护上海本地日期计划、领取到期计划、触发每日测试、执行协作事件保留和更新健康状态。
- 持久化：`operations_settings`、`operations_test_schedules`、`operations_runs`、`operations_audit_logs`。
- 测试和演练事件继续复用既有事件、评估、27 类图件、全部报告、工作组协同和成果生产链路。

## 2. 首次启动

先准备 `.env` 并启动依赖、迁移数据库，再启动全部服务。以下命令使用隔离 project `codex-operations`：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations build
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations up -d postgres temporal-postgres temporal qdrant embedding
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations up -d
```

完整链路需要 API、`operations-worker`、`collaboration-worker`、`assessment-dispatcher`、`temporal-worker`、`artifact-worker` 和 `artifact-dispatcher`。`frontend` 容器启动 Vite；生产或专项验收应按实际端口覆盖 `API_PORT`、`VITE_API_PROXY_TARGET`、`E2E_BASE_URL` 和 `E2E_API_BASE_URL`。

页面地址：

```text
http://localhost:5173/operations
```

停止：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations down --remove-orphans
```

仅在一次性验收环境可清除卷：

```powershell
# 仅在确认是 codex-operations 的一次性验收环境执行。
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations down -v --remove-orphans
```

## 3. 每日计划

### 3.1 创建

`operations-worker` 每个循环按上海本地日期幂等创建当日计划。同一 `local_date` 使用唯一约束，最多一条；重启或并发领取不会重复创建。计划创建由 worker 自动完成，前端和 API 不提供手工创建每日计划的入口。

窗口和范围来自 `operations_settings` 默认值或超级管理员设置：

- 时区默认 `Asia/Shanghai`。
- 窗口默认 `02:00` 至 `04:00`。
- 震级默认 `5.0` 至 `5.9`，步长 `0.1`。
- 深度默认 `5.00` 至 `20.00` 千米，步长 `0.01`。
- 震中通过当前激活上海市行政边界使用 `ST_PointOnSurface(geom)` 随机取样；没有激活边界时触发失败，不回退到硬编码坐标。

`scheduled_at` 在当天窗口内使用 `secrets.randbelow()` 随机选择秒数。第一次创建后，计划时间以数据库中的值为准。

### 3.2 查看

页面首屏显示“下一次每日测试”“今日计划”和“最近运行”。API 查询：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/operations/overview | ConvertTo-Json -Depth 8
Invoke-RestMethod http://127.0.0.1:8000/api/v1/operations/schedules?limit=20 | ConvertTo-Json -Depth 8
```

`Invoke-RestMethod` 只适合无鉴权接口；运维接口需要 Bearer Token。不要在上面的命令中打印 Token。

### 3.3 延后

当前没有手工“延后”按钮或 API。延后由真实事件优先守卫自动完成：

1. 正式报或手工正式事件首次成为当前修订后，同事务内守卫将到期的 `scheduled`/`deferred` 每日测试计划改为 `deferred`。
2. 每日测试计划不会再被调度器补偿触发。
3. 真实事件 7 天保护窗口结束且没有活动真实事件时，worker 调用 `release_deferred_runs()`，把到期计划恢复为 `scheduled`，由下一轮领取。

查看延后计划：

```sql
SELECT local_date, state, scheduled_at, failure_reason
FROM operations_test_schedules
WHERE state = 'deferred'
ORDER BY local_date DESC;
```

### 3.4 失败恢复

调度失败不会自动重复触发同一计划。失败原因保存在 `failure_reason`，审计记录写入 `operations_audit_logs`。恢复步骤：

1. 查看失败原因。
2. 修复配置、激活上海市边界、恢复数据库或相关 worker。
3. 确认同一上海日期尚未成功生成测试事件。
4. 确认修复后，可把失败计划重置为可领取状态。以下 SQL 只能用于明确失败且尚未产生事件的计划：

```sql
UPDATE operations_test_schedules
SET state = 'due',
    failure_reason = NULL,
    updated_at = now()
WHERE state = 'failed'
  AND event_id IS NULL
  AND scheduled_at <= now();
```

不要通过新建同日计划补偿失败，否则会命中 `operations_test_schedules.local_date` 唯一约束。调度器不通过重复触发补偿失败。

## 4. 手工测试与演练

### 4.1 页面触发

超级管理员、`group_leader`、`group_deputy` 可打开 `/operations`：

- “手工测试”调用 `POST /api/v1/operations/test-trigger`。
- “演练测试”调用 `POST /api/v1/operations/drill-trigger`。
- “手工正式事件”链接到既有 `/manual`，不重复实现坐标表单。

手工测试场景使用当前设置随机生成上海 `5.0-5.9` 级测试事件，地点固定为“上海市（系统测试）”，`event_kind` 为 `test`。演练事件复用同一生成器，但 `event_kind` 为 `drill`。

前端会为一次点击生成 `Idempotency-Key`；同一请求重试复用该键，重新点击生成新键。运维 API 不直接调用 `/api/v1/events/manual`，以便完整记录运行和审计。

### 4.2 API 触发

先登录取得 Bearer Token，然后发起：

```text
POST /api/v1/operations/test-trigger
Idempotency-Key: <UUID>
Authorization: Bearer <token>
```

```text
POST /api/v1/operations/drill-trigger
Idempotency-Key: <UUID>
Authorization: Bearer <token>
```

测试触发成功返回 `201`，字段包括 `event_id`、`run_id`、`trigger_mode`、`operation_state`、`event_kind=test` 和 `triggered_assessment`。演练成功返回 `event_kind=drill`。不要把响应中的运行编号解释为已经完成的成果状态；成果状态必须从评估和成果 API 实时读取。

## 5. 上海时区窗口和随机位置

`OperationsSettings.timezone` 默认 `Asia/Shanghai`。调度器按设置时区把 `observed_at` 转为本地日期，再组合 `daily_window_start` 和 `daily_window_end`。窗口必须满足开始早于结束，否则 API 设置和 worker 创建都会拒绝。

随机场景使用当前激活边界查询：

```sql
SELECT ST_X(ST_PointOnSurface(geom)) AS longitude,
       ST_Y(ST_PointOnSurface(geom)) AS latitude
FROM region_boundaries
WHERE is_active
ORDER BY id
LIMIT 1;
```

没有激活边界时，`OperationsConfigurationError("active_shanghai_boundary_required")` 会使触发失败，不会回退到硬编码上海坐标。

## 6. 真实事件优先和冲突策略

真实正式报通过 `ingest_collected()` 入库，手工正式事件通过 `ingest()` 入库；两条路径都会在同一事务内调用优先级守卫。守卫只处理 `test`/`drill`，不修改正式或人工事件。

保护窗口为滚动 7 天：同时满足以下条件的当前 `formal|manual` 事件被视为活动真实事件：

- `current_revision_id IS NOT NULL`。
- `origin_time >= observed_at - 7 days`。
- `origin_time <= observed_at`。

冲突结果：

- 手工测试：真实事件活动时返回 HTTP `409`，detail 为 `real_event_priority_active`。
- 每日测试：真实事件活动时计划变为 `deferred`。
- 演练：按 `drill_conflict_policy` 处理，见下一节。

页面在真实事件活动时显示“真实事件优先保护中”，并禁用“手工测试”按钮。

## 7. `pause` 与 `terminate`

设置中的 `drill_conflict_policy` 只影响演练冲突：

- `pause`：尚未创建运行的新演练触发请求返回 `operation_state=deferred`，不生成 `operations_runs` 行；已经存在且处于 `queued|running` 的演练运行改为 `paused`，`resume_required=true`。成果生产层仍按真实事件优先安全边界终止非正式生产，不尝试在 Temporal 中不可靠地中途挂起。
- `terminate`：新演练触发请求返回 `operation_state=canceled`；已经存在的演练运行改为 `canceled`，`resume_required` 不自动置位。

暂停或取消的测试/演练会被 `OperationsWorkBlocked` 冻结：协同成果提交、退回、取消、上传和发布返回 `409 operations_work_blocked`，截止提醒也不会再新建。真实事件窗口结束只恢复到期每日测试计划，不自动重启已经进入安全暂停边界的演练。

查询被暂停或取消的演练：

```sql
SELECT r.id,
       r.event_id,
       r.trigger_mode,
       r.operation_state,
       r.resume_required,
       r.detail
FROM operations_runs r
WHERE r.trigger_mode = 'drill'
  AND r.operation_state IN ('paused', 'canceled')
ORDER BY r.created_at DESC;
```

## 8. 保留核验

### 8.1 边界

协作事件保留由 `operations-worker` 调用 `CollaborationRetentionService.purge_due()`：

- 测试事件保留 400 天。
- 演练事件保留 2 个自然年。
- 正式和人工事件长期保留。

事件成果文件保留由 `artifact-worker` 的 `ArtifactRetentionService` 处理，测试文件 400 天、演练文件 2 年；正式和人工成果不删除。两者是不同进程的独立保留周期。

未到期数据不会被删除。E2E 会读取测试事件 `origin_time` 并与 400 天边界比较，确认未到期事件仍可通过真实 API 查询。

### 8.2 只读 SQL 核验

列出将在“今天”边界前到期的测试和演练事件，只读查询不删除：

```sql
SELECT id,
       event_type,
       origin_time,
       CASE
           WHEN event_type = 'test' THEN now() - interval '400 days'
           WHEN event_type = 'drill' THEN now() - interval '2 years'
       END AS retention_cutoff
FROM earthquake_events
WHERE event_type IN ('test', 'drill')
  AND origin_time <= CASE
          WHEN event_type = 'test' THEN now() - interval '400 days'
          WHEN event_type = 'drill' THEN now() - interval '2 years'
      END
ORDER BY origin_time;
```

核验未到期测试事件仍可查询：

```sql
SELECT e.id,
       e.event_type,
       e.origin_time,
       r.id AS run_id,
       r.operation_state
FROM earthquake_events e
JOIN operations_runs r ON r.event_id = e.id
WHERE e.event_type = 'test'
  AND e.origin_time > now() - interval '400 days'
ORDER BY e.origin_time DESC
LIMIT 20;
```

正式和人工事件不应进入保留清理。可核验清理逻辑不会选中它们：

```sql
SELECT count(*)
FROM earthquake_events
WHERE event_type NOT IN ('test', 'drill')
  AND origin_time <= now() - interval '400 days';
```

该查询预期只证明正式/人工事件不是按 400 天测试边界选择；它们不会被运维保留服务删除。

## 9. 运维 worker 健康、重启、死信和运行对账

### 9.1 健康检查

Compose 服务健康检查执行：

```text
python -m app.process_health operations
```

该命令只检查当前容器中是否存在命令行包含 `app.operations.worker` 的进程，返回 `0` 表示进程存在，`1` 表示进程不存在。它不读取 `app.operations.health.OperationsHealthState` 中的 `last_heartbeat_at`，因此进程存活但循环卡死时仍可能被 Compose 判定为健康。

`OperationsHealthState` 心跳目前只由 worker 进程内部维护，`health_app` 未作为 Compose healthcheck 暴露。要发现“进程存在但循环卡死”，应检查最近成功循环日志：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations logs --tail=100 operations-worker |
  Select-String 'operations worker cycle counts='
```

如果最近一次 `operations worker cycle counts=` 时间超过 `OPERATIONS_WORKER_POLL_SECONDS` 与陈旧窗口之和，应把循环卡死与进程存活分开诊断，不要仅凭 `process_health operations` 返回成功就认为调度和保留循环正常。

查看服务状态：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations ps
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations logs --tail=100 operations-worker
```

### 9.2 重启

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations restart operations-worker
```

重启后 worker 会再次 `ensure_schedule()`；因为 `local_date` 唯一，不会重复创建同日计划。已处于终态的计划不会自动重试。

### 9.3 死信

运维子系统没有独立运维死信表。运维失败证据保存为：

- `operations_test_schedules.failure_reason`。
- `operations_audit_logs` 中的 `operations.schedule.failed` 和触发审计。

底层评估、成果和协同链路继续使用各自的 Outbox 与现有恢复流程，请分别参阅 [评估编排运行手册](assessment-orchestration.md)、[成果生产运行手册](../operations/artifact-production-runbook.md) 和 [工作组协同与指挥大厅运行手册](workgroup-collaboration-command-hall.md)。

### 9.4 运行对账

当前 `operations-worker` 不会把评估或成果终态回写到 `operations_runs`。`operations_runs.operation_state` 记录触发侧状态；真实评估和成果状态由既有 API 提供。对账应使用只读 SQL 对照两边：

```sql
SELECT r.id AS operations_run_id,
       r.trigger_mode,
       r.operation_state,
       r.event_id,
       ar.id AS assessment_run_id,
       ar.status AS assessment_status,
       pr.id AS production_run_id,
       pr.status AS production_status,
       pr.production_mode
FROM operations_runs r
LEFT JOIN assessment_runs ar ON ar.event_id = r.event_id
LEFT JOIN artifact_production_runs pr ON pr.assessment_run_id = ar.id
WHERE r.trigger_mode IN ('scheduled_test', 'manual_test', 'drill')
ORDER BY r.created_at DESC
LIMIT 50;
```

不要把 `operations_runs.operation_state=queued` 当作评估失败；先确认 `assessment_runs`、`artifact_production_runs` 和对应 worker 状态。

## 10. 从数据库核验计划、运行和审计

进入 `postgres` 容器的只读查询方式：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-operations exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

进入 psql 后建议使用 `\x auto`。以下为常用只读 SQL：

```sql
-- 设置
SELECT settings_key,
       daily_test_enabled,
       timezone,
       daily_window_start,
       daily_window_end,
       test_magnitude_min,
       test_magnitude_max,
       test_depth_min_km,
       test_depth_max_km,
       drill_conflict_policy,
       row_version,
       updated_by,
       updated_at
FROM operations_settings;

-- 每日计划
SELECT local_date,
       window_start,
       window_end,
       scheduled_at,
       state,
       random_seed,
       event_id,
       failure_reason,
       triggered_at
FROM operations_test_schedules
ORDER BY local_date DESC
LIMIT 20;

-- 运行
SELECT id,
       event_id,
       schedule_id,
       trigger_mode,
       operation_state,
       priority,
       idempotency_key,
       triggered_by,
       resume_required,
       detail,
       created_at,
       updated_at
FROM operations_runs
ORDER BY created_at DESC
LIMIT 20;

-- 审计
SELECT action,
       actor,
       resource_type,
       resource_id,
       before_state,
       after_state,
       detail,
       created_at
FROM operations_audit_logs
ORDER BY created_at DESC
LIMIT 50;
```

## 11. 测试和演练标识不可移除

测试/演练标识来自：

- `earthquake_events.event_type`。
- 成果生产模式 `artifact_production_runs.production_mode`、`generated_artifacts.production_mode`。
- 成果文件名、可见文字和 API `marker`（测试为“【测试】”，演练为“【演练】”）。

当前运维和成果 API 不提供重新打开、改写或移除测试/演练标识的接口。`artifact_production_runs` 对 `production_mode` 有检查约束，不允许把测试/演练模式写成普通实战模式。恢复或运维时不要手工把 `production_mode`、`event_type`、`file_name` 或 marker 改为正式/人工值；这会被验收、审计和保留边界视为数据篡改。

## 12. E2E 验收

E2E 使用真实登录、真实数据库和真实 API，不使用 mock。需要准备宿主机 Vite、Playwright Chromium 和以下环境变量：

- `E2E_SUPERADMIN_USERNAME`（可选，默认 `superadmin`）。
- `E2E_SUPERADMIN_PASSWORD`（必填）。
- `E2E_ALLOW_EVENT_PURGE=1`（必填）：仅允许在专用一次性 `codex-operations` 验收环境运行。测试会创建手工正式事件并在 `finally` 中调用超级管理员 `DELETE /api/v1/admin/events/{event_id}/purge` 清理；未设置该变量时测试直接 skip，避免正式/人工长期保留数据被污染。
- `E2E_BASE_URL`（可选，默认 `http://localhost:5173`）。
- `E2E_API_BASE_URL`（可选，设为绝对 API 地址后只有 Playwright 控制请求直连该地址，页面仍走 Vite `/api` 代理）。

```powershell
cd frontend
$env:E2E_SUPERADMIN_USERNAME = "<超级管理员用户名>"
$env:E2E_SUPERADMIN_PASSWORD = "<超级管理员密码>"
$env:E2E_ALLOW_EVENT_PURGE = "1"
$env:E2E_BASE_URL = "http://127.0.0.1:5173"
$env:E2E_API_BASE_URL = "http://127.0.0.1:8000"
npm run test:e2e -- operations.spec.ts
```

缺失 `E2E_SUPERADMIN_PASSWORD` 或真实 API 不可用时，测试会 skip 并报告原因，不会伪造通过。验收覆盖 `1920x1080` 管理终端，不新增手机端视口。

## 13. 故障排查速查

| 现象 | 检查 |
| --- | --- |
| `/operations` 无法加载 | API 是否启动、Bearer token 是否有效、`operations` 路由是否随 `app.main` 注册 |
| 手工测试返回 `409 real_event_priority_active` | 是否处于真实事件 7 天保护窗口；预期为保护生效 |
| 手工测试返回 `503 operations_configuration_unavailable` | 是否缺少当前激活上海市边界 |
| 每日计划一直 `scheduled` | `OPERATIONS_WORKER_ENABLED`、`OPERATIONS_WORKER_POLL_SECONDS`、worker 容器健康状态和窗口时间 |
| 每日计划 `failed` | 查看 `failure_reason`，修复后按第 3.4 节重置为 `due` |
| 成果没有全部生成 | 查看 `assessment_runs`、`artifact_production_runs`、`artifact-production-tasks` 和对应 worker 日志；不要从运维运行表推断 |
| 未到期事件被误删 | 确认是否错误执行了 `down -v`、手工删除或错误调用保留服务；保留边界见第 8 节 |
