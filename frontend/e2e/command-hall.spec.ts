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
  await expect(page.getByRole("link", { name: "指挥大厅" })).toBeVisible();
}

async function openCommandHall(page: Page): Promise<void> {
  await page.getByRole("link", { name: "指挥大厅" }).click();
  await expect(
    page.locator(".command-hall-shell, .command-hall-state").first(),
  ).toBeVisible();
}

async function assertNoGroupOverlap(page: Page): Promise<void> {
  const overlaps = await page
    .locator('[data-testid="command-hall-group-card"]')
    .evaluateAll((cards) =>
      cards.some((card, index) =>
        cards.slice(index + 1).some((other) => {
          const first = card.getBoundingClientRect();
          const second = other.getBoundingClientRect();
          return !(
            first.right <= second.left ||
            second.right <= first.left ||
            first.bottom <= second.top ||
            second.bottom <= first.top
          );
        }),
      ),
    );
  expect(overlaps).toBe(false);
}

async function assertNoPageOverflow(page: Page): Promise<void> {
  const dimensions = await page.evaluate(() => ({
    bodyWidth: document.body.scrollWidth,
    bodyHeight: document.body.scrollHeight,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
  }));
  expect(dimensions.bodyWidth).toBeLessThanOrEqual(dimensions.viewportWidth);
  expect(dimensions.bodyHeight).toBeLessThanOrEqual(
    dimensions.viewportHeight,
  );
}

test("command hall fits 7680x2430 with all seven groups and test markers", async ({
  page,
}) => {
  await page.setViewportSize({ width: 7680, height: 2430 });
  await login(page);
  await openCommandHall(page);

  await expect(
    page.getByRole("heading", {
      name: "上海成果中心验收测试事件",
    }),
  ).toBeVisible();
  await expect(
    page.locator('[data-testid="command-hall-event-marker"]'),
  ).toHaveText("测试");
  await expect(
    page.locator('[data-testid="command-hall-group-card"]'),
  ).toHaveCount(7);
  await expect(page.getByText("新闻信息值守组")).toBeVisible();
  await expect(page.getByText("中心站组")).toBeVisible();
  await expect(page.getByText("任务总览")).toBeVisible();
  await assertNoGroupOverlap(page);
  await assertNoPageOverflow(page);
});

test("command hall fits 1920x1080 management terminal", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await login(page);
  await openCommandHall(page);

  await expect(
    page.locator('[data-testid="command-hall-group-card"]'),
  ).toHaveCount(7);
  await expect(page.getByText("数据已同步")).toBeVisible();
  await expect(page.getByText("任务总览")).toBeVisible();
  await expect(page.getByText("中心站组")).toBeVisible();
  await assertNoGroupOverlap(page);
  await assertNoPageOverflow(page);
});

test("command hall uses SSE and falls back to five-second polling", async ({
  page,
}) => {
  await login(page);
  await openCommandHall(page);
  await expect(page.getByText("实时更新")).toBeVisible();

  await page.route(
    "**/api/v1/command-hall/events/*/stream",
    async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "stream unavailable" }),
      });
    },
  );
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/command-hall(?:\/|$)/);
  await expect(page.getByText("5 秒轮询更新")).toBeVisible();
});

test("command hall preserves permission and service error states", async ({
  page,
}) => {
  await login(page);
  await page.route(
    "**/api/v1/command-hall/events/*/overview",
    async (route) => {
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({ detail: "forbidden" }),
      });
    },
  );
  await openCommandHall(page);
  await expect(
    page.getByText("当前账号无权查看该事件指挥大厅"),
  ).toBeVisible();

  await page.unroute("**/api/v1/command-hall/events/*/overview");
  await page.route(
    "**/api/v1/command-hall/events/*/overview",
    async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "command hall unavailable" }),
      });
    },
  );
  await page.getByRole("button", { name: "重试" }).click();
  await expect(page.locator(".state-panel--error")).toBeVisible();
  await expect(page.getByRole("button", { name: "重试" })).toBeVisible();
});
