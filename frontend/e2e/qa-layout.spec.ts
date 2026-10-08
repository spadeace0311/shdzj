import { expect, test, type Page, type Route } from "@playwright/test";

const VIEWPORTS = [
  { name: "1920x1080", width: 1920, height: 1080 },
  { name: "7680x2430", width: 7680, height: 2430 },
] as const;

const eventDetail = {
  id: "event-1",
  source: "test",
  place: "布局验收事件",
  magnitude: 5.2,
  depth_km: 10,
  origin_time: "2026-10-08T00:00:00Z",
  longitude: 121.5,
  latitude: 31.2,
  institutional_level: "general",
  service_level: 4,
  response_suggestion: null,
  response_rule_version: "rule-v1",
  revision_no: 1,
  event_kind: "formal",
  lifecycle_state: "formal_triggered",
  t1_at: "2026-10-08T00:01:00Z",
};

async function mockApis(page: Page): Promise<void> {
  await page.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/api/v1/auth/login")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ access_token: "layout-token", token_type: "bearer" }),
      });
      return;
    }
    if (url.pathname.endsWith("/api/v1/auth/me")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          username: "layout-user",
          role: "superadmin",
          workgroup: null,
        }),
      });
      return;
    }
    if (url.pathname.endsWith("/api/v1/events")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "event-1",
            source: "test",
            event_kind: "formal",
            place: "布局验收事件",
            magnitude: 5.2,
            depth_km: 10,
            origin_time: "2026-10-08T00:00:00Z",
            longitude: 121.5,
            latitude: 31.2,
            institutional_level: "general",
            service_level: 4,
            revision_no: 1,
            lifecycle_state: "formal_triggered",
            t1_at: null,
          },
        ]),
      });
      return;
    }
    if (url.pathname.endsWith("/api/v1/events/event-1")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(eventDetail),
      });
      return;
    }
    if (
      url.pathname.endsWith(
        "/api/v1/assessments/events/event-1/current",
      )
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run_id: "run-1",
          event_id: "event-1",
          revision_id: "revision-1",
          run_no: 1,
          status: "completed",
          t1_at: "2026-10-08T00:01:00Z",
          deadline_at: "2026-10-08T00:10:00Z",
          completed_task_count: 1,
          failed_task_count: 0,
          total_task_count: 1,
          tasks: [],
          intensity: null,
        }),
      });
      return;
    }
    if (
      url.pathname.endsWith(
        "/api/v1/assessments/runs/run-1/production",
      )
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "null",
      });
      return;
    }
    if (url.pathname.endsWith("/api/v1/assessments/runs/run-1/loss")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run_id: "run-1",
          event_id: "event-1",
          revision_id: "revision-1",
          effective_run_id: "run-1",
          is_fallback: false,
          products: [],
        }),
      });
      return;
    }
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "layout mock not found" }),
    });
  });
}

async function login(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("用户名").fill("layout-user");
  await page.getByLabel("密码").fill("layout-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("link", { name: "事件列表" })).toBeVisible();
}

async function openQaLayout(page: Page): Promise<void> {
  await page
    .getByRole("link", { name: "布局验收事件" })
    .click();
  await expect(
    page.getByRole("heading", { name: "布局验收事件", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".loss-map")).toBeVisible();
  await expect(page.locator(".loss-map canvas")).toBeVisible();
  await page.getByRole("button", { name: "智能问策" }).click();
  await expect(page.getByLabel("事件上下文问答")).toBeVisible();
}

for (const viewport of VIEWPORTS) {
  test(`QA layout fits ${viewport.name} without header, map, or panel overlap`, async ({
    page,
  }) => {
    await page.setViewportSize({
      width: viewport.width,
      height: viewport.height,
    });
    await mockApis(page);
    await login(page);
    await openQaLayout(page);

    const metrics = await page.evaluate(() => {
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const select = <T extends Element>(selector: string): T => {
        const element = document.querySelector<T>(selector);
        if (!element) {
          throw new Error(`Missing layout element: ${selector}`);
        }
        return element;
      };
      const header = select<HTMLElement>(".app-header");
      const pageSection = select<HTMLElement>(".page-section--qa-open");
      const map = select<HTMLElement>(".loss-map");
      const panel = select<HTMLElement>(
        '[aria-label="事件上下文问答"]',
      );
      const panelContent = panel.querySelector<HTMLElement>(
        ".qa-panel__content",
      );
      const rect = (element: Element) => element.getBoundingClientRect();
      const intersects = (left: DOMRect, right: DOMRect) =>
        !(
          left.right <= right.left ||
          right.right <= left.left ||
          left.bottom <= right.top ||
          right.bottom <= left.top
        );

      return {
        viewportWidth,
        viewportHeight,
        header: rect(header),
        pageSection: rect(pageSection),
        map: rect(map),
        panel: rect(panel),
        panelOverlapsHeader: intersects(rect(panel), rect(header)),
        panelOverlapsMap: intersects(rect(panel), rect(map)),
        mapLeftWithinPage: rect(map).left >= rect(pageSection).left - 1,
        mapRightWithinPage: rect(map).right <= rect(pageSection).right + 1,
        panelScrollWidth: panel.scrollWidth,
        panelClientWidth: panel.clientWidth,
        panelContentScrollWidth: panelContent?.scrollWidth ?? 0,
        panelContentClientWidth: panelContent?.clientWidth ?? 0,
        documentScrollWidth: document.documentElement.scrollWidth,
        bodyScrollWidth: document.body.scrollWidth,
      };
    });

    expect(metrics.pageSection.width).toBeLessThanOrEqual(1721);
    expect(metrics.panel.left).toBeGreaterThanOrEqual(-1);
    expect(metrics.panel.top).toBeGreaterThanOrEqual(metrics.header.bottom - 1);
    expect(metrics.panel.right).toBeLessThanOrEqual(
      metrics.viewportWidth + 1,
    );
    expect(metrics.panel.bottom).toBeLessThanOrEqual(
      metrics.viewportHeight + 1,
    );
    expect(metrics.panelOverlapsHeader).toBe(false);
    expect(metrics.panelOverlapsMap).toBe(false);
    expect(metrics.mapLeftWithinPage).toBe(true);
    expect(metrics.mapRightWithinPage).toBe(true);
    expect(metrics.panelScrollWidth).toBeLessThanOrEqual(
      metrics.panelClientWidth + 1,
    );
    expect(metrics.panelContentScrollWidth).toBeLessThanOrEqual(
      metrics.panelContentClientWidth + 1,
    );
    expect(metrics.documentScrollWidth).toBeLessThanOrEqual(
      metrics.viewportWidth + 1,
    );
    expect(metrics.bodyScrollWidth).toBeLessThanOrEqual(
      metrics.viewportWidth + 1,
    );
  });
}
