# 烈度评估运行手册

本文面向烈度评估子系统的部署、值守和交接人员，说明模型烈度、仪器烈度、融合烈度的运行、核验、更正、告警和恢复方法。

## Scope

当前已实现并纳入验收范围：

- `intensity.model`：基于上海 2019 参数和 1 km 网格计算模型烈度场。
- `intensity.instrument`：读取或降级处理仪器烈度产品；默认 provider 为 `unavailable`。
- `intensity.fusion`：对模型与仪器场做逆方差融合；仪器不可用时输出 `model_only`/`F3`。

当前明确不在本阶段范围：

- 受灾人口、人员伤亡、房屋破坏、经济损失等损失算法。
- 27 类图件、专题图模板和报告文件生成。
- 报告审批、工作组成果上传和协同。
- AI 知识库问答。

## Required Configuration

烈度算法依赖以下配置，修改前必须核验版本和校验和：

- `INTENSITY_PARAMETERS_PATH`，默认 `/config/intensity/shanghai-2019.yaml`。
- `INTENSITY_REGION_PROFILE_PATH`，默认 `/config/intensity/shanghai-region.yaml`。
- `ASSESSMENT_WORKFLOW_SAFETY_TIMEOUT_SECONDS`，默认 `1800`。

默认网格使用 `EPSG:32651`、`1000 m` 分辨率。距离计算使用 WGS 84 球面测地距离，连续值以双精度存储，展示时四舍五入到 `0.1` 烈度度。

仪器 provider 由 `config/intensity/shanghai-region.yaml` 的 `instrument_provider` 决定。当前仓库默认 `unavailable`；接入真实 provider 时必须返回经过验证的 GRID 产品，并同时更新 provider 实现和该配置。

烈度模型运行要求当前修订存在非空 `region_boundary_version`。通过 collector coordinator 接入正式报时会由区域解析器写入当前活跃边界版本；直接调用 API `/api/v1/ingest/formal` 的 `regionContext` 不携带边界版本，需由调用方先完成区域解析或补入活跃版本。

## Starting Services

从仓库根目录执行：

```powershell
docker compose --env-file .env -f infra/compose.yaml build
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal-postgres temporal
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker temporal-ui
```

如果长时间运行的 Worker 或 Dispatcher 是在依赖更新前创建的，必须重建并重创容器，否则进程内存中仍是旧代码或缺少新依赖：

```powershell
docker compose --env-file .env -f infra/compose.yaml build api temporal-worker assessment-dispatcher
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker
```

检查服务：

```powershell
docker compose --env-file .env -f infra/compose.yaml ps
```

## Verifying a Formal-Report Run

`T1` 是第一条正式报的首次入库时间。每次更正的五分钟告警截止时间以该更正报自身的入库时间为起点，而不是沿用首报时间。

正式报或更正报经事件接入链路进入 `event_lifecycle_outbox` 后，Dispatcher 发布到 Temporal，Worker 执行 `prepare_assessment`、三个烈度 Activity 和 `finalize_assessment`。

查找最近评估运行：

```sql
SELECT run_no, status, report_ingested_at, deadline_at, deadline_exceeded_at,
       algorithm_bundle_version, superseded_by_run_id
FROM assessment_runs
ORDER BY created_at DESC
LIMIT 5;
```

核验某事件的运行：

```sql
SELECT id, revision_id, run_no, status, report_ingested_at,
       deadline_basis_at, deadline_at, deadline_exceeded_at,
       algorithm_bundle_version, superseded_by_run_id
FROM assessment_runs
WHERE event_id = '<事件 UUID>'
ORDER BY run_no DESC;
```

正常完成条件：

- `intensity.model` 为 `succeeded`。
- `intensity.fusion` 为 `succeeded`。
- 后六个损失、报告和协同任务为 `skipped`，原因 `out_of_phase_scope`。
- 运行状态为 `completed`，`algorithm_bundle_version` 为 `intensity-v1`。

仪器任务有两种正确结果：

- 正常降级路径：`intensity.instrument` 为 `succeeded`，产品状态是
  `unavailable` 或 `invalid`，融合回退到 `model_only`/`F3`。
