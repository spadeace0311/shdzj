import { useEffect, useRef, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";

import {
  ApiError,
  completeCollaborationTask,
  createTemporaryTask,
  getCollaborationTask,
  getEvent,
  listCollaborationTasks,
  newIdempotencyKey,
  startCollaborationTask,
} from "../api/client";
import { CollaborationTaskPanel } from "../components/CollaborationTaskPanel";
import {
  formatDateTime,
  type WorkgroupTask,
  type WorkgroupTaskStatus,
  type WorkgroupTaskTimelinessState,
} from "../types";

interface WorkgroupTasksPageProps {
  userRole: string;
  workgroup: string | null;
}

const WORKGROUP_CODES: Record<string, string> = {
  新闻信息值守组: "news_information",
  监测预报组: "monitoring_forecast",
  综合协调组: "comprehensive_coordination",
  震害评估组: "damage_assessment",
  应急技术组: "emergency_technology",
  后勤保障组: "logistics",
  中心站组: "center_station",
};

const WORKGROUP_OPTIONS = [
  { code: "news_information", name: "新闻信息值守组" },
  { code: "monitoring_forecast", name: "监测预报组" },
  { code: "comprehensive_coordination", name: "综合协调组" },
  { code: "damage_assessment", name: "震害评估组" },
  { code: "emergency_technology", name: "应急技术组" },
  { code: "logistics", name: "后勤保障组" },
  { code: "center_station", name: "中心站组" },
] as const;

const PHASE_ORDER = [
  "within_30m",
  "30m_to_60m",
  "1h_to_2h",
  "2h_to_4h",
  "4h_to_end",
  "1h_to_end",
  "2h_to_end",
  "other",
] as const;

const PHASE_LABELS: Record<string, string> = {
  within_30m: "震后30分钟内",
  "30m_to_60m": "震后30分钟至1小时",
  "1h_to_2h": "震后1至2小时",
  "2h_to_4h": "震后2至4小时",
  "4h_to_end": "震后4小时至应急结束",
  "1h_to_end": "震后1小时至应急结束",
  "2h_to_end": "震后2小时至应急结束",
  other: "其他任务",
};

const STATUS_LABELS: Record<WorkgroupTaskStatus, string> = {
  pending: "待接收",
  in_progress: "处理中",
  pending_review: "待确认",
  completed: "已完成",
  not_required: "不再需要",
  failed: "失败",
};

const TIMELINESS_LABELS: Record<WorkgroupTaskTimelinessState, string> = {
  on_time: "正常",
  at_risk: "临近截止",
  overdue: "已超时",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return "任务已被其他人更新，请刷新后重试";
    }
    if (error.status === 403) {
      return "当前账号无权执行该操作";
    }
    return error.message;
  }
  return "操作失败，请稍后重试";
}

function eventMarker(kind: string | null): {
  text: string;
  className: string;
} | null {
  if (kind === "test") {
    return { text: "测试", className: "event-marker event-marker--test" };
  }
  if (kind === "drill") {
    return { text: "演练", className: "event-marker event-marker--drill" };
  }
  return null;
}

function phaseForTask(task: WorkgroupTask): string {
  return task.phase_code && PHASE_LABELS[task.phase_code]
    ? task.phase_code
    : "other";
}

