# 专业制图与报告生产中心设计

## 1. 文档状态

- 日期：2026-09-30。
- 所属系统：上海市地震应急评估与辅助决策系统。
- 子系统：专业制图与报告生产中心。
- 本阶段范围：从正式速报、更正报、人工正式事件、测试事件或演练事件完成评估准备开始，到 27 类专业图件、4 类核心文档和 8 类背景文档形成版本化成果为止。
- 本设计不包含：工作组任务协同、指挥大厅完整布局、AI 问答、内网 EQIM 接入、现场灾情和遥感数据接入。

## 2. 建设目标

在正式速报、更正报、人工正式事件、测试事件或演练事件进入生产流程后，自动形成可追溯、可复核、可下载的专业成果包。首轮全量生产以 `AssessmentRun.deadline_basis_at` 和 `deadline_at` 为准，后续重算从 `rebuild_requested_at+300 秒`重新计时。对于上海市行政区域内 5.0 至 5.9 级正式速报事件，该基准时刻等于首份正式速报成功入库时刻 `T1`，全部 27 类图件和 12 类文档或演示稿必须在 `T1+5 分钟`内完成写入。

该 5 分钟是平台自动产出验收线，不是对外上报时限。自动产出完成后，仍为专家、领导、应急技术组专题图人工制图人员预留研判和加工时间。

生产中心必须同时满足以下目标：

- 只实现旧系统专业版图件和文档，不实现公众版、领导版、救援版、简图版或手机版。
- 27 类图件全部进入目标范围；数据尚未成熟的 B、C 类成果按可用数据生成并明确标记“待复核”，不伪造数据。
- 4 类核心成果包括快速评估简报 DOCX、快速评估专报 DOCX、辅助决策报告 DOCX、辅助决策报告 PPTX。
- 8 类背景成果包括背景信息、房屋统计、经济信息、人口信息、重点目标、空间距离、震区基本情况、历史及灾害地震目录，均为 DOCX。
- 图件输出 JPG 或 PNG，文档输出 DOCX，演示稿输出 PPTX。
- 首期不生成 PDF。
- 真实、测试、演练和回放共用同一生产链路，但使用独立状态、模板快照、文件名前缀和可见标识。
- 每份成果均绑定事件修订、评估运行、数据资产快照、模型参数、图件模板和文档模板版本。
- 修正报、重算和人工覆盖生成新版本，不覆盖历史成果。

## 3. 已确认的业务边界

- `T0` 为发震时刻，`T1` 为首份 CENC 正式速报成功入库时刻。自动速报阶段不生成灾情图件或报告。
- 生产基准时刻持久化为 `AssessmentRun.deadline_basis_at`，并只按以下规则取值：首份正式报取 `T1`；更正报取该份修正报成功入库时刻；人工正式事件、测试事件和演练事件取正式参数成功入库并创建评估触发记录的时刻。
- 人工正式事件、测试事件、演练事件以及没有 CENC 正式报的事件允许 `T1` 为空，不得再用 `T1` 为这些事件计算生产时限。所有事件的首轮生产时限均以对应 `AssessmentRun.deadline_basis_at` 为准，后续重算另行从请求时刻起算。
- 每份更正报创建新的评估运行和新生产运行，并从该份更正报成功入库时刻重新计算 5 分钟；不得沿用首次正式报的 `T1` 截止时间。
- 正式速报到达后，系统自动启动平台内评估流程，并向管理者提供制度响应启动建议；制度响应仍由局领导决定。
- 仅上海市行政区域内的有感事件、本市一至四级响应事件、外省波及上海事件，以及距上海市边界 20 公里以内（含）的外省事件形成自动评估和生产任务。
- 其他区域事件只记录和通知，不启动评估任务。
- CENC 是首期唯一自动地震信息来源，不作人工确认。
- 模型烈度、仪器烈度分别测算，再形成融合烈度；仪器烈度不可用时降级为模型烈度。
- 2 度以上事件自动生成成果，阈值后续可通过配置重设。
- 真实事件优先。真实事件发生时，测试任务停止调度并取消；演练任务按配置取消或终止。
- 测试和演练成果命名必须带不可移除的“测试”或“演练”标识。
- 首期部署在 Z440 本机外网环境，运行过程中不依赖互联网获取底图和模板。
- 高德地图作为主底图，天地图作为离线备用底图。高德 API 和天地图 API 仅用于数据准备与缓存刷新，震后生产使用本地 PMTiles、MBTiles 或等价离线缓存。
- 旧系统 `.mxd` 文件只作为版式、图层、图例和标注参考。因不再使用 ArcGIS Pro、ArcGIS Enterprise 或 ArcGIS Server，运行期不得依赖 ArcGIS。
- 中国地震局模板中的遥感影像图和不同幅面版本作为报送格式映射，不增加第 28 类图件。

### 3.1 测试与演练

- 测试事件用于验证平台接入、评估、制图、报告、存储和调度功能，可采用每日随机触发或人工触发，不要求实际工作组参加。
- 演练事件用于验证应急业务流程和人员协同，具有场景、参演单位和演练过程；本阶段先完成评估和成果生产，工作组流程后续接入。
- 测试和演练都使用合成地震，不得写成真实事件。
- 测试成果保留 400 天，演练成果保留 2 年。
- 测试和演练事件与真实事件共用生产链路，但使用不同可见标识、文件名前缀、运行状态和审计记录。

## 4. 总体方案

采用“生产运行、生产任务、渲染适配器、成果版本库”四层结构：

1. `ProductionRun` 表示一次评估修订对应的生产快照和总体状态，范围可以是全量 39 项，也可以是单个指定成果。
2. `ProductionTask` 表示一类图件或一份文档，具有独立依赖、截止时间、重试次数和结果。
3. MapLibre/Playwright、Matplotlib 与 python-docx/python-pptx 作为可替换渲染适配器。
4. `GeneratedArtifact` 保存文件元数据、校验和、模板版本、数据版本、质量和发布状态。

生产编排统一使用 `ArtifactProductionWorkflow`，但按运行来源区分启动方式：

- 每个评估运行的首轮自动全量生产可以由仍处于运行中的 `AssessmentWorkflow` 作为子工作流启动，并接收评估阶段信号。
- 首轮截止前核心产品替换形成的后续 `event_deadline` 全量运行、模板升级、底图切换、整批重算、单成果重算和管理员强制重新生成，必须以 `production_run_id` 为业务主键、作为顶层 Temporal 工作流启动，不得依赖已经结束的评估工作流继续创建子工作流。
- 超级管理员上传覆盖成果时同步创建合成生产运行，不经 Temporal。

首轮子工作流按以下阶段接收父评估工作流信号：

- 准备阶段：冻结事件、修订、数据资产、模型参数、模板和底图缓存版本，创建 39 个生产任务；尚未就绪的评估产品在对应依赖完成时再绑定到任务输入。
- 背景阶段：立即生成不依赖评估结果的背景图件和背景文档。
- 烈度阶段：融合烈度完成后生成地震影响场图。
- 损失阶段：建筑、人口、伤亡、经济、资源需求成果完成后分别触发对应图件和统计文档。
- 汇总阶段：依赖齐全后生成快速评估简报、快速评估专报、辅助决策报告和 PPTX。
- 校验阶段：检查 39 项是否全部形成可用成果、文件名和标识是否符合规则、文件是否存在且校验和一致。

该结构允许背景生产与评估计算并行，避免等待全部损失任务结束后才启动制图。

## 5. 成果目录与编号

### 5.1 27 类专业图件

所有专业图件使用旧系统专业版 `A3V` 工作幅面参考尺寸 `4761 x 3369`、300 DPI。输出格式默认 JPEG，允许在模板清单中按图件配置为 PNG。图中必须包含标题、事件摘要、图例、比例尺、指北针、数据来源、生成时间、质量标识和版本信息。

成熟度汇总：A 类 19 项，B 类 7 项，C 类 1 项，合计 27 项。

中国地震局模板中的 A4 横版、A3 横版、A0 交通图和 2.5、5、10、30、50 公里遥感影像等仅作为输出规格或报送映射，不增加成果类别。首期默认生成 `A3V` 专业版成果，其他幅面在成果目录中作为同一 `artifact_key` 的输出配置管理。

| 编号 | 图件 | 主要来源 | 首期成熟度 | 缺数处理 |
| --- | --- | --- | --- | --- |
| M01 | 城区疏散场地分布图 | 高德或公开应急设施数据、行政区、底图 | B | 有边界和震中即出图，附“疏散场地数据待复核” |
| M02 | 地震影响场分布图 | 融合烈度、行政边界、底图 | A | 融合不可用则阻止生成，不得用伪值替代 |
| M03 | 经济损失预估专题图 | 经济损失产品、行政区 | A | 核心损失产品缺失时阻止发布 |
| M04 | 救援力量需求专题图 | 资源需求产品、行政区 | A | 核心资源产品缺失时阻止发布 |
| M05 | 人员死亡预估分布图 | 人员伤亡产品、行政区 | A | 核心伤亡产品缺失时阻止发布 |
| M06 | 人员受伤预估分布图 | 人员伤亡产品、行政区 | A | 核心伤亡产品缺失时阻止发布 |
| M07 | 人员压埋预估分布图 | 人员伤亡产品、行政区 | A | 核心伤亡产品缺失时阻止发布 |
| M08 | 物资需求预估专题图 | 资源需求产品、行政区 | A | 核心资源产品缺失时阻止发布 |
| M09 | 震区 GDP 图 | GDP 栅格或区县经济数据 | A | 区县数据可用时使用分级设色 |
| M10 | 震区地震动峰值加速度区划图 | 地震动区划或 PGA 栅格 | B | 缺图层时保留行政区底图并标记“区划数据待复核” |
| M11 | 震区交通图 | 本地路网、行政边界、高德底图缓存 | A | 行政边界和底图必需；路网为可选增强层，缺失时降级并标记“路网数据待复核” |
| M12 | 震区历史地震分布图 | 历史及灾害地震目录 | A | 目录为空时明确显示“无匹配记录” |
| M13 | 震区人口分布图 | 街镇人口、行政区 | A | 核心人口资产缺失时阻止发布 |
| M14 | 震区水库分布图 | 公开水库或责任单位数据 | B | 缺图层时保留边界和震中并标记“待复核” |
| M15 | 震区危险源分布图 | MDB 基础数据、责任单位补充数据 | A | 无记录时输出空图层说明，不伪造点 |
| M16 | 震区学校分布图 | 教育设施数据 | A | 核心设施数据缺失时阻止发布 |
| M17 | 震区医院分布图 | 医疗设施数据 | A | 核心设施数据缺失时阻止发布 |
| M18 | 震中附近地铁分布图 | 高德 POI 缓存或公开轨道数据 | B | 缺数据时保留底图和震中并标记“待复核” |
| M19 | 震中附近地震台站分布图 | 权威台站数据 | B | 缺数据时输出缺数说明，不生成估计台站 |
| M20 | 震中附近活动断裂图 | 上海市活动断层、底图 | A | 无邻近断层时明确显示检索半径和无结果说明 |
| M21 | 震中附近建筑物分布图 | 房屋破坏产品、街镇房屋总量、行政区 | A | 按街镇聚合；任一受影响街镇缺少可靠房屋数据时失败，不得用零值代替 |
| M22 | 震中附近建筑物公里格网分布图 | 街镇房屋总量或损失建筑栅格 | C | 有可追溯模型聚合时显示 `spatialized_estimate=true` 和“模型分配，待复核”；无模型依据时失败，不生成估计格网 |
| M23 | 震中附近救援队伍分布图 | 公开或责任组救援队伍数据 | B | 缺数据时保留边界和震中并标记“待复核” |
| M24 | 震中附近文物单位分布图 | 文物部门公开数据 | B | 缺数据时保留边界和震中并标记“待复核” |
| M25 | 震中附近重要目标分布图 | 重点目标、生命线、关键设施 | A | 无记录时显示空图层说明 |
| M26 | 震中位置分布图 | 事件修订、行政边界、底图 | A | 始终可生成 |
| M27 | 震中与主要城市距离分布图 | 行政中心、空间距离计算 | A | 始终可生成 |

