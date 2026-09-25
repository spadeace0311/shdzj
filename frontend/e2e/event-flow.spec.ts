import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

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
  await page.getByRole("link", { name: "事件列表" }).click();
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

  const runId = Date.now().toString();
  const place = `上海浦东新区 E2E ${runId}`;
  const response = await page.request.post(apiUrl("/api/v1/ingest/formal"), {
    data: {
      eventId: `CENC-E2E-${runId}`,
      reportType: "formal",
      originTime: "2026-09-17T02:30:05Z",
      longitude: 121.54,
      latitude: 31.22,
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

  expect(response.status()).toBe(201);
  expect((await response.json()) as { event_kind?: string }).toMatchObject({
    event_kind: "formal",
  });

  await refreshListWithoutReload(page);
  await expect(page.getByText(place)).toBeVisible();
  await page.getByRole("link", { name: place }).click();

  await expect(page.getByRole("heading", { level: 1, name: place })).toBeVisible();
  await expect(page.getByText("重大响应")).toBeVisible();
  await expect(page.getByText("服务响应二级")).toBeVisible();
  await expect(page.getByText("正式报告")).toBeVisible();
});

test("drill event keeps its identifier visible in list and detail", async ({ page, request }) => {
  await loginThroughUi(page);

  const runId = Date.now().toString();
  const place = `演练事件 E2E ${runId}`;
  const token = await getApiToken(request);
  const response = await request.post(apiUrl("/api/v1/events/manual"), {
    headers: {
      Authorization: `Bearer ${token}`,
    },
    data: {
      origin_time: "2026-09-17T02:30:05Z",
      longitude: 121.54,
      latitude: 31.22,
      magnitude: 3.2,
      depth_km: 8.0,
      source: "shanghai-e2e",
      source_event_id: `MANUAL-E2E-${runId}`,
      place,
      event_kind: "drill",
    },
  });

  expect(response.status()).toBe(201);
  expect((await response.json()) as { event_kind?: string }).toMatchObject({
    event_kind: "drill",
  });

  await refreshListWithoutReload(page);
  await page.getByLabel("事件类型").selectOption("drill");
  const row = page.getByRole("row").filter({ hasText: place });
  await expect(row).toContainText("演练");
  await row.getByRole("link", { name: place }).click();

  await expect(page.getByRole("heading", { level: 1, name: place })).toBeVisible();
  await expect(page.getByText("演练")).toBeVisible();
});
