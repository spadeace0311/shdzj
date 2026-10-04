# 工作组任务协同与指挥大厅 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有评估、制图和报告系统上增加七工作组任务协同、成果版本流转、通知和 `7680x2430` 指挥大厅，不改变现有 300 秒成果生产和评估验收边界。

**Architecture:** 采用模块化单体。新增 `app.collaboration` 保存工作组、任务、成果版本和通知业务；新增 `app.command_hall` 保存只读投影和接口。事件生命周期 outbox 触发任务生成，现有 Temporal 继续只负责自动评估和成果生产，人工任务状态以 PostgreSQL 为主数据源。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy async、PostgreSQL/PostGIS、Alembic、pytest、React 19、TypeScript、Vite、Vitest、Playwright、MapLibre GL JS、ECharts。

**Spec:** `docs/superpowers/specs/2026-10-03-workgroup-collaboration-command-hall-design.md`

## Global Constraints

- 工作组固定为新闻信息值守组、监测预报组、综合协调组、震害评估组、应急技术组、后勤保障组、中心站组。
- 现场工作队不单列为第八组，纪检监督组不进入平台成果流转。
- 任务默认分派到工作组，不要求个人逐项签收；平台记录实际处理贡献人。
- 组长到岗时由组长确认发布；组长未到岗时，按事件名单顺序由第一位到岗副组长代理。
- 正式速报或人工正式事件生成协同任务的目标为 `P95 <= 5 秒`。
- 自动评估、27 类专业图件、4 类核心文档或演示稿、8 类背景文档仍必须按既有要求达到 `T1+300 秒`。
- 既有成果终态必须保持 `28 complete + 11 degraded`、`0 failed`、`0 timeout`，本计划不得放宽。
- 系统自动版、人工修订版、超级管理员覆盖版并存，同一成果最多一个当前发布版。
- 正式事件优先于测试和演练，测试和演练标识不可移除。
- 测试事件和成果保留 400 天，演练事件和成果保留 2 年。
- 指挥大厅主分辨率固定为 `7680x2430`，同时验证 `1920x1080`。
- 本地账号体系，不启用二次验证，不增加高风险操作密码复验。
- 不开发手机端，不实现完整指令审批，不实现正式多租户。
- 不读取、输出、提交或暴露 `.env`、密码、Token、API Key、连接串。
- 使用 `apply_patch` 修改文件；每个任务完成后运行指定验证并独立提交。

## Execution Prerequisites

1. 使用 `superpowers:using-git-worktrees` 验证或创建隔离工作树。
2. 确认当前分支为 `codex/workgroup-collaboration-command-hall` 或由其派生的执行分支。
3. 在 `infra` 目录启动标准 PostgreSQL 服务并准备测试数据库。不得打印 `.env`。
4. 在 `backend` 目录运行 `alembic upgrade head`，确认迁移为干净状态。
5. 在 `backend` 目录运行 `pytest -q` 建立基线。
6. 在 `frontend` 目录运行 `npm test -- --run` 和 `npm run typecheck` 建立基线。

## File Structure

### Backend Collaboration

- `backend/app/collaboration/domain.py`：枚举、值对象、状态转换和模板定义。
- `backend/app/collaboration/models.py`：工作组、任务、成果、通知、投影 outbox 的 SQLAlchemy 模型。
- `backend/app/collaboration/templates.py`：版本化模板目录和适用规则。
- `backend/app/collaboration/repository.py`：工作组、名单、任务、成果事务仓储。
- `backend/app/collaboration/roster.py`：名单、到岗和代理判定。
- `backend/app/collaboration/generation.py`：事件 outbox 消费和任务生成。
- `backend/app/collaboration/service.py`：任务生命周期、临时任务和成果流转。
- `backend/app/collaboration/artifact_link.py`：自动成果关联。
- `backend/app/collaboration/notifications.py`：通知端口、站内消息和回落策略。
- `backend/app/collaboration/scheduler.py`：超时、临期和通知调度。
- `backend/app/collaboration/schemas.py`：HTTP 请求和响应模型。
- `backend/app/collaboration/router.py`：工作组、任务、成果接口。
- `backend/app/collaboration/worker.py`：outbox、通知、超时和投影处理进程。

### Backend Command Hall

- `backend/app/command_hall/models.py`：事件、分组和告警投影模型。
- `backend/app/command_hall/projector.py`：投影重建和增量刷新。
- `backend/app/command_hall/service.py`：只读查询和活跃事件选择。
- `backend/app/command_hall/schemas.py`：HTTP 和 SSE 响应模型。
- `backend/app/command_hall/router.py`：总览、分组、任务、活跃事件和 SSE 接口。

### Template and Infrastructure

- `config/collaboration/shanghai-2026-tasks.yaml`：七工作组预案模板。
- `backend/migrations/versions/0018_collaboration_command_hall.py`：协同与大屏数据库迁移。
- `infra/compose.yaml`：新增协同 worker。

### Frontend

- `frontend/src/pages/WorkgroupTasksPage.tsx`：任务列表、详情、处理和确认。
- `frontend/src/pages/CommandHallPage.tsx`：大屏总览与钻取。
- `frontend/src/components/CollaborationTaskPanel.tsx`：任务状态和成果流转组件。
- `frontend/src/components/CommandHallGroupBoard.tsx`：七工作组总览。
- `frontend/src/components/CommandHallDetailDrawer.tsx`：分组和任务详情抽屉。
- `frontend/src/api/client.ts`：新增协同和大屏 API 客户端。
- `frontend/src/types.ts`：新增协同和大屏类型。
- `frontend/src/styles.css`：任务页面和大屏样式。

---

### Task 1: Collaboration Schema and Migration

**Files:**
- Create: `backend/app/collaboration/__init__.py`
- Create: `backend/app/collaboration/domain.py`
- Create: `backend/app/collaboration/models.py`
- Create: `backend/migrations/versions/0018_collaboration_command_hall.py`
- Create: `backend/tests/test_collaboration_schema.py`
- Modify: `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: `app.db.Base`, existing `users`, `earthquake_events`, `earthquake_revisions`, `assessment_runs`, `generated_artifacts`, `artifact_publications`.
- Produces: `WorkgroupCode`, `DutyRole`, `TaskStatus`, `TimelinessState`, `TaskSourceType`, `DeliverableRequirementKind`, `DeliverableSourceKind`, `CollaborationOutbox`, and all persistent collaboration tables.

- [ ] **Step 1: Write the failing schema test**

```python
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


async def test_collaboration_tables_and_deputy_order_constraint(session):
    connection = await session.connection()
    table_names = await connection.run_sync(
        lambda sync: set(inspect(sync).get_table_names())
    )
    assert {
        "workgroup_definitions",
        "workgroup_memberships",
        "workgroup_roster_snapshots",
        "workgroup_attendance",
        "collaboration_settings",
        "collaboration_task_templates",
        "collaboration_task_template_versions",
        "collaboration_tasks",
        "collaboration_task_contributors",
        "collaboration_task_deliverables",
        "collaboration_deliverable_versions",
        "collaboration_deliverable_publications",
        "collaboration_task_events",
        "collaboration_notification_deliveries",
        "collaboration_projection_outbox",
        "command_hall_event_projections",
        "command_hall_group_projections",
        "command_hall_alert_projections",
    } <= table_names

    first_user = uuid.uuid4()
    second_user = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO users (
                id, username, password_hash, role, workgroup, is_active
            )
            VALUES
                (:first_user, 'deputy-1', 'not-used', 'group_deputy',
                 'monitoring_forecast', true),
                (:second_user, 'deputy-2', 'not-used', 'group_deputy',
                 'monitoring_forecast', true)
            """
        ),
        {"first_user": first_user, "second_user": second_user},
    )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    INSERT INTO workgroup_memberships (
                        user_id, workgroup_code, duty_role, deputy_order
                    )
                    VALUES
                        (:first_user, 'monitoring_forecast', 'deputy', 1),
                        (:second_user, 'monitoring_forecast', 'deputy', 1)
                    """
                ),
                {"first_user": first_user, "second_user": second_user},
            )
            await session.flush()
```

- [ ] **Step 2: Run the schema test and verify it fails**

Run:

```powershell
cd backend
pytest tests/test_collaboration_schema.py -v
```

Expected: FAIL because collaboration tables and models do not exist.

- [ ] **Step 3: Implement domain enums, models, and migration**

Define stable enums in `domain.py`:

```python
class WorkgroupCode(StrEnum):
    NEWS_INFORMATION = "news_information"
    MONITORING_FORECAST = "monitoring_forecast"
    COMPREHENSIVE_COORDINATION = "comprehensive_coordination"
    DAMAGE_ASSESSMENT = "damage_assessment"
    EMERGENCY_TECHNOLOGY = "emergency_technology"
    LOGISTICS = "logistics"
    CENTER_STATION = "center_station"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    NOT_REQUIRED = "not_required"
    FAILED = "failed"


