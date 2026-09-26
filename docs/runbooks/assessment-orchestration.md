# 评估编排运行手册

本文面向评估编排子系统的部署、值守和交接人员，说明 Outbox、Temporal Workflow、Dispatcher、Worker、评估运行和评估任务的核验与恢复方法。

本文只描述当前仓库已经实现的能力。当前版本只建立评估运行和 9 个任务骨架，不执行烈度、损失、制图或报告算法。

## 1. 子系统范围

当前已实现：

- 正式报和更正报触发 `assessment.requested` Outbox。
- 独立 `assessment-dispatcher` 认领 Outbox 并启动 Temporal Workflow。
- 独立 `temporal-worker` 执行幂等 Activity。
- 为每个事件修订建立一条 `assessment_runs` 记录和 9 条 `assessment_tasks`。
- 只读 API `GET /api/v1/assessments/events/{event_id}/current`。
- 事件详情页展示评估运行版本、状态、任务计数和任务状态点。

当前未实现：

- 模型烈度、仪器烈度、融合烈度计算。
- 受灾人口、人员伤亡、房屋破坏和经济损失计算。
- 27 类图件、专题图模板和报告文件生成。
- 工作组成果上传、审核和指挥大厅协同。
- AI 知识库问答。

## 2. 组件与数据流

```text
正式报 / 更正报
      |
      v
event_lifecycle_outbox
      |
      | assessment-dispatcher
      v
Temporal Workflow
assessment:{event_id}:{revision_id}
      |
      | temporal-worker / prepare_assessment
      v
assessment_runs + assessment_tasks
      |
      v
评估状态 API 与前端事件详情
```

关键边界：

- PostgreSQL Outbox 是事务边界和故障恢复来源。
- Temporal 不可用时，Dispatcher 不把 Outbox 标记为 `published`。
- API 和 Temporal Worker 不运行 Dispatcher；只有 `assessment-dispatcher` 容器显式设置 `ASSESSMENT_DISPATCHER_ENABLED=true`。
- Workflow ID 固定，重复投递使用 Temporal `USE_EXISTING` 冲突策略。
- Activity 以 Outbox、事件和修订为幂等键；同一修订重复执行不会重复创建运行或任务。

## 3. 启动与迁移

从仓库根目录执行。

先完成环境变量配置，再按以下顺序启动：

```powershell
docker compose --env-file .env -f infra/compose.yaml build
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal-postgres temporal
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker temporal-ui
```

查看服务：

```powershell
docker compose --env-file .env -f infra/compose.yaml ps
```

检查迁移版本：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic current
```

当前评估编排迁移头应为：

```text
0010_assessment_orchestration (head)
```

Temporal UI 只绑定本机：

```text
http://127.0.0.1:8088
```

默认 namespace 为 `default`，Task Queue 为 `assessment`。

## 4. 健康检查

检查 PostgreSQL：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec postgres pg_isready `
  -U earthquake `
  -d earthquake
```

检查 Temporal PostgreSQL：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec temporal-postgres pg_isready `
  -U earthquake `
  -d temporal
```

查看 Temporal、Dispatcher 和 Worker 日志：

```powershell
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 temporal
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 assessment-dispatcher
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 temporal-worker
```

如果 `.env` 修改了 PostgreSQL 用户名或数据库名，请替换上述命令中的 `earthquake` 和 `earthquake`/`temporal`。

## 5. Outbox 核验

按状态汇总：

```sql
SELECT status, count(*)
FROM event_lifecycle_outbox
GROUP BY status
ORDER BY status;
```

查看待处理、处理中或死信：

```sql
SELECT id, event_id, revision_id, status, attempt_count, available_at, last_error
FROM event_lifecycle_outbox
WHERE status IN ('pending', 'processing', 'dead_letter')
ORDER BY available_at;
```

查看最近评估运行：

```sql
SELECT id, event_id, revision_id, status, deadline_at, last_error
FROM assessment_runs
ORDER BY created_at DESC
LIMIT 20;
```

查看某事件当前评估运行：

```sql
SELECT id, revision_id, run_no, status, t1_at, deadline_at, created_at, completed_at
FROM assessment_runs
WHERE event_id = '<事件 UUID>'
ORDER BY run_no DESC
LIMIT 1;
```

查看该运行的任务：

```sql
SELECT sequence, task_key, status, deadline_at, attempt_count, max_attempts, last_error
FROM assessment_tasks
WHERE run_id = '<运行 UUID>'
ORDER BY sequence;
```

正常状态转换：

```text
pending -> processing -> published
```

Temporal 不可用或启动失败时，Outbox 可以回到 `pending` 并退避重试；达到最大尝试次数后为 `dead_letter`。不得手工把未实际发布的 Outbox 改为 `published`。

## 6. Temporal Workflow 核验

在 Temporal UI 中按以下 Workflow ID 搜索：

```text
assessment:<event_id>:<revision_id>
```

核验要点：

- Workflow 类型为 `AssessmentWorkflow`。
- Activity 名称为 `prepare_assessment`。
- 成功结果中的 `task_count` 为 `9`。
- 重复投递不会产生第二条同修订评估运行。
- Workflow 失败时查看 Activity 错误和重试历史，不删除 Workflow 来掩盖故障。

数据库侧核验：

```sql
SELECT
  r.event_id,
  r.revision_id,
  r.run_no,
  r.status AS run_status,
  count(t.id) AS total_tasks,
  count(*) FILTER (WHERE t.status = 'succeeded') AS completed_tasks,
  count(*) FILTER (WHERE t.status = 'failed') AS failed_tasks
