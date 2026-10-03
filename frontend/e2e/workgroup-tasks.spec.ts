import { expect, test, type Page } from "@playwright/test";

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

async function login(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(superadminPassword());
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("link", { name: "工作组任务" })).toBeVisible();
}

async function openWorkgroupTasks(page: Page): Promise<void> {
  await page.getByRole("link", { name: "事件列表" }).click();
  await page
    .getByRole("link", { name: "上海成果中心验收测试事件" })
    .click();
  await page
    .getByLabel("事件详情")
    .getByRole("link", { name: "工作组任务" })
    .click();
  await expect(
    page.getByRole("heading", { name: "工作组任务" }),
  ).toBeVisible();
}

async function assertNoHorizontalOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    bodyWidth: document.body.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(dimensions.bodyWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
}

test("workgroup task console closes the seven-group loop with immutable test markers", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await login(page);
  await openWorkgroupTasks(page);

  await expect(
    page.getByRole("heading", { name: "工作组任务" }),
  ).toBeVisible();
  await expect(page.locator(".event-marker").getByText("测试")).toBeVisible();
  await expect(page.locator(".workgroup-task-row")).toHaveCount(60);
  await expect(
    page.getByRole("button", {
      name: "查看任务详情：开启指挥大厅和视频会议系统",
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "查看任务详情：检查辖区内观测仪器和设施",
    }),
  ).toBeVisible();
  await assertNoHorizontalOverflow(page);
});

test("automatic and manual revisions coexist with the manual revision current", async ({
  page,
}) => {
  await page.setViewportSize({ width: 7680, height: 2430 });
  await login(page);
  await openWorkgroupTasks(page);

  await page
    .getByRole("button", {
      name: "查看任务详情：产出地震灾害快速评估简报",
    })
    .click();

  const panel = page.locator(".collaboration-task-panel");
  await expect(panel.getByText("成果与当前发布版")).toBeVisible();
  await expect(panel.getByText("系统自动版")).toBeVisible();
  await expect(panel.getByText("人工修订版")).toBeVisible();
  const versions = panel.locator(".collaboration-version");
  await expect(versions).toHaveCount(2);
  await expect(
    versions.filter({ hasText: "人工修订版" }).getByText("当前发布版"),
  ).toBeVisible();
  await assertNoHorizontalOverflow(page);
});
