import { useState } from "react";

import type {
  QaCitation,
  QaMapAction,
  QaToolCall,
} from "../types";

interface QaEvidencePanelProps {
  citations: QaCitation[];
  toolCalls: QaToolCall[];
  mapActions: QaMapAction[];
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

export function QaEvidencePanel({
  citations,
  toolCalls,
  mapActions,
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
    </section>
  );
}

