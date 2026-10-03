import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  getCommandHallGroup,
  getCommandHallTask,
} from "../api/client";
import {
  formatDateTime,
  formatEventKind,
  type CommandHallGroupDetail,
  type CommandHallTaskDetail,
} from "../types";

interface CommandHallDetailDrawerProps {
  eventId: string;
  eventKind: string | null;
  groupCode: string;
  groupName: string;
  onClose: () => void;
}

const STATUS_LABELS: Record<string, string> = {
  pending: "待接收",
  in_progress: "处理中",
  pending_review: "待确认",
  completed: "已完成",
  not_required: "不再需要",
  failed: "失败",
};

const TIMELINESS_LABELS: Record<string, string> = {
  on_time: "正常",
  at_risk: "临近截止",
  overdue: "已超时",
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

function personName(person: unknown, fallback = "待确认"): string {
  if (typeof person === "string" && person.trim()) {
    return person;
  }
  const record = asRecord(person);
  if (!record) {
    return fallback;
  }
  return textValue(
    record.username ?? record.display_name ?? record.user_id,
    fallback,
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return "登录状态已失效，请重新登录";
    }
    if (error.status === 403) {
      return "当前账号无权查看该工作组详情";
    }
    if (error.status === 404) {
      return "未找到该工作组或任务的公开数据";
    }
    return error.message;
  }
  return "无法加载工作组详情";
}

function markerClass(kind: string | null): string {
  if (kind === "test" || kind === "drill") {
    return `event-marker event-marker--${kind}`;
  }
  return "event-marker event-marker--unknown";
}

function taskTitle(task: Record<string, unknown>): string {
  return textValue(task.title ?? task.task_code, "未命名任务");
}

function formatTaskStatus(task: Record<string, unknown>): string {
  const status = textValue(task.status, "pending");
  return STATUS_LABELS[status] ?? status;
}

function formatTaskTimeliness(task: Record<string, unknown>): string {
  const state = textValue(task.timeliness_state, "on_time");
  return TIMELINESS_LABELS[state] ?? state;
}

function currentPublishedVersion(
  deliverable: Record<string, unknown>,
): Record<string, unknown> | null {
  const publication = asRecord(deliverable.current_publication);
  const versionId = publication?.version_id;
  if (typeof versionId !== "string") {
    return null;
  }
  return (
    asRecords(deliverable.versions).find(
      (version) => version.id === versionId,
    ) ?? null
  );
}

function publishedSourceLabel(
  deliverable: Record<string, unknown>,
): string {
  const version = currentPublishedVersion(deliverable);
  const sourceKind = textValue(version?.source_kind, "");
  return SOURCE_LABELS[sourceKind] ?? "来源待确认";
}

function publishedFileName(
  deliverable: Record<string, unknown>,
): string {
  const version = currentPublishedVersion(deliverable);
  if (!version) {
    return "当前发布版内容待同步";
  }
  if (typeof version.file_name === "string" && version.file_name) {
    return version.file_name;
  }
  if (asRecord(version.text_result)) {
    return "文字成果";
  }
  return "已发布成果";
}

