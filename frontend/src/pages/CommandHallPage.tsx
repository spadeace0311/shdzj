import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  ApiError,
  getCommandHallActiveEvent,
  getCommandHallOverview,
  streamCommandHall,
} from "../api/client";
import { CommandHallDetailDrawer } from "../components/CommandHallDetailDrawer";
import { CommandHallGroupBoard } from "../components/CommandHallGroupBoard";
import {
  formatDateTime,
  formatEventKind,
  formatInstitutionalLevel,
  formatServiceLevel,
  type CommandHallGroup,
  type CommandHallOverview,
} from "../types";

const SEVERITY_ORDER: Record<string, number> = {
  critical: 3,
  warning: 2,
  info: 1,
};

const SEVERITY_LABELS: Record<string, string> = {
  critical: "严重",
  warning: "警告",
  info: "提示",
};

const PUBLICATION_MODE_LABELS: Record<string, string> = {
  automatic: "自动版",
  rebuild: "人工修订版",
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

function nullableText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return null;
}

function numberValue(value: unknown, fallback: number): number {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number(value)
        : Number.NaN;
  return Number.isFinite(parsed) ? parsed : fallback;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return "登录状态已失效，请重新登录";
    }
    if (error.status === 403) {
      return "当前账号无权查看该事件指挥大厅";
    }
    if (error.status === 404) {
      return "未找到该事件的指挥大厅数据";
    }
    return error.message;
  }
  return "无法加载指挥大厅";
}

function markerClass(kind: string | null): string {
  if (kind === "test" || kind === "drill") {
    return `event-marker event-marker--${kind}`;
  }
  return "event-marker event-marker--unknown";
}

