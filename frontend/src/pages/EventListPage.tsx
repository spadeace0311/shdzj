import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { listEvents } from "../api/client";
import {
  formatDateTime,
  formatDepth,
  formatEventKind,
  formatInstitutionalLevel,
  formatMagnitude,
  formatServiceLevel,
  isTestOrDrill,
  type EventSummary,
} from "../types";

type ListStatus = "loading" | "ready" | "error";

export function EventListPage() {
  const [events, setEvents] = useState<EventSummary[]>([]);
  const [status, setStatus] = useState<ListStatus>("loading");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("all");

  const loadEvents = useCallback(async () => {
    setStatus("loading");
    try {
      setEvents(await listEvents());
      setStatus("ready");
    } catch {
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    void loadEvents();
  }, [loadEvents]);

  const normalizedQuery = query.trim().toLowerCase();
  const visibleEvents = events.filter((event) => {
    const matchesKind = kind === "all" || event.event_kind === kind;
    const matchesQuery =
      normalizedQuery === "" ||
      event.place.toLowerCase().includes(normalizedQuery) ||
      event.source.toLowerCase().includes(normalizedQuery);
    return matchesKind && matchesQuery;
  });

  return (
    <section className="page-section" aria-labelledby="event-list-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">当前事件</p>
          <h1 id="event-list-title">地震事件列表</h1>
        </div>
        <Link className="primary-button" to="/manual">
          创建人工事件
        </Link>
      </header>

      <div className="filter-bar">
        <label className="filter-field filter-field--search">
          <span>检索</span>
          <input
            type="search"
            placeholder="地点或来源"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label className="filter-field">
          <span>事件类型</span>
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="all">全部</option>
            <option value="auto">自动速报</option>
            <option value="formal">正式报告</option>
            <option value="correction">更正报告</option>
            <option value="manual">人工事件</option>
            <option value="test">测试</option>
            <option value="drill">演练</option>
          </select>
        </label>
      </div>

      {status === "loading" ? (
        <div className="state-panel">
          <span className="state-icon" aria-hidden="true" />
          <p>正在加载事件列表</p>
        </div>
      ) : null}

      {status === "error" ? (
        <div className="state-panel state-panel--error" role="alert">
          <p>无法加载事件列表</p>
          <button className="secondary-button" type="button" onClick={() => void loadEvents()}>
            重试
          </button>
        </div>
      ) : null}

      {status === "ready" && visibleEvents.length === 0 ? (
        <div className="state-panel">
          <p>{events.length === 0 ? "暂无地震事件" : "没有符合筛选条件的事件"}</p>
        </div>
      ) : null}

      {status === "ready" && visibleEvents.length > 0 ? (
        <div className="table-scroll">
          <table className="event-table">
            <thead>
              <tr>
                <th>发震时刻</th>
                <th>地点</th>
                <th>震级</th>
                <th>深度</th>
                <th>事件类型</th>
                <th>制度响应</th>
                <th>服务响应</th>
                <th>修订号</th>
              </tr>
            </thead>
            <tbody>
              {visibleEvents.map((event) => (
                <tr key={event.id}>
                  <td>{formatDateTime(event.origin_time)}</td>
                  <td>
                    <Link className="event-link" to={`/events/${event.id}`}>
                      {event.place}
                    </Link>
                  </td>
                  <td>{formatMagnitude(event.magnitude)}</td>
                  <td>{formatDepth(event.depth_km)}</td>
                  <td>
                    <span
                      className={`kind-tag kind-tag--${isTestOrDrill(event.event_kind) ? "drill" : "real"}`}
                    >
                      {formatEventKind(event.event_kind)}
                    </span>
                  </td>
                  <td>{formatInstitutionalLevel(event.institutional_level)}</td>
                  <td>{formatServiceLevel(event.service_level)}</td>
                  <td>R{event.revision_no}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
