import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import type { EventIngestResponse } from "../src/types";

const USERNAME = process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin";
const API_BASE_URL = (process.env.E2E_API_BASE_URL ?? "").replace(/\/+$/, "");

function superadminPassword(): string {
  const password = process.env.E2E_SUPERADMIN_PASSWORD;
  if (!password) {
    throw new Error(
      "E2E_SUPERADMIN_PASSWORD is not set. Export the Compose superadmin password before running Playwright.",
    );
  }
  return password;
}

function apiUrl(path: string): string {
  return API_BASE_URL ? `${API_BASE_URL}${path}` : path;
}

async function buildIsolatedEventFixture(
  request: APIRequestContext,
  runId: string,
): Promise<{ originTime: string; longitude: number; latitude: number }> {
  const response = await request.get(apiUrl("/api/v1/events"));
  expect(response.status()).toBe(200);
  const events = (await response.json()) as Array<{ origin_time: string }>;
  const existingTimes = events
    .map((event) => Date.parse(event.origin_time))
    .filter(Number.isFinite);
  const compactRunId = runId.replaceAll("-", "");
  const timeSeed = Number.parseInt(compactRunId.slice(0, 10), 16);
  let originTimeMs = Date.UTC(2000, 0, 1) + timeSeed;

  while (existingTimes.some((time) => Math.abs(time - originTimeMs) <= 120_000)) {
    originTimeMs += 180_000;
  }

  const longitudeSeed = Number.parseInt(compactRunId.slice(10, 18), 16);
  const latitudeSeed = Number.parseInt(compactRunId.slice(18, 26), 16);

  return {
    originTime: new Date(originTimeMs).toISOString(),
    longitude: 121.4 + (longitudeSeed % 400) / 1000,
    latitude: 31 + (latitudeSeed % 600) / 1000,
  };
}

async function loginThroughUi(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "事件控制台" })).toBeVisible();
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(superadminPassword());
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("link", { name: "事件列表" })).toBeVisible();
}

async function refreshListWithoutReload(page: Page): Promise<void> {
  await page.getByRole("link", { name: "人工触发" }).click();
  await expect(page.getByRole("heading", { name: "创建人工地震事件" })).toBeVisible();
  await page.getByRole("link", { name: "事件列表", exact: true }).click();
  await expect(page.getByRole("heading", { name: "地震事件列表" })).toBeVisible();
}

async function getApiToken(request: APIRequestContext): Promise<string> {
  const response = await request.post(apiUrl("/api/v1/auth/login"), {
    form: {
      username: USERNAME,
      password: superadminPassword(),
    },
  });
  expect(response.status()).toBe(200);
  const payload = (await response.json()) as { access_token?: string };
  expect(typeof payload.access_token).toBe("string");
  return payload.access_token as string;
}

test("formal CENC event exposes dual response suggestions", async ({ page }) => {
  await loginThroughUi(page);

  const runId = crypto.randomUUID();
  const { originTime, longitude, latitude } = await buildIsolatedEventFixture(
    page.request,
    runId,
  );
  const place = `上海浦东新区 E2E ${runId}`;
  const response = await page.request.post(apiUrl("/api/v1/ingest/formal"), {
    headers: {
      Authorization: `Bearer ${await getApiToken(page.request)}`,
    },
    data: {
      eventId: `CENC-E2E-${runId}`,
      reportType: "formal",
      originTime,
      longitude,
      latitude,
      magnitude: 5.2,
      depth: 12,
      place,
      regionContext: {
        insideShanghai: true,
        distanceToBoundaryKm: 0,
        deaths: null,
        maxIntensity: 6,
      },
    },
  });

  const responsePayload = (await response.json()) as EventIngestResponse;
  expect(response.status(), JSON.stringify(responsePayload)).toBe(201);
  expect(responsePayload).toMatchObject({
    event_kind: "formal",
  });

  await refreshListWithoutReload(page);
  const row = page.getByRole("row").filter({ hasText: place });
  await expect(row).toContainText("重大响应");
  await expect(row).toContainText("服务响应二级");
  await row.getByRole("link", { name: place }).click();

  await expect(page.getByRole("heading", { level: 1, name: place })).toBeVisible();
  await expect(page.getByText("重大响应")).toBeVisible();
  await expect(page.getByText("服务响应二级")).toBeVisible();
  await expect(page.getByText("正式报告")).toBeVisible();
});

test("drill event keeps its identifier visible in list and detail", async ({ page, request }) => {
  await loginThroughUi(page);

  const runId = crypto.randomUUID();
  const { originTime, longitude, latitude } = await buildIsolatedEventFixture(
    request,
    runId,
  );
  const place = `浦东新区 E2E ${runId}`;
  const token = await getApiToken(request);
  const response = await request.post(apiUrl("/api/v1/events/manual"), {
    headers: {
      Authorization: `Bearer ${token}`,
    },
    data: {
      origin_time: originTime,
      longitude,
      latitude,
      magnitude: 3.2,
      depth_km: 8.0,
      source: "shanghai-e2e",
      source_event_id: `MANUAL-E2E-${runId}`,
      place,
      event_kind: "drill",
    },
  });

  const responsePayload = (await response.json()) as { event_kind?: string };
  expect(response.status(), JSON.stringify(responsePayload)).toBe(201);
  expect(responsePayload).toMatchObject({
    event_kind: "drill",
  });

  await refreshListWithoutReload(page);
  await page.getByLabel("事件类型").selectOption("drill");
  const row = page.getByRole("row").filter({ hasText: place });
  await expect(row.getByText("演练", { exact: true })).toBeVisible();
  await row.getByRole("link", { name: place }).click();

  await expect(page.getByRole("heading", { level: 1, name: place })).toBeVisible();
  await expect(page.locator(".kind-tag").getByText("演练", { exact: true })).toBeVisible();
});

test("anonymous detail route redirects to login", async ({ page }) => {
  await page.goto("/events/not-a-real-id");
  await expect(page.getByRole("heading", { name: "事件控制台" })).toBeVisible();
  await expect(page.getByLabel("用户名")).toBeVisible();
});
