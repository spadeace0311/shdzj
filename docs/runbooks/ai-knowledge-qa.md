# AI 知识与问答运行手册

本手册覆盖 `knowledge-worker`、`embedding`、`qdrant`、问答 API、DeepSeek 配置、知识源生命周期、事件快照、审计删除、十万切片性能测试以及备份恢复。所有 Compose 命令统一使用：

```text
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 ...
```

不要输出 `.env`、密码、Token、API Key 或连接串；本手册只使用占位符。

## 服务拓扑与端口

| 服务 | 角色 | 端口 |
| --- | --- | --- |
| `postgres` | PostgreSQL/PostGIS，知识源、版本、切片、索引、快照、问答与审计 | 容器内 `5432`，宿主机由 `POSTGRES_HOST_PORT` 映射 |
| `qdrant` | 稠密/稀疏向量存储 | 容器内 `6333`（HTTP）与 `6334`（gRPC），仅 `expose` 不映射宿主机 |
| `embedding` | BGE-M3 稠密/稀疏向量与 BGE 重排 | 容器内 `8080`，仅 `expose` 不映射宿主机 |
| `knowledge-worker` | 消费知识任务：抓取、解析、切片、嵌入、索引、发布、回滚 | 无端口 |
| `api` | 知识/问答 REST 与 SSE | `8000`，由 `API_BIND_HOST`/`API_PORT` 映射 |
| `frontend` | 事件详情页与指挥大厅“智能问策” | `5173` |

验证时使用 `--project-name codex-task15` 和空闲端口，避免停止或复用宿主机已有容器；不得对用户现有卷执行 `down -v`。

## 健康检查与 QA 依赖降级

API 保留 `GET /health` 的简单存活契约。QA 依赖诊断使用：

```text
GET /system/health
```

响应包含总状态和 `postgresql`、`qdrant`、`embedding`、`knowledge_worker`
四类检查，只返回 `ok`、`starting`、`unavailable` 等状态，不返回连接串、
密码、Token、API Key 或错误正文。PostgreSQL 不可用时总状态为
`unavailable`；Qdrant、embedding 或 knowledge-worker 任一项不可用时为
`degraded`。Qdrant 失败不会阻止 API 启动，API 对 Qdrant 使用
`service_started`，运行时由混合检索链路记录 `qdrant_unavailable` 并回退到
PostgreSQL 词法检索。

`embedding` 的健康检查要求 `/health` 返回 `status: ok`；
`knowledge-worker` 在 `8100` 提供内部 `/health`，并在工作循环内刷新
heartbeat。检查时使用独立 Compose project 和空闲宿主机端口，不要修改已有
`.env` 或删除用户卷：

```powershell
$env:POSTGRES_HOST_PORT='55480'
docker compose --env-file .env -f infra/compose.yaml --project-name codex-final-suite up -d postgres qdrant embedding knowledge-worker
docker compose --env-file .env -f infra/compose.yaml --project-name codex-final-suite ps postgres qdrant embedding knowledge-worker
docker compose --env-file .env -f infra/compose.yaml --project-name codex-final-suite run --rm --no-deps api python -c "from fastapi.testclient import TestClient; from app.main import app; print(TestClient(app).get('/system/health').json())"
```

## 模型首次下载和离线缓存

`embedding` 服务使用 `embedding-models:/models` 卷，`HF_HOME=/models/huggingface`。首次启动会下载 `EMBEDDING_MODEL_NAME` 与 `RERANKER_MODEL_NAME` 两个模型，之后复用同一卷即可离线运行。

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d embedding
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 logs -f embedding
```

模型就绪后 `/health` 返回 `status: ok`；加载中返回 `starting`；加载失败返回 `error`。嵌入服务无模型时 `POST /v1/embed` 与 `POST /v1/rerank` 返回 `503`，问答链路会记录 `embedding_unavailable` 或 `reranker_unavailable` 降级。

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm --no-deps api python -c "import httpx; print(httpx.get('http://embedding:8080/health', timeout=5).json())"
```

## 环境变量与密钥轮换

非敏感默认值写入 `.env.example`。`DEEPSEEK_API_KEY` 必须保持为空，由部署环境注入；仓库和日志不记录任何密钥。

