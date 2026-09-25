# CENC 实时采集与自动事件生命周期设计规格

日期：2026-09-25
状态：已确认设计，待用户审阅
所属系统：上海市地震应急评估与辅助决策系统
上游规格：`docs/superpowers/specs/2026-09-17-shanghai-earthquake-emergency-decision-system-design.md`

## 1. 建设目标

建设独立的 CENC 实时采集子系统，将持续接收的地震自动速报、正式速报和正式报修订转换为平台标准事件，可靠记录 `T0`、首次正式报入库时刻 `T1`，并在正式报到达时持久化评估触发事件，供后续评估编排子系统消费。

本子系统首先解决以下问题：

- FAN Studio WebSocket 主链路持续接收 CENC 地震信息。
- Wolfx HTTP 备用链路低频常驻运行，补充主链路漏报并用于交叉核对。
- 两条链路进入同一去重、修订和生命周期流程。
- 自动速报只建立待定事件，不触发损失评估。
- 首次正式速报自动记录 `T1` 并产生唯一的评估触发。
- 后续正式报修订产生新版本评估触发，不覆盖历史成果。
- 采集连接、消息、错误和触发状态可监控、可诊断、可恢复。

## 2. 范围边界

### 2.1 本阶段范围

- 独立 `collector` 进程及容器。
- FAN Studio WebSocket 主链路。
- Wolfx HTTP 备用链路。
- CENC 自动速报和正式测定解析。
- 跨链路语义去重和事件修订。
- `T0`、`T1`、事件生命周期状态和评估触发 Outbox。
- 上海范围上下文解析及制度响应建议输入。
- 采集状态、健康检查和死信记录。
- 受认证的采集状态查询接口和管理页面。
- 自动化测试、故障注入测试和真实 PostGIS 集成验证。

### 2.2 明确不在本阶段范围

- 不实现损失评估、烈度计算或影响场模型。
- 不实现制图、报告和任务协同。
- 不实现 Temporal；未来评估编排子系统落地后消费 Outbox 并接入 Temporal。
- 不接入内网 EQIM。
- 不接入正式仪器烈度系统。
- 不把 FAN 或 Wolfx 标记为对外权威来源；平台内部统一来源仍为 CENC。
- 不自动替代领导决定制度响应等级。
- 不通过采集器自身 HTTP 接口回灌报文。

## 3. 已确认关键决策

- FAN Studio WebSocket 是主链路。
- Wolfx HTTP 是备用链路，但常驻低频运行，不只在主链路断开后启动。
- 两条链路只采用 CENC 数据。
- FAN 与 Wolfx 的重复报文不得重复建立事件、修订或评估触发。
- 正式速报到达后，平台自动启动评估流程；制度响应仍由领导决定。
- 首次正式速报入库时刻固定为 `T1`，后续修订不重置 `T1`。
- 上海行政区域及边界外 50 公里海域属于本地响应范围。
- 区域关系无法确定时返回 `pending`，不得猜测制度响应等级。
- 当前使用数据库 Outbox 作为采集和后续评估编排之间的稳定边界。
- 外部接口凭据通过环境变量或受控密钥文件注入，不进入源码和设计文档。

## 4. 总体架构

系统采用独立采集 Worker 和 PostgreSQL 持久化。

```text
FAN Studio WebSocket (主链路) ─┐
                               ├─> CollectorCoordinator
Wolfx HTTP 低频轮询 (备用链路) ─┘
                                      |
                                      v
                              原始报文持久化
                                      |
                                      v
                              CENC 归一化与去重
                                      |
                                      v
                             事件修订和生命周期
                                |           |
                                v           v
                        区域上下文解析   生命周期 Outbox
                                |           |
                                v           v
                         响应建议入库   未来评估编排消费
```

`collector` 与 API 共用后端代码、领域模型和数据库，但运行生命周期完全独立。API 重启、升级和前端故障不影响采集器；采集器重启不影响 API 和已有事件。

采集器直接调用领域服务，不通过 API 的 HTTP 接入接口回灌自身消息。现有 `POST /api/v1/ingest/auto` 继续保留为兼容入口且不触发评估；`POST /api/v1/ingest/formal` 和 `POST /api/v1/ingest/correction` 用于受控补录和故障恢复，并按 `trigger_reason=recovery` 进入同一生命周期事务，生成审计记录和评估 Outbox。

## 5. 组件设计

### 5.1 FANCollector

职责：

