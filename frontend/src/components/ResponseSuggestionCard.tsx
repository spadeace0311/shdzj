import {
  formatInstitutionalLevel,
  formatServiceLevel,
  type ResponseSuggestion,
} from "../types";

interface ResponseSuggestionCardProps {
  suggestion: ResponseSuggestion;
  fallbackRuleVersion?: string | null;
}

export function ResponseSuggestionCard({
  suggestion,
  fallbackRuleVersion,
}: ResponseSuggestionCardProps) {
  const ruleVersion = suggestion.rule_version || fallbackRuleVersion || "-";
  const downgraded = suggestion.downgraded === true;
  const serviceLevel = suggestion.service_level ?? null;

  return (
    <div className="response-grid">
      <section className="response-panel response-panel--institutional">
        <header className="panel-header">
          <span className="panel-eyebrow">上海市制度响应</span>
          <span className="response-level response-level--institutional">
            {formatInstitutionalLevel(suggestion.institutional_level)}
          </span>
        </header>
        <dl className="response-facts">
          <div>
            <dt>是否降级</dt>
            <dd>{downgraded ? "已降级" : "未降级"}</dd>
          </div>
          <div>
            <dt>规则版本</dt>
            <dd>{ruleVersion}</dd>
          </div>
        </dl>
      </section>

      <section className="response-panel response-panel--service">
        <header className="panel-header">
          <span className="panel-eyebrow">中国地震局应急服务响应</span>
          <span className="response-level response-level--service">
            {formatServiceLevel(serviceLevel)}
          </span>
        </header>
        <dl className="response-facts">
          <div>
            <dt>服务级别</dt>
            <dd>{serviceLevel ?? "待研判"}</dd>
          </div>
          <div>
            <dt>规则版本</dt>
            <dd>{ruleVersion}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}