轮换 `DEEPSEEK_API_KEY` 或超级管理员密码时，编辑 `.env` 后按顺序重启依赖该值的服务，不打印新值：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d api embedding knowledge-worker
```

`JWT_SECRET`、`POSTGRES_PASSWORD`、`SUPERADMIN_INITIAL_PASSWORD` 等运行时密钥的最小长度为 16 字符，且不得使用占位值。JWT 与超级管理员密码轮换流程见 [地震事件接入基础子系统运行手册](event-ingestion-foundation.md)。

## 迁移和启动顺序

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 build api embedding knowledge-worker frontend
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d postgres qdrant embedding
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d
```

`knowledge-worker` 在 `qdrant` 健康且 `embedding` 已启动后开始工作；API
仍要求 PostgreSQL 健康，但只要求 Qdrant/embedding 已启动，Qdrant 延迟
健康由运行链路记录降级。迁移头包含 AI 知识与问答表，启动后可用
`alembic current` 核验。

## 历史问答 API

查询同一会话历史回答：

```text
GET /api/v1/qa/sessions/{session_id}/answers?limit=50&cursor=<answer_id>
```

接口要求当前用户身份有效；普通用户只能读取自己创建的会话，超级管理员可
读取全部会话。结果按 `created_at DESC, id DESC` 稳定排序，`limit` 范围为
`1..100`，cursor 指向上一页最后一条回答。返回项包含问题文本/状态/时间和
完整 `answer`，answer 中包含 `model_name`、`model_version`、
`prompt_version`、`execution_plan`、`tool_call_summary`、
`structured`、`degraded_reasons` 等审计字段；旧回答缺少模型审计字段时以
`null` 或空结构返回。

## 知识源上传、URL 入库、发布、回滚

创建知识源、上传文件版本、创建 URL 版本、发布与回滚都通过 API 完成；`knowledge-worker` 消费队列中的 `fetch`、`ingest`、`publish`、`rollback` 任务。

```powershell
# 创建知识源（请求体不含密钥）
curl.exe -X POST "http://127.0.0.1:8000/api/v1/knowledge/sources" `
  -H "Authorization: Bearer <token>" `
  -H "Content-Type: application/json" `
  -d "{\"source_key\":\"<source_key>\",\"title\":\"<title>\",\"layer\":\"local_authority\",\"source_type\":\"preplan\",\"access_level\":\"internal\"}"

# 上传文件版本
curl.exe -X POST "http://127.0.0.1:8000/api/v1/knowledge/sources/<source_id>/versions" `
  -H "Authorization: Bearer <token>" `
  -F "version=<version>" `
  -F "file=@<path>"

# URL 入库
curl.exe -X POST "http://127.0.0.1:8000/api/v1/knowledge/sources/<source_id>/url-versions" `
  -H "Authorization: Bearer <token>" `
  -H "Content-Type: application/json" `
  -d "{\"version\":\"<version>\",\"source_uri\":\"https://www.sh.gov.cn/<path>\"}"

# 发布/回滚
curl.exe -X POST "http://127.0.0.1:8000/api/v1/knowledge/versions/<version_id>/publish" `
  -H "Authorization: Bearer <token>" `
  -H "Content-Type: application/json" `
  -d "{\"reason\":\"<reason>\"}"
```

回滚使用同结构请求 `POST /api/v1/knowledge/versions/<version_id>/rollback`。任务状态通过 `GET /api/v1/knowledge/jobs` 查看；失败任务可用 `POST /api/v1/knowledge/jobs/<job_id>/retry` 重试。

索引版本需要临时下线时使用 `POST /api/v1/knowledge/versions/<version_id>/disable`，请求体与发布接口相同。停用后版本不参与发布、检索或新快照，历史快照仍保留其锁定版本。使用同结构请求 `POST /api/v1/knowledge/versions/<version_id>/enable` 重新启用后，版本恢复为 `indexed`，必须再次显式发布；若 Qdrant 指针缺失或为空，则先使用 `/versions/<version_id>/rebuild` 重建。

## Qdrant 集合检查与从 PostgreSQL 重建

每个发布索引版本对应一个 Qdrant 集合，集合名保存在 `knowledge_index_versions.collection_name`，默认形如 `shanghai-knowledge-<source_key>`。写入前会校验稠密向量维度、稀疏向量配置和点 payload 字段；配置不一致时索引任务失败，不会覆盖旧点位。