### 5.2 4 类核心文档或演示稿

| 编号 | 成果 | 格式 | 主要内容 | 主要依赖 |
| --- | --- | --- | --- | --- |
| D09 | 地震灾害快速评估简报 | DOCX | 事件概况、T0/正式报时刻、响应建议、烈度、人员、房屋、经济、资源和核心图件 | E、F、L、P、C、X、R、Q；M02、M05、M06、M13、M21、M26 |
| D10 | 地震灾害快速评估专报 | DOCX | 更完整的烈度方法、损失指标、空间分布、质量等级、历史与断裂背景 | F、L、P、C、X、R、Q；D01-D08、M01-M27 |
| D11 | 辅助决策报告 | DOCX | 响应等级分析、灾情趋势、重点区域、救援与物资需求、处置建议 | D09、D10；M02-M08、M20、M25-M27 |
| D12 | 辅助决策报告 | PPTX | 决策摘要、事件信息、灾情、空间分布、重点目标、资源需求和处置建议 | D11；M02-M08、M25-M27 |

依赖缩写：

- E：事件和修订。
- F：融合烈度。
- L：房屋破坏。
- P：受灾人口。
- C：人员伤亡。
- X：经济损失。
- R：应急资源需求。
- Q：损失质检。

D09 中的“正式报时刻”在 `T1` 为空时显示“不适用（人工/测试/演练）”，不得伪造 CENC 正式报时间。D09-D12 的图件依赖按表中 `artifact(artifact_key, output_profile)` 展开为可校验依赖图，首期默认规格统一为 `a3v-professional`。

D09-D12 的评估产品依赖按稳定 `product_key` 绑定，至少支持：

- `intensity.fusion`：融合烈度。
- `loss.buildings`：房屋破坏。
- `loss.population`：受灾人口。
- `loss.casualties`：人员伤亡。
- `loss.economic`：经济损失。
- `loss.resources`：应急资源需求。
- `loss.validate`：损失质检。

`intensity.model` 和 `intensity.instrument` 通过 `intensity.fusion` 的产品血缘进入报告，不单独作为 D09-D12 的执行依赖，避免仪器烈度正常降级时误阻断报告。D09 硬依赖融合烈度、五类损失产品、损失质检产品和列出的 A 类图件。D10 硬依赖融合烈度、五类损失产品、损失质检产品、D01-D08 和图件目录规定的 A 类图件；其 B、C 类图件属于 `optional_depends_on`。D11、D12 只把表中列出的 A 类重点图件作为硬依赖。

硬依赖满足规则：

- 依赖为 `succeeded` 时继续。
- 依赖为 `degraded` 时继续，但当前成果继承 `degraded`。
- 依赖为 `failed`、`timed_out` 或 `canceled` 时，当前成果进入 `failed`。

可选依赖满足规则：

- 报告必须等待每个可选依赖达到终态，或在报告组装保留时间所对应的 `optional_dependency_wait_cutoff_at` 截止。
- 可选依赖为 `succeeded` 时正常引用。
- 可选依赖为 `degraded` 时引用有效文件，并使当前报告 `degraded`。
- 可选依赖为 `failed`、`timed_out` 或 `canceled` 时省略该项，记录缺失原因，并使当前报告 `degraded`。
- 到 `optional_dependency_wait_cutoff_at` 仍未终态时，将该依赖视为省略并生成 `degraded` 报告，不得无限等待；若剩余时间不足以完成报告存储和校验，则报告按全局截止规则进入 `timed_out`。

### 5.3 8 类背景文档

| 编号 | 成果 | 格式 | 主要内容 |
| --- | --- | --- | --- |
| D01 | 地震背景信息 | DOCX | 事件参数、所在行政区、历史地震、邻近断裂和基础地理背景 |
| D02 | 震区房屋统计 | DOCX | 街镇房屋总量、结构分类、破坏统计和数据质量 |
| D03 | 震区经济信息 | DOCX | GDP、产业结构、经济损失结果和来源说明 |
| D04 | 震区人口信息 | DOCX | 常住人口、浮动人口、家庭户、年龄结构和受灾人口 |
| D05 | 重点目标信息 | DOCX | 学校、医院、危险源、救援队伍、文物单位和重要目标 |
| D06 | 震中空间距离分布 | DOCX | 到区县中心、街镇、主要城市、重点目标和断裂带距离 |
| D07 | 震区基本情况 | DOCX | 自然地理、行政区、烈度、人口、房屋、经济和关键风险概览 |
| D08 | 历史及灾害地震目录 | DOCX | 指定半径和震级阈值内的历史地震、灾害地震和统计表 |

### 5.4 命名规则

文件名统一为：

```text
{marker}{震中参考位置}_{震级}级地震_{成果名称}_V{三位版本号}_{yyyyMMdd-HHmmss}.{扩展名}
```

规则：

- `production_mode=live` 时 `marker` 为空。
- `production_mode=manual` 的真实小震 `marker` 为空。
- `production_mode=test` 时 `marker` 为 `【测试】`。
- `production_mode=drill` 时 `marker` 为 `【演练】`。
- `production_mode=replay` 时 `marker` 为 `【测试回放】`。
- 震中参考位置去除 Windows 非法字符并限制长度。
- 震级保留一位小数。
- 版本号在同一事件和同一成果内单调递增，从 `V001` 开始。
- 时间使用 `Asia/Shanghai`。
- 扩展名仅允许 `jpg`、`png`、`docx`、`pptx`。

示例：

```text
浦东新区_5.1级地震_震中位置分布图_V001_20260930-153000.jpg
【测试】浦东新区_5.1级地震_辅助决策报告_V001_20260930-153000.pptx
```

## 6. 成果模板体系

### 6.1 模板清单

建立版本化模板清单 `artifact_catalog`，每个成果至少声明：

- `artifact_key`：稳定的机器标识，例如 `map.epicenter`。
- `display_name`：中文成果名称。
- `kind`：`map`、`docx` 或 `pptx`。
- `legacy_code`：旧系统编号，例如 `T10`、`D09`。
- `priority`：调度优先级。
- `depends_on`：必须满足的前置评估产品或成果，使用类型化依赖数组。
- `optional_depends_on`：允许缺失或失败的展示型前置评估产品或成果，使用类型化依赖数组。
- `required_assets`：必需数据资产键。
- `optional_assets`：可选数据资产键。
- `template_key`：模板键。
- `format`：输出格式。
- `dimensions` 或 `page_size`：图幅或文档幅面。
- `marker_policy`：按 `production_mode` 控制实时、手工、测试、演练、测试回放标识。
- `failure_policy`：`block` 或 `degrade`。合格事件不得跳过成果；`block` 在依赖缺失时使任务失败，`degrade` 只在能够形成可识别、可追溯的降级文件时使用。
- `quality_policy`：最低质量等级和待复核条件。

A 类成果默认采用 `block`。只有不影响成果主体可读性、且已在目录中明确列为 `optional_assets` 的增强图层允许 `degrade`；M11 的路网层是首期明确例外，不得把其他 A 类必需依赖静默降级。

类型化依赖只允许以下两种形式：

```json
{"kind": "assessment_product", "product_key": "intensity.fusion"}
{"kind": "artifact", "artifact_key": "map.intensity", "output_profile": "a3v-professional"}
```

任务调度、完整性校验、重算范围检查和指纹计算必须读取同一份类型化依赖数组，不得再维护仅含成果键的第二套隐式依赖表。

### 6.2 图件模板

旧系统 `.mxd` 不能直接由新系统执行。实施时按旧系统专业版和示例图件重建以下版本化资产：

- MapLibre style JSON。
- 图层顺序和样式。
- 标题、副标题、页脚、图例、比例尺、指北针、指北图和来源区布局。
- 高德主底图离线缓存和天地图备用底图离线缓存引用。
- 数据图层与业务字段绑定。
- 图件专属注记、单位、分级阈值和缺失数据提示。

图件模板是声明式 JSON，不保存可执行脚本。模板变更必须发布新版本，进行代表性事件渲染测试后方可用于正式生产。

### 6.3 文档和演示稿模板

旧系统 DOCX 和 PPTX 作为结构和视觉参考。新系统使用受控模板包：