function formatDuration(start: string, now = Date.now()): string {
  const startTime = new Date(start).getTime();
  if (!Number.isFinite(startTime)) {
    return "-";
  }
  const totalSeconds = Math.max(0, Math.floor((now - startTime) / 1000));
  const days = Math.floor(totalSeconds / 86_400);
  const hours = Math.floor((totalSeconds % 86_400) / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  if (days > 0) {
    return `${days}天 ${hours}小时 ${minutes}分钟`;
  }
  if (hours > 0) {
    return `${hours}小时 ${minutes}分钟`;
  }
  if (minutes > 0) {
    return `${minutes}分钟 ${seconds}秒`;
  }
  return `${seconds}秒`;
}

function sortAlerts(
  alerts: CommandHallOverview["alerts"],
): CommandHallOverview["alerts"] {
  return [...alerts].sort((left, right) => {
    const severity =
      (SEVERITY_ORDER[right.severity] ?? 0) -
      (SEVERITY_ORDER[left.severity] ?? 0);
    if (severity !== 0) {
      return severity;
    }
    return left.first_seen_at.localeCompare(right.first_seen_at);
  });
}

function eventRevision(overview: CommandHallOverview): string {
  const event = overview.event;
  const revision = asRecord(event.current_revision);
  if (!revision) {
    return "修订信息待同步";
  }
  return `R${textValue(revision.revision_no)} · ${formatEventKind(
    textValue(revision.revision_kind, ""),
  )}`;
}

function responseSummary(overview: CommandHallOverview): {
  institutional: string;
  service: string;
} {
  const event = overview.event;
  const suggestion = asRecord(event.response_suggestion);
  const institutional = nullableText(
    suggestion?.institutional_level ?? event.institutional_level,
  );
  const service = numberValue(
    suggestion?.service_level ?? event.service_level,
    Number.NaN,
  );
  return {
    institutional: formatInstitutionalLevel(institutional),
    service: formatServiceLevel(Number.isFinite(service) ? service : null),
  };
}

function keyArtifacts(overview: CommandHallOverview): Record<string, unknown>[] {
  return asRecords(overview.artifact_summary.latest_artifacts).slice(0, 6);
}

function publicationSourceLabel(
  artifact: Record<string, unknown>,
): string {
  if (
    artifact.is_forced === true ||
    artifact.publication_mode === "superadmin_override"
  ) {
    return PUBLICATION_MODE_LABELS.superadmin_override;
  }
  const mode = textValue(artifact.publication_mode, "");
  return PUBLICATION_MODE_LABELS[mode] ?? "版本来源待确认";
}

function alertTypeLabel(alert: CommandHallOverview["alerts"][number]): string {
  if (alert.alert_type === "projection.lag") {
    return "投影延迟";
  }
  return SEVERITY_LABELS[alert.severity] ?? alert.severity;
}

export function CommandHallPage() {
  const { eventId: routeEventId } = useParams<{ eventId: string }>();
  const [resolvedEventId, setResolvedEventId] = useState<string | null>(null);
  const [readyEventId, setReadyEventId] = useState<string | null>(null);
  const [overview, setOverview] = useState<CommandHallOverview | null>(null);
  const [status, setStatus] = useState<
    "loading" | "ready" | "empty" | "error"
  >("loading");
  const [error, setError] = useState("");
  const [syncNotice, setSyncNotice] = useState("");
  const [syncMode, setSyncMode] = useState<"connecting" | "sse" | "polling">(
    "connecting",
  );
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [selectedGroup, setSelectedGroup] = useState<CommandHallGroup | null>(
    null,
  );
  const [reloadKey, setReloadKey] = useState(0);
  const generationRef = useRef(0);
  const overviewRequestRef = useRef(0);
  const resolvedEventRef = useRef<string | null>(null);
  const readyEventRef = useRef<string | null>(null);

  async function refreshOverview(
    targetEventId: string,
    showLoading: boolean,
    generation = generationRef.current,
  ) {
    const requestId = ++overviewRequestRef.current;
    if (showLoading) {
      setStatus("loading");
    }
    try {
      const loaded = await getCommandHallOverview(targetEventId);
      if (
        requestId !== overviewRequestRef.current ||
        generation !== generationRef.current ||
        resolvedEventRef.current !== targetEventId
      ) {
        return;
      }
      setOverview(loaded);
      setReadyEventId(targetEventId);
      readyEventRef.current = targetEventId;
      setRefreshEpoch((current) => current + 1);
      setNow(Date.now());
      setStatus("ready");
      setError("");
      setSyncNotice("");
    } catch (caught) {
      if (
        requestId !== overviewRequestRef.current ||
        generation !== generationRef.current ||
        resolvedEventRef.current !== targetEventId
      ) {
        return;
      }
      if (showLoading) {
        setOverview(null);
        setReadyEventId(null);
        readyEventRef.current = null;
        setStatus("error");
        setError(errorMessage(caught));
      } else {
        setSyncNotice(errorMessage(caught));
      }
    }
  }

  useEffect(() => {
    const generation = ++generationRef.current;
    overviewRequestRef.current += 1;
    resolvedEventRef.current = null;
    readyEventRef.current = null;
    setResolvedEventId(null);
    setReadyEventId(null);
    setOverview(null);
    setSelectedGroup(null);
    setStatus("loading");
    setError("");
    setSyncNotice("");
    setSyncMode("connecting");
    let activePollingTimer: number | null = null;
    let activeRequestInFlight = false;

    async function resolveRouteEvent() {
      const targetEventId = routeEventId;
      if (!targetEventId) {
        return;
      }
      try {
        resolvedEventRef.current = targetEventId;
        setResolvedEventId(targetEventId);
        setSelectedGroup(null);
        await refreshOverview(targetEventId, true, generation);
      } catch (caught) {
        if (generation !== generationRef.current) {
          return;
        }
        setStatus("error");
        setError(errorMessage(caught));
      }
    }

    async function probeActiveEvent() {
      if (
        activeRequestInFlight ||
        generation !== generationRef.current
      ) {
        return;
      }
      activeRequestInFlight = true;
      try {
        const active = await getCommandHallActiveEvent();
        if (generation !== generationRef.current) {
          return;
        }
        const targetEventId = active.event_id;
        if (!targetEventId) {
          resolvedEventRef.current = null;
          readyEventRef.current = null;
          setResolvedEventId(null);
          setReadyEventId(null);
          setOverview(null);
          setSelectedGroup(null);
          setStatus("empty");
          setError("");
          setSyncNotice("");
          return;
        }
        if (
          targetEventId === resolvedEventRef.current &&
          targetEventId === readyEventRef.current
        ) {
          return;
        }
        resolvedEventRef.current = targetEventId;
        setResolvedEventId(targetEventId);
        setSelectedGroup(null);
        await refreshOverview(targetEventId, true, generation);
      } catch (caught) {
        if (generation !== generationRef.current) {
          return;
        }
        if (readyEventRef.current === null) {
          setStatus("error");
          setError(errorMessage(caught));
        } else {
          setSyncNotice(errorMessage(caught));
        }
      } finally {
        activeRequestInFlight = false;
      }
    }

    if (routeEventId) {
      void resolveRouteEvent();
    } else {
      void probeActiveEvent();
      activePollingTimer = globalThis.setInterval(() => {
        void probeActiveEvent();
      }, 5_000);
    }

    return () => {
      if (generationRef.current === generation) {
        generationRef.current += 1;
      }
      overviewRequestRef.current += 1;
      resolvedEventRef.current = null;
      readyEventRef.current = null;
      if (activePollingTimer !== null) {
        globalThis.clearInterval(activePollingTimer);
      }
    };
  }, [reloadKey, routeEventId]);

  useEffect(() => {
    if (status !== "ready") {
      return;
    }
    setNow(Date.now());
    const timer = globalThis.setInterval(() => {
      setNow(Date.now());
    }, 1_000);
    return () => globalThis.clearInterval(timer);
  }, [status]);

  useEffect(() => {
    if (
      !resolvedEventId ||
      readyEventId !== resolvedEventId ||
      status !== "ready"
    ) {
      return;
    }

    const targetEventId = resolvedEventId;
    const controller = new AbortController();
    let disposed = false;
    let pollingTimer: number | null = null;

    function startPolling() {
      if (disposed || pollingTimer !== null) {
        return;
      }
      setSyncMode("polling");
      pollingTimer = globalThis.setInterval(() => {
        void refreshOverview(targetEventId, false);
      }, 5_000);
    }

    setSyncMode("sse");
    void streamCommandHall(
      targetEventId,
      () => {
        if (!disposed) {
          setSyncMode("sse");
          void refreshOverview(targetEventId, false);
        }
      },
      controller.signal,
    )
      .then(() => {
        if (!disposed && !controller.signal.aborted) {
          startPolling();
        }
      })
      .catch(() => {
        if (!disposed && !controller.signal.aborted) {
          startPolling();
        }
      });

    return () => {
      disposed = true;
      controller.abort();
      if (pollingTimer !== null) {
        globalThis.clearInterval(pollingTimer);
      }
    };
  }, [readyEventId, resolvedEventId, status]);

  if (status === "loading") {
    return (
      <section className="command-hall-state" aria-labelledby="command-hall-title">
        <div className="state-panel" role="status">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载指挥大厅</p>
        </div>
      </section>
    );
  }

  if (status === "empty") {
    return (
      <section className="command-hall-state" aria-labelledby="command-hall-title">
        <h1 id="command-hall-title">应急指挥大厅</h1>
        <div className="state-panel">
          <p>当前没有活跃事件</p>
          <Link className="secondary-button" to="/">
            返回事件列表
          </Link>
        </div>
      </section>
    );
  }

  if (status === "error" || !overview) {
    return (
      <section className="command-hall-state" aria-labelledby="command-hall-title">
        <h1 id="command-hall-title">应急指挥大厅</h1>
        <div className="state-panel state-panel--error" role="alert">
          <p>{error || "无法加载指挥大厅"}</p>
          <div className="command-hall-state__actions">
            <button
              className="secondary-button"
              type="button"
              onClick={() => {
                const targetEventId =
                  resolvedEventRef.current ?? routeEventId ?? null;
                if (targetEventId) {
                  void refreshOverview(targetEventId, true);
                } else {
                  setReloadKey((current) => current + 1);
                }
              }}
            >
              重试
            </button>
            <Link className="text-button" to="/">
              返回事件列表
            </Link>
          </div>
        </div>
      </section>
    );
  }

  const event = overview.event;
  const eventKind =
    nullableText(event.event_kind) ?? nullableText(event.event_type);
  const response = responseSummary(overview);
  const counts = overview.task_counts;
  const alerts = sortAlerts(overview.alerts);
  const artifacts = keyArtifacts(overview);
  const originTime = textValue(event.origin_time, "");
  const t1At = textValue(event.t1_at, "");
  const currentRevision = asRecord(event.current_revision);
  const projectionSyncing = overview.sync_status === "syncing";

  return (
    <div className="command-hall-shell">
      <main className="command-hall" aria-labelledby="command-hall-title">
        <header className="command-hall__header">
          <div className="command-hall__identity">
            <p className="eyebrow">上海市地震应急辅助决策系统</p>
            <div className="command-hall__title-row">
              <h1 id="command-hall-title">
                {textValue(event.place, "应急指挥大厅")}
              </h1>
              <span
                className={markerClass(eventKind)}
                data-testid="command-hall-event-marker"
              >
                {formatEventKind(eventKind)}
              </span>
              <span className="command-hall__revision">
                {eventRevision(overview)}
              </span>
            </div>
            <p>
              {textValue(event.magnitude)} 级 · 深度{" "}
              {textValue(event.depth_km)} km · 东经{" "}
              {textValue(event.longitude)}° · 北纬{" "}
              {textValue(event.latitude)}°
            </p>
          </div>

          <div className="command-hall__header-status">
            <span
              className={`command-hall__projection-sync${
                projectionSyncing
                  ? " command-hall__projection-sync--syncing"
                  : ""
              }`}
              data-testid="command-hall-sync-status"
            >
              {projectionSyncing ? "数据同步中" : "数据已同步"}
            </span>
            <span
              className={`command-hall__sync command-hall__sync--${syncMode}`}
            >
              {syncMode === "polling"
                ? "5 秒轮询更新"
                : syncMode === "sse"
                  ? "实时更新"
                  : "正在连接实时更新"}
            </span>
            <span>投影版本 {overview.projection_version}</span>
            <span>更新于 {formatDateTime(overview.updated_at)}</span>
            <Link className="command-hall__exit" to="/">
              退出大屏
            </Link>
          </div>
        </header>

        {syncNotice ? (
          <div className="command-hall__notice" role="status">
            {syncNotice}
          </div>
        ) : null}

        <section
          className="command-hall__alerts"
          aria-labelledby="command-hall-alerts-title"
        >
          <header>
            <div>
              <p className="eyebrow">优先处置</p>
              <h2 id="command-hall-alerts-title">告警与异常</h2>
            </div>
            <strong>{alerts.length} 项未解决</strong>
          </header>
          {alerts.length === 0 ? (
            <p className="command-hall__alerts-empty">当前无告警或系统异常</p>
          ) : (
            <div className="command-hall__alert-list">
              {alerts.slice(0, 6).map((alert) => (
                <article
                  className={`command-hall__alert command-hall__alert--${alert.severity}`}
                  key={alert.id}
                >
                  <span>{alertTypeLabel(alert)}</span>
                  <strong>{alert.title}</strong>
                  <small>
                    {alert.workgroup_code ?? "全局"} ·{" "}
                    {formatDateTime(alert.first_seen_at)}
                  </small>
                </article>
              ))}
            </div>
          )}
        </section>

        <section
          className="command-hall__summary"
          aria-label="事件与任务摘要"
        >
          <article className="command-hall__clock">
            <span>主时钟</span>
            <dl>
              <div>
                <dt>T0 发震时刻</dt>
                <dd>{formatDateTime(originTime)}</dd>
              </div>
              <div>
                <dt>T1 正式速报</dt>
                <dd>{formatDateTime(t1At)}</dd>
              </div>
              <div>
                <dt>已运行</dt>
                <dd>{formatDuration(originTime, now)}</dd>
              </div>
            </dl>
          </article>

          <article className="command-hall__response">
            <span>响应建议</span>
            <dl>
              <div>
                <dt>制度响应建议</dt>
                <dd>{response.institutional}</dd>
              </div>
              <div>
                <dt>服务响应</dt>
                <dd>{response.service}</dd>
              </div>
              <div>
                <dt>当前修订</dt>
                <dd>
                  {currentRevision
                    ? `R${textValue(currentRevision.revision_no)}`
                    : "待同步"}
                </dd>
              </div>
            </dl>
          </article>

          <article className="command-hall__task-summary">
            <span>任务总览</span>
            <dl>
              <div>
                <dt>完成</dt>
                <dd>{counts.completed}</dd>
              </div>
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
              <div>
                <dt>双版本</dt>
                <dd>{overview.dual_version_count}</dd>
              </div>
            </dl>
          </article>
        </section>

        <section
          className="command-hall__artifacts"
          aria-labelledby="command-hall-artifacts-title"
        >
          <header>
            <div>
              <p className="eyebrow">成果摘要</p>
              <h2 id="command-hall-artifacts-title">关键当前发布版</h2>
            </div>
            <span>
              已发布 {numberValue(overview.artifact_summary.published_count, 0)}
            </span>
          </header>
          {artifacts.length === 0 ? (
            <p className="command-hall__artifacts-empty">
              暂无当前发布成果
            </p>
          ) : (
            <div className="command-hall__artifact-list">
              {artifacts.map((artifact) => {
                return (
                  <article
                    className="command-hall__artifact"
                    key={textValue(
                      artifact.publication_id ?? artifact.artifact_id,
                    )}
                  >
                    <strong>
                      {textValue(
                        artifact.file_name ?? artifact.artifact_key,
                        "未命名成果",
                      )}
                    </strong>
                    <span>
                      {publicationSourceLabel(artifact)} ·{" "}
                      {textValue(artifact.status)}
                    </span>
                    <small>
                      {textValue(artifact.artifact_key)} ·{" "}
                      {formatDateTime(
                        textValue(artifact.published_at, ""),
                      )}
                    </small>
                  </article>
                );
              })}
            </div>
          )}
        </section>

        <section
          className="command-hall__groups-section"
          aria-labelledby="command-hall-groups-title"
        >
          <header>
            <div>
              <p className="eyebrow">七组协同</p>
              <h2 id="command-hall-groups-title">工作组任务进度</h2>
            </div>
            <span>点击卡片查看组内任务，再点击任务查看公开成果版本</span>
          </header>
          <CommandHallGroupBoard
            eventId={overview.event_id}
            groups={overview.groups}
            onOpenGroup={setSelectedGroup}
          />
        </section>
      </main>

      {selectedGroup ? (
        <CommandHallDetailDrawer
          eventId={overview.event_id}
          eventKind={eventKind}
          groupCode={selectedGroup.workgroup_code}
          groupName={selectedGroup.name}
          refreshEpoch={refreshEpoch}
          onClose={() => setSelectedGroup(null)}
        />
      ) : null}
    </div>
  );
}
