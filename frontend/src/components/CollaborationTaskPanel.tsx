import { useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  completeCollaborationTask,
  getAccessToken,
  getCommandHallTask,
  newIdempotencyKey,
  returnCollaborationTask,
  startCollaborationTask,
  submitCollaborationTask,
} from "../api/client";
import {
  formatDateTime,
  type CommandHallTaskDetail,
  type WorkgroupTask,
} from "../types";

interface DeliverableVersionRecord {
  id: string;
  deliverable_id: string;
  version_no: number;
  source_kind: string;
  artifact_id: string | null;
  artifact_publication_id: string | null;
  storage_key: string | null;
  file_name: string | null;
  checksum: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  text_result: Record<string, unknown> | null;
  created_by: string | null;
  basis_text: string | null;
  supersedes_version_id: string | null;
  created_at: string;
}

interface DeliverablePublicationRecord {
  id: string;
  deliverable_id: string;
  version_id: string;
  published_by: string;
  published_role: string;
  published_at: string;
  superseded_at: string | null;
  publication_note: string | null;
}

interface DeliverableRecord {
  id: string;
  task_id: string;
  deliverable_code: string;
  title: string;
  is_required: boolean;
  requirement_kind: string;
  artifact_binding: Record<string, unknown> | null;
  display_order: number;
  current_publication: DeliverablePublicationRecord | null;
  versions: DeliverableVersionRecord[];
}

export interface CollaborationTaskPanelProps {
  task: WorkgroupTask;
  canWork: boolean;
  canConfirm: boolean;
  eventKind: string | null;
  onRefresh: () => Promise<void>;
}

const SOURCE_LABELS: Record<string, string> = {
  automatic: "系统自动版",
  manual: "人工修订版",
  superadmin_override: "超级管理员覆盖版",
};

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

const REQUIREMENT_LABELS: Record<string, string> = {
  automatic_artifact: "系统自动成果",
  manual_file: "人工文件",
  manual_text: "文字结果",
  manual_file_or_text: "文件或文字结果",
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

function asDeliverables(detail: CommandHallTaskDetail): DeliverableRecord[] {
  return detail.deliverables as unknown as DeliverableRecord[];
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

async function responseError(response: Response): Promise<ApiError> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string") {
      return new ApiError(payload.detail, response.status);
    }
  } catch {
    // Fall through to a stable message for non-JSON responses.
  }
  return new ApiError(`请求失败（HTTP ${response.status}）`, response.status);
}

async function uploadCandidateVersion(
  deliverableId: string,
  file: File,
  basisText: string,
  idempotencyKey: string,
): Promise<DeliverableVersionRecord> {
  const token = getAccessToken();
  if (!token) {
    throw new ApiError("请先登录后上传候选成果", 401);
  }
  const body = new FormData();
  body.set("file", file);
  if (file.type) {
    body.set("mime_type", file.type);
  }
  if (basisText.trim()) {
    body.set("basis_text", basisText.trim());
  }
  const response = await fetch(
    `/api/v1/collaboration/deliverables/${encodeURIComponent(
      deliverableId,
    )}/versions/file`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Idempotency-Key": idempotencyKey,
      },
      body,
    },
  );
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as DeliverableVersionRecord;
}

async function publishCandidateVersion(
  deliverableId: string,
  versionId: string,
  taskVersion: number,
  publicationNote: string,
  idempotencyKey: string,
): Promise<DeliverablePublicationRecord> {
  const token = getAccessToken();
  if (!token) {
    throw new ApiError("请先登录后确认发布", 401);
  }
  const response = await fetch(
    `/api/v1/collaboration/deliverables/${encodeURIComponent(
      deliverableId,
    )}/publish`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "If-Match": String(taskVersion),
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        version_id: versionId,
        publication_note: publicationNote.trim() || null,
      }),
    },
  );
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as DeliverablePublicationRecord;
}