FROM assessment_runs r
LEFT JOIN assessment_tasks t ON t.run_id = r.id
WHERE r.event_id = '<事件 UUID>'
GROUP BY r.id
ORDER BY r.run_no DESC;
```

## 7. 故障恢复

### 7.1 Dispatcher 或 Worker 重启

先确认数据库和 Temporal 可用，再重启独立进程：

```powershell
docker compose --env-file .env -f infra/compose.yaml restart assessment-dispatcher
docker compose --env-file .env -f infra/compose.yaml restart temporal-worker
```

重启不会改变 Outbox、Workflow ID、评估运行或任务记录。Dispatcher 会继续认领 `pending` 或租约到期的 `processing` 记录。

### 7.2 Temporal 不可用

处理顺序：

1. 检查 `temporal` 和 `temporal-postgres` 容器状态。
2. 检查 Temporal 日志和 `temporal-postgres` 就绪状态。
3. 不要修改 Outbox 状态；等待 Dispatcher 退避重试。
4. Temporal 恢复后检查 Dispatcher 日志和 Outbox 状态。

```powershell
docker compose --env-file .env -f infra/compose.yaml ps temporal temporal-postgres
docker compose --env-file .env -f infra/compose.yaml logs --tail 300 temporal
docker compose --env-file .env -f infra/compose.yaml logs --tail 300 temporal-postgres
```

### 7.3 死信安全重放

只有确认 Temporal 和 Worker 已恢复后才执行。先保存当前记录：

```sql
SELECT id, event_id, revision_id, status, attempt_count, available_at, last_error
FROM event_lifecycle_outbox
WHERE id = '<Outbox UUID>';
```

把指定死信恢复为 `pending`。此更新只重置调度状态，不伪报发布成功：

```sql
WITH replayed AS (
  UPDATE event_lifecycle_outbox
  SET status = 'pending',
      attempt_count = 0,
      available_at = now(),
      published_at = NULL,
      last_error = NULL
  WHERE id = '<Outbox UUID>'
    AND status = 'dead_letter'
  RETURNING id
)
SELECT id FROM replayed;
```

必须返回恰好一行。返回零行时不得继续猜测，应重新查询状态和错误。

### 7.4 过期 processing 租约

只有确认原 Dispatcher 已退出、租约已经过期时，才允许恢复：

```sql
WITH replayed AS (
  UPDATE event_lifecycle_outbox
  SET status = 'pending',
      available_at = now()
  WHERE id = '<Outbox UUID>'
    AND status = 'processing'
    AND available_at <= now()
  RETURNING id
)
SELECT id FROM replayed;
```

如果记录仍在有效租约内，不要修改，等待当前 Dispatcher 完成或租约自然过期。

### 7.5 数据库恢复

PostgreSQL 恢复后：

```powershell
docker compose --env-file .env -f infra/compose.yaml restart assessment-dispatcher temporal-worker
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 assessment-dispatcher
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 temporal-worker
```

恢复成功的证据是 Outbox 最终变为 `published`，并且对应修订存在一条评估运行和 9 条任务。不要通过清空 Outbox、删除 Temporal 历史或重复创建运行来绕过故障。

## 8. 已知边界与风险

- 本子系统只编排任务，不执行任何评估算法。
- Temporal UI 仅用于本机运维，不得直接暴露到互联网。
- 当前没有告警或自动清理死信；需要值守人员按本手册核验和处理。
- 当前未验证生产级 Temporal 高可用、TLS、鉴权或多节点 Worker 部署。
- 修改 Workflow 代码或 Task Queue 前必须评估运行中 Workflow 的兼容性。
- 正式接入内网数据、生产数据库或上级系统前，需要单独完成安全、网络和灾备评审。