class TimelinessState(StrEnum):
    ON_TIME = "on_time"
    AT_RISK = "at_risk"
    OVERDUE = "overdue"
```

Create SQLAlchemy models with these mandatory constraints:

```python
__table_args__ = (
    CheckConstraint(
        "duty_role != 'deputy' OR deputy_order IS NOT NULL",
        name="ck_workgroup_membership_deputy_order_required",
    ),
    Index(
        "uq_workgroup_membership_active_deputy_order",
        "workgroup_code",
        "deputy_order",
        unique=True,
        postgresql_where=text(
            "is_active AND duty_role = 'deputy' AND effective_to IS NULL"
        ),
    ),
    Index(
        "uq_workgroup_membership_active_leader",
        "workgroup_code",
        unique=True,
        postgresql_where=text(
            "is_active AND duty_role = 'leader' AND effective_to IS NULL"
        ),
    ),
)
```

Use partial unique indexes for:

- one current active leader per group;
- one current publication per deliverable;
- one projection row per event/group;
- one contributor per task/user.

The migration must:

- create all tables and indexes;
- seed the seven `workgroup_definitions` rows and one default
  `collaboration_settings` row with an intensity threshold of `2.0`;
- use `ondelete="CASCADE"` for child business rows;
- use `ondelete="SET NULL"` for optional cross-module references;
- provide a complete reverse `downgrade()`.

Update `LATEST_REVISION` in `backend/tests/test_migrations.py` to:

```python
LATEST_REVISION = "0018_collaboration_command_hall"
```

Add a reversibility test that downgrades to `0017_production_cancel_outbox`, asserts collaboration tables are absent, upgrades to head, and asserts they exist.

- [ ] **Step 4: Run schema and migration tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_schema.py tests/test_migrations.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration backend/migrations/versions/0018_collaboration_command_hall.py backend/tests/test_collaboration_schema.py backend/tests/test_migrations.py
git commit -m "feat: add collaboration persistence model"
```

---

### Task 2: Workgroup Roster, Attendance, and Deputy Authority

**Files:**
- Create: `backend/app/collaboration/roster.py`
- Create: `backend/app/collaboration/schemas.py`
- Create: `backend/app/collaboration/router.py`
- Create: `backend/tests/test_workgroup_roster.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `WorkgroupDefinition`, `WorkgroupMembership`, `WorkgroupRosterSnapshot`, `WorkgroupAttendance`, `User`.
- Produces:
  - `RosterService.replace_group_members(session, group_code, members, actor) -> None`
  - `RosterService.snapshot_for_event(session, event_id) -> tuple[WorkgroupRosterSnapshot, ...]`
  - `RosterService.set_attendance(session, event_id, group_code, user_id, state, actor) -> WorkgroupAttendance`
  - `RosterService.resolve_confirming_authority(session, event_id, group_code) -> ConfirmingAuthority | None`
  - HTTP routes under `/api/v1/workgroups` and `/api/v1/events/{event_id}/workgroups`.

- [ ] **Step 1: Write failing roster tests**

```python
async def test_deputy_authority_uses_first_present_deputy_by_order(
    session, event_factory, user_factory
):
    event = await event_factory()
    leader = await user_factory("leader", "group_leader", "monitoring_forecast")
    deputy_1 = await user_factory(
        "deputy-1", "group_deputy", "monitoring_forecast"
    )
    deputy_2 = await user_factory(
        "deputy-2", "group_deputy", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER, None),
            MemberInput(deputy_1.id, DutyRole.DEPUTY, 1),
            MemberInput(deputy_2.id, DutyRole.DEPUTY, 2),
        ],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)
    await service.set_attendance(
        session, event.id, "monitoring_forecast", deputy_1.id, "absent", "superadmin"
    )
    await service.set_attendance(
        session, event.id, "monitoring_forecast", deputy_2.id, "present", "superadmin"
    )

    authority = await service.resolve_confirming_authority(
        session, event.id, "monitoring_forecast"
    )

    assert authority is not None
    assert authority.user_id == deputy_2.id
    assert authority.role == DutyRole.DEPUTY


async def test_confirmation_is_blocked_when_no_leader_or_deputy_is_present(
    session, event_factory, user_factory
):
    event = await event_factory()
    member = await user_factory(
        "member", "group_member", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(member.id, DutyRole.MEMBER, None)],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)

    assert (
        await service.resolve_confirming_authority(
            session, event.id, "monitoring_forecast"
        )
        is None
    )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_workgroup_roster.py -v
```

Expected: FAIL because `RosterService` and roster APIs do not exist.

- [ ] **Step 3: Implement roster service and API**

Implement the exact authority algorithm:

```python
async def resolve_confirming_authority(
    self,
    session: AsyncSession,
    event_id: UUID,
    group_code: str,
) -> ConfirmingAuthority | None:
    roster = await self.repository.get_roster_snapshot(
        session, event_id, group_code
    )
    if roster is None:
        return None
    attendance = {
        row.user_id: row.state
        for row in await self.repository.list_attendance(
            session, event_id, group_code
        )
    }
    if attendance.get(roster.leader_user_id) == "present":
        return ConfirmingAuthority(
            user_id=roster.leader_user_id,
            role=DutyRole.LEADER,
            deputy_order=None,
        )
    for deputy in sorted(roster.deputies, key=lambda item: item.order):
        if attendance.get(deputy.user_id) == "present":
            return ConfirmingAuthority(
                user_id=deputy.user_id,
                role=DutyRole.DEPUTY,
                deputy_order=deputy.order,
            )
    return None
```

Permission rules:

- `superadmin` can replace memberships and attendance.
- group leader can set attendance for the own group.
- deputy and member can set only own attendance.
- other authenticated roles can read permitted hall data but cannot mutate rosters.

Register the router in `backend/app/main.py`:

```python
from app.collaboration.router import router as collaboration_router

app.include_router(collaboration_router)
```

- [ ] **Step 4: Run roster and regression tests**

Run:

```powershell
cd backend
pytest tests/test_workgroup_roster.py tests/test_auth.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration backend/app/main.py backend/tests/test_workgroup_roster.py
git commit -m "feat: manage workgroup roster and deputy authority"
```

---

### Task 3: Versioned Shanghai Task Template Catalog

**Files:**
- Create: `config/collaboration/shanghai-2026-tasks.yaml`
- Create: `backend/app/collaboration/templates.py`
- Create: `backend/tests/test_collaboration_templates.py`
- Modify: `backend/app/config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `WorkgroupCode`, `EventKind`, event/revision applicability data, artifact catalog keys.
- Produces:
  - `load_task_template_catalog(path: str | Path) -> TaskTemplateCatalog`
  - `TaskTemplateCatalog.get_applicable(event, revision) -> tuple[TaskTemplateDefinition, ...]`
  - `TaskTemplateCatalog.get(template_code: str) -> TaskTemplateDefinition`
  - `TaskTemplateDefinition` with `template_code`, `workgroup_code`, `phase_code`, `start_offset_seconds`, `due_offset_seconds`, `continues_until_response_end`, `required_deliverables`, `artifact_bindings`, and `applicability`.
  - `ArtifactBinding(artifact_key: str, output_profile: str)`

- [ ] **Step 1: Write the failing catalog test**

```python
from pathlib import Path

from app.artifacts.catalog import load_catalog
from app.collaboration.templates import (
    ArtifactBinding,
    load_task_template_catalog,
)


def test_catalog_covers_all_groups_and_preplan_phases():
    catalog = load_task_template_catalog(
        Path("/config/collaboration/shanghai-2026-tasks.yaml")
    )

    assert {item.workgroup_code for item in catalog.definitions} == {
        "news_information",
        "monitoring_forecast",
        "comprehensive_coordination",
        "damage_assessment",
        "emergency_technology",
        "logistics",
        "center_station",
    }
    assert catalog.version == "shanghai-2026.1"
    assert catalog.get("technology.rapid_brief").artifact_bindings == (
        ArtifactBinding("doc.rapid_brief", "a3v-professional"),
    )
    assert catalog.get("technology.rapid_special").due_offset_seconds == 3600
    assert catalog.get("technology.intensity_map").continues_until_response_end


def test_catalog_artifact_bindings_exist_in_artifact_catalog():
    task_catalog = load_task_template_catalog(
        Path("/config/collaboration/shanghai-2026-tasks.yaml")
    )
    artifact_catalog = load_catalog(Path("/config/artifacts/catalog.yaml"))
    for definition in task_catalog.definitions:
        for binding in definition.artifact_bindings:
            artifact_catalog.get(binding.artifact_key, binding.output_profile)
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_templates.py -v
```

Expected: FAIL because the YAML catalog and loader do not exist.

- [ ] **Step 3: Implement the catalog and seed all required task themes**

Add setting:

```python
collaboration_task_template_path: str = (
    "/config/collaboration/shanghai-2026-tasks.yaml"
)
```

Seed `config/collaboration/shanghai-2026-tasks.yaml` with exact stable codes:

