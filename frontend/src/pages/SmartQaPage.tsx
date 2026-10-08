import { useEffect, useRef, useState } from "react";

import {
  createQaSession,
  getQaAnswer,
  listQaSessions,
  sendQaFeedback,
  streamQaQuestion,
} from "../api/client";
import type {
  QaAnswer,
  QaCitation,
  QaMapAction,
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

export function SmartQaPage() {
  const [sessions, setSessions] = useState<QaSession[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [events, setEvents] = useState<QaStreamEvent[]>([]);
  const [answer, setAnswer] = useState<QaAnswer | null>(null);
  const [mapActions, setMapActions] = useState<QaMapAction[]>([]);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let active = true;

    void listQaSessions()
      .then((loadedSessions) => {
        if (!active) {
          return;
        }
        setSessions(loadedSessions);
        if (loadedSessions[0]) {
          setCurrentSessionId(loadedSessions[0].id);
        }
      })
      .catch(() => {
        if (active) {
          setSessions([]);
        }
      });

    return () => {
      active = false;
      abortRef.current?.abort();
    };
  }, []);

  const currentSession =
    sessions.find((session) => session.id === currentSessionId) ?? null;

  function clearConversation() {
    setEvents([]);
    setAnswer(null);
    setMapActions([]);
    setBusy(false);
  }

  async function createSession(title = NEW_SESSION_TITLE): Promise<string | null> {
    try {
      const created = await createQaSession({ title });
      setSessions((current) => [created, ...current]);
      setCurrentSessionId(created.id);
      return created.id;
    } catch {
      return null;
    }
  }

  function handleNewSession() {
    abortRef.current?.abort();
    clearConversation();
    setCurrentSessionId(null);
    void createSession();
  }

  function handleSelectSession(sessionId: string) {
    if (sessionId === currentSessionId) {
      return;
    }
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
          <QaMap actions={mapActions} />
        </aside>
      </div>
    </section>
  );
}