```powershell
$check = @'
import asyncio
from qdrant_client import AsyncQdrantClient
from app.config import settings

async def main():
    client = AsyncQdrantClient(url=settings.qdrant_url)
    print(await client.get_collections())
    await client.close()

asyncio.run(main())
'@
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm --no-deps -T api python -c $check
```

若 Qdrant 集合丢失或为空，但 PostgreSQL 仍保留 `knowledge_chunks`，使用重建 API 排队强制 `index` 任务。该任务绕过既有成功 job 的幂等短路，按 `KNOWLEDGE_WORKER_BATCH_SIZE` 分批生成 embeddings 和 `PointStruct`，再以稳定 point ID 写入 staging collection。全部批次成功后才通过 Qdrant alias 原子切换；任一批次失败时 staging 被清理，仍可检索的 published 点位保持不变。同一版本已有 pending forced rebuild 时返回同一 job，不重复排队。

强制重建只 upsert 当前 PostgreSQL 切片对应的稳定 point ID，不删除孤儿或历史 stale points。当前 chunk 在正常生命周期内不可变，重建不会替换或删除现有 chunk ID，因此保留这些点不会污染当前版本的检索结果。若重试耗尽，只有 `index` job 进入 `dead_letter`，原有 `indexed`/`published` 版本状态和旧 Qdrant 点位保持不变。若未来流程开始替换或删除 chunk ID，必须先引入临时 collection/alias 切换，或在成功 upsert 后按 version 执行 stale-point 清理，并补充失败安全、清理后置条件和旧点保留测试；不得恢复到 upsert 前直接删除 published 点位。

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/knowledge/versions/<version_id>/rebuild" `
  -H "Authorization: Bearer <token>" `
  -H "Content-Type: application/json" `
  -d "{\"reason\":\"restore missing qdrant collection\"}"
```

后置核验：`GET /api/v1/knowledge/jobs` 中该 version 的最新 `index` job 必须为 `succeeded`，`result_payload.status` 必须为 `rebuilt`，`result_payload.chunk_count` 等于 `knowledge_index_versions.chunk_count`；`GET /api/v1/knowledge/versions/<version_id>` 的版本状态必须仍为 `indexed` 或 `published`。Qdrant 集合必须存在且对应 `version_id` 的点位数与上述 `chunk_count` 一致。

## DeepSeek、嵌入和重排故障降级

混合检索同时执行 Qdrant 稠密、Qdrant 稀疏和 PostgreSQL 三元组词法检索。任一依赖失败时，答案的 `degraded_reasons` 会包含 `qdrant_unavailable`、`postgres_unavailable`、`embedding_unavailable` 或 `reranker_unavailable`，并回退到可用路径；全部不可用时回答进入 `unavailable`。

DeepSeek 不可用或未配置密钥时，规划与生成阶段会返回 `unavailable`，不会伪造答案。核验单条回答：

```powershell
curl.exe "http://127.0.0.1:8000/api/v1/qa/answers/<answer_id>" -H "Authorization: Bearer <token>"
```

检查嵌入/重排服务健康状态与 Qdrant 集合可用性，结合 `degraded_reasons` 定位降级来源。

## 网页白名单与内网关闭验证

URL 入库受 `config/knowledge/source-whitelist.yaml` 约束：仅允许 `http`/`https`、标准端口、白名单域名、白名单内容类型，并阻断环回、私网、链路本地、组播、保留地址与云元数据地址。默认白名单为 `cea.gov.cn`、`gov.cn`、`mem.gov.cn`、`sh.gov.cn`、`samr.gov.cn`。

`ONLINE_SEARCH_ENABLED=false` 时 URL 抓取被直接拒绝，worker 的 refresh scan 也不会入队远程抓取。自动 scan 在 `knowledge-worker` 主循环内执行，间隔由 `KNOWLEDGE_ONLINE_REFRESH_INTERVAL_SECONDS` 控制；也可以手动触发一次扫描：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm knowledge-worker python -m app.knowledge.worker refresh-once
```

每次扫描按时间桶创建唯一的 `auto-refresh-<bucket>` 版本，重复扫描不会重复入队。内网关闭验证应确认白名单仅含公网域名，并尝试访问私有地址以确认 `UnsafeUrlError`。

## 事件知识快照核验

