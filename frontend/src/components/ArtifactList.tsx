import {
  artifactGroup,
  type ArtifactGroup,
  type ArtifactPublicationMode,
  type ArtifactStatus,
  type ArtifactSummary,
} from "../types";

interface ArtifactListProps {
  artifacts: ArtifactSummary[];
  selectedArtifactId: string | null;
  canRebuild: boolean;
  onSelect: (artifact: ArtifactSummary) => void;
  onDownload: (artifact: ArtifactSummary) => void;
  onRebuild: (artifact: ArtifactSummary) => void;
}

const GROUP_ORDER: ArtifactGroup[] = ["map", "background", "core"];

const GROUP_LABELS: Record<ArtifactGroup, string> = {
  map: "图件",
  background: "背景文档",
  core: "核心文档",
};

const STATUS_LABELS: Record<ArtifactStatus, string> = {
  complete: "完整",
  degraded: "降级",
  failed: "失败",
};

const PUBLICATION_LABELS: Record<ArtifactPublicationMode, string> = {
  automatic: "自动发布",
  rebuild: "重生成",
  superadmin_override: "强制覆盖",
};

function groupedArtifacts(
  artifacts: ArtifactSummary[],
): Array<{ group: ArtifactGroup; items: ArtifactSummary[] }> {
  return GROUP_ORDER.map((group) => ({
    group,
    items: artifacts.filter(
      (artifact) => artifactGroup(artifact.artifact_key) === group,
    ),
  })).filter((entry) => entry.items.length > 0);
}

export function ArtifactList({
  artifacts,
  selectedArtifactId,
  canRebuild,
  onSelect,
  onDownload,
  onRebuild,
}: ArtifactListProps) {
  if (artifacts.length === 0) {
    return <p className="artifact-list__empty">暂无符合条件的成果</p>;
  }

  return (
    <div className="artifact-list">
      {groupedArtifacts(artifacts).map(({ group, items }) => (
        <section
          className="artifact-group"
          key={group}
          aria-labelledby={`artifact-group-${group}`}
        >
          <header className="artifact-group__header">
            <h2 id={`artifact-group-${group}`}>{GROUP_LABELS[group]}</h2>
            <span>{items.length} 项</span>
          </header>
          <div className="artifact-rows">
            {items.map((artifact) => {
              const selected = artifact.artifact_id === selectedArtifactId;
              return (
                <article
                  className={`artifact-row${
                    selected ? " artifact-row--selected" : ""
                  }`}
                  key={artifact.artifact_id}
                >
                  <button
                    className="artifact-row__name"
                    type="button"
                    onClick={() => onSelect(artifact)}
                  >
                    <span>{artifact.display_name}</span>
                    <span className="artifact-row__key">{artifact.artifact_key}</span>
                  </button>
                  <span className="artifact-row__version">
                    V{artifact.artifact_version}
                  </span>
                  <span
                    className={`artifact-status artifact-status--${artifact.status}`}
                  >
                    {STATUS_LABELS[artifact.status]}
                  </span>
                  <span className="artifact-row__quality">
                    {artifact.quality_grade || "-"}
                  </span>
                  <span className="artifact-row__publication">
                    {PUBLICATION_LABELS[artifact.publication_mode] ??
                      artifact.publication_mode}
                  </span>
                  {artifact.needs_review ? (
                    <span className="artifact-review-badge">需复核</span>
                  ) : (
                    <span aria-hidden="true" />
                  )}
                  <div className="artifact-row__actions">
                    <button
                      className="secondary-button artifact-row__action"
                      type="button"
                      onClick={() => onDownload(artifact)}
                    >
                      下载原文件
                    </button>
                    {canRebuild ? (
                      <button
                        className="secondary-button artifact-row__action"
                        type="button"
                        onClick={() => onRebuild(artifact)}
                      >
                        重新生成
                      </button>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
