import { useEffect, useRef, useState } from "react";

import {
  createQaSession,
  getEvent,
  getQaAnswer,
  listQaSessions,
  sendQaFeedback,
  streamQaQuestion,
} from "../api/client";
import type {
  EventDetail,
  QaAnswer,
  QaCitation,
  QaMapAction,
  QaMapActionRecord,
  QaSession,
  QaStreamEvent,
  QaToolCall,
} from "../types";
import { QaConversation } from "../components/QaConversation";
import { QaEvidencePanel } from "../components/QaEvidencePanel";
import { QaMap } from "../components/QaMap";

const NEW_SESSION_TITLE = "当前问答";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function streamMapAction(data: Record<string, unknown>): QaMapAction | null {
  const actionType = stringValue(data.action_type);
  const reason = stringValue(data.reason) ?? "";
  const validUntil = stringValue(data.valid_until) ?? "";
  if (!actionType) {
    return null;
  }

  if (actionType === "locate") {
    const targetRef = stringValue(data.target_ref);
    return targetRef
      ? {
          action_type: "locate",
          target_ref: targetRef,
          reason,
          valid_until: validUntil,
        }
      : null;
  }

  if (actionType === "fit_bounds") {
    const bounds = data.bounds;
    if (
      !Array.isArray(bounds) ||
      bounds.length !== 4 ||
      !bounds.every((value) => numberValue(value) !== null)
    ) {
      return null;
    }
    return {
      action_type: "fit_bounds",
      bounds: bounds as [number, number, number, number],
      reason,
      valid_until: validUntil,
    };
  }

  if (actionType === "buffer") {
    const targetRef = stringValue(data.target_ref);
    const radius = numberValue(data.radius_km);
    return targetRef && radius !== null
      ? {
          action_type: "buffer",
          target_ref: targetRef,
          radius_km: radius,
          reason,
          valid_until: validUntil,
        }
      : null;
  }

  if (actionType === "highlight") {
    const targetRef = stringValue(data.target_ref);
    const layerId = stringValue(data.layer_id);
    return targetRef && layerId
      ? {
          action_type: "highlight",
          target_ref: targetRef,
          layer_id: layerId,
          reason,
          valid_until: validUntil,
        }
      : null;
  }

  if (actionType === "set_layers") {
    if (
      !Array.isArray(data.layers) ||
      !data.layers.every((layer) => typeof layer === "string" && layer.length > 0)
    ) {
      return null;
    }
    return {
      action_type: "set_layers",
      layers: data.layers as string[],
      reason,
      valid_until: validUntil,
    };
  }

  return null;
}

function citationsFromEvents(events: QaStreamEvent[]): QaCitation[] {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!;
    if (event.type !== "answer_completed") {
      continue;
    }
    const citations = event.data.citations;
    if (
      Array.isArray(citations) &&
      citations.every((citation) => isRecord(citation))
    ) {
      return citations as unknown as QaCitation[];
    }
  }
  return [];
}

function toolsFromEvents(events: QaStreamEvent[]): QaToolCall[] {
  const seen = new Set<string>();
  const tools: QaToolCall[] = [];
  for (const event of events) {
    if (event.type !== "tool") {
      continue;
    }
    const name = stringValue(event.data.name);
    if (!name || seen.has(name)) {
      continue;
    }
    seen.add(name);
    tools.push({
      id: name,
      answer_id: stringValue(event.data.answer_id) ?? "",
      tool_name: name,
      tool_status: stringValue(event.data.status) ?? "ok",
      arguments: {},
      result: null,
      limitations: [],
      duration_ms: null,
      created_at: "",
    });
  }
  return tools;
}

function answerIdFromEvents(events: QaStreamEvent[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!;
    const id = stringValue(event.data.answer_id);
    if (id && (event.type === "answer_completed" || event.type === "answer_started")) {
      return id;
    }
  }
  return null;
}

