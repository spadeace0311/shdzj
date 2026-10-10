import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";
import { Link } from "react-router-dom";

import {
  getOperationsOverview,
  getOperationsSettings,
  resumeOperationsDrill,
  triggerOperationsDrill,
  triggerOperationsTest,
  updateOperationsSettings,
} from "../api/client";
import {
  formatDateTime,
  type OperationsOverview,
  type OperationsRun,
  type OperationsRunState,
  type OperationsSchedule,
  type OperationsSettings,
  type OperationsSettingsUpdateInput,
  type OperationsTriggerResult,
} from "../types";

export type {
  OperationsOverview,
  OperationsSettings,
  OperationsTriggerResult,
};

interface OperationsPageProps {
  userRole: string;
}

type PageStatus = "loading" | "ready" | "error";
type TriggerMode = "manual_test" | "drill";
type TriggerNotice = {
  kind: "success" | "error";
  text: string;
};

interface SettingsDraft {
  daily_window_start: string;
  daily_window_end: string;
  test_magnitude_min: string;
  test_magnitude_max: string;
  test_depth_min_km: string;
  test_depth_max_km: string;
  drill_conflict_policy: "pause" | "terminate";
}

const SCHEDULE_STATE_LABELS: Record<string, string> = {
  scheduled: "已排定",
  due: "已到窗口",
  deferred: "已延后",
  triggered: "已触发",
  skipped: "已跳过",
  failed: "失败",
};

const RUN_STATE_LABELS: Record<OperationsRunState, string> = {
  queued: "排队中",
  running: "运行中",
  paused: "已暂停",
  completed: "已完成",
  canceled: "已取消",
  failed: "失败",
};

const TRIGGER_MODE_LABELS: Record<OperationsRun["trigger_mode"], string> = {
  scheduled_test: "每日测试",
  manual_test: "手工测试",
  drill: "演练",
  manual_event: "手工正式事件",
};

function newIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) =>
    byte.toString(16).padStart(2, "0"),
  );
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

function settingsToDraft(settings: OperationsSettings): SettingsDraft {
  return {
    daily_window_start: settings.daily_window_start.slice(0, 5),
    daily_window_end: settings.daily_window_end.slice(0, 5),
    test_magnitude_min: settings.test_magnitude_min,
    test_magnitude_max: settings.test_magnitude_max,
    test_depth_min_km: settings.test_depth_min_km,
    test_depth_max_km: settings.test_depth_max_km,
    drill_conflict_policy: settings.drill_conflict_policy,
  };
}

function scheduleStateLabel(state: string): string {
  return SCHEDULE_STATE_LABELS[state] ?? state;
}

function runStateLabel(state: OperationsRunState): string {
  return RUN_STATE_LABELS[state];
}

function triggerModeLabel(mode: OperationsRun["trigger_mode"]): string {
  return TRIGGER_MODE_LABELS[mode];
}

