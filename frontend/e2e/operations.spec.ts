import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const USERNAME = process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin";
const API_BASE_URL = (process.env.E2E_API_BASE_URL ?? "").replace(/\/+$/, "");
const TEST_PLACE = "上海市（系统测试）";
const PRODUCTION_TIMEOUT_MS = 240_000;
const RETENTION_TEST_DAYS = 400;
const TOTAL_OUTPUT_COUNT = 39;
const MAP_OUTPUT_COUNT = 27;
const REPORT_AND_DECK_OUTPUT_COUNT = 12;

const credentialMissingReason =
  "缺少真实超级管理员密码环境变量 E2E_SUPERADMIN_PASSWORD；" +
  "该 E2E 不得在无真实凭据时伪造通过。";

test.skip(!process.env.E2E_SUPERADMIN_PASSWORD, credentialMissingReason);

interface TriggerPayload {
  event_id: string | null;
  run_id: string | null;
  trigger_mode: string;
  operation_state: string;
  event_kind: string | null;
  triggered_assessment: boolean;
}

interface EventPayload {
  id: string;
  event_kind: string | null;
  origin_time: string;
}

interface AssessmentPayload {
  run_id: string;
  status: string;
}

interface ArtifactPayload {
  artifact_key: string;
}

interface ProductionPayload {
  production_run_id: string;
  assessment_run_id: string;
  status: string;
  production_mode: string;
  marker: string | null;
  required_output_count: number;
  complete_count: number;
  degraded_count: number;
  failed_count: number;
  artifacts: ArtifactPayload[];
}

function superadminPassword(): string {
  const password = process.env.E2E_SUPERADMIN_PASSWORD;
  if (!password) {
    throw new Error(credentialMissingReason);
  }
  return password;
}

function apiUrl(path: string): string {
  return API_BASE_URL ? `${API_BASE_URL}${path}` : path;
}

function authorizedHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function ensureRealApiIsAvailable(request: APIRequestContext): Promise<void> {
  const response = await request.get(apiUrl("/health"));
  if (response.status() !== 200) {
    test.skip(
      true,
      `真实后端不可用：GET /health 返回 ${response.status()}；` +
        "该 E2E 不使用 mock，必须连接真实 API。",
    );
  }
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

async function loginThroughUi(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "事件控制台" })).toBeVisible();
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(superadminPassword());
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("link", { name: "测试与演练" })).toBeVisible();
}

async function waitForAssessment(
  request: APIRequestContext,
  token: string,
  eventId: string,
): Promise<AssessmentPayload> {
  const deadline = Date.now() + PRODUCTION_TIMEOUT_MS;
  let lastStatus = "unavailable";

  while (Date.now() < deadline) {
    const response = await request.get(
      apiUrl(`/api/v1/assessments/events/${encodeURIComponent(eventId)}/current`),
      { headers: authorizedHeaders(token) },
    );
    if (response.status() === 200) {
      return (await response.json()) as AssessmentPayload;
    }
    if (response.status() !== 404 && response.status() !== 503) {
      throw new Error(
        `真实评估查询失败：GET assessment current 返回 ${response.status()}`,
      );
    }
    lastStatus = String(response.status());
    await delay(2_000);
  }

  throw new Error(
    `等待真实评估运行超时；最后状态 ${lastStatus}。请确认 assessment-dispatcher 与 temporal-worker 已启动。`,
  );
}

async function waitForProduction(
  request: APIRequestContext,
  token: string,
  assessmentRunId: string,
): Promise<ProductionPayload> {
  const terminalStatuses = new Set(["completed", "partial", "failed", "canceled"]);
  const deadline = Date.now() + PRODUCTION_TIMEOUT_MS;
  let lastPayload: ProductionPayload | null = null;

  while (Date.now() < deadline) {
    const response = await request.get(
      apiUrl(
        `/api/v1/assessments/runs/${encodeURIComponent(assessmentRunId)}/production`,
      ),
      { headers: authorizedHeaders(token) },
    );
    if (response.status() === 200) {
      const payload = (await response.json()) as ProductionPayload;
      lastPayload = payload;
      if (terminalStatuses.has(payload.status)) {
        return payload;
      }
    } else if (response.status() !== 404 && response.status() !== 503) {
      throw new Error(
        `真实成果生产查询失败：GET production 返回 ${response.status()}`,
      );
    }
    await delay(3_000);
  }

  throw new Error(
    `等待真实成果生产超时；最后状态 ${lastPayload?.status ?? "unavailable"}。` +
      "请确认 artifact-worker 与 artifact-dispatcher 已启动。",
  );
}

