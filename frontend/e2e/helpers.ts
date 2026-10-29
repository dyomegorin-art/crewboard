import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const MAILPIT = process.env.MAILPIT_URL ?? "http://localhost:8025";
export const PASSWORD = "e2e-password-1";

export const uniqueEmail = (prefix = "e2e") => `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e4)}@example.com`;

/** Ждёт письмо в Mailpit (его шлёт воркер из outbox) и возвращает текст. */
export async function waitForMail(request: APIRequestContext, to: string, subjectPart: string): Promise<string> {
  let text = "";
  await expect
    .poll(
      async () => {
        const list = await request.get(`${MAILPIT}/api/v1/search`, { params: { query: `to:${to}` } });
        const body = (await list.json()) as { messages?: { ID: string; Subject: string }[] };
        const hit = body.messages?.find((m) => m.Subject.includes(subjectPart));
        if (!hit) return false;
        const msg = await request.get(`${MAILPIT}/api/v1/message/${hit.ID}`);
        text = ((await msg.json()) as { Text: string }).Text;
        return true;
      },
      { timeout: 45_000, message: `письмо «${subjectPart}» для ${to}` },
    )
    .toBe(true);
  return text;
}

export function linkFrom(text: string, path: string): string {
  const m = new RegExp(`https?://[^\\s]*${path}[^\\s]*`).exec(text);
  if (!m) throw new Error(`no ${path} link in mail:\n${text}`);
  return m[0];
}

export async function register(page: Page, email: string, name = "E2E Tester"): Promise<void> {
  await page.goto("/register");
  await page.getByLabel("Имя").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Пароль").fill(PASSWORD);
  await page.getByRole("button", { name: "Создать аккаунт" }).click();
  await expect(page).toHaveURL(/check-email/);
}

export async function verifyEmail(page: Page, request: APIRequestContext, email: string): Promise<void> {
  const mail = await waitForMail(request, email, "Подтвердите");
  // Ссылка ведёт на PUBLIC_URL; в тестах заходим тем же хостом, что и baseURL.
  const url = new URL(linkFrom(mail, "/verify-email"));
  await page.goto(`${url.pathname}${url.search}`);
  await expect(page.getByTestId("email-verified")).toBeVisible();
}

export async function pay(page: Page, card = "4242 4242 4242 4242"): Promise<void> {
  await expect(page.getByTestId("test-payment-banner")).toContainText("Тестовый платёж");
  await page.getByLabel("Номер карты").fill(card);
  await page.getByRole("button", { name: /^Оплатить/ }).click();
}

export async function workspaceId(page: Page): Promise<number> {
  const token = await page.evaluate(async () => {
    const r = await fetch("/api/auth/refresh", { method: "POST", headers: { "X-Requested-With": "e2e" } });
    return ((await r.json()) as { access_token: string }).access_token;
  });
  const list = await page.request.get("/api/workspaces", { headers: { Authorization: `Bearer ${token}` } });
  return ((await list.json()) as { id: number }[])[0].id;
}
