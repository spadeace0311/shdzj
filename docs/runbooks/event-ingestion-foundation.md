# 地震事件接入基础子系统运行手册

本文面向部署、值守和交接人员，说明当前“地震事件接入基础”子系统的启动、接入、核验、轮换和故障诊断方法。

本文只描述仓库中已经实现的代码。当前子系统不是完整的上海市地震应急辅助决策平台。

## 1. 子系统范围

当前子系统包括：

- CENC 报文的自动速报、正式报告和更正报告接入。
- 人工事件、测试事件和演练事件录入。
- 原始 JSON 报文留存、标准事件生成和修订历史追加。
- 上海市制度响应建议与中国地震局应急服务响应建议。
- 本地账号登录、超级管理员初始化和人工事件角色校验。
- React 事件列表、事件详情、人工录入和两级响应建议展示。

当前子系统不包含：

- 数据资产中心和数据目录管理。
- 模型烈度、仪器烈度、融合烈度、人员伤亡、经济损失等评估引擎。
- 27 类图件、报告模板和制图流程。
- 工作组任务协同、成果上传和指挥大厅。
- AI 知识库和问答。
- 备份自动化。
- 每日测试调度器、每日随机测试和人工测试调度。
- 独立的外部 CENC 定时采集器。当前 API 接收调用方提交的 CENC 兼容 JSON 报文。

`CENC_APP_ID` 和 `CENC_API_BASE_URL` 已保留在配置中，但当前代码没有使用这两个值发起联网请求。正式接入 Kanameishi 或其他 CENC 上游时，需要另行实现或部署采集适配器。

## 2. 架构与数据流

```text
CENC 上游或采集适配器
        |
        | POST /api/v1/ingest/auto
        | POST /api/v1/ingest/formal
        | POST /api/v1/ingest/correction
        v
FastAPI 事件接入 API ------> PostgreSQL 16 + PostGIS 3.4
        ^                         |
        |                         +-- raw_messages
        |                         +-- earthquake_events
        |                         +-- earthquake_revisions
        |                         +-- users
        |
        | /api 反向代理
        |
React + Vite 前端
```

关键组件边界：

| 组件 | 当前职责 | 当前边界 |
| --- | --- | --- |
| React/Vite 前端 | 登录、列表、详情、人工录入、两级响应展示 | token 只保存在 React 内存；刷新页面后需要重新登录 |
| FastAPI API | 报文校验、归一化、修订追加、响应规则计算、认证 | `/health` 只表示进程存活，不检查数据库 |
| PostgreSQL/PostGIS | 保存原始 JSON、标准事件、修订快照、用户和空间点 | 不是完整数据资产中心；当前没有修订历史查询 API |
| 响应规则 YAML | 提供制度响应和服务响应分级规则及规则版本 | 修改 YAML 后需要重新发起接入请求；当前不保存规则全文 |

接口访问边界：

| 接口 | 认证 | 说明 |
| --- | --- | --- |
| `GET /health` | 无 | 只检查 API 进程是否响应 |
| `POST /api/v1/auth/login` | 无 | 表单登录并签发 Bearer token |
| `GET /api/v1/auth/me` | Bearer token | 返回当前账号的用户名、角色和工作组，用于验证登录会话 |
| `GET /api/v1/events` | 无 | 返回当前修订事件列表 |
| `GET /api/v1/events/{event_id}` | 无 | 返回当前修订和当前响应建议 |
| `POST /api/v1/ingest/auto` | 无 | 当前未实现接入 API Key |
| `POST /api/v1/ingest/formal` | Bearer token | 仅 `superadmin`、`group_leader`、`group_deputy` |
| `POST /api/v1/ingest/correction` | Bearer token | 仅 `superadmin`、`group_leader`、`group_deputy` |
| `POST /api/v1/events/manual` | Bearer token | 仅 `superadmin`、`group_leader`、`group_deputy` |

自动速报兼容接口仍无认证；正式报、更正报和人工事件接口要求具备已授权角色的
Bearer token。未授权请求返回 `401`，角色不足返回 `403`。当前 API 仍应部署在
受控网络中，不应直接暴露到互联网。

