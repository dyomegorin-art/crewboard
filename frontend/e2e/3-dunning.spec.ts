import { expect, test, type APIRequestContext } from "@playwright/test";
import { PASSWORD, pay, register, uniqueEmail, verifyEmail } from "./helpers";

// Карта 4000 0000 0000 9995: оплата проходит, автосписания отклоняются.
// Время двигаем dev-эндпоинтом; воркер каждую минуту проверяет продления и dunning.
test.describe.configure({ timeout: 360_000 });

async function login(request: APIRequestContext, email: string): Promise<string> {
  const r = await request.post("/api/auth/login", { data: { email, password: PASSWORD } });
  return ((await r.json()) as { access_token: string }).access_token;
}

test.afterAll(async ({ request }) => {
  await request.post("/api/dev/reset-clock");
});

test("продление не проходит → past_due → повторы → read_only → оплата новой картой → active", async ({ page, request }) => {
  const email = uniqueEmail("dunning");
  await register(page, email);
  await verifyEmail(page, request, email);
  await page.goto("/app");
  await page.getByRole("link", { name: "Биллинг" }).click();
  await page.getByRole("button", { name: "Перейти на Pro" }).click();
  await pay(page, "4000 0000 0000 9995");
  await expect(page.getByTestId("payment-success")).toBeVisible({ timeout: 30_000 });

  let token = await login(request, email);
  const auth = () => ({ Authorization: `Bearer ${token}` });
  const wsId = ((await (await request.get("/api/workspaces", { headers: auth() })).json()) as { id: number }[])[0].id;
  const status = async () => ((await (await request.get(`/api/w/${wsId}`, { headers: auth() })).json()) as { status: string }).status;
  const advance = async (days: number) => {
    await request.post("/api/dev/advance-clock", { data: { days } });
    token = await login(request, email); // refresh-токены живут 30 дней — после скачка входим заново
  };

  // Конец оплаченного периода: воркер создаёт счёт, списание отклоняется.
  await advance(31);
  await expect.poll(status, { timeout: 150_000, intervals: [3_000] }).toBe("past_due");

  // Баннер у участников виден в интерфейсе.
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(PASSWORD);
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page.getByTestId("banner-past-due")).toBeVisible();

  // Через 7 дней после первой неудачи — режим чтения.
  await advance(7);
  await expect.poll(status, { timeout: 150_000, intervals: [3_000] }).toBe("read_only");
  const blocked = await request.post(`/api/w/${wsId}/projects`, { data: { name: "nope" }, headers: auth() });
  expect(blocked.status()).toBe(402);
  expect((await blocked.json()).error.code).toBe("workspace_read_only");

  // Владелец обновляет карту и оплачивает сразу.
  await page.goto("/app");
  const banner = page.getByTestId("banner-read-only");
  await expect(banner).toBeVisible();
  await banner.getByRole("link", { name: "Биллинг" }).click();
  await page.getByRole("button", { name: "Обновить карту и оплатить" }).click();
  await pay(page, "4242 4242 4242 4242");
  await expect(page.getByTestId("payment-success")).toBeVisible({ timeout: 30_000 });
  await expect.poll(status, { timeout: 30_000 }).toBe("active");
  const ok = await request.post(`/api/w/${wsId}/projects`, { data: { name: "back to work" }, headers: auth() });
  expect(ok.status()).toBe(201);
});