```yaml
version: shanghai-2026.1
tasks:
  - code: news.takeover_duty
    group: news_information
    phase: within_30m
    title: 接管行政值班室应急值守
    source: 预案5.2（1）
    due_offset_seconds: 1800
    continuous: true
  - code: news.release_quake_info
    group: news_information
    phase: within_30m
    title: 发布震情速报信息
    source: 预案5.2（1）
    due_offset_seconds: 1800
  - code: news.receive_directives
    group: news_information
    phase: within_30m
    title: 接收并转呈上级指示批示
    source: 预案5.2（1）
    due_offset_seconds: 1800
    continuous: true
  - code: news.monitor_public_opinion
    group: news_information
    phase: within_30m
    title: 开展热点敏感舆情监测
    source: 预案5.2（1）
    due_offset_seconds: 1800
    continuous: true
  - code: news.issue_duty_information
    group: news_information
    phase: within_30m
    title: 编报值班信息并通知报送方式
    source: 预案5.2（1）
    due_offset_seconds: 1800
  - code: news.meeting_support
    group: news_information
    phase: within_30m
    title: 承办应急指挥部会议并编发纪要
    source: 预案5.2（1）
    due_offset_seconds: 1800
  - code: news.summarize_group_reports
    group: news_information
    phase: 30m_to_60m
    title: 汇总各工作组信息形成汇报材料
    source: 预案5.2（1）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: news.spokesperson_materials
    group: news_information
    phase: 30m_to_60m
    title: 准备新闻发言人和宣传口径材料
    source: 预案5.2（1）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: news.website_topic
    group: news_information
    phase: 30m_to_60m
    title: 建立事件专题并对外发布信息
    source: 预案5.2（1）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: news.decision_compilation
    group: news_information
    phase: 1h_to_2h
    title: 整理决策建议并视情上报
    source: 预案5.2（1）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
    continuous: true
  - code: news.public_opinion_report
    group: news_information
    phase: 1h_to_2h
    title: 报送舆情监测报告
    source: 预案5.2（1）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
  - code: news.dynamics_release
    group: news_information
    phase: 1h_to_2h
    title: 汇总并发布应急处置动态
    source: 预案5.2（1）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
    continuous: true
  - code: news.media_interview
    group: news_information
    phase: 1h_to_2h
    title: 组织专家采访和新闻通稿
    source: 预案5.2（1）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
  - code: news.science_communication
    group: news_information
    phase: 2h_to_4h
    title: 发布地震应急科普作品
    source: 预案5.2（1）
    start_offset_seconds: 7200
    due_offset_seconds: 14400
  - code: news.rumor_response
    group: news_information
    phase: 2h_to_4h
    title: 开展传言和谣言应对
    source: 预案5.2（1）
    start_offset_seconds: 7200
    due_offset_seconds: 14400
    continuous: true
  - code: news.press_conference
    group: news_information
    phase: 4h_to_end
    title: 组织地震情况通报会
    source: 预案5.2（1）
    start_offset_seconds: 14400
    continuous: true
  - code: monitoring.epicenter_maps
    group: monitoring_forecast
    phase: within_30m
    title: 提供震中、历史和仪器烈度图
    source: 预案5.2（2）
    due_offset_seconds: 1800
    artifacts:
      - [map.historical_earthquakes, a3v-professional]
      - [map.intensity, a3v-professional]
  - code: monitoring.sequence_tracking
    group: monitoring_forecast
    phase: 30m_to_60m
    title: 跟踪序列并滚动提供余震目录
    source: 预案5.2（2）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
    continuous: true
  - code: monitoring.trend_consultation
    group: monitoring_forecast
    phase: 30m_to_60m
    title: 开展紧急趋势会商
    source: 预案5.2（2）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: monitoring.intensity_products
    group: monitoring_forecast
    phase: 30m_to_60m
    title: 提供烈度速报等专业产出
    source: 预案5.2（2）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: monitoring.geophysical_check
    group: monitoring_forecast
    phase: 1h_to_2h
    title: 核实地球物理台网异常
    source: 预案5.2（2）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
    continuous: true
  - code: monitoring.station_and_mechanism
    group: monitoring_forecast
    phase: 1h_to_2h
    title: 提供台站分布和震源机制
    source: 预案5.2（2）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
    artifacts:
      - [map.seismic_stations, a3v-professional]
  - code: monitoring.special_reports
    group: monitoring_forecast
    phase: 1h_to_2h
    title: 形成地震专报和震情专报
    source: 预案5.2（2）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
  - code: monitoring.aftershock_catalog
    group: monitoring_forecast
    phase: 1h_to_2h
    title: 编制余震序列并每两小时报送
    source: 预案5.2（2）
    start_offset_seconds: 3600
    continuous: true
  - code: coordination.response_suggestion
    group: comprehensive_coordination
    phase: within_30m
    title: 提出应急服务响应启动建议
    source: 预案5.2（3）
    due_offset_seconds: 1800
  - code: coordination.collect_situation
    group: comprehensive_coordination
    phase: within_30m
    title: 收集震情灾情社情并编报
    source: 预案5.2（3）
    due_offset_seconds: 1800
    continuous: true
  - code: coordination.hotline
    group: comprehensive_coordination
    phase: within_30m
    title: 处理12345热线工单
    source: 预案5.2（3）
    due_offset_seconds: 1800
    continuous: true
  - code: coordination.dispatch_field_team
    group: comprehensive_coordination
    phase: within_30m
    title: 协调先期处置并确定第一批出队人员
    source: 预案5.2（3）
    due_offset_seconds: 1800
  - code: coordination.support_field_team
    group: comprehensive_coordination
    phase: 30m_to_60m
    title: 前后方支撑现场队赶赴震区
    source: 预案5.2（3）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
    continuous: true
  - code: coordination.followup_team
    group: comprehensive_coordination
    phase: 1h_to_2h
    title: 视情派出后续现场队并通报震情
    source: 预案5.2（3）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
  - code: coordination.advanced_acts
    group: comprehensive_coordination
    phase: 1h_to_2h
    title: 收集应急工作先进事迹
    source: 预案5.2（3）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
    continuous: true
  - code: coordination.field_management
    group: comprehensive_coordination
    phase: 2h_to_end
    title: 做好现场队组织和保障
    source: 预案5.2（3）
    start_offset_seconds: 7200
    continuous: true
  - code: coordination.end_suggestion
    group: comprehensive_coordination
    phase: 2h_to_end
    title: 提出应急服务响应结束建议
    source: 预案5.2（3）
    start_offset_seconds: 7200
    continuous: true
  - code: damage.field_survey_ready
    group: damage_assessment
    phase: within_30m
    title: 确定现场调查人员并完成出队准备
    source: 预案5.2（4）
    due_offset_seconds: 1800
  - code: damage.background_materials
    group: damage_assessment
    phase: 30m_to_60m
    title: 提供构造、断裂和抗震设防资料
    source: 预案5.2（4）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: damage.structural_array
    group: damage_assessment
    phase: 30m_to_60m
    title: 提供结构台阵观测数据情况
    source: 预案5.2（4）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: damage.field_investigation
    group: damage_assessment
    phase: 30m_to_60m
    title: 开展现场破坏、地质灾害和烈度调查
    source: 预案5.2（4）
    start_offset_seconds: 1800
    continuous: true
  - code: damage.loss_assessment
    group: damage_assessment
    phase: 1h_to_end
    title: 开展地震灾害损失评估
    source: 预案5.2（4）
    start_offset_seconds: 3600
    continuous: true
  - code: damage.intensity_map
    group: damage_assessment
    phase: 1h_to_end
    title: 开展地震烈度图绘制
    source: 预案6.2（4）
    start_offset_seconds: 3600
    continuous: true
  - code: technology.open_command_hall
    group: emergency_technology
    phase: within_30m
    title: 开启指挥大厅和视频会议系统
    source: 预案5.2（5）
    due_offset_seconds: 1800
  - code: technology.rapid_brief
    group: emergency_technology
    phase: within_30m
    title: 产出地震灾害快速评估简报
    source: 预案5.2（5）
    due_offset_seconds: 1800
    artifacts:
      - [doc.rapid_brief, a3v-professional]
  - code: technology.network_security
    group: emergency_technology
    phase: within_30m
    title: 保障网络、网站和系统运行
    source: 预案5.2（5）
    due_offset_seconds: 1800
    continuous: true
  - code: technology.rapid_special
    group: emergency_technology
    phase: 30m_to_60m
    title: 产出地震灾害快速评估专报
    source: 预案5.2（5）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
    artifacts:
      - [doc.rapid_report, a3v-professional]
  - code: technology.professional_outputs
    group: emergency_technology
    phase: 1h_to_end
    title: 持续产出专业图件和文字说明
    source: 预案5.2（5）
    start_offset_seconds: 3600
    continuous: true
  - code: technology.system_operations
    group: emergency_technology
    phase: 1h_to_end
    title: 保障应急技术系统正常运行
    source: 预案5.2（5）
    start_offset_seconds: 3600
    continuous: true
  - code: technology.field_data_interpretation
    group: emergency_technology
    phase: 1h_to_end
    title: 分析现场信息和动态灾情
    source: 预案5.2（5）
    start_offset_seconds: 3600
    continuous: true
  - code: logistics.start_support
    group: logistics
    phase: within_30m
    title: 启动指挥部保障工作
    source: 预案5.2（6）
    due_offset_seconds: 1800
  - code: logistics.hall_seats
    group: logistics
    phase: within_30m
    title: 在指挥大厅设立工作组席位
    source: 预案5.2（6）
    due_offset_seconds: 1800
  - code: logistics.security
    group: logistics
    phase: within_30m
    title: 做好局本部安保工作
    source: 预案5.2（6）
    due_offset_seconds: 1800
    continuous: true
  - code: logistics.vehicle_materials
    group: logistics
    phase: 30m_to_60m
    title: 提供车辆和物资保障
    source: 预案5.2（6）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: logistics.vehicle_procurement
    group: logistics
    phase: 1h_to_2h
    title: 保障应急用车并送人员赶赴现场
    source: 预案5.2（6）
    start_offset_seconds: 3600
    due_offset_seconds: 7200
  - code: logistics.lodging_food
    group: logistics
    phase: 2h_to_end
    title: 做好现场人员食宿和物资保障
    source: 预案5.2（6）
    start_offset_seconds: 7200
    continuous: true
  - code: logistics.finance
    group: logistics
    phase: 2h_to_end
    title: 管理应急经费支出
    source: 预案5.2（6）
    start_offset_seconds: 7200
    continuous: true
  - code: station.video_terminal
    group: center_station
    phase: within_30m
    title: 开启视频会议终端
    source: 预案5.2（7）
    due_offset_seconds: 1800
  - code: station.local_survey
    group: center_station
    phase: within_30m
    title: 派就近人员开展现场调查
    source: 预案5.2（7）
    due_offset_seconds: 1800
  - code: station.district_contact
    group: center_station
    phase: within_30m
    title: 联系松江、崇明区应急局了解影响
    source: 预案5.2（7）
    due_offset_seconds: 1800
  - code: station.observation_check
    group: center_station
    phase: within_30m
    title: 检查辖区内观测仪器和设施
    source: 预案5.2（7）
    due_offset_seconds: 1800
    continuous: true
  - code: station.security
    group: center_station
    phase: within_30m
    title: 做好中心站安保工作
    source: 预案5.2（7）
    due_offset_seconds: 1800
    continuous: true
  - code: station.flow_observation_ready
    group: center_station
    phase: 30m_to_60m
    title: 做好流动观测出队准备
    source: 预案5.2（7）
    start_offset_seconds: 1800
    due_offset_seconds: 3600
  - code: station.restore_and_observe
    group: center_station
    phase: 1h_to_end
    title: 恢复监测设施并开展流动观测
    source: 预案5.2（7）
    start_offset_seconds: 3600
    continuous: true
  - code: news.external_event_record_notify
    group: news_information
    phase: within_30m
    title: 登记外省或范围外事件并完成通知
    source: 平台规则
    due_offset_seconds: 300
    applicability:
      spatial_class: outside_assessment_scope
```

