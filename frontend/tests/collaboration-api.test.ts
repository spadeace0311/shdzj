import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
  addCollaborationTextVersion,
  ApiError,
  cancelCollaborationTask,
  clearAccessToken,
  completeCollaborationTask,
  createTemporaryTask,
  getCollaborationTask,
  getCommandHallActiveEvent,
  getCommandHallGroup,
  getCommandHallOverview,
  getCommandHallTask,
  listCollaborationTasks,
  newIdempotencyKey,
  publishCollaborationDeliverableVersion,
  returnCollaborationTask,
  setAccessToken,
  startCollaborationTask,
  streamCommandHall,
  submitCollaborationTask,
  uploadCollaborationDeliverableVersion,
} from "../src/api/client";
import type { CommandHallStreamEvent, WorkgroupTask } from "../src/types";

const fetchMock = vi.fn();

const task: WorkgroupTask = {
  id: "task-1",
  event_id: "event-1",
  workgroup_code: "monitoring_forecast",
  task_code: "trend_consultation",
  title: "趋势会商",
  status: "pending",
  timeliness_state: "on_time",
  due_at: "2026-10-03T02:00:00Z",
  row_version: 4,
  instruction: "组织震后趋势会商",
  priority: 10,
  phase_code: "within_30_minutes",
  source_type: "preplan",
  source_ref: null,
  activated_at: "2026-10-03T01:00:00Z",
  completed_at: null,
  closed_at: null,
  created_at: "2026-10-03T01:00:00Z",
  updated_at: "2026-10-03T01:00:00Z",
  contributors: [],
  can_work: true,
  can_confirm: false,
};

const taskVersion = {
  id: "version-1",
  deliverable_id: "deliverable-1",
  version_no: 1,
  source_kind: "manual",
  artifact_id: null,
  artifact_publication_id: null,
  storage_key: "collaboration/result.pdf",
  file_name: "result.pdf",
  checksum: "a".repeat(64),
  mime_type: "application/pdf",
  size_bytes: 10,
  text_result: null,
  created_by: "operator",
  basis_text: "人工成果",
  supersedes_version_id: null,
  created_at: "2026-10-03T01:10:00Z",
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mutationRequest(index = 0): [string, RequestInit] {
  return fetchMock.mock.calls[index] as [string, RequestInit];
}

function isUuidV4(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
    value,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  clearAccessToken();
  fetchMock.mockReset();
  setAccessToken("test-access-token");
});

afterEach(() => {
  clearAccessToken();
  vi.unstubAllGlobals();
});

test("listCollaborationTasks reads an event task list with bearer auth", async () => {
  fetchMock.mockResolvedValue(jsonResponse([task]));

  await expect(listCollaborationTasks("event-1")).resolves.toEqual([task]);

  const [path, init] = mutationRequest();
  expect(path).toBe("/api/v1/events/event-1/collaboration/tasks");
  expect(init.method).toBeUndefined();
  expect(new Headers(init.headers).get("Authorization")).toBe(
    "Bearer test-access-token",
  );
});

test("getCollaborationTask reads task detail with bearer auth", async () => {
  fetchMock.mockResolvedValue(jsonResponse(task));

  await expect(getCollaborationTask("task-1")).resolves.toEqual(task);

  const [path, init] = mutationRequest();
  expect(path).toBe("/api/v1/collaboration/tasks/task-1");
  expect(new Headers(init.headers).get("Authorization")).toBe(
    "Bearer test-access-token",
  );
});

test("newIdempotencyKey falls back when randomUUID is unavailable", () => {
  const getRandomValues = globalThis.crypto.getRandomValues.bind(
    globalThis.crypto,
  );
  vi.stubGlobal("crypto", { getRandomValues } as Crypto);

  expect(isUuidV4(newIdempotencyKey())).toBe(true);
});

test("newIdempotencyKey falls back to Math.random without Web Crypto", () => {
  vi.stubGlobal("crypto", undefined);

  expect(isUuidV4(newIdempotencyKey())).toBe(true);
});

