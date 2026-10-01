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

保留任务每日运行一次并记录各模式删除数量。对象删除失败时保留数据库记录，供下一轮重试。

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