- DOCX 使用 python-docx 支持的段落、表格、页眉、页脚、分节、图片和样式。
- PPTX 使用 python-pptx 支持的母版、版式、占位符、表格、图片和文本。
- DOCX 页面尺寸读取 `w:sectPr/w:pgSz`，PPTX 页面尺寸读取演示文稿尺寸，不得依据旧文件名中的 `A4H`、`A3V` 等标记推断。
- 实现模板迁移时生成占位符清单、表格单元格映射、图片关系映射和页幅报告；旧模板中的 `X`、`XX` 等无结构占位符不得直接用于正式生成。
- 复杂文本框、SmartArt、ActiveX、外部链接和 Python 库无法稳定保留的对象必须转换为普通段落、表格、图片或受控文本，不得依赖 Office 自动修复。
- 模板中的动态字段使用显式占位符。生成后不得残留任何未替换占位符。
- 每次发布模板均生成清单、哈希和示例页，纳入版本库。

### 6.4 模板快照

生产运行创建时，将模板键解析为已发布模板版本并写入快照：

```json
{
  "catalog_version": "artifact-catalog-v1",
  "templates": {
    "map.default": {"version": "map-default-v3", "checksum": "..."},
    "doc.rapid_brief": {"version": "rapid-brief-v2", "checksum": "..."},
    "ppt.decision": {"version": "decision-deck-v1", "checksum": "..."}
  }
}
```

模板在运行中被修正时，原运行继续使用原快照；新模板只能用于新运行或由有权限用户发起的新版本重算。

## 7. 生产编排

### 7.1 触发条件

- 正式速报首次入库后，由现有评估 Outbox 和评估工作流触发。
- 更正报入库后创建新的评估运行和新生产运行，生产时限从该份更正报成功入库时刻 `+300 秒`计算，并在同一事务写入旧运行取消请求；旧运行不得继续占用更正报所需的渲染资源。
- 人工正式事件、测试事件和演练事件走同一编排入口；人工事件用于 CENC 未发布但本局已有正式报的小震。
- 人工正式事件、测试事件和演练事件以正式参数成功入库并创建评估触发记录的时刻为生产基准时刻，生产时限为“触发时刻 `+300 秒`”。这些事件允许 `AssessmentRun.t1_at` 为空。
- `production_mode` 默认映射为：正式报和更正报 `live`，人工正式事件 `manual`，测试事件 `test`，演练事件 `drill`。`replay` 只能由隔离回放命令显式指定，并使用独立事件和发布空间。
- 测试和演练事件必须使用独立事件标识，不得复用真实事件。
- 不满足平台生产范围的事件只记录和通知，不创建 `AssessmentRun` 或 `ProductionRun`。
- 同一评估运行允许因模板、渲染器、底图缓存、成果目录、模型参数或人工重算创建多个 `ProductionRun`；每个运行使用单调递增的 `generation_seq`。
- 单类成果重算、整批重算和管理员强制重新生成均创建新的 `ProductionRun`，通过 `generation_scope` 记录 `full` 或 `artifact:<artifact_key>:<output_profile>`，不复用已完成运行的 Temporal 工作流或 Activity 幂等键。
- 超级管理员直接上传覆盖成果时创建新的 `ProductionRun`（`generation_scope=artifact:<artifact_key>:<output_profile>`、`deadline_kind=rebuild_deadline`、一个 `superadmin_override` 任务）、`GeneratedArtifact` 和发布记录，再原子替换当前发布，不删除历史成果、原始报文或操作日志；若要求平台重新渲染，则同一运行按正常渲染器执行。
- `launch_mode=assessment_child` 用于每个评估运行的首轮自动全量生产，包括首份正式报、更正报、人工正式事件、测试事件和演练事件；同一评估运行中的核心产品替换后续运行、模板升级、底图切换、整批重算、单成果重算和管理员强制重新生成均为 `launch_mode=standalone`。直接上传覆盖不创建 Temporal 工作流，但在数据模型中仍使用同一生产运行和任务结构。

### 7.2 生产运行状态

`ProductionRun` 状态为：

- `pending`：已创建，等待冻结输入。
- `running`：至少一个任务已进入执行。
- `completed`：该运行范围内要求的全部成果形成 `complete` 或 `degraded`，通过整体校验，且最后一份成果提交时间不晚于 `deadline_at`。全量运行要求 39 项，单成果重算运行只要求该目标成果。
- `partial`：至少形成一份可用成果，但存在 `failed` 或 `timed_out` 成果，或全部范围成果完成时间超过 `deadline_at`；成功或降级成果仍保留，但该次生产验收不通过。
- `failed`：没有形成任何可用成果，或冻结输入、存储完整性、审计链等关键条件失败，无法形成可信成果。
- `canceled`：被真实事件优先级策略、人工取消、父工作流明确取消或更正报替代旧修订。测试或演练运行因真实事件预占而暂停调度时，以 `canceled + reason=real_event_priority` 结束；更正报替代时以 `canceled + reason=revision_superseded` 结束；需要继续时创建新的生产运行。

### 7.3 单个生产任务状态

- `pending`：等待依赖。
- `ready`：依赖满足，等待执行器。
- `running`：渲染或组装中。
- `succeeded`：文件形成且校验通过。
- `degraded`：文件形成，但因缺数或质量不足标记待复核。
- `failed`：达到最大重试次数仍失败。
- `timed_out`：达到生产总截止时间仍未形成可用结果，或成果在截止时间之后才完成存储提交。
- `canceled`：真实事件抢占或工作流取消。

合格事件不创建 `skipped` 成果。事件是否进入生产范围在创建 `ProductionRun` 前判定；全量运行必须覆盖 39 项任务，单成果重算运行只覆盖目标成果，范围内任务必须进入成功、降级、失败、超时或取消状态。

### 7.4 分段调度

第一阶段可并行：

- M01、M09、M10、M11、M12、M13、M14、M15、M16、M17、M18、M19、M20、M23、M24、M25、M26、M27。
- D01、D08。

融合烈度完成后：

- M02。

损失产品分批完成后：

- 建筑和人口产品完成后：M21、M22、D02、D04。
- 伤亡、经济、资源产品完成后：M03、M04、M05、M06、M07、M08、D03、D05。

汇总阶段：

- D06、D07。
- D09。
- D10。
- D11、D12。

同一成果不同阶段产生增强版本时，不覆盖早期版本。生产运行内可以选择最终发布版本，但过程版本继续保留。

### 7.5 Temporal 接口

新增 `ArtifactProductionWorkflow`：

```text
workflow_id = artifact-production:{production_run_id}
```

`assessment_run_id` 只作为业务关联键。一个评估运行下的多个生产运行必须使用彼此独立的 Temporal 工作流 ID。

输入：

```python
ArtifactProductionWorkflowInput(
    production_run_id: str,
    assessment_run_id: str,
    event_id: str,
    revision_id: str,
    deadline_at: datetime,
    catalog_version: str,
    context_fingerprint: str,
    launch_mode: str,
    generation_seq: int,
    generation_scope: str,
    required_outputs: tuple[tuple[str, str], ...],
)
```

首轮自动全量运行的 `launch_mode=assessment_child`。父评估工作流向子工作流发送阶段信号：

- `intensity_ready`
- `loss_core_ready`
- `loss_final_ready`
- `assessment_failed`
- `cancel_requested`

首轮自动全量运行以 `ParentClosePolicy=ABANDON` 启动。父评估工作流完成或失败时不得自动终止已启动的成果生产；父工作流只发送 `assessment_failed` 或正常完成信号。收到 `assessment_failed` 后，不依赖评估产品的背景成果继续完成，依赖缺失评估产品的任务以 `failed + error_category=assessment_unavailable` 结束；若已有背景成果则生产运行进入 `partial`，否则进入 `failed`。

同一评估运行中，除首轮全量生产以外的所有后续生产运行使用 `launch_mode=standalone`，由生产调度入口通过 Temporal Client 以 `artifact-production:{production_run_id}` 启动顶层工作流，不使用父工作流信号。

`standalone` 工作流的 `prepare_artifact_production` Activity 从已持久化的评估产品版本、成果版本和生产输入快照解析依赖绑定。对于尚未结束的评估运行，使用 `wait_for_artifact_dependencies` Activity 按固定退避间隔读取持久化产品/成果绑定，直到依赖满足或截止；不得回溯访问父工作流实例。对于评估已经结束后的重算，所有硬依赖必须已持久化，缺失时按任务失败规则处理。

主要 Activity：

- `prepare_artifact_production`
- `wait_for_artifact_dependencies`
- `render_map_artifact`
- `compose_docx_artifact`
- `compose_pptx_artifact`
- `validate_artifact_production`
- `publish_artifact_production`
- `mark_production_deadline_exceeded`

工作流根据 `launch_mode` 选择信号驱动或快照驱动方式，并按依赖图逐项调度 Activity。每类图件调用一次 `render_map_artifact`，每份 DOCX 调用一次 `compose_docx_artifact`，PPTX 调用一次 `compose_pptx_artifact`；不存在批量渲染全部背景、烈度或损失成果的汇总 Activity。

渲染和文档 Activity 必须定期心跳并响应取消信号。取消时终止当前 Playwright 页面或浏览器进程、关闭文档临时文件、释放渲染并发槽位，再把任务置为 `canceled`；不得仅修改数据库状态而让旧进程继续占用资源。

Activity 按成果独立执行并具有幂等键。任务依赖尚未就绪时 `input_fingerprint` 为空；依赖满足或按可选依赖规则完成省略判定后，固化类型化依赖绑定和任务输入指纹，再调度 Activity：

```text
artifact:{production_run_id}:{artifact_key}:{output_profile}:{task_input_fingerprint}
```

## 8. 图件渲染设计

### 8.1 渲染链路

1. `ArtifactContextBuilder` 读取事件、修订、产品、数据资产和模板快照。
2. `MapSpecBuilder` 生成不可变的 `MapRenderSpec`。
3. `MapRenderer` 启动或复用预热 Chromium 页面。
4. 页面加载本地 MapLibre、字体、样式、已选底图的 PMTiles/MBTiles 和 GeoJSON。
5. 页面等待所有图层完成、字体加载完成和布局稳定。
6. Playwright 在 `1587 x 1123` CSS 像素、`deviceScaleFactor=3` 下截图，得到 `4761 x 3369` 图像。
7. 后端验证尺寸、非空像素比例、文件大小、图例和标题边界。
8. JPEG 或 PNG 写入成果存储并计算 SHA-256。

