import { expect, test, type Page } from "@playwright/test";

const USERNAME = process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin";
const VIEWER_USERNAME = process.env.E2E_VIEWER_USERNAME ?? "e2e-viewer";

const VIEWPORTS = [
  { name: "7680x2430 主屏", width: 7680, height: 2430 },
  { name: "1920x1080 管理终端", width: 1920, height: 1080 },
] as const;

const LAYOUT_SELECTORS = [
  ".command-hall__header",
  ".command-hall__alerts",
  ".command-hall__summary",
  ".command-hall__artifacts",
  ".command-hall__groups-section",
  '[data-testid="command-hall-event-marker"]',
  '[data-testid="command-hall-sync-status"]',
  ".command-hall__sync",
  '[data-testid="command-hall-group-card"]',
] as const;

const TEXT_SELECTORS = [
  "#command-hall-title",
  ".command-hall__alerts h2",
  ".command-hall__artifacts h2",
  ".command-hall__groups-section h2",
  '[data-testid="command-hall-event-marker"]',
  '[data-testid="command-hall-sync-status"]',
  ".command-hall__sync",
  '[data-testid="command-hall-group-card"] h3',
] as const;

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

interface LayoutIssue {
  selector: string;
  index: number;
  kind: string;
  detail: string;
}

async function assertLayoutFits(page: Page): Promise<void> {
  const issues = await page.evaluate(
    ({ layoutSelectors, textSelectors }) => {
      const failures: LayoutIssue[] = [];
      const epsilon = 1;
      const viewport = {
        width: window.innerWidth,
        height: window.innerHeight,
      };

      function inspect(
        selector: string,
        element: Element,
        index: number,
        checkText: boolean,
      ) {
        const rect = element.getBoundingClientRect();
        if (
          rect.left < -epsilon ||
          rect.top < -epsilon ||
          rect.right > viewport.width + epsilon ||
          rect.bottom > viewport.height + epsilon
        ) {
          failures.push({
            selector,
            index,
            kind: "viewport",
            detail: JSON.stringify({
              left: rect.left,
              top: rect.top,
              right: rect.right,
              bottom: rect.bottom,
              viewport,
            }),
          });
        }
        if (element.scrollWidth > element.clientWidth + epsilon) {
          failures.push({
            selector,
            index,
            kind: "horizontal-overflow",
            detail: `${element.scrollWidth}/${element.clientWidth}`,
          });
        }
        if (element.scrollHeight > element.clientHeight + epsilon) {
          failures.push({
            selector,
            index,
            kind: "vertical-overflow",
            detail: `${element.scrollHeight}/${element.clientHeight}`,
          });
        }
        if (checkText && (element as HTMLElement).innerText.trim()) {
          if (element.scrollWidth > element.clientWidth + epsilon) {
            failures.push({
              selector,
              index,
              kind: "text-horizontal-clipping",
              detail: `${element.scrollWidth}/${element.clientWidth}`,
            });
          }
          if (element.scrollHeight > element.clientHeight + epsilon) {
            failures.push({
              selector,
              index,
              kind: "text-vertical-clipping",
              detail: `${element.scrollHeight}/${element.clientHeight}`,
            });
          }
        }
      }

      for (const selector of layoutSelectors) {
        document.querySelectorAll(selector).forEach((element, index) => {
          inspect(selector, element, index, false);
        });
      }
      for (const selector of textSelectors) {
        document.querySelectorAll(selector).forEach((element, index) => {
          inspect(selector, element, index, true);
        });
      }

      const cards = Array.from(
        document.querySelectorAll(
          '[data-testid="command-hall-group-card"]',
        ),
      );
      cards.forEach((card, index) => {
        cards.slice(index + 1).forEach((other) => {
          const first = card.getBoundingClientRect();
          const second = other.getBoundingClientRect();
          const overlaps = !(
            first.right <= second.left ||
            second.right <= first.left ||
            first.bottom <= second.top ||
            second.bottom <= first.top
          );
          if (overlaps) {
            failures.push({
              selector: '[data-testid="command-hall-group-card"]',
              index,
              kind: "card-overlap",
              detail: JSON.stringify({
                first: {
                  left: first.left,
                  top: first.top,
                  right: first.right,
                  bottom: first.bottom,
                },
                second: {
                  left: second.left,
                  top: second.top,
                  right: second.right,
                  bottom: second.bottom,
                },
              }),
            });
          }
        });
      });

      for (const selector of layoutSelectors) {
        document.querySelectorAll(selector).forEach((element, index) => {
          const rect = element.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) {
            return;
          }
          const points = [
            [rect.left + rect.width / 2, rect.top + rect.height / 2],
            [rect.left + rect.width * 0.2, rect.top + rect.height * 0.2],
            [rect.left + rect.width * 0.8, rect.top + rect.height * 0.2],
            [rect.left + rect.width * 0.2, rect.top + rect.height * 0.8],
            [rect.left + rect.width * 0.8, rect.top + rect.height * 0.8],
          ];
          for (const [x, y] of points) {
            const hit = document.elementFromPoint(x, y);
            if (!hit || !element.contains(hit)) {
              failures.push({
                selector,
                index,
                kind: "occluded",
                detail: JSON.stringify({
                  x,
                  y,
                  hit: hit?.className ?? hit?.tagName ?? null,
                }),
              });
              break;
            }
          }
        });
      }

      const body = document.body;
      const root = document.documentElement;
      const bodyWidth = Math.max(body.scrollWidth, root.scrollWidth);
      const bodyHeight = Math.max(body.scrollHeight, root.scrollHeight);
      if (bodyWidth > viewport.width + epsilon) {
        failures.push({
          selector: "document",
          index: 0,
          kind: "page-horizontal-overflow",
          detail: `${bodyWidth}/${viewport.width}`,
        });
      }
      if (bodyHeight > viewport.height + epsilon) {
        failures.push({
          selector: "document",
          index: 0,
          kind: "page-vertical-overflow",
          detail: `${bodyHeight}/${viewport.height}`,
        });
      }
      return failures;
    },
    {
      layoutSelectors: LAYOUT_SELECTORS,
      textSelectors: TEXT_SELECTORS,
    },
  );
  expect(issues).toEqual([]);
}

