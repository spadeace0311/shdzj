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

test("artifact center opens a completed test event and downloads the original file", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "事件控制台" })).toBeVisible();
  await page.getByLabel("用户名").fill(USERNAME);
  await page.getByLabel("密码").fill(superadminPassword());
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByLabel("成果中心导航")).toBeVisible();

  const place = "上海成果中心验收测试事件";
  await page
    .getByRole("row")
    .filter({ hasText: place })
    .getByRole("link", { name: place })
    .click();
  await expect(
    page.getByRole("heading", { name: place, exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "成果中心", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "地震应急成果" }),
  ).toBeVisible();
  await expect(page.getByText("39/39")).toBeVisible();
  await expect(page.getByText("【测试】").first()).toBeVisible();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载原文件" }).first().click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toContain("【测试】");
  await expect(page.getByText("下载已开始")).toBeVisible();
});
