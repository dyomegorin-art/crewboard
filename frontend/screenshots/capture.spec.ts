import { expect, test, type APIRequestContext } from "@playwright/test";
import path from "node:path";
import { PASSWORD, pay, register, uniqueEmail, verifyEmail } from "../e2e/helpers";

const OUT = path.resolve(process.cwd(), "../docs/img"); // запуск из frontend/

test.afterAll(async ({ request }) => {
  await request.post("/api/dev/reset-clock");
});

test("доска (демо-воркспейс)", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Попробовать без регистрации" }).click();
  await expect(page.getByTestId("task-card").first()).toBeVisible();
  await page.getByTestId("task-card").first().hover();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT, "board.png") });
});

test("биллинг и модалка доплаты", async ({ page, request }) => {
  const email = uniqueEmail("shots");
  await register(page, email, "Анна Седова");
  await verifyEmail(page, request, email);
  await page.goto("/app");
  await page.getByRole("link", { name: "Биллинг" }).click();
  await page.getByRole("button", { name: "Перейти на Pro" }).click();
  await pay(page);
  await expect(page.getByTestId("payment-success")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("link", { name: "К биллингу" }).click();
  await page.getByRole("link", { name: "Участники" }).click();
  await page.getByRole("button", { name: "Пригласить" }).click();
  await page.getByLabel("Email").fill("igor@example.com");
  await page.getByRole("button", { name: "Продолжить" }).click();
  await expect(page.getByTestId("surcharge-confirm")).toBeVisible();
  await page.screenshot({ path: path.join(OUT, "surcharge.png") });
  await page.getByRole("button", { name: /Оплатить .* и пригласить/ }).click();
  await expect(page.getByTestId("invitations-list")).toContainText("Ожидает принятия", { timeout: 45_000 });
  await page.getByRole("link", { name: "Биллинг" }).click();
  await expect(page.getByTestId("invoices")).toContainText("Доплата за место", { timeout: 15_000 });
  await page.setViewportSize({ width: 1280, height: 1150 }); // вся страница без внутреннего скролла
  await page.waitForTimeout(5_500); // тост «Приглашение создано» гаснет через 5 с
  await page.screenshot({ path: path.join(OUT, "billing.png") });
});

async function login(request: APIRequestContext, email: string): Promise<string> {
  const r = await request.post("/api/auth/login", { data: { email, password: PASSWORD } });
  return ((await r.json()) as { access_token: string }).access_token;
}

test("баннер read_only после неуплаты", async ({ page, request }) => {
  const email = uniqueEmail("readonly");
  await register(page, email, "Олег Власов");
  await verifyEmail(page, request, email);
  await page.goto("/app");
  await page.getByRole("button", { name: "+ Новый проект" }).click();
  await page.getByLabel("Название").fill("Запуск сайта");
  await page.getByRole("button", { name: "Создать", exact: true }).click();
  await page.getByRole("region", { name: "To do" }).getByRole("button", { name: "+ Добавить задачу" }).click();
  await page.getByLabel("Название задачи").fill("Подготовить релиз");
  await page.keyboard.press("Enter");
  await page.getByRole("link", { name: "Биллинг" }).click();
  await page.getByRole("button", { name: "Перейти на Pro" }).click();
  await pay(page, "4000 0000 0000 9995");
  await expect(page.getByTestId("payment-success")).toBeVisible({ timeout: 30_000 });

  let token = await login(request, email);
  const auth = () => ({ Authorization: `Bearer ${token}` });
  const wsId = ((await (await request.get("/api/workspaces", { headers: auth() })).json()) as { id: number }[])[0].id;
  const status = async () => ((await (await request.get(`/api/w/${wsId}`, { headers: auth() })).json()) as { status: string }).status;
  await request.post("/api/dev/advance-clock", { data: { days: 31 } });
  token = await login(request, email);
  await expect.poll(status, { timeout: 150_000, intervals: [3_000] }).toBe("past_due");
  await request.post("/api/dev/advance-clock", { data: { days: 7 } });
  token = await login(request, email);
  await expect.poll(status, { timeout: 150_000, intervals: [3_000] }).toBe("read_only");

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(PASSWORD);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page.getByTestId("banner-read-only")).toBeVisible();
  await page.screenshot({ path: path.join(OUT, "read-only.png") });
});