function eventEpicenter(detail: EventDetail): [number, number] | undefined {
  const longitude = Number(detail.longitude);
  const latitude = Number(detail.latitude);
  if (
    !Number.isFinite(longitude) ||
    !Number.isFinite(latitude) ||
    longitude < -180 ||
    longitude > 180 ||
    latitude < -90 ||
    latitude > 90
  ) {
    return undefined;
  }
  return [longitude, latitude];
}

function mapActionRecordToAction(
  record: QaMapActionRecord,
): QaMapAction | null {
  const payload = isRecord(record.payload) ? record.payload : {};
  const validUntil =
    record.valid_until ?? stringValue(payload.valid_until) ?? "";
  return streamMapAction({
    ...payload,
    action_type: record.action_type,
    valid_until: validUntil,
  });
}

function mapActionKey(action: QaMapAction): string {
  return JSON.stringify(action);
}

function mergeMapActions(
  current: QaMapAction[],
  recovered: QaMapAction[],
): QaMapAction[] {
  const existing = new Set(current.map(mapActionKey));
  return [
    ...current,
    ...recovered.filter((action) => !existing.has(mapActionKey(action))),
  ];
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
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return <span>{String(value)}</span>;
  }
  return <span>{String(value)}</span>;
}

function StructuredResult({ value }: { value: Record<string, unknown> }) {
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

export function SmartQaPage() {
  const [sessions, setSessions] = useState<QaSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [events, setEvents] = useState<QaStreamEvent[]>([]);
  const [answer, setAnswer] = useState<QaAnswer | null>(null);
  const [mapActions, setMapActions] = useState<QaMapAction[]>([]);
  const [busy, setBusy] = useState(false);
  const [epicenter, setEpicenter] = useState<[number, number] | undefined>(
    undefined,
  );
  const abortRef = useRef<AbortController | null>(null);
  const sessionIntentRef = useRef(0);
  const currentSession =
    sessions.find((session) => session.id === currentSessionId) ?? null;

  useEffect(() => {
    let active = true;
    const requestId = ++sessionIntentRef.current;

    void listQaSessions()
      .then((loadedSessions) => {
        if (!active || requestId !== sessionIntentRef.current) {
          return;
        }
        setSessions(loadedSessions);
        if (loadedSessions[0]) {
          setCurrentSessionId(loadedSessions[0].id);
        }
      })
      .catch(() => {
        if (active && requestId === sessionIntentRef.current) {
          setSessions([]);
        }
      });

    return () => {
      active = false;
      abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    let active = true;
    const eventId = currentSession?.event_id;
    if (!eventId) {
      setEpicenter(undefined);
      return () => {
        active = false;
      };
    }

    void getEvent(eventId)
      .then((detail) => {
        if (active) {
          setEpicenter(eventEpicenter(detail));
        }
      })
      .catch(() => {
        if (active) {
          setEpicenter(undefined);
        }
      });

    return () => {
      active = false;
    };
  }, [currentSession?.event_id]);

  function clearConversation() {
    setEvents([]);
    setAnswer(null);
    setMapActions([]);
    setBusy(false);
  }

  async function createSession(title = NEW_SESSION_TITLE): Promise<string | null> {
    const requestId = ++sessionIntentRef.current;
    try {
      const created = await createQaSession({ title });
      if (requestId === sessionIntentRef.current) {
        setSessions((current) => [created, ...current]);
        setCurrentSessionId(created.id);
      }
      return created.id;
    } catch {
      return null;
    }
  }

  function handleNewSession() {
    sessionIntentRef.current += 1;
    abortRef.current?.abort();
    clearConversation();
    setCurrentSessionId(null);
    void createSession();
  }

  function handleSelectSession(sessionId: string) {
    if (sessionId === currentSessionId) {
      return;
    }
    sessionIntentRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    clearConversation();
    setCurrentSessionId(sessionId);
  }

  async function handleAsk(question: string) {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    let sessionId = currentSessionId;
    if (!sessionId) {
      sessionId = await createSession();
      if (!sessionId || controller.signal.aborted) {
        return;
      }
    }

    setEvents([]);
    setAnswer(null);
    setMapActions([]);
    setBusy(true);

    const collectedEvents: QaStreamEvent[] = [];
    try {
      await streamQaQuestion(
        sessionId,
        question,
        (event) => {
          if (controller.signal.aborted) {
            return;
          }
          if (event.type === "map_action") {
            const action = streamMapAction(event.data);
            if (action) {
              setMapActions((current) => [...current, action]);
            }
          }
          collectedEvents.push(event);
          setEvents([...collectedEvents]);
        },
        controller.signal,
      );
    } catch {
      if (!controller.signal.aborted) {
        collectedEvents.push({
          type: "error",
          data: { code: "stream_failed", recoverable: true },
        });
        setEvents([...collectedEvents]);
      }
    }

    if (controller.signal.aborted) {
      return;
    }
    setBusy(false);

    const answerId = answerIdFromEvents(collectedEvents);
    if (!answerId) {
      return;
    }
    try {
      const persistedAnswer = await getQaAnswer(answerId);
      if (!controller.signal.aborted) {
        setAnswer(persistedAnswer);
        const recoveredActions = persistedAnswer.map_actions
          .map(mapActionRecordToAction)
          .filter((action): action is QaMapAction => action !== null);
        setMapActions((current) =>
          mergeMapActions(current, recoveredActions),
        );
      }
    } catch {
      // Stream citations and tools remain available even if reloading fails.
    }
  }

  async function handleFeedback(answerId: string, helpful: boolean) {
    try {
      await sendQaFeedback(answerId, { helpful });
    } catch {
      // Feedback is best-effort and should not disrupt the conversation.
    }
  }

  const citations =
    answer?.citations ?? citationsFromEvents(events);
  const toolCalls =
    answer?.tool_calls ?? toolsFromEvents(events);

  return (
    <section className="page-section smart-qa-page" aria-label="智能问策">
      <header className="page-heading">
        <h1>智能问策</h1>
      </header>

      <div className="smart-qa-layout">
        <aside className="smart-qa-left" data-testid="qa-left-column">
          <div className="smart-qa-panel">
            <button
              className="primary-button"
              type="button"
              onClick={handleNewSession}
            >
              新建问题
            </button>
          </div>

          <div className="smart-qa-panel">
            <h2>历史会话</h2>
            {sessions.length === 0 ? (
              <p className="qa-empty">暂无历史会话</p>
            ) : (
              <div className="smart-qa-session-list">
                {sessions.map((session) => (
                  <button
                    className={
                      session.id === currentSessionId
                        ? "smart-qa-session smart-qa-session--active"
                        : "smart-qa-session"
                    }
                    type="button"
                    key={session.id}
                    onClick={() => handleSelectSession(session.id)}
                  >
                    {session.title}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="smart-qa-panel">
            <h2>当前事件</h2>
            <p className="smart-qa-event">
              {currentSession?.event_id ?? "未关联事件"}
            </p>
            {currentSession?.event_id && !epicenter ? (
              <p className="smart-qa-event-warning">震中坐标不可用</p>
            ) : null}
          </div>
        </aside>

        <main className="smart-qa-center" data-testid="qa-center-column">
          <QaConversation
            events={events}
            answer={answer}
            busy={busy}
            onAsk={(question) => void handleAsk(question)}
            onFeedback={(answerId, helpful) =>
              void handleFeedback(answerId, helpful)
            }
          />
        </main>

        <aside className="smart-qa-right" data-testid="qa-right-column">
          <QaEvidencePanel
            citations={citations}
            toolCalls={toolCalls}
            mapActions={mapActions}
          />
          {answer?.structured ? (
            <StructuredResult value={answer.structured} />
          ) : null}
          <QaMap actions={mapActions} epicenter={epicenter} />
        </aside>
      </div>
    </section>
  );
}
