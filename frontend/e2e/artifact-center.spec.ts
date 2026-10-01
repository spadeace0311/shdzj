import { expect, test } from "@playwright/test";

test("artifact center opens a completed test event and downloads the original file", async ({ page }) => {
  await page.goto("/events/test-artifact-event");
  await page.getByRole("link", { name: "成果中心" }).click();
  await expect(page.getByText("39/39")).toBeVisible();
  await expect(page.getByText("【测试】")).toBeVisible();
  await page.getByRole("button", { name: "下载原文件" }).first().click();
  await expect(page.getByText("下载已开始")).toBeVisible();
});
