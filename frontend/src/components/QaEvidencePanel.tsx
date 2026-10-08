import { useState } from "react";

import type {
  QaAnswerAudit,
  QaCitation,
  QaMapAction,
  QaToolCall,
} from "../types";

interface QaEvidencePanelProps {
  citations: QaCitation[];
  toolCalls: QaToolCall[];
  mapActions: QaMapAction[];
  structured?: Record<string, unknown> | null;
  degradedReasons?: string[];
  audit?: QaAnswerAudit | null;
}

function checksumPrefix(value: string): string {
  return value.slice(0, 8);
}

function actionLabel(action: QaMapAction): string {
  switch (action.action_type) {
    case "locate":
      return "定位";
    case "fit_bounds":
      return "范围适配";
    case "buffer":
      return "缓冲区";
    case "highlight":
      return "高亮";
    case "set_layers":
      return "图层显隐";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function StructuredValue({
  value,
  depth = 0,
}: {
  value: unknown;
  depth?: number;
}) {
  if (value === null || value === undefined) {
    return <span>-</span>;
  }
  if (Array.isArray(value)) {
    if (depth >= 4) {
      return <span>[...]</span>;
    }
    return (
      <ul className="qa-structured-list">
        {value.map((item, index) => (
          <li key={index}>
            <StructuredValue value={item} depth={depth + 1} />
          </li>
        ))}
      </ul>
    );
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return <span>-</span>;
    }
    if (depth >= 4) {
      return <span>{"{...}"}</span>;
    }
    return (
      <dl className="qa-structured-object">
        {entries.map(([key, item]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>
              <StructuredValue value={item} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span>{String(value)}</span>;
}

export function QaStructuredResult({
  value,
}: {
  value: Record<string, unknown>;
}) {
  return (
    <section
      className="qa-structured-result"
      data-testid="qa-structured"
      aria-label="结构化结果"
    >
      <h2>结构化结果</h2>
      <StructuredValue value={value} />
    </section>
  );
}

function auditValue(value: string | null | undefined): string {
  return value || "-";
}

function auditIntent(
  value: Record<string, unknown> | null,
): string {
  return typeof value?.intent === "string" ? value.intent : "-";
}

export function QaEvidencePanel({
  citations,
  toolCalls,
  mapActions,
  structured = null,
  degradedReasons = [],
  audit = null,
}: QaEvidencePanelProps) {
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(
    () => new Set(),
  );

  function toggleCitation(key: string) {
    setExpandedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  return (
    <section className="qa-evidence-panel" aria-label="引用与工具证据">
      <div className="qa-evidence-panel__section">
        <h2>引用证据</h2>
        {citations.length === 0 ? (
          <p className="qa-empty">暂无引用</p>
        ) : (
          <div className="qa-citation-list">
            {citations.map((citation) => {
              const expanded = expandedKeys.has(citation.citation_key);
              return (
                <article className="qa-citation" key={citation.citation_key}>
                  <button
                    className="qa-citation__toggle"
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => toggleCitation(citation.citation_key)}
                  >
                    <span className="qa-citation__key">
                      {citation.citation_key}
                    </span>
                    <strong>{citation.source_title}</strong>
                  </button>
                  <dl className="qa-citation__meta">
                    <div>
                      <dt>版本</dt>
                      <dd>{citation.version_label || "-"}</dd>
                    </div>
                    <div>
                      <dt>定位</dt>
                      <dd>{citation.locator || "-"}</dd>
                    </div>
                    <div>
                      <dt>校验</dt>
                      <dd className="mono">
                        {checksumPrefix(citation.checksum)}
                      </dd>
                    </div>
                  </dl>
                  {expanded ? (
                    <p className="qa-citation__excerpt">{citation.excerpt}</p>
                  ) : null}
                </article>
              );
            })}
          </div>
        )}
      </div>

      <div className="qa-evidence-panel__section">
        <h2>工具过程</h2>
        {toolCalls.length === 0 ? (
          <p className="qa-empty">暂无工具调用</p>
        ) : (
          <ul className="qa-tool-result-list">
            {toolCalls.map((tool) => (
              <li key={tool.id || tool.tool_name}>
                <span className="mono">{tool.tool_name}</span>
                <span>{tool.tool_status}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="qa-evidence-panel__section">
        <h2>地图动作</h2>
        {mapActions.length === 0 ? (
          <p className="qa-empty">暂无地图动作</p>
        ) : (
          <ul className="qa-map-action-list">
            {mapActions.map((action, index) => (
              <li key={`${action.action_type}-${index}`}>
                {actionLabel(action)}
              </li>
            ))}
          </ul>
        )}
      </div>

      {structured ? <QaStructuredResult value={structured} /> : null}

      {degradedReasons.length > 0 ? (
        <div
          className="qa-evidence-panel__section"
          data-testid="qa-degraded"
        >
          <h2>降级原因</h2>
          <ul className="qa-tool-result-list">
            {degradedReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {audit ? (
        <div
          className="qa-evidence-panel__section qa-audit"
          data-testid="qa-audit"
        >
          <h2>模型与提示词审计</h2>
          <dl className="qa-audit__facts">
            <div>
              <dt>模型</dt>
              <dd>{auditValue(audit.model_name)}</dd>
            </div>
            <div>
              <dt>模型版本</dt>
              <dd>{auditValue(audit.model_version)}</dd>
            </div>
            <div>
              <dt>提示词版本</dt>
              <dd>{auditValue(audit.prompt_version)}</dd>
            </div>
            <div>
              <dt>执行意图</dt>
              <dd>{auditIntent(audit.execution_plan)}</dd>
            </div>
          </dl>
        </div>
      ) : null}
    </section>
  );
}
