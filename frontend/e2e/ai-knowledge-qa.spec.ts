import { expect, test, type Page } from "@playwright/test";

const USERNAME = process.env.E2E_SUPERADMIN_USERNAME ?? "superadmin";
const eventId = process.env.E2E_QA_EVENT_ID;

function superadminPassword(): string {
  const password = process.env.E2E_SUPERADMIN_PASSWORD;
  if (!password) {
    throw new Error(
      "E2E_SUPERADMIN_PASSWORD is not set; the real QA event fixture cannot be exercised without the configured login password.",
    );
  }
  return password;
}

async function login(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(superadminPassword());
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("link", { name: "事件列表" })).toBeVisible();
}

test("answers with citation, tool process and map action", async ({ page }) => {
  test.skip(!eventId, "E2E_QA_EVENT_ID is required for the real QA event fixture.");
  await login(page);
  await page.goto(`/events/${eventId}`);
  await page.getByRole("button", { name: "智能问策" }).click();
  await page.getByLabel("问题").fill("震中距最近断裂带多少公里？");
  await page.getByRole("button", { name: "提问" }).click();

  await expect(page.getByTestId("qa-answer")).toContainText("公里");
  await expect(
    page.locator(".qa-tool-result-list").getByText("fault.nearest"),
  ).toBeVisible();
  await expect(
    page.locator(".qa-citation__key").filter({ hasText: "C1" }),
  ).toBeVisible();
  await expect(page.getByTestId("loss-map-last-action")).toHaveText(
    /^(locate|fit_bounds|buffer|highlight|set_layers)$/,
  );
});