### 8.2 MapRenderSpec

`MapRenderSpec` 至少包含：

```json
{
  "artifact_key": "map.epicenter",
  "title": "震中位置分布图",
  "event": {
    "event_id": "...",
    "revision_id": "...",
    "place": "浦东新区",
    "magnitude": 5.1,
    "origin_time": "...",
    "longitude": 121.5,
    "latitude": 31.2,
    "depth_km": 10
  },
  "viewport": {
    "center": [121.5, 31.2],
    "radius_km": 50,
    "padding": 40
  },
  "base_style": "gaode-local-v1",
  "layers": [],
  "legend": [],
  "source_notes": [],
  "quality": {
    "grade": "A",
    "needs_review": false,
    "missing_assets": []
  },
  "marker": null,
  "output": {
    "format": "jpg",
    "width": 4761,
    "height": 3369,
    "dpi": 300
  }
}
```

在底图来源选定前，先根据图件视口、缩放级别、像素缓冲区和输出尺寸生成与底图提供方无关的 `MapViewportTileManifest`，其中包含精确 `(z,x,y)` 必需瓦片集合。该清单同时用于校验高德和天地图候选包；通过校验并选定包后，才将该来源写入最终 `MapRenderSpec.base_style`。不得先固定 `base_style` 再反推校验范围。

渲染规格必须由服务端生成，浏览器端不得自行读取数据库或决定业务口径。

### 8.3 图层来源

- 底图：生产准备阶段校验高德缓存；通过校验则整批固定使用高德，不通过则校验天地图并整批固定使用天地图。生产运行一经创建不再中途切换底图来源。
- 行政区：`shanghai.admin.city`、`shanghai.admin.county`、`shanghai.admin.town`。
- 烈度：融合烈度栅格和矢量边界。
- 损失：损失产品指标和损失栅格。
- 基础专题：数据资产中心已发布版本。
- 距离：PostGIS 实时派生结果，但派生输入固定为生产快照。
- 历史地震、断裂、重点目标：版本化数据资产。
- 任一底图缓存均不可用时，相关图件任务失败；不得在震后运行期回退到互联网 API。
- 已选底图在单个成果渲染时发生瓦片解码失败，该任务按失败处理并记录瓦片坐标；需要换底图时创建新的生产运行并递增 `generation_seq`，避免同一运行、同一任务指纹产生不可复现文件。

### 8.4 缺失数据和降级

- A 类必需依赖缺失：任务标记 `failed`，不生成伪成果，其他独立成果继续执行。
- A 类成果声明的可选增强图层缺失时，主体成果仍可生成，但必须标记 `degraded`；该例外必须逐项写入 `artifact_catalog`，不得由渲染器临时决定。
- B 类可选依赖缺失：只要基础底图、震中和行政边界可用，就生成带“待复核”章和缺数说明的 `degraded` 成果。
- C 类成果只有在可追溯的模型聚合输入可用时生成，并标记 `degraded`、`spatialized_estimate=true`；否则标记 `failed`。
- 文档和演示稿必须按第 5.2 节等待硬依赖及可选依赖满足规则；可选依赖未满足时，先在文档中记录“依赖失败/超时/取消”或缺项说明，再生成 `degraded` 成果，不得把缺失内容画成空图或零值。
- 空结果与缺失数据不同。查询成功但结果为空时显示“检索范围内无记录”；查询失败或数据版本不存在时显示“数据暂缺，待复核”。
- 不允许使用前一次事件数据、全国均值或人工编写数值填补本轮缺失值。
- 合格事件的全量生产运行会调度全部 39 项任务；缺数规则只决定任务以 `succeeded`、`degraded`、`failed` 还是 `timed_out` 结束，不把成果标记为 `skipped`。单成果重算运行只调度其声明的目标成果。

## 9. 文档和演示稿生成

### 9.1 生成链路

1. `ArtifactContextBuilder` 汇总事件、评估产品、背景数据和图件版本。
2. `DocumentContextBuilder` 将数据转换为只读文档上下文。
3. `ChartRenderer` 使用 Matplotlib 生成文档中的统计图和趋势图。
4. `DocxRenderer` 克隆 DOCX 模板并替换文本、表格、图片和页眉页脚。
5. `PptxRenderer` 克隆 PPTX 模板并填充各版式和图表页。
6. 生成器执行占位符、空值、单位、数值精度和图片引用检查。
7. 对 DOCX 和 PPTX 执行结构校验。必要时通过 LibreOffice 仅作无头兼容性复核，不作为运行依赖。
8. 文件写入成果存储并计算 SHA-256。

### 9.2 文档控制信息

每份文档首页或固定控制区必须包含：

- 事件名称、震级、发震时刻、震中经纬度和深度。
- 数据来源和报告类型。
- 正式、测试或演练标识。
- 生成时间和成果版本。
- 事件修订号、评估运行号。
- 数据资产快照指纹。
- 模型参数版本和模板版本。
- 质量等级、降级原因和待复核项。

页脚至少包含文件名、页码、版本和系统生成标识。

### 9.3 实时性策略

为满足生产基准时刻 `+5 分钟`：

- D01、D08 等背景文档在评估计算期间并行准备。
- 损失指标和图表在评估产品写入时生成可复用片段。
- 文档汇总只进行模板填充和图片嵌入，不在最后阶段重新计算空间分析。
- 同一图件被多份文档引用时复用同一生成文件和校验和。
- PPTX 使用固定页数和版式，不动态插入无限内容。

## 10. 数据与版本快照

生产输入分为“运行级静态上下文”和“逐任务评估产品绑定”，两者不得混为一个在生产开始时无法闭合的指纹。

运行级静态上下文在 `ProductionRun` 创建时冻结，包含：

- 事件 ID、修订 ID、修订号、事件类型、`T1`（可为空）和 `AssessmentRun.deadline_basis_at`。
- 评估运行 ID 和允许依赖的评估产品类型清单。
- 数据资产快照指纹和每个资产的版本、校验和、角色。
- 高德和天地图离线底图缓存版本、覆盖范围、校验和及预选底图。
- 损失模型、参数包和区域配置版本。
- 图件模板、DOCX 模板、PPTX 模板版本和校验和。
- 字体包版本、渲染器版本、成果目录版本和命名规则版本。

静态上下文的规范化 JSON SHA-256 记为 `context_fingerprint`，在生产运行创建时确定且不再变化。评估产品 ID、版本、校验和不在该指纹中提前占位。

生产任务保留 `depends_on`、`optional_depends_on` 两个类型化依赖数组。每个依赖产生一条不可变 `artifact_task_dependency_bindings` 记录；每个生产任务在依赖满足或完成可选依赖省略判定时生成自己的 `input_fingerprint`：

```text
SHA-256(
    context_fingerprint
    + artifact_key
    + output_profile
    + sorted(
        dependency_kind,
        dependency_key,
        dependency_output_profile,
        bound_entity_id,
        bound_version,
        bound_checksum,
        resolution_status
      )
)
```

当依赖状态为 `bound` 或 `degraded` 时，`dependency_kind=assessment_product` 的绑定记录必须包含评估产品 ID、产品版本和产品校验和；`dependency_kind=artifact` 的绑定记录必须包含成果 ID、成果版本、输出规格和文件校验和。失败、超时或取消的硬依赖仍写入状态绑定记录后再使任务失败；被省略的可选依赖写入 `resolution_status=omitted_after_wait`。这些非成功状态均不写伪造 ID 或校验和。

任务指纹一旦生成即冻结；同一任务使用相同指纹重复调度时必须复用已有结果。全部任务完成后，生产运行计算最终的 `final_input_fingerprint` 仅用于审计和验收，不反向改变运行级上下文。

发生以下任一变化时，不得在现有 `ProductionRun` 中静默改写输入或覆盖成果，必须按对应规则创建新的 `ProductionRun` 并递增 `generation_seq`：

- 事件修订发生变化时，创建新的评估运行和 `event_deadline` 全量运行，以更正报入库时刻重新计时。
- 核心评估产品在首轮截止时间前被自动替换时，创建新的 `event_deadline` 全量运行并原样继承原 `AssessmentRun.deadline_at`，不延长 5 分钟；截止时间后才发生变化时不自动创建事件运行，原运行按超时规则结束，只能由有权限用户显式发起 `rebuild_deadline` 重算。
- 数据资产快照、模型参数、底图缓存、模板、成果目录、命名规则或渲染器发生变化时不自动改写运行中快照；当前运行继续使用原快照，有权限用户需要重生产时创建 `rebuild_deadline` 运行。
- 用户发起整批或单类成果重算时创建 `rebuild_deadline` 运行，从 `rebuild_requested_at+300 秒`计时。

模板升级、底图切换、手动重算和超级管理员重新生成不得复用已完成运行中的 Activity 幂等键。原运行、原任务和原成果全部保留，新运行通过 `rebuild_parent_run_id` 指向来源运行。

评估产品第一次到达并绑定到等待任务不触发新运行。自动替换仅在“首轮截止时间前、同一评估修订、核心评估产品产生新版本且旧版本从未完成任何可发布任务绑定”时触发；其他产品版本变化都按显式重算处理，避免晚到报文或重复回调造成运行风暴。

### 10.1 数据资产注册表补齐

现有注册表已覆盖行政边界、人口、房屋、经济、活动断层、GDP 栅格、DEM 和损失参数。图件依赖的其他数据必须先登记为版本化数据资产，禁止在渲染器内直接读取未登记文件。首期至少补齐以下资产键和契约：

