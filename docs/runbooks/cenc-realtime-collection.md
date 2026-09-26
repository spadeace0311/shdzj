# CENC 实时采集运行手册

本文面向部署、值守和交接人员，说明 FAN 主链路、Wolfx 常驻备用链路、生命周期 Outbox、数据库故障 spool 和死信重放的操作方法。

## 1. 启动

### 1.1 复制环境变量模板

从仓库根目录执行：

```powershell
Copy-Item .env.example .env
```

### 1.2 在迁移和启动前替换全部占位值

编辑 `.env`，至少完成以下替换。不要在实际值仍为模板占位值或空值时继续执行迁移和启动命令。

- `POSTGRES_PASSWORD`：改为独立的高强度数据库密码。
- `DATABASE_URL`：使用同一个数据库密码，并按 URL 规则编码；Compose 内数据库主机必须是 `postgres`。
- `JWT_SECRET`：改为至少 16 个字符的独立随机值，不得继续使用模板值。
- `SUPERADMIN_INITIAL_PASSWORD`：改为至少 16 个字符的独立随机值。
- `FAN_APP_ID`：填写有效的 FAN Studio 客户端标识；留空时才回退到 `CENC_APP_ID`。
- `FAN_API_KEY`：填写有效的 FAN Studio 密钥。

collector 在 Compose 中强制启用。没有有效 FAN 客户端标识和 `FAN_API_KEY` 时，collector 不是可安全启动状态，也不会形成可验证的主备采集链路。没有凭据时可以运行数据库迁移、API 和不含 collector 的测试，但不得把 collector 的启动失败误判为实时采集已部署。

### 1.3 启动数据库、迁移和完整服务

仅在 1.2 的数据库、JWT、超级管理员和 FAN 值均已替换且有效后执行：

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d api collector frontend
docker compose --env-file .env -f infra/compose.yaml ps
docker compose --env-file .env -f infra/compose.yaml logs -f collector
```

API、collector 和 frontend 均可独立重启。只重启 collector 不会停止 API：

```powershell
docker compose --env-file .env -f infra/compose.yaml restart collector
```

只停止 collector：

```powershell
docker compose --env-file .env -f infra/compose.yaml stop collector
docker compose --env-file .env -f infra/compose.yaml start collector
```

## 2. 凭据与配置

- `FAN_APP_ID` 是规范的 FAN Studio 客户端标识。
- `CENC_APP_ID` 是兼容回退别名；仅当 `FAN_APP_ID` 为空或全为空白时使用。它仍是 FAN 客户端标识，不是 Kanameishi 私有接口凭据。
- `FAN_API_KEY` 是 collector 必需的 FAN Studio 密钥。
- `CENC_API_BASE_URL` 当前仅为未来私有 CENC 适配器预留，collector 不读取它发起请求。
- 缺少 FAN 客户端标识或 `FAN_API_KEY` 会阻止 collector 启动；API、Alembic 和普通后端测试不要求这些凭据。
- 密钥只保存在受控 `.env` 中，不得提交、打印、写入工单或放入日志。

## 3. 健康核验

查看 Compose 状态：

```powershell
docker compose --env-file .env -f infra/compose.yaml ps
```

检查 collector 健康端点：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec collector python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=3).read().decode())"
```

至少一个 provider 为 `healthy` 或 `degraded` 时端点返回 HTTP `200`。FAN 正常表示主链路正常；只有 Wolfx 正常时总体状态为降级，但备用链路仍可接收入库。

查看运行状态：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql -U earthquake -d earthquake -c "SELECT provider, state, connected, last_http_status, last_message_at, last_success_at, last_processed_source_time, consecutive_failures, reconnect_count, last_error, updated_at FROM collector_runtime_state ORDER BY provider;"
```

如果 `.env` 修改了 PostgreSQL 用户或数据库名，请同步替换命令中的 `earthquake`。

## 4. 边界导入

边界 GeoJSON 必须挂载到 API 容器内，并使用绝对 HTTPS 来源 URI。以下示例把宿主机文件只读挂载到 `/config/region.geojson`：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm -v "<绝对-GeoJSON-路径>:/config/region.geojson:ro" api python -m app.regions.cli import --file /config/region.geojson --version <版本> --name <名称> --source-uri <https-来源-URI> --activate
```

`--activate` 会把新版本设为唯一活动边界。导入后检查版本和数量：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql -U earthquake -d earthquake -c "SELECT version, name, source_uri, is_active, ST_NumGeometries(geom) AS administrative_parts, ST_NumGeometries(maritime_geom) AS maritime_parts FROM region_boundaries ORDER BY created_at DESC;"
```

无活动边界时，新正式报的区域上下文为 `pending`；需要恢复真实判定时必须先导入并激活边界。

## 5. 生命周期 Outbox

自动速报只创建或更新待定事件，不触发评估。首次正式报告记录不可变 `T1` 并恰好创建一个 Outbox；后续实质变更的更正报告创建新的 Outbox。

检查最近一条 Outbox：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql -U earthquake -d earthquake -c "SELECT id, event_id, revision_id, trigger_type, trigger_reason, status, attempt_count, created_at, available_at, published_at, last_error FROM event_lifecycle_outbox ORDER BY created_at DESC LIMIT 1;"
```

检查某事件的全部 Outbox：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql -U earthquake -d earthquake -c "SELECT id, revision_id, trigger_type, trigger_reason, status, created_at FROM event_lifecycle_outbox WHERE event_id = '<事件 UUID>' ORDER BY created_at;"
```

## 6. 死信重放

先查看待处理死信：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql -U earthquake -d earthquake -c "SELECT id, provider, lane, source_message_id, error_category, received_at, last_failed_at, left(error_message, 200) AS error_summary FROM collector_dead_letters WHERE status = 'open' ORDER BY last_failed_at;"
```

