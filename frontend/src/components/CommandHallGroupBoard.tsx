import type { CommandHallGroup, CommandHallTaskCounts } from "../types";

interface CommandHallGroupBoardProps {
  eventId: string;
  groups: CommandHallGroup[];
  onOpenGroup: (group: CommandHallGroup) => void;
}

const WORKGROUP_DEFINITIONS = [
  { code: "news_information", name: "新闻信息值守组" },
  { code: "monitoring_forecast", name: "监测预报组" },
  { code: "comprehensive_coordination", name: "综合协调组" },
  { code: "damage_assessment", name: "震害评估组" },
  { code: "emergency_technology", name: "应急技术组" },
  { code: "logistics", name: "后勤保障组" },
  { code: "center_station", name: "中心站组" },
] as const;

const EMPTY_COUNTS: CommandHallTaskCounts = {
  total: 0,
  pending: 0,
  in_progress: 0,
  pending_review: 0,
  completed: 0,
  not_required: 0,
  failed: 0,
  overdue: 0,
  at_risk: 0,
  dual_version_count: 0,
};

const SOURCE_LABELS: Record<string, string> = {
  automatic: "自动版",
  manual: "人工修订版",
  superadmin_override: "超级管理员覆盖版",
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function asRecords(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => item !== null);
}

function textValue(value: unknown, fallback = "-"): string {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  if (typeof value === "number") {
    return String(value);
  }
  return fallback;
}

function personName(person: unknown, fallback: string): string {
  const record = asRecord(person);
  if (!record) {
    return fallback;
  }
  return textValue(
    record.username ?? record.display_name ?? record.user_id,
    fallback,
  );
}

function attendanceSummary(group: CommandHallGroup): string {
  const attendance = asRecords(group.attendance);
  if (attendance.length === 0) {
    return "到岗待确认";
  }
  const present = attendance.filter((item) => item.state === "present").length;
  return `到岗 ${present}/${attendance.length}`;
}

function authorityLabel(group: CommandHallGroup): string {
  const authority = asRecord(group.confirming_authority);
  if (!authority) {
    return "代理待确认";
  }
  const role = authority.role === "deputy" ? "代理负责人" : "确认负责人";
  return `${role}：${personName(authority, "待确认")}`;
}

function emptyGroup(
  eventId: string,
  code: string,
  name: string,
  displayOrder: number,
): CommandHallGroup {
  return {
    event_id: eventId,
    workgroup_code: code,
    name,
    display_order: displayOrder,
    roster_version: null,
    roster_fingerprint: null,
    leader: null,
    deputies: [],
    members: [],
    attendance: [],
    confirming_authority: null,
    tasks: [],
    task_count: 0,
    task_counts: { ...EMPTY_COUNTS },
    latest_deliverable: null,
    alert_summary: {},
    projection_version: 0,
    updated_at: "",
  };
}

function normalizedGroups(
  eventId: string,
  groups: CommandHallGroup[],
): CommandHallGroup[] {
  const groupsByCode = new Map(
    groups.map((group) => [group.workgroup_code, group]),
  );
  return WORKGROUP_DEFINITIONS.map((definition, index) =>
    groupsByCode.get(definition.code) ??
    emptyGroup(eventId, definition.code, definition.name, index + 1),
  );
}

function progressRatio(counts: CommandHallTaskCounts): number {
  if (counts.total <= 0) {
    return 0;
  }
  return Math.min(
    100,
    Math.round(
      ((counts.completed + counts.not_required) / counts.total) * 100,
    ),
  );
}

function latestDeliverableText(group: CommandHallGroup): string {
  const deliverable = asRecord(group.latest_deliverable);
  if (!deliverable) {
    return "暂无当前发布版";
  }
  const title = textValue(
    deliverable.title ?? deliverable.deliverable_code,
    "未命名成果",
  );
  const sourceKind = textValue(deliverable.source_kind, "");
  const source = SOURCE_LABELS[sourceKind] ?? sourceKind;
  return source
    ? `${title} · 当前发布版 · ${source}`
    : `${title} · 当前发布版`;
}

function alertCount(group: CommandHallGroup): number {
  return Object.values(group.alert_summary ?? {}).reduce(
    (total, count) =>
      total + (typeof count === "number" && Number.isFinite(count) ? count : 0),
    0,
  );
}

export function CommandHallGroupBoard({
  eventId,
  groups,
  onOpenGroup,
}: CommandHallGroupBoardProps) {
  return (
    <section
      className="command-hall__groups"
      aria-label="七个工作组状态"
    >
      {normalizedGroups(eventId, groups).map((group) => {
        const counts = group.task_counts ?? EMPTY_COUNTS;
        const alerts = alertCount(group);
        return (
          <button
            className="command-hall__group-card"
            data-testid="command-hall-group-card"
            key={group.workgroup_code}
            type="button"
            aria-label={`打开${group.name}详情，进度 ${counts.completed}/${counts.total}`}
            onClick={() => onOpenGroup(group)}
          >
            <header className="command-hall__group-card-header">
              <span className="command-hall__group-order">
                {String(group.display_order).padStart(2, "0")}
              </span>
              <h3>{group.name}</h3>
            </header>

            <div className="command-hall__group-authority">
              <span>
                组长：{personName(group.leader, group.roster_version ? "未配置" : "待同步")}
              </span>
              <span>{authorityLabel(group)}</span>
            </div>

            <div className="command-hall__group-attendance">
              <span>{attendanceSummary(group)}</span>
              <span>{group.deputies.length} 名副组长</span>
            </div>

            <div className="command-hall__group-progress">
              <div>
                <strong>
                  {counts.completed}/{counts.total}
                </strong>
                <span>任务完成</span>
              </div>
              <div
                className="command-hall__progress-track"
                aria-label={`任务进度 ${progressRatio(counts)}%`}
              >
                <span style={{ width: `${progressRatio(counts)}%` }} />
              </div>
            </div>

            <dl className="command-hall__group-counts">
              <div>
                <dt>待确认</dt>
                <dd>{counts.pending_review}</dd>
              </div>
              <div>
                <dt>临期</dt>
                <dd>{counts.at_risk}</dd>
              </div>
              <div>
                <dt>超时</dt>
                <dd>{counts.overdue}</dd>
              </div>
              <div>
                <dt>失败</dt>
                <dd>{counts.failed}</dd>
              </div>
            </dl>

            <div className="command-hall__group-deliverable">
              <span>关键成果</span>
              <strong>{latestDeliverableText(group)}</strong>
            </div>

            <div
              className={`command-hall__group-alert${
                alerts > 0 ? " command-hall__group-alert--active" : ""
              }`}
            >
              {alerts > 0 ? `${alerts} 项告警` : "无未解决告警"}
            </div>
          </button>
        );
      })}
    </section>
  );
}
