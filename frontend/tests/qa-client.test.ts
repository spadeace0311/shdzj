import { afterEach, expect, test, vi } from "vitest";

import {
  clearAccessToken,
  getQaAnswer,
  setAccessToken,
  streamQaQuestion,
} from "../src/api/client";
import type { QaMapActionRecord } from "../src/types";


afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

function responseWithSpiedReader(
  chunks: Uint8Array[],
  headers: Record<string, string> = {
    "Content-Type": "text/event-stream",
  },
): {
  response: Response;
  releaseLock: () => ReturnType<typeof vi.fn> | undefined;
  cancel: () => ReturnType<typeof vi.fn> | undefined;
} {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(chunk);
      }
    },
  });
  const response = new Response(stream, { status: 200, headers });
  let releaseLock: ReturnType<typeof vi.fn> | undefined;
  let cancel: ReturnType<typeof vi.fn> | undefined;

  vi.spyOn(response.body!, "getReader").mockImplementation(() => {
    // The spy replaces the body method, so the bound prototype method must be
    // called directly to avoid recursive lookup.
    const originalReader = (
      Object.getPrototypeOf(response.body) as ReadableStream<Uint8Array>
    ).getReader.call(response.body);
    releaseLock = vi.spyOn(
      originalReader,
      "releaseLock",
    ) as unknown as ReturnType<typeof vi.fn>;
    cancel = vi.spyOn(
      originalReader,
      "cancel",
    ) as unknown as ReturnType<typeof vi.fn>;
    return originalReader;
  });

  return {
    response,
    releaseLock: () => releaseLock,
    cancel: () => cancel,
  };
}


test("parses named SSE events split across chunks", async () => {
  setAccessToken("test-token");
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode("event: retrieval\ndata: {\"answer_id\":\"a1\"}\n\n"));
      controller.enqueue(encoder.encode("event: answer_delta\ndata: {\"text\":\"结\"}\n\n"));
      controller.close();
    },
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );
  const events: unknown[] = [];
  await streamQaQuestion("s1", "问题", (event) => events.push(event));
  expect(events).toEqual([
    { type: "retrieval", data: { answer_id: "a1" } },
    { type: "answer_delta", data: { text: "结" } },
  ]);
});


test("decodes UTF-8 SSE events split at every byte boundary", async () => {
  setAccessToken("test-token");
  const encoder = new TextEncoder();
  const bytes = encoder.encode('event: answer_delta\ndata: {"text":"结果"}\n\n');
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const byte of bytes) {
        controller.enqueue(Uint8Array.of(byte));
      }
      controller.close();
    },
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );

  const events: unknown[] = [];
  await streamQaQuestion("s1", "问题", (event) => events.push(event));

  expect(events).toEqual([
    { type: "answer_delta", data: { text: "结果" } },
  ]);
});


test("handles CRLF, heartbeat comments, missing event, and multiline data", async () => {
  setAccessToken("test-token");
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(": heartbeat\r\n\r\n"));
      controller.enqueue(encoder.encode('data: {"ignored":true}\r\n\r\n'));
      controller.enqueue(
        encoder.encode('event: answer_delta\r\ndata: {"text":\r\ndata: "多行"}\r\n\r\n'),
      );
      controller.close();
    },
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );

  const events: unknown[] = [];
  await streamQaQuestion("s1", "问题", (event) => events.push(event));

  expect(events).toEqual([
    { type: "answer_delta", data: { text: "多行" } },
  ]);
});


test("flushes a final SSE frame without a trailing delimiter", async () => {
  setAccessToken("test-token");
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode('event: answer_completed\ndata: {"answer_id":"a1","status":"completed"}'),
      );
      controller.close();
    },
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );

  const events: unknown[] = [];
  await streamQaQuestion("s1", "问题", (event) => events.push(event));

  expect(events).toEqual([
    {
      type: "answer_completed",
      data: { answer_id: "a1", status: "completed" },
    },
  ]);
});


test("throws ApiError when SSE data is not valid JSON", async () => {
  setAccessToken("test-token");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("event: retrieval\ndata: not-json\n\n"));
      controller.close();
    },
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );

  await expect(
    streamQaQuestion("s1", "问题", () => undefined),
  ).rejects.toMatchObject({ name: "ApiError", status: 0 });
});

test("rejects a successful non-SSE response before reading its body", async () => {
  setAccessToken("test-token");
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );

  await expect(
    streamQaQuestion("s1", "问题", () => undefined),
  ).rejects.toThrow("事件流响应格式错误");
});

