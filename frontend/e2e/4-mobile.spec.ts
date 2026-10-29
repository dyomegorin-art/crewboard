import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 375, height: 800 } });

const noPageOverflow = async (page: import("@playwright/test").Page) => {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
};

test("на 375 px страницы не скроллятся вбок, канбан скроллится внутри", async ({ page }) => {
  await page.goto("/");
  await noPageOverflow(page);
  await page.getByRole("button", { name: "Попробовать без регистрации" }).click();
  await expect(page.getByTestId("task-card").first()).toBeVisible();
  await noPageOverflow(page);

  // Колонки шире экрана: горизонтальный скролл — внутри доски, а не страницы.
  const board = page.getByTestId("board");
  expect(await board.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);

  // Меню открывается кнопкой-гамбургером.
  await page.getByRole("button", { name: "Меню" }).click();
  const billing = page.getByRole("navigation", { name: "Разделы" }).getByRole("link", { name: "Биллинг" });
  await expect(billing).toBeVisible();
  await billing.click();
  await expect(page.getByTestId("plan-badge")).toBeVisible();
  await noPageOverflow(page);
});
