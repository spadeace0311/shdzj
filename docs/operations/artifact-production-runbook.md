# 地震应急成果生产运维手册

本手册只包含可执行的检查、恢复和保留操作。命令中不使用数据库密码、连接字符串或令牌。所有 SQL 都从数据库容器或已配置的运维会话中执行。

## 1. 检查超时生产运行

```sql
SELECT id,
       status,
       production_mode,
       launch_mode,
       deadline_basis_at,
       deadline_at,
       completed_at,
       deadline_exceeded_at,
       last_error
FROM artifact_production_runs
WHERE status = 'partial'
   OR deadline_exceeded_at IS NOT NULL
   OR completed_at > deadline_at
ORDER BY created_at DESC;
```

定位未完成任务：

```sql
SELECT t.artifact_key,
       t.output_profile,
       t.status,
       t.attempt_count,
       t.last_error
FROM artifact_production_tasks t
JOIN artifact_production_runs r
  ON r.id = t.production_run_id
WHERE r.id = :production_run_id
  AND t.status IN ('pending', 'ready', 'running', 'timed_out')
ORDER BY t.sequence;
```

## 2. 识别缺失瓦片包

检查目录中的 `manifest.json` 和瓦片索引：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -c "from pathlib import Path; from app.config import settings; root=Path(settings.artifact_basemap_root); print(root); [print(p) for p in root.rglob('manifest.json')]"
```

读取清单但不打印连接串。若缺失包，重新发布离线高德或天地图包，并校验 `checksum` 与清单内容一致。

## 3. 重建单个成果

```sql
SELECT id
FROM earthquake_events
WHERE canonical_source_id = :event_source_id
ORDER BY created_at DESC
LIMIT 1;
```

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -c "from app.artifacts.router import rebuild_artifact; print('use authenticated API')"
```

实际执行使用成果中心“重新生成”按钮，或调用：

```text
POST /api/v1/events/{event_id}/artifacts/{artifact_key}/rebuild
```

重生成会创建独立 `standalone` 运行，不会覆盖完整运行进度。

## 4. 重建完整运行

确认当前修订后，通过评估 Outbox 重新触发正式或纠正评估。生产运行由评估工作流创建并启动成果子工作流。

```sql
SELECT r.id,
       r.status,
       r.generation_scope,
       r.is_current,
       r.superseded_at
FROM artifact_production_runs r
WHERE r.event_id = :event_id
ORDER BY r.generation_seq DESC, r.created_at DESC;
```

## 5. 恢复过期覆盖租约

```sql
SELECT id,
       status,
       lease_expires_at,
       lease_generation,
       attempt_count,
       artifact_id,
       production_run_id
FROM artifact_override_requests
WHERE status = 'processing'
  AND lease_expires_at < now()
ORDER BY created_at DESC;
```

如果数据库已经包含 `artifact_id` 和 `production_run_id`，可把请求标记为成功并复用现有成果；否则应清理过期 `processing` 记录，让客户端重新提交同一幂等键。

## 6. 重建失败后检查旧发布仍为当前

```sql
SELECT p.artifact_key,
       p.output_profile,
       p.artifact_id,
       p.superseded_at,
       g.status AS artifact_status
FROM artifact_publications p
JOIN generated_artifacts g
  ON g.id = p.artifact_id
WHERE p.event_id = :event_id
  AND p.superseded_at IS NULL
ORDER BY p.artifact_key;
```

失败重建不得把旧发布替换掉。若当前发布仍存在且 `artifact_status` 为 `complete` 或 `degraded`，则旧成果保持有效。

## 7. 清理未引用临时对象

先确认候选对象没有数据库记录引用：

```sql
SELECT storage_path, checksum, generated_at
FROM generated_artifacts
WHERE storage_path LIKE 'objects/%';
```

再对每个候选对象执行 `ArtifactStore.delete_unreferenced`。不要在数据库记录删除前删除磁盘对象。

## 7.1 迁移对象存储根目录

每个清理 intent 和 purge receipt 都冻结了创建时的 `storage_namespace`。
`storage_namespace` 默认由解析后的 `ARTIFACT_STORAGE_ROOT` 生成，也可以用
`ARTIFACT_STORAGE_NAMESPACE` 显式指定。namespace 不匹配时 worker 只会
告警并保留 intent，不会使用当前 root 解析旧相对路径。

迁移存储根目录时必须按以下顺序执行：