一次接入的主要数据流如下：

1. API 校验 CENC JSON 并归一化事件三要素。
2. 系统按来源和来源事件编号计算稳定事件身份；缺少编号时使用发震时刻、位置和震级等生成备用身份。
3. 原始 JSON 写入 `raw_messages`，并用确定性 checksum 防止同一原始报文重复入库。
4. 系统把本次报文追加到 `earthquake_revisions`，不覆盖旧修订。
5. 自动速报只建立待定事件，不生成响应建议。
6. 正式报告或更正报告携带 `regionContext` 时，系统计算并原子保存制度响应和服务响应建议。
7. 当前事件表只冗余保存当前修订和当前建议；每个修订自己的建议快照仍保存在 `earthquake_revisions`。

`originTime` 是发震时刻。当前子系统没有单独命名为 `T1` 的字段；首次正式报送的接收时刻需要结合 `raw_messages.received_at` 和对应 `earthquake_revisions.created_at` 核验。

## 3. 环境准备与首次启动

以下命令均从仓库根目录执行。

前置条件：

- Windows PowerShell。
- Docker Desktop，包含 Docker Compose v2。
- Git。
- 建议至少 8 GB 可用内存，本机端口 `5173` 和 `8000` 可用。

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

`.env` 已列入 `.gitignore`。不得把真实密码、JWT 密钥或上游凭据提交到仓库。

### 3.1 环境变量

| 变量 | 必填 | 安全示例或约束 |
| --- | --- | --- |
| `POSTGRES_DB` | 是 | 示例：`earthquake` |
| `POSTGRES_USER` | 是 | 示例：`earthquake`；正式部署应使用专用最小权限账号 |
| `POSTGRES_PASSWORD` | 是 | 独立生成的高强度随机值，不得复用 JWT 或管理员密码 |
| `DATABASE_URL` | 是 | `postgresql+asyncpg://<用户>:<URL编码后的密码>@postgres:5432/<数据库>` |
| `API_BIND_HOST` | 否 | 单机默认 `127.0.0.1`；只在明确需要局域网访问时改为其他地址 |
| `API_PORT` | 否 | 默认 `8000`；修改后 API 健康检查地址同步变化 |
| `JWT_SECRET` | 是 | 至少 16 个字符，建议至少 32 字节随机值；禁止使用模板占位值 |
| `JWT_EXPIRE_MINUTES` | 否 | 默认 `480` 分钟 |
| `SUPERADMIN_USERNAME` | 否 | 默认 `superadmin` |
| `SUPERADMIN_INITIAL_PASSWORD` | 是 | 至少 16 个字符；只在数据库中用户名不存在时用于初始化 |
| `VITE_API_PROXY_TARGET` | 否 | Vite 开发服务器 `/api` 代理目标；Compose frontend 未设置时默认 `http://api:8000`，宿主机直接运行且 API 暴露在默认端口时设为 `http://127.0.0.1:8000` |
| `CENC_APP_ID` | 当前可空 | 仅保留配置位；当前代码不读取它发起请求 |
| `CENC_API_BASE_URL` | 当前可空 | 仅保留配置位；当前代码不读取它发起请求 |
| `RESPONSE_RULES_PATH` | 否 | 代码默认值为 `/config/response_rules/shanghai-2026.yaml`，Compose 已挂载该路径 |

安全示例只展示格式，不要照抄为实际密钥：

```dotenv
POSTGRES_DB=earthquake
POSTGRES_USER=earthquake
POSTGRES_PASSWORD=<单独生成的随机密码>
DATABASE_URL=postgresql+asyncpg://earthquake:<URL编码后的同一个随机密码>@postgres:5432/earthquake
API_BIND_HOST=127.0.0.1
API_PORT=8000
JWT_SECRET=<至少32字节的独立随机值>
JWT_EXPIRE_MINUTES=480
SUPERADMIN_USERNAME=superadmin
SUPERADMIN_INITIAL_PASSWORD=<至少16字符的独立随机密码>
CENC_APP_ID=
CENC_API_BASE_URL=
RESPONSE_RULES_PATH=/config/response_rules/shanghai-2026.yaml
```