The loader must reject duplicate codes, unknown groups, unknown phases, unknown artifact keys, negative offsets, `due_offset_seconds < start_offset_seconds`, and artifact bindings that do not exist in `config/artifacts/catalog.yaml`.

- [ ] **Step 4: Run catalog tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_templates.py tests/test_artifact_catalog.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add config/collaboration backend/app/collaboration/templates.py backend/app/config.py backend/tests/test_collaboration_templates.py .env.example
git commit -m "feat: add versioned workgroup task templates"
```

---

### Task 4: Idempotent Task Generation from Event Outbox

**Files:**
- Create: `backend/app/collaboration/generation.py`
- Create: `backend/tests/test_collaboration_generation.py`
- Modify: `backend/app/events/repository.py`
- Modify: `backend/app/events/service.py`
- Modify: `backend/tests/test_event_lifecycle.py`
- Modify: `backend/tests/test_event_service_responses.py`

**Interfaces:**
- Consumes: `EventLifecycleOutbox(status="pending", trigger_type="collaboration.requested")`, `TaskTemplateCatalog`, roster snapshots.
- Produces:
  - `EventRepository.enqueue_collaboration(...) -> bool`
  - `CollaborationTaskGenerator.generate_for_revision(session, event_id, revision_id, outbox_id) -> GenerationResult`
  - `CollaborationOutboxDispatcher.dispatch_once() -> int`

- [ ] **Step 1: Write failing generation tests**

```python
async def test_formal_revision_generates_seven_group_tasks_once(
    session, formal_event_factory, session_factory
):
    event, revision = await formal_event_factory()
    dispatcher = CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path="config/collaboration/shanghai-2026-tasks.yaml",
    )

    assert await dispatcher.dispatch_once() == 1
    assert await dispatcher.dispatch_once() == 0

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert {task.workgroup_code for task in tasks} == {
        group.value for group in WorkgroupCode
    }
    assert len(tasks) == len({task.task_code for task in tasks})


async def test_out_of_scope_event_creates_only_record_notify_task(
    session, out_of_scope_event_factory, session_factory
):
    event, _revision = await out_of_scope_event_factory()

    await CollaborationOutboxDispatcher(
        session_factory=session_factory,
        catalog_path="config/collaboration/shanghai-2026-tasks.yaml",
    ).dispatch_once()

    tasks = (
        await session.scalars(
            select(WorkgroupTask).where(WorkgroupTask.event_id == event.id)
        )
    ).all()
    assert [task.task_code for task in tasks] == [
        "news.external_event_record_notify"
    ]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_generation.py -v
```

Expected: FAIL because the collaboration outbox and generator do not exist.

- [ ] **Step 3: Implement outbox enqueue, claim, and generation**

Add `enqueue_collaboration` with an idempotent uniqueness check:

```python
async def enqueue_collaboration(
    self,
    session: AsyncSession,
    *,
    event_id: object,
    revision_id: object,
    revision_no: int,
    trigger_reason: str,
    created_at: datetime,
) -> bool:
    return await self._enqueue_lifecycle_trigger(
        session,
        event_id=event_id,
        revision_id=revision_id,
        revision_no=revision_no,
        trigger_reason=trigger_reason,
        trigger_type="collaboration.requested",
        created_at=created_at,
    )
```

In `EventService.ingest_collected`, for every current new formal, correction, manual, test, or drill revision, call `enqueue_collaboration` regardless of whether `_assessment_applicable` returns true. Assessment and collaboration outboxes are independent.

When enqueueing, load the current `CollaborationSettings` row and copy these
values into the immutable outbox payload:

```python
{
    "intensity_threshold": str(policy.intensity_threshold),
    "policy_version": policy.row_version,
    "region_boundary_version": revision.region_boundary_version,
}
```

The generator must read the snapshot from the outbox payload. Updating the
setting later must not change applicability for an event that has already been
enqueued.

Implement claim logic modeled on `AssessmentDispatcher`, with a stable task key:

```python
task_key = (
    f"{event_id}:{template_version_id}:{definition.template_code}"
)
```

For correction reconciliation:

- create missing tasks;
- keep completed and in-progress tasks;
- set only still-pending tasks that are no longer applicable to `not_required`;
- write a `CollaborationTaskEvent` for each change.

For institutional/service response upgrade:

- add only tasks newly made applicable by the upgraded response level;
- keep all existing task rows and deliverables.

For institutional/service response downgrade:

- transition only still-pending tasks that are no longer applicable to
  `not_required`;
- keep `in_progress`, `pending_review`, and `completed` rows unchanged.

- [ ] **Step 4: Run generation and event regression tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_generation.py tests/test_event_lifecycle.py tests/test_event_service_responses.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration/generation.py backend/app/events backend/tests/test_collaboration_generation.py backend/tests/test_event_lifecycle.py backend/tests/test_event_service_responses.py
git commit -m "feat: generate workgroup tasks from event lifecycle"
```

---

### Task 5: Task Lifecycle Service and API

**Files:**
- Create: `backend/app/collaboration/repository.py`
- Create: `backend/app/collaboration/service.py`
- Create: `backend/tests/test_collaboration_tasks_api.py`
- Modify: `backend/app/collaboration/router.py`
- Modify: `backend/app/collaboration/schemas.py`

**Interfaces:**
- Consumes: `WorkgroupTask`, `CollaborationTaskEvent`, roster authority.
- Produces:
  - `CollaborationTaskService.start(session, task_id, actor, expected_version) -> WorkgroupTask`
  - `CollaborationTaskService.submit(...) -> WorkgroupTask`
  - `CollaborationTaskService.return_to_work(...) -> WorkgroupTask`
  - `CollaborationTaskService.complete(...) -> WorkgroupTask`
  - `CollaborationTaskService.cancel(...) -> WorkgroupTask`
  - `CollaborationTaskService.update(...) -> WorkgroupTask`
  - HTTP routes under `/api/v1/collaboration/tasks`.