async function cardRects(page: Page): Promise<number[][]> {
  return page
    .locator('[data-testid="command-hall-group-card"]')
    .evaluateAll((cards) =>
      cards.map((card) => {
        const rect = card.getBoundingClientRect();
        return [rect.left, rect.top, rect.right, rect.bottom];
      }),
    );
}

async function assertDrawerLayoutStable(page: Page): Promise<void> {
  const before = await cardRects(page);
  await page
    .locator('[data-testid="command-hall-group-card"]')
    .first()
    .click();

  const drawer = page.locator(".command-hall-drawer");
  await expect(drawer).toBeVisible();
  await expect(
    drawer.getByRole("heading", { name: "到岗与代理权限" }),
  ).toBeVisible();

  const drawerMetrics = await drawer.evaluate((element) => {
    const header = element.querySelector<HTMLElement>(
      ".command-hall-drawer__header",
    );
    const content = element.querySelector<HTMLElement>(
      ".command-hall-drawer__content",
    );
    const rect = element.getBoundingClientRect();
    return {
      rect: {
        left: rect.left,
        top: rect.top,
        right: rect.right,
        bottom: rect.bottom,
      },
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      contentScrollWidth: content?.scrollWidth ?? 0,
      contentClientWidth: content?.clientWidth ?? 0,
      headerBottom: header?.getBoundingClientRect().bottom ?? 0,
      contentTop: content?.getBoundingClientRect().top ?? 0,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
    };
  });

  expect(drawerMetrics.rect.left).toBeGreaterThanOrEqual(-1);
  expect(drawerMetrics.rect.top).toBeGreaterThanOrEqual(-1);
  expect(drawerMetrics.rect.right).toBeLessThanOrEqual(
    drawerMetrics.viewportWidth + 1,
  );
  expect(drawerMetrics.rect.bottom).toBeLessThanOrEqual(
    drawerMetrics.viewportHeight + 1,
  );
  expect(drawerMetrics.scrollWidth).toBeLessThanOrEqual(
    drawerMetrics.clientWidth + 1,
  );
  expect(drawerMetrics.scrollHeight).toBeLessThanOrEqual(
    drawerMetrics.clientHeight + 1,
  );
  expect(drawerMetrics.contentScrollWidth).toBeLessThanOrEqual(
    drawerMetrics.contentClientWidth + 1,
  );
  expect(drawerMetrics.headerBottom).toBeLessThanOrEqual(
    drawerMetrics.contentTop + 1,
  );

  const clipped = await drawer.evaluate((element) =>
    Array.from(
      element.querySelectorAll<HTMLElement>(
        "h2, h3, h4, .command-hall-drawer__roster dt, .command-hall-drawer__roster dd",
      ),
    )
      .filter((item) => item.innerText.trim())
      .filter(
        (item) =>
          item.scrollWidth > item.clientWidth + 1 ||
          item.scrollHeight > item.clientHeight + 1,
      )
      .map((item) => ({
        text: item.innerText,
        scrollWidth: item.scrollWidth,
        clientWidth: item.clientWidth,
        scrollHeight: item.scrollHeight,
        clientHeight: item.clientHeight,
      })),
  );
  expect(clipped).toEqual([]);

  await drawer.getByRole("button", { name: "关闭详情" }).click();
  await expect(drawer).toBeHidden();
  const after = await cardRects(page);
  expect(after).toEqual(before);
}

