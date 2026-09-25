# 上海市地震应急辅助决策系统

本仓库实现地震事件接入与响应研判控制台。后端接收 CENC 报文并生成制度响应、服务响应建议；前端提供登录、事件列表、事件详情和人工事件录入。

## 前置条件

- Docker Desktop（含 Docker Compose v2）
- Git
- CENC 测试凭据（用于本地 CENC 接入测试；不需要真实凭据也能跑单元测试）
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
- `CENC_APP_ID`、`CENC_API_BASE_URL`（仅真实 CENC 联调时需要）

## 启动与停止

构建并启动：

```powershell
docker compose -f infra/compose.yaml up -d --build
```

执行数据库迁移：

```powershell
docker compose -f infra/compose.yaml run --rm api alembic upgrade head
```

启动后访问：

- 前端：`http://localhost:5173`
- API 健康检查：`http://localhost:8000/health`

停止：

```powershell
docker compose -f infra/compose.yaml down
```

停止并清除数据库卷（用于本地环境重置）：

```powershell
docker compose -f infra/compose.yaml down -v
```

## 测试

后端单元测试和静态检查：

```powershell
docker compose -f infra/compose.yaml run --rm api pytest -v
docker compose -f infra/compose.yaml run --rm api ruff check app tests
```

前端单元测试、类型检查和构建：

```powershell
docker compose -f infra/compose.yaml run --rm frontend npm test
docker compose -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose -f infra/compose.yaml run --rm frontend npm run build
```

单元测试不需要真实 CENC 凭据。后端测试使用假服务和内存中的响应规则；前端测试模拟 `fetch`。只有真实 CENC 联调才需要填写 `CENC_APP_ID` 和 `CENC_API_BASE_URL`。

## 端到端测试

E2E 使用真实登录并提交一条正式 CENC 报文，再验证事件列表和详情页能独立显示制度响应与服务响应。测试不读取或写入浏览器存储，登录 token 仅保存在 React 内存中。

环境变量：

- `E2E_SUPERADMIN_PASSWORD`（必填）：超级管理员密码。测试代码不包含密码。
- `E2E_BASE_URL`（可选）：前端地址，默认 `http://localhost:5173`。
- `E2E_API_BASE_URL`（可选）：只影响 Playwright `APIRequestContext` 发出的 API 请求。留空时这些请求使用 Vite 的 `/api` 相对路径；设为绝对地址时，只有测试中的 API 请求直连该地址，页面 UI 的 `/api` 请求仍走 Vite 代理。

通过 Compose 运行：

```powershell
docker compose -f infra/compose.yaml run --rm `
  -e E2E_SUPERADMIN_PASSWORD="<你的超级管理员密码>" `
  frontend npm run test:e2e
```

需要让 Playwright 的 API 测试请求直连时，在 Compose 容器内应使用服务名 `http://api:8000`，不是容器内的 `localhost`：

```powershell
docker compose -f infra/compose.yaml run --rm `
  -e E2E_SUPERADMIN_PASSWORD="<你的超级管理员密码>" `
  -e E2E_API_BASE_URL="http://api:8000" `
  frontend npm run test:e2e
```

页面 UI 的登录和事件列表请求始终通过 Vite 的 `/api` 代理转发，代理目标当前为 `http://api:8000`。`E2E_API_BASE_URL` 只改变 Playwright `APIRequestContext` 的 API 测试请求，不会把页面 UI 的 API 基地址改成其他值。Playwright `APIRequestContext` 在浏览器上下文之外发送 HTTP 请求，因此不受浏览器 CORS 限制；页面 UI 的 `/api` 请求则依赖 Vite 代理，通常无需跨域配置。若要脱离 Compose 网络在宿主机直接运行前端，需要自行让 Vite 的代理目标指向宿主机可访问的 API 地址，并相应设置 `E2E_BASE_URL`。

## 本工作区验证状态

本次交付在 `2026-09-25` 的工作区中验证，当前机器没有 Docker Desktop、Podman、PostgreSQL 或 `psql`，也没有安装 Playwright 浏览器。因此以下检查在本工作区没有执行：

- `docker compose up` 及基于真实 PostGIS 的迁移和联调
- 需要真实 PostgreSQL/PostGIS 的后端集成检查
- 需要 Playwright 浏览器实际运行前端页面的 E2E 测试

已在本工作区执行并确认通过：

- `npm test`
- `npm run typecheck`
- `npm run build`
- `npx playwright test --list`

`npx playwright test --list` 只验证 E2E 测试发现与配置解析，不能证明端到端流程已经通过。请勿把该命令的通过当作真实 E2E 已通过。