- [ ] **Step 1: Write failing lifecycle tests**

```python
async def test_task_state_machine_and_group_contribution(
    session, task_factory, group_member_user
):
    task = await task_factory(status="pending")
    service = CollaborationTaskService()

    started = await service.start(
        session,
        task.id,
        actor=group_member_user,
        expected_version=task.row_version,
    )
    assert started.status == TaskStatus.IN_PROGRESS

    submitted = await service.submit(
        session,
        task.id,
        actor=group_member_user,
        expected_version=started.row_version,
        result_text="已完成现场联系",
    )
    assert submitted.status == TaskStatus.PENDING_REVIEW

    contributors = await session.scalars(
        select(WorkgroupTaskContributor).where(
            WorkgroupTaskContributor.task_id == task.id
        )
    )
    assert [item.user_id for item in contributors] == [group_member_user.id]


async def test_wrong_group_user_cannot_start_task(
    session, task_factory, other_group_user
):
    task = await task_factory(workgroup_code="monitoring_forecast")
    with pytest.raises(PermissionError):
        await CollaborationTaskService().start(
            session,
            task.id,
            actor=other_group_user,
            expected_version=task.row_version,
        )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_tasks_api.py -v
```

Expected: FAIL because lifecycle service and APIs do not exist.

- [ ] **Step 3: Implement lifecycle transitions and event ledger**

Use explicit allowed transitions:

```python
ALLOWED_TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.IN_PROGRESS, TaskStatus.NOT_REQUIRED},
    TaskStatus.IN_PROGRESS: {TaskStatus.PENDING_REVIEW, TaskStatus.NOT_REQUIRED},
    TaskStatus.PENDING_REVIEW: {
        TaskStatus.COMPLETED,
        TaskStatus.IN_PROGRESS,
    },
}
```

Every mutation must:

- lock the task with `with_for_update=True`;
- compare `expected_version` and raise `StaleTaskVersion` on mismatch;
- validate actor permission;
- transition status;
- increment `row_version`;
- add a `CollaborationTaskEvent`;
- enqueue a projection outbox row;
- return the updated task.

`complete` must call `RosterService.resolve_confirming_authority` and accept only the returned user or `superadmin`. It must also verify required deliverables have a current publication or text result.

Map `StaleTaskVersion` to HTTP `409` and `PermissionError` to `403`.

- [ ] **Step 4: Run lifecycle and permission tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_tasks_api.py tests/test_workgroup_roster.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration backend/tests/test_collaboration_tasks_api.py
git commit -m "feat: implement workgroup task lifecycle"
```

---

### Task 6: Deliverable Versions and Publication

**Files:**
- Create: `backend/tests/test_collaboration_deliverables_api.py`
- Modify: `backend/app/collaboration/repository.py`
- Modify: `backend/app/collaboration/service.py`
- Modify: `backend/app/collaboration/router.py`
- Modify: `backend/app/collaboration/schemas.py`

**Interfaces:**
- Consumes: `TaskDeliverable`, `TaskDeliverableVersion`, `TaskDeliverablePublication`, `ArtifactStore`.
- Produces:
  - `DeliverableService.add_text_version(...) -> TaskDeliverableVersion`
  - `DeliverableService.add_manual_version(...) -> TaskDeliverableVersion`
  - `DeliverableService.publish(...) -> TaskDeliverablePublication`
  - `DeliverableService.restore(...) -> TaskDeliverablePublication`
  - `DeliverableService.override(...) -> TaskDeliverablePublication`
  - `DeliverableService.delete_candidate(...) -> None`

- [ ] **Step 1: Write failing version tests**

```python
async def test_automatic_and_manual_versions_coexist_with_one_current_publication(
    session, task_with_automatic_deliverable, group_leader_user
):
    deliverable = task_with_automatic_deliverable
    service = DeliverableService()
    manual = await service.add_text_version(
        session,
        deliverable.id,
        actor=group_leader_user,
        text_result={"result": "人工复核后修订"},
        basis_text="修正自动模型结果",
    )

    publication = await service.publish(
        session,
        deliverable.id,
        version_id=manual.id,
        actor=group_leader_user,
    )

    versions = (
        await session.scalars(
            select(TaskDeliverableVersion)
            .where(TaskDeliverableVersion.deliverable_id == deliverable.id)
            .order_by(TaskDeliverableVersion.version_no)
        )
    ).all()
    assert [version.source_kind for version in versions] == [
        "automatic",
        "manual",
    ]
    assert publication.version_id == manual.id
    assert await service.current_version_id(session, deliverable.id) == manual.id


async def test_member_cannot_publish_deliverable(
    session, deliverable, group_member_user
):
    with pytest.raises(PermissionError):
        await DeliverableService().publish(
            session,
            deliverable.id,
            version_id=deliverable.candidate_version_id,
            actor=group_member_user,
        )
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_deliverables_api.py -v
```

Expected: FAIL because deliverable versioning does not exist.

- [ ] **Step 3: Implement candidate versions and publication**

Use `ArtifactStore.store_upload` for manual files. Insert a version with:

```python
TaskDeliverableVersion(
    deliverable_id=deliverable.id,
    version_no=next_version,
    source_kind=DeliverableSourceKind.MANUAL,
    storage_key=stored.relative_path,
    file_name=stored.file_name,
    checksum=stored.checksum,
    mime_type=mime_type,
    size_bytes=stored.size_bytes,
    text_result=text_result,
    created_by=actor.username,
    basis_text=basis_text,
    supersedes_version_id=previous_version_id,
)
```

Publishing must:

- lock the deliverable;
- ensure the version belongs to it;
- reject a version with missing storage object or invalid checksum;
- set `superseded_at` on the previous current publication;
- create one new current `TaskDeliverablePublication`;
- accept only leader, effective deputy, or superadmin;
- add task event and projection outbox rows.

Delete candidate is allowed only when:

- `source_kind == manual`;
- there is no publication row for the version;
- actor is uploader, group leader, effective deputy, or superadmin.

- [ ] **Step 4: Run deliverable and task tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_deliverables_api.py tests/test_collaboration_tasks_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration backend/tests/test_collaboration_deliverables_api.py
git commit -m "feat: add versioned workgroup deliverables"
```

---

### Task 7: Automatic Artifact Linking and Projection Outbox

**Files:**
- Create: `backend/app/collaboration/artifact_link.py`
- Create: `backend/tests/test_collaboration_artifact_link.py`
- Modify: `backend/app/artifacts/worker.py`

**Interfaces:**
- Consumes: `GeneratedArtifact`, `ArtifactPublication`, `TaskDeliverable.artifact_binding`, `CollaborationOutbox`.
- Produces:
  - `ArtifactLinkService.sync_run_publications(session, production_run_id) -> ArtifactLinkResult`
  - automatic `TaskDeliverableVersion` rows with `source_kind="automatic"`.

- [ ] **Step 1: Write failing artifact-link test**

```python
async def test_published_artifact_links_to_matching_deliverable_without_copying_file(
    session, event, published_artifact, matching_deliverable
):
    result = await ArtifactLinkService().sync_run_publications(
        session, published_artifact.production_run_id
    )

    assert result.linked_count == 1
    version = await session.scalar(
        select(TaskDeliverableVersion).where(
            TaskDeliverableVersion.deliverable_id
            == matching_deliverable.id
        )
    )
    assert version is not None
    assert version.source_kind == "automatic"
    assert version.artifact_id == published_artifact.id
    assert version.storage_key is None
    assert version.checksum == published_artifact.checksum
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_artifact_link.py -v
```

Expected: FAIL because automatic linking does not exist.

- [ ] **Step 3: Implement idempotent linking**

Match on:

```python
(
    GeneratedArtifact.event_id,
    GeneratedArtifact.artifact_key,
    GeneratedArtifact.output_profile,
    ArtifactPublication.id,
)
```

Use a uniqueness rule on `(deliverable_id, artifact_publication_id)` to prevent duplicates.

At the end of `ArtifactActivities.publish_artifact_production`, before leaving the transaction, call:

```python
from app.collaboration.artifact_link import ArtifactLinkService

await ArtifactLinkService().sync_run_publications(session, run.id)
```

Do not import collaboration models at module import time in `artifacts.worker`; use the local import shown above to avoid circular imports.

The sync must add one projection outbox row for the event when any link changes.

- [ ] **Step 4: Run artifact link and artifact publication regressions**

Run:

```powershell
cd backend
pytest tests/test_collaboration_artifact_link.py tests/test_artifact_publication_closure.py tests/test_artifact_workflow.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration/artifact_link.py backend/app/artifacts/worker.py backend/tests/test_collaboration_artifact_link.py
git commit -m "feat: link published artifacts to workgroup tasks"
```

---