function remainingText(scheduledAt: string): string {
  const target = new Date(scheduledAt).getTime();
  if (Number.isNaN(target)) {
    return "-";
  }
  const differenceMs = target - Date.now();
  if (differenceMs <= 0) {
    return "已进入窗口";
  }
  const totalMinutes = Math.floor(differenceMs / 60_000);
  const days = Math.floor(totalMinutes / 1_440);
  const hours = Math.floor((totalMinutes % 1_440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) {
    return `${days} 天 ${hours} 小时后`;
  }
  if (hours > 0) {
    return `${hours} 小时 ${minutes} 分钟后`;
  }
  return `${minutes} 分钟后`;
}

function scenarioText(settings: OperationsSettings): string {
  return `随机上海 ${settings.test_magnitude_min}-${settings.test_magnitude_max} 级测试场景`;
}

function scheduleDescription(schedule: OperationsSchedule): string {
  return `${formatDateTime(schedule.window_start)} - ${formatDateTime(schedule.window_end)}`;
}

function buildSettingsInput(
  settings: OperationsSettings,
  draft: SettingsDraft,
): OperationsSettingsUpdateInput {
  return {
    expected_version: settings.row_version,
    daily_window_start: `${draft.daily_window_start}:00`,
    daily_window_end: `${draft.daily_window_end}:00`,
    test_magnitude_min: draft.test_magnitude_min,
    test_magnitude_max: draft.test_magnitude_max,
    test_depth_min_km: draft.test_depth_min_km,
    test_depth_max_km: draft.test_depth_max_km,
    drill_conflict_policy: draft.drill_conflict_policy,
  };
}

function isRetryableTriggerError(error: unknown): boolean {
  if (error && typeof error === "object" && "status" in error) {
    const status = Number((error as { status?: unknown }).status);
    return status === 0 || status === 408 || status >= 500;
  }
  return true;
}

async function runTriggerRequest(request: {
  mode: TriggerMode;
  idempotencyKey: string;
}): Promise<OperationsTriggerResult> {
  const execute = () =>
    request.mode === "manual_test"
      ? triggerOperationsTest(request.idempotencyKey)
      : triggerOperationsDrill(request.idempotencyKey);

  try {
    return await execute();
  } catch (error) {
    if (!isRetryableTriggerError(error)) {
      throw error;
    }
    return execute();
  }
}

function triggerFeedbackText(
  actionLabel: string,
  result: OperationsTriggerResult,
): string {
  const state = result.operation_state || result.outcome;
  if (state === "deferred") {
    return `${actionLabel}已延后`;
  }
  if (state === "canceled") {
    return `${actionLabel}已终止`;
  }
  if (result.run_id) {
    return `${actionLabel}已触发，运行编号 ${result.run_id}`;
  }
  return `${actionLabel}已受理`;
}

export function OperationsPage({ userRole }: OperationsPageProps) {
  const [overview, setOverview] = useState<OperationsOverview | null>(null);
  const [settings, setSettings] = useState<OperationsSettings | null>(null);
  const [status, setStatus] = useState<PageStatus>("loading");
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsNotice, setSettingsNotice] = useState<TriggerNotice | null>(null);
  const [pendingTrigger, setPendingTrigger] = useState<{
    mode: TriggerMode;
    idempotencyKey: string;
  } | null>(null);
  const [triggerBusy, setTriggerBusy] = useState<TriggerMode | null>(null);
  const [resumingRunId, setResumingRunId] = useState<string | null>(null);
  const [triggerNotice, setTriggerNotice] = useState<TriggerNotice | null>(null);

  const loadData = useCallback(async () => {
    setStatus("loading");
    setSettingsNotice(null);
    try {
      const [nextOverview, nextSettings] = await Promise.all([
        getOperationsOverview(),
        getOperationsSettings(),
      ]);
      setOverview(nextOverview);
      setSettings(nextSettings);
      setSettingsDraft(settingsToDraft(nextSettings));
      setStatus("ready");
    } catch {
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  async function executeTrigger(request: {
    mode: TriggerMode;
    idempotencyKey: string;
  }) {
    setTriggerBusy(request.mode);
    setTriggerNotice(null);
    try {
      const result = await runTriggerRequest(request);
      const actionLabel =
        request.mode === "manual_test" ? "手工测试" : "演练测试";
      setTriggerNotice({
        kind: "success",
        text: triggerFeedbackText(actionLabel, result),
      });
      setPendingTrigger(null);
      await loadData();
    } catch {
      setTriggerNotice({
        kind: "error",
        text: "操作未完成，可使用上次请求重试",
      });
    } finally {
      setTriggerBusy(null);
    }
  }

  function startTrigger(mode: TriggerMode) {
    const request = { mode, idempotencyKey: newIdempotencyKey() };
    setPendingTrigger(request);
    void executeTrigger(request);
  }

  async function resumeDrill(runId: string) {
    setResumingRunId(runId);
    setTriggerNotice(null);
    try {
      const result = await resumeOperationsDrill(runId);
      setTriggerNotice({
        kind: "success",
        text: triggerFeedbackText("演练恢复", result),
      });
      await loadData();
    } catch {
      setTriggerNotice({
        kind: "error",
        text: "演练恢复失败",
      });
    } finally {
      setResumingRunId(null);
    }
  }

  async function handleSaveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || !settingsDraft) {
      return;
    }
    setSettingsSaving(true);
    setSettingsNotice(null);
    try {
      const updated = await updateOperationsSettings(
        buildSettingsInput(settings, settingsDraft),
      );
      setSettings(updated);
      setSettingsDraft(settingsToDraft(updated));
      setSettingsNotice({ kind: "success", text: "运维设置已保存" });
    } catch {
      setSettingsNotice({ kind: "error", text: "运维设置保存失败" });
    } finally {
      setSettingsSaving(false);
    }
  }

  return (
    <section className="page-section operations-page" aria-labelledby="operations-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">运维控制</p>
          <h1 id="operations-title">测试与演练</h1>
        </div>
        <button
          className="secondary-button"
          type="button"
          disabled={status === "loading"}
          onClick={() => void loadData()}
        >
          刷新状态
        </button>
      </header>

      {status === "loading" ? (
        <div className="state-panel">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载运维状态</p>
        </div>
      ) : null}

      {status === "error" ? (
        <div className="state-panel state-panel--error" role="alert">
          <p>无法加载运维状态</p>
          <button className="secondary-button" type="button" onClick={() => void loadData()}>
            重试
          </button>
        </div>
      ) : null}

      {status === "ready" && overview && settings ? (
        <>
          <section className="operations-overview" aria-labelledby="operations-protection-title">
            <header>
              <div>
                <p className="eyebrow">真实事件保护</p>
                <h2 id="operations-protection-title">
                  {overview.real_event_priority_active
                    ? "真实事件优先保护中"
                    : "当前无真实事件保护"}
                </h2>
              </div>
              <span
                className={`operations-status ${
                  overview.real_event_priority_active
                    ? "operations-status--protected"
                    : "operations-status--clear"
                }`}
              >
                {overview.real_event_priority_active ? "保护中" : "可测试"}
              </span>
            </header>
            <dl className="operations-facts">
              <div>
                <dt>下一次每日测试</dt>
                <dd>
                  {overview.next_schedule
                    ? `${scheduleDescription(overview.next_schedule)}（${remainingText(overview.next_schedule.scheduled_at)}）`
                    : "暂无下一次每日测试计划"}
                </dd>
              </div>
              <div>
                <dt>今日计划</dt>
                <dd>
                  {overview.today_schedule
                    ? scheduleStateLabel(overview.today_schedule.state)
                    : "今日暂无计划"}
                </dd>
              </div>
              <div>
                <dt>活动运行</dt>
                <dd>{overview.active_run_count}</dd>
              </div>
            </dl>
          </section>

          <section className="operations-actions" aria-labelledby="operations-actions-title">
            <header className="section-header">
              <div>
                <p className="eyebrow">手工入口</p>
                <h2 id="operations-actions-title">运维操作</h2>
              </div>
            </header>

            <div className="operations-action-grid">
              <article className="operations-action">
                <div className="operations-action__header">
                  <h3>手工测试</h3>
                  <span className="operations-badge operations-badge--test">测试</span>
                </div>
                <p>{scenarioText(settings)}</p>
                {overview.real_event_priority_active ? (
                  <p className="operations-warning">真实事件优先，暂停手工测试</p>
                ) : null}
                <button
                  className="primary-button"
                  type="button"
                  disabled={overview.real_event_priority_active || triggerBusy !== null}
                  onClick={() => startTrigger("manual_test")}
                >
                  {triggerBusy === "manual_test" ? "触发中" : "手工测试"}
                </button>
              </article>

              <article className="operations-action">
                <div className="operations-action__header">
                  <h3>演练测试</h3>
                  <span className="operations-badge operations-badge--drill">
                    演练标识：不可移除
                  </span>
                </div>
                <p>触发带演练标识的全链路测试，运行与审计单独记录。</p>
                <button
                  className="primary-button"
                  type="button"
                  disabled={triggerBusy !== null}
                  onClick={() => startTrigger("drill")}
                >
                  {triggerBusy === "drill" ? "触发中" : "演练测试"}
                </button>
              </article>

              <article className="operations-action">
                <div className="operations-action__header">
                  <h3>手工正式事件</h3>
                  <span className="operations-badge operations-badge--real">正式</span>
                </div>
                <p>进入现有正式事件入口，填写坐标和来源后启动真实评估。</p>
                <Link className="primary-button operations-link" to="/manual">
                  手工正式事件
                </Link>
              </article>
            </div>

            {pendingTrigger && !triggerBusy ? (
              <button
                className="text-button"
                type="button"
                onClick={() => void executeTrigger(pendingTrigger)}
              >
                重试上次请求
              </button>
            ) : null}
            {triggerNotice ? (
              <p
                className={triggerNotice.kind === "success" ? "form-success" : "form-error"}
                role="status"
              >
                {triggerNotice.text}
              </p>
            ) : null}
          </section>

          <section className="operations-runs" aria-labelledby="operations-runs-title">
            <header className="section-header">
              <div>
                <p className="eyebrow">运行记录</p>
                <h2 id="operations-runs-title">最近运行</h2>
              </div>
            </header>
            {overview.recent_runs.length > 0 ? (
              <div className="table-scroll">
                <table className="operations-table">
                  <thead>
                    <tr>
                      <th scope="col">模式</th>
                      <th scope="col">状态</th>
                      <th scope="col">触发人</th>
                      <th scope="col">开始时间</th>
                      {userRole === "superadmin" ? <th scope="col">操作</th> : null}
                    </tr>
                  </thead>
                  <tbody>
                    {overview.recent_runs.map((run) => (
                      <tr key={run.id}>
                        <td>
                          <span className="operations-run-mode">
                            {triggerModeLabel(run.trigger_mode)}
                          </span>
                          <span className="mono">{run.trigger_mode}</span>
                        </td>
                        <td>
                          <span className={`operations-run-state operations-run-state--${run.operation_state}`}>
                            {runStateLabel(run.operation_state)}
                          </span>
                        </td>
                        <td>{run.triggered_by}</td>
                        <td>{formatDateTime(run.started_at)}</td>
                        {userRole === "superadmin" ? (
                          <td>
                            {run.trigger_mode === "drill" &&
                            run.operation_state === "paused" &&
                            run.resume_required ? (
                              <button
                                className="text-button"
                                type="button"
                                disabled={resumingRunId !== null}
                                onClick={() => void resumeDrill(run.id)}
                              >
                                {resumingRunId === run.id ? "恢复中" : "恢复演练"}
                              </button>
                            ) : null}
                          </td>
                        ) : null}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="operations-empty">暂无最近运行记录</p>
            )}
          </section>

          {userRole === "superadmin" ? (
            <section
              className="operations-settings"
              aria-labelledby="operations-settings-title"
            >
              <header className="section-header">
                <div>
                  <p className="eyebrow">超级管理员</p>
                  <h2 id="operations-settings-title">运维设置</h2>
                </div>
              </header>
              <form onSubmit={handleSaveSettings}>
                <div className="operations-settings-grid">
                  <label htmlFor="daily_window_start">窗口开始</label>
                  <input
                    id="daily_window_start"
                    type="time"
                    value={settingsDraft?.daily_window_start ?? ""}
                    onChange={(event) =>
                      setSettingsDraft((current) =>
                        current
                          ? { ...current, daily_window_start: event.target.value }
                          : current,
                      )
                    }
                  />

                  <label htmlFor="daily_window_end">窗口结束</label>
                  <input
                    id="daily_window_end"
                    type="time"
                    value={settingsDraft?.daily_window_end ?? ""}
                    onChange={(event) =>
                      setSettingsDraft((current) =>
                        current
                          ? { ...current, daily_window_end: event.target.value }
                          : current,
                      )
                    }
                  />

                  <label htmlFor="test_magnitude_min">震级下限</label>
                  <input
                    id="test_magnitude_min"
                    type="number"
                    min="-2"
                    max="12"
                    step="0.1"
                    value={settingsDraft?.test_magnitude_min ?? ""}
                    onChange={(event) =>
                      setSettingsDraft((current) =>
                        current
                          ? { ...current, test_magnitude_min: event.target.value }
                          : current,
                      )
                    }
                  />

                  <label htmlFor="test_magnitude_max">震级上限</label>
                  <input
                    id="test_magnitude_max"
                    type="number"
                    min="-2"
                    max="12"
                    step="0.1"
                    value={settingsDraft?.test_magnitude_max ?? ""}
                    onChange={(event) =>
                      setSettingsDraft((current) =>
                        current
                          ? { ...current, test_magnitude_max: event.target.value }
                          : current,
                      )
                    }
                  />

                  <label htmlFor="test_depth_min_km">深度下限</label>
                  <input
                    id="test_depth_min_km"
                    type="number"
                    min="0"
                    max="1000"
                    step="0.01"
                    value={settingsDraft?.test_depth_min_km ?? ""}
                    onChange={(event) =>
                      setSettingsDraft((current) =>
                        current
                          ? { ...current, test_depth_min_km: event.target.value }
                          : current,
                      )
                    }
                  />

                  <label htmlFor="test_depth_max_km">深度上限</label>
                  <input
                    id="test_depth_max_km"
                    type="number"
                    min="0"
                    max="1000"
                    step="0.01"
                    value={settingsDraft?.test_depth_max_km ?? ""}
                    onChange={(event) =>
                      setSettingsDraft((current) =>
                        current
                          ? { ...current, test_depth_max_km: event.target.value }
                          : current,
                      )
                    }
                  />

                  <label htmlFor="drill_conflict_policy">冲突策略</label>
                  <select
                    id="drill_conflict_policy"
                    value={settingsDraft?.drill_conflict_policy ?? "pause"}
                    onChange={(event) =>
                      setSettingsDraft((current) =>
                        current
                          ? {
                              ...current,
                              drill_conflict_policy:
                                event.target.value as SettingsDraft["drill_conflict_policy"],
                            }
                          : current,
                      )
                    }
                  >
                    <option value="pause">暂停演练</option>
                    <option value="terminate">终止演练</option>
                  </select>
                </div>
                <div className="operations-settings-footer">
                  {settingsNotice ? (
                    <p
                      className={
                        settingsNotice.kind === "success" ? "form-success" : "form-error"
                      }
                      role="status"
                    >
                      {settingsNotice.text}
                    </p>
                  ) : null}
                  <button className="primary-button" type="submit" disabled={settingsSaving}>
                    {settingsSaving ? "保存中" : "保存设置"}
                  </button>
                </div>
              </form>
            </section>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
