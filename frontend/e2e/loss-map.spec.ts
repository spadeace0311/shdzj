import { expect, test } from "@playwright/test";

const USERNAME = process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin";

function superadminPassword(): string {
  const password = process.env.E2E_SUPERADMIN_PASSWORD;
  if (!password) {
    throw new Error(
      "E2E_SUPERADMIN_PASSWORD is not set; seed the E2E fixture and provide the configured login password.",
    );
  }
  return password;
}

test("renders town and grid loss layers with the real MapLibre bundle", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(superadminPassword());
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("link", { name: "事件列表" })).toBeVisible();

  const place = "上海成果中心验收测试事件";
  await page
    .getByRole("row")
    .filter({ hasText: place })
    .getByRole("link", { name: place })
    .click();

  const map = page.locator(".maplibregl-map");
  await expect(map).toBeVisible();
  await expect(map.locator("canvas.maplibregl-canvas")).toBeVisible();
  await expect(page.getByLabel("公里格网为空间化估算")).toBeVisible();
  await expect(page.getByLabel("融合烈度")).toBeVisible();
  await page.getByLabel("融合烈度").uncheck();
  await page.getByLabel("街镇损失").uncheck();
  await expect(map.locator(".maplibregl-canvas")).toBeVisible();
});