### Task 8: Deadline Scheduler and Notification Adapters

**Files:**
- Create: `backend/app/collaboration/notifications.py`
- Create: `backend/app/collaboration/scheduler.py`
- Create: `backend/app/collaboration/retention.py`
- Create: `backend/tests/test_collaboration_notifications.py`
- Create: `backend/tests/test_collaboration_retention.py`

**Interfaces:**
- Consumes: `WorkgroupTask`, roster authority, `NotificationDelivery`.
- Produces:
  - `DeadlineScheduler.run_once(session, observed_at) -> SchedulerResult`
  - `NotificationService.dispatch_pending(session, limit) -> DispatchResult`
  - adapters implementing `NotificationAdapter.send(delivery) -> AdapterResult`.
  - `CollaborationRetentionService.purge_due(session, observed_at) -> RetentionResult`

- [ ] **Step 1: Write failing notification tests**

```python
async def test_overdue_task_emits_initial_and_thirty_minute_reminders(
    session, overdue_task_for_leader
):
    scheduler = DeadlineScheduler()
    first = await scheduler.run_once(
        session, observed_at=overdue_task_for_leader.due_at + timedelta(seconds=1)
    )
    second = await scheduler.run_once(
        session, observed_at=overdue_task_for_leader.due_at + timedelta(minutes=30)
    )

    assert first.overdue_marked == 1
    assert second.overdue_reminder_count == 1
    rows = (
        await session.scalars(
            select(NotificationDelivery).where(
                NotificationDelivery.task_id == overdue_task_for_leader.id
            )
        )
    ).all()
    assert {row.intent_type for row in rows} >= {"overdue"}


async def test_external_notification_failure_falls_back_to_in_app(
    session, notification_delivery
):
    result = await NotificationService(
        adapters={
            "wecom": FailingAdapter(),
            "in_app": RecordingInAppAdapter(),
        }
    ).dispatch_pending(session, limit=10)

    assert result.fallback_sent == 1
    assert notification_delivery.status == "fallback_sent"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_notifications.py -v
```

Expected: FAIL because scheduler and adapters do not exist.

- [ ] **Step 3: Implement deadline and notification rules**

Set timeliness before creating notifications:

```python
if observed_at >= task.due_at:
    task.timeliness_state = TimelinessState.OVERDUE
elif task.due_at - observed_at <= timedelta(minutes=15):
    task.timeliness_state = TimelinessState.AT_RISK
else:
    task.timeliness_state = TimelinessState.ON_TIME
```

Notification schedule:

- initial assignment at task creation;
- 15 minutes before due;
- at due;
- every 30 minutes while overdue and nonterminal.

Create `in_app` delivery first. Create external intents when channel config exists. On external failure, retry up to three attempts, then set the external row to `fallback_sent` and ensure the in-app delivery exists.

During an active live event, skip `wecom`, `email`, and `phone` intents for test and drill events, but keep in-app rows.

Retention rules:

- formal events and their collaboration rows are retained indefinitely;
- test events and their collaboration rows are purged 400 days after `origin_time`;
- drill events and their collaboration rows are purged two years after `origin_time`;
- purge in bounded batches and never remove a row that still has an active live dependency;
- record only aggregate purge counts in worker logs, never event content.

- [ ] **Step 4: Run scheduler and notification tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration/notifications.py backend/app/collaboration/scheduler.py backend/app/collaboration/retention.py backend/tests/test_collaboration_notifications.py backend/tests/test_collaboration_retention.py
git commit -m "feat: schedule workgroup deadlines and notifications"
```

---

### Task 9: Temporary Tasks and Collaboration Worker

**Files:**
- Create: `backend/app/collaboration/worker.py`
- Create: `backend/tests/test_collaboration_worker.py`
- Modify: `backend/app/collaboration/service.py`
- Modify: `backend/app/collaboration/router.py`
- Modify: `backend/app/collaboration/schemas.py`
- Modify: `backend/app/config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: outbox dispatcher, scheduler, notification service.
- Produces:
  - `TemporaryTaskService.create(...) -> WorkgroupTask`
  - `TemporaryTaskService.update(...) -> WorkgroupTask`
  - `run_collaboration_worker(stop_event) -> None`
  - CLI module command `python -m app.collaboration.worker`.

- [ ] **Step 1: Write failing temporary-task and worker tests**

```python
async def test_coordination_user_can_create_temporary_task_for_one_group(
    session, coordination_user
):
    task = await TemporaryTaskService().create(
        session,
        event_id=EVENT_ID,
        workgroup_code="center_station",
        title="补充检查观测点",
        instruction="检查受影响观测点并上传结果",
        priority=80,
        due_at=datetime(2026, 10, 3, 3, 0, tzinfo=UTC),
        actor=coordination_user,
    )

    assert task.source_type == "ad_hoc"
    assert task.workgroup_code == "center_station"


async def test_worker_processes_outbox_and_scheduler_once(
    session_factory, fake_clock
):
    result = await run_worker_cycle(
        session_factory=session_factory,
        observed_at=fake_clock.now,
    )
    assert result.generated_tasks >= 1
    assert result.scheduler_ran is True
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_worker.py -v
```

Expected: FAIL because temporary tasks and worker cycle do not exist.

- [ ] **Step 3: Implement temporary tasks and polling worker**

Add settings:

```python
collaboration_worker_enabled: bool = False
collaboration_worker_poll_seconds: float = 1.0
collaboration_worker_batch_size: int = 50
collaboration_outbox_max_attempts: int = 10
collaboration_outbox_lease_seconds: int = 60
```

Worker cycle order:

1. dispatch pending `collaboration.requested` outbox rows;
2. dispatch pending projection outbox rows;
3. update timeliness states;
4. create due notifications;
5. dispatch pending notifications.

The worker must log structured counts and catch loop-level exceptions without terminating the process.

- [ ] **Step 4: Run temporary task, worker, and full collaboration tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_worker.py tests/test_collaboration_tasks_api.py tests/test_collaboration_notifications.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration backend/app/config.py backend/tests/test_collaboration_worker.py .env.example
git commit -m "feat: run temporary task and notification workers"
```

---

### Task 10: Command Hall Projection and Read APIs

**Files:**
- Create: `backend/app/command_hall/__init__.py`
- Create: `backend/app/command_hall/models.py`
- Create: `backend/app/command_hall/projector.py`
- Create: `backend/app/command_hall/service.py`
- Create: `backend/app/command_hall/schemas.py`
- Create: `backend/app/command_hall/router.py`
- Create: `backend/tests/test_command_hall_projection.py`
- Create: `backend/tests/test_command_hall_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/collaboration/worker.py`

**Interfaces:**
- Consumes: task, deliverable publication, notification, event, assessment, artifact publication state.
- Produces:
  - `CommandHallProjector.refresh_event(session, event_id) -> ProjectionResult`
  - `CommandHallService.active_event(session) -> UUID | None`
  - `CommandHallService.overview(session, event_id) -> EventOverview`
  - `CommandHallService.group_detail(session, event_id, group_code) -> GroupDetail`
  - `CommandHallService.task_detail(session, task_id) -> TaskDetail`
  - routes under `/api/v1/command-hall`.

- [ ] **Step 1: Write failing projection and API tests**

```python
async def test_projection_counts_overdue_and_waiting_for_review(
    session, collaboration_fixture
):
    await CommandHallProjector().refresh_event(
        session, collaboration_fixture.event.id
    )
    overview = await CommandHallService().overview(
        session, collaboration_fixture.event.id
    )

    assert overview.group_count == 7
    assert overview.task_counts.pending_review == 1
    assert overview.task_counts.overdue == 1
    assert overview.projection_version >= 1


