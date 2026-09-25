import { useCallback, useEffect, useState } from "react";

import { getCollectorStatus } from "../api/client";
import {
  formatDateTime,
  type CollectorProviderStatus,
  type CollectorState,
  type CollectorStatus,
} from "../types";

type PageStatus = "loading" | "ready" | "error";

const OVERALL_STATE_LABELS: Record<CollectorState, string> = {
  starting: "启动中",
  healthy: "正常",
  degraded: "降级",
  critical: "严重",
  stopped: "已停止",
};

const PROVIDER_LABELS: Record<CollectorProviderStatus["provider"], string> = {
  fan: "FAN 主链路",
  wolfx: "Wolfx 备用链路",
};

function stateLabel(state: CollectorState): string {
  return OVERALL_STATE_LABELS[state];
}

export function CollectorStatusPage() {
  const [collectorStatus, setCollectorStatus] = useState<CollectorStatus | null>(null);
  const [status, setStatus] = useState<PageStatus>("loading");

  const loadStatus = useCallback(async () => {
    setStatus("loading");
    try {
      setCollectorStatus(await getCollectorStatus());
      setStatus("ready");
    } catch {
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  return (
    <section className="page-section" aria-labelledby="collector-status-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">实时采集</p>
          <h1 id="collector-status-title">采集状态</h1>
        </div>
        <button
          className="secondary-button"
          type="button"
          disabled={status === "loading"}
          onClick={() => void loadStatus()}
        >
          刷新状态
        </button>
      </header>

      {status === "loading" ? (
        <div className="state-panel">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载采集状态</p>
        </div>
      ) : null}

      {status === "error" ? (
        <div className="state-panel state-panel--error" role="alert">
          <p>无法加载采集状态</p>
          <button className="secondary-button" type="button" onClick={() => void loadStatus()}>
            重试
          </button>
        </div>
      ) : null}

      {status === "ready" && collectorStatus ? (
        <>
          <section className="collector-overview" aria-labelledby="collector-overview-title">
            <header className="collector-overview__header">
              <div>
                <p className="eyebrow">链路汇总</p>
                <h2 id="collector-overview-title">
                  总体状态：{stateLabel(collectorStatus.overall_state)}
                </h2>
              </div>
              <span
                className={`collector-state collector-state--${collectorStatus.overall_state}`}
              >
                {stateLabel(collectorStatus.overall_state)}
              </span>
            </header>
            <dl className="collector-summary">
              <div>
                <dt>死信队列</dt>
                <dd>待处理死信 {collectorStatus.open_dead_letter_count} 条</dd>
              </div>
              <div>
                <dt>边界版本</dt>
                <dd>{collectorStatus.boundary_version || "-"}</dd>
              </div>
              <div>
                <dt>最近入库事件</dt>
                <dd className="mono">{collectorStatus.last_ingested_event_id || "-"}</dd>
              </div>
            </dl>
          </section>

          <section className="collector-providers" aria-labelledby="collector-providers-title">
            <header className="section-header">
              <div>
                <p className="eyebrow">双链路</p>
                <h2 id="collector-providers-title">主备采集状态</h2>
              </div>
            </header>
            <div className="table-scroll">
              <table className="collector-table">
                <thead>
                  <tr>
                    <th scope="col">链路</th>
                    <th scope="col">状态</th>
                    <th scope="col">连接</th>
                    <th scope="col">最近连接</th>
                    <th scope="col">最后消息</th>
                    <th scope="col">最后成功</th>
                    <th scope="col">连续失败</th>
                    <th scope="col">重连次数</th>
                    <th scope="col">HTTP 状态</th>
                    <th scope="col">错误摘要</th>
                    <th scope="col">更新时间</th>
                  </tr>
                </thead>
                <tbody>
                  {collectorStatus.providers.map((provider) => (
                    <tr key={provider.provider}>
                      <td className="collector-provider-name">
                        {PROVIDER_LABELS[provider.provider]}
                      </td>
                      <td>
                        <span className={`collector-state collector-state--${provider.state}`}>
                          {stateLabel(provider.state)}
                        </span>
                      </td>
                      <td>{provider.connected ? "已连接" : "未连接"}</td>
                      <td>{formatDateTime(provider.last_connected_at)}</td>
                      <td>{formatDateTime(provider.last_message_at)}</td>
                      <td>{formatDateTime(provider.last_success_at)}</td>
                      <td>{provider.consecutive_failures}</td>
                      <td>{provider.reconnect_count}</td>
                      <td>{provider.last_http_status ?? "-"}</td>
                      <td className="collector-error-cell">{provider.last_error || "无"}</td>
                      <td>{formatDateTime(provider.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </section>
  );
}
