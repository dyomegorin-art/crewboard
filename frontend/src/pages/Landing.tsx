import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { Button } from "../components/ui";
import { useAuth } from "../hooks/auth";
import { errorMessage } from "../lib/format";

const REPO_URL = import.meta.env.VITE_REPO_URL ?? "https://github.com/dyomegorin-art/crewboard";

const features = [
  ["Канбан без лишнего", "Колонки, перетаскивание карточек, фильтры, комментарии и история изменений. Доска обновляется у всех в реальном времени."],
  ["Мультитенантность", "Несколько воркспейсов на пользователя, роли owner / admin / member / guest. Чужие данные недоступны на уровне API и базы."],
  ["Оплата за места", "Pro — 490 ₽ за место в месяц. Пропорциональная доплата при приглашении, продления, dunning и вебхуки считаются в нашем коде."],
];

const plans = [
  ["Цена", "0 ₽", "490 ₽ / место / мес."],
  ["Участники", "до 3", "без лимита"],
  ["Гости", "—", "бесплатно"],
  ["Проекты", "до 3 активных", "без лимита"],
  ["Свои колонки", "—", "да"],
  ["История активности", "7 дней", "полная"],
  ["Экспорт CSV", "—", "да"],
];

export function Landing() {
  const { user, signIn } = useAuth();
  const nav = useNavigate();
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const demo = useMutation({
    mutationFn: api.demo,
    onSuccess: async (out) => {
      signIn(out);
      const list = await api.workspaces();
      nav(`/w/${list[0]?.slug ?? ""}`);
    },
  });

  return (
    <div className="min-h-full bg-surface">
      <header className="mx-auto flex max-w-5xl items-center justify-between px-4 py-4">
        <span className="text-lg font-semibold tracking-tight">Crewboard</span>
        <nav className="flex items-center gap-2">
          {user ? (
            <Link to="/app" className="text-sm font-medium text-accent">
              Открыть приложение →
            </Link>
          ) : (
            <>
              <Link to="/login" className="px-2 text-sm text-muted hover:text-ink">
                Войти
              </Link>
              <Link to="/register">
                <Button variant="secondary">Зарегистрироваться</Button>
              </Link>
            </>
          )}
        </nav>
      </header>

      <main className="mx-auto max-w-5xl px-4">
        <section className="py-14 text-center sm:py-20">
          <h1 className="mx-auto max-w-2xl text-4xl font-semibold tracking-tight sm:text-5xl">
            Таск-трекер для команд, который считает места честно
          </h1>
          <p className="mx-auto mt-4 max-w-xl text-lg text-muted">
            Доски, задачи и подписка по числу участников. Учебный портфолио-проект: все данные вымышлены.
          </p>
          <div className="mt-8 flex flex-wrap justify-center gap-3">
            <Button className="h-11 px-5 text-base" onClick={() => demo.mutate()} loading={demo.isPending}>
              Попробовать без регистрации
            </Button>
            <Link to="/register">
              <Button variant="secondary" className="h-11 px-5 text-base">
                Зарегистрироваться
              </Button>
            </Link>
          </div>
          {demo.isError && <p className="mt-3 text-sm text-danger">{errorMessage(demo.error)}</p>}
          {providers.data?.github && (
            <a className="mt-4 inline-block text-sm text-accent" href="/api/auth/github/login">
              Войти через GitHub
            </a>
          )}
        </section>

        <section className="grid gap-4 pb-14 sm:grid-cols-3">
          {features.map(([title, text]) => (
            <div key={title} className="rounded-xl border border-line p-5">
              <h3 className="font-semibold">{title}</h3>
              <p className="mt-2 text-sm text-muted">{text}</p>
            </div>
          ))}
        </section>

        <section className="pb-14">
          <h2 className="mb-4 text-2xl font-semibold tracking-tight">Тарифы</h2>
          <div className="overflow-x-auto rounded-xl border border-line">
            <table className="w-full min-w-[480px] text-sm">
              <thead className="bg-bg text-left text-muted">
                <tr>
                  <th className="px-4 py-2.5 font-medium" />
                  <th className="px-4 py-2.5 font-medium">Free</th>
                  <th className="px-4 py-2.5 font-medium text-accent">Pro</th>
                </tr>
              </thead>
              <tbody>
                {plans.map(([name, free, pro]) => (
                  <tr key={name} className="border-t border-line">
                    <td className="px-4 py-2.5 font-medium">{name}</td>
                    <td className="px-4 py-2.5 text-muted">{free}</td>
                    <td className="px-4 py-2.5">{pro}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-muted">Pro — 4 900 ₽ за место в год. 14 дней пробного периода без карты.</p>
        </section>

        <section className="mb-16 rounded-xl border border-accent/30 bg-accent-soft p-6">
          <h2 className="text-lg font-semibold">Для рекрутеров</h2>
          <ul className="mt-3 list-disc space-y-1.5 pl-5 text-sm">
            <li>Нажмите «Попробовать без регистрации» — откроется готовый воркспейс с 25 задачами и активным пробным периодом Pro.</li>
            <li>
              Оплата тестовая (провайдер mock): карта <code>4242 4242 4242 4242</code> — успех,{" "}
              <code>4000 0000 0000 0002</code> — отказ, <code>4000 0000 0000 9995</code> — оплата проходит, а автосписания
              отклоняются (удобно смотреть dunning).
            </li>
            <li>Загляните в «Биллинг» и «Участники»: приглашение в Pro показывает доплату за остаток периода.</li>
            <li>
              Код и документация:{" "}
              <a className="font-medium text-accent underline" href={REPO_URL} target="_blank" rel="noreferrer">
                GitHub
              </a>
              .
            </li>
          </ul>
        </section>
      </main>
      <footer className="border-t border-line py-6 text-center text-xs text-muted">
        Crewboard — учебный проект. Платежи тестовые, данные вымышлены.
      </footer>
    </div>
  );
}
