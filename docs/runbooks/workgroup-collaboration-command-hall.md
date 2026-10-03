# 工作组协同与应急指挥大厅运行手册

本文面向平台值守、应急技术组和系统运维人员，说明七个固定工作组的任务生成、成果流转、通知降级、指挥大厅投影和大屏验收方法。

本文只描述当前分支已经实现的能力。协同子系统不改变既有评估和成果生产的时限：上海市行政区域内 `5.0` 至 `5.9` 级事件的自动评估与全部 39 项专业成果仍以正式报入库为基准，在 `T1 + 300 秒` 内完成。既有终态仍为 `28 complete + 11 degraded`、`0 failed`、`0 timeout`。

## 1. 组件与数据流

`collaboration-worker` 是常驻进程，负责：

- 消费 `event_lifecycle_outbox` 中的 `collaboration.requested`。
- 按 `shanghai-2026.2` 预案模板生成七个工作组的固定任务。
- 将成果关联、任务状态、到岗和告警变化写入 `collaboration_projection_outbox`。
- 刷新单事件和分组指挥大厅投影。
- 执行临期、超时、提醒和通知降级。

数据流：

```text
正式报 / 更正报 / 人工正式事件 / 测试 / 演练
                    |
                    v
          event_lifecycle_outbox
                    |
                    | collaboration-worker
                    v
      collaboration_tasks + deliverables + versions
                    |
                    v
       collaboration_projection_outbox
                    |
                    v
 command_hall_event/group/alert_projections
                    |
                    v
            SSE / 5 秒轮询降级
```

固定工作组为：

1. 新闻信息值守组
2. 监测预报组
3. 综合协调组
4. 震害评估组
5. 应急技术组
6. 后勤保障组
7. 中心站组

上海市行政区域内 `5.0` 至 `5.9` 级测试事件必须生成七个固定工作组共 60 条任务，并关联当前成果目录中的全部 27 类专业图件和全部 12 类报告或演示稿。

## 2. 启停与迁移

从仓库根目录执行。首次启动必须先完成数据库迁移：

```powershell
docker compose --env-file .env -f infra/compose.yaml build
docker compose --env-file .env -f infra/compose.yaml up -d postgres
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api collaboration-worker
```

单独启停协同 Worker：

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d collaboration-worker
docker compose --env-file .env -f infra/compose.yaml restart collaboration-worker
docker compose --env-file .env -f infra/compose.yaml stop collaboration-worker
```

Worker 使用 `restart: unless-stopped`，并依赖健康的 PostgreSQL。沿用与 API、成果 Worker 相同的 `artifact-data` 和 `data-assets` 卷，不创建第二套成果存储。

重建 Worker 前先查看日志：

```powershell
docker compose --env-file .env -f infra/compose.yaml logs --tail 300 collaboration-worker
```

## 3. 健康检查

检查容器和依赖：

```powershell
docker compose --env-file .env -f infra/compose.yaml ps collaboration-worker postgres
```

执行进程级健康检查：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec collaboration-worker `
  python -m app.process_health collaboration
```

返回码 `0` 表示主进程存在；返回码非零时应先检查容器日志，不要通过删除 Outbox 或数据库记录来恢复。

Compose 健康检查的预期配置为：

```text
command: python -m app.collaboration.worker
healthcheck: python -m app.process_health collaboration
restart: unless-stopped
```

## 4. 事件与任务核验

查看指定事件的七个工作组任务数量：

```sql
SELECT workgroup_code, count(*) AS task_count,
       count(*) FILTER (WHERE status = 'completed') AS completed_count
FROM collaboration_tasks
WHERE event_id = '<事件 UUID>'
GROUP BY workgroup_code
ORDER BY workgroup_code;
```

核验总任务数：

```sql
SELECT count(*) AS total_tasks,
       count(DISTINCT workgroup_code) AS workgroup_count