- 意外 Activity 异常路径：`intensity.instrument` 为 `failed`，但运行仍可
  `completed`，因为完成运行只强制要求模型和融合成功。此时融合同样回退到
  `model_only`/`F3`；必须查看任务尝试和 `last_error`，不能把失败任务误当成
  正常降级。

## Inspecting Model, Instrument, and Fusion Products

查看任务：

```sql
SELECT task_key, status, attempt_count, output_checksum, last_error
FROM assessment_tasks
WHERE run_id = '<run-id>'
ORDER BY sequence;
```

查看产品：

```sql
SELECT product_type, status, quality_grade, coverage_ratio,
       output_checksum, completed_at
FROM intensity_field_products
WHERE run_id = '<run-id>'
ORDER BY product_type;
```

查看栅格：

```sql
SELECT product_id, ST_Width(rast), ST_Height(rast), ST_SRID(rast)
FROM intensity_rasters
WHERE product_id IN (
  SELECT id FROM intensity_field_products WHERE run_id = '<run-id>'
);
```

每个可用或部分产品都必须有 `intensity_rasters` 行、非空 `output_checksum`、`band_manifest` 和 `statistics`。算法、参数、网格、区域、来源、校验和、质量和时间元数据均保留在产品及任务/尝试表中。

## Interpreting Quality and Coverage

产品状态：

- `available`：完整有效产品。
- `partial`：仪器产品只有部分有效格点。
- `unavailable`：仪器未连接或 provider 返回不可用。
- `invalid`：仪器产品格式、网格、有限性或来源验证失败。
- `stale`：产品过期。

模型产品覆盖率为 `1.0`。融合质量：

- `F1`：覆盖率至少 `0.90` 且融合 sigma 的 p95 不超过 `0.75`。
- `F2`：覆盖率至少 `0.50` 且融合 sigma 的 p95 不超过 `1.25`。
- `F3`：其余情况，包括仪器不可用或全部质量码无效。

仪器不可用时融合模式为 `model_only`，质量为 `F3`，覆盖率为 `0.0`。这不代表运行失败。

## Correcting a Report

更正报会建立新的 `run_no`，并把旧运行标记 `superseded_by_run_id`、`superseded_at`。在新更正完成前，事件的 `effective_assessment_run_id` 仍指向旧运行，旧完整产品作为带标签的 fallback 继续对外提供。

核验有效运行指针：

```sql
SELECT e.id, e.latest_assessment_run_id, e.effective_assessment_run_id,
       r.run_no, r.status
FROM earthquake_events e
LEFT JOIN assessment_runs r
  ON r.id = e.effective_assessment_run_id
WHERE e.id = '<事件 UUID>';
```

只有最新运行且模型、融合均成功后，`complete_run` 才会把 `effective_assessment_run_id` 更新为该最新运行。

## Handling Deadline Exceeded Alerts

正式报或更正报的 `deadline_basis_at` 等于该报入库时间，`deadline_at` 为入库时间加 300 秒。Workflow 会在截止时间后执行 `mark_deadline_exceeded`，但不会取消仍在执行的 Activity。

```sql
SELECT run_no, status, report_ingested_at, deadline_basis_at, deadline_at,
       deadline_exceeded_at, completed_at, last_error
FROM assessment_runs
WHERE event_id = '<事件 UUID>'
ORDER BY run_no DESC;
```

`deadline_exceeded_at` 非空表示错过五分钟业务截止。技术安全超时为 1800 秒，Activity 达到该限制时由 Temporal 停止，不无限执行。

## Handling Model or Fusion Failure

模型和融合是完成运行的必要条件。任一活动最终失败后，Workflow 以 `failed` 收尾并调用 `fail_run`。

处理顺序：

1. 查询任务和尝试错误，不把运行改回 `completed`。
2. 查看 Worker 日志和 Temporal Workflow 历史。
3. 确认输入是否缺活跃边界、参数或 provider 配置。
4. 修复根因后让新报文重新触发，或通过正确恢复流程重放 Outbox；不得手工伪造产品校验和。

```powershell
docker compose --env-file .env -f infra/compose.yaml logs --tail 300 temporal-worker
```