export function CollaborationTaskPanel({
  task,
  canWork,
  canConfirm,
  eventKind,
  onRefresh,
}: CollaborationTaskPanelProps) {
  const [detail, setDetail] = useState<CommandHallTaskDetail | null>(null);
  const [detailStatus, setDetailStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  const [resultText, setResultText] = useState("");
  const [submissionKey, setSubmissionKey] = useState<string | null>(null);
  const [returnReason, setReturnReason] = useState("");
  const [returnKey, setReturnKey] = useState<string | null>(null);
  const [actionKeys, setActionKeys] = useState<Record<string, string>>({});
  const [uploadBasis, setUploadBasis] = useState<Record<string, string>>({});
  const [uploadFiles, setUploadFiles] = useState<Record<string, File | null>>({});
  const [uploadKeys, setUploadKeys] = useState<Record<string, string>>({});
  const [publishingVersionId, setPublishingVersionId] = useState<string | null>(
    null,
  );
  const [publicationNote, setPublicationNote] = useState("");
  const [publicationKeys, setPublicationKeys] = useState<Record<string, string>>(
    {},
  );

  async function loadDetail() {
    setDetailStatus("loading");
    setError("");
    try {
      const loaded = await getCommandHallTask(task.id);
      setDetail(loaded);
      setDetailStatus("ready");
    } catch (caught) {
      setError(errorMessage(caught));
      setDetailStatus("error");
    }
  }

  useEffect(() => {
    void loadDetail();
  }, [task.id, task.row_version]);

  async function afterMutation(message: string) {
    setSuccess(message);
    await onRefresh();
    await loadDetail();
  }

  function reusableActionKey(key: string): string {
    const existing = actionKeys[key];
    if (existing) {
      return existing;
    }
    const created = newIdempotencyKey();
    setActionKeys((current) => ({ ...current, [key]: created }));
    return created;
  }

  function clearActionKey(key: string) {
    setActionKeys((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
  }

  async function runAction(
    operation: () => Promise<unknown>,
    successMessage: string,
  ): Promise<boolean> {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await operation();
      await afterMutation(successMessage);
      return true;
    } catch (caught) {
      setError(errorMessage(caught));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!resultText.trim()) {
      setError("请填写文字结果");
      return;
    }
    const key = submissionKey ?? newIdempotencyKey();
    if (!submissionKey) {
      setSubmissionKey(key);
    }
    const succeeded = await runAction(
      () =>
        submitCollaborationTask(
          task.id,
          task.row_version,
          { result_text: resultText.trim() },
          key,
        ),
      "文字结果已提交",
    );
    if (succeeded) {
      setResultText("");
      setSubmissionKey(null);
    }
  }

  async function handleReturn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!returnReason.trim()) {
      setError("请填写退回说明");
      return;
    }
    const key = returnKey ?? newIdempotencyKey();
    if (!returnKey) {
      setReturnKey(key);
    }
    const succeeded = await runAction(
      () =>
        returnCollaborationTask(
          task.id,
          task.row_version,
          returnReason.trim(),
          key,
        ),
      "任务已退回处理",
    );
    if (succeeded) {
      setReturnReason("");
      setReturnKey(null);
    }
  }

  async function handleUpload(deliverable: DeliverableRecord) {
    const file = uploadFiles[deliverable.id];
    if (!file) {
      setError(`请选择${deliverable.title}的候选文件`);
      return;
    }
    const key = uploadKeys[deliverable.id] ?? newIdempotencyKey();
    if (!uploadKeys[deliverable.id]) {
      setUploadKeys((current) => ({ ...current, [deliverable.id]: key }));
    }
    const succeeded = await runAction(
      () =>
        uploadCandidateVersion(
          deliverable.id,
          file,
          uploadBasis[deliverable.id] ?? "",
          key,
        ),
      "候选文件已上传",
    );
    if (succeeded) {
      setUploadFiles((current) => ({ ...current, [deliverable.id]: null }));
      setUploadBasis((current) => ({ ...current, [deliverable.id]: "" }));
      setUploadKeys((current) => {
        const next = { ...current };
        delete next[deliverable.id];
        return next;
      });
    }
  }

  async function handlePublish(
    deliverable: DeliverableRecord,
    version: DeliverableVersionRecord,
  ) {
    const key = publicationKeys[version.id] ?? newIdempotencyKey();
    if (!publicationKeys[version.id]) {
      setPublicationKeys((current) => ({ ...current, [version.id]: key }));
    }
    const succeeded = await runAction(
      () =>
        publishCandidateVersion(
          deliverable.id,
          version.id,
          task.row_version,
          publicationNote,
          key,
        ),
      "当前发布版已更新",
    );
    if (succeeded) {
      setPublishingVersionId(null);
      setPublicationNote("");
      setPublicationKeys((current) => {
        const next = { ...current };
        delete next[version.id];
        return next;
      });
    }
  }

  const marker = eventMarker(eventKind);
  const deliverables = detail ? asDeliverables(detail) : [];

  return (
    <section className="collaboration-task-panel" aria-labelledby="collaboration-task-title">
      <header className="collaboration-task-panel__header">
        <div>
          <p className="eyebrow">任务详情</p>
          <div className="collaboration-task-panel__title-row">
            <h2 id="collaboration-task-title">{task.title}</h2>
            {marker ? (
              <span className={marker.className}>{marker.text}</span>
            ) : null}
          </div>
        </div>
        <span className={`task-status task-status--${task.status}`}>
          {STATUS_LABELS[task.status] ?? task.status}
        </span>
      </header>

      <dl className="collaboration-task-facts">
        <div>
          <dt>任务说明</dt>
          <dd>{task.instruction}</dd>
        </div>
        <div>
          <dt>时效状态</dt>
          <dd>
            {TIMELINESS_LABELS[task.timeliness_state] ?? task.timeliness_state}
          </dd>
        </div>
        <div>
          <dt>截止时间</dt>
          <dd>{formatDateTime(task.due_at)}</dd>
        </div>
        <div>
          <dt>实际贡献者</dt>
          <dd>
            {task.contributors.length > 0
              ? task.contributors.map((item) => item.username).join("、")
              : "暂无"}
          </dd>
        </div>
      </dl>

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

      <div className="collaboration-task-actions">
        {canWork && task.status === "pending" ? (
          <button
            className="primary-button"
            type="button"
            disabled={busy}
            onClick={() => {
              const key = reusableActionKey(`task-start:${task.id}`);
              void runAction(
                () => startCollaborationTask(task.id, task.row_version, key),
                "任务已开始处理",
              ).then((succeeded) => {
                if (succeeded) {
                  clearActionKey(`task-start:${task.id}`);
                }
              });
            }}
          >
            开始处理
          </button>
        ) : null}

        {canConfirm && task.status === "pending_review" ? (
          <button
            className="primary-button"
            type="button"
            disabled={busy}
            onClick={() => {
              const key = reusableActionKey(`task-complete:${task.id}`);
              void runAction(
                () =>
                  completeCollaborationTask(task.id, task.row_version, key),
                "任务已确认完成",
              ).then((succeeded) => {
                if (succeeded) {
                  clearActionKey(`task-complete:${task.id}`);
                }
              });
            }}
          >
            确认完成
          </button>
        ) : null}

        {canConfirm &&
        (task.status === "pending" || task.status === "in_progress") ? (
          <button
            className="secondary-button"
            type="button"
            disabled={busy}
            onClick={() => {
              const key = reusableActionKey(`task-cancel:${task.id}`);
              void runAction(
                async () => {
                  const token = getAccessToken();
                  if (!token) {
                    throw new ApiError("请先登录后取消任务", 401);
                  }
                  const response = await fetch(
                    `/api/v1/collaboration/tasks/${encodeURIComponent(
                      task.id,
                    )}/cancel`,
                    {
                      method: "POST",
                      headers: {
                        Authorization: `Bearer ${token}`,
                        "Content-Type": "application/json",
                        "If-Match": String(task.row_version),
                        "Idempotency-Key": key,
                      },
                      body: JSON.stringify({ reason: "平台标记不再需要" }),
                    },
                  );
                  if (!response.ok) {
                    throw await responseError(response);
                  }
                },
                "任务已标记为不再需要",
              ).then((succeeded) => {
                if (succeeded) {
                  clearActionKey(`task-cancel:${task.id}`);
                }
              });
            }}
          >
            标记不再需要
          </button>
        ) : null}
      </div>

      {canWork && task.status === "in_progress" ? (
        <form className="collaboration-task-form" onSubmit={handleSubmit}>
          <label htmlFor="collaboration-result-text">文字结果</label>
          <textarea
            id="collaboration-result-text"
            value={resultText}
            onChange={(event) => {
              setResultText(event.target.value);
              setSubmissionKey(null);
            }}
            placeholder="填写处置结果或外部联络情况"
          />
          <div className="form-actions">
            <button
              className="primary-button"
              type="submit"
              disabled={busy || !resultText.trim()}
            >
              提交成果
            </button>
          </div>
        </form>
      ) : null}

      {canConfirm && task.status === "pending_review" ? (
        <form className="collaboration-task-form" onSubmit={handleReturn}>
          <label htmlFor="collaboration-return-reason">退回说明</label>
          <textarea
            id="collaboration-return-reason"
            value={returnReason}
            onChange={(event) => {
              setReturnReason(event.target.value);
              setReturnKey(null);
            }}
            placeholder="说明需要补充或修正的内容"
          />
          <div className="form-actions">
            <button
              className="secondary-button"
              type="submit"
              disabled={busy || !returnReason.trim()}
            >
              退回处理
            </button>
          </div>
        </form>
      ) : null}

      <section className="collaboration-deliverables" aria-labelledby="deliverables-title">
        <header className="section-header">
          <div>
            <p className="eyebrow">成果版本</p>
            <h3 id="deliverables-title">成果与当前发布版</h3>
          </div>
        </header>

        {detailStatus === "loading" ? (
          <p className="collaboration-task-panel__empty">正在加载成果版本</p>
        ) : null}
        {detailStatus === "error" ? (
          <button
            className="secondary-button"
            type="button"
            onClick={() => void loadDetail()}
          >
            重新加载成果版本
          </button>
        ) : null}
        {detailStatus === "ready" && deliverables.length === 0 ? (
          <p className="collaboration-task-panel__empty">暂无成果版本</p>
        ) : null}

        {deliverables.map((deliverable) => (
          <article className="collaboration-deliverable" key={deliverable.id}>
            <header>
              <div>
                <h4>{deliverable.title}</h4>
                <p>
                  {REQUIREMENT_LABELS[deliverable.requirement_kind] ??
                    deliverable.requirement_kind}
                  {deliverable.is_required ? " · 必需成果" : " · 可选成果"}
                </p>
              </div>
              <span className="mono">{deliverable.deliverable_code}</span>
            </header>

            {deliverable.versions.length > 0 ? (
              <div className="collaboration-version-list">
                {deliverable.versions.map((version) => {
                  const isCurrent =
                    deliverable.current_publication?.version_id === version.id;
                  return (
                    <div className="collaboration-version" key={version.id}>
                      <div>
                        <strong>V{version.version_no}</strong>
                        <span
                          className={`collaboration-source collaboration-source--${version.source_kind}`}
                        >
                          {SOURCE_LABELS[version.source_kind] ??
                            version.source_kind}
                        </span>
                        {isCurrent ? (
                          <span className="collaboration-current">当前发布版</span>
                        ) : null}
                      </div>
                      <p>
                        {version.file_name ??
                          (version.text_result ? "文字成果" : "自动成果")}
                        {version.created_by
                          ? ` · ${version.created_by}`
                          : ""}
                      </p>
                      <p>{formatDateTime(version.created_at)}</p>
                      {version.basis_text ? (
                        <p className="collaboration-version__basis">
                          修订依据：{version.basis_text}
                        </p>
                      ) : null}
                      {canConfirm && !isCurrent && !busy ? (
                        <button
                          className="text-button"
                          type="button"
                          onClick={() => {
                            setPublishingVersionId(version.id);
                            setPublicationNote("");
                          }}
                        >
                          发布此版本
                        </button>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="collaboration-task-panel__empty">暂无候选版本</p>
            )}

            {canWork &&
            !["completed", "not_required", "failed"].includes(task.status) &&
            ["manual_file", "manual_file_or_text"].includes(
              deliverable.requirement_kind,
            ) ? (
              <div className="collaboration-upload">
                <label htmlFor={`candidate-file-${deliverable.id}`}>
                  候选文件：{deliverable.title}
                </label>
                <input
                  id={`candidate-file-${deliverable.id}`}
                  type="file"
                  onChange={(event) => {
                    const file = event.target.files?.[0] ?? null;
                    setUploadFiles((current) => ({
                      ...current,
                      [deliverable.id]: file,
                    }));
                    setUploadKeys((current) => {
                      const next = { ...current };
                      delete next[deliverable.id];
                      return next;
                    });
                  }}
                />
                <label htmlFor={`candidate-basis-${deliverable.id}`}>
                  候选版本说明：{deliverable.title}
                </label>
                <input
                  id={`candidate-basis-${deliverable.id}`}
                  value={uploadBasis[deliverable.id] ?? ""}
                  onChange={(event) => {
                    setUploadBasis((current) => ({
                      ...current,
                      [deliverable.id]: event.target.value,
                    }));
                    setUploadKeys((current) => {
                      const next = { ...current };
                      delete next[deliverable.id];
                      return next;
                    });
                  }}
                />
                <button
                  className="secondary-button"
                  type="button"
                  disabled={busy || !uploadFiles[deliverable.id]}
                  onClick={() => void handleUpload(deliverable)}
                >
                  上传候选文件
                </button>
              </div>
            ) : null}

            {publishingVersionId &&
            deliverable.versions.some(
              (version) => version.id === publishingVersionId,
            ) ? (
              <div className="collaboration-publish-confirmation">
                <strong>确认发布</strong>
                <label htmlFor={`publication-note-${deliverable.id}`}>
                  发布说明
                </label>
                <input
                  id={`publication-note-${deliverable.id}`}
                  value={publicationNote}
                  onChange={(event) => setPublicationNote(event.target.value)}
                />
                <div className="form-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={busy}
                    onClick={() => setPublishingVersionId(null)}
                  >
                    取消发布
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      const version = deliverable.versions.find(
                        (item) => item.id === publishingVersionId,
                      );
                      if (version) {
                        void handlePublish(deliverable, version);
                      }
                    }}
                  >
                    确认发布
                  </button>
                </div>
              </div>
            ) : null}
          </article>
        ))}
      </section>
    </section>
  );
}
