import { expect, test, type Page } from "@playwright/test";

import type { EventIngestResponse } from "../src/types";

async function getApiToken(page: Page): Promise<string> {
  const response = await page.request.post("/api/v1/auth/login", {
    form: {
      username: process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin",
      password: process.env.E2E_SUPERADMIN_PASSWORD ?? "",
    },
  });
  expect(response.status()).toBe(200);
  const payload = (await response.json()) as { access_token?: string };
  expect(typeof payload.access_token).toBe("string");
  return payload.access_token as string;
}

test("collector status remains visible when backup is serving events", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill(process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin");
  await page.getByLabel("密码").fill(process.env.E2E_SUPERADMIN_PASSWORD ?? "");
  await page.getByRole("button", { name: "登录" }).click();

  await page.getByRole("link", { name: "采集状态" }).click();

  await expect(
    page.getByRole("heading", { name: /总体状态：(正常|降级)/ }),
  ).toBeVisible();
  const wolfxRow = page.getByRole("row").filter({ hasText: "Wolfx 备用链路" });
  await expect(wolfxRow).toContainText("正常");
  await expect(wolfxRow).toContainText("已连接");
  await expect(wolfxRow.locator("td").nth(3)).not.toHaveText("-");
  await expect(wolfxRow.locator("td").nth(8)).toHaveText("200");
});

test("formal recovery ingest reaches the lifecycle list", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill(process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin");
  await page.getByLabel("密码").fill(process.env.E2E_SUPERADMIN_PASSWORD ?? "");
  await page.getByRole("button", { name: "登录" }).click();

  const runId = crypto.randomUUID();
  const seed = Number.parseInt(runId.replaceAll("-", "").slice(0, 8), 16);
  const originTime = new Date(Date.UTC(2030, 0, 1) + seed * 1_000).toISOString();
  const place = `上海正式报 E2E ${runId}`;
  const token = await getApiToken(page);
  const response = await page.request.post("/api/v1/ingest/formal", {
    headers: {
      Authorization: `Bearer ${token}`,
    },
    data: {
      eventId: `CENC-FORMAL-${runId}`,
      reportType: "formal",
      originTime,
      longitude: 121.4 + (seed % 400) / 1_000,
      latitude: 31 + ((seed >> 8) % 600) / 1_000,
      magnitude: 5.2,
      depth: 10,
      place,
      regionContext: {
        insideShanghai: true,
        distanceToBoundaryKm: 0,
        deaths: null,
        maxIntensity: null,
      },
    },
  });
  const responsePayload = (await response.json()) as EventIngestResponse;
  expect(response.status(), JSON.stringify(responsePayload)).toBe(201);
  expect(responsePayload).toMatchObject({
    event_kind: "formal",
    lifecycle_state: "formal_triggered",
  });

  await page.getByRole("link", { name: "人工触发" }).click();
  await page.getByRole("link", { name: "事件列表", exact: true }).click();

  const row = page.getByRole("row").filter({ hasText: place });
  await expect(row).toContainText("正式报已触发评估");
});