function TaskDetailView({ detail }: { detail: CommandHallTaskDetail }) {
  const task = asRecord(detail.task) ?? {};
  const contributors = asRecords(detail.contributors);
  const deliverables = asRecords(detail.deliverables);

  return (
    <section
      className="command-hall-drawer__task-detail"
      aria-labelledby="command-hall-task-detail-title"
    >
      <header>
        <p className="eyebrow">任务详情</p>
        <h3 id="command-hall-task-detail-title">{taskTitle(task)}</h3>
        <p>{textValue(task.instruction, "暂无任务说明")}</p>
      </header>

      <dl className="command-hall-drawer__facts">
        <div>
          <dt>状态</dt>
          <dd>{formatTaskStatus(task)}</dd>
        </div>
        <div>
          <dt>时效</dt>
          <dd>{formatTaskTimeliness(task)}</dd>
        </div>
        <div>
          <dt>截止时间</dt>
          <dd>{formatDateTime(textValue(task.due_at, ""))}</dd>
        </div>
        <div>
          <dt>处理贡献人</dt>
          <dd>
            {contributors.length > 0
              ? contributors.map((item) => personName(item)).join("、")
              : "暂无贡献人"}
          </dd>
        </div>
      </dl>

      <section className="command-hall-drawer__published">
        <header>
          <div>
            <p className="eyebrow">已发布成果</p>
            <h4>当前发布版</h4>
          </div>
          <span>仅展示已发布版本</span>
        </header>

        {deliverables.length === 0 ? (
          <p className="command-hall-drawer__empty">
            暂无可展示的已发布成果
          </p>
        ) : (
          <div className="command-hall-drawer__publication-list">
            {deliverables.map((deliverable) => {
              const publication = asRecord(
                deliverable.current_publication,
              );
              const version = currentPublishedVersion(deliverable);
              return (
                <article
                  className="command-hall-drawer__publication"
                  key={textValue(
                    deliverable.id ?? deliverable.deliverable_code,
                  )}
                >
                  <div>
                    <strong>{textValue(deliverable.title, "未命名成果")}</strong>
                    <span>
                      {publication
                        ? `当前发布版 · ${publishedSourceLabel(deliverable)}`
                        : "尚无当前发布版"}
                    </span>
                  </div>
                  {publication ? (
                    <>
                      <p>{publishedFileName(deliverable)}</p>
                      <p>
                        {version?.version_no !== undefined
                          ? `V${textValue(version.version_no)} · `
                          : ""}
                        发布人：
                        {personName(
                          publication.published_by ?? publication.published_role,
                        )}
                        {publication.published_at
                          ? ` · ${formatDateTime(
                              textValue(publication.published_at),
                            )}`
                          : ""}
                      </p>
                    </>
                  ) : null}
                </article>
              );
            })}
          </div>
        )}
      </section>
    </section>
  );
}