def test_sse_rejects_unknown_event(client):
    response = client.get(
        "/api/v1/command-hall/events/00000000-0000-0000-0000-000000000000/stream"
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_command_hall_projection.py tests/test_command_hall_api.py -v
```

Expected: FAIL because command hall projection and routes do not exist.

- [ ] **Step 3: Implement projection, active-event ordering, and SSE**

Active event ordering:

```python
CASE
  WHEN event_type IN ('formal', 'manual')
       AND lifecycle_state != 'not_applicable' THEN 1
  WHEN event_type = 'drill'
       AND lifecycle_state != 'not_applicable' THEN 2
  WHEN event_type = 'test'
       AND lifecycle_state != 'not_applicable' THEN 3
  ELSE 4
END,
origin_time DESC
```

Projection refresh must upsert:

- one event row;
- seven group rows;
- delete and recreate derived alert rows for the event;
- increment `projection_version` once per successful refresh.

SSE implementation:

- query current `projection_version` every second;
- emit `event: projection.updated` when it changes;
- emit heartbeat comments every 15 seconds;
- stop on client disconnect;
- require authenticated read permission.

- [ ] **Step 4: Run command hall and collaboration tests**

Run:

```powershell
cd backend
pytest tests/test_command_hall_projection.py tests/test_command_hall_api.py tests/test_collaboration_tasks_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/command_hall backend/app/collaboration/worker.py backend/app/main.py backend/tests/test_command_hall_projection.py backend/tests/test_command_hall_api.py
git commit -m "feat: expose command hall projections"
```

---

### Task 11: Frontend Collaboration API and Types

**Files:**
- Create: `frontend/tests/collaboration-api.test.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/types.ts`

**Interfaces:**
- Consumes: collaboration and command hall HTTP endpoints.
- Produces:
  - `listCollaborationTasks(eventId)`
  - `getCollaborationTask(taskId)`
  - `startCollaborationTask(taskId, version)`
  - `submitCollaborationTask(taskId, version, input)`
  - `returnCollaborationTask(taskId, version, reason)`
  - `completeCollaborationTask(taskId, version)`
  - `createTemporaryTask(eventId, input)`
  - `getCommandHallActiveEvent()`
  - `getCommandHallOverview(eventId)`
  - `getCommandHallGroup(eventId, groupCode)`
  - `getCommandHallTask(taskId)`
  - `streamCommandHall(eventId, onEvent, signal)`

- [ ] **Step 1: Write failing API client tests**

```ts
test("completeCollaborationTask sends If-Match and idempotency key", async () => {
  setAccessToken("token");
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        id: "task-1",
        status: "completed",
        row_version: 5,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ),
  );

  await completeCollaborationTask("task-1", 4);

  const [, init] = vi.mocked(globalThis.fetch).mock.calls[0];
  const headers = new Headers(init?.headers);
  expect(headers.get("If-Match")).toBe("4");
  expect(headers.get("Idempotency-Key")).toBeTruthy();
});
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
cd frontend
npm test -- --run tests/collaboration-api.test.ts
```

Expected: FAIL because the functions do not exist.

- [ ] **Step 3: Add types and API functions**

Add TypeScript types exactly matching backend:

```ts
export type WorkgroupTaskStatus =
  | "pending"
  | "in_progress"
  | "pending_review"
  | "completed"
  | "not_required"
  | "failed";

export interface WorkgroupTask {
  id: string;
  event_id: string;
  workgroup_code: string;
  task_code: string;
  title: string;
  status: WorkgroupTaskStatus;
  timeliness_state: "on_time" | "at_risk" | "overdue";
  due_at: string | null;
  row_version: number;
}

export interface CommandHallOverview {
  event: Record<string, unknown>;
  groups: CommandHallGroup[];
  alerts: CommandHallAlert[];
  projection_version: number;
  updated_at: string;
}
```

Use a helper for mutation headers:

```ts
function mutationHeaders(version: number): Record<string, string> {
  return {
    ...authenticatedHeaders(),
    "Content-Type": "application/json",
    "If-Match": String(version),
    "Idempotency-Key": globalThis.crypto.randomUUID(),
  };
}
```

For task completion, return the server response and never optimistically alter `row_version`.

Implement `streamCommandHall` with `fetch` plus `ReadableStream` parsing, not the
browser-native `EventSource`, because the SSE endpoint requires the bearer token
and native `EventSource` cannot set an `Authorization` header. Pass an
`AbortController.signal`, require a `200` response with
`text/event-stream`, parse named events and data blocks, and throw `ApiError`
for non-2xx responses.

- [ ] **Step 4: Run API tests and typecheck**

Run:

```powershell
cd frontend
npm test -- --run tests/collaboration-api.test.ts
npm run typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/api/client.ts frontend/src/types.ts frontend/tests/collaboration-api.test.ts
git commit -m "feat: add collaboration frontend api"
```

---

### Task 12: Workgroup Task Console

**Files:**
- Create: `frontend/src/pages/WorkgroupTasksPage.tsx`
- Create: `frontend/src/components/CollaborationTaskPanel.tsx`
- Create: `frontend/tests/workgroup-tasks.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: Task 11 API functions and `CurrentUser.role/workgroup`.
- Produces: task list, task detail panel, state actions, manual result submission, candidate upload, publish confirmation, and temporary task form.

- [ ] **Step 1: Write failing page tests**

```tsx
test("group member sees start and submit but not confirm", async () => {
  listCollaborationTasksMock.mockResolvedValue([pendingTask]);
  getCollaborationTaskMock.mockResolvedValue(pendingTask);

  render(
    <MemoryRouter initialEntries={["/tasks/event-1"]}>
      <Routes>
        <Route
          path="/tasks/:eventId"
          element={
            <WorkgroupTasksPage
              userRole="group_member"
              workgroup="监测预报组"
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByText("开始处理")).toBeInTheDocument();
  expect(screen.queryByText("确认完成")).not.toBeInTheDocument();
});


test("leader can confirm a pending review task", async () => {
  getCollaborationTaskMock.mockResolvedValue({
    ...pendingTask,
    status: "pending_review",
  });

  renderTaskPage("group_leader", "监测预报组");

  expect(await screen.findByText("确认完成")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd frontend
npm test -- --run tests/workgroup-tasks.test.tsx
```

Expected: FAIL because the page and component do not exist.

- [ ] **Step 3: Build the task page and actions**

Add navigation:

```tsx
<NavLink
  to="/tasks"
  className={({ isActive }) =>
    isActive ? "nav-link nav-link--active" : "nav-link"
  }
>
  工作组任务
</NavLink>
```

Routes:

```tsx
<Route
  path="/tasks"
  element={
    <WorkgroupTasksPage
      userRole={userRole}
      workgroup={userWorkgroup}
    />
  }
/>
<Route
  path="/tasks/:eventId"
  element={
    <WorkgroupTasksPage
      userRole={userRole}
      workgroup={userWorkgroup}
    />
  }
/>
```

The page must:

- list tasks grouped by phase;
- show status, timeliness, due time, and contributors;
- open details without leaving the page;
- show only allowed actions;
- refresh server state after every mutation;
- show `409` as "任务已被其他人更新，请刷新后重试";
- show immutable source labels `系统自动版`, `人工修订版`, `超级管理员覆盖版`;
- never display test or drill results without the corresponding visible marker.

- [ ] **Step 4: Run page tests, all frontend tests, and typecheck**

Run:

```powershell
cd frontend
npm test -- --run tests/workgroup-tasks.test.tsx
npm test -- --run
npm run typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/App.tsx frontend/src/pages/WorkgroupTasksPage.tsx frontend/src/components/CollaborationTaskPanel.tsx frontend/src/styles.css frontend/tests/workgroup-tasks.test.tsx
git commit -m "feat: add workgroup task console"
```

---

### Task 13: Command Hall Overview and Drill-Down

**Files:**
- Create: `frontend/src/pages/CommandHallPage.tsx`
- Create: `frontend/src/components/CommandHallGroupBoard.tsx`
- Create: `frontend/src/components/CommandHallDetailDrawer.tsx`
- Create: `frontend/tests/command-hall.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: Task 11 command hall APIs and `streamCommandHall`.
- Produces: `7680x2430` overview, seven group cards, task/group drawer, SSE refresh, and 5-second polling fallback.

- [ ] **Step 1: Write failing command hall tests**

```tsx
test("renders seven group cards and opens a task drawer", async () => {
  activeEventMock.mockResolvedValue(activeEvent);
  overviewMock.mockResolvedValue(overviewWithSevenGroups);
  groupMock.mockResolvedValue(groupDetail);

  renderCommandHall();

  expect(await screen.findByText("新闻信息值守组")).toBeInTheDocument();
  expect(screen.getByText("监测预报组")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /监测预报组/ }));
  expect(await screen.findByText("趋势会商")).toBeInTheDocument();
});