async function assertCommandHallReady(page: Page): Promise<void> {
  await expect(
    page.locator('[data-testid="command-hall-group-card"]'),
  ).toHaveCount(7);
  await expect(page.getByText("新闻信息值守组")).toBeVisible();
  await expect(page.getByText("中心站组")).toBeVisible();
  await expect(page.getByText("任务总览")).toBeVisible();
  await assertLayoutFits(page);
  await assertDrawerLayoutStable(page);
}

for (const viewport of VIEWPORTS) {
  test(`command hall fits ${viewport.name} with seven groups and stable drawer`, async ({
    page,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
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
    await expect(page.getByText("数据已同步")).toBeVisible();
    await assertCommandHallReady(page);

    await page.getByRole("button", { name: "智能问策" }).click();
    await expect(page.getByLabel("事件上下文问答")).toBeVisible();
    const qaBounds = await page.getByLabel("事件上下文问答").boundingBox();
    expect(qaBounds!.x).toBeGreaterThanOrEqual(0);
    expect(qaBounds!.x + qaBounds!.width).toBeLessThanOrEqual(
      viewport.width + 1,
    );
    await page.getByRole("button", { name: "关闭智能问策" }).click();
  });
}

test("command hall uses SSE and falls back to five-second polling", async ({
  page,
}) => {
  await login(page);
  await openCommandHall(page);
  await expect(page.getByText("实时更新")).toBeVisible();

  let streamFailed = false;
  let pollingOverviewRequests = 0;
  await page.route(
    "**/api/v1/command-hall/events/*/stream",
    async (route) => {
      streamFailed = true;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "stream unavailable" }),
      });
    },
  );
  await page.route(
    "**/api/v1/command-hall/events/*/overview",
    async (route) => {
      if (!streamFailed) {
        await route.continue();
        return;
      }
      pollingOverviewRequests += 1;
      const response = await route.fetch();
      const body = await response.json();
      if (pollingOverviewRequests === 1) {
        body.task_counts.completed = 123;
      }
      await route.fulfill({ response, json: body });
    },
  );

  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/command-hall(?:\/|$)/);
  await expect(page.getByText("5 秒轮询更新")).toBeVisible();
  await expect
    .poll(() => pollingOverviewRequests, {
      timeout: 8_000,
      message: "SSE failure should trigger at least one overview GET",
    })
    .toBeGreaterThanOrEqual(1);
  await expect(
    page.locator(".command-hall__task-summary dd").first(),
  ).toHaveText("123");
});

test("command hall preserves permission and service error states", async ({
  page,
  request,
}) => {
  const viewerLogin = await request.post("/api/v1/auth/login", {
    form: {
      username: VIEWER_USERNAME,
      password: superadminPassword(),
    },
  });
  expect(viewerLogin.status()).toBe(200);
  const viewerToken = (await viewerLogin.json()).access_token as string;
  const forbidden = await request.put(
    "/api/v1/workgroups/news_information/memberships",
    {
      headers: { Authorization: `Bearer ${viewerToken}` },
      data: { members: [] },
    },
  );
  expect(forbidden.status()).toBe(403);

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