| 资产键 | 类型与几何 | 最少字段 | 主要用途 |
| --- | --- | --- | --- |
| `shanghai.shelter.emergency` | 点或面 | `id`、`name`、`address`、`capacity`、`level`、`authority`、`updated_at` | M01、D05 |
| `shanghai.road.network` | 线 | `road_id`、`name`、`road_class`、`oneway`、`speed_limit` | M11、D06 |
| `shanghai.pga.raster` | 栅格 | 像元值、单位、坐标系、有效范围 | M10 |
| `shanghai.historical.earthquakes` | 表 | `event_id`、`origin_time`、`longitude`、`latitude`、`magnitude`、`depth_km`、`place`、`source`、`disaster_flag` | M12、D01、D08 |
| `shanghai.reservoir` | 点或面 | `id`、`name`、`reservoir_type`、`capacity`、`risk_level`、`authority`、`updated_at` | M14、D05 |
| `shanghai.hazard_source` | 点或面 | `id`、`name`、`category`、`risk_level`、`authority`、`updated_at` | M15、D05 |
| `shanghai.education.school` | 点或面 | `id`、`name`、`school_level`、`address`、`capacity`、`building_area`、`updated_at` | M16、D05 |
| `shanghai.health.hospital` | 点或面 | `id`、`name`、`hospital_level`、`address`、`beds`、`emergency_capacity`、`updated_at` | M17、D05 |
| `shanghai.metro` | 线和点 | `id`、`name`、`feature_kind`、`line_name`、`station_name`、`geometry` | M18 |
| `shanghai.seismic_station` | 点 | `station_code`、`name`、`network`、`longitude`、`latitude`、`elevation`、`status` | M19 |
| `shanghai.rescue_team` | 点 | `team_id`、`name`、`category`、`authority`、`personnel`、`base_address` | M23、D05 |
| `shanghai.cultural_relic` | 点或面 | `id`、`name`、`protection_level`、`address`、`authority` | M24、D05 |
| `shanghai.key_target` | 点或面 | `id`、`name`、`category`、`criticality`、`address`、`authority` | M25、D05 |
| `shanghai.lifeline` | 点、线或面 | `id`、`name`、`category`、`criticality`、`authority` | M25、D05 |
| `shanghai.distance.reference_points` | 点 | `id`、`name`、`category`、`longitude`、`latitude` | M27、D06 |

共同契约要求：

- 所有矢量资产必须声明 CRS；统一在导入阶段转换到 `EPSG:4326` 和 `EPSG:3857`，生产阶段不进行不可追溯的动态转换。
- 所有资产必须包含来源、更新时间、行政区域、发布版本、校验和、记录数和空间范围。
- 设施类资产必须区分“查询失败”“版本缺失”和“范围内无记录”。只有查询成功且确认“范围内无记录”时方可生成空图层说明。
- A 类成果引用的资产键必须在 `artifact_catalog` 中列为 `required_assets`；B、C 类资产按成果分别列入 `optional_assets` 或具备明确降级规则的 `required_assets`。
- 当前必需资产注册表继续保留 `shanghai.admin.city`、`shanghai.admin.county`、`shanghai.admin.town`、`shanghai.population.town`、`shanghai.building.town`、`shanghai.economy.county` 和 `shanghai.loss.parameters`，并按成果目录补充上述硬依赖和可选依赖。

高德和天地图离线缓存使用独立发布包，分别登记 `basemap.gaode.offline` 和 `basemap.tianditu.offline`。发布包至少记录缩放级别、覆盖边界、瓦片数量、格式、生成时间、最大有效年龄、来源声明和逐包校验和。

生产准备阶段必须完成以下硬校验后才允许选择底图并进入 `MapRenderSpec`：

- 包级校验和与发布清单一致，记录数、文件数和覆盖范围一致。
- 根据每个输出图件的 `MapViewportTileManifest`，按事件在 2.5、5、10、30、50 公里视口、全部缩放级别和像素缓冲区计算精确必需瓦片集合；使用同一瓦片方案分别与高德、天地图候选包的 PMTiles 或 MBTiles 索引逐项比对，必需瓦片缺失即为该候选包校验失败。
- 精确索引比对通过后，再按固定空间网格抽取至少 200 个代表性瓦片做解码、坐标和像素读取测试，作为压缩格式及索引正确性的额外健康检查；抽样通过不能替代精确必需瓦片集合校验。
- 包未超过清单声明的最大有效年龄；超期时不得静默使用。
- 在静态上下文中冻结实际底图来源、包 ID、版本、校验和和预选结果，并将实际来源写入每个地图成果的 `render_manifest`。

高德校验失败时改用天地图；两者均失败时，地图类任务直接失败，不联网补瓦片。

## 11. 数据模型

### 11.1 模板表

`artifact_templates`

- `id`
- `template_key`
- `kind`
- `display_name`
- `created_at`

`artifact_template_versions`

- `id`
- `template_id`
- `version`
- `status`：`draft`、`published`、`retired`
- `manifest`
- `checksum`
- `storage_path`
- `created_by`
- `published_at`
- `created_at`

### 11.2 生产输入快照表

`production_input_snapshots`

- `id`
- `production_run_id`
- `context_fingerprint`
- `region_id`
- `manifest`
- `created_at`

`production_input_snapshot_items`

- `id`
- `snapshot_id`
- `asset_key`
- `asset_version_id`
- `checksum`
- `role`
- `coverage`
- `selected_for_render`

每个生产运行拥有一个不可变生产输入快照。数据资产快照逻辑可以复用现有采集服务，但持久化必须独立于 `data_asset_snapshots.run_id=AssessmentRun.id`，不得受评估运行级 `(run_id, region_id, asset_key)` 唯一约束影响。快照和条目创建后只读；底图、模板、字体和渲染器版本也写入 `manifest`。

### 11.3 生产运行表

`artifact_production_runs`

- `id`
- `assessment_run_id`
- `event_id`
- `revision_id`
- `revision_no`
- `production_mode`：`live`、`manual`、`test`、`drill`、`replay`
- `launch_mode`：`assessment_child` 或 `standalone`
- `status`
- `priority`
- `deadline_basis_at`
- `deadline_at`
- `deadline_kind`：`event_deadline` 或 `rebuild_deadline`
- `deadline_exceeded_at`
- `last_artifact_committed_at`
- `cancel_requested_at`
- `cancel_reason`
- `started_at`
- `completed_at`
- `catalog_version`
- `input_snapshot_id`
- `context_fingerprint`
- `final_input_fingerprint`
- `generation_seq`
- `generation_scope`
- `required_outputs`
- `rebuild_parent_run_id`
- `is_current`
- `superseded_by_run_id`
- `superseded_at`
- `snapshot`
- `last_error`
- `created_at`
- `updated_at`

约束：

- `assessment_run_id` 不唯一；同一评估运行允许因模板、渲染器、底图缓存、成果目录、模型参数或人工重算形成多个生产运行。
- `assessment_run_id + generation_seq` 唯一；`generation_seq` 在同一评估运行内单调递增。
- `generation_scope=full` 时 `required_outputs` 必须恰好包含 39 个首期 A3V 专业版 `(artifact_key, output_profile)`；`generation_scope=artifact:<artifact_key>:<output_profile>` 时必须只包含目标输出。
- `input_snapshot_id` 必须指向与 `production_run_id` 一致的不可变输入快照。
- 事件触发后的首轮全量运行使用 `launch_mode=assessment_child` 和 `deadline_kind=event_deadline`，`deadline_basis_at` 等于对应 `AssessmentRun.deadline_basis_at`，并原样继承其 `deadline_at`。
- 更正报后的首轮全量运行使用 `launch_mode=assessment_child` 和 `deadline_kind=event_deadline`，由该更正报创建的新评估工作流启动，`deadline_basis_at` 为更正报成功入库时刻。
- 模板升级、底图切换、整批重算、单成果重算和管理员强制重新生成使用 `launch_mode=standalone`、`deadline_kind=rebuild_deadline`，`deadline_basis_at` 为重算请求提交时刻，`deadline_at=deadline_basis_at+300 秒`，不得沿用已经过期或正在倒计时的原截止时间。
- 首轮截止前核心产品替换形成的后续全量运行使用 `launch_mode=standalone`、`deadline_kind=event_deadline`，并继承原评估运行的 `deadline_basis_at` 和 `deadline_at`。
- 直接上传覆盖成果同样使用 `launch_mode=standalone`、`deadline_kind=rebuild_deadline` 和独立 `deadline_basis_at`，但其合成任务不进入 Temporal。
- 同一 `event_id + revision_id + generation_scope` 最多只有一条 `is_current=true AND superseded_at IS NULL` 的运行，通过部分唯一索引保证。
- 同一修订创建新的同范围运行，必须在事务中把旧运行的 `is_current` 置为 `false`，但旧运行和已发布成果仍可查询。
- 更正报入库时，旧修订对应的所有生产运行必须原子标记为被替代，写入 `cancel_requested_at` 和 `cancel_reason=revision_superseded`，并将优先级降为最低；事务提交后通过 Outbox 向对应工作流发送 `cancel_requested`。调度器收到信号后立即停止该运行获取新任务槽位，取消可取消的 Activity；新更正报运行使用最高优先级并可以预占旧运行槽位。旧运行晚完成时只能保留历史成果，不能成为当前发布。

### 11.4 生产任务表

`artifact_production_tasks`

- `id`
- `production_run_id`
- `artifact_key`
- `output_profile`
- `kind`
- `priority`
- `sequence`
- `status`
- `depends_on`
- `optional_depends_on`
- `optional_dependency_wait_cutoff_at`
- `deadline_at`
- `started_at`
- `completed_at`
- `attempt_count`
- `max_attempts`
- `input_fingerprint`
- `output_checksum`
- `final_artifact_id`
- `deadline_exceeded_at`
- `last_error`
- `result`
- `created_at`
- `updated_at`

约束：

- `production_run_id + artifact_key + output_profile` 唯一。
- `depends_on` 和 `optional_depends_on` 是第 6.1 节定义的类型化依赖数组，不使用只有成果键或隐式产品类型的模糊依赖；两个数组的 `(kind, dependency_key, output_profile)` 不得重复。
- `optional_dependency_wait_cutoff_at` 必须早于生产运行 `deadline_at`，并至少为成果存储、校验和发布事务预留配置的固定时间；无文档汇总依赖的任务该字段为空。
- `output_checksum` 与成果记录一致。
- `input_fingerprint` 在依赖就绪前为空，就绪后一次写入并冻结。
- `final_artifact_id` 指向该任务当前选中发布的成果版本。

### 11.5 任务依赖绑定表

`artifact_task_dependency_bindings`

- `id`
- `production_task_id`
- `dependency_kind`：`assessment_product` 或 `artifact`
- `dependency_key`：评估产品 `product_key` 或成果 `artifact_key`
- `dependency_output_profile`：成果依赖必填，评估产品依赖为空
- `is_optional`
- `bound_entity_id`
- `bound_version`
- `bound_checksum`
- `resolution_status`：`bound`、`degraded`、`failed`、`timed_out`、`canceled`、`omitted_after_wait`
- `resolution_detail`
- `resolved_at`
- `created_at`

