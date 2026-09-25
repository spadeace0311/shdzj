# 上海市地震应急辅助决策系统

本仓库实现地震事件接入与响应研判控制台。后端接收 CENC 报文并生成制度响应、服务响应建议；前端提供登录、事件列表、事件详情和人工事件录入。

## 前置条件

- Docker Desktop（含 Docker Compose v2）
- Git
- 至少 8 GB 可用内存，建议 16 GB
- 本机开放的端口：`5173`（前端）、`8000`（API，由 `API_BIND_HOST`/`API_PORT` 控制）

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

## 启动与停止

构建镜像，先启动数据库并完成迁移，再启动全部服务：

```powershell
docker compose --env-file .env -f infra/compose.yaml build
docker compose --env-file .env -f infra/compose.yaml up -d postgres
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml up -d
```

启动后访问：

- 前端：`http://localhost:5173`
- API 健康检查：`http://localhost:8000/health`

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
docker compose --env-file .env -f infra/compose.yaml down
```

停止并清除数据库卷（用于本地环境重置）：

```powershell
docker compose --env-file .env -f infra/compose.yaml down -v
```

## 测试

后端单元测试和静态检查：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api pytest -v
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
```

前端单元测试、类型检查和构建：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

当前接入接口接收调用方提交的 CENC 兼容 JSON。`CENC_APP_ID` 和 `CENC_API_BASE_URL` 只是预留配置，当前代码没有读取它们发起请求，也没有外部 CENC 采集器；填写这两个变量本身不会建立真实上游联调。后端单元测试使用假服务和内存中的响应规则，前端测试模拟 `fetch`。

## 端到端测试

E2E 使用真实登录并提交一条正式 CENC 报文，再验证事件列表和详情页能独立显示制度响应与服务响应。测试不读取或写入浏览器存储，登录 token 仅保存在 React 内存中。

环境变量：

- `E2E_SUPERADMIN_USERNAME`（可选）：超级管理员用户名，默认 `superadmin`。若 `.env` 修改了 `SUPERADMIN_USERNAME`，必须同步设置该变量。
- `E2E_SUPERADMIN_PASSWORD`（必填）：超级管理员密码。测试代码不包含密码。
- `E2E_BASE_URL`（可选）：前端地址，默认 `http://localhost:5173`。
- `E2E_API_BASE_URL`（可选）：只影响 Playwright `APIRequestContext` 发出的 API 请求。留空时这些请求使用 Vite 的 `/api` 相对路径；设为绝对地址时，只有测试中的 API 请求直连该地址，页面 UI 的 `/api` 请求仍走 Vite 代理。

通过 Compose 运行：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm `
  -e E2E_SUPERADMIN_USERNAME="<你的超级管理员用户名>" `
  -e E2E_SUPERADMIN_PASSWORD="<你的超级管理员密码>" `
  frontend npm run test:e2e
```

需要让 Playwright 的 API 测试请求直连时，在 Compose 容器内应使用服务名 `http://api:8000`，不是容器内的 `localhost`：

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm `
  -e E2E_SUPERADMIN_USERNAME="<你的超级管理员用户名>" `
  -e E2E_SUPERADMIN_PASSWORD="<你的超级管理员密码>" `
  -e E2E_API_BASE_URL="http://api:8000" `
  frontend npm run test:e2e
```

页面 UI 的登录和事件列表请求始终通过 Vite 的 `/api` 代理转发。Compose 模式未设置 `VITE_API_PROXY_TARGET` 时默认使用 `http://api:8000`；宿主机直接运行前端时应设置为 `http://127.0.0.1:8000`（或实际可达地址）。`E2E_API_BASE_URL` 只改变 Playwright `APIRequestContext` 的 API 测试请求，不会把页面 UI 的 API 基地址改成其他值。Playwright `APIRequestContext` 在浏览器上下文之外发送 HTTP 请求，因此不受浏览器 CORS 限制；页面 UI 的 `/api` 请求则依赖 Vite 代理，通常无需跨域配置。

## 运行与交接手册

事件接入基础子系统的架构边界、环境变量、事件接入、修订与原始报文核验、两级响应查看、JWT 和超级管理员密码轮换、数据库迁移诊断以及已知运行风险，统一记录在：

- [地震事件接入基础子系统运行手册](docs/runbooks/event-ingestion-foundation.md)

运行手册明确区分了本子系统已实现能力、预留配置和后续平台能力。涉及 CENC 定时采集、评估引擎、制图、任务协同、AI、备份自动化和每日测试调度的交接，不应把本子系统描述为已经包含这些功能。

## 本工作区验证状态

本次交付在 `2026-09-25` 的 Z440 工作区完成真实运行验证：

- Docker Desktop `4.92.0`、Docker Engine `29.8.0` 正常运行。
- PostgreSQL `16`、PostGIS `3.4.3` 容器正常运行，Alembic 已迁移至 `0004_users`。
- `docker compose --env-file .env -f infra/compose.yaml config --quiet` 通过。
- `docker compose --env-file .env -f infra/compose.yaml build api` 通过。
- 后端测试套件在真实 PostgreSQL/PostGIS 环境下为 `180 passed`（其中 `test_event_tables_and_postgis_exist` 直接验证建表结果和 PostGIS 扩展），Ruff 检查通过。
- API `/health` 返回 `200`；正式报接入后返回重大响应和服务响应二级，并能从详情接口读取。
- 前端 `npm test` 为 `23 passed`，`npm run typecheck` 和 `npm run build` 通过。
- 宿主机 Vite 加真实 API 加 Playwright Chromium 的三条 E2E 用例全部通过。

尚未完成：

- 前端容器镜像构建未完成，原因是 `mcr.microsoft.com/playwright:v1.49.1-noble` 基础镜像较大且当前网络下载极慢；本轮改以宿主机 Vite 和 Playwright Chromium 完成前端与 E2E 验证。
- 尚未连接真实 CENC 上游采集器；当前验证使用兼容 CENC JSON 的正式报接入请求。
