# 上海市地震应急辅助决策系统

本仓库实现上海市地震事件接入、响应研判、评估编排、专业成果生产、七个固定工作组协同和应急指挥大厅。后端接收 CENC 报文并生成制度响应、服务响应建议；正式报与更正报可经 Outbox 和 Temporal 建立评估运行，成果生产链生成既有 27 类图件和全部报告，`collaboration-worker` 负责工作组任务、期限、通知、成果版本和指挥大厅投影。

## 前置条件

- Docker Desktop（含 Docker Compose v2）
- Git
- 至少 8 GB 可用内存，建议 16 GB
- 本机开放的端口：`5173`（前端）、`8000`（API，由 `API_BIND_HOST`/`API_PORT` 控制）
- Temporal UI 默认绑定 `127.0.0.1:8088`

## 首次配置

复制环境变量模板，并填入真实值。`.env` 已列入 `.gitignore`，不得提交到版本库。

```powershell
Copy-Item .env.example .env
```

```bash
cp .env.example .env
```

至少需要替换 `.env` 中的以下占位值：

- `POSTGRES_PASSWORD`
- `DATABASE_URL`
- `JWT_SECRET`
- `SUPERADMIN_INITIAL_PASSWORD`

## 隔离启动与停止

顶层快速开始用于隔离验证，固定使用 Compose project `codex-task15`。构建镜像，先启动数据库并完成迁移，再启动全部服务：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 build
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d postgres
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d
```

启动后访问：

- 前端：`http://localhost:5173`
- API 健康检查：`http://localhost:8000/health`
- Temporal UI：`http://127.0.0.1:8088`

Compose 模式下，frontend 容器内的 Vite 开发服务器使用默认代理目标 `http://api:8000`，无需设置 `VITE_API_PROXY_TARGET`。

在宿主机直接运行前端时，Vite 无法解析 Compose 服务名 `api`，需要把代理目标显式指向宿主机可访问的 API 地址：

```powershell
cd frontend
npm install
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:8000"
npm run dev
```

若通过 Compose 暴露 API，默认地址为 `http://127.0.0.1:8000`；若修改了 `API_PORT` 或 API 所在主机，请同步调整该值。未设置 `VITE_API_PROXY_TARGET` 时仍默认使用容器服务地址 `http://api:8000`。

停止：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 down
```

停止并清除数据库卷（用于本地环境重置）：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 down -v
```

生产部署必须使用显式且环境唯一的 project，并将上述命令中的 `codex-task15` 替换为 `--project-name <environment-project>`；不要复用验证 project 的容器或卷。

## 测试

以下隔离验证命令固定使用 Compose project `codex-task15`，避免触碰本机默认 project 的容器和卷。普通 `pytest -q` 默认排除 `performance` 标记，性能测试必须显式使用 `-m performance`。

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api ruff check app tests
```

固定上海损失性能基准：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api pytest -m performance tests/test_loss_performance.py -v
```

