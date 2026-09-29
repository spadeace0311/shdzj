# Loss Assessment Runbook

## Scope

本手册覆盖上海损失评估链路的启动、运行、读取、校正、故障恢复、PostGIS raster 核验和备份恢复。损失评估在 `intensity.fusion` 成功后才开始，当前实现房屋破坏、受灾人口、人员伤亡、直接经济损失和应急资源需求五类业务产品，并写入 `loss.validate` 质量校验产品。

当前生产参数集仍是 `reference_uncalibrated`。它允许完成固定场景联调和公开 API/地图，但正式发布前必须逐项补齐来源与上海校核证据。

## Required Data Asset Versions

损失评估通过 `data_asset_snapshots` 冻结运行创建时的已发布资产版本。下列 required 资产缺少已发布版本时，运行会记录 `missing_required`；损失服务在读取锁定版本失败时按对应任务失败处理。

必需资产：

- `shanghai.admin.city`
- `shanghai.admin.county`
- `shanghai.admin.town`
- `shanghai.population.town`
- `shanghai.building.town`
- `shanghai.economy.county`
- `shanghai.loss.parameters`

可选资产：

- `shanghai.fault`
- `shanghai.gdp.raster`
- `shanghai.dem.raster`

核验某个运行冻结的资产版本：

```sql
SELECT r.id AS run_id,
       r.data_asset_snapshot_fingerprint,
       r.data_asset_snapshot_result,
       das.asset_key,
       das.version,
       das.checksum,
       das.role,
       das.required
FROM assessment_runs r
LEFT JOIN data_asset_snapshots das ON das.run_id = r.id
WHERE r.id = '<run-id>'
ORDER BY das.asset_key;
```

## Required Parameter Sources

默认参数源：

- 损失模型与参数：`/config/loss/shanghai-reference-uncalibrated.yaml`
- 上海损失区域配置：`/config/loss/shanghai-region.yaml`

区域配置确定：

- 计算栅格：`EPSG:32651`，`1000 m`
- 输出栅格：`EPSG:4326`
- 空间分配规则：`town-uniform-v1`
- 最小城镇覆盖率：`0.95`
- 1 km 残差复核阈值：`0.01`
- 受灾人口起始烈度：`6.0`

`reference_uncalibrated` 参数集在逐项系数具备已审核来源、版本、适用震级烈度、单位、有效日期和审核人之前不得升级为校准状态。缺少单个资源系数时，该资源返回 `unavailable`，不是零。

## Starting Services

从仓库根目录执行：

```powershell
docker compose --env-file .env -f infra/compose.yaml build
docker compose --env-file .env -f infra/compose.yaml up -d postgres temporal-postgres temporal
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api assessment-dispatcher temporal-worker temporal-ui
```

检查迁移头：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic current
```

应为：

```text
0014_loss_assessment (head)
```

检查服务状态：

```powershell
docker compose --env-file .env -f infra/compose.yaml ps
```

## Running a Fixed Shanghai Scenario

通过正式报接入链路提交一条正式 CENC 报文。Dispatcher 发布 Outbox，Worker 依次执行：

```text
intensity.model
intensity.instrument
intensity.fusion
loss.population
loss.casualties
loss.buildings
loss.economic
loss.resources
loss.validate
report.rapid_assessment
workgroup.response_tasks
```

前八个任务必须成功才能完成运行。`report.rapid_assessment` 与 `workgroup.response_tasks` 仍为 `skipped`。

运行完成后，认证读取：

```text
GET /api/v1/assessments/runs/{run_id}/loss
```

该请求返回 HTTP 200 并包含六类已发布产品时，视为固定场景验收完成。硬验收时间是 `report_ingested_at` 到该请求返回 HTTP 200 的经过时间，上限 300 秒。

## Reading Building, Population, Casualty, Economic, and Resource Products

查询运行任务：

```sql
SELECT sequence, task_key, status, attempt_count, input_fingerprint,
       output_checksum, last_error
FROM assessment_tasks
WHERE run_id = '<run-id>'
ORDER BY sequence;
```

查询六类损失产品：

```sql
SELECT product_type, status, quality_grade, calibration_status,
       coverage_ratio, partial_scope, needs_review,
       spatialized_estimate, output_checksum
FROM loss_products
WHERE run_id = '<run-id>'
ORDER BY product_type;
```

查询产品指标：

```sql
SELECT area_scope, area_code, metric_key, value_type,
       numeric_value, unit, quality_grade