FROM collaboration_tasks
WHERE event_id = '<事件 UUID>';
```

预期为 `60` 条任务、`7` 个工作组。外省且距本市边界超过 20 公里的事件只记录并通知，不启动正常评估任务。

查看任务生成 Outbox：

```sql
SELECT id, event_id, revision_id, status, attempt_count,
       available_at, last_error
FROM event_lifecycle_outbox
WHERE event_id = '<事件 UUID>'
  AND trigger_type = 'collaboration.requested'
ORDER BY created_at DESC;
```

查看成果投影 Outbox：

```sql
SELECT id, event_id, task_id, event_type, status, attempt_count,
       available_at, last_error
FROM collaboration_projection_outbox
WHERE event_id = '<事件 UUID>'
ORDER BY created_at DESC;
```

核验全部专业成果绑定：

```sql
SELECT count(DISTINCT deliverable_code) AS distinct_outputs
FROM collaboration_task_deliverables d
JOIN collaboration_tasks t ON t.id = d.task_id
WHERE t.event_id = '<事件 UUID>'
  AND d.requirement_kind = 'automatic_artifact';
```

预期至少包含既有成果目录中的 39 个成果键；其中前 27 个为专业图件，后 12 个为报告或演示稿。

## 5. 单事件投影重建

仅当任务、成果或到岗数据已正确写入，但指挥大厅投影未同步时执行。先从日志和两个 Outbox 确认 Worker 是否仍在处理有效租约。

以下命令使用当前数据库事务重建一个事件的投影：

```powershell
$code = @'
import asyncio
from uuid import UUID

from app.command_hall.projector import CommandHallProjector
from app.db import SessionFactory


async def main() -> None:
    event_id = UUID("<事件 UUID>")
    async with SessionFactory() as session:
        async with session.begin():
            result = await CommandHallProjector().refresh_event(
                session,
                event_id,
            )
            print(result)


asyncio.run(main())
'@
$code | docker compose --env-file .env -f infra/compose.yaml run --rm -T api python -
```

重建只更新投影版本和分组快照，不修改任务、成果版本、发布历史、原始报文或评估截止时间。

## 6. Dead-letter outbox

查看协同任务生成和投影死信：

```sql
SELECT 'event_lifecycle' AS outbox, id, event_id, revision_id AS ref_id,
       status, attempt_count, available_at, last_error
FROM event_lifecycle_outbox
WHERE trigger_type = 'collaboration.requested'
  AND status = 'dead_letter'

UNION ALL

SELECT 'projection' AS outbox, id, event_id, task_id AS ref_id,
       status, attempt_count, available_at, last_error
FROM collaboration_projection_outbox
WHERE status = 'dead_letter'
ORDER BY available_at;
```

确认底层故障已消除后，才能把指定投影死信恢复为 `pending`。该操作只重置调度状态，不伪造成功：

```sql
WITH replayed AS (
  UPDATE collaboration_projection_outbox
  SET status = 'pending',
      attempt_count = 0,
      available_at = now(),
      lease_expires_at = NULL,
      dispatched_at = NULL,
      last_error = NULL,
      updated_at = now()
  WHERE id = '<投影 Outbox UUID>'
    AND status = 'dead_letter'
  RETURNING id
)
SELECT id FROM replayed;
```

必须返回恰好一行。事件生命周期 Outbox 的重放还必须核对 `collaboration.requested` 的 `event_id` 和 `revision_id`，不得绕过原始报文和修订链。

## 7. 通知降级核验

查看指定事件的通知投递：

```sql
SELECT id, task_id, recipient_user_id, intent_type, channel, status,
       attempt_count, sent_at, last_error