1. 暂停会创建清理 intent 的写入路径。
2. 等待 `event_object_cleanup_intents` 中旧 namespace 的 `pending`、
   `processing`、`dead_letter` 和 `unprocessable` 行全部处理完毕或转入人工处置。
3. 在旧 root 上运行一次清理 worker，确认没有剩余 intent。
4. 切换 `ARTIFACT_STORAGE_ROOT`；如有必要，同步设置新的
   `ARTIFACT_STORAGE_NAMESPACE`。
5. 启动新 root 的 worker，并检查是否出现
   `cleanup intents require a different storage namespace` 告警。

如果必须保留旧 namespace 的待处理 intent，需要为 worker 配置能解析该
namespace 的旧 store，而不是直接把旧 intent 交给新 root。不要手工把
namespace 改写成当前 root，也不要直接删除仍未完成的 intent。

## 8. 轮换测试和演练成果

系统按生产模式保留：

| 模式 | 保留策略 |
| --- | --- |
| `test` | 400 天后可删除 |
| `drill` | 2 年后可删除 |
| `live` / `manual` | 保留 |

查询过期候选：

```sql
SELECT g.id,
       g.file_name,
       g.generated_at,
       r.production_mode
FROM generated_artifacts g
JOIN artifact_production_runs r
  ON r.id = g.production_run_id
WHERE r.production_mode IN ('test', 'drill')
  AND (
       (r.production_mode = 'test' AND g.generated_at < now() - interval '400 days')
    OR (r.production_mode = 'drill' AND g.generated_at < now() - interval '2 years')
  )
ORDER BY g.generated_at;
```

保留任务每日运行一次并记录 `test`、`drill`、`live`、`manual`、
`replay`、失败数和受保护发布数的结构化计数。对象删除失败时保留数据库
记录，供下一轮重试。

任何仍被 `artifact_publications` 引用的成果都不会被静默删除，包括
`superseded_at` 非空的旧发布。保留任务分别记录
`current_publication` 和 `superseded_publication` 保护原因及计数，
运维人员只有在明确确认不再需要该发布并完成发布关系处理后，才能清理
对应对象。

## 9. 校验存储校验和与磁盘容量

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -c "from pathlib import Path; from app.config import settings; print(Path(settings.artifact_storage_root).resolve())"
```

校验单个文件：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -c "from pathlib import Path; import hashlib; print(hashlib.sha256(Path('PATH').read_bytes()).hexdigest())"
```

检查容量：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api df -h /var/lib/artifacts
```

如果校验和不一致，禁止把该对象作为当前发布继续使用。

## 10. 渲染并发与 worker 拓扑

成果渲染并发由单一 `artifact-worker` 进程内的全局信号量执行，
`ARTIFACT_RENDER_CONCURRENCY` 的有效值就是该进程同时进行真实地图、
文档和演示文稿渲染的上限。`temporal-worker` 只运行评估工作流和评估
活动，不注册成果渲染活动；不要横向扩展 `artifact-worker` 来增加
容量，否则每个进程会重新获得该上限。

Compose 通过 `deploy.replicas: 1` 固定当前部署拓扑。若未来必须扩展
渲染容量，应先引入跨进程分布式信号量或独立队列级容量控制，再调整
该配置。

## 11. 检查 worker 健康状态

两个 Compose health check 都直接检查实际进程参数，不使用
`pgrep -f`，避免健康检查命令行匹配自身。检查状态：

```powershell
docker compose --env-file .env -f infra/compose.yaml ps artifact-worker artifact-dispatcher
```

在容器内验证缺失进程会失败：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -m app.process_health worker
```

该命令应在没有真实 `artifact-worker` 进程时返回非零退出码；在运行中的
artifact worker 容器内执行时，`python -m app.process_health worker`
应返回零。dispatcher 使用同样的检查方式。

## 12. 执行浏览器验收

先设置一次性验收密码，再运行统一入口完成固定测试事件、39 项成果和
浏览器下载验证。密码只从进程环境读取，不写入仓库：

```powershell
$env:E2E_SUPERADMIN_PASSWORD = "<一次性测试密码>"
.\scripts\run-artifact-e2e.ps1 -Scope focused
.\scripts\run-artifact-e2e.ps1 -Scope full
```

脚本先执行 `python -m tests.e2e_fixture`，再运行 Playwright。缺少密码、
固定测试事件、成果或真实服务时，命令必须失败，不得以 skip 作为通过。
