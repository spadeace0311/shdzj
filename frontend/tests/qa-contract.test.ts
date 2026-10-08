import { describe, expect, test } from "vitest";

import {
  answerIdFromEvents,
  eventEpicenter,
  parseQaMapAction,
  qaAnswerAudit,
} from "../src/qa/qaContract";
import type { EventDetail, QaAnswer, QaStreamEvent } from "../src/types";

function detailWithCoordinates(
  longitude: unknown,
  latitude: unknown,
): EventDetail {
  return {
    id: "event-1",
    source: "test",
    place: "测试事件",
    magnitude: 5.2,
    depth_km: 10,
    origin_time: "2026-10-08T00:00:00Z",
    longitude: longitude as string | number,
    latitude: latitude as string | number,
    institutional_level: null,
    service_level: null,
    response_suggestion: null,
    response_rule_version: null,
    revision_no: 1,
    event_kind: "auto",
    lifecycle_state: "formal_triggered",
    t1_at: null,
  };
}

describe("QA frontend contract parsing", () => {
  test("extracts answer_id from any SSE event before answer_started", () => {
    const events: QaStreamEvent[] = [
      { type: "retrieval", data: { answer_id: "a1", count: 1 } },
      { type: "answer_delta", data: { answer_id: "a1", text: "部分结果" } },
    ];

    expect(answerIdFromEvents(events)).toBe("a1");
  });

  test("preserves frozen coordinates, feature IDs and dictionary set_layers", () => {
    expect(
      parseQaMapAction({
        action_type: "locate",
        target_ref: "fault:f1",
        reason: "定位断层",
        valid_until: "2026-10-08T00:10:00Z",
        coordinates: [121.5, 31.2],
        feature_id: "f1",
      }),
    ).toMatchObject({
      action_type: "locate",
      coordinates: [121.5, 31.2],
      feature_id: "f1",
    });

    expect(
      parseQaMapAction({
        action_type: "set_layers",
        reason: "只显示相关图层",
        valid_until: "2026-10-08T00:10:00Z",
        layers: {
          epicenter: true,
          faults: false,
        },
        visibility: {
          epicenter: true,
          faults: false,
          population: false,
        },
      }),
    ).toMatchObject({
      action_type: "set_layers",
      layers: {
        epicenter: true,
        faults: false,
      },
      visibility: {
        epicenter: true,
        faults: false,
        population: false,
      },
    });
  });

  test("does not turn null or empty coordinates into zero", () => {
    for (const value of [null, "", Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(eventEpicenter(detailWithCoordinates(value, value))).toBeUndefined();
    }
  });

  test("builds audit display data from the optional backend contract", () => {
    const answer: QaAnswer = {
      id: "a1",
      question_id: "q1",
      session_id: "s1",
      status: "completed",
      text: "回答",
      structured: null,
      citation_keys: [],
      degraded_reasons: [],
      duration_ms: null,
      created_at: "2026-10-08T00:00:00Z",
      updated_at: "2026-10-08T00:00:00Z",
      completed_at: null,
      citations: [],
      tool_calls: [],
      map_actions: [],
      model_name: "deepseek-chat",
      model_version: "chat-v1",
      prompt_version: "qa-2026-10-08",
      execution_plan: { intent: "knowledge_query" },
      tool_call_summary: [{ name: "fault.nearest", status: "ok" }],
    };
    expect(
      qaAnswerAudit(answer),
    ).toEqual({
      model_name: "deepseek-chat",
      model_version: "chat-v1",
      prompt_version: "qa-2026-10-08",
      execution_plan: { intent: "knowledge_query" },
      tool_call_summary: [{ name: "fault.nearest", status: "ok" }],
    });
  });
});