前端单元测试、类型检查和构建：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm frontend npm run build
```

## CENC 实时采集

collector 是独立于 API 的可选服务。FAN WebSocket 是主链路；Wolfx HTTP 是常驻备用链路，FAN `auth_fail` 或连接失败时仍可继续接收正式报并进入生命周期。`FAN_APP_ID` 是规范的 FAN 客户端标识，未设置时兼容回退到 `CENC_APP_ID`；`FAN_API_KEY` 是 FAN 密钥。缺少 `FAN_APP_ID`/`CENC_APP_ID` 或 `FAN_API_KEY` 只会阻止 collector 启动，不会阻止 API、迁移或后端测试运行。

部署、边界导入、运行状态核验、spool/数据库恢复、死信重放和 FAN 密钥轮换请参阅 [CENC 实时采集运行手册](docs/runbooks/cenc-realtime-collection.md)。

## 评估编排基础

正式报和更正报首次入库时创建评估 Outbox。独立的 `assessment-dispatcher` 消费 Outbox，以稳定 Workflow ID 启动 Temporal Workflow；`temporal-worker` 执行幂等 Activity，并在 PostgreSQL 中建立 `assessment_runs` 和 `assessment_tasks`。API 服务不运行 Dispatcher。

当前每个正式报或更正报修订建立 11 个任务：

- 模型烈度、仪器烈度、融合烈度。
- 受灾人口、人员伤亡、房屋破坏、经济损失、应急资源需求、损失校验。
- 快速评估报告。
- 工作组响应任务。

当前已实现 `intensity.model`、`intensity.instrument`、`intensity.fusion`，以及 `loss.population`、`loss.casualties`、`loss.buildings`、`loss.economic`、`loss.resources`、`loss.validate`。模型、融合和六个损失任务必须成功才能把运行标记为 `completed`；仪器缺失或失败会保存 `unavailable`/`invalid` 产品，并由融合回退到 `model_only`/`F3`，不会导致运行失败。评估运行内的报告和协同占位任务保持 `skipped`，报告文件和成果流转由独立成果生产与协同子系统处理。Temporal 不可用时会阻塞 Outbox 发布并退避重试，不会阻止事件报文和正式报修订入库。

损失结果通过以下只读 API 对外提供，并在事件详情页展示产品、指标、城镇/格网地图和融合烈度图层：

```text
GET /api/v1/assessments/runs/{run_id}/loss
GET /api/v1/assessments/runs/{run_id}/loss/products/{product_type}
GET /api/v1/assessments/runs/{run_id}/loss/areas?scope=city|county|town
GET /api/v1/assessments/runs/{run_id}/loss/artifact?product_id=<product-id>[&band=<band>]
GET /api/v1/assessments/runs/{run_id}/loss/artifact/{product_id}/{band}/{z}/{x}/{y}.png
GET /api/v1/assessments/runs/{run_id}/intensity/artifact?product_id=<product-id>[&band=<band>]
GET /api/v1/assessments/runs/{run_id}/intensity/artifact/{product_id}/{band}/{z}/{x}/{y}.png
```

烈度评估的启动、正式报核验、产品检查、更正语义、超时处理、仪器降级和栅格备份请参阅 [烈度评估运行手册](docs/runbooks/intensity-assessment.md)。

评估编排的启动、健康检查、Outbox 查询、死信安全重放、Workflow 核验和 Worker 恢复请参阅 [评估编排运行手册](docs/runbooks/assessment-orchestration.md)。

损失评估的启动、数据资产、参数来源、固定上海场景、产品/SQL 核验、缺参处理、更正、恢复和 PostGIS raster 备份请参阅 [损失评估运行手册](docs/runbooks/loss-assessment.md)。

## 数据资产中心

数据资产中心已实现上海区域数据资产的版本化接入、校验、发布、停用、回滚和评估快照。导入格式包括 GeoJSON、Windows Access MDB、GeoTIFF 和 YAML/JSON 参数文件；MDB 因 ODBC 驱动边界必须在 Windows 宿主机运行，其他格式由 `data-asset-worker` 在 Linux 容器中处理。

角色权限：

- `data_maintainer`：导入和校验。
- `data_publisher`：导入、校验、发布、停用和回滚。
- `superadmin`：拥有全部数据资产操作。

评估运行创建时会冻结已发布资产版本、校验和以及 required/optional 角色到 `data_asset_snapshots`。损失评估服务通过快照读取运行锁定的行政区划、人口、建筑、经济和损失参数版本；缺少必需资产时，损失任务或运行按对应失败/不可用语义处理。

数据资产的部署、导入、发布、故障排查、快照核验、更新逾期和备份恢复请参阅 [数据资产中心运行手册](docs/runbooks/data-asset-center.md)。

## AI 知识与问答

知识问答子系统由 `knowledge-worker`、`embedding` 和 `qdrant` 提供，API 通过混合检索编排调用 Qdrant 稠密/稀疏向量、PostgreSQL 三元组词法检索与 DeepSeek 生成最终回答。前端在事件详情页和指挥大厅提供“智能问策”，独立“智能问策”页面提供历史会话、引用证据、工具过程和地图动作。

首次启动会向 `embedding-models` 卷下载 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`，之后离线复用。非敏感配置位于 `.env`，其中 `DEEPSEEK_API_KEY` 留空并由部署环境注入，不在仓库中填写。

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d postgres qdrant embedding
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d
```

十万切片混合检索性能测试与真实事件问答验收：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api pytest -m performance tests/test_qa_performance.py -v
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm frontend npm run test:e2e -- ai-knowledge-qa.spec.ts
```

