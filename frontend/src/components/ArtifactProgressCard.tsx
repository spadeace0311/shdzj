import {
  artifactGroup,
  type ArtifactProgressSummary,
  type ProductionRun,
} from "../types";

interface ArtifactProgressCardProps {
  production: ArtifactProgressSummary;
}

const RUN_STATUS_LABELS: Record<string, string> = {
  pending: "待执行",
  running: "执行中",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  canceled: "已取消",
};

export function buildArtifactProgressSummary(
  run: ProductionRun | null,
): ArtifactProgressSummary | null {
  if (!run) {
    return null;
  }

  const artifacts = Array.isArray(run.artifacts) ? run.artifacts : [];
  const mapCount = artifacts.filter(
    (artifact) => artifactGroup(artifact.artifact_key) === "map",
  ).length;
  const backgroundDocumentCount = artifacts.filter(
    (artifact) => artifactGroup(artifact.artifact_key) === "background",
  ).length;
  const coreDocumentCount = artifacts.filter(
    (artifact) => artifactGroup(artifact.artifact_key) === "core",
  ).length;
  const requiredOutputCount = run.required_output_count || artifacts.length;
  const completeCount = run.complete_count || 0;
  const degradedCount = run.degraded_count || 0;
  const acceptedCount = completeCount + degradedCount;

  return {
    productionRunId: run.production_run_id || "",
    status: run.status || "",
    completeCount,
    degradedCount,
    failedCount: run.failed_count || 0,
    timeoutCount: run.timeout_count || 0,
    needsReviewCount: run.needs_review_count || 0,
    requiredOutputCount,
    mapCount,
    backgroundDocumentCount,
    coreDocumentCount,
    progressText: `${acceptedCount}/${requiredOutputCount}`,
  };
}

export function ArtifactProgressCard({
  production,
}: ArtifactProgressCardProps) {
  return (
    <section
      className="artifact-progress"
      aria-labelledby="artifact-progress-title"
    >
      <header className="artifact-progress__header">
        <div>
          <p className="eyebrow">成果生产</p>
          <h2 id="artifact-progress-title">当前完整运行</h2>
        </div>
        <span
          className={`artifact-run-status artifact-run-status--${production.status}`}
        >
          {RUN_STATUS_LABELS[production.status] ?? production.status}
        </span>
      </header>

      <div className="artifact-progress__overview">
        <div className="artifact-progress__total">
          <span>产出进度</span>
          <strong>{production.progressText}</strong>
        </div>
        <dl className="artifact-progress__counts">
          <div>
            <dt>图件</dt>
            <dd>{production.mapCount}</dd>
          </div>
          <div>
            <dt>背景文档</dt>
            <dd>{production.backgroundDocumentCount}</dd>
          </div>
          <div>
            <dt>核心文档</dt>
            <dd>{production.coreDocumentCount}</dd>
          </div>
        </dl>
      </div>

      <div className="artifact-progress__issues">
        <span>降级 {production.degradedCount}</span>
        <span>失败 {production.failedCount}</span>
        <span>超时 {production.timeoutCount}</span>
        <span>需复核 {production.needsReviewCount}</span>
      </div>
    </section>
  );
}