test("startCollaborationTask sends If-Match and an idempotency key", async () => {
  fetchMock.mockResolvedValue(jsonResponse({ ...task, status: "in_progress" }));

  await startCollaborationTask("task-1", 4);

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe("/api/v1/collaboration/tasks/task-1/start");
  expect(init.method).toBe("POST");
  expect(headers.get("If-Match")).toBe("4");
  expect(headers.get("Idempotency-Key")).toBeTruthy();
});

test("startCollaborationTask reuses a caller-provided idempotency key", async () => {
  fetchMock.mockImplementation(async () =>
    jsonResponse({ ...task, status: "in_progress" }),
  );

  await startCollaborationTask("task-1", 4, "retry-safe-key");
  await startCollaborationTask("task-1", 4, "retry-safe-key");

  const firstHeaders = Object.fromEntries(
    new Headers(mutationRequest(0)[1].headers).entries(),
  );
  const secondHeaders = Object.fromEntries(
    new Headers(mutationRequest(1)[1].headers).entries(),
  );
  expect(firstHeaders).toEqual(secondHeaders);
  expect(firstHeaders["idempotency-key"]).toBe("retry-safe-key");
});

test("submitCollaborationTask sends the result text and optimistic version", async () => {
  fetchMock.mockResolvedValue(
    jsonResponse({ ...task, status: "pending_review" }),
  );

  await submitCollaborationTask("task-1", 4, {
    result_text: "趋势会商完成",
  });

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe("/api/v1/collaboration/tasks/task-1/submit");
  expect(init.method).toBe("POST");
  expect(headers.get("If-Match")).toBe("4");
  expect(headers.get("Idempotency-Key")).toBeTruthy();
  expect(JSON.parse(init.body as string)).toEqual({
    result_text: "趋势会商完成",
  });
});

test("returnCollaborationTask sends the required reason", async () => {
  fetchMock.mockResolvedValue(jsonResponse({ ...task, status: "in_progress" }));

  await returnCollaborationTask("task-1", 4, "补充震源机制分析");

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe("/api/v1/collaboration/tasks/task-1/return");
  expect(headers.get("If-Match")).toBe("4");
  expect(headers.get("Idempotency-Key")).toBeTruthy();
  expect(JSON.parse(init.body as string)).toEqual({
    reason: "补充震源机制分析",
  });
});