模型缓存、迁移、知识源发布与回滚、Qdrant 重建、故障降级、网页白名单、快照核验、审计删除、性能核验和备份恢复请参阅 [AI 知识与问答运行手册](docs/runbooks/ai-knowledge-qa.md)。

## 工作组协同与应急指挥大厅

`collaboration-worker` 消费 `collaboration.requested` Outbox，按上海预案模板生成新闻信息值守、监测预报、综合协调、震害评估、应急技术、后勤保障和中心站七个工作组的固定任务。正式事件在满足适用范围时生成 60 条任务；自动成果、人工修订、组长确认、到岗状态、期限提醒和通知降级均保留版本链和审计记录。

成果关联会保留自动版和人工修订版，当前发布版只有一个。指挥大厅投影通过事务 Outbox 刷新，前端优先使用带 Bearer 鉴权的 SSE，SSE 不可用时自动降级为 5 秒轮询。主屏验收覆盖 `7680 x 2430`，管理终端覆盖 `1920 x 1080`，测试和演练标识不可由任务或成果编辑移除。

协同服务和 Compose 部署由 `collaboration-worker` 提供，健康检查、单事件投影重建、死信处理、通知降级、双版本核验和大屏验收请参阅 [工作组协同与指挥大厅运行手册](docs/runbooks/workgroup-collaboration-command-hall.md)。

## 端到端测试

E2E 使用真实登录和真实后端数据库，准备测试事件、39 项专业成果、七个工作组任务、自动/人工双版本和指挥大厅投影，再验证事件、任务、权限、错误状态、SSE 和轮询降级。测试不读取或写入浏览器存储，登录 token 仅保存在 React 内存中。

环境变量：

- `E2E_SUPERADMIN_USERNAME`（可选）：超级管理员用户名，默认 `superadmin`。若 `.env` 修改了 `SUPERADMIN_USERNAME`，必须同步设置该变量。
- `E2E_SUPERADMIN_PASSWORD`（必填）：超级管理员密码。测试代码不包含密码。
- `E2E_VIEWER_USERNAME`（可选）：非超级管理员验收账号，默认 `e2e-viewer`。夹具使用与超级管理员相同的测试密码创建该账号，用于真实 403 权限链路。
- `E2E_BASE_URL`（可选）：前端地址，默认 `http://localhost:5173`。
- `E2E_API_BASE_URL`（可选）：只影响 Playwright `APIRequestContext` 发出的 API 请求。留空时这些请求使用 Vite 的 `/api` 相对路径；设为绝对地址时，只有测试中的 API 请求直连该地址，页面 UI 的 `/api` 请求仍走 Vite 代理。

干净环境的准备顺序如下。先构建后端、迁移数据库、启动 API、`collaboration-worker`、成果 Worker 和 Temporal Worker：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 build
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d postgres temporal-postgres temporal
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d `
  api collaboration-worker artifact-worker artifact-dispatcher `
  temporal-worker assessment-dispatcher
```

设置只存在于当前终端的测试变量，然后准备夹具。不要执行会回显变量值的命令：

```powershell
$env:E2E_SUPERADMIN_USERNAME = "<你的超级管理员用户名>"
$env:E2E_SUPERADMIN_PASSWORD = "<你的超级管理员密码>"
$env:E2E_VIEWER_USERNAME = "e2e-viewer"
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm -T `
  -e E2E_SUPERADMIN_USERNAME `
  -e E2E_SUPERADMIN_PASSWORD `
  -e E2E_VIEWER_USERNAME `
  -e PYTHONPATH=/app `
  api python -m tests.e2e_fixture
```

确认 PostgreSQL、API 和 Worker 已就绪：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 ps
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 exec collaboration-worker `
  python -m app.process_health collaboration
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Compress
```