test("operations test trigger, artifact production, real-event priority, and retention use real backend", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  await page.setViewportSize({ width: 1920, height: 1080 });
  await ensureRealApiIsAvailable(request);
  await loginThroughUi(page);
  const token = await getApiToken(request);

  await page.getByRole("link", { name: "测试与演练" }).click();
  await expect(
    page.getByRole("heading", { name: "测试与演练" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "手工测试" })).toBeEnabled();

  const triggerResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/v1/operations/test-trigger"),
  );
  await page.getByRole("button", { name: "手工测试" }).click();
  const triggerResponse = await triggerResponsePromise;
  expect(triggerResponse.status()).toBe(201);
  const trigger = (await triggerResponse.json()) as TriggerPayload;
  expect(trigger.event_kind).toBe("test");
  expect(trigger.trigger_mode).toBe("manual_test");
  expect(trigger.event_id).not.toBeNull();
  expect(trigger.run_id).not.toBeNull();
  await expect(page.getByText(/手工测试已触发，运行编号/)).toBeVisible();

  const testEventId = trigger.event_id as string;
  const assessment = await waitForAssessment(request, token, testEventId);
  const production = await waitForProduction(
    request,
    token,
    assessment.run_id,
  );

  expect(production.production_mode).toBe("test");
  expect(production.marker).toBe("【测试】");
  expect(production.required_output_count).toBe(TOTAL_OUTPUT_COUNT);

  const artifacts = Array.isArray(production.artifacts)
    ? production.artifacts
    : [];
  const mapKeys = new Set(
    artifacts
      .filter((artifact) => artifact.artifact_key.startsWith("map."))
      .map((artifact) => artifact.artifact_key),
  );
  const reportAndDeckKeys = artifacts.filter(
    (artifact) => !artifact.artifact_key.startsWith("map."),
  );
  expect(mapKeys.size).toBe(MAP_OUTPUT_COUNT);
  expect(reportAndDeckKeys).toHaveLength(REPORT_AND_DECK_OUTPUT_COUNT);
  expect(production.complete_count + production.degraded_count).toBe(
    TOTAL_OUTPUT_COUNT,
  );
  expect(production.failed_count).toBe(0);

  await page.getByRole("link", { name: "事件列表" }).click();
  await expect(
    page.getByRole("heading", { name: "地震事件列表" }),
  ).toBeVisible();
  await page.getByLabel("事件类型").selectOption("test");
  const testRow = page.getByRole("row").filter({ hasText: TEST_PLACE }).first();
  await expect(testRow).toContainText("测试");
  await testRow.getByRole("link", { name: TEST_PLACE }).click();

  await expect(
    page.getByRole("heading", { level: 1, name: TEST_PLACE }),
  ).toBeVisible();
  await expect(
    page.locator(".kind-tag").filter({ hasText: "测试" }).first(),
  ).toBeVisible();

  await page.getByRole("link", { name: "成果中心", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "地震应急成果" }),
  ).toBeVisible();
  await expect(page.getByText("39/39")).toBeVisible();
  await expect(
    page.locator(".artifact-mode-bar").getByText("【测试】", { exact: true }),
  ).toBeVisible();

  const formalRunId = crypto.randomUUID();
  const formalPlace = `上海运营正式事件 E2E ${formalRunId}`;
  const formalResponse = await request.post(apiUrl("/api/v1/events/manual"), {
    headers: authorizedHeaders(token),
    data: {
      origin_time: new Date().toISOString(),
      longitude: "121.47",
      latitude: "31.23",
      magnitude: "5.2",
      depth_km: "10.0",
      source: "shanghai-operations-e2e",
      source_event_id: `OPERATIONS-FORMAL-${formalRunId}`,
      place: formalPlace,
      event_kind: "manual",
    },
  });
  expect(formalResponse.status()).toBe(201);
  const formalPayload = (await formalResponse.json()) as {
    event_kind?: string;
  };
  expect(formalPayload.event_kind).toBe("manual");

  const conflictResponse = await request.post(
    apiUrl("/api/v1/operations/test-trigger"),
    {
      headers: {
        ...authorizedHeaders(token),
        "Idempotency-Key": crypto.randomUUID(),
      },
    },
  );
  expect(conflictResponse.status()).toBe(409);
  const conflictPayload = (await conflictResponse.json()) as {
    detail?: string;
  };
  expect(conflictPayload.detail).toBe("real_event_priority_active");

  await page.getByRole("link", { name: "测试与演练" }).click();
  await expect(
    page.getByRole("heading", { name: "真实事件优先保护中" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "手工测试" })).toBeDisabled();

  const retainedResponse = await request.get(
    apiUrl(`/api/v1/events/${encodeURIComponent(testEventId)}`),
    { headers: authorizedHeaders(token) },
  );
  expect(retainedResponse.status()).toBe(200);
  const retainedEvent = (await retainedResponse.json()) as EventPayload;
  expect(retainedEvent.event_kind).toBe("test");

  const originTimeMs = Date.parse(retainedEvent.origin_time);
  expect(Number.isFinite(originTimeMs)).toBe(true);
  const ageMs = Date.now() - originTimeMs;
  const retentionCutoffMs = RETENTION_TEST_DAYS * 24 * 60 * 60 * 1_000;
  expect(ageMs).toBeGreaterThanOrEqual(0);
  expect(ageMs).toBeLessThan(retentionCutoffMs);
});