- 连接 FAN Studio WebSocket 主地址。
- 认证成功后订阅 CENC 地震列表。
- 周期发送查询消息，维持心跳和增量获取。
- 处理 `auth_success`、`auth_fail`、`initial_all`、`query_response`、`cenclist_response` 和 `update` 消息。
- 记录消息到达时间、提供方、原始消息类型和原始 JSON。
- 断线时指数退避重连，并轮换备用地址。
- 向 `CollectorCoordinator` 提交标准化前的 `CollectorEnvelope`。

默认连接地址：

- 主地址：`wss://ws.fanstudio.tech/all`
- 备用地址：`wss://ws.fanstudio.hk/all`

认证参数和订阅名称均通过配置注入，不在代码中写入密钥。

### 5.2 WolfxCollector

职责：

- 常驻轮询 `https://api.wolfx.jp/cenc_eqlist.json`。
- 默认每 10 秒请求一次，允许通过配置调整。
- 解析 `No1`、`No2` 等动态键。
- 仅提交 `automatic` 或 `reviewed` CENC 消息。
- 记录 HTTP 状态、响应时间、最后成功时间和最后有效报文时间。
- 请求失败时按退避策略重试；连续失败时标记备用链路降级。

Wolfx 在 FAN 正常时仍持续运行，用于补漏和核对，不因主链路健康而停止。

### 5.3 CollectorCoordinator

职责：

- 接收两条链路的 `CollectorEnvelope`。
- 固定内部事件来源为 `cenc`，同时保留实际提供方和传输链路用于审计。
- 将同一批消息按报文时间顺序串行处理。
- 保存原始报文、归一化事件、生成语义指纹并调用现有事件服务。
- 将无法解析或无法持久化的消息分类为可重试错误或死信。
- 更新 `collector_runtime_state`。

### 5.4 LifecycleCoordinator

职责：

- 根据事件已有修订和当前报文判断 `auto`、`formal` 或 `correction`。
- 自动速报只维护待定事件。
- 首个正式报设置 `T1`，生成评估触发。
- 内容发生变化的后续正式测定生成 `correction` 修订和新评估触发。
- 只有新且当前正式修订产生评估触发；乱序旧修订只保留审计记录。
- 语义重复报文不创建修订或新触发。
- 晚到自动报不替换正式报。

### 5.5 RegionContextResolver

职责：

- 使用版本化区域边界判断震中是否位于上海本地响应范围。
- 本地响应范围包括上海市行政区域和边界外 50 公里海域。
- 对范围外震中计算到上海边界的最短距离。
- 输出 `inside_shanghai`、`distance_to_boundary_km`、边界版本和计算来源。
- 边界数据缺失或版本不可用时返回 `pending` 上下文，不猜测结果。

区域边界不得以硬编码多边形形式写入业务代码。首期可使用带来源、校验和和版本号的公开候选边界；正式数据到位后通过数据管理流程发布新版本。

## 6. 外部接口与配置

### 6.1 FAN Studio 协议

连接后发送认证消息，字段包括 `type`、`appId` 和 `key`。认证成功后发送 CENC 列表查询。采集器处理以下消息：

- 认证结果消息。
- 初始全量响应。
- 查询响应。
- `cenclist_response` 历史列表响应。
- 增量更新消息。

`initial_all` 和 `query_response` 的 CENC 业务数据位于顶层来源键 `message["cenc"]["Data"]`；`cenclist_response` 的 `Data` 为历史事件对象映射。

规范的客户端标识配置名为 `FAN_APP_ID`。现有 `CENC_APP_ID` 仅作为兼容别名，在未配置 `FAN_APP_ID` 时回退读取；二者都只表示 FAN Studio 客户端标识，不表示 Kanameishi 私有接口凭据。

### 6.2 Wolfx 协议

接口返回 JSON 对象，使用 `No1`、`No2` 等键保存最近事件。每个事件至少包含：

- `EventID`
- `type`
- `time`
- `ReportTime`
- `placeName`
- `magnitude`
- `depth`
- `latitude`
- `longitude`

### 6.3 新增环境配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `CENC_COLLECTOR_ENABLED` | `false` | 是否启动实时采集器；Compose 中 collector 服务显式设为 `true` |
| `FAN_APP_ID` | 必填 | FAN Studio 客户端标识；缺省时兼容读取 `CENC_APP_ID` |
| `FAN_API_KEY` | 必填 | FAN Studio 密钥，使用 `SecretStr` |
| `FAN_WS_PRIMARY_URL` | `wss://ws.fanstudio.tech/all` | FAN 主地址 |
| `FAN_WS_BACKUP_URL` | `wss://ws.fanstudio.hk/all` | FAN 备用地址 |
| `FAN_QUERY_INTERVAL_SECONDS` | `10` | FAN 查询和心跳间隔 |
| `WOLFX_CENC_URL` | `https://api.wolfx.jp/cenc_eqlist.json` | Wolfx CENC 地址 |
| `WOLFX_POLL_INTERVAL_SECONDS` | `10` | Wolfx 轮询间隔 |
| `CENC_BOOTSTRAP_LOOKBACK_HOURS` | `24` | 首次启动处理窗口 |
| `COLLECTOR_STALE_AFTER_SECONDS` | `60` | 链路无有效消息的降级阈值 |
| `COLLECTOR_SPOOL_DIR` | `/var/lib/collector-spool` | 数据库故障时的本地持久化缓冲目录 |
| `COLLECTOR_MAX_SPOOL_BYTES` | `1073741824` | spool 容量上限；达到上限时停止接收并告警 |

