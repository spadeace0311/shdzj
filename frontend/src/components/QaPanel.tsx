import { useEffect, useRef, useState } from "react";

import {
  createQaSession,
  getQaAnswer,
  sendQaFeedback,
  streamQaQuestion,
} from "../api/client";
import type {
  QaAnswer,
  QaCitation,
  QaMapAction,
  QaMapActionRecord,
  QaStreamEvent,
  QaToolCall,
} from "../types";
import { QaConversation } from "./QaConversation";
import { QaEvidencePanel } from "./QaEvidencePanel";
import { useMapActionPublisher } from "../qa/MapActionContext";

interface QaPanelProps {
  eventId: string;
  mode: "event" | "hall";
  onClose: () => void;
}

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
      !data.layers.every(
        (layer) => typeof layer === "string" && layer.length > 0,
      )
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
    if (
      id &&
      (event.type === "answer_completed" || event.type === "answer_started")
    ) {
      return id;
    }
  }
  return null;
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

export function QaPanel({ eventId, mode, onClose }: QaPanelProps) {
  const [events, setEvents] = useState<QaStreamEvent[]>([]);
  const [answer, setAnswer] = useState<QaAnswer | null>(null);
  const [mapActions, setMapActions] = useState<QaMapAction[]>([]);
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const sessionIntentRef = useRef(0);
  const sessionIdRef = useRef<string | null>(null);
  const sessionInFlightRef = useRef<Promise<string | null> | null>(null);
  const publish = useMapActionPublisher();

  useEffect(() => {
    sessionIntentRef.current += 1;
    sessionIdRef.current = null;
    sessionInFlightRef.current = null;
    setEvents([]);
    setAnswer(null);
    setMapActions([]);
    setBusy(false);
    abortRef.current?.abort();
    return () => {
      abortRef.current?.abort();
    };
  }, [eventId]);

  function assignSessionId(nextSessionId: string) {
    sessionIdRef.current = nextSessionId;
  }

  function ensureSession(targetEventId: string): Promise<string | null> {
    if (sessionIdRef.current) {
      return Promise.resolve(sessionIdRef.current);
    }
    if (sessionInFlightRef.current) {
      return sessionInFlightRef.current;
    }

    const requestId = ++sessionIntentRef.current;
    const pending = createQaSession({
      title: "当前问答",
      event_id: targetEventId,
    })
      .then((created) => {
        if (requestId !== sessionIntentRef.current) {
          return null;
        }
        assignSessionId(created.id);
        return created.id;
      })
      .catch(() => null)
      .finally(() => {
        if (requestId === sessionIntentRef.current) {
          sessionInFlightRef.current = null;
        }
      });
    sessionInFlightRef.current = pending;
    return pending;
  }

  async function handleAsk(question: string) {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setBusy(true);
    const nextSessionId = await ensureSession(eventId);
    if (!nextSessionId || controller.signal.aborted) {
      if (!controller.signal.aborted) {
        setBusy(false);
      }
      return;
    }

    setEvents([]);
    setAnswer(null);
    setMapActions([]);

    const collectedEvents: QaStreamEvent[] = [];
    try {
      await streamQaQuestion(
        nextSessionId,
        question,
        (event) => {
          if (controller.signal.aborted) {
            return;
          }
          if (event.type === "map_action") {
            const action = streamMapAction(event.data);
            if (action) {
              setMapActions((current) => [...current, action]);
              publish(eventId, action);
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

    const completedAnswerId = answerIdFromEvents(collectedEvents);
    if (!completedAnswerId) {
      return;
    }

    try {
      const persistedAnswer = await getQaAnswer(completedAnswerId);
      if (!controller.signal.aborted) {
        setAnswer(persistedAnswer);
        const recoveredActions = persistedAnswer.map_actions
          .map(mapActionRecordToAction)
          .filter((action): action is QaMapAction => action !== null);
        setMapActions((current) =>
          mergeMapActions(current, recoveredActions),
        );
        for (const action of recoveredActions) {
          publish(eventId, action);
        }
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
    <div className={`qa-panel-layer qa-panel-layer--${mode}`}>
      <div className="qa-panel__scrim" onClick={onClose} />
      <aside
        className={`qa-panel qa-panel--${mode}`}
        role="dialog"
        aria-label="事件上下文问答"
      >
        <header className="qa-panel__header">
          <div>
            <p className="eyebrow">智能问策</p>
            <h2>事件上下文问答</h2>
          </div>
          <button
            className="text-button"
            type="button"
            aria-label="关闭智能问策"
            onClick={onClose}
          >
            关闭
          </button>
        </header>
        <div className="qa-panel__content">
          <QaConversation
            events={events}
            answer={answer}
            busy={busy}
            onAsk={(question) => void handleAsk(question)}
            onFeedback={(answerId, helpful) =>
              void handleFeedback(answerId, helpful)
            }
          />
          <QaEvidencePanel
            citations={citations}
            toolCalls={toolCalls}
            mapActions={mapActions}
          />
        </div>
      </aside>
    </div>
  );
}