另开终端启动宿主机 Vite，并安装 Playwright Chromium：

```powershell
cd frontend
npm ci
npx playwright install chromium
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:8000"
npm run dev
```

保留 Vite 终端运行，在另一个终端执行全量或协同专项验收：

```powershell
cd frontend
$env:E2E_BASE_URL = "http://127.0.0.1:5173"
$env:E2E_API_BASE_URL = "http://127.0.0.1:8000"
npm run test:e2e

# 协同与指挥大厅专项
npm run test:e2e -- e2e/command-hall.spec.ts e2e/workgroup-tasks.spec.ts
```

该专项测试包含 `7680 x 2430` 主屏和 `1920 x 1080` 管理终端，检查元素和文本的视口边界、`scrollWidth`/`scrollHeight`、卡片及抽屉遮挡、测试标识、自动/人工版本共存、SSE、5 秒轮询后的真实 GET、真实后端 403 和关键服务错误。若本机 `8000` 或 `5173` 已被其他任务占用，可在当前终端覆盖 `API_PORT`、`VITE_API_PROXY_TARGET`、`E2E_BASE_URL` 和 `E2E_API_BASE_URL`，不要停止或复用其他任务的数据。

清理：

```powershell
# 先停止 Vite 和 Playwright。
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 down --remove-orphans
# 仅对一次性验收数据库执行：
# docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 down -v --remove-orphans
```

页面 UI 的登录和事件列表请求始终通过 Vite 的 `/api` 代理转发。宿主机直接运行前端时，`VITE_API_PROXY_TARGET` 应指向实际 API 地址。`E2E_API_BASE_URL` 只改变 Playwright `APIRequestContext` 的控制请求，不会把页面 UI 的 API 基地址改成其他值。不要输出 `.env`、密码、Token、API Key 或连接串。

## 运行与交接手册

事件接入基础子系统的架构边界、环境变量、事件接入、修订与原始报文核验、两级响应查看、JWT 和超级管理员密码轮换、数据库迁移诊断以及已知运行风险，统一记录在：

- [地震事件接入基础子系统运行手册](docs/runbooks/event-ingestion-foundation.md)

运行手册明确区分了本子系统已实现能力、预留配置和后续平台能力。涉及 CENC 定时采集、评估引擎、制图、任务协同、AI、备份自动化和每日测试调度的交接，不应把本子系统描述为已经包含这些功能。

## 验证状态

当前 CENC 实时采集生命周期修复的迁移头、测试命令和结果记录在
[Final Fix Wave 报告](.superpowers/sdd/2026-09-25-cenc-realtime-collection-lifecycle-implementation-plan/final-fix-report.md)。

以下内容是 `2026-09-25` 基础事件子系统的历史验证快照，不代表当前分支的
Alembic revision、测试数量或 E2E 范围：

- Docker Desktop `4.92.0`、Docker Engine `29.8.0` 正常运行。
- PostgreSQL `16`、PostGIS `3.4.3` 容器正常运行，当时 Alembic 已迁移至 `0004_users`。
- `docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 config --quiet` 通过。
- `docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 build api` 通过。
- 当时后端测试套件在真实 PostgreSQL/PostGIS 环境下为 `180 passed`（其中 `test_event_tables_and_postgis_exist` 直接验证建表结果和 PostGIS 扩展），Ruff 检查通过。
- API `/health` 返回 `200`；正式报接入后返回重大响应和服务响应二级，并能从详情接口读取。
- 当时前端 `npm test` 为 `23 passed`，`npm run typecheck` 和 `npm run build` 通过。
- 当时宿主机 Vite 加真实 API 加 Playwright Chromium 的 E2E 用例通过。

该历史快照中的未完成事项：

- 前端容器镜像构建未完成，原因是 `mcr.microsoft.com/playwright:v1.49.1-noble` 基础镜像较大且当前网络下载极慢；本轮改以宿主机 Vite 和 Playwright Chromium 完成前端与 E2E 验证。
- 尚未连接真实 CENC 上游采集器；当前验证使用兼容 CENC JSON 的正式报接入请求。
