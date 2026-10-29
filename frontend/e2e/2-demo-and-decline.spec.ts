import { expect, test } from "@playwright/test";
import { pay, register, uniqueEmail, verifyEmail } from "./helpers";

test("демо-вход открывает заполненный воркспейс с trial Pro", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Попробовать без регистрации" }).click();
  await expect(page).toHaveURL(/\/w\/.+\/p\/\d+/);
  await expect(page.getByTestId("banner-trialing")).toContainText("Пробный период Pro");
  await expect(page.getByTestId("task-card").first()).toBeVisible();
  await page.getByRole("link", { name: "Участники" }).click();
  await expect(page.getByTestId("members-list").locator("li")).toHaveCount(4);
});

test("карта 4000 0000 0000 0002 отклоняется, подписка остаётся Free", async ({ page, request }) => {
  const email = uniqueEmail();
  await register(page, email);
  await verifyEmail(page, request, email);
  await page.goto("/app");
  await page.getByRole("link", { name: "Биллинг" }).click();
  await page.getByRole("button", { name: "Перейти на Pro" }).click();
  await pay(page, "4000 0000 0000 0002");
  await expect(page.getByTestId("payment-failed")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: "К биллингу" }).click();
  await expect(page.getByTestId("plan-badge")).toHaveText("Free");
});

test("лимит Free: четвёртый проект открывает модалку «Перейдите на Pro»", async ({ page, request }) => {
  const email = uniqueEmail();
  await register(page, email);
  await verifyEmail(page, request, email);
  await page.goto("/app");
  for (const name of ["Alpha", "Beta", "Gamma", "Delta"]) {
    await page.getByRole("button", { name: "+ Новый проект" }).click();
    await page.getByLabel("Название").fill(name);
    await page.getByRole("button", { name: "Создать", exact: true }).click();
    if (name !== "Delta") await expect(page.getByRole("heading", { name })).toBeVisible();
  }
  await expect(page.getByTestId("upgrade-modal")).toContainText("не больше 3 активных проектов");
});
