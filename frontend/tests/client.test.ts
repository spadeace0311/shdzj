import { afterEach, beforeEach, expect, test, vi } from "vitest";

import {
  clearAccessToken,
  createManualEvent,
  getCurrentAssessment,
  getCollectorStatus,
  login,
  setAccessToken,
} from "../src/api/client";

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  clearAccessToken();
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("login sends OAuth2 form data with the expected content type", async () => {
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ access_token: "token-1", token_type: "bearer" }),
  } as Response);

  await login("operator", "secret");

  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/auth/login");
  expect(init.method).toBe("POST");
  expect(init.headers).toMatchObject({
    "Content-Type": "application/x-www-form-urlencoded",
  });
  expect(init.body).toBeInstanceOf(URLSearchParams);
  expect((init.body as URLSearchParams).get("username")).toBe("operator");
  expect((init.body as URLSearchParams).get("password")).toBe("secret");
});

test("createManualEvent sends a JSON body with bearer authorization", async () => {
  setAccessToken("token-1");
  fetchMock.mockResolvedValue({
    ok: true,
    status: 201,
    json: async () => ({ event_id: "event-1" }),
  } as Response);

  await createManualEvent({
    origin_time: "2026-09-17T10:30:00+08:00",
    longitude: "121.54",
    latitude: "31.22",
    magnitude: "3.2",
    depth_km: "8",
    source: "shanghai-network",
    event_kind: "manual",
  });

  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/events/manual");
  expect(init.method).toBe("POST");
  expect(init.headers).toMatchObject({
    "Content-Type": "application/json",
    Authorization: "Bearer token-1",
  });
  expect(JSON.parse(init.body as string)).toMatchObject({
    source: "shanghai-network",
    event_kind: "manual",
  });
});

test("getCollectorStatus requests the restricted collector endpoint", async () => {
  setAccessToken("test-access-token");
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      overall_state: "healthy",
      providers: [],
      open_dead_letter_count: 0,
      boundary_version: null,
      last_ingested_event_id: null,
    }),
  } as Response);

  await getCollectorStatus();

  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/collector/status");
  expect(init.headers).toMatchObject({
    Authorization: "Bearer test-access-token",
  });
});

test("getCurrentAssessment requests the event's latest run with bearer authorization", async () => {
  setAccessToken("test-access-token");
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({
      run_id: "run-1",
      event_id: "event-1",
      revision_id: "revision-1",
      run_no: 1,
      status: "pending",
      t1_at: "2026-09-26T01:00:00Z",
      deadline_at: "2026-09-26T01:05:00Z",
      completed_task_count: 0,
      failed_task_count: 0,
      total_task_count: 9,
      tasks: [],
    }),
  } as Response);

  await getCurrentAssessment("event-1");

  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/assessments/events/event-1/current");
  expect(init.headers).toMatchObject({
    Authorization: "Bearer test-access-token",
  });
});

test("getCurrentAssessment returns null when the event has no run", async () => {
  setAccessToken("test-access-token");
  fetchMock.mockResolvedValue({
    ok: false,
    status: 404,
    json: async () => ({ detail: "assessment_run_not_found" }),
  } as Response);

  await expect(getCurrentAssessment("event-1")).resolves.toBeNull();
});

test("createManualEvent parses FastAPI validation array detail", async () => {
  setAccessToken("token-1");
  fetchMock.mockResolvedValue({
    ok: false,
    status: 422,
    json: async () => ({
      detail: [
        {
          type: "less_than_equal",
          loc: ["body", "magnitude"],
          msg: "Input should be less than or equal to 10",
        },
      ],
    }),
  } as Response);

  await expect(
    createManualEvent({
      origin_time: "2026-09-17T10:30:00+08:00",
      longitude: "121.54",
      latitude: "31.22",
      magnitude: "12",
      depth_km: "8",
      source: "shanghai-network",
      event_kind: "manual",
    }),
  ).rejects.toMatchObject({
    status: 422,
    message: "Input should be less than or equal to 10",
  });
});

test("createManualEvent identifies a timed-out fetch", async () => {
  vi.useFakeTimers();
  setAccessToken("token-1");
  fetchMock.mockImplementation(
    (_url: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      }),
  );

  const request = createManualEvent({
    origin_time: "2026-09-17T10:30:00+08:00",
    longitude: "121.54",
    latitude: "31.22",
    magnitude: "3.2",
    depth_km: "8",
    source: "shanghai-network",
    event_kind: "manual",
  });
  const assertion = expect(request).rejects.toMatchObject({ status: 408 });

  await vi.advanceTimersByTimeAsync(10_001);
  await assertion;
  vi.useRealTimers();
});
