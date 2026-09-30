import { useEffect, useState } from "react";

import { fetchArtifactBlob } from "../api/client";
import type {
  ArtifactPreviewState,
  ArtifactSummary,
} from "../types";

interface ArtifactPreviewProps {
  artifact: ArtifactSummary | null;
  onDownload: (artifact: ArtifactSummary) => void;
}

export function ArtifactPreview({
  artifact,
  onDownload,
}: ArtifactPreviewProps) {
  const [preview, setPreview] = useState<ArtifactPreviewState>({
    status: "idle",
  });

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;

    setPreview({ status: "idle" });
    if (!artifact?.thumbnail_url) {
      return () => {
        active = false;
      };
    }

    setPreview({ status: "loading" });
    void fetchArtifactBlob(artifact.artifact_id, "thumbnail")
      .then((blob) => {
        if (!active) {
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setPreview({ status: "ready", url: objectUrl });
      })
      .catch(() => {
        if (active) {
          setPreview({
            status: "error",
            message: "无法加载成果预览",
          });
        }
      });

    return () => {
      active = false;
      if (objectUrl) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [artifact?.artifact_id, artifact?.thumbnail_url]);

  if (!artifact) {
    return (
      <section className="artifact-preview" aria-label="成果预览">
        <p className="artifact-preview__empty">请选择成果查看详情</p>
      </section>
    );
  }

  const isOfficeDocument =
    artifact.format === "docx" || artifact.format === "pptx";
  const showDocumentFallback = isOfficeDocument && !artifact.thumbnail_url;

  return (
    <section className="artifact-preview" aria-labelledby="artifact-preview-title">
      <header className="artifact-preview__header">
        <div>
          <p className="eyebrow">成果预览</p>
          <h2 id="artifact-preview-title">{artifact.display_name}</h2>
        </div>
        {artifact.needs_review ? (
          <span className="artifact-review-badge">需复核</span>
        ) : null}
      </header>

      <dl className="artifact-preview__facts">
        <div>
          <dt>文件</dt>
          <dd>{artifact.file_name}</dd>
        </div>
        <div>
          <dt>版本</dt>
          <dd>V{artifact.artifact_version}</dd>
        </div>
        <div>
          <dt>格式</dt>
          <dd>{artifact.format}</dd>
        </div>
        <div>
          <dt>质量</dt>
          <dd>{artifact.quality_grade || "-"}</dd>
        </div>
      </dl>

      {artifact.status === "failed" ? (
        <div className="artifact-preview__error" role="status">
          该成果生成失败，无可用预览
        </div>
      ) : null}

      {artifact.status !== "failed" && showDocumentFallback ? (
        <div className="artifact-preview__document">
          <span>{artifact.format.toUpperCase()}</span>
          <p>文档成果使用下载查看，不在浏览器中解压 Office 文件。</p>
        </div>
      ) : null}

      {artifact.status !== "failed" &&
      artifact.thumbnail_url &&
      preview.status === "loading" ? (
        <div className="artifact-preview__loading">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载预览</p>
        </div>
      ) : null}

      {artifact.status !== "failed" &&
      artifact.thumbnail_url &&
      preview.status === "ready" ? (
        <div className="artifact-preview__image-frame">
          <img
            className="artifact-preview__image"
            src={preview.url}
            alt={artifact.display_name}
          />
        </div>
      ) : null}

      {artifact.status !== "failed" &&
      artifact.thumbnail_url &&
      preview.status === "error" ? (
        <div className="artifact-preview__error" role="status">
          {preview.message}
        </div>
      ) : null}

      <div className="artifact-preview__actions">
        <button
          className="primary-button"
          type="button"
          onClick={() => onDownload(artifact)}
        >
          下载原文件
        </button>
      </div>
    </section>
  );
}