FROM collaboration_notification_deliveries
WHERE event_id = '<事件 UUID>'
ORDER BY created_at, id;
```

外部渠道不可用、未配置适配器或达到最大尝试次数后，外部投递应变为 `fallback_sent`，同一事件、任务、接收人和去重键必须存在一条 `in_app` 投递或站内消息。真实正式事件期间，测试或演练事件的外部通知会被抑制并降级到站内消息。

验收命令：

```sql
SELECT channel, status, count(*)
FROM collaboration_notification_deliveries
WHERE event_id = '<事件 UUID>'
GROUP BY channel, status
ORDER BY channel, status;
```

`fallback_sent` 不是删除或忽略通知；站内消息仍须在平台中可见。

## 8. 测试与演练标识

事件标识来自事件类型，不在任务或成果版本中覆盖：

```sql
SELECT e.id, e.event_type, r.revision_kind, e.place
FROM earthquake_events e
JOIN earthquake_revisions r ON r.id = e.current_revision_id
WHERE e.id = '<事件 UUID>';
```

测试事件必须显示“测试”，演练事件必须显示“演练”。任务列表、任务详情、成果面板和指挥大厅标题均读取同一事件类型。不得通过编辑任务标题、成果文件名或投影 JSON 去除标识。

测试和演练成果仍进入正常版本链，保留 400 天或按既有保留策略处理；正式事件数据不得被测试成果覆盖。

## 9. 自动版与人工修订版

同一个成果可以同时保留自动版和人工修订版。当前发布版只能有一条，历史版本不能删除。

查看版本和当前发布版：

```sql
SELECT d.id AS deliverable_id,
       d.deliverable_code,
       v.version_no,
       v.source_kind,
       p.published_at,
       p.published_by,
       p.superseded_at
FROM collaboration_task_deliverables d
JOIN collaboration_tasks t ON t.id = d.task_id
LEFT JOIN collaboration_deliverable_versions v
  ON v.deliverable_id = d.id
LEFT JOIN collaboration_deliverable_publications p
  ON p.deliverable_id = d.id
 AND p.version_id = v.id
WHERE t.event_id = '<事件 UUID>'
  AND d.deliverable_code = 'doc.rapid_brief'
ORDER BY v.version_no, p.published_at;
```

验收要求：

- 自动版和人工修订版均可查询。
- 人工修订版发布后，当前发布版为人工版。
- 自动版仍保留在历史版本中，不能被覆盖或删除。

前端可在 `/tasks/<事件 UUID>` 打开快速评估简报任务，确认“成果与当前发布版”同时显示“系统自动版”和“人工修订版”，并确认人工修订版带有“当前发布版”。

## 10. 大屏验收

后端夹具准备好测试事件、39 项成果、60 条任务和双版本后，运行：

```powershell
$env:E2E_SUPERADMIN_PASSWORD = "<验收账号密码>"
cd frontend
npm run test:e2e -- e2e/command-hall.spec.ts e2e/workgroup-tasks.spec.ts
```

必须覆盖：

- `7680 x 2430` 主屏，七个工作组卡片全部可见，无卡片互相遮挡或页面溢出。
- `1920 x 1080` 管理终端，七个工作组、任务总览和同步状态可见。
- 测试标识不可移除。
- SSE 正常时显示实时更新；SSE 返回错误后显示 `5 秒轮询更新`。
- 权限错误显示无权查看状态；服务错误显示可重试错误状态。
- 工作组任务页显示 60 条任务和自动/人工双版本。

本机浏览时可访问：

```text
http://localhost:5173/command-hall/<事件 UUID>
```

## 11. 与既有评估/成果时限的边界

协同 Worker、投影刷新和通知调度不得修改以下边界：

- 正式报或更正报入库是评估和成果生产的基准时刻。
- 上海市行政区域内 `5.0` 至 `5.9` 级事件在 `T1 + 300 秒` 内完成全部 27 类图件和全部报告。
- 既有成果终态保持 `28 complete + 11 degraded`、`0 failed`、`0 timeout`。
- 不得通过协同 Outbox、测试事件或投影重建延长评估截止时间。

任何需要改变上述时限或产物范围的工作，必须单独评审并形成新的规格和迁移方案，不能作为本手册的运维操作执行。
