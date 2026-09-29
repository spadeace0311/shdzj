import { expect, test } from "@playwright/test";

const eventId = process.env.E2E_LOSS_EVENT_ID;
const password = process.env.E2E_SUPERADMIN_PASSWORD;

test("renders town and grid loss layers with the real MapLibre bundle", async ({
  page,
}) => {
  test.skip(!eventId || !password, "loss E2E fixture is not configured");

  await page.goto("/");
  await page.getByLabel("用户名").fill(
    process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin",
  );
  await page.getByLabel("密码").fill(password as string);
  await page.getByRole("button", { name: "登录" }).click();
  await page.goto(`/events/${eventId}`);

  const map = page.locator(".maplibregl-map");
  await expect(map).toBeVisible();
  await expect(map.locator("canvas.maplibregl-canvas")).toBeVisible();
  await expect(page.getByLabel("公里格网为空间化估算")).toBeVisible();
  await expect(page.getByLabel("融合烈度")).toBeVisible();
  await page.getByLabel("融合烈度").uncheck();
  await page.getByLabel("街镇损失").uncheck();
  await expect(map.locator(".maplibregl-canvas")).toBeVisible();
});