export function WorkgroupTasksPage({
  userRole,
  workgroup,
}: WorkgroupTasksPageProps) {
  const { eventId } = useParams<{ eventId: string }>();
  const [tasks, setTasks] = useState<WorkgroupTask[]>([]);
  const [eventKind, setEventKind] = useState<string | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<WorkgroupTask | null>(null);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  const [actionKeys, setActionKeys] = useState<Record<string, string>>({});
  const [temporaryKey, setTemporaryKey] = useState<string | null>(null);
  const [temporaryBusy, setTemporaryBusy] = useState(false);
  const listRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const eventIdRef = useRef(eventId);
  eventIdRef.current = eventId;
  const [temporaryForm, setTemporaryForm] = useState({
    title: "",
    workgroupCode: "comprehensive_coordination",
    instruction: "",
    priority: "10",
    dueAt: "",
    continuesUntilCancelled: false,
  });

  const currentWorkgroupCode = WORKGROUP_CODES[workgroup ?? ""] ?? null;
  const visibleTasks = tasks;
  const marker = eventMarker(eventKind);
  const canCreateTemporary =
    Boolean(eventId) &&
    (userRole === "superadmin" ||
      (currentWorkgroupCode === "comprehensive_coordination" &&
        ["group_leader", "group_deputy", "group_member"].includes(userRole)));

  async function loadTasks() {
    const requestId = ++listRequestRef.current;
    if (!eventId) {
      setStatus("ready");
      return;
    }
    setStatus("loading");
    setError("");
    try {
      const [taskList, eventDetail] = await Promise.all([
        listCollaborationTasks(eventId),
        getEvent(eventId).catch(() => null),
      ]);
      if (
        requestId !== listRequestRef.current ||
        eventId !== eventIdRef.current
      ) {
        return;
      }
      setTasks(taskList);
      setEventKind(eventDetail?.event_kind ?? null);
      setSelectedTaskId((current) =>
        current && taskList.some((task) => task.id === current)
          ? current
          : null,
      );
      setSelectedTask((current) => {
        if (!current) {
          return null;
        }
        return taskList.find((task) => task.id === current.id) ?? null;
      });
      setStatus("ready");
    } catch (caught) {
      if (
        requestId !== listRequestRef.current ||
        eventId !== eventIdRef.current
      ) {
        return;
      }
      setStatus("error");
      setError(errorMessage(caught));
    }
  }

  useEffect(() => {
    listRequestRef.current += 1;
    detailRequestRef.current += 1;
    setTasks([]);
    setEventKind(null);
    setSelectedTaskId(null);
    setSelectedTask(null);
    setError("");
    setSuccess("");
    setActionKeys({});
    setTemporaryKey(null);
    void loadTasks();
  }, [eventId]);

  async function refreshTasks() {
    if (!eventId) {
      return;
    }
    const requestEventId = eventId;
    const requestId = ++listRequestRef.current;
    const taskList = await listCollaborationTasks(requestEventId);
    if (
      requestId !== listRequestRef.current ||
      requestEventId !== eventIdRef.current
    ) {
      return;
    }
    setTasks(taskList);
    setSelectedTaskId((current) =>
      current && taskList.some((task) => task.id === current)
        ? current
        : null,
    );
    setSelectedTask((current) => {
      if (!current) {
        return null;
      }
      return taskList.find((task) => task.id === current.id) ?? null;
    });
  }

  async function openTask(taskId: string) {
    const requestId = ++detailRequestRef.current;
    setSelectedTaskId(taskId);
    setSelectedTask(null);
    setActionKeys({});
    setError("");
    setSuccess("");
    try {
      const detail = await getCollaborationTask(taskId);
      if (requestId !== detailRequestRef.current) {
        return;
      }
      setSelectedTask(detail);
    } catch (caught) {
      if (requestId !== detailRequestRef.current) {
        return;
      }
      setError(errorMessage(caught));
    }
  }

  function reusableKey(key: string): string {
    const existing = actionKeys[key];
    if (existing) {
      return existing;
    }
    const created = newIdempotencyKey();
    setActionKeys((current) => ({ ...current, [key]: created }));
    return created;
  }

  function clearKey(key: string) {
    setActionKeys((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  async function runRowAction(
    task: WorkgroupTask,
    operationKey: string,
    operation: (idempotencyKey: string) => Promise<unknown>,
    successMessage: string,
  ) {
    const requestEventId = eventIdRef.current;
    setBusyTaskId(task.id);
    setError("");
    setSuccess("");
    try {
      await operation(reusableKey(operationKey));
      if (
        eventIdRef.current !== requestEventId ||
        eventIdRef.current !== task.event_id
      ) {
        return;
      }
      clearKey(operationKey);
      await refreshTasks();
      if (
        eventIdRef.current !== requestEventId ||
        eventIdRef.current !== task.event_id
      ) {
        return;
      }
      setSuccess(successMessage);
    } catch (caught) {
      if (eventIdRef.current === requestEventId) {
        setError(errorMessage(caught));
      }
    } finally {
      if (eventIdRef.current === requestEventId) {
        setBusyTaskId(null);
      }
    }
  }

  async function handleTemporaryTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!eventId) {
      return;
    }
    if (!temporaryForm.title.trim()) {
      setError("请填写临时任务标题");
      return;
    }
    if (!temporaryForm.instruction.trim()) {
      setError("请填写任务说明");
      return;
    }
    if (
      !temporaryForm.continuesUntilCancelled &&
      !temporaryForm.dueAt
    ) {
      setError("请填写截止时间或选择持续到另行通知");
      return;
    }

    const idempotencyKey = temporaryKey ?? newIdempotencyKey();
    if (!temporaryKey) {
      setTemporaryKey(idempotencyKey);
    }
    const requestEventId = eventId;
    setTemporaryBusy(true);
    setError("");
    setSuccess("");
    try {
      await createTemporaryTask(
        eventId,
        {
          workgroup_code: temporaryForm.workgroupCode,
          title: temporaryForm.title.trim(),
          instruction: temporaryForm.instruction.trim(),
          priority: Number(temporaryForm.priority),
          due_at:
            temporaryForm.continuesUntilCancelled || !temporaryForm.dueAt
              ? null
              : new Date(temporaryForm.dueAt).toISOString(),
          continues_until_cancelled:
            temporaryForm.continuesUntilCancelled,
        },
        idempotencyKey,
      );
      if (eventIdRef.current !== requestEventId) {
        return;
      }
      setTemporaryKey(null);
      setTemporaryForm({
        title: "",
        workgroupCode: "comprehensive_coordination",
        instruction: "",
        priority: "10",
        dueAt: "",
        continuesUntilCancelled: false,
      });
      await refreshTasks();
      setSuccess("临时任务已创建");
    } catch (caught) {
      if (eventIdRef.current === requestEventId) {
        setError(errorMessage(caught));
      }
    } finally {
      if (eventIdRef.current === requestEventId) {
        setTemporaryBusy(false);
      }
    }
  }

  if (!eventId) {
    return (
      <section className="page-section" aria-labelledby="workgroup-tasks-title">
        <header className="page-heading">
          <div>
            <p className="eyebrow">应急协同</p>
            <h1 id="workgroup-tasks-title">工作组任务</h1>
          </div>
        </header>
        <div className="state-panel">
          <p>请从事件详情进入工作组任务控制台</p>
        </div>
      </section>
    );
  }

  return (
    <section className="page-section workgroup-tasks-page" aria-labelledby="workgroup-tasks-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">应急协同</p>
          <div className="workgroup-tasks-page__title-row">
            <h1 id="workgroup-tasks-title">工作组任务</h1>
            {marker ? (
              <span className={marker.className}>{marker.text}</span>
            ) : null}
            {eventKind === null ? (
              <span className="event-marker event-marker--unknown">
                事件类型待确认
              </span>
            ) : null}
          </div>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void loadTasks()}
          disabled={status === "loading"}
        >
          刷新
        </button>
      </header>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      {success ? (
        <p className="form-success" role="status">
          {success}
        </p>
      ) : null}

      {status === "loading" ? (
        <div className="state-panel">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载工作组任务</p>
        </div>
      ) : null}
      {status === "error" ? (
        <div className="state-panel state-panel--error" role="alert">
          <p>{error || "无法加载工作组任务"}</p>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void loadTasks()}
          >
            重试
          </button>
        </div>
      ) : null}

      {status === "ready" ? (
        <div className="workgroup-tasks-layout">
          <section className="workgroup-task-list" aria-label="工作组任务列表">
            {visibleTasks.length === 0 ? (
              <p className="workgroup-task-list__empty">
                {currentWorkgroupCode || userRole === "superadmin"
                  ? "暂无任务"
                  : "当前账号未关联工作组"}
              </p>
            ) : (
              PHASE_ORDER.map((phase) => {
                const phaseTasks = visibleTasks.filter(
                  (task) => phaseForTask(task) === phase,
                );
                if (phaseTasks.length === 0) {
                  return null;
                }
                return (
                  <section className="workgroup-task-phase" key={phase}>
                    <header>
                      <h2>{PHASE_LABELS[phase]}</h2>
                      <span>{phaseTasks.length} 项</span>
                    </header>
                    <div className="workgroup-task-rows">
                      {phaseTasks.map((task) => {
                        const canWork = task.can_work;
                        const canConfirm = task.can_confirm;
                        return (
                          <article
                            className={`workgroup-task-row${
                              selectedTaskId === task.id
                                ? " workgroup-task-row--selected"
                                : ""
                            }`}
                            key={task.id}
                          >
                            <button
                              className="workgroup-task-row__main"
                              type="button"
                              aria-label={`查看任务详情：${task.title}`}
                              onClick={() => void openTask(task.id)}
                            >
                              <strong>{task.title}</strong>
                              <span>{task.instruction}</span>
                            </button>
                            <div className="workgroup-task-row__metadata">
                              <span
                                className={`task-status task-status--${task.status}`}
                              >
                                {STATUS_LABELS[task.status]}
                              </span>
                              <span
                                className={`timeliness-state timeliness-state--${task.timeliness_state}`}
                              >
                                {TIMELINESS_LABELS[task.timeliness_state]}
                              </span>
                              <span>{formatDateTime(task.due_at)}</span>
                              <span>
                                {task.contributors.length > 0
                                  ? task.contributors
                                      .map((item) => item.username)
                                      .join("、")
                                  : "暂无贡献人"}
                              </span>
                            </div>
                            <div className="workgroup-task-row__actions">
                              {canWork && task.status === "pending" ? (
                                <button
                                  className="primary-button"
                                  type="button"
                                  disabled={busyTaskId === task.id}
                                  onClick={() =>
                                    void runRowAction(
                                      task,
                                      `task-start:${task.id}`,
                                      (key) =>
                                        startCollaborationTask(
                                          task.id,
                                          task.row_version,
                                          key,
                                        ),
                                      "任务已开始处理",
                                    )
                                  }
                                >
                                  开始处理
                                </button>
                              ) : null}
                              {canWork && task.status === "in_progress" ? (
                                <button
                                  className="secondary-button"
                                  type="button"
                                  onClick={() => void openTask(task.id)}
                                >
                                  填写结果
                                </button>
                              ) : null}
                              {canConfirm && task.status === "pending_review" ? (
                                <button
                                  className="primary-button"
                                  type="button"
                                  disabled={busyTaskId === task.id}
                                  onClick={() =>
                                    void runRowAction(
                                      task,
                                      `task-complete:${task.id}`,
                                      (key) =>
                                        completeCollaborationTask(
                                          task.id,
                                          task.row_version,
                                          key,
                                        ),
                                      "任务已确认完成",
                                    )
                                  }
                                >
                                  确认完成
                                </button>
                              ) : null}
                            </div>
                            {eventKind === "test" || eventKind === "drill" ? (
                              <span
                                className={`task-event-marker task-event-marker--${eventKind}`}
                              >
                                {eventKind === "test" ? "测试" : "演练"}
                              </span>
                            ) : null}
                          </article>
                        );
                      })}
                    </div>
                  </section>
                );
              })
            )}
          </section>

          <div className="workgroup-task-detail">
            {selectedTask ? (
              <CollaborationTaskPanel
                key={selectedTask.id}
                task={selectedTask}
                canWork={selectedTask.can_work}
                canConfirm={selectedTask.can_confirm}
                eventKind={eventKind}
                onRefresh={refreshTasks}
              />
            ) : (
              <div className="workgroup-task-detail__empty">
                <p>选择任务后在此查看详情、成果版本并执行操作</p>
              </div>
            )}
          </div>
        </div>
      ) : null}

      {status === "ready" && canCreateTemporary ? (
        <section className="temporary-task-panel" aria-labelledby="temporary-task-title">
          <header className="section-header">
            <div>
              <p className="eyebrow">预案外事项</p>
              <h2 id="temporary-task-title">创建临时任务</h2>
            </div>
          </header>
          <form className="temporary-task-form" onSubmit={handleTemporaryTask}>
            <label htmlFor="temporary-title">临时任务标题</label>
            <input
              id="temporary-title"
              value={temporaryForm.title}
              onChange={(event) => {
                setTemporaryForm((current) => ({
                  ...current,
                  title: event.target.value,
                }));
                setTemporaryKey(null);
              }}
            />

            <label htmlFor="temporary-workgroup">责任工作组</label>
            <select
              id="temporary-workgroup"
              value={temporaryForm.workgroupCode}
              onChange={(event) => {
                setTemporaryForm((current) => ({
                  ...current,
                  workgroupCode: event.target.value,
                }));
                setTemporaryKey(null);
              }}
            >
              {WORKGROUP_OPTIONS.map((item) => (
                <option key={item.code} value={item.code}>
                  {item.name}
                </option>
              ))}
            </select>

            <label htmlFor="temporary-instruction">任务说明</label>
            <textarea
              id="temporary-instruction"
              value={temporaryForm.instruction}
              onChange={(event) => {
                setTemporaryForm((current) => ({
                  ...current,
                  instruction: event.target.value,
                }));
                setTemporaryKey(null);
              }}
            />

            <label htmlFor="temporary-priority">优先级</label>
            <input
              id="temporary-priority"
              type="number"
              min="0"
              value={temporaryForm.priority}
              onChange={(event) => {
                setTemporaryForm((current) => ({
                  ...current,
                  priority: event.target.value,
                }));
                setTemporaryKey(null);
              }}
            />

            <label htmlFor="temporary-due">截止时间</label>
            <input
              id="temporary-due"
              type="datetime-local"
              value={temporaryForm.dueAt}
              disabled={temporaryForm.continuesUntilCancelled}
              onChange={(event) => {
                setTemporaryForm((current) => ({
                  ...current,
                  dueAt: event.target.value,
                }));
                setTemporaryKey(null);
              }}
            />

            <label className="temporary-task-form__checkbox">
              <input
                type="checkbox"
                checked={temporaryForm.continuesUntilCancelled}
                onChange={(event) => {
                  setTemporaryForm((current) => ({
                    ...current,
                    continuesUntilCancelled: event.target.checked,
                    dueAt: event.target.checked ? "" : current.dueAt,
                  }));
                  setTemporaryKey(null);
                }}
              />
              持续到另行通知
            </label>

            <div className="form-actions">
              <button
                className="primary-button"
                type="submit"
                disabled={temporaryBusy}
              >
                创建临时任务
              </button>
            </div>
          </form>
        </section>
      ) : null}
    </section>
  );
}