可使用下面的 PowerShell 命令生成十六进制随机值，分别用于数据库密码和 JWT：

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
```

数据库密码包含 `@`、`:`、`/`、`#` 等字符时，必须按 URL 规则编码后再写入 `DATABASE_URL`。使用十六进制随机值可避免此问题。

### 3.2 推荐的干净启动顺序

以下操作会删除本地 PostgreSQL 卷和全部事件历史，只允许用于无数据测试环境：

```powershell
docker compose --env-file .env -f infra/compose.yaml down -v
```

先构建镜像，再启动数据库、执行迁移、启动全部服务：

```powershell
docker compose --env-file .env -f infra/compose.yaml build
docker compose --env-file .env -f infra/compose.yaml up -d postgres
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d
```

检查服务：

```powershell
docker compose --env-file .env -f infra/compose.yaml ps
Invoke-RestMethod http://localhost:8000/health
```

预期健康检查返回：

```json
{"status":"ok"}
```

访问前端：

```text
http://localhost:5173
```

首次登录使用 `.env` 中的 `SUPERADMIN_USERNAME` 和 `SUPERADMIN_INITIAL_PASSWORD`。

### 3.3 宿主机直接运行前端

Compose 模式下，frontend 容器内的 Vite 开发服务器使用默认代理目标 `http://api:8000`，无需设置 `VITE_API_PROXY_TARGET`。该默认地址只在 Compose 网络内可解析，不要在宿主机直接运行时使用。

若 API 通过 Compose 暴露在宿主机默认端口 `8000`，则从仓库根目录执行：

```powershell
cd frontend
npm install
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:8000"
npm run dev
```

若修改了 `API_PORT` 或 API 运行在其他主机，请把 `VITE_API_PROXY_TARGET` 改为对应的宿主机可达地址。该变量只在启动 Vite 时读取；修改后需要重启前端开发服务器。

### 3.4 任务验收用的完整重置序列

以下标准验收序列用于无数据环境，并按“先迁移、后启动 API”的顺序避免新建表前启动主 API：

```powershell
docker compose --env-file .env -f infra/compose.yaml down -v
docker compose --env-file .env -f infra/compose.yaml build
docker compose --env-file .env -f infra/compose.yaml up -d postgres
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
```

生产或保留数据环境不要执行 `down -v`。

`2026-09-25` 已在 Z440 上执行数据库和 API 部分：PostgreSQL `16`、PostGIS `3.4.3` 启动成功，迁移到 `0004_users`，API 启动并完成健康检查、正式报接入和详情读取。完整序列中的前端容器构建已尝试但未完成，原因是 Playwright 基础镜像约 `833 MB` 且下载受当前网络带宽限制；前端和三条浏览器 E2E 改由宿主机 Vite 加本机 Chromium 验证通过。

停止服务：

```powershell
docker compose --env-file .env -f infra/compose.yaml down
```

## 4. 事件接入方式

### 4.1 自动速报

调用：

```text
POST /api/v1/ingest/auto
```

自动速报只建立待定事件和修订，不计算制度响应或服务响应建议，也不执行损失评估。

PowerShell 示例：

```powershell
$autoPayload = @{
  eventId = "CENC-AUTO-RUNBOOK-01"
  reportType = "automatic"
  originTime = "2026-09-25T10:30:05+08:00"
  longitude = 121.54
  latitude = 31.22
  magnitude = 5.0
  depth = 12.0
  place = "上海浦东新区"
} | ConvertTo-Json -Depth 5

$autoResult = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/ingest/auto" `
  -ContentType "application/json" `
  -Body $autoPayload

$autoResult | ConvertTo-Json
```

预期：

- HTTP `201`。
- `event_kind` 为 `auto`。
- `institutional_level` 和 `service_level` 均为 `null`。

### 4.2 正式报告

调用：

```text
POST /api/v1/ingest/formal
```