## Handling Instrument Degradation

仪器缺失、未连接、provider 异常或产品验证失败都不会使运行失败。服务保存 `unavailable` 或 `invalid` 产品，融合回退到 `model_only`/`F3`。

核验：

```sql
SELECT status, statistics->>'source', statistics->>'reason', coverage_ratio
FROM intensity_field_products
WHERE run_id = '<run-id>'
  AND product_type = 'instrument';
```

如需接入或禁用仪器 provider，修改 `config/intensity/shanghai-region.yaml` 的 `instrument_provider` 并重启 Worker。禁止把未验证产品标为 `available`。

### Instrument Activity Transient Deadlock

真实联调中观察到 `intensity.instrument` 首次尝试在 `assessment_runs` 的
`FOR UPDATE` 上触发 `DBAPIError`/`DeadlockDetectedError`。Temporal 重试策略为
`maximum_attempts=3`、初始间隔 `2s`、最大间隔 `8s`、退避系数 `4.0`，第二次尝试
成功。按操作风险监控：

```sql
SELECT t.task_key, a.attempt_number, a.status, a.error_category, a.error_summary
FROM assessment_tasks t
LEFT JOIN assessment_task_attempts a ON a.task_id = t.id
WHERE t.run_id = '<run-id>'
ORDER BY t.sequence, a.attempt_number;
```

偶发一次并成功重试不改变正常完成语义。若重复出现或三次尝试均失败，应作为模型
与仪器 Activity 的锁顺序并发问题处理，并检查 Postgres 日志与 Temporal 历史；
不得把 `dead_letter` 或运行状态手工改为成功来掩盖失败。

## Database and Raster Checks

烈度栅格保存在 PostgreSQL/PostGIS 的 `intensity_rasters` 中，不是外部文件。核验 PostGIS raster 扩展：

```sql
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('postgis', 'postgis_raster')
ORDER BY extname;
```

抽查栅格元数据和值域：

```sql
SELECT r.product_id, r.checksum, r.width, r.height, r.srid,
       p.product_type, p.status, p.output_checksum,
       p.statistics->>'minimum' AS minimum,
       p.statistics->>'maximum' AS maximum
FROM intensity_rasters r
JOIN intensity_field_products p ON p.id = r.product_id
WHERE p.run_id = '<run-id>'
ORDER BY p.product_type;
```

`intensity_rasters` 行必须与可用/部分产品一一对应。缺栅格、校验和不一致或 `ST_SRID` 不是 `32651` 都应视为故障。

## Backup and Restore Notes

备份必须包含：

- `assessment_runs`、`assessment_tasks`、`assessment_task_attempts`。
- `intensity_field_products`、`intensity_rasters`。
- 对应 `earthquake_events`、`earthquake_revisions`、`raw_messages` 和 `event_lifecycle_outbox`。
- `region_boundaries` 及活跃边界版本。

`intensity_rasters` 是数据库内 raster 数据，不能只备份普通表而漏掉该表。恢复后先核对迁移头、活跃边界版本和 raster 扩展，再重启 Worker 与 Dispatcher。

## Verification Commands

以下命令在 `650d7b6` 分支于 2026-09-28 上海时间完成，使用真实 PostgreSQL/PostGIS 与 Temporal 环境。

```powershell
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -m performance tests/test_intensity_performance.py -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

实际结果：

- Compose 配置退出码 `0`，无输出。
- 迁移头：`0011_intensity_assessment (head)`。
- 后端全量测试：`453 passed, 70 warnings in 208.08s (0:03:28)`。
- 全网格性能：验收上限 `model<=60s`、`instrument<=60s`、`fusion<=90s`；
  `1 passed, 6 warnings in 5.87s`，补充 `-s` 实测为
  `model=0.874s instrument=0.617s fusion=2.044s`。
- Ruff：`All checks passed!`。
- 前端测试：`Test Files 8 passed (8)`，`Tests 34 passed (34)`，`Duration 3.60s`。
- 前端类型检查：退出码 `0`，无错误输出。
- 前端生产构建：`50 modules transformed`，`built in 2.27s`。
