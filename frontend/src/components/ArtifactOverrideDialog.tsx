import { useState, type FormEvent } from "react";

import { ApiError, overrideArtifact } from "../api/client";
import type { ArtifactSummary } from "../types";

interface ArtifactOverrideDialogProps {
  eventId: string;
  artifact: ArtifactSummary | null;
  revisionId: string;
  onClose: () => void;
  onSuccess: () => void;
}

export function newIdempotencyKey(): string {
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

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "覆盖提交失败，请稍后重试";
}

export function ArtifactOverrideDialog({
  eventId,
  artifact,
  revisionId,
  onClose,
  onSuccess,
}: ArtifactOverrideDialogProps) {
  const [file, setFile] = useState<File | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!artifact) {
      return;
    }
    if (!file) {
      setError("请选择要覆盖的文件");
      return;
    }
    if (!reason.trim()) {
      setError("请填写覆盖原因");
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      await overrideArtifact(eventId, artifact.artifact_key, {
        file,
        revision_id: revisionId,
        reason: reason.trim(),
        ...(artifact.artifact_id
          ? { expected_current_artifact_id: artifact.artifact_id }
          : {}),
        idempotency_key: newIdempotencyKey(),
      });
      onSuccess();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSubmitting(false);
    }
  }

  if (!artifact) {
    return null;
  }

  return (
    <div className="data-asset-dialog-backdrop">
      <section
        className="data-asset-dialog artifact-override-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="artifact-override-dialog-title"
      >
        <header>
          <p className="eyebrow">超级管理员覆盖</p>
          <h2 id="artifact-override-dialog-title">
            覆盖 {artifact.display_name}
          </h2>
        </header>

        <form onSubmit={handleSubmit}>
          <p>
            当前成果版本 V{artifact.artifact_version}，覆盖后将以新版本发布。
          </p>
          <label htmlFor="artifact-override-file">替换文件</label>
          <input
            id="artifact-override-file"
            type="file"
            accept=".jpg,.jpeg,.png,.webp,.docx,.pptx"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <label htmlFor="artifact-override-reason">覆盖原因</label>
          <textarea
            id="artifact-override-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          {error ? (
            <p className="form-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className="data-asset-dialog-actions">
            <button
              className="secondary-button"
              type="button"
              onClick={onClose}
              disabled={submitting}
            >
              取消
            </button>
            <button
              className="primary-button"
              type="submit"
              disabled={!file || !reason.trim() || submitting}
            >
              {submitting ? "提交中" : "确认覆盖"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