export function CommandHallDetailDrawer({
  eventId,
  eventKind,
  groupCode,
  groupName,
  onClose,
}: CommandHallDetailDrawerProps) {
  const [detail, setDetail] = useState<CommandHallGroupDetail | null>(null);
  const [detailStatus, setDetailStatus] = useState<
    "loading" | "ready" | "error"
  >("loading");
  const [detailError, setDetailError] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [taskDetail, setTaskDetail] = useState<CommandHallTaskDetail | null>(
    null,
  );
  const [taskStatus, setTaskStatus] = useState<
    "idle" | "loading" | "ready" | "error"
  >("idle");
  const [taskError, setTaskError] = useState("");
  const groupRequestRef = useRef(0);
  const taskRequestRef = useRef(0);

  useEffect(() => {
    const requestId = ++groupRequestRef.current;
    taskRequestRef.current += 1;
    setDetail(null);
    setDetailStatus("loading");
    setDetailError("");
    setSelectedTaskId(null);
    setTaskDetail(null);
    setTaskStatus("idle");
    setTaskError("");

    void getCommandHallGroup(eventId, groupCode)
      .then((loaded) => {
        if (requestId !== groupRequestRef.current) {
          return;
        }
        setDetail(loaded);
        setDetailStatus("ready");
      })
      .catch((error: unknown) => {
        if (requestId !== groupRequestRef.current) {
          return;
        }
        setDetailError(errorMessage(error));
        setDetailStatus("error");
      });

    return () => {
      groupRequestRef.current += 1;
      taskRequestRef.current += 1;
    };
  }, [eventId, groupCode]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  async function openTask(task: Record<string, unknown>) {
    const taskId = textValue(task.id, "");
    if (!taskId) {
      return;
    }
    const requestId = ++taskRequestRef.current;
    setSelectedTaskId(taskId);
    setTaskDetail(null);
    setTaskStatus("loading");
    setTaskError("");
    try {
      const loaded = await getCommandHallTask(taskId);
      if (requestId !== taskRequestRef.current) {
        return;
      }
      if (loaded.event_id !== eventId) {
        setTaskError("任务不属于当前事件");
        setTaskStatus("error");
        return;
      }
      setTaskDetail(loaded);
      setTaskStatus("ready");
    } catch (error) {
      if (requestId !== taskRequestRef.current) {
        return;
      }
      setTaskError(errorMessage(error));
      setTaskStatus("error");
    }
  }

  const group = detail?.group ?? {};
  const tasks = detail?.tasks ?? [];
  const leader = group.leader;
  const deputies = asRecords(group.deputies);
  const authority = asRecord(group.confirming_authority);

  return (
    <div className="command-hall-drawer-layer">
      <button
        className="command-hall-drawer__scrim"
        type="button"
        aria-label="关闭工作组详情"
        onClick={onClose}
      />
      <aside
        className="command-hall-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="command-hall-drawer-title"
      >
        <header className="command-hall-drawer__header">
          <div>
            <div className="command-hall-drawer__title-row">
              <h2 id="command-hall-drawer-title">
                {textValue(group.name, groupName)}
              </h2>
              <span className={markerClass(eventKind)}>
                {eventKind ? formatEventKind(eventKind) : "事件标识待确认"}
              </span>
            </div>
            <p>总览保持在后方，详情按预案阶段和公开成果下钻</p>
          </div>
          <button
            className="secondary-button"
            type="button"
            onClick={onClose}
          >
            关闭详情
          </button>
        </header>

        {detailStatus === "loading" ? (
          <div className="state-panel" role="status">
            <span className="state-icon" aria-hidden="true" />
            <p>正在加载工作组详情</p>
          </div>
        ) : null}

        {detailStatus === "error" ? (
          <div className="state-panel state-panel--error" role="alert">
            <p>{detailError}</p>
            <button
              className="secondary-button"
              type="button"
              onClick={() => {
                const requestId = ++groupRequestRef.current;
                setDetailStatus("loading");
                void getCommandHallGroup(eventId, groupCode)
                  .then((loaded) => {
                    if (requestId !== groupRequestRef.current) {
                      return;
                    }
                    setDetail(loaded);
                    setDetailStatus("ready");
                  })
                  .catch((error: unknown) => {
                    if (requestId !== groupRequestRef.current) {
                      return;
                    }
                    setDetailError(errorMessage(error));
                    setDetailStatus("error");
                  });
              }}
            >
              重新加载
            </button>
          </div>
        ) : null}

        {detailStatus === "ready" ? (
          <div className="command-hall-drawer__content">
            <section
              className="command-hall-drawer__roster"
              aria-labelledby="command-hall-roster-title"
            >
              <header>
                <div>
                  <p className="eyebrow">事件名单</p>
                  <h3 id="command-hall-roster-title">到岗与代理权限</h3>
                </div>
              </header>
              <dl>
                <div>
                  <dt>组长</dt>
                  <dd>{personName(leader, "未配置")}</dd>
                </div>
                <div>
                  <dt>副组长顺序</dt>
                  <dd>
                    {deputies.length > 0
                      ? deputies
                          .map(
                            (deputy, index) =>
                              `${index + 1}. ${personName(deputy)}`,
                          )
                          .join("；")
                      : "未配置"}
                  </dd>
                </div>
                <div>
                  <dt>当前负责人</dt>
                  <dd>
                    {authority
                      ? `${personName(authority)}${
                          authority.role === "deputy" ? "（代理）" : ""
                        }`
                      : "待确认"}
                  </dd>
                </div>
              </dl>
            </section>

            <section
              className="command-hall-drawer__tasks"
              aria-labelledby="command-hall-tasks-title"
            >
              <header>
                <div>
                  <p className="eyebrow">预案阶段任务</p>
                  <h3 id="command-hall-tasks-title">任务清单</h3>
                </div>
                <span>{tasks.length} 项</span>
              </header>

              {tasks.length === 0 ? (
                <p className="command-hall-drawer__empty">暂无工作组任务</p>
              ) : (
                <div className="command-hall-drawer__task-list">
                  {tasks.map((task) => {
                    const taskId = textValue(task.id, "");
                    return (
                      <button
                        className={`command-hall-drawer__task${
                          selectedTaskId === taskId
                            ? " command-hall-drawer__task--selected"
                            : ""
                        }`}
                        key={taskId || taskTitle(task)}
                        type="button"
                        aria-label={`查看任务详情：${taskTitle(task)}`}
                        onClick={() => void openTask(task)}
                      >
                        <span>
                          <strong>{taskTitle(task)}</strong>
                          <small>{textValue(task.instruction, "暂无说明")}</small>
                        </span>
                        <span>
                          <em>{formatTaskStatus(task)}</em>
                          <small>
                            {formatTaskTimeliness(task)} ·{" "}
                            {formatDateTime(textValue(task.due_at, ""))}
                          </small>
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </section>

            {taskStatus === "loading" ? (
              <div className="state-panel" role="status">
                <span className="state-icon" aria-hidden="true" />
                <p>正在加载任务详情</p>
              </div>
            ) : null}

            {taskStatus === "error" ? (
              <div className="state-panel state-panel--error" role="alert">
                <p>{taskError}</p>
              </div>
            ) : null}

            {taskStatus === "ready" && taskDetail ? (
              <TaskDetailView detail={taskDetail} />
            ) : null}
          </div>
        ) : null}
      </aside>
    </div>
  );
}