正式报告会追加或建立正式修订。请求携带 `regionContext` 时，系统独立计算上海市制度响应和中国地震局应急服务响应建议；没有 `regionContext` 时不会凭空推断区域关系。

```powershell
$credential = Get-Credential -Message "输入具备采集恢复权限的账号"
$loginBody = @{
  username = $credential.UserName
  password = $credential.GetNetworkCredential().Password
}
$token = (
  Invoke-RestMethod `
    -Method Post `
    -Uri "http://localhost:8000/api/v1/auth/login" `
    -Body $loginBody
).access_token
$headers = @{ Authorization = "Bearer $token" }

$formalPayload = @{
  eventId = "CENC-FORMAL-RUNBOOK-01"
  reportType = "formal"
  reportTime = "2026-09-25T10:35:00+08:00"
  reportNum = 1
  originTime = "2026-09-25T10:30:05+08:00"
  longitude = 121.54
  latitude = 31.22
  magnitude = 5.2
  depth = 12.0
  place = "上海浦东新区"
  regionContext = @{
    insideShanghai = $true
    distanceToBoundaryKm = 0
    deaths = $null
    maxIntensity = 6
  }
} | ConvertTo-Json -Depth 5

$formalResult = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/ingest/formal" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $formalPayload

$formalResult | ConvertTo-Json
```

按当前 `2026.1` 规则，上例通常得到 `major` 和 `2`。实际结果以 `/config/response_rules/shanghai-2026.yaml` 为准。

### 4.3 更正报告

调用：

```text
POST /api/v1/ingest/correction
```

更正报告复用同一 `eventId` 时追加新修订，不覆盖正式报告。示例：

```powershell
$correctionPayload = @{
  eventId = "CENC-FORMAL-RUNBOOK-01"
  reportType = "correction"
  reportTime = "2026-09-25T10:42:00+08:00"
  reportNum = 2
  originTime = "2026-09-25T10:30:05+08:00"
  longitude = 121.54
  latitude = 31.22
  magnitude = 5.1
  depth = 12.0
  place = "上海浦东新区"
  regionContext = @{
    insideShanghai = $true
    distanceToBoundaryKm = 0
    deaths = $null
    maxIntensity = 6
  }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/api/v1/ingest/correction" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $correctionPayload
```

预期 `revision_no` 在原修订基础上递增。若报文顺序或 `reportTime`、`reportNum` 表明它是旧报告，系统仍保存修订，但不会让它覆盖当前修订。

### 4.4 人工事件

前端操作：

1. 登录系统。
2. 打开“人工触发”。
3. 填写发震时刻、经度、纬度、震级、震源深度和数据来源。
4. 事件类型选择“人工事件”。
5. 提交后回到事件列表核验。

人工事件 API 必须携带 Bearer token：

```text
POST /api/v1/events/manual
```

必填字段：

- `origin_time`
- `longitude`
- `latitude`
- `magnitude`
- `depth_km`
- `source`

可选字段：

- `source_event_id`
- `place`
- `event_kind`

`event_kind` 允许 `manual`、`test`、`drill`。当前人工事件接口只建立事件和修订，不接收 `regionContext`，因此不会计算两级响应建议。前端“启动评估”按钮只表示提交人工事件，不代表损失评估引擎已经存在。

### 4.5 测试事件和演练事件

测试和演练事件通过人工事件接口创建：

```json
{
  "origin_time": "2026-09-25T10:30:05+08:00",
  "longitude": 121.54,
  "latitude": 31.22,
  "magnitude": 5.2,
  "depth_km": 12.0,
  "source": "shanghai-drill",
  "source_event_id": "DRILL-20260925-01",
  "place": "上海浦东新区",
  "event_kind": "drill"
}
```

将 `event_kind` 改为 `test` 即创建测试事件。

差异：

- `auto`、`formal`、`correction`、`manual` 属于真实事件类型。
- `test` 和 `drill` 属于非真实事件。
- 测试和演练标识在事件列表和详情页可见。
- 真实事件修订优先于测试或演练修订；测试或演练不得覆盖真实事件当前成果。
- 当前子系统没有“每日自动触发测试”和定时调度器。

## 5. 核验修订和原始报文

### 5.1 从 API 查看当前修订

列出事件：

```powershell
Invoke-RestMethod http://localhost:8000/api/v1/events | ConvertTo-Json -Depth 6
```

获取事件详情：

```powershell
$eventId = "<从列表复制事件 id>"
Invoke-RestMethod "http://localhost:8000/api/v1/events/$eventId" | ConvertTo-Json -Depth 8
```

详情中的 `revision_no` 是当前修订号。当前 API 不返回全部修订历史，因此确认多个修订必须查询数据库。

### 5.2 查询全部修订

以下命令假设 `.env` 使用示例数据库名和用户名；如已修改，请替换 `earthquake`：

```powershell
$eventId = "<事件 id>"
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql `
  -U earthquake `
  -d earthquake `
  -v event_id="$eventId" `
  -c "SELECT revision_no, revision_kind, is_current, origin_time, magnitude, depth_km, place, source_report_number, source_report_time, created_at, raw_message_id FROM earthquake_revisions WHERE event_id = :'event_id' ORDER BY revision_no;"
```

判断要点：

- 同一 `event_id` 有多行且 `revision_no` 递增，说明存在多个修订。
- 每个事件最多只有一行 `is_current = t`。
- `raw_message_id` 指回产生该修订的原始报文。

### 5.3 查询原始报文

```powershell
$eventId = "<事件 id>"
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql `
  -U earthquake `
  -d earthquake `
  -v event_id="$eventId" `
  -c "SELECT r.revision_no, r.revision_kind, rm.source, rm.source_message_id, rm.message_kind, rm.received_at, rm.checksum, jsonb_pretty(rm.payload) AS raw_payload FROM earthquake_revisions r JOIN raw_messages rm ON rm.id = r.raw_message_id WHERE r.event_id = :'event_id' ORDER BY r.revision_no;"
```

核验要点：

- 每个修订都应能通过 `raw_message_id` 找到一条 `raw_messages` 记录。
- `checksum` 用于识别相同来源、类型和内容的重复报文。
- JSON 以 PostgreSQL `JSONB` 保存，语义内容会保留，但键顺序和空格格式可能被规范化，不是字节级原始文件存档。

检查重复报文是否被去重：

```sql
SELECT checksum, count(*)
FROM raw_messages
GROUP BY checksum
HAVING count(*) > 1;
```

预期零行。

## 6. 查看制度响应和服务响应

两种响应必须独立查看，不得用一个等级推导另一个等级。

前端：

1. 登录并打开事件详情。
2. “上海市制度响应”显示制度建议，例如“重大响应”。
3. “中国地震局应急服务响应”显示服务建议，例如“服务响应二级”。
4. “研判依据”显示规则版本和触发原因。

API：

```powershell
$eventId = "<事件 id>"
$detail = Invoke-RestMethod "http://localhost:8000/api/v1/events/$eventId"
$detail.institutional_level
$detail.service_level
$detail.response_suggestion
$detail.response_rule_version
```

字段含义：

- `institutional_level`：上海市制度响应建议，取值可为 `pending`、`none`、`general`、`larger`、`major`、`special_major`。
- `service_level`：中国地震局应急服务响应建议，当前可为 `1` 至 `4`，也可能为空。
- `response_suggestion`：当前建议的完整快照，包括 `causes` 和 `downgraded`。
- `response_rule_version`：产生建议的 YAML 规则版本。

数据库核验当前建议：

```powershell
$eventId = "<事件 id>"
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql `
  -U earthquake `
  -d earthquake `
  -v event_id="$eventId" `
  -c "SELECT institutional_level, service_level, response_rule_version, response_suggestion FROM earthquake_events WHERE id = :'event_id';"
```

数据库核验每个修订的建议快照：

```powershell
$eventId = "<事件 id>"
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql `
  -U earthquake `
  -d earthquake `
  -v event_id="$eventId" `
  -c "SELECT revision_no, revision_kind, institutional_level, service_level, response_rule_version, response_suggestion FROM earthquake_revisions WHERE event_id = :'event_id' ORDER BY revision_no;"
```

自动速报的两个等级为空是预期行为。没有 `regionContext` 的正式报告或更正报告也不会形成新的完整建议。

## 7. 安全轮换

### 7.1 轮换 JWT_SECRET

`JWT_SECRET` 用于签发和验证 Bearer token，与超级管理员账号密码是相互独立的凭据。轮换 `JWT_SECRET` 不需要先轮换超级管理员密码，也不依赖 7.2 的密码验证结果。

修改并重启 API 后，新的 `JWT_SECRET` 会立即使所有现有 token 失效，所有用户必须重新登录。如已确认或高度怀疑 JWT 泄露，应立即按本节轮换，不应等待无关的密码轮换完成。

操作顺序：

1. 非紧急轮换可选择维护窗口并通知值守人员；发生 JWT 泄露时应立即轮换，通知和复盘可在遏制风险后完成。
2. 生成新的独立随机值。
3. 修改 `.env` 中的 `JWT_SECRET`，不要把新值发到群聊、工单或截图中。
4. 强制重建 API，使新密钥生效。
5. 验证旧会话或旧 token 返回 `401`，再验证用户重新登录后系统可用。

PowerShell 示例：

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
$newJwtSecret = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""

$envFile = (Resolve-Path .env).Path
$content = [System.IO.File]::ReadAllText($envFile)
$content = [System.Text.RegularExpressions.Regex]::Replace(
  $content,
  "(?m)^JWT_SECRET=.*$",
  "JWT_SECRET=$newJwtSecret"
)
[System.IO.File]::WriteAllText(
  $envFile,
  $content,
  [System.Text.UTF8Encoding]::new($false)
)

docker compose --env-file .env -f infra/compose.yaml up -d --force-recreate api
```

当前 Compose 只有一个 API 实例。未来若扩展到多个实例，必须同时切换全部实例；混合密钥会造成部分请求间歇性返回 `401`。

### 7.2 轮换超级管理员密码

当前系统没有修改密码 API。启动时的 `SUPERADMIN_INITIAL_PASSWORD` 只用于在 `users` 表不存在该用户名时创建账号，修改 `.env` 不会自动更新已存在账号的密码。

以下流程从安全交互输入读取新密码，只通过子进程环境变量传给哈希工具，不把明文密码放到命令行参数、日志或仓库中。

先输入管理员用户名和新密码，并生成 Argon2 哈希。哈希命令失败，或结果不以 `$argon2` 开头时必须停止，不得继续写数据库：

```powershell
$adminUsername = Read-Host "超级管理员用户名"
$newPassword = Read-Host "新的超级管理员密码" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($newPassword)
try {
  $env:NEW_SUPERADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
  $hashOutput = (
    docker compose --env-file .env -f infra/compose.yaml run --rm `
      -e NEW_SUPERADMIN_PASSWORD `
      api python -c "import os; from app.security import hash_password; print(hash_password(os.environ['NEW_SUPERADMIN_PASSWORD']))"
  )
  if ($LASTEXITCODE -ne 0) {
    throw "生成密码哈希失败，不得更新数据库"
  }
  $hash = ($hashOutput | Out-String).Trim()
  if (-not $hash.StartsWith('$argon2')) {
    throw "密码哈希格式异常，不得更新数据库"
  }
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  Remove-Item Env:NEW_SUPERADMIN_PASSWORD -ErrorAction SilentlyContinue
}
```

更新数据库中的超级管理员密码哈希。`ON_ERROR_STOP=1` 保证 SQL 错误立即失败，`RETURNING username` 用于确认实际更新行数；必须恰好返回 1 行，否则停止：

```powershell
$updatedRows = @(
  docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql `
    -U earthquake `
    -d earthquake `
    -v ON_ERROR_STOP=1 `
    -v username="$adminUsername" `
    -v password_hash="$hash" `
    --tuples-only `
    --no-align `
    -c "UPDATE users SET password_hash = :'password_hash' WHERE username = :'username' RETURNING username;"
)
$updatedRows = @(
  $updatedRows |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ }
)
if ($LASTEXITCODE -ne 0 -or $updatedRows.Count -ne 1 -or $updatedRows[0] -ne $adminUsername) {
  throw "超级管理员密码未恰好更新 1 行；停止轮换并检查数据库"
}
```

如 `.env` 修改过数据库用户名或数据库名，请替换命令中的 `earthquake`。

数据库更新后，用同一个安全输入的 `$newPassword` 实际登录验证。登录失败时，本次密码轮换不得标记为完成，必须排查并修正后重新验证。该失败不阻止因 JWT 独立泄露而按 7.1 立即处置：

```powershell
$loginBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($newPassword)
try {
  $env:NEW_SUPERADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($loginBstr)
  try {
    $loginResult = Invoke-RestMethod `
      -Method Post `
      -Uri "http://localhost:8000/api/v1/auth/login" `
      -ContentType "application/x-www-form-urlencoded" `
      -Body @{
        username = $adminUsername
        password = $env:NEW_SUPERADMIN_PASSWORD
      }
  } catch {
    throw "新密码登录验证失败；本次密码轮换不得标记为完成"
  } finally {
    Remove-Item Env:NEW_SUPERADMIN_PASSWORD -ErrorAction SilentlyContinue
  }
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($loginBstr)
}

if (-not $loginResult.access_token) {
  throw "登录响应缺少 token；本次密码轮换不得标记为完成"
}
```

登录验证成功后，再同步更新 `.env` 的 `SUPERADMIN_INITIAL_PASSWORD`，确保以后在空数据库中初始化时仍使用当前密码。该值至少 16 个字符，修改 `.env` 时不得泄露旧密码或新密码。

安全顺序：

- 仅轮换超级管理员密码不会使已经签发的 JWT 失效。
- 如果超级管理员密码已经泄露，建议在确认新密码实际登录可用后尽快按 7.1 轮换 `JWT_SECRET`，以终止旧会话；这是密码泄露场景的推荐顺序，不是所有 JWT 轮换的通用前置条件。
- 哈希生成失败、数据库更新未返回 1 行或新密码登录失败时，本次密码轮换不得标记为完成；如果同时存在 JWT 泄露风险或需要终止会话，仍应按 7.1 独立轮换 `JWT_SECRET`。
- 轮换 JWT 后，所有用户必须重新登录；完成密码轮换的场景使用已验证的新密码，其他场景使用当前有效密码。

## 8. 数据库连接和迁移诊断

### 8.1 检查容器和网络

```powershell
docker compose --env-file .env -f infra/compose.yaml ps
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 postgres
docker compose --env-file .env -f infra/compose.yaml logs --tail 200 api
```

检查 PostgreSQL 就绪状态：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec postgres pg_isready `
  -U earthquake `
  -d earthquake
```

检查 PostGIS：

```powershell
docker compose --env-file .env -f infra/compose.yaml exec -T postgres psql `
  -U earthquake `
  -d earthquake `
  -c "SELECT PostGIS_Full_Version();"
```

若 `.env` 修改了数据库名或用户名，请同步替换命令参数。

### 8.2 常见数据库连接故障

| 现象 | 常见原因 | 处理 |
| --- | --- | --- |
| `Name or service not known`、`could not translate host name` | `DATABASE_URL` 使用了错误的数据库主机 | Compose 内主机名必须是 `postgres`，不能写 `localhost` 或 `127.0.0.1` |
| `InvalidPasswordError`、认证失败 | `POSTGRES_PASSWORD` 与 `DATABASE_URL` 中密码不一致 | 同步修正两个变量；密码含特殊字符时做 URL 编码 |
| API 日志出现 `Superadmin bootstrap failed; the database may be unavailable` | 数据库不可达，或 `users` 表尚未迁移 | 检查 PostgreSQL 日志并执行迁移；这通常不是登录密码错误 |
| `psycopg`/asyncpg 连接被拒绝 | PostgreSQL 容器未就绪或已停止 | 查看 `docker compose --env-file .env -f infra/compose.yaml ps` 和 `postgres` 日志 |
| 外部数据库连接失败 | 网络、防火墙或权限限制 | 从 API 容器验证网络；不要只从宿主机测试 |

`GET /health` 不访问数据库。它返回 `200` 只能说明 API 进程还在运行，不能替代数据库检查。

### 8.3 迁移检查

查看 Alembic 当前版本和头版本：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic current
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic heads
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic history
```

执行迁移：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
```

常见迁移故障：

- `relation "users" does not exist`：迁移未执行或只执行到旧版本；运行 `alembic upgrade head`。
- `type "geometry" does not exist` 或 PostGIS 函数不存在：当前数据库不是 PostGIS 镜像，或执行账号无权创建扩展；确认使用 `postgis/postgis:16-3.4`。
- `Multiple head revisions are present`：迁移脚本存在分叉；先检查 `alembic heads` 和历史，不能直接 `stamp head`。
- `Can't locate revision`：容器中的迁移目录与数据库版本不一致；确认 API 镜像或 `backend/migrations` 卷内容完整。
- 修改 `DATABASE_URL` 后仍连接旧库：重新创建 API 容器，并检查容器实际环境变量；不要输出真实密码到日志。

只有确认数据库结构已经与目标版本一致时，才允许使用 `alembic stamp`。不要用 `stamp head` 掩盖尚未执行的建表语句。

### 8.4 数据重置

以下命令会删除 PostgreSQL 卷和全部事件历史：

```powershell
docker compose --env-file .env -f infra/compose.yaml down -v
```

仅用于无价值数据测试环境。正式运行环境不得使用该命令排障。

## 9. 测试和验证命令

后端单元测试：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
```

后端静态检查：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

前端单元测试、类型检查和构建：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

端到端测试：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm `
  -e E2E_SUPERADMIN_USERNAME="<当前超级管理员用户名>" `
  -e E2E_SUPERADMIN_PASSWORD="<当前超级管理员密码>" `
  frontend npm run test:e2e
```

若 `.env` 修改了 `SUPERADMIN_USERNAME`，必须同步传入 `E2E_SUPERADMIN_USERNAME`。`E2E_SUPERADMIN_USERNAME` 和 `E2E_SUPERADMIN_PASSWORD` 都不得写入测试源码或 `.env.example`。

## 10. 已验证状态与残余风险

本手册在 `2026-09-25` 的 Z440 工作区完成真实运行验证：

- PostgreSQL `16`、PostGIS `3.4.3` 和 API 容器启动成功。
- Alembic 迁移执行到 `0004_users`。
- 后端测试套件在真实 PostgreSQL/PostGIS 环境下为 `180 passed`；其中 `test_event_tables_and_postgis_exist` 直接验证建表结果和 PostGIS 扩展，其他用例主要使用内存或 mock。
- API `/health`、超级管理员初始化、正式报接入和事件详情读取通过。
- 前端单元测试 `23 passed`，类型检查、生产构建和三条 Chromium E2E 通过。
- 前端容器镜像构建未完成；Playwright 基础镜像下载受当前网络带宽限制，本轮改用宿主机 Vite 加本机 Chromium 完成浏览器验证。
- 尚未执行真实 CENC 上游联调；当前只验证了兼容 CENC JSON 的接入接口。

上述结果证明当前基础事件子系统的数据库、API、前端代理和浏览器主流程可用，但不代表后续评估、制图、任务协同或 AI 模块已经实现。

当前主要残余风险：

- CENC 自动速报兼容接口尚未增加 API Key；正式报、更正报和人工事件接口依赖
  Bearer token 和角色授权，整个 API 仍必须部署在受控网络中。
- 当前没有外部 CENC 定时采集器；`CENC_APP_ID` 和 `CENC_API_BASE_URL` 尚未参与代码逻辑。
- 当前没有修订历史 API，运维核验全部修订需要直接查询数据库。
- 原始报文以 JSONB 保存，不是原始字节归档。
- 当前没有密码修改 API，超级管理员轮换需要按本手册执行数据库更新。
