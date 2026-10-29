import { ApiError } from "../api/client";
import type { Period, Priority, Role } from "../api/types";

/** Копейки → «1 234,50 ₽». Деньги в API — всегда целые копейки. */
export function money(kopecks: number): string {
  const rub = Math.floor(kopecks / 100);
  const kop = kopecks % 100;
  const base = rub.toLocaleString("ru-RU");
  return `${base}${kop ? `,${String(kop).padStart(2, "0")}` : ""} ₽`;
}

export const date = (iso: string | null | undefined): string =>
  iso ? new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: "numeric" }) : "—";

export const dateTime = (iso: string): string =>
  new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

export function daysLeft(iso: string | null): number {
  if (!iso) return 0;
  return Math.max(0, Math.ceil((new Date(iso).getTime() - Date.now()) / 86400000));
}

export const periodLabel: Record<Period, string> = { month: "месяц", year: "год" };
export const roleLabel: Record<Role, string> = {
  owner: "Владелец",
  admin: "Админ",
  member: "Участник",
  guest: "Гость",
};
export const priorityLabel: Record<Priority, string> = {
  low: "Низкий",
  medium: "Средний",
  high: "Высокий",
  urgent: "Срочный",
};

const MESSAGES: Record<string, string> = {
  invalid_credentials: "Неверный email или пароль",
  email_taken: "Этот email уже зарегистрирован",
  rate_limited: "Слишком много попыток, подождите минуту",
  invalid_token: "Ссылка недействительна или устарела",
  email_not_verified: "Сначала подтвердите email",
  forbidden: "Недостаточно прав для этого действия",
  workspace_read_only: "Воркспейс в режиме чтения: оплатите подписку, чтобы редактировать",
  plan_limit_members: "На тарифе Free — не больше 3 участников",
  plan_limit_projects: "На тарифе Free — не больше 3 активных проектов",
  plan_limit_guests: "Гости доступны на тарифе Pro",
  plan_limit_statuses: "Свои колонки доступны на тарифе Pro",
  plan_limit_export: "Экспорт CSV доступен на тарифе Pro",
  assignee_not_member: "Исполнитель должен быть участником воркспейса",
  status_not_empty: "Удалить можно только пустую колонку",
  project_archived: "Проект в архиве — он доступен только для чтения",
  already_member: "Этот пользователь уже в воркспейсе",
  already_invited: "Этому адресу уже отправлено приглашение",
  invitation_email_mismatch: "Приглашение выписано на другой email",
  invitation_expired: "Срок приглашения истёк",
  invitation_revoked: "Приглашение отозвано",
  invitation_awaiting_payment: "Место под это приглашение ещё не оплачено",
  trial_already_used: "Пробный период уже использован",
  already_subscribed: "Pro уже подключён",
  subscription_past_due: "Сначала оплатите просроченный счёт",
  seats_below_usage: "Нельзя оставить мест меньше, чем занято",
  invalid_card: "Неизвестная тестовая карта",
  payment_confirmation_required: "Подтвердите сумму доплаты",
  subscription_active: "Сначала отмените подписку",
  cannot_remove_owner: "Сначала передайте владение",
};

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return MESSAGES[err.code] ?? err.message;
  return "Что-то пошло не так. Попробуйте ещё раз";
}

/** Безопасный мини-markdown: экранируем всё, затем разрешаем **жирный**, *курсив*, `код`, ссылки и списки. */
export function renderMarkdown(src: string): string {
  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const inline = (s: string) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const out: string[] = [];
  let list = false;
  for (const line of src.split("\n")) {
    const item = /^\s*[-*]\s+(.*)$/.exec(line);
    if (item) {
      if (!list) out.push("<ul>");
      list = true;
      out.push(`<li>${inline(item[1])}</li>`);
      continue;
    }
    if (list) out.push("</ul>");
    list = false;
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return out.join("");
}
