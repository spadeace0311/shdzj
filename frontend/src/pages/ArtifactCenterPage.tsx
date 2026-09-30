import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import { useParams } from "react-router-dom";

import {
  ApiError,
  fetchArtifactBlob,
  getArtifactVersions,
  getAssessmentProduction,
  getCurrentAssessment,
  listEventArtifacts,
  listEvents,
  rebuildArtifact,
} from "../api/client";
import { ArtifactList } from "../components/ArtifactList";
import { ArtifactOverrideDialog } from "../components/ArtifactOverrideDialog";
import { ArtifactPreview } from "../components/ArtifactPreview";
import {
  buildArtifactProgressSummary,
  ArtifactProgressCard,
} from "../components/ArtifactProgressCard";
import {
  formatDateTime,
  matchesArtifactFilters,
  type ArtifactSummary,
  type EventSummary,
  type ProductionFilters,
  type ProductionRun,
} from "../types";

type PageStatus = "loading" | "ready" | "error";
type SelectorStatus = "loading" | "ready" | "error";

interface ArtifactCenterPageProps {
  eventId?: string;
  userRole: string;
  workgroup: string | null;
  loadMode?: "assessment-run" | "event";
}

const EMPTY_FILTERS: ProductionFilters = {
  kind: "all",
  status: "all",
  quality_grade: "all",
  publication_mode: "all",
};

const PRODUCTION_MODE_LABELS: Record<string, string> = {
  live: "实战",
  manual: "人工",
  test: "测试",
  drill: "演练",
  replay: "测试回放",
};

const PUBLICATION_MODE_LABELS: Record<string, string> = {
  automatic: "自动发布",
  rebuild: "重生成",
  superadmin_override: "强制覆盖",
};

