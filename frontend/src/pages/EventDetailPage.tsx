import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, getCurrentAssessment, getEvent, getLossAssessment } from "../api/client";
import { AssessmentProgressCard } from "../components/AssessmentProgressCard";
import { LossAssessmentPanel } from "../components/LossAssessmentPanel";
import { ResponseSuggestionCard } from "../components/ResponseSuggestionCard";
import {
  formatCoordinate,
  formatDateTime,
  formatDepth,
  formatEventKind,
  formatInstitutionalLevel,
  formatLifecycleState,
  formatMagnitude,
  formatServiceLevel,
  isTestOrDrill,
  type AssessmentRunStatus,
  type EventDetail,
  type LossResult,
} from "../types";

type DetailStatus = "loading" | "ready" | "error";
type LossLoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; result: LossResult }
  | { status: "error" };

export function EventDetailPage() {
  const { eventId = "" } = useParams();
  const [event, setEvent] = useState<EventDetail | null>(null);
  const [assessment, setAssessment] = useState<AssessmentRunStatus | null>(null);
  const [status, setStatus] = useState<DetailStatus>("loading");
  const [lossState, setLossState] = useState<LossLoadState>({ status: "idle" });
  const lossRequestRef = useRef(0);

  const loadDetail = useCallback(async () => {
    setStatus("loading");
    try {
      setEvent(await getEvent(eventId));
      setStatus("ready");
    } catch {
      setStatus("error");
    }
  }, [eventId]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  useEffect(() => {
    let active = true;
    setAssessment(null);
    void getCurrentAssessment(eventId)
      .then((result) => {
        if (active) {
          setAssessment(result);
        }
      })
      .catch(() => {
        if (active) {
          setAssessment(null);
        }
      });
    return () => {
      active = false;
    };
  }, [eventId]);

  const loadLoss = useCallback(async (runId: string) => {
    const requestId = ++lossRequestRef.current;
    setLossState({ status: "loading" });
    try {
      const result = await getLossAssessment(runId);
      if (requestId === lossRequestRef.current) {
        setLossState({ status: "ready", result });
      }
    } catch (error) {
      if (requestId !== lossRequestRef.current) {
        return;
      }
      if (error instanceof ApiError && error.status === 404) {
        setLossState({ status: "idle" });
      } else {
        setLossState({ status: "error" });
      }
    }
  }, []);

  const assessmentRunId = assessment?.run_id;

  useEffect(() => {
    if (!assessmentRunId) {
      lossRequestRef.current += 1;
      setLossState({ status: "idle" });
      return;
    }
    void loadLoss(assessmentRunId);
  }, [assessmentRunId, loadLoss]);

  const suggestion = event?.response_suggestion;
  const causes =
    suggestion && Array.isArray(suggestion.causes) ? suggestion.causes : [];

  return (
    <section className="page-section" aria-labelledby="detail-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">事件详情</p>
          <h1 id="detail-title">{event?.place ?? "事件详情"}</h1>
        </div>
        <Link className="text-button" to="/">
          返回事件列表
        </Link>
      </header>

      {status === "loading" ? (
        <div className="state-panel">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载事件详情</p>
        </div>
      ) : null}

      {status === "error" ? (
        <div className="state-panel state-panel--error" role="alert">
          <p>无法加载事件详情</p>
          <button className="secondary-button" type="button" onClick={() => void loadDetail()}>
            重试
          </button>
        </div>
      ) : null}

      {status === "ready" && event ? (
        <div className="detail-layout">
          <section className="detail-block detail-block--summary">
            <header className="section-header">
              <div>
                <p className="eyebrow">当前修订</p>
                <h2>
                  {event.place}
                  <span className="revision-chip">R{event.revision_no}</span>
                </h2>
              </div>
              <span
                className={`kind-tag kind-tag--${isTestOrDrill(event.event_kind) ? "drill" : "real"}`}
              >
                {formatEventKind(event.event_kind)}
              </span>
            </header>

            <dl className="detail-facts">
              <div>
                <dt>发震时刻</dt>
                <dd>{formatDateTime(event.origin_time)}</dd>
              </div>
              <div>
                <dt>地点</dt>
                <dd>{event.place || "-"}</dd>
              </div>
              <div>
                <dt>震级</dt>
                <dd>{formatMagnitude(event.magnitude)}</dd>
              </div>
              <div>
                <dt>震源深度</dt>
                <dd>{formatDepth(event.depth_km)}</dd>
              </div>
              <div>
                <dt>经度</dt>
                <dd>{formatCoordinate(event.longitude)}</dd>
              </div>
              <div>
                <dt>纬度</dt>
                <dd>{formatCoordinate(event.latitude)}</dd>
              </div>
              <div>
                <dt>数据来源</dt>
                <dd>{event.source || "-"}</dd>
              </div>
              <div>
                <dt>事件编号</dt>
                <dd className="mono">{event.id}</dd>
              </div>
              <div>
                <dt>生命周期状态</dt>
                <dd>
                  <span
                    className={`lifecycle-tag lifecycle-tag--${event.lifecycle_state ?? "unentered"}`}
                  >
                    {formatLifecycleState(event.lifecycle_state)}
                  </span>
                </dd>
              </div>
              <div>
                <dt>T1</dt>
                <dd>{formatDateTime(event.t1_at)}</dd>
              </div>
            </dl>
          </section>

          <AssessmentProgressCard run={assessment} />

          {lossState.status === "loading" ? (
            <div className="state-panel">
              <span className="state-icon" aria-hidden="true" />
              <p>正在加载损失评估结果</p>
            </div>
          ) : null}

          {lossState.status === "error" ? (
            <div className="state-panel state-panel--error" role="alert">
              <p>无法加载损失评估结果</p>
              <button
                className="secondary-button"
                type="button"
                onClick={() => {
                  if (assessmentRunId) {
                    void loadLoss(assessmentRunId);
                  }
                }}
              >
                重试
              </button>
            </div>
          ) : null}

          {lossState.status === "ready" && assessmentRunId ? (
            <LossAssessmentPanel
              runId={assessmentRunId}
              result={lossState.result}
            />
          ) : null}

          <section className="detail-block">
            <header className="section-header">
              <div>
                <p className="eyebrow">响应建议</p>
                <h2>制度与服务分级</h2>
              </div>
            </header>

            {event.response_suggestion ? (
              <ResponseSuggestionCard
                suggestion={event.response_suggestion}
                fallbackRuleVersion={event.response_rule_version}
              />
            ) : (
              <div className="response-grid">
                <section className="response-panel">
                  <header className="panel-header">
                    <span className="panel-eyebrow">上海市制度响应</span>
                    <span className="response-level">
                      {formatInstitutionalLevel(event.institutional_level)}
                    </span>
                  </header>
                </section>
                <section className="response-panel">
                  <header className="panel-header">
                    <span className="panel-eyebrow">中国地震局应急服务响应</span>
                    <span className="response-level">
                      {formatServiceLevel(event.service_level)}
                    </span>
                  </header>
                </section>
              </div>
            )}
          </section>

          <section className="detail-block">
            <header className="section-header">
              <div>
                <p className="eyebrow">研判依据</p>
                <h2>触发原因与规则版本</h2>
              </div>
            </header>
            <dl className="detail-facts detail-facts--single">
              <div>
                <dt>规则版本</dt>
                <dd>{event.response_rule_version || "-"}</dd>
              </div>
              <div>
                <dt>触发原因</dt>
                <dd>
                  {causes.length > 0 ? (
                    <ul className="cause-list">
                      {causes.map((cause) => (
                        <li key={cause}>{cause}</li>
                      ))}
                    </ul>
                  ) : (
                    "暂无触发原因记录"
                  )}
                </dd>
              </div>
            </dl>
          </section>
        </div>
      ) : null}
    </section>
  );
}
