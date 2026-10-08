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
  QaAnswer,
  QaCitation,
  QaMapAction,
  QaSession,
  QaStreamEvent,
  QaToolCall,
} from "../types";
import { QaConversation } from "../components/QaConversation";
import {
  QaEvidencePanel,
} from "../components/QaEvidencePanel";
import { QaMap } from "../components/QaMap";
import {
  answerIdFromEvents,
  eventEpicenter,
  mapActionRecordToAction,
  parseQaMapAction,
  qaAnswerAudit,
} from "../qa/qaContract";

const NEW_SESSION_TITLE = "当前问答";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
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

interface ConversationState {
  events: QaStreamEvent[];
  answer: QaAnswer | null;
  mapActions: QaMapAction[];
  busy: boolean;
}

const EMPTY_CONVERSATION: ConversationState = {
  events: [],
  answer: null,
  mapActions: [],
  busy: false,
};

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
  const conversationStatesRef = useRef<
    Record<string, ConversationState>
  >({});
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
          activateConversation(loadedSessions[0].id);
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

  function saveConversation(sessionId: string, state: ConversationState) {
    conversationStatesRef.current = {
      ...conversationStatesRef.current,
      [sessionId]: state,
    };
    setEvents(state.events);
    setAnswer(state.answer);
    setMapActions(state.mapActions);
    setBusy(state.busy);
  }

  function activateConversation(sessionId: string) {
    const state =
      conversationStatesRef.current[sessionId] ?? EMPTY_CONVERSATION;
    setEvents(state.events);
    setAnswer(state.answer);
    setMapActions(state.mapActions);
    setBusy(state.busy);
  }

  function suspendCurrentConversation() {
    const sessionId = currentSessionId;
    if (!sessionId) {
      return;
    }
    conversationStatesRef.current = {
      ...conversationStatesRef.current,
      [sessionId]: {
        events,
        answer,
        mapActions,
        busy: false,
      },
    };
  }

  async function createSession(title = NEW_SESSION_TITLE): Promise<string | null> {
    const requestId = ++sessionIntentRef.current;
    try {
      const created = await createQaSession({ title });
      if (requestId === sessionIntentRef.current) {
        setSessions((current) => [created, ...current]);
        setCurrentSessionId(created.id);
        activateConversation(created.id);
      }
      return created.id;
    } catch {
      return null;
    }
  }

  function handleNewSession() {
    sessionIntentRef.current += 1;
    abortRef.current?.abort();
    suspendCurrentConversation();
    saveConversation("__draft__", EMPTY_CONVERSATION);
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
    suspendCurrentConversation();
    setCurrentSessionId(sessionId);
    activateConversation(sessionId);
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

    const collectedEvents: QaStreamEvent[] = [];
    let collectedMapActions: QaMapAction[] = [];
    saveConversation(sessionId, {
      events: [],
      answer: null,
      mapActions: [],
      busy: true,
    });
    try {
      await streamQaQuestion(
        sessionId,
        question,
        (event) => {
          if (controller.signal.aborted) {
            return;
          }
          if (event.type === "map_action") {
            const action = parseQaMapAction(event.data);
            if (action) {
              collectedMapActions = [...collectedMapActions, action];
            }
          }
          collectedEvents.push(event);
          saveConversation(sessionId, {
            events: [...collectedEvents],
            answer: null,
            mapActions: collectedMapActions,
            busy: true,
          });
        },
        controller.signal,
      );
    } catch {
      if (!controller.signal.aborted) {
        collectedEvents.push({
          type: "error",
          data: { code: "stream_failed", recoverable: true },
        });
        saveConversation(sessionId, {
          events: [...collectedEvents],
          answer: null,
          mapActions: collectedMapActions,
          busy: false,
        });
      }
    }

    if (controller.signal.aborted) {
      return;
    }
    saveConversation(sessionId, {
      events: [...collectedEvents],
      answer: null,
      mapActions: collectedMapActions,
      busy: false,
    });

    const answerId = answerIdFromEvents(collectedEvents);
    if (!answerId) {
      return;
    }
    try {
      const persistedAnswer = await getQaAnswer(answerId);
      if (!controller.signal.aborted) {
        const recoveredActions = persistedAnswer.map_actions
          .map(mapActionRecordToAction)
          .filter((action): action is QaMapAction => action !== null);
        const nextActions = mergeMapActions(
          collectedMapActions,
          recoveredActions,
        );
        saveConversation(sessionId, {
          events: [...collectedEvents],
          answer: persistedAnswer,
          mapActions: nextActions,
          busy: false,
        });
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
            structured={answer?.structured ?? null}
            degradedReasons={answer?.degraded_reasons ?? []}
            audit={qaAnswerAudit(answer ?? {
              model_name: null,
              model_version: null,
              prompt_version: null,
              execution_plan: null,
              tool_call_summary: [],
            })}
          />
          <QaMap actions={mapActions} epicenter={epicenter} />
        </aside>
      </div>
    </section>
  );
}