所有凭据只从环境或受控密钥文件读取。示例文件和日志不得包含真实密钥。

## 7. 数据模型与事务边界

### 7.1 事件表扩展

`raw_messages` 新增：

- `provider`：`fan`、`wolfx` 或 `api`，记录实际接收提供方。
- `ingest_lane`：`websocket` 或 `http`，记录实际传输链路。

`earthquake_events` 新增：

- `t1_at`：首次正式报入库时刻，可空。
- `lifecycle_state`：`auto_pending`、`formal_triggered` 或 `correction_triggered`。
- `latest_trigger_revision_id`：最近一次产生评估触发的修订 ID，可空。

`earthquake_revisions` 新增：

- `semantic_fingerprint`：同一事件内唯一的语义指纹。
- `provider`：`fan`、`wolfx` 或 `api`，仅用于审计，不改变 CENC 内部来源。
- `ingest_lane`：`websocket` 或 `http`。
- `ingested_at`：采集器成功接收报文的时刻。
- `inside_shanghai`、`distance_to_boundary_km`、`region_boundary_version`、`region_computed_at`：本次修订使用的区域边界快照。

### 7.2 语义指纹

语义指纹对以下内容规范化后计算 SHA-256：

- 内部来源 `cenc`。
- 报文类别组：`auto` 或 `reviewed`。
- `T0`。
- 经纬度、震级和深度。
- 有报次时使用报次；无报次时使用提供方明确给出的正式报文时间。

指纹不包含 CENC 事件编号、提供方名称、传输方式和仅由提供方添加的字段。事件编号只用于定位和合并标准事件，不用于证明报文内容变化。`formal` 与 `correction` 属于同一 `reviewed` 类别组，因此 FAN 与 Wolfx 对同一正式报的不同封装不会重复生成修订。

同一事件内 `(event_id, semantic_fingerprint)` 具有唯一约束。

### 7.3 生命周期 Outbox

新增 `event_lifecycle_outbox`：

| 字段 | 说明 |
| --- | --- |
| `id` | UUID 主键 |
| `event_id` | 标准事件 ID |
| `revision_id` | 触发评估的事件修订 ID |
| `trigger_type` | `assessment.requested` |
| `trigger_reason` | `live` 或 `recovery` |
| `payload` | 评估所需的稳定事件快照引用 |
| `status` | `pending`、`processing`、`published`、`failed` 或 `dead_letter` |
| `attempt_count` | 消费尝试次数 |
| `created_at` | 创建时间 |
| `available_at` | 最早可处理时间 |
| `published_at` | 成功发布给后续编排器的时间 |
| `last_error` | 最近错误摘要 |

唯一约束为 `(event_id, revision_id, trigger_type)`。

事件入库、修订、响应建议、生命周期字段更新和 Outbox 写入必须在同一数据库事务内完成。

### 7.4 采集运行状态

新增 `collector_runtime_state`，按链路保存：

- 链路名称和提供方。
- 当前状态 `starting`、`healthy`、`degraded`、`critical` 或 `stopped`。
- WebSocket 连接状态或 HTTP 最近状态码。
- 最后连接成功时间。
- 最后有效报文时间。
- 最后成功入库时间。
- 最后成功处理的来源时间水位，用于恢复时按来源时间补漏。
- 连续失败次数。
- 重连次数。
- 最后错误摘要和更新时间。

### 7.5 死信记录

新增 `collector_dead_letters`，保存：

- 提供方、传输链路和原始消息标识。
- 原始 JSON。
- 错误类别和错误摘要。
- 首次失败时间和最后失败时间。
- 处理状态 `open`、`retried` 或 `resolved`。

不包含任何凭据或认证消息正文。FAN 认证消息只记录成功或失败结果，不保存密钥。

## 8. 数据流与生命周期

### 8.1 自动速报

