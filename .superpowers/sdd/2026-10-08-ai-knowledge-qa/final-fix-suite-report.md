# AI Knowledge QA 最终收尾报告

日期：2026-10-08

分支：`codex/ai-knowledge-qa`

实现提交：`f2127a64d179114f8404f3a1b2d1ddb8f5fbd692`

## RED

在干净的 Compose project `codex-final-suite` 上，修改前默认全量结果为：

```text
8 failed, 1641 passed, 3 skipped, 8 deselected
```

其中 3 个失败是 Temporal 服务未启动导致的测试环境错误，修复期间已启动
`temporal-postgres` 和 `temporal`。目标收尾失败及根因如下：

| 项目 | 根因 | 处理 |
| --- | --- | --- |
| Compose 配置测试 | 容器默认把仓库根解析为 `/`，且只挂载 `backend`、`config` | 挂载 `.env.example` 和 `infra/compose.yaml` 到 `/project`，设置 `PROJECT_ROOT=/project` |
| Type3 PDF 文本 | ChartRenderer 修改 Matplotlib 全局字体后，PyPDF 将 Type3 Differences 中的 glyph name 直接输出 | 按 Type3 `/Encoding /Differences` 重建字符码，仅对该类字形名安全解码，普通 PDF 保持原路径 |
| knowledge/QA schema 跨 loop | 测试复用全局 SQLAlchemy engine 的跨 loop 连接池连接 | 该 schema 测试改用独立 `NullPool` engine，并在 `finally` 中 dispose |
| stale outbox | 测试把未来时间设为固定 `2026-10-04`，而 dispatcher 使用真实当前时间 `2026-10-08`，两个 outbox 都已到期 | 修正测试的绝对时间基准为当前 UTC + 1 天；生产 dispatcher 语义未改变 |
| PDF 回归/健康/history/audit 契约 | 新契约尚未实现 | 按 TDD 先写失败测试，再实现生产代码 |

## GREEN

最终工作树的默认完整后端套件：

```text
1658 passed, 3 skipped, 8 deselected, 26 warnings
```

迁移、Qdrant、PostgreSQL、Temporal、Collaboration E2E 均包含在默认套件中；
`8 deselected` 是 `pyproject.toml` 显式排除的 `performance` 标记测试。

### A. Compose 与降级/健康

- API 对 Qdrant 改为 `service_started`，Qdrant 延迟或失败不会阻止 API
  启动，QA 链路仍保留 PostgreSQL 降级检索。
- embedding 增加 `/health` 检查，要求 `status: ok`。
- knowledge-worker 在容器内 `8100` 暴露内部 `/health`，工作循环刷新
  heartbeat。
- 新增 `GET /system/health`，返回 PostgreSQL、Qdrant、embedding、
  knowledge-worker 状态；响应不包含连接串、密码、Token 或 API Key。
- Compose 文件挂载 `.env.example` 和 `infra/compose.yaml`，唯一 project
  `codex-final-suite` 使用空闲宿主机端口 `55480`。

### B. QA 历史与审计字段

- 新增 `GET /api/v1/qa/sessions/{session_id}/answers`。
- 支持 `limit` 和 cursor keyset 分页，稳定排序为
  `created_at DESC, id DESC`。
- 普通用户不能读取他人 session；超级管理员保留全部读取能力。
- `QaAnswerView` 增加 `model_name`、`model_version`、
  `prompt_version`、`execution_plan`、`tool_call_summary`；
  `structured` 和 `degraded_reasons` 已包含，旧数据字段可空兼容。

## 命令结果

```text
docker compose --env-file .env -f infra/compose.yaml -p codex-final-suite run --rm --no-deps -T api pytest -q
结果：1658 passed, 3 skipped, 8 deselected in 2426.71s

docker compose --env-file .env -f infra/compose.yaml -p codex-final-suite run --rm --no-deps -T api pytest -q tests/test_migrations.py tests/test_qa_audit_schema.py tests/test_knowledge_schema.py
结果：23 passed in 701.58s

docker compose --env-file .env -f infra/compose.yaml -p codex-final-suite run --rm --no-deps -T api ruff check app tests
结果：All checks passed

docker compose --env-file .env -f infra/compose.yaml -p codex-final-suite config --quiet
结果：通过
```

实际依赖健康检查结果：

```text
system/health status: ok
postgresql: ok
qdrant: ok
embedding: ok
knowledge_worker: ok
```

## 文件列表

```text
.env.example
backend/app/config.py
backend/app/knowledge/health.py
backend/app/knowledge/parser.py
backend/app/knowledge/worker.py
backend/app/main.py
backend/app/qa/repository.py
backend/app/qa/router.py
backend/app/qa/schemas.py
backend/app/system_health.py
backend/tests/test_collaboration_generation.py
backend/tests/test_health.py
backend/tests/test_knowledge_parser.py
backend/tests/test_knowledge_schema.py
backend/tests/test_knowledge_worker.py
backend/tests/test_qa_api.py
docs/runbooks/ai-knowledge-qa.md
infra/compose.yaml
```

## 残余风险

- `pytest -q` 按项目配置排除了 `performance` 标记；真实 BGE 推理性能门禁
  未执行。运行手册已补充真实 BGE 门禁命令和注意事项，报告中不把未执行的
  压测标记为通过。
- 真实 DeepSeek/外部模型推理和真实浏览器账号 E2E 未执行；默认后端套件
  使用确定性 Fake DeepSeek 和测试夹具。真实 E2E 账号前置条件已写入运行
  手册。
- 最终全量套件有 26 个第三方或既有测试警告，主要是 Matplotlib、rasterio、
  Starlette 弃用提示和部分 unraisable subprocess 资源警告，不影响本次
  失败项。
- 未修改 `.env`，未打印或写入任何密码、连接串、Token 或 API Key。