test("test event marker cannot be hidden", async () => {
  activeEventMock.mockResolvedValue({ ...activeEvent, event_kind: "test" });
  overviewMock.mockResolvedValue({ ...overviewWithSevenGroups, event_kind: "test" });

  renderCommandHall();

  expect(await screen.findByText("测试")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
cd frontend
npm test -- --run tests/command-hall.test.tsx
```

Expected: FAIL because the command hall page does not exist.

- [ ] **Step 3: Implement total overview and drawer**

Add route:

```tsx
<Route
  path="/command-hall"
  element={<CommandHallPage />}
/>
<Route
  path="/command-hall/:eventId"
  element={<CommandHallPage />}
/>
```

The layout must use fixed CSS grid tracks:

```css
.command-hall {
  min-width: 1920px;
  min-height: 1080px;
  grid-template-columns: minmax(360px, 1fr) minmax(720px, 2fr) minmax(420px, 1.2fr);
  grid-template-rows: auto minmax(0, 1fr) auto;
}

.command-hall__groups {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}
```

Behavior:

- open `CommandHallDetailDrawer` on group click;
- use buttons for clickable group cards;
- preserve overview behind the drawer;
- display current publication and source-kind text;
- show alerts before lower-priority metrics;
- connect through `streamCommandHall` with the authenticated fetch transport;
- close SSE on unmount;
- poll overview every 5 seconds when SSE is unavailable;
- never provide mutation buttons on the hall page.

- [ ] **Step 4: Run hall tests, all frontend tests, and typecheck**

Run:

```powershell
cd frontend
npm test -- --run tests/command-hall.test.tsx
npm test -- --run
npm run typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/App.tsx frontend/src/pages/CommandHallPage.tsx frontend/src/components/CommandHallGroupBoard.tsx frontend/src/components/CommandHallDetailDrawer.tsx frontend/src/styles.css frontend/tests/command-hall.test.tsx
git commit -m "feat: add command hall overview"
```

---

### Task 14: Deployment Wiring, End-to-End, and Performance Verification

**Files:**
- Create: `frontend/e2e/command-hall.spec.ts`
- Create: `frontend/e2e/workgroup-tasks.spec.ts`
- Create: `backend/tests/test_collaboration_end_to_end.py`
- Create: `docs/runbooks/workgroup-collaboration-command-hall.md`
- Modify: `infra/compose.yaml`
- Modify: `README.md`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: executable deployment process and end-to-end acceptance evidence.

- [ ] **Step 1: Write failing end-to-end acceptance tests**

```python
async def test_formal_event_closes_all_seven_groups_with_dual_versions(
    collaboration_e2e,
):
    await collaboration_e2e.ingest_formal_event()
    tasks = await collaboration_e2e.wait_for_tasks()
    assert {task.workgroup_code for task in tasks} == {
        group.value for group in WorkgroupCode
    }

    await collaboration_e2e.process_and_confirm_every_group()
    await collaboration_e2e.publish_manual_revisions()
    overview = await collaboration_e2e.command_hall_overview()

    assert overview["task_counts"]["completed"] == len(tasks)
    assert overview["task_counts"]["overdue"] == 0
    assert overview["task_counts"]["failed"] == 0
    assert overview["dual_version_count"] >= 1
```

```ts
test("command hall fits 7680x2430 without overlap", async ({ page }) => {
  await page.setViewportSize({ width: 7680, height: 2430 });
  await page.goto("/command-hall");
  await expect(page.getByText("新闻信息值守组")).toBeVisible();
  await expect(page.getByText("中心站组")).toBeVisible();

  const overlap = await page.evaluate(() => {
    const cards = [...document.querySelectorAll(".command-hall__group-card")];
    return cards.some((card, index) =>
      cards.slice(index + 1).some((other) => {
        const a = card.getBoundingClientRect();
        const b = other.getBoundingClientRect();
        return !(
          a.right <= b.left ||
          b.right <= a.left ||
          a.bottom <= b.top ||
          b.bottom <= a.top
        );
      }),
    );
  });
  expect(overlap).toBe(false);
});
```

- [ ] **Step 2: Run acceptance tests and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_end_to_end.py -v
cd ..\frontend
npm run test:e2e -- e2e/workgroup-tasks.spec.ts e2e/command-hall.spec.ts
```

Expected: FAIL until worker wiring and E2E fixtures are complete.

- [ ] **Step 3: Wire the worker and document operation**

Add to `infra/compose.yaml`:

```yaml
  collaboration-worker:
    build:
      context: ../backend
    env_file:
      - ../.env
    environment:
      COLLABORATION_WORKER_ENABLED: "true"
    command: ["python", "-m", "app.collaboration.worker"]
    restart: unless-stopped
    volumes:
      - ../backend:/app
      - ../config:/config
      - artifact-data:/var/lib/artifacts
    depends_on:
      postgres:
        condition: service_healthy
```

Runbook must include:

- start and stop commands;
- worker health check;
- how to rebuild projections for one event;
- how to inspect dead-letter collaboration outbox rows;
- how to verify notification fallback;
- how to verify test and drill markers;
- how to verify automatic and manual current publication versions;
- how to run `7680x2430` visual acceptance;
- how to avoid changing existing artifact and assessment deadlines.

- [ ] **Step 4: Run the complete verification suite**

Run:

```powershell
cd backend
pytest -q
cd ..\frontend
npm test -- --run
npm run typecheck
npm run build
npm run test:e2e
```

Expected:

- backend suite passes;
- frontend unit suite passes;
- TypeScript typecheck passes;
- production build passes;
- E2E passes;
- collaboration acceptance reports `P95 <= 5 秒`;
- command hall overview reports `P95 <= 500 毫秒`;
- no regression in the existing `28 complete + 11 degraded`、`0 failed`、`0 timeout` artifact acceptance.
- a `5.0` to `5.9` test event inside Shanghai generates all seven-workgroup
  tasks and links all existing 27 professional maps and all reports.

- [ ] **Step 5: Commit**

```powershell
git add infra/compose.yaml frontend/e2e backend/tests/test_collaboration_end_to_end.py docs/runbooks/workgroup-collaboration-command-hall.md README.md
git commit -m "test: verify workgroup collaboration and command hall"
```

---

### Task 15: Superadmin Hard Purge Closure

**Files:**
- Create: `backend/app/collaboration/purge.py`
- Create: `backend/tests/test_collaboration_purge.py`
- Modify: `backend/app/collaboration/router.py`
- Modify: `backend/app/collaboration/schemas.py`

**Interfaces:**
- Consumes: all collaboration, event, artifact, raw-message, and audit models.
- Produces:
  - `SuperadminPurgeService.purge_event(session, event_id, actor) -> PurgeResult`
  - `DELETE /api/v1/admin/events/{event_id}/purge`

- [ ] **Step 1: Write the failing hard-purge test**

```python
async def test_superadmin_purge_removes_raw_message_artifacts_and_collaboration_logs(
    session, purge_fixture, superadmin_user
):
    result = await SuperadminPurgeService().purge_event(
        session,
        purge_fixture.event.id,
        actor=superadmin_user,
    )

    assert result.deleted_event_id == purge_fixture.event.id
    assert result.deleted_revision_count == 1
    assert result.deleted_raw_message_count == 1
    assert await session.get(EarthquakeEvent, purge_fixture.event.id) is None
    assert await session.get(RawMessage, purge_fixture.raw.id) is None
    assert (
        await session.scalar(
            select(func.count())
            .select_from(WorkgroupTask)
            .where(WorkgroupTask.event_id == purge_fixture.event.id)
        )
        == 0
    )
    assert purge_fixture.artifact_path in result.storage_paths


async def test_non_superadmin_cannot_purge_event(session, purge_fixture, group_leader_user):
    with pytest.raises(PermissionError):
        await SuperadminPurgeService().purge_event(
            session,
            purge_fixture.event.id,
            actor=group_leader_user,
        )
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```powershell
cd backend
pytest tests/test_collaboration_purge.py -v
```

Expected: FAIL because hard-purge behavior does not exist.

- [ ] **Step 3: Implement explicit, idempotent purge**

The service must:

- require `actor.role == "superadmin"`;
- lock the event and return an already-purged result if it no longer exists;
- collect raw-message IDs and object-storage paths before deleting rows;
- delete event-owned artifact override requests, publications, generated
  artifacts, production runs/tasks, assessments, collaboration rows, and audit
  rows through their foreign-key closure;
- delete revisions before raw messages because `EarthquakeRevision.raw_message_id`
  has no cascade;
- expose storage paths in the result so the HTTP layer deletes files only after
  the database transaction commits;
- never delete a data-asset or artifact object still referenced by another
  event.

The endpoint returns `204` on success and `404` only when the caller requests an
event that has never existed and no purge receipt matches the idempotency key.

- [ ] **Step 4: Run purge, collaboration, and artifact regression tests**

Run:

```powershell
cd backend
pytest tests/test_collaboration_purge.py tests/test_collaboration_tasks_api.py tests/test_artifact_override.py tests/test_event_service.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/collaboration/purge.py backend/app/collaboration/router.py backend/app/collaboration/schemas.py backend/tests/test_collaboration_purge.py
git commit -m "feat: add superadmin event purge"
```

---

## Final Review Checklist

- [ ] All seven workgroups are represented in templates and authority tests.
- [ ] Formal, correction, manual, test, and drill events use the same collaboration model.
- [ ] Automatic reporting does not generate group tasks.
- [ ] Out-of-scope events create only the record and notification task.
- [ ] Duplicate and unordered events do not duplicate tasks.
- [ ] Leader and ordered deputy authority tests pass.
- [ ] System automatic and manual versions coexist.
- [ ] One current publication exists per deliverable.
- [ ] Temporary tasks use the shared lifecycle.
- [ ] Test, drill, and formal events follow the configured retention periods.
- [ ] Superadmin hard purge removes raw messages, collaboration logs, and
  unreferenced artifact objects without breaking other events.
- [ ] Station notifications and external-channel fallback are tested.
- [ ] Command hall reads projections only.
- [ ] SSE and polling fallback are both tested.
- [ ] `7680x2430` overlap and overflow checks pass.
- [ ] Test and drill markers cannot be hidden.
- [ ] Existing evaluation, artifact, report, and permission suites pass.
- [ ] No secrets are committed.