1. 采集器接收 `automatic` 报文。
2. 保存原始报文和提供方信息。
3. 归一化为 `EventKind.AUTO`。
4. 计算语义指纹并执行跨链路去重。
5. 新建或更新待定事件，设置 `lifecycle_state=auto_pending`。
6. 不写评估 Outbox。

### 8.2 首次正式速报

1. 采集器接收有效 `reviewed` 报文。
2. 保存原始报文和提供方信息。
3. 归一化并识别为首次正式报。
4. 将采集器接收并成功入库的时刻记录为 `T1`。
5. 计算上海范围和制度响应建议输入。
6. 生成 `EventKind.FORMAL` 修订和响应建议。
7. 设置 `lifecycle_state=formal_triggered`。
8. 在同一事务内写入唯一 `assessment.requested` Outbox。

### 8.3 正式报修订

1. 同一标准事件收到内容有变化的后续 `reviewed` 报文。
2. 语义指纹与现有正式报不同。
3. 生成 `EventKind.CORRECTION` 修订。
4. 不修改原始 `T1`。
5. 重算响应建议并生成新的 `assessment.requested`。
6. 设置 `lifecycle_state=correction_triggered`。

### 8.4 乱序和重复

- 完全相同的报文按键值和语义指纹双重去重。
- 语义相同但封装不同的 FAN/Wolfx 报文合并为一条修订。
- 自动报晚于正式报到达时只保留历史修订。
- 正式报乱序时按报次优先、正式报文时间其次排序。
- 补录批次按报文时间升序处理。

### 8.5 恢复

- 正常运行重启使用数据库检查点补录遗漏报文。
- 首次部署只自动处理最近 24 小时事件。
- `last_processed_source_time` 只作为上游恢复查询/补漏窗口的下界，不得过滤 live、spool 或死信报文。
- 启动时必须先清空 spool，再恢复实时接收；运行中数据库恢复后也必须先排空 spool，再处理新的实时队列项。
- 早于当前水位的 spool/死信报文仍进入同一生命周期流程，由语义指纹和“新且当前”规则决定保存与是否触发。
- 恢复期间形成的正式报触发使用 `trigger_reason=recovery`。
- 早于首次启动窗口的历史数据不自动触发评估，必须通过受控回放命令处理。

## 9. 区域上下文和响应建议

- 本地响应范围由版本化区域边界解析，不使用地名文本猜测。
- `inside_shanghai=true` 表示震中位于上海市行政区域或边界外 50 公里海域。
- 范围外事件计算到上海边界的距离。
- 外省事件缺少死亡人数时，不猜测重大及以上制度响应等级。
- 区域边界不可用时制度响应返回 `pending`，服务响应只对明确属于本地区域的事件计算。
- 区域判断结果保存边界版本和计算时间，确保可追溯。

## 10. 异常处理与降级

### 10.1 FAN 主链路

- 首次连接失败使用 3 秒起步、最长 30 秒的指数退避。
- 连续失败后轮换主备 WebSocket 地址。
- 认证失败不进行高频重试，状态标记为 `degraded` 并告警。
- 认证失败或断线期间，Wolfx 继续承担接收任务。
- 重连成功后立即执行一次 CENC 列表查询，补齐断线期间消息。

### 10.2 Wolfx 备用链路

- 请求超时或非 2xx 状态按退避策略重试。
- 连续失败达到阈值后标记 `degraded`。
- FAN 和 Wolfx 同时不可用时标记 `critical`。
- 单个报文格式错误不阻塞后续报文。

### 10.3 持久化

- 数据库临时不可用时，采集消息在有界内存队列中短暂等待，并记录接收时刻。
- 存储重试仍失败或队列溢出时，将原始业务 envelope 原子写入本地 spool，并记录接收时刻。
- 数据库恢复后按接收时间升序重放 spool，成功一条删除一条；排空顺序必须早于后续 live 消息。
- spool 写入失败或容量耗尽时停止 supervisor 并告警；无法可靠保留时不得继续假装接收成功。
- 认证消息和无业务值的协议控制消息不写入原始报文表。

### 10.4 死信

- 解析错误、字段越界和无法识别的报文类型进入死信。
- 死信不自动无限重试。
- 管理员修复解析规则后可执行受控重放。
- 重放仍使用原始报文和原接收时间，不能把历史报文伪装成实时报文。

## 11. 状态查询与界面

### 11.1 API

新增 `GET /api/v1/collector/status`，仅允许已认证的超级管理员、组长和副组长访问。

返回：

- 总体状态。
- FAN 和 Wolfx 分别的状态。
- 最后报文时间。
- 最后成功入库时间。
- 连续失败次数。
- 死信数量。
- 当前区域边界版本。
- 最近一次成功入库事件 ID。