FROM loss_metric_values
WHERE product_id = '<product-id>'
ORDER BY area_scope, area_code, metric_key, value_type;
```

API 合同：

```text
GET /api/v1/assessments/runs/{run_id}/loss
GET /api/v1/assessments/runs/{run_id}/loss/products/{product_type}
GET /api/v1/assessments/runs/{run_id}/loss/areas?scope=city|county|town
GET /api/v1/assessments/runs/{run_id}/loss/artifact?product_id=<product-id>[&band=<band>]
GET /api/v1/assessments/runs/{run_id}/loss/artifact/{product_id}/{band}/{z}/{x}/{y}.png
```

## Interpreting Quality, Calibration, and Coverage

产品质量等级：

- `L1`：完整校核结果；`reference_uncalibrated` 当前不得发布。
- `L2`：可复核的完整或部分结果。
- `L3`：需复核或存在回退/降级的结果。
- `L0`：阻断性验证失败。

校准状态：

- `calibrated`
- `reference_uncalibrated`
- `uncalibrated`

生产运行当前保持 `reference_uncalibrated`。`needs_review` 为真、覆盖率低于 `0.95`、`partial_scope` 为真、使用了回退模型、数据资产过期或验证存在阻断问题时，应人工复核。

## Interpreting Spatialized 1 km Results

城镇结果是权威结果。1 km 格网结果由 `town-uniform-v1` 规则从城镇结果派生。只有 `spatialized_estimate=true` 且存在 `loss_product_rasters` 的产品才可加载格网 PNG。

核验 raster 元数据：

```sql
SELECT product_id, raster_version, ST_Width(rast), ST_Height(rast),
       ST_SRID(rast), spatial_allocation_rule, coverage_ratio
FROM loss_product_rasters
WHERE product_id = '<product-id>';
```

`ST_SRID(rast)` 应为 `32651`。不能把 1 km 派生结果当作高于城镇结果的精度。

## Handling Missing Parameters

缺少单个资源系数时，只把该资源指标标记为 `unavailable`，不得阻塞其他已具备完整系数的资源。生产参数集保持 `reference_uncalibrated`，直到每个系数都有审核来源和上海校核证据。

核验资源缺口：

```sql
SELECT p.product_type, p.status, p.reason, m.metric_key,
       m.value_status, m.numeric_value
FROM loss_products p
LEFT JOIN loss_metric_values m ON m.product_id = p.id
WHERE p.run_id = '<run-id>'
  AND p.product_type = 'resource_demand'
ORDER BY m.metric_key;
```

## Handling Missing Town Data

缺少核心城镇暴露、建筑、人口、经济或行政区划数据时，对应损失产品不可用或验证结果为 `L0`。缺少必需资产的任务不能成功，运行不能标记为 `completed`。不得以零值伪造缺失数据。

核验快照缺失：

```sql
SELECT run_no, status, data_asset_snapshot_result, last_error
FROM assessment_runs
WHERE id = '<run-id>';
```

修复资产发布后，应让新正式报或更正报重新触发新运行；不要手工改写已失败产品的校验和或状态。

## Correcting a Formal Report

更正报创建新的 `run_no`，旧运行标记 `superseded_by_run_id` 和 `superseded_at`。在更正完成前，事件 `effective_assessment_run_id` 继续指向旧完整运行。更正完成不会覆盖旧产品。

核验有效指针：

```sql
SELECT e.id, e.latest_assessment_run_id, e.effective_assessment_run_id,
       r.run_no, r.status
FROM earthquake_events e
LEFT JOIN assessment_runs r
  ON r.id = e.effective_assessment_run_id
WHERE e.id = '<event-id>';
```

## Recovering a Failed Workflow

先查询任务错误和尝试错误，不把失败运行改成 `completed`：

```sql
SELECT t.sequence, t.task_key, t.status, t.attempt_count, t.last_error,
       a.attempt_number, a.status, a.error_category, a.error_summary
FROM assessment_tasks t
LEFT JOIN assessment_task_attempts a ON a.task_id = t.id
WHERE t.run_id = '<run-id>'
ORDER BY t.sequence, a.attempt_number;
```

修复根因后，从 Outbox 或新报重新触发。不得删除 Temporal 历史、清空 Outbox 或伪造产品来绕过故障。

## Inspecting PostgreSQL and PostGIS Raster Results

确认 PostGIS raster：

```sql
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('postgis', 'postgis_raster')
ORDER BY extname;
```

核验 loss raster 行与可用/部分产品一致：

```sql
SELECT r.product_id, r.raster_version, r.checksum,
       ST_Width(r.rast) AS width, ST_Height(r.rast) AS height,
       ST_SRID(r.rast) AS srid, p.product_type, p.status,
       p.spatialized_estimate
FROM loss_product_rasters r
JOIN loss_products p ON p.id = r.product_id
WHERE p.run_id = '<run-id>'
ORDER BY p.product_type;
```

## Backup and Restore

备份必须包含 PostgreSQL 与 PostGIS Raster：

- `assessment_runs`
- `assessment_tasks`
- `assessment_task_attempts`
- `intensity_field_products`
- `intensity_rasters`
- `loss_products`
- `loss_metric_values`
- `loss_product_rasters`
- `data_assets`、`data_asset_versions`、`data_asset_records`、`data_asset_snapshots`
- `earthquake_events`、`earthquake_revisions`、`raw_messages`、`event_lifecycle_outbox`
- `region_boundaries`

不能只备份普通业务表而遗漏 `intensity_rasters` 或 `loss_product_rasters`。恢复后先核对 Alembic 头、PostGIS raster 扩展、活跃边界版本和已发布资产版本，再重启 Worker 与 Dispatcher。

## Verification Commands

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

验收判定：

- Alembic 头为 `0014_loss_assessment`。
- 后端 `pytest -v` 全部通过。
- 固定上海损失性能测试通过；四个阶段预算为诊断指标，硬验收时间是 `report_ingested_at` 到认证 `GET /loss` HTTP 200 的经过时间不超过 300 秒。阶段耗时之和不等同于总耗时。
- Ruff、前端测试、类型检查和构建全部通过。