约束：

- 使用两个部分唯一索引保证每个依赖只产生一条绑定记录：`UNIQUE (production_task_id, dependency_key) WHERE dependency_kind='assessment_product'`，以及 `UNIQUE (production_task_id, dependency_key, dependency_output_profile) WHERE dependency_kind='artifact'`。不得依赖 PostgreSQL 对空值默认不去重的普通唯一约束。
- `dependency_kind=assessment_product` 时 `dependency_output_profile` 为空；存在绑定字段时，`bound_entity_id`、`bound_version` 和 `bound_checksum` 必须共同引用同一评估运行内对应产品版本的不可变记录。
- `dependency_kind=artifact` 时 `dependency_output_profile` 非空；存在绑定字段时，三个绑定字段必须共同引用同一事件修订下对应的 `GeneratedArtifact`。
- 只有 `resolution_status=bound` 或 `degraded` 时，`bound_entity_id`、`bound_version` 和 `bound_checksum` 才必须非空且引用对应不可变记录；`failed`、`timed_out`、`canceled` 或 `omitted_after_wait` 允许绑定字段为空，但必须记录状态原因和判定时间。
- `resolution_status=omitted_after_wait` 时 `is_optional` 必须为 `true`。
- 硬依赖绑定为 `failed`、`timed_out` 或 `canceled` 后，当前任务必须进入 `failed`；可选依赖则按第 5.2 节规则省略并使当前任务 `degraded`。

### 11.6 成果表

`generated_artifacts`

- `id`
- `production_run_id`
- `production_task_id`
- `event_id`
- `revision_id`
- `artifact_key`
- `output_profile`
- `artifact_version`
- `is_final`
- `production_mode`
- `status`：`complete`、`degraded`、`failed`
- `quality_grade`
- `needs_review`
- `publication_mode`：`automatic`、`rebuild`、`superadmin_override`
- `generation_reason`
- `marker`
- `file_name`
- `format`
- `storage_path`
- `checksum`
- `size_bytes`
- `width`
- `height`
- `page_count`
- `template_snapshot`
- `data_snapshot`
- `render_manifest`
- `generated_at`
- `published_at`
- `superseded_by_id`
- `created_at`

约束：

- `event_id + artifact_key + output_profile + artifact_version` 唯一。
- 一个生产任务可以产生多个中间版本，但最多只能有一个 `is_final=true` 的最终版本。
- `checksum` 必须与存储对象一致。

### 11.7 发布表

`artifact_publications`

- `id`
- `event_id`
- `revision_id`
- `revision_no`
- `production_mode`
- `artifact_key`
- `output_profile`
- `artifact_id`
- `production_run_id`
- `generation_seq`
- `published_by`
- `published_at`
- `is_forced`
- `superseded_at`

正式成果发布时更新该表。数据库必须建立部分唯一索引：

```text
UNIQUE (event_id, artifact_key, output_profile, production_mode)
WHERE superseded_at IS NULL
```

`output_profile` 区分专业版 A3、报送 A4、A3、A0 等同一成果的不同输出规格。发布事务必须以行锁或可序列化隔离级别完成“旧记录写入 `superseded_at`、新记录插入”；同一事件、成果、输出规格和 `production_mode` 任一时刻只能有一条当前发布记录。超级管理员直接覆盖时同样插入新发布记录，不删除旧文件和历史记录。

发布前必须同时满足：生产运行 `is_current=true` 且未 `superseded`；成果修订号不低于当前有效修订号；不存在同一成果、输出规格且 `generation_seq` 更高的已发布记录。更正报到达时先将旧修订的当前发布记录标记为被替代，并显示“更正成果生成中”，同时按第 11.3 节取消旧运行并释放调度槽位，避免旧运行晚完成后反向覆盖。相同修订的模板或重算运行则在新运行成功发布时原子替换旧发布，失败的新运行不得使旧的可下载版本失效。

### 11.8 覆盖上传幂等表

`artifact_override_requests`

- `id`
- `actor_id`
- `endpoint`
- `idempotency_key`
- `request_fingerprint`
- `status`：`processing`、`succeeded`、`failed`
- `response_status`
- `response_body`
- `production_run_id`
- `artifact_id`
- `claimed_at`
- `lease_expires_at`
- `lease_generation`
- `attempt_count`
- `created_at`
- `completed_at`

约束与使用规则：

- `actor_id + endpoint + idempotency_key` 唯一。
- 请求进入文件校验前，先以 `INSERT ... ON CONFLICT DO NOTHING` 抢占幂等键并写入 `claimed_at`、`lease_expires_at`、初始 `lease_generation=1` 和 `attempt_count`；抢到的请求继续执行，未抢到的请求读取已有记录。
- `request_fingerprint` 覆盖事件 ID、修订 ID、成果键、输出规格、`expected_current_artifact_id`、文件名、文件大小、文件 SHA-256 和原因；同一幂等键但请求指纹不同必须返回冲突，不得复用首次结果。
- 首次请求仍在租约有效期内且状态为 `processing` 时，并发重复请求返回处理中或等待同一结果，不得各自创建合成运行。
- 合成运行、任务、成果、发布指针替换、`artifact_override_requests.status=succeeded`、关联 ID 和响应摘要必须处于同一个数据库事务。事务提交后幂等记录不可能仍停留在 `processing`；任一步失败则整体回滚。
- 租约过期后的恢复任务必须先检查是否存在已提交的关联运行、成果和发布记录：存在时补写幂等记录为 `succeeded`；不存在时使用 `UPDATE ... WHERE status='processing' AND lease_expires_at < now()` 原子续租、递增 `lease_generation` 并返回新代次，不能直接放行同一幂等键创建第二条发布。
- 每个执行者在内存中保存领取到的 `lease_generation`。最终事务必须先用 `SELECT ... FOR UPDATE` 锁定幂等记录，仅当状态仍为 `processing` 且数据库代次等于执行者代次时才创建运行、任务、成果和发布并更新为 `succeeded`。代次不匹配时，旧执行者必须回滚事务、删除本次临时对象和未引用存储对象，不得替换发布指针。
- 校验失败可把幂等记录置为 `failed` 并保存响应状态和摘要；重复请求直接返回已保存结果。

## 12. 后端模块

建议新增目录：

```text
backend/app/artifacts/
  __init__.py
  catalog.py
  domain.py
  models.py
  repository.py
  context.py
  dependencies.py
  naming.py
  service.py
  workflow.py
  worker.py
  router.py
  schemas.py
  storage.py
  validation.py
  renderers/
    base.py
    map_renderer.py
    chart_renderer.py
    docx_renderer.py
    pptx_renderer.py
```

职责：

- `catalog.py`：加载和校验成果目录。
- `domain.py`：定义生产状态、成果状态、模板、结果和质量对象。
- `models.py`：SQLAlchemy 持久化模型。
- `repository.py`：生产运行、任务、成果和发布的事务操作。
- `context.py`：构建不可变生产上下文。
- `dependencies.py`：生成依赖图并确定可执行任务。
- `naming.py`：生成规范文件名并校验测试、演练标识。
- `service.py`：生产任务幂等执行和结果提交。
- `workflow.py`：Temporal 工作流和 Activity。
- `renderers/map_renderer.py`：MapLibre/Playwright 图件渲染。
- `renderers/chart_renderer.py`：Matplotlib 统计图和趋势图。
- `renderers/docx_renderer.py`：DOCX 模板填充。
- `renderers/pptx_renderer.py`：PPTX 模板填充。
- `validation.py`：文件、元数据、命名和缺失标识校验。
- `storage.py`：成果二进制存储适配器。

成果存储首期使用本地内容寻址目录，路径根为 `ARTIFACT_STORAGE_ROOT`。接口保持 S3 兼容边界，后续可以替换为 SeaweedFS，不改变领域模型。

## 13. API

读取接口对现有 `superadmin`、`group_leader`、`group_deputy`、`group_member`、`viewer` 开放：

```text
GET /api/v1/assessments/runs/{assessment_run_id}/production
GET /api/v1/artifact-production-runs/{production_run_id}
GET /api/v1/events/{event_id}/artifacts?current=true&kind=map&status=complete
GET /api/v1/artifacts/{artifact_id}
GET /api/v1/artifacts/{artifact_id}/download
GET /api/v1/artifacts/{artifact_id}/thumbnail
GET /api/v1/events/{event_id}/artifact-versions
```

重算接口：

```text
POST /api/v1/events/{event_id}/artifacts/{artifact_key}/rebuild?output_profile=a3v-professional
```

超级管理员上传覆盖接口：

```text
POST /api/v1/events/{event_id}/artifacts/{artifact_key}/override?output_profile=a3v-professional
Content-Type: multipart/form-data
Idempotency-Key: <uuid>

file=<binary>
revision_id=<当前修订 ID>
reason=<操作原因>
expected_current_artifact_id=<可选的当前发布版本 ID>
```

上传覆盖流程：

1. 接口只允许 `superadmin` 调用，并校验事件、当前修订、成果键和 `output_profile` 均属于当前可发布范围。
2. 文件先写入隔离临时区并计算 SHA-256，不直接覆盖当前文件；随后按第 11.8 节原子抢占 `Idempotency-Key`、取得租约代次并比较请求指纹。
3. `revision_id` 必须等于当前有效修订；`expected_current_artifact_id` 存在时必须在事务内与当前发布指针一致，否则返回冲突，防止覆盖并发到达的更正成果。
4. 以扩展名、内容嗅探结果、格式解析结果三者一致为准，校验允许的格式、最大文件大小、损坏情况、占位符、尺寸或页面规格和模板元数据；图件还必须校验像素非空比例。
5. 校验通过后，在一个数据库事务内创建 `launch_mode=standalone` 的合成 `ProductionRun`、一个 `superadmin_override` 任务、一条 `GeneratedArtifact` 和一条 `artifact_publications` 记录，同时把对应的 `artifact_override_requests` 更新为 `succeeded` 并写入关联 ID；该任务不声明评估产品或成果依赖，并原子替换当前发布指针。
6. 文件在提交前先完成内容寻址存储写入和校验，数据库事务只登记已验证的不可变对象；若事务回滚，删除本次新写对象。旧发布记录只写 `superseded_at`，旧文件、原始报文、失败记录和操作日志均不删除。
7. 合成运行不调用 Temporal，创建时即完成快照冻结并在提交事务中进入 `completed`；任务进入 `succeeded`，成果进入 `complete`。缩略图按成果类型预生成或标记为预览待生成，下载和当前发布不受影响。

