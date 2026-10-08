import { afterEach, expect, test, vi } from "vitest";

import {
  clearAccessToken,
  setAccessToken,
  streamQaQuestion,
} from "../src/api/client";


afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});


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
