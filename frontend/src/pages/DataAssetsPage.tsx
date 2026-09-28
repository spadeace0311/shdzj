import { useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  importDataAsset,
  listDataAssetVersions,
  listDataAssets,
  publishDataAssetVersion,
  retireDataAssetVersion,
  rollbackDataAssetVersion,
  validateDataAssetVersion,
} from "../api/client";
import {
  formatDateTime,
  type DataAssetSummary,
  type DataAssetVersion,
  type ValidationIssue,
  type ValidationReport,
} from "../types";

type PageStatus = "loading" | "ready" | "error";
type LifecycleAction = "publish" | "retire" | "rollback";

const STATUS_LABELS: Record<string, string> = {
  imported: "已导入",
  validated: "已校验",
  published: "已发布",
  retired: "已停用",
  rejected: "已拒绝",
};

const DATA_TYPE_LABELS: Record<string, string> = {
  vector: "矢量",
  table: "表格",
  raster: "栅格",
  parameter: "参数",
};

const ACTION_LABELS: Record<LifecycleAction, string> = {
  publish: "发布",
  retire: "停用",
  rollback: "回滚",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "网络连接失败，请稍后重试";
}

function IssueList({
  title,
  issues,
}: {
  title: string;
  issues: ValidationIssue[];
}) {
  if (issues.length === 0) {
    return null;
  }

  return (
    <div className="data-asset-finding-group">
      <strong>{title}</strong>
      <ul>
        {issues.map((issue, index) => (
          <li key={`${issue.code}-${index}`}>
            <span className="mono">{issue.code}</span>
            <span>{issue.message}</span>
            {issue.row_number !== null ? (
              <span>第 {issue.row_number} 行</span>
            ) : null}
            {issue.field_name ? <span>{issue.field_name}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function DataAssetsPage({ userRole }: { userRole: string }) {
  const canWrite = ["superadmin", "data_maintainer", "data_publisher"].includes(
    userRole,
  );
  const canPublish = ["superadmin", "data_publisher"].includes(userRole);

  const [pageStatus, setPageStatus] = useState<PageStatus>("loading");
  const [assets, setAssets] = useState<DataAssetSummary[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [versions, setVersions] = useState<DataAssetVersion[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [validationReport, setValidationReport] =
    useState<ValidationReport | null>(null);
  const [validationMessage, setValidationMessage] = useState("");

  const [importVersion, setImportVersion] = useState("");
  const [sourceUri, setSourceUri] = useState("");
  const [licenseName, setLicenseName] = useState("");
  const [changeNote, setChangeNote] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [importState, setImportState] = useState<
    "idle" | "submitting" | "queued"
  >("idle");
  const [importMessage, setImportMessage] = useState("");
  const [importError, setImportError] = useState("");

  const [dialog, setDialog] = useState<{
    action: LifecycleAction;
    version: DataAssetVersion;
  } | null>(null);
  const [dialogReason, setDialogReason] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [dialogSubmitting, setDialogSubmitting] = useState(false);

  async function loadAssets() {
    setPageStatus("loading");
    try {
      const assetList = await listDataAssets();
      const initialKey = assetList[0]?.asset_key ?? null;
      const versionList = initialKey
        ? await listDataAssetVersions(initialKey)
        : [];
      setAssets(assetList);
      setSelectedKey(initialKey);
      setVersions(versionList);
      setSelectedVersionId(versionList[0]?.id ?? null);
      setPageStatus("ready");
    } catch (caught) {
      setPageStatus("error");
      setImportError(errorMessage(caught));
    }
  }

  useEffect(() => {
    void loadAssets();
  }, []);

  async function selectAsset(assetKey: string) {
    setSelectedKey(assetKey);
    setSelectedVersionId(null);
    setValidationReport(null);
    setValidationMessage("");
    try {
      const versionList = await listDataAssetVersions(assetKey);
      setVersions(versionList);
      setSelectedVersionId(versionList[0]?.id ?? null);
    } catch {
      setVersions([]);
      setSelectedVersionId(null);
    }
  }

  async function loadVersionsForAsset(assetKey: string) {
    try {
      const versionList = await listDataAssetVersions(assetKey);
      setVersions(versionList);
      setSelectedVersionId(versionList[0]?.id ?? null);
    } catch {
      // Keep the existing table so a failed refresh does not discard known state.
    }
  }

  async function handleImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const selectedAsset = assets.find((asset) => asset.asset_key === selectedKey);
    if (!selectedAsset || !file) {
      setImportError("请选择数据文件");
      return;
    }

    setImportError("");
    setImportMessage("");
    setImportState("submitting");
    try {
      await importDataAsset(selectedAsset.asset_key, {
        version: importVersion.trim(),
        source_uri: sourceUri.trim(),
        ...(licenseName.trim() ? { license_name: licenseName.trim() } : {}),
        change_note: changeNote.trim(),
        file,
      });
      setImportState("queued");
      setImportMessage("导入任务已进入队列");
      void loadVersionsForAsset(selectedAsset.asset_key);
    } catch (caught) {
      setImportState("idle");
      setImportError(errorMessage(caught));
    }
  }

  async function handleValidate(version: DataAssetVersion) {
    setValidationMessage("正在校验");
    try {
      const report = await validateDataAssetVersion(version.id);
      setValidationReport(report);
      setValidationMessage("校验完成");
    } catch (caught) {
      setValidationMessage(errorMessage(caught));
    }
  }

  function openLifecycle(action: LifecycleAction, version: DataAssetVersion) {
    setDialog({ action, version });
    setDialogReason("");
    setDialogError("");
  }

  async function confirmLifecycle() {
    if (!dialog) {
      return;
    }

    const reason = dialogReason.trim();
    if (!reason) {
      setDialogError("请填写操作原因");
      return;
    }

    setDialogError("");
    setDialogSubmitting(true);
    try {
      let updated: DataAssetVersion;
      if (dialog.action === "publish") {
        updated = await publishDataAssetVersion(dialog.version.id, reason);
      } else if (dialog.action === "retire") {
        updated = await retireDataAssetVersion(dialog.version.id, reason);
      } else {
        updated = await rollbackDataAssetVersion(dialog.version.id, reason);
      }

      setVersions((current) =>
        current.map((version) => (version.id === updated.id ? updated : version)),
      );
      setSelectedVersionId(updated.id);
      setDialog(null);
    } catch (caught) {
      setDialogError(errorMessage(caught));
    } finally {
      setDialogSubmitting(false);
    }
  }

  const selectedAsset = assets.find((asset) => asset.asset_key === selectedKey);
  const selectedVersion =
    versions.find((version) => version.id === selectedVersionId) ??
    versions[0] ??
    null;
  const currentErrors =
    validationReport && validationReport.version_id === selectedVersion?.id
      ? validationReport.errors
      : (selectedVersion?.validation_errors ?? []);
  const currentWarnings =
    validationReport && validationReport.version_id === selectedVersion?.id
      ? validationReport.warnings
      : (selectedVersion?.validation_warnings ?? []);

  return (
    <section className="page-section" aria-labelledby="data-assets-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">数据治理</p>
          <h1 id="data-assets-title">数据资产中心</h1>
        </div>
      </header>

      {pageStatus === "loading" ? (
        <div className="state-panel">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载数据资产</p>
        </div>
      ) : null}

      {pageStatus === "error" ? (
        <div className="state-panel state-panel--error" role="alert">
          <p>{importError || "无法加载数据资产"}</p>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void loadAssets()}
          >
            重试
          </button>
        </div>
      ) : null}

      {pageStatus === "ready" ? (
        <div className="data-asset-layout">
          <aside
            className="data-asset-panel data-asset-selector"
            aria-label="数据资产列表"
          >
            <header className="section-header">
              <div>
                <p className="eyebrow">资产目录</p>
                <h2>数据资产</h2>
              </div>
            </header>
            {assets.length > 0 ? (
              <div className="data-asset-selector-list">
                {assets.map((asset) => (
                  <button
                    key={asset.asset_key}
                    className={`data-asset-selector-item${
                      asset.asset_key === selectedKey
                        ? " data-asset-selector-item--active"
                        : ""
                    }`}
                    type="button"
                    aria-current={
                      asset.asset_key === selectedKey ? "true" : undefined
                    }
                    onClick={() => void selectAsset(asset.asset_key)}
                  >
                    <strong>{asset.name}</strong>
                    <span className="mono">{asset.asset_key}</span>
                  </button>
                ))}
              </div>
            ) : (
              <p className="data-asset-empty">暂无数据资产</p>
            )}
          </aside>

          <div className="data-asset-main">
            {selectedAsset ? (
              <section
                className="data-asset-panel"
                aria-labelledby="selected-asset-title"
              >
                <header className="section-header">
                  <div>
                    <p className="eyebrow">资产档案</p>
                    <h2 id="selected-asset-title">资产详情</h2>
                  </div>
                  {selectedAsset.is_update_overdue ? (
                    <span className="data-asset-overdue">更新已逾期</span>
                  ) : null}
                </header>
                <dl className="data-asset-facts">
                  <div>
                    <dt>资产标识</dt>
                    <dd className="mono">{selectedAsset.asset_key}</dd>
                  </div>
                  <div>
                    <dt>数据类型</dt>
                    <dd>{DATA_TYPE_LABELS[selectedAsset.data_type] ?? selectedAsset.data_type}</dd>
                  </div>
                  <div>
                    <dt>空间粒度</dt>
                    <dd>{selectedAsset.spatial_granularity || "-"}</dd>
                  </div>
                  <div>
                    <dt>责任单位</dt>
                    <dd>{selectedAsset.responsibility_unit}</dd>
                  </div>
                  <div>
                    <dt>更新周期</dt>
                    <dd>{selectedAsset.update_interval_days} 天</dd>
                  </div>
                  <div>
                    <dt>核心资产</dt>
                    <dd>{selectedAsset.is_core ? "是" : "否"}</dd>
                  </div>
                  <div>
                    <dt>已发布版本</dt>
                    <dd>
                      {selectedAsset.published_version
                        ? `${selectedAsset.published_version}（${formatDateTime(
                            selectedAsset.published_at,
                          )}）`
                        : "-"}
                    </dd>
                  </div>
                  <div>
                    <dt>下次更新</dt>
                    <dd>{formatDateTime(selectedAsset.update_due_at)}</dd>
                  </div>
                </dl>
              </section>
            ) : (
              <div className="data-asset-panel">
                <p className="data-asset-empty">请选择数据资产</p>
              </div>
            )}

            {canWrite && selectedAsset ? (
              <section
                className="data-asset-panel"
                aria-labelledby="import-form-title"
              >
                <header className="section-header">
                  <div>
                    <p className="eyebrow">数据接入</p>
                    <h2 id="import-form-title">导入新版本</h2>
                  </div>
                </header>
                <form className="data-asset-import-form" onSubmit={handleImport}>
                  <div className="data-asset-import-grid">
                    <label htmlFor="import-version">数据版本</label>
                    <input
                      id="import-version"
                      value={importVersion}
                      onChange={(event) => setImportVersion(event.target.value)}
                    />

                    <label htmlFor="import-source-uri">来源 URI</label>
                    <input
                      id="import-source-uri"
                      value={sourceUri}
                      onChange={(event) => setSourceUri(event.target.value)}
                    />

                    <label htmlFor="import-license">许可证</label>
                    <input
                      id="import-license"
                      value={licenseName}
                      onChange={(event) => setLicenseName(event.target.value)}
                    />

                    <label htmlFor="import-change-note">变更说明</label>
                    <input
                      id="import-change-note"
                      value={changeNote}
                      onChange={(event) => setChangeNote(event.target.value)}
                    />

                    <label htmlFor="import-file">选择文件</label>
                    <input
                      id="import-file"
                      type="file"
                      onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                    />
                  </div>

                  {importError ? (
                    <p className="form-error" role="alert">
                      {importError}
                    </p>
                  ) : null}
                  {importMessage ? (
                    <p className="form-success" role="status">
                      {importMessage}
                    </p>
                  ) : null}

                  <div className="form-actions">
                    <button
                      className="primary-button"
                      type="submit"
                      disabled={importState === "submitting"}
                    >
                      {importState === "submitting" ? "导入中" : "导入"}
                    </button>
                  </div>
                </form>
              </section>
            ) : null}

            <section
              className="data-asset-panel"
              aria-labelledby="version-table-title"
            >
              <header className="section-header">
                <div>
                  <p className="eyebrow">版本档案</p>
                  <h2 id="version-table-title">数据资产版本</h2>
                </div>
              </header>

              <div className="table-scroll">
                <table className="data-asset-table">
                  <thead>
                    <tr>
                      <th scope="col">版本</th>
                      <th scope="col">状态</th>
                      <th scope="col">质量</th>
                      <th scope="col">记录数</th>
                      <th scope="col">校验和</th>
                      <th scope="col">更新时间</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {versions.length > 0 ? (
                      versions.map((version) => (
                        <tr key={version.id}>
                          <td className="data-asset-version-name">
                            {version.version}
                          </td>
                          <td>
                            <span
                              className={`data-asset-status data-asset-status--${version.status}`}
                            >
                              {STATUS_LABELS[version.status] ?? version.status}
                            </span>
                          </td>
                          <td>{version.quality_grade || "-"}</td>
                          <td>{version.record_count}</td>
                          <td className="mono data-asset-checksum">
                            {version.checksum}
                          </td>
                          <td>
                            {formatDateTime(
                              version.published_at ??
                                version.validated_at ??
                                version.imported_at,
                            )}
                          </td>
                          <td>
                            <div className="data-asset-actions">
                              {canWrite ? (
                                <button
                                  className="text-button"
                                  type="button"
                                  onClick={() => void handleValidate(version)}
                                >
                                  校验
                                </button>
                              ) : null}
                              {canPublish ? (
                                <button
                                  className="text-button"
                                  type="button"
                                  onClick={() => openLifecycle("publish", version)}
                                >
                                  发布
                                </button>
                              ) : null}
                              {canPublish ? (
                                <button
                                  className="text-button"
                                  type="button"
                                  onClick={() => openLifecycle("retire", version)}
                                >
                                  停用
                                </button>
                              ) : null}
                              {canPublish ? (
                                <button
                                  className="text-button"
                                  type="button"
                                  onClick={() => openLifecycle("rollback", version)}
                                >
                                  回滚
                                </button>
                              ) : null}
                            </div>
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td className="data-asset-empty-cell" colSpan={7}>
                          暂无版本
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {selectedVersion ? (
                <div className="data-asset-findings">
                  <header className="section-header">
                    <div>
                      <p className="eyebrow">质量校验</p>
                      <h2>校验结果</h2>
                    </div>
                    <span className="data-asset-validation-message">
                      {validationMessage}
                    </span>
                  </header>
                  <IssueList title="错误" issues={currentErrors} />
                  <IssueList title="警告" issues={currentWarnings} />
                  {currentErrors.length === 0 && currentWarnings.length === 0 ? (
                    <p className="data-asset-empty">暂无校验问题</p>
                  ) : null}
                </div>
              ) : null}
            </section>
          </div>
        </div>
      ) : null}

      {dialog ? (
        <div className="data-asset-dialog-backdrop">
          <section
            className="data-asset-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="lifecycle-dialog-title"
          >
            <header>
              <p className="eyebrow">数据资产生命周期</p>
              <h2 id="lifecycle-dialog-title">
                {ACTION_LABELS[dialog.action]}版本
              </h2>
            </header>
            <p>
              版本 <strong>{dialog.version.version}</strong> 将执行
              {ACTION_LABELS[dialog.action]}操作。
            </p>
            <label htmlFor="lifecycle-reason">操作原因</label>
            <textarea
              id="lifecycle-reason"
              value={dialogReason}
              onChange={(event) => setDialogReason(event.target.value)}
            />
            {dialogError ? (
              <p className="form-error" role="alert">
                {dialogError}
              </p>
            ) : null}
            <div className="data-asset-dialog-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setDialog(null)}
                disabled={dialogSubmitting}
              >
                取消
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => void confirmLifecycle()}
                disabled={!dialogReason.trim() || dialogSubmitting}
              >
                {dialogSubmitting ? "提交中" : "确认"}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