问答会话创建时，`KnowledgeSnapshotService` 冻结当时的发布索引版本、事件修订、评估运行、成果生产运行和数据资产版本，并写入 `knowledge_snapshots`。每个会话锁定一个 `snapshot_id`，回答使用 `snapshot.index_version_id` 检索，不随后续发布漂移。

核验某事件会话的快照指纹：

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT id, event_id, index_version_id, fingerprint, manifest FROM knowledge_snapshots WHERE event_id = \$q\$<event_id>\$q\$;"'
```

## 问答审计和超级管理员删除

超级管理员删除回答会写入 `qa_admin_audit_logs`，审计记录包含资源类型、资源 ID、操作者、事件和详情。

```powershell
curl.exe -X DELETE "http://127.0.0.1:8000/api/v1/admin/qa/answers/<answer_id>" -H "Authorization: Bearer <token>"
curl.exe -X DELETE "http://127.0.0.1:8000/api/v1/admin/knowledge/versions/<version_id>" -H "Authorization: Bearer <token>"
```

知识版本若被历史快照引用则拒绝删除。按事件、回答、操作者或时间清理审计日志使用 `DELETE /api/v1/admin/qa/operation-logs`，清理动作本身会保留一条审计记录。

## 十万切片性能测试

性能测试使用确定性假嵌入与假重排，真实写入测试 Qdrant 与 PostgreSQL，测量混合检索编排端到端延迟；真实 BGE 推理延迟在部署健康检查中单独记录。

```powershell
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 up -d postgres qdrant
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api alembic upgrade head
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 run --rm api pytest -m performance tests/test_qa_performance.py -v
```

默认 `pytest -q` 通过 `addopts=-m 'not performance'` 排除 `performance` 标记；只有显式 `-m performance` 才会运行十万切片测试。阈值：100000 个 500..1000 中文字符切片下，20 次请求排序后第 19 个（索引 18）P95 延迟不超过 1.0 秒，且每次请求返回非空证据。测试结束无论 setup、用例主体或 Qdrant 清理是否失败，都会继续删除 Qdrant 集合和 benchmark PostgreSQL 数据行。

真实 BGE 性能门禁不等价于上述确定性性能测试。先确认 embedding 返回
`status: ok`，再在同一部署设备上记录 `POST /v1/embed` 的
BGE-M3 稠密/稀疏向量延迟、`POST /v1/rerank` 延迟和 1 秒内可完成的请求
比例；不要把测试容器的共享磁盘/网络或模型下载阶段作为生产延迟。本收尾
未把未执行的真实 BGE 压测或真实 E2E 验收标记为通过。

真实 E2E 账号前置条件：

```text
E2E_SUPERADMIN_USERNAME=superadmin
E2E_VIEWER_USERNAME=e2e-viewer
E2E_SUPERADMIN_PASSWORD=<run-only-password>
```

`e2e_fixture.py` 会使用一次性密码创建/更新 superadmin 和 viewer；不要把该
密码写入 `.env`、日志或最终报告。

## 备份、恢复和磁盘扩容

PostgreSQL 逻辑备份：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 exec -T postgres sh -lc "pg_dump --encoding=UTF8 -U \`$POSTGRES_USER -d \`$POSTGRES_DB" |
  Set-Content -LiteralPath knowledge-qa-backup.sql -Encoding utf8NoBOM
```

恢复时先停止写入，再导入 UTF-8 备份：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -LiteralPath knowledge-qa-backup.sql -Raw -Encoding utf8 |
  docker compose --env-file .env -f infra/compose.yaml --project-name codex-task15 exec -T postgres sh -lc "psql -v ON_ERROR_STOP=1 -U \`$POSTGRES_USER -d \`$POSTGRES_DB"
```

`qdrant-data` 与 `embedding-models` 卷以及 `KNOWLEDGE_STORAGE_HOST_DIR`（默认 `../data/knowledge`）按各自生命周期独立备份；恢复模型卷可避免重复下载，恢复 Qdrant 卷后再从 PostgreSQL 重建索引以校验一致性。

磁盘扩容时先扩展 Docker Desktop 磁盘，再迁移 `KNOWLEDGE_STORAGE_HOST_DIR` 或卷所在位置。不要在未备份现有数据的情况下删除 `qdrant-data`、`embedding-models` 或 PostgreSQL 卷。
