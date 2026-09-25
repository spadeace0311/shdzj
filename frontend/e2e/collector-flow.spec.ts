import { expect, test } from "@playwright/test";

test("collector status remains visible when backup is serving events", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill(process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin");
  await page.getByLabel("密码").fill(process.env.E2E_SUPERADMIN_PASSWORD ?? "");
  await page.getByRole("button", { name: "登录" }).click();

  await page.getByRole("link", { name: "采集状态" }).click();

  await expect(page.getByText(/总体状态：/)).toBeVisible();
  await expect(page.getByText("FAN 主链路")).toBeVisible();
  await expect(page.getByText("Wolfx 备用链路")).toBeVisible();
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
  const response = await page.request.post("/api/v1/ingest/formal", {
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
  const responsePayload = (await response.json()) as {
    event_kind: string;
    lifecycle_state: string;
  };
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
