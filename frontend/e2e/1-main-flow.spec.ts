import { expect, test } from "@playwright/test";
import { pay, register, uniqueEmail, verifyEmail } from "./helpers";

// Сквозной сценарий: регистрация → письмо → проект и задача → drag-and-drop →
// апгрейд на Pro картой 4242 → приглашение второго участника с доплатой.
test("регистрация, доска, апгрейд на Pro и приглашение с доплатой", async ({ page, request }) => {
  const email = uniqueEmail();
  await register(page, email);
  await verifyEmail(page, request, email);

  await page.goto("/app");
  await expect(page).toHaveURL(/\/w\//);

  // Проект и задачи
  await page.getByRole("button", { name: "+ Новый проект" }).click();
  await page.getByLabel("Название").fill("Roadmap");
  await page.getByRole("button", { name: "Создать", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Roadmap" })).toBeVisible();
  const todo = page.getByRole("region", { name: "To do" });
  const doing = page.getByRole("region", { name: "In progress" });
  await todo.getByRole("button", { name: "+ Добавить задачу" }).click();
  await page.getByLabel("Название задачи").fill("Подготовить релиз");
  await page.keyboard.press("Enter");
  await expect(todo.getByTestId("task-card")).toHaveCount(1);

  // Перетаскивание карточки в «In progress» (pointer-события, как у пользователя)
  const card = todo.getByTestId("task-card").first();
  const from = (await card.boundingBox())!;
  const to = (await doing.boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + from.width / 2 + 20, from.y + from.height / 2 + 10, { steps: 5 });
  await page.mouse.move(to.x + to.width / 2, to.y + 80, { steps: 15 });
  await page.mouse.up();
  await expect(doing.getByTestId("task-card")).toHaveCount(1);
  await expect(todo.getByTestId("task-card")).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("region", { name: "In progress" }).getByTestId("task-card")).toHaveCount(1);

  // Апгрейд на Pro через mock-checkout
  await page.getByRole("link", { name: "Биллинг" }).click();
  await expect(page.getByTestId("plan-badge")).toHaveText("Free");
  await page.getByRole("button", { name: "Перейти на Pro" }).click();
  await expect(page).toHaveURL(/mock-checkout/);
  await pay(page, "4242 4242 4242 4242");
  await expect(page.getByTestId("payment-success")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: "К биллингу" }).click();
  await expect(page.getByTestId("plan-badge")).toHaveText("Pro");
  await expect(page.getByTestId("sub-status")).toHaveText("Активна");
  await expect(page.getByText("Visa •••• 4242")).toBeVisible();

  // Приглашение второго участника: предпросмотр доплаты и подтверждение
  await page.getByRole("link", { name: "Участники" }).click();
  await page.getByRole("button", { name: "Пригласить" }).click();
  const invitee = uniqueEmail("invitee");
  await page.getByLabel("Email").fill(invitee);
  await page.getByRole("button", { name: "Продолжить" }).click();
  const confirm = page.getByTestId("surcharge-confirm");
  await expect(confirm).toContainText("Будет списано");
  await confirm.getByRole("button", { name: /Оплатить .* и пригласить/ }).click();
  const list = page.getByTestId("invitations-list");
  await expect(list).toContainText(invitee);
  // Доплата списывается с сохранённой карты, вебхук подтверждает место — приглашение уходит.
  await expect(list).toContainText("Ожидает принятия", { timeout: 45_000 });
  await expect(page.getByTestId("members-list")).toContainText("E2E Tester");
});