test("reports direct fetch rejection as an explicit stream connection error", async () => {
  setAccessToken("test-token");
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("offline"));

  await expect(
    streamQaQuestion("s1", "问题", () => undefined),
  ).rejects.toMatchObject({
    name: "ApiError",
    status: 0,
    message: "事件流连接失败，请稍后重试",
  });
});

test("cancels and releases the reader after an SSE parse error", async () => {
  setAccessToken("test-token");
  const { response, releaseLock, cancel } = responseWithSpiedReader([
    new TextEncoder().encode("event: retrieval\ndata: not-json\n\n"),
  ]);
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response);

  await expect(
    streamQaQuestion("s1", "问题", () => undefined),
  ).rejects.toMatchObject({ name: "ApiError", status: 0 });

  expect(cancel()).toHaveBeenCalled();
  expect(releaseLock()).toHaveBeenCalled();
});

test("cancels and releases the reader after a network read error", async () => {
  setAccessToken("test-token");
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode(
          'event: answer_delta\ndata: {"answer_id":"a1","text":"部分"}\n\n',
        ),
      );
    },
    pull(controller) {
      controller.error(new TypeError("network interrupted"));
    },
  });
  const response = new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
  let releaseLock: ReturnType<typeof vi.fn> | undefined;
  let cancel: ReturnType<typeof vi.fn> | undefined;
  vi.spyOn(response.body!, "getReader").mockImplementation(() => {
    const reader = (
      Object.getPrototypeOf(response.body) as ReadableStream<Uint8Array>
    ).getReader.call(response.body);
    releaseLock = vi.spyOn(
      reader,
      "releaseLock",
    ) as unknown as ReturnType<typeof vi.fn>;
    cancel = vi.spyOn(
      reader,
      "cancel",
    ) as unknown as ReturnType<typeof vi.fn>;
    return reader;
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(response);

  await expect(
    streamQaQuestion("s1", "问题", () => undefined),
  ).rejects.toThrow("事件流连接中断，请稍后重试");

  expect(cancel).toHaveBeenCalled();
  expect(releaseLock).toHaveBeenCalled();
});


test("sends bearer token and reports stream errors", async () => {
  setAccessToken("test-token");
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ detail: "qa unavailable" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    }),
  );

  await expect(
    streamQaQuestion("s1", "问题", () => undefined),
  ).rejects.toThrow("qa unavailable");

  expect(vi.mocked(globalThis.fetch).mock.calls[0][1]?.headers).toMatchObject({
    Authorization: "Bearer test-token",
  });
});


test("getQaAnswer returns persisted map action records", async () => {
  setAccessToken("test-token");
  const persistedAction = {
    id: "ma1",
    answer_id: "a1",
    action_type: "locate",
    payload: {
      target_ref: "event:epicenter",
      reason: "定位震中",
    },
    valid_until: null,
    created_at: "2026-10-08T00:00:00Z",
  };
  const answer = {
    id: "a1",
    question_id: "q1",
    session_id: "s1",
    status: "completed",
    text: "回答",
    structured: null,
    citation_keys: [],
    degraded_reasons: [],
    duration_ms: 120,
    created_at: "2026-10-08T00:00:00Z",
    updated_at: "2026-10-08T00:00:01Z",
    completed_at: "2026-10-08T00:00:01Z",
    citations: [],
    tool_calls: [],
    map_actions: [persistedAction],
  };
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(answer), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );

  const result = await getQaAnswer("a1");
  const action: QaMapActionRecord = result.map_actions[0]!;

  expect(action).toEqual(persistedAction);
  expect(action.valid_until).toBeNull();
  expect(action.payload).toEqual({
    target_ref: "event:epicenter",
    reason: "定位震中",
  });
  expect(vi.mocked(globalThis.fetch).mock.calls[0][0]).toBe(
    "/api/v1/qa/answers/a1",
  );
  expect(vi.mocked(globalThis.fetch).mock.calls[0][1]?.headers).toMatchObject({
    Authorization: "Bearer test-token",
  });
});


test("aborting the stream resolves without surfacing user cancellation", async () => {
  setAccessToken("test-token");
  const controller = new AbortController();
  const stream = new ReadableStream<Uint8Array>({
    pull() {
      return new Promise(() => undefined);
    },
  });
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
  );
  const onEvent = vi.fn();

  const request = streamQaQuestion(
    "s1",
    "问题",
    onEvent,
    controller.signal,
  );
  controller.abort();

  await expect(request).resolves.toBeUndefined();
  expect(onEvent).not.toHaveBeenCalled();
});