function canRebuild(userRole: string, workgroup: string | null): boolean {
  if (userRole === "superadmin") {
    return true;
  }
  return (
    workgroup === "应急技术组" &&
    ["group_leader", "group_deputy", "group_member"].includes(userRole)
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "网络连接失败，请稍后重试";
}

export function ArtifactCenterPage({
  eventId,
  userRole,
  workgroup,
  loadMode = "assessment-run",
}: ArtifactCenterPageProps) {
  const [pageStatus, setPageStatus] = useState<PageStatus>("loading");
  const [selectorStatus, setSelectorStatus] =
    useState<SelectorStatus>("loading");
  const [events, setEvents] = useState<EventSummary[]>([]);
  const [run, setRun] = useState<ProductionRun | null>(null);
  const [publications, setPublications] = useState<ArtifactSummary[]>([]);
  const [versions, setVersions] = useState<ArtifactSummary[]>([]);
  const [revisionId, setRevisionId] = useState("");
  const [filters, setFilters] =
    useState<ProductionFilters>(EMPTY_FILTERS);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(
    null,
  );
  const [pageError, setPageError] = useState("");
  const [message, setMessage] = useState("");
  const [downloadingArtifactId, setDownloadingArtifactId] = useState<
    string | null
  >(null);
  const [rebuildTarget, setRebuildTarget] = useState<ArtifactSummary | null>(
    null,
  );
  const [rebuildReason, setRebuildReason] = useState("");
  const [rebuildError, setRebuildError] = useState("");
  const [rebuildSubmitting, setRebuildSubmitting] = useState(false);
  const [overrideTarget, setOverrideTarget] = useState<ArtifactSummary | null>(
    null,
  );

  const rebuildAllowed = canRebuild(userRole, workgroup);
  const overrideAllowed = userRole === "superadmin";
  const usesEventLoad = loadMode === "event";

  const loadAssessmentRun = useCallback(async (assessmentRunId: string) => {
    setPageStatus("loading");
    setPageError("");
    try {
      const production = await getAssessmentProduction(assessmentRunId);
      setRun(production);
      setPublications(production.artifacts ?? []);
      setVersions([]);
      setRevisionId("");
      setSelectedArtifactId(null);
      setPageStatus("ready");
    } catch (caught) {
      setRun(null);
      setPublications([]);
      setVersions([]);
      setPageError(errorMessage(caught));
      setPageStatus("error");
    }
  }, []);

  const loadEventData = useCallback(async (selectedEventId: string) => {
    setPageStatus("loading");
    setPageError("");
    try {
      const assessment = await getCurrentAssessment(selectedEventId);
      let latestRun: ProductionRun | null = null;
      if (assessment) {
        try {
          latestRun = await getAssessmentProduction(assessment.run_id);
        } catch {
          latestRun = null;
        }
      }

      const [publishedArtifacts, versionArtifacts] = await Promise.all([
        listEventArtifacts(selectedEventId, EMPTY_FILTERS),
        getArtifactVersions(selectedEventId),
      ]);

      setRun(latestRun);
      setPublications(publishedArtifacts);
      setVersions(versionArtifacts);
      setRevisionId(assessment?.revision_id ?? "");
      setSelectedArtifactId(null);
      setPageStatus("ready");
    } catch (caught) {
      setRun(null);
      setPublications([]);
      setVersions([]);
      setPageError(errorMessage(caught));
      setPageStatus("error");
    }
  }, []);

  const loadData = useCallback(() => {
    if (!eventId) {
      return;
    }
    if (usesEventLoad) {
      void loadEventData(eventId);
      return;
    }
    void loadAssessmentRun(eventId);
  }, [eventId, loadAssessmentRun, loadEventData, usesEventLoad]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    if (eventId) {
      return;
    }
    let active = true;
    setSelectorStatus("loading");
    void listEvents()
      .then((eventList) => {
        if (active) {
          setEvents(eventList);
          setSelectorStatus("ready");
        }
      })
      .catch(() => {
        if (active) {
          setSelectorStatus("error");
        }
      });
    return () => {
      active = false;
    };
  }, [eventId]);

  const visiblePublications = useMemo(
    () =>
      publications.filter((artifact) =>
        matchesArtifactFilters(artifact, filters),
      ),
    [filters, publications],
  );
  const visibleVersions = useMemo(
    () =>
      versions.filter((artifact) =>
        matchesArtifactFilters(artifact, filters),
      ),
    [filters, versions],
  );
  const selectedArtifact =
    publications.find(
      (artifact) => artifact.artifact_id === selectedArtifactId,
    ) ??
    versions.find((artifact) => artifact.artifact_id === selectedArtifactId) ??
    null;
  const progress = useMemo(
    () => buildArtifactProgressSummary(run),
    [run],
  );

  function updateFilter(
    field: keyof ProductionFilters,
    value: ProductionFilters[typeof field],
  ) {
    setFilters((current) => ({ ...current, [field]: value }));
    setSelectedArtifactId(null);
  }

  async function handleDownload(artifact: ArtifactSummary) {
    setDownloadingArtifactId(artifact.artifact_id);
    setMessage("");
    try {
      const blob = await fetchArtifactBlob(artifact.artifact_id, "download");
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifact.file_name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      globalThis.setTimeout(() => URL.revokeObjectURL(url), 0);
      setMessage("下载已开始");
    } catch (caught) {
      setPageError(errorMessage(caught));
    } finally {
      setDownloadingArtifactId(null);
    }
  }

  async function handleRebuildSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!eventId || !rebuildTarget) {
      return;
    }
    if (!rebuildReason.trim()) {
      setRebuildError("请填写重新生成原因");
      return;
    }

    setRebuildError("");
    setRebuildSubmitting(true);
    try {
      await rebuildArtifact(
        eventId,
        rebuildTarget.artifact_key,
        rebuildTarget.output_profile,
        rebuildReason.trim(),
      );
      setRebuildTarget(null);
      setRebuildReason("");
      setMessage("重新生成已启动");
    } catch (caught) {
      setRebuildError(errorMessage(caught));
    } finally {
      setRebuildSubmitting(false);
    }
  }

  return (
    <section className="page-section" aria-labelledby="artifact-center-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">成果中心</p>
          <h1 id="artifact-center-title">地震应急成果</h1>
        </div>
        <div className="artifact-center__heading-actions">
          {eventId ? (
            <a className="text-button" href="/artifacts">
              选择事件
            </a>
          ) : null}
          {eventId && overrideAllowed ? (
            <button
              className="primary-button"
              type="button"
              disabled={!selectedArtifact || pageStatus !== "ready"}
              onClick={() => {
                if (selectedArtifact) {
                  setOverrideTarget(selectedArtifact);
                }
              }}
            >
              强制覆盖
            </button>
          ) : null}
        </div>
      </header>

      {!eventId ? (
        <section className="artifact-selector" aria-labelledby="artifact-selector-title">
          <header className="section-header">
            <div>
              <p className="eyebrow">事件选择</p>
              <h2 id="artifact-selector-title">选择事件查看成果</h2>
            </div>
          </header>

          {selectorStatus === "loading" ? (
            <div className="state-panel">
              <span className="state-icon" aria-hidden="true" />
              <p>正在加载事件列表</p>
            </div>
          ) : null}

          {selectorStatus === "error" ? (
            <div className="state-panel state-panel--error" role="alert">
              <p>无法加载事件列表</p>
            </div>
          ) : null}

          {selectorStatus === "ready" ? (
            events.length > 0 ? (
              <div className="artifact-selector__list">
                {events.map((event) => (
                  <a
                    className="data-asset-selector-item"
                    key={event.id}
                    href={`/artifacts/${event.id}`}
                  >
                    <strong>{event.place}</strong>
                    <span className="mono">{event.id}</span>
                    <span>{formatDateTime(event.origin_time)}</span>
                  </a>
                ))}
              </div>
            ) : (
              <div className="state-panel">
                <p>暂无地震事件</p>
              </div>
            )
          ) : null}
        </section>
      ) : null}

      {eventId ? (
        <>
          {progress ? <ArtifactProgressCard production={progress} /> : null}

          {pageStatus === "loading" ? (
            <div className="state-panel">
              <span className="state-icon" aria-hidden="true" />
              <p>正在加载成果</p>
            </div>
          ) : null}

          {pageStatus === "error" ? (
            <div className="state-panel state-panel--error" role="alert">
              <p>{pageError || "无法加载成果"}</p>
              <button
                className="secondary-button"
                type="button"
                onClick={loadData}
              >
                重试
              </button>
            </div>
          ) : null}

          {pageStatus === "ready" ? (
            <div className="artifact-center">
              <div className="artifact-mode-bar" aria-label="生产模式状态">
                <span>生产模式</span>
                <strong>
                  {PRODUCTION_MODE_LABELS[run?.production_mode ?? ""] ??
                    run?.production_mode ??
                    "未知"}
                </strong>
                {run?.marker ? <span>{run.marker}</span> : null}
                <span>完整运行：{run?.generation_scope ?? "暂无"}</span>
              </div>

              <div className="filter-bar artifact-filters">
                <label className="filter-field">
                  <span>成果类型</span>
                  <select
                    value={filters.kind}
                    onChange={(event) =>
                      updateFilter(
                        "kind",
                        event.target.value as ProductionFilters["kind"],
                      )
                    }
                  >
                    <option value="all">全部</option>
                    <option value="map">图件</option>
                    <option value="background">背景文档</option>
                    <option value="core">核心文档</option>
                  </select>
                </label>
                <label className="filter-field">
                  <span>状态</span>
                  <select
                    value={filters.status}
                    onChange={(event) =>
                      updateFilter(
                        "status",
                        event.target.value as ProductionFilters["status"],
                      )
                    }
                  >
                    <option value="all">全部</option>
                    <option value="complete">完整</option>
                    <option value="degraded">降级</option>
                    <option value="failed">失败</option>
                  </select>
                </label>
                <label className="filter-field">
                  <span>质量</span>
                  <select
                    value={filters.quality_grade}
                    onChange={(event) =>
                      updateFilter("quality_grade", event.target.value)
                    }
                  >
                    <option value="all">全部</option>
                    <option value="A">A</option>
                    <option value="B">B</option>
                    <option value="C">C</option>
                    <option value="background">背景</option>
                    <option value="core">核心</option>
                  </select>
                </label>
                <label className="filter-field">
                  <span>发布方式</span>
                  <select
                    value={filters.publication_mode}
                    onChange={(event) =>
                      updateFilter(
                        "publication_mode",
                        event.target.value as ProductionFilters["publication_mode"],
                      )
                    }
                  >
                    <option value="all">全部</option>
                    <option value="automatic">自动发布</option>
                    <option value="rebuild">重生成</option>
                    <option value="superadmin_override">强制覆盖</option>
                  </select>
                </label>
              </div>

              {message ? (
                <p className="form-success" role="status">
                  {message}
                </p>
              ) : null}

              <div className="artifact-center__layout">
                <section
                  className="artifact-center__list"
                  aria-labelledby="current-publications-title"
                >
                  <header className="section-header">
                    <div>
                      <p className="eyebrow">当前发布</p>
                      <h2 id="current-publications-title">当前成果</h2>
                    </div>
                  </header>
                  <ArtifactList
                    artifacts={visiblePublications}
                    selectedArtifactId={selectedArtifactId}
                    canRebuild={rebuildAllowed}
                    onSelect={(artifact) =>
                      setSelectedArtifactId(artifact.artifact_id)
                    }
                    onDownload={(artifact) => void handleDownload(artifact)}
                    onRebuild={(artifact) => {
                      setRebuildTarget(artifact);
                      setRebuildReason("");
                      setRebuildError("");
                    }}
                  />
                </section>

                <ArtifactPreview
                  artifact={selectedArtifact}
                  onDownload={(artifact) => void handleDownload(artifact)}
                />
              </div>

              <section
                className="artifact-versions"
                aria-labelledby="artifact-versions-title"
              >
                <header className="section-header">
                  <div>
                    <p className="eyebrow">版本档案</p>
                    <h2 id="artifact-versions-title">历史版本</h2>
                  </div>
                </header>
                {visibleVersions.length > 0 ? (
                  <div className="table-scroll">
                    <table className="event-table artifact-version-table">
                      <thead>
                        <tr>
                          <th>成果</th>
                          <th>版本</th>
                          <th>状态</th>
                          <th>质量</th>
                          <th>发布方式</th>
                          <th>生成时间</th>
                          <th>操作</th>
                        </tr>
                      </thead>
                      <tbody>
                        {visibleVersions.map((artifact) => (
                          <tr key={artifact.artifact_id}>
                            <td>
                              <button
                                className="text-button"
                                type="button"
                                onClick={() =>
                                  setSelectedArtifactId(artifact.artifact_id)
                                }
                              >
                                {artifact.display_name}
                              </button>
                            </td>
                            <td>V{artifact.artifact_version}</td>
                            <td>{artifact.status}</td>
                            <td>{artifact.quality_grade || "-"}</td>
                            <td>
                              {PUBLICATION_MODE_LABELS[
                                artifact.publication_mode
                              ] ?? artifact.publication_mode}
                            </td>
                            <td>{formatDateTime(artifact.generated_at)}</td>
                            <td>
                              <button
                                className="text-button"
                                type="button"
                                disabled={downloadingArtifactId === artifact.artifact_id}
                                onClick={() => void handleDownload(artifact)}
                              >
                                下载
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="artifact-list__empty">暂无历史版本</p>
                )}
              </section>
            </div>
          ) : null}
        </>
      ) : null}

      {rebuildTarget ? (
        <div className="data-asset-dialog-backdrop">
          <section
            className="data-asset-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="artifact-rebuild-dialog-title"
          >
            <header>
              <p className="eyebrow">成果重生成</p>
              <h2 id="artifact-rebuild-dialog-title">
                重新生成 {rebuildTarget.display_name}
              </h2>
            </header>
            <form onSubmit={handleRebuildSubmit}>
              <p>
                将创建单独重生成运行，当前完整运行进度保持不变。
              </p>
              <label htmlFor="artifact-rebuild-reason">重新生成原因</label>
              <textarea
                id="artifact-rebuild-reason"
                value={rebuildReason}
                onChange={(event) => setRebuildReason(event.target.value)}
              />
              {rebuildError ? (
                <p className="form-error" role="alert">
                  {rebuildError}
                </p>
              ) : null}
              <div className="data-asset-dialog-actions">
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => setRebuildTarget(null)}
                  disabled={rebuildSubmitting}
                >
                  取消
                </button>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={!rebuildReason.trim() || rebuildSubmitting}
                >
                  {rebuildSubmitting ? "提交中" : "确认重新生成"}
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}

      {overrideTarget ? (
        <ArtifactOverrideDialog
          eventId={eventId ?? ""}
          artifact={overrideTarget}
          revisionId={revisionId}
          onClose={() => setOverrideTarget(null)}
          onSuccess={() => {
            setOverrideTarget(null);
            loadData();
          }}
        />
      ) : null}
    </section>
  );
}

export function ArtifactCenterRoute({
  userRole,
  workgroup,
}: Pick<ArtifactCenterPageProps, "userRole" | "workgroup">) {
  const { eventId } = useParams();
  return (
    <ArtifactCenterPage
      eventId={eventId}
      userRole={userRole}
      workgroup={workgroup}
      loadMode="event"
    />
  );
}
