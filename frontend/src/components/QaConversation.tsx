import { useState } from "react";

import type { QaAnswer, QaStreamEvent } from "../types";

interface QaConversationProps {
  events: QaStreamEvent[];
  answer: QaAnswer | null;
  busy: boolean;
  onAsk: (question: string) => void;
  onFeedback: (answerId: string, helpful: boolean) => void;
}

function streamText(events: QaStreamEvent[]): string {
  return events
    .map((event) =>
      event.type === "answer_delta" && typeof event.data.text === "string"
        ? event.data.text
        : "",
    )
    .join("");
}

function toolNames(events: QaStreamEvent[]): string[] {
  const names: string[] = [];
  for (const event of events) {
    if (
      event.type === "tool" &&
      typeof event.data.name === "string" &&
      !names.includes(event.data.name)
    ) {
      names.push(event.data.name);
    }
  }
  return names;
}

function lastError(events: QaStreamEvent[]): QaStreamEvent | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]!.type === "error") {
      return events[index]!;
    }
  }
  return null;
}

function answerId(
  events: QaStreamEvent[],
  answer: QaAnswer | null,
): string | null {
  if (answer?.id) {
    return answer.id;
  }
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!;
    if (
      typeof event.data.answer_id === "string" &&
      event.data.answer_id.length > 0
    ) {
      return event.data.answer_id;
    }
  }
  return null;
}

export function QaConversation({
  events,
  answer,
  busy,
  onAsk,
  onFeedback,
}: QaConversationProps) {
  const [question, setQuestion] = useState("");
  const text = answer?.text ?? streamText(events);
  const completed =
    answer?.status === "completed" ||
    events.some((event) => event.type === "answer_completed");
  const tools = toolNames(events);
  const error = lastError(events);
  const currentAnswerId = answerId(events, answer);

  function submit() {
    const trimmed = question.trim();
    if (!trimmed || busy) {
      return;
    }
    onAsk(trimmed);
  }

  return (
    <section className="qa-conversation" aria-label="智能问策对话">
      <div className="qa-conversation__input">
        <label htmlFor="qa-question">问题</label>
        <textarea
          id="qa-question"
          value={question}
          disabled={busy}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <button
          className="primary-button"
          type="button"
          disabled={busy || question.trim().length === 0}
          onClick={submit}
        >
          提问
        </button>
      </div>

      <div className="qa-conversation__body">
        {busy && !text ? <p className="qa-conversation__status">生成中</p> : null}
        {text ? (
          <div
            className={`qa-answer${completed ? " qa-answer--final" : ""}`}
            data-testid="qa-answer"
          >
            {text}
          </div>
        ) : null}

        {tools.length > 0 ? (
          <ul className="qa-tool-list">
            {tools.map((tool) => (
              <li key={tool}>{tool}</li>
            ))}
          </ul>
        ) : null}

        {error ? (
          <div className="qa-conversation__error" role="status">
            回答中断，可重试
            {question.trim() && !busy ? (
              <button
                className="text-button"
                type="button"
                onClick={() => onAsk(question.trim())}
              >
                重试
              </button>
            ) : null}
          </div>
        ) : null}

        {currentAnswerId && completed ? (
          <div className="qa-feedback">
            <button
              type="button"
              onClick={() => onFeedback(currentAnswerId, true)}
            >
              有帮助
            </button>
            <button
              type="button"
              onClick={() => onFeedback(currentAnswerId, false)}
            >
              无帮助
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