校验或存储失败时不替换当前发布；接口返回可操作的错误类别和审计记录。重复请求使用同一 `Idempotency-Key` 时只返回第一次处理结果，不得创建第二条覆盖版本。

权限：

- 应急技术组和 `superadmin` 可以触发单类成果重算。
- 重算必须创建新生产运行，不能覆盖历史文件；请求必须指定 `output_profile`，未指定时使用该成果的默认发布规格。
- 超级管理员可以通过独立上传接口强制覆盖错误成果并直接重新发布；不得复用 `rebuild` 接口携带任意二进制文件。
- 强制覆盖不改变测试或演练标识。

响应至少包含：

- 生产运行总体状态、`launch_mode`、`generation_seq`、`generation_scope`、范围内成果数、`deadline_basis_at` 和截止时间。
- `complete`、`degraded`、`failed` 数量。
- 每个成果的名称、版本、状态、质量、待复核项、文件大小和生成时间。
- 可直接下载的原始文件和缩略图地址。
- 数据资产、模型、模板和渲染器版本。
- 是否真实、测试或演练。

## 14. 前端成果中心

新增一级导航“成果中心”，并在事件详情页增加“生产进度”卡片。

### 14.1 事件详情卡片

- 显示 `生成中 / 已完成 / 部分完成 / 失败`。
- 显示完成数量，例如 `37/39`。
- 进度和 39 项总体验收取该修订最新的 `generation_scope=full` 运行；单成果重算运行只更新对应成果的历史版本和当前发布，不覆盖全量运行的 39 项计数。
- 显示图件、背景文档、核心文档三个分组计数。
- 显示超时、失败和待复核数量。
- 提供进入成果中心的链接。

### 14.2 成果中心页面

- 左侧固定事件、修订和评估运行上下文。
- 顶部显示真实、测试或演练状态条。
- 支持按成果类型、状态、质量和生成来源（系统自动、重算、超级管理员覆盖）筛选；工作组来源筛选在后续工作组协同子系统落地时再增加。
- 以紧凑列表展示缩略图、编号、名称、版本、状态、质量和生成时间。
- 点击成果打开右侧预览，不跳转空白页面。
- 可下载原文件。
- 失败的成果显示可操作错误信息，不显示伪缩略图。
- 有权限用户可触发重算。

### 14.3 显示规则

- 测试和演练标识使用不可关闭的高对比状态标签。
- “待复核”必须在缩略图列表和预览中同时可见。
- 降级成果不得使用与完整成果相同的绿色完成样式。
- 不开发手机端。

## 15. 权限、审计与保留

- 读取权限沿用事件和评估权限。
- 自动生产由系统账号执行。
- 人工重算记录操作人、时间、成果键、旧版本、新版本和原因。
- 超级管理员覆盖记录 `publication_mode=superadmin_override`。
- 普通用户不能删除任何生产运行、模板或成果。
- 测试成果保留 400 天。
- 演练成果保留 2 年。
- 正式成果长期保留。
- 模板和成果删除若被允许，必须先建立引用关系检查；存在历史引用的版本不能静默删除。

## 16. 异常处理和降级

- 单个成果失败不阻塞其他独立成果。
- 每个成果最多自动重试 3 次，指数退避，失败后记录错误类别和摘要。
- 浏览器崩溃后重建浏览器上下文并重试当前成果。
- 本地字体或模板缺失时立即失败，不自动访问互联网代替。
- 生产准备阶段在高德和天地图之间选定一个通过完整校验的底图包；已选包在成果渲染中发生瓦片错误时，该成果失败并保留瓦片、包版本和校验错误，不在运行中静默换图。系统可以创建新的生产运行显式改用备用底图。
- B、C 类缺数只降级一次，不进行无意义重试。
- 存储校验和失败视为渲染失败，删除损坏的临时文件。
- 核心报告依赖的关键图件失败时，报告任务不得使用旧事件图件或静默替换图片；D09 至 D12 以 `failed` 结束，不生成“完整正式包”。仅背景资料缺数时，报告可以按目录规则生成带缺数说明的 `degraded` 成果。
- 报告的可选依赖失败、超时、取消或到等待截止仍为运行时，按第 5.2 节省略对应展示项并生成 `degraded` 报告；该规则不适用于硬依赖。
- 生产运行达到总截止时间后，未完成的任务标记超时；已经成功的成果保留。
- Temporal 不可用时，评估产品仍可入库，但不得发布“完整生产完成”状态。

## 17. 性能设计

### 17.1 时限定义

```text
initial full run:
    production_basis_at = AssessmentRun.deadline_basis_at
    production_deadline = AssessmentRun.deadline_at = production_basis_at + 300 秒

rebuild run:
    production_basis_at = rebuild_requested_at
    production_deadline = rebuild_requested_at + 300 秒
```

基准取值规则：

- 首份正式报：`production_basis_at = T1`。
- 更正报：`production_basis_at = 修正报成功入库时刻`。
- 人工正式事件、测试事件和演练事件：`production_basis_at = 正式参数成功入库并创建评估触发记录的时刻`。

生产运行不得在评估计算结束后重新起算 5 分钟。评估产品每就绪一批，就立即释放对应生产任务，以共享同一个总截止时间。

计时终点为第 39 份成果成功完成存储写入、校验和计算和成果记录提交的时刻。数据准备、模板填充、图件渲染、写盘和校验全部包含在 300 秒内，不设额外宽限。

工作流必须设置与 `deadline_at` 对齐的定时 Activity 或计时器。截止时刻到达时：

- `pending`、`ready` 或 `running` 任务停止后续渲染并标记 `timed_out`。
- 已完成存储提交的 `succeeded`、`degraded` 成果继续保留。
- 正在写盘但尚未完成成果记录提交的文件不计为有效成果，移入临时区等待清理。
- 在截止时间之后才提交的成果不能把生产运行改为 `completed`。
- 最终有至少一份有效成果时运行状态为 `partial`，没有有效成果时为 `failed`；两种情况都记录 `deadline_exceeded_at` 和 `last_artifact_committed_at`。

### 17.2 预热

Temporal worker 启动后预加载：

- Chromium 可执行文件和浏览器上下文池。
- MapLibre 静态资源。
- 中文字体和地图字体字形。
- 高德主底图、天地图备用底图及其空间索引。
- DOCX、PPTX 模板缓存。
- 常用行政区、断层和设施 GeoJSON 内存副本。

### 17.3 并发

- 图件渲染默认并发 4，配置范围为 1 至 6。
- DOCX 和 PPTX 生成与图件渲染并行。
- Z440 的 CPU、内存和磁盘压力超过阈值时，自动降低到并发 2，优先保证真实事件。
- 真实事件运行优先级高于测试和演练；资源不足时取消测试或演练运行并记录 `real_event_priority`。后续重新运行使用新的 `generation_seq` 和 `deadline_kind=rebuild_deadline`，从重新触发时刻起算 `+300 秒`，原取消运行不参与验收。
- 更正报运行优先级高于同一事件旧修订的所有运行。更正报事务提交后立即将旧运行优先级降为最低、禁止其获取新槽位，并通过 Temporal 取消可取消 Activity；新更正报运行可以预占旧运行释放的渲染槽位，保证自己的 `+300 秒` 验收窗口不被旧成果占满。

### 17.4 资源预算

- 默认单成果临时文件在完成后立即清理。
- 成果存储写入采用临时文件、校验、原子重命名。
- 27 张 300 DPI 图件和 12 份文档的峰值临时空间按不低于 2 GB 规划。
- 成果版本保留策略、磁盘告警和备份策略由系统运维模块统一执行。

## 18. 监控

记录以下指标：

- 生产运行从创建到完成的耗时。
- 每类成果的准备、渲染、写盘和校验耗时。
- 各阶段并行任务数量和等待队列。
- 浏览器启动、崩溃、超时和重试次数。
- 图件非空像素比例、尺寸和输出大小。
- 模板缺失、占位符残留和文档生成失败次数。
- 降级、待复核和缺数成果数量。
- 成果存储容量、增长速度和校验失败次数。
- 测试、演练和真实事件之间的抢占次数。

日志关联 `event_id`、`revision_id`、`assessment_run_id`、`production_run_id`、`artifact_key` 和 `attempt_count`。

## 19. 测试策略

### 19.1 单元测试

- 成果目录必须恰好包含 27 类图件和 12 类文档。
- 所有成果键唯一，类型化依赖图无环，且不存在只含 `artifact_key` 的模糊依赖。
- 评估产品依赖能够解析为 `product_key`，并按同评估运行内产品 ID、版本和校验和生成任务绑定。
- 命名规则、版本递增、测试和演练前缀正确。
- 数据质量、降级和 A/B/C 类缺数策略正确。
- 合格事件不存在 `skipped` 成果，范围内任务终态只能是 `succeeded`、`degraded`、`failed`、`timed_out` 或 `canceled`。
- 运行级静态上下文变化或人工重算请求能够创建新的 `generation_seq` 生产运行。
- 任务依赖就绪后计算出的 `input_fingerprint` 稳定、可复现，并在同一任务重试时保持不变。
- 替换评估产品 ID、产品或成果版本、校验和、硬/可选依赖类型或可选依赖省略状态时，`input_fingerprint` 必须变化。
- 同一 `artifact_key` 的不同 `output_profile` 具有不同任务指纹、Activity 幂等键和重算作用域。
- 硬依赖 `degraded` 使下游 `degraded`，硬依赖 `failed/timed_out/canceled` 使下游 `failed`；可选依赖失败、超时、取消或等待截止未完成时按规则省略并使下游 `degraded`。
- 评估产品依赖和成果依赖分别由部分唯一索引约束；重复绑定、同一幂等键并发抢占和同键不同请求指纹都能被稳定拒绝。
- 修改模板不改变历史生产运行快照。