重放指定死信：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api python -m app.collector.replay --dead-letter <死信-UUID>
```

重放会把持久化的单个事件重新包装为 supervisor 消费的 `{"No1": raw_payload}` 结构，并使用原始 `received_at` 和 `recovery` 触发原因。状态为 `open` 时首次成功变为 `retried`；从 `retried` 或 `resolved` 再次成功才变为 `resolved`。失败时记录恢复为 `open` 并增加错误摘要。

## 7. Spool 与数据库故障恢复

collector 在数据库不可用时先重试，仍失败时把单个 envelope 以 JSON 文件写入 `/var/lib/collector-spool`。检查待恢复文件：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec collector sh -lc "find /var/lib/collector-spool -maxdepth 1 -type f \( -name '*.json' -o -name '*.json.deleting' \) -printf '%TY-%Tm-%TdT%TH:%TM:%TS %f\n' | sort"
```

数据库恢复后的正常路径是：collector 下一次 drain 按 `received_at` 顺序重放 spool，成功后删除文件，再继续接收 live 数据。

spool 删除是两阶段状态转换。`remove()` 先把 `*.json` 原子重命名为 `*.json.deleting`，再对该目录执行 `fsync`。`*.json.deleting` 在重启时仍按待处理报文加载并重放；只有这次目录 `fsync` 成功才算删除已持久提交。重命名或首次目录 `fsync` 失败时 collector 进入 `critical` 并停止，不报告成功，报文仍可从 `*.json` 或 `*.json.deleting` 恢复。

持久删除提交后，collector 才删除 `*.json.deleting` 并再次同步目录。若后续 unlink 或最终目录 `fsync` 失败，removal 错误仍会向上传播并停止 collector，但此时不能声称原文件名仍保留：coordinator 的入库事务已经提交，且首次目录 `fsync` 已持久提交删除。重启后该报文可能已不存在，也可能仍以 `*.json.deleting` 出现并按语义指纹幂等重放；两种状态都不会丢失报文，也不会静默报告成功水位。

Windows 不提供 POSIX 目录 `fsync`，collector 在该平台不会打开目录。原子重命名、unlink 和恢复标记共同保证：崩溃后报文要么仍以待处理文件出现并重放，要么其入库事务和删除提交已经生效。支持目录 `fsync` 的 POSIX 文件系统仍严格传播所有打开、同步、重命名和删除错误。

恢复演练：

```powershell
docker compose --env-file .env -f infra/compose.yaml stop postgres
# 等待 collector 日志出现数据库重试或 spool 记录，然后确认 spool 文件存在。
docker compose --env-file .env -f infra/compose.yaml start postgres
docker compose --env-file .env -f infra/compose.yaml restart collector
docker compose --env-file .env -f infra/compose.yaml logs --since=5m collector
```

运行时 drain 若持续遇到数据库故障，collector 会进入 `critical` 并停止，这是有意的终止行为。数据库恢复后必须重启 collector；启动阶段会先按 `received_at` 顺序排空 spool，确认成功后才启动 live 接收。不得在 spool 尚有待恢复报文时强制删除卷或文件。

如果 spool 写入失败或容量耗尽，supervisor 同样进入 `critical` 并停止。继续接收会让内存队列之后的新报文无处持久化，造成静默丢失，因此系统选择停止而不是丢弃。

## 8. 轮换 FAN API Key

无需修改源码：

1. 在受控 `.env` 中更新 `FAN_API_KEY`。如需同时轮换客户端标识，更新 `FAN_APP_ID`。
2. 强制重建 collector，使新环境变量生效：

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d --force-recreate collector
```

3. 检查 `collector_runtime_state.fan` 的连接、认证和更新时间。旧密钥不被日志或状态接口返回。

`docker compose restart collector` 不会重新读取 `.env`，轮换凭据必须使用 `up -d --force-recreate collector`。

## 9. 故障诊断

| 现象 | 检查 | 处置 |
| --- | --- | --- |
| FAN `auth_fail` | `collector_runtime_state.fan.last_error`、`FAN_APP_ID`/`CENC_APP_ID` 和 `FAN_API_KEY` | 校正受控 `.env`，强制重建 collector；不得输出密钥 |
| WebSocket 传输停滞 | FAN 的 `state`、`stale_after`、`reconnect_count`、collector 日志；`last_message_at` 只表示业务报文时间，传输活跃时间当前仅保存在进程内 | 检查 FAN 服务和外网；Wolfx 应继续提供备用可用性 |
| Wolfx HTTP 故障 | Wolfx 的 `last_http_status`、`last_error`、连续失败和重连次数 | 检查 `WOLFX_CENC_URL`、DNS、代理、证书和上游 HTTP 状态 |
| 数据库故障 | PostgreSQL 状态、API/collector 日志、spool 文件 | 恢复数据库；重启 collector，确认 spool 排空 |
| 双链路严重 | 两个 provider 均为 `degraded`/`critical`、spool 数量和健康端点 | 优先恢复至少一个 provider；若 spool 写入失败，不得继续运行 |

常用日志命令：

```powershell
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 collector
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 postgres
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 api
```

## 10. 已知边界

- 真实 FAN/Wolfx 上游验证需要用户提供的密钥和外网连接；没有这些条件时不得根据单元测试推断真实链路成功。
- FAN 是 WebSocket 主链路，Wolfx HTTP 是常驻备用链路；单一 FAN 故障不阻止 Wolfx 正式报入库。
- 自动速报永不触发评估；首次正式报设置不可变 `T1` 并创建一个 Outbox，更正报按修订创建新的 Outbox。
- 运行 spool 按 `received_at` 排序；来源时间排序只用于显式 recovery 批次。
- 移动端不是当前支持目标。