test("completeCollaborationTask sends If-Match and idempotency key", async () => {
  fetchMock.mockResolvedValue(
    jsonResponse({
      id: "task-1",
      status: "completed",
      row_version: 5,
    }),
  );

  await completeCollaborationTask("task-1", 4);

  const [, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(headers.get("If-Match")).toBe("4");
  expect(headers.get("Idempotency-Key")).toBeTruthy();
});

test("cancelCollaborationTask sends the reason with optimistic concurrency", async () => {
  fetchMock.mockResolvedValue(jsonResponse({ ...task, status: "not_required" }));

  await cancelCollaborationTask(
    "task-1",
    4,
    "平台标记不再需要",
    "cancel-key",
  );

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe("/api/v1/collaboration/tasks/task-1/cancel");
  expect(init.method).toBe("POST");
  expect(headers.get("If-Match")).toBe("4");
  expect(headers.get("Idempotency-Key")).toBe("cancel-key");
  expect(JSON.parse(init.body as string)).toEqual({
    reason: "平台标记不再需要",
  });
});

test("uploadCollaborationDeliverableVersion posts multipart data", async () => {
  fetchMock.mockResolvedValue(jsonResponse(taskVersion));
  const file = new File(["result"], "result.pdf", {
    type: "application/pdf",
  });

  await expect(
    uploadCollaborationDeliverableVersion(
      "deliverable-1",
      file,
      "人工修订依据",
      "upload-key",
    ),
  ).resolves.toEqual(taskVersion);

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe(
    "/api/v1/collaboration/deliverables/deliverable-1/versions/file",
  );
  expect(init.method).toBe("POST");
  expect(headers.get("Authorization")).toBe("Bearer test-access-token");
  expect(headers.get("Idempotency-Key")).toBe("upload-key");
  expect(headers.get("Content-Type")).toBeNull();
  expect((init.body as FormData).get("file")).toBe(file);
  expect((init.body as FormData).get("mime_type")).toBe("application/pdf");
  expect((init.body as FormData).get("basis_text")).toBe("人工修订依据");
});

test("addCollaborationTextVersion posts a structured text result", async () => {
  fetchMock.mockResolvedValue(
    jsonResponse({
      ...taskVersion,
      storage_key: null,
      file_name: null,
      mime_type: null,
      text_result: { text: "已完成震情核对" },
    }),
  );

  await addCollaborationTextVersion(
    "deliverable-1",
    { text: "已完成震情核对" },
    "组长核验",
    "text-version-key",
  );

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe(
    "/api/v1/collaboration/deliverables/deliverable-1/versions",
  );
  expect(init.method).toBe("POST");
  expect(headers.get("Idempotency-Key")).toBe("text-version-key");
  expect(JSON.parse(init.body as string)).toEqual({
    text_result: { text: "已完成震情核对" },
    basis_text: "组长核验",
  });
});

test("publishCollaborationDeliverableVersion confirms the selected version", async () => {
  fetchMock.mockResolvedValue(
    jsonResponse({
      id: "publication-2",
      deliverable_id: "deliverable-1",
      version_id: "version-1",
      published_by: "leader",
      published_role: "leader",
      published_at: "2026-10-03T01:20:00Z",
      superseded_at: null,
      publication_note: "确认发布",
      created_at: "2026-10-03T01:20:00Z",
    }),
  );

  await publishCollaborationDeliverableVersion(
    "deliverable-1",
    "version-1",
    4,
    "确认发布",
    "publish-key",
  );

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe(
    "/api/v1/collaboration/deliverables/deliverable-1/publish",
  );
  expect(init.method).toBe("POST");
  expect(headers.get("If-Match")).toBe("4");
  expect(headers.get("Idempotency-Key")).toBe("publish-key");
  expect(JSON.parse(init.body as string)).toEqual({
    version_id: "version-1",
    publication_note: "确认发布",
  });
});

test("createTemporaryTask posts the backend fields with an idempotency key", async () => {
  fetchMock.mockResolvedValue(jsonResponse(task, 201));

  await createTemporaryTask("event-1", {
    workgroup_code: "comprehensive_coordination",
    title: "临时协调任务",
    instruction: "联系相关单位确认信息",
    priority: 20,
    due_at: "2026-10-03T02:00:00Z",
    continues_until_cancelled: false,
    source_ref: "manual-1",
  });

  const [path, init] = mutationRequest();
  const headers = new Headers(init.headers);
  expect(path).toBe("/api/v1/events/event-1/collaboration/tasks");
  expect(init.method).toBe("POST");
  expect(headers.get("If-Match")).toBeNull();
  expect(headers.get("Idempotency-Key")).toBeTruthy();
  expect(JSON.parse(init.body as string)).toEqual({
    workgroup_code: "comprehensive_coordination",
    title: "临时协调任务",
    instruction: "联系相关单位确认信息",
    priority: 20,
    due_at: "2026-10-03T02:00:00Z",
    continues_until_cancelled: false,
    source_ref: "manual-1",
  });
});

test("all collaboration task writes accept explicit idempotency keys", async () => {
  fetchMock.mockImplementation(async () => jsonResponse(task));

  await submitCollaborationTask("task-1", 4, {}, "submit-key");
  await returnCollaborationTask("task-1", 4, "补充说明", "return-key");
  await completeCollaborationTask("task-1", 4, "complete-key");
  await createTemporaryTask(
    "event-1",
    {
      workgroup_code: "comprehensive_coordination",
      title: "临时协调任务",
      instruction: "联系相关单位确认信息",
      priority: 20,
    },
    "temporary-key",
  );

  expect(
    fetchMock.mock.calls.map(([, init]) =>
      new Headers((init as RequestInit).headers).get("Idempotency-Key"),
    ),
  ).toEqual(["submit-key", "return-key", "complete-key", "temporary-key"]);
});

test("command hall read helpers use authenticated backend routes", async () => {
  fetchMock
    .mockResolvedValueOnce(jsonResponse({ event_id: "event-1" }))
    .mockResolvedValueOnce(jsonResponse({ event_id: "event-1", groups: [] }))
    .mockResolvedValueOnce(jsonResponse({ event_id: "event-1", tasks: [] }))
    .mockResolvedValueOnce(jsonResponse({ id: "task-1", task: {} }));

  await getCommandHallActiveEvent();
  await getCommandHallOverview("event-1");
  await getCommandHallGroup("event-1", "monitoring_forecast");
  await getCommandHallTask("task-1");

  expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
    "/api/v1/command-hall/active-event",
    "/api/v1/command-hall/events/event-1/overview",
    "/api/v1/command-hall/events/event-1/groups/monitoring_forecast",
    "/api/v1/command-hall/tasks/task-1",
  ]);
  for (const [, init] of fetchMock.mock.calls as Array<[string, RequestInit]>) {
    expect(new Headers(init.headers).get("Authorization")).toBe(
      "Bearer test-access-token",
    );
  }
});