### 19.2 渲染器测试

- 每类图件使用固定样例数据生成成功。
- 输出尺寸、DPI、格式和颜色模式正确。
- 标题、图例、比例尺、来源和版本区不重叠。
- 地图、图例和注记区域非空白。
- 测试和演练文字已烘焙进图像，不只存在于元数据。
- DOCX 和 PPTX 可被对应库重新打开。
- 没有未替换占位符。
- 图片、表格、单位和数值精度正确。

### 19.3 集成测试

- 评估产品就绪后能够触发对应生产任务。
- 首轮自动全量运行可以作为 `AssessmentWorkflow` 子工作流接收阶段信号；评估工作流结束后，模板升级和单成果重算仍能以 `production_run_id` 顶层启动并完成。
- 单个图件失败不阻塞其他成果。
- 核心图件失败时依赖报告失败，背景数据缺数时依赖报告可以降级。
- 存储失败、模板失败、浏览器崩溃和超时能够重试或告警。
- 修正报生成新版本且旧版本可下载。
- 修正报的生产截止时间为该更正报入库时刻 `+300 秒`，不改用首次正式报 `T1`。
- 修正报事务提交后旧修订运行收到取消请求、优先级下降、停止获取新槽位，且更正报运行可以优先使用释放的渲染资源。
- 人工、测试和演练事件在 `t1_at` 为空时仍能按触发入库时刻完成生产。
- 测试或演练事件与真实事件并发时按优先级处理。
- 同一评估运行下模板升级生成新生产运行，旧运行和旧成果保持可追溯。
- 首轮截止前核心评估产品自动替换时，新运行继承原事件截止时间；截止后发生替换时不自动创建事件运行，只有显式重算才使用新 `+300 秒` 截止时间。
- 单成果重算生成 `generation_scope=artifact:<artifact_key>:<output_profile>` 的新运行，只更新该成果版本，不改变最新全量运行的 39 项进度。
- 重算运行的截止时间为 `rebuild_requested_at + 300 秒`，不沿用已经过期的首轮事件截止时间。
- 超级管理员上传覆盖通过“一运行一任务”的合成运行写入成果和发布表，发布前置校验与普通运行一致。
- 超级管理员上传覆盖在格式、尺寸、内容或校验和失败时不替换当前发布；同一 `Idempotency-Key` 并发重试、提交前崩溃、租约过期恢复和旧代次迟到提交均不产生重复发布，已提交事务的幂等记录能够正确恢复为 `succeeded`。
- 生产准备时高德缓存不可用、校验失败时能够整批选择天地图缓存；双缓存失效时不会访问互联网。
- 离线底图校验能按精确 `(z,x,y)` 必需集合发现单个缺失瓦片，200 个抽样瓦片通过不能掩盖该缺失。
- 评估运行 `completed` 与生产运行 `partial` 可以同时存在；成功成果可下载，但整体生产验收不通过。
- API 权限、下载、重算和超级管理员覆盖正确。

### 19.4 端到端验收

使用上海市行政区域内 `5.0` 至 `5.9` 级测试事件，在 Z440 目标环境执行完整流程：

1. 测试事件以正式参数成功入库并创建评估触发记录，不得伪装成 CENC 正式事件。
2. 系统记录 `deadline_basis_at` 并自动启动评估和生产。
3. 在 `deadline_basis_at + 300 秒`内生成 27 类图件和 12 类文档或演示稿。
4. 所有文件具有正确名称、校验和、版本和不可移除的“测试”标识。
5. 全部成果绑定同一事件修订、生产输入快照和逐任务输入指纹。
6. 网页成果中心可以查看、预览和下载，`T1` 显示为“不适用”。
7. 失败注入测试不会生成伪造结果。

性能验收至少执行三次预热后端到端运行，取最差结果。若最差结果超过 300 秒，则验收失败。

另执行一次隔离的正式速报回放，验证 `production_basis_at=T1` 的计算边界。回放运行使用不可变的 `production_mode=replay`，成果名和页面带 `【测试回放】` 标识，不得进入真实事件发布目录。

## 20. 首期实施顺序

1. 成果目录、模板清单、命名规则和依赖图。
2. 数据模型、迁移、仓储和只读 API。
3. 通用成果存储、版本发布和质检框架。
4. MapLibre 无头渲染基础设施和代表性图件。
5. A 类 19 类图件。
6. B、C 类 8 类图件的降级和待复核实现。
7. 8 类背景文档。
8. 4 类核心文档和演示稿。
9. Temporal 分段编排、并发控制和超时处理。
10. 前端成果中心。
11. 修正、重算、超级管理员覆盖和测试、演练规则。
12. 初始全量运行从 `deadline_basis_at` 起 300 秒的端到端性能验收，并单列正式报 `T1` 边界回放。

## 21. 验收标准

- 27 类图件全部纳入专业版目录，旧系统其他版本不进入首期。
- 4 类核心文档或演示稿和 8 类背景文档全部可生成。
- 图件仅输出 JPG 或 PNG，文档输出 DOCX，演示稿输出 PPTX，不生成 PDF。
- 上海市行政区域内 5.0 至 5.9 级正式速报事件从 `T1` 到全部 39 份成果最后一份写入不超过 5 分钟；更正报从修正报入库时刻重新计时。
- 人工、测试和演练事件从各自由触发入库确定的 `deadline_basis_at` 起 5 分钟内完成，不要求也不伪造 `T1`。
- 图件、文档、文件和元数据中的测试、演练标识不可移除。
- B、C 类缺数成果明确显示“待复核”，没有伪造数据。
- 所有成果可追溯到事件修订、评估运行、数据资产快照、模型参数和模板版本。
- 更正、重算和超级管理员覆盖生成新版本，旧版本不被静默覆盖。
- 单个任务失败可隔离，其他成果继续生成。
- 成果中心可以按事件查看 39 份成果的进度、质量、版本和下载入口。
- 运行期不依赖 ArcGIS、互联网底图或未缓存的外部模板；高德缓存不可用时可以切换到已验证的天地图离线缓存。

## 22. 旧系统资料映射

本设计参考以下本地资料：

- `D:\地震应急辅助决策系统\旧系统模板\模板类型.xlsx`：专业版、公众版、领导版、救援版、简图和幅面编号。
- `D:\地震应急辅助决策系统\旧系统模板\省内破坏\图件-专业版`：27 类专业版 `.mxd` 参考。
- `D:\地震应急辅助决策系统\旧系统模板\文档图模板.zip` 与 `模板.7z`：文档和图件模板参考。文档内嵌图的大图、小图模板属于模板资源，不增加专业图件类别。
- `D:\地震应急辅助决策系统\旧系统示例`：24 张 A3 图件、10 份 DOCX 和 1 份 PPTX，图件实测为 `4761 x 3369`、300 DPI。
- `D:\地震应急辅助决策系统\中国地震局模板【测试】上海浦东新区5.1级地震`：快速评估简报、快速评估专报、辅助决策报告、历史目录、遥感影像和不同幅面报送模板。
- `D:\地震应急辅助决策系统\基础数据`：上海应急基础数据 2022 MDB、DEM 2023 和 GDP 2020 栅格。
- `D:\地震应急辅助决策系统\规章制度`：上海市和国家级地震应急预案、响应等级、烈度表和现场工作规范。

## 23. 与现有系统的集成点

- `AssessmentRun` 和 `AssessmentTask` 继续作为评估阶段状态源。
- `AssessmentPlanBuilder` 从当前仅接受正式报和更正报扩展为接受人工、测试、演练、正式报和更正报；自动速报仍不启动评估。
- 事件生命周期在人工、测试、演练事件携带正式事件参数时创建评估触发记录，并沿用同一修订和版本模型。
- `AssessmentRepository` 不得再以 `event.t1_at is not None` 作为创建评估运行的前置条件，必须改为校验有效的 `deadline_basis_at`；人工、测试和演练事件允许 `t1_at` 为空。
- `AssessmentRun.t1_at` 的可空能力必须同步落实为数据库迁移、SQLAlchemy 模型、Pydantic 响应模型、快照序列化和 API 序列化；空值不得调用 `.isoformat()`，统一输出 `null` 或“不适用”。
- 3 个烈度产品和 6 个损失工作流产品共 9 个版本化产品继续作为图件生产的输入，其中损失质检产品用于控制可发布性。
- 现有 `data_asset_snapshots` 继续作为评估运行的基础数据锁定机制；制图报告中心新增独立 `production_input_snapshots`，避免同一评估运行下多个生产运行争用同一评估级唯一约束。
- 每个评估运行的首轮自动全量生产可以作为该 `AssessmentWorkflow` 的 `ParentClosePolicy=ABANDON` 子工作流；同一评估运行中的核心产品替换后续运行、模板升级、底图切换、整批重算、单成果重算和管理员强制重新生成均以 `production_run_id` 为工作流 ID 顶层启动，不依赖父评估工作流继续存活。首轮全量运行中，`AssessmentRun.deadline_basis_at + 300 秒` 和生产运行 `deadline_at` 必须完全一致；更正报首轮和重算运行分别按各自基准时刻重新计算截止时间。
- 原有 `report.rapid_assessment` 占位任务由独立的 `artifact.production` 生产监督任务替代。评估运行是否 `completed` 只由评估产品链决定；生产运行的正常终态包括 `completed`、`partial`、`failed`，并可按第 7.2 节进入 `canceled`。生产 `partial` 时保留成功成果并明确标记整体生产未通过，不反向把已经完成的评估运行改成 `failed`。`workgroup.response_tasks` 继续留给后续工作组协同子系统。
- 前端继续使用现有 React、TypeScript 和 MapLibre，不新增 ArcGIS 依赖。
- 后端继续使用 FastAPI、SQLAlchemy、PostgreSQL/PostGIS 和 Temporal。

## 24. 不在本阶段实施

- 公众版、领导版、救援版和简图版成果。
- PDF 导出。
- 现场灾情、遥感、无人机和现场工作队数据。
- 完整工作组任务接收、审核、退回和指挥大厅协同。
- AI 问答和知识库。
- 内网 EQIM 和正式仪器烈度网络接入。
- 移动端。
- ArcGIS 模板运行、ArcGIS 服务发布和在线几何编辑。