`CollectorStatusService` 统一聚合运行状态、开放死信数量、当前边界版本和最近入库事件；路由层不直接拼装数据库查询。

正式报和修订兼容接口的成功响应必须包含落库后的 `lifecycle_state` 与 `t1_at`，供受控补录、故障恢复调用方和端到端测试确认生命周期结果。

### 11.2 管理页面

现有前端新增采集状态页：

- 使用醒目的主链路、备用链路和总体状态标识。
- 显示连接状态、最后报文时间和错误摘要。
- 支持手动刷新，不提供浏览器内直接修改凭据。
- 普通事件列表显示 `auto_pending`、`formal_triggered` 或 `correction_triggered`。
- 测试和演练事件不得误用采集状态。

页面不显示 App ID、API Key、完整认证载荷或敏感连接信息。

## 12. 安全与审计

- 密钥使用 `SecretStr` 或受控密钥文件，不写入日志、异常、接口响应和前端。
- 共享 `Settings` 默认禁用 collector；仅 collector 服务显式启用并要求凭据，API 和数据库迁移不因缺少 FAN 密钥而失败。
- 死信只保存业务报文，不保存认证报文。
- 状态接口要求认证和角色授权。
- 原始报文、提供方、传输链路、接收时间和语义指纹均可追溯。
- 采集器不修改现有超级管理员覆盖和删除权限规则。

## 13. 测试设计

### 13.1 单元测试

- FAN 认证成功、认证失败和协议消息解析。
- FAN 主备地址轮换、退避和重连。
- Wolfx 动态 `NoN` 键解析和错误响应。
- CENC 自动、正式和修订分类。
- 语义指纹稳定性和跨提供方一致性。
- `T0`、`T1` 和生命周期状态转换。
- 区域边界内外和 50 公里海域判断。
- 死信分类和凭据脱敏。

### 13.2 服务测试

- 自动报不产生 Outbox。
- 首次正式报只产生一条 Outbox。
- 重复正式报不重复产生 Outbox。
- 有内容变化的修订产生新 Outbox 且保留 `T1`。
- 晚到自动报不替换正式报。
- 乱序正式报不覆盖更新修订。
- 响应建议和 Outbox 与事件修订原子提交。

### 13.3 集成测试

- 使用真实 PostgreSQL/PostGIS 验证迁移、唯一约束和事务回滚。
- 使用本地假 FAN WebSocket 服务验证认证、断线、重连和备份地址。
- 使用本地假 Wolfx HTTP 服务验证轮询、超时、重试和补漏。
- 同时发送 FAN 和 Wolfx 等价报文，验证只生成一条修订和一条触发。
- 恢复窗口内补录正式报，验证 `trigger_reason=recovery`。

### 13.4 端到端测试

- 采集状态页能显示健康、降级和临界状态。
- 自动报进入事件列表但不显示已触发评估。
- 正式报进入列表并显示已触发评估。
- Wolfx 在 FAN 断开后仍能接收并触发一次正式报。

## 14. 验收标准

- `collector` 独立启动，不依赖 API 进程存活。
- FAN 正常时通过 WebSocket 接收 CENC 自动报和正式报。
- Wolfx 常驻轮询，FAN 断线时仍可完成接收。
- 两链路重复报文不产生重复事件、修订或评估触发。
- 自动报不产生评估触发。
- 首次正式报在事务提交后产生唯一 Outbox，并固定记录 `T1`。
- 内容变化的正式修订生成新修订和新 Outbox，不重置 `T1`。
- 乱序到达的旧正式修订不生成新的评估触发。
- 采集器重启后可补录持久化检查点之后的遗漏报文。
- 数据库故障期间的消息可保存在本地 spool，并在恢复后按原接收时间补录。
- 首次启动不会把超过 24 小时的历史事件自动当作实时事件触发。
- FAN 认证失败、断线、Wolfx 失败和双链路失败均能正确降级并出现在状态接口。
- 区域边界缺失时制度响应为 `pending`，不生成虚构等级。
- 日志、API 和页面不泄露密钥。
- 真实 PostgreSQL/PostGIS、假 FAN WebSocket、假 Wolfx HTTP 和前端端到端测试全部通过。

## 15. 后续演进

- 评估编排子系统消费 Outbox，并以 Temporal Workflow 编排评估、制图和报告。
- 增加 CENC 仪器烈度明细采集。
- 增加正式 EQIM 适配器，保持相同标准事件和生命周期接口。
- 通过数据管理中心发布正式上海边界和海域版本。
- 增加区级区域边界和独立部署配置。
- 增加企业微信、邮件、电话和短信告警适配器。