test("streamCommandHall parses split named SSE events with bearer auth", async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          'event: projection.updated\ndata: {"event_id":"event-1",',
        ),
      );
      controller.enqueue(
        encoder.encode(
          '"projection_version":2}\n\n: heartbeat\n\n' +
            'event: task.alert.created\ndata: {"task_id":"task-1"}\n\n',
        ),
      );
      controller.close();
    },
  });
  fetchMock.mockResolvedValue(
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream; charset=utf-8" },
    }),
  );
  const events: CommandHallStreamEvent[] = [];
  const controller = new AbortController();

  await streamCommandHall(
    "event-1",
    (event) => events.push(event),
    controller.signal,
  );

  const [path, init] = mutationRequest();
  expect(path).toBe("/api/v1/command-hall/events/event-1/stream");
  expect(init.signal).toBe(controller.signal);
  expect(new Headers(init.headers).get("Authorization")).toBe(
    "Bearer test-access-token",
  );
  expect(events).toEqual([
    {
      type: "projection.updated",
      data: { event_id: "event-1", projection_version: 2 },
    },
    {
      type: "task.alert.created",
      data: { task_id: "task-1" },
    },
  ]);
});

test("streamCommandHall handles split boundaries, multiline data, and a final frame without a delimiter", async () => {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode('event: projection.updated\ndata: {"event_id":"event-1",\n'),
      );
      controller.enqueue(
        encoder.encode('data: "projection_version":3}\r\n\r'),
      );
      controller.enqueue(
        encoder.encode(
          '\nevent: task.alert.created\ndata: {"task_id":"task-1"}',
        ),
      );
      controller.close();
    },
  });
  fetchMock.mockResolvedValue(
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
  const events: CommandHallStreamEvent[] = [];

  await streamCommandHall(
    "event-1",
    (event) => events.push(event),
    new AbortController().signal,
  );

  expect(events).toEqual([
    {
      type: "projection.updated",
      data: { event_id: "event-1", projection_version: 3 },
    },
    {
      type: "task.alert.created",
      data: { task_id: "task-1" },
    },
  ]);
});

test("streamCommandHall throws ApiError for non-2xx responses", async () => {
  fetchMock.mockResolvedValue(
    new Response(JSON.stringify({ detail: "unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    }),
  );

  await expect(
    streamCommandHall("event-1", () => undefined, new AbortController().signal),
  ).rejects.toEqual(
    expect.objectContaining<ApiError>({
      name: "ApiError",
      status: 401,
      message: "unauthorized",
    }),
  );
});

test("streamCommandHall requires an access token before fetching", async () => {
  clearAccessToken();

  await expect(
    streamCommandHall("event-1", () => undefined, new AbortController().signal),
  ).rejects.toEqual(
    expect.objectContaining({
      name: "ApiError",
      status: 401,
    }),
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

test("streamCommandHall cancels the reader when its signal aborts", async () => {
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    pull() {
      return new Promise(() => undefined);
    },
    cancel() {
      cancelled = true;
    },
  });
  fetchMock.mockResolvedValue(
    new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
  );
  const controller = new AbortController();
  const stream = streamCommandHall(
    "event-1",
    () => undefined,
    controller.signal,
  );

  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  controller.abort();

  await expect(stream).resolves.toBeUndefined();
  expect(cancelled).toBe(true);
});
