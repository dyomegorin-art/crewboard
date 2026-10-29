import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../hooks/auth";
import { useWorkspaceEvents } from "../hooks/events";
import { WorkspaceProvider, useWorkspace } from "../hooks/workspace";
import { daysLeft, errorMessage, money } from "../lib/format";
import { subscribeUpgrade, toast, type UpgradePrompt } from "../lib/ui-events";
import { Banner, Button, Empty, ErrorBox, Field, Modal, Skeleton, Toasts } from "./ui";

export function AppShell() {
  const { slug = "" } = useParams();
  const { user } = useAuth();
  const list = useQuery({ queryKey: ["workspaces"], queryFn: api.workspaces });
  const current = list.data?.find((w) => w.slug === slug);

  if (list.isPending) return <ShellSkeleton />;
  if (list.isError) return <div className="p-6"><ErrorBox message={errorMessage(list.error)} onRetry={() => void list.refetch()} /></div>;
  if (!current)
    return (
      <div className="mx-auto max-w-md p-10">
        <Empty title="Воркспейс не найден" hint="Возможно, у вас нет к нему доступа." action={<Link to="/app"><Button>К моим воркспейсам</Button></Link>} />
      </div>
    );

  return (
    <WorkspaceProvider id={current.id}>
      {(ws) =>
        ws && user ? (
          <Frame userId={user.id} workspaces={list.data ?? []} />
        ) : (
          <ShellSkeleton />
        )
      }
    </WorkspaceProvider>
  );
}

function ShellSkeleton() {
  return (
    <div className="flex h-full">
      <div className="hidden w-60 border-r border-line bg-surface p-4 md:block"><Skeleton className="h-6 w-32" /></div>
      <div className="flex-1 p-6"><Skeleton className="h-8 w-64" /></div>
    </div>
  );
}

function Frame({ userId, workspaces }: { userId: number; workspaces: { id: number; name: string; slug: string }[] }) {
  const { ws } = useWorkspace();
  const { user } = useAuth();
  useWorkspaceEvents(ws.id, userId);
  const [menu, setMenu] = useState(false);
  const [upgrade, setUpgrade] = useState<UpgradePrompt | null>(null);
  useEffect(() => subscribeUpgrade(setUpgrade), []);
  useEffect(() => setMenu(false), [ws.slug]);

  return (
    <div className="flex h-full flex-col">
      <Banners />
      <div className="flex min-h-0 flex-1">
        <aside className={`${menu ? "fixed inset-0 z-40 block bg-black/30" : "hidden"} md:static md:block md:bg-transparent`} onClick={() => setMenu(false)}>
          <div className="h-full w-64 border-r border-line bg-surface md:w-60" onClick={(e) => e.stopPropagation()}>
            <Sidebar workspaces={workspaces} />
          </div>
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-12 items-center gap-3 border-b border-line bg-surface px-4 md:hidden">
            <button aria-label="Меню" className="rounded p-1 hover:bg-black/5" onClick={() => setMenu(true)}>☰</button>
            <span className="truncate font-semibold">{ws.name}</span>
          </header>
          <main className="min-h-0 flex-1 overflow-auto">
            {user && !user.email_verified && <VerifyNudge />}
            <Outlet />
          </main>
        </div>
      </div>
      {upgrade && <UpgradeModal prompt={upgrade} onClose={() => setUpgrade(null)} />}
      <Toasts />
    </div>
  );
}

function VerifyNudge() {
  const resend = useMutation({ mutationFn: api.resendVerification, onSuccess: () => toast("Письмо отправлено") });
  return (
    <Banner tone="warn" testId="verify-banner">
      <span>Подтвердите email: пока он не подтверждён, нельзя приглашать участников и платить.</span>
      <button className="font-medium underline" onClick={() => resend.mutate()}>Отправить письмо ещё раз</button>
    </Banner>
  );
}

function Banners() {
  const { ws, can } = useWorkspace();
  const slug = ws.slug;
  const billingLink = can("billing.view") ? (
    <Link className="font-medium underline" to={`/w/${slug}/billing`}>Биллинг</Link>
  ) : null;
  return (
    <>
      {ws.status === "trialing" && (
        <Banner tone="info" testId="banner-trialing">
          <span>Пробный период Pro: осталось {daysLeft(ws.trial_end)} дн.</span>
          {billingLink}
        </Banner>
      )}
      {ws.status === "past_due" && (
        <Banner tone="danger" testId="banner-past-due">
          <span>Не удалось списать оплату. Мы повторим попытку; обновите карту, чтобы не потерять доступ.</span>
          {billingLink}
        </Banner>
      )}
      {ws.status === "read_only" && (
        <Banner tone="danger" testId="banner-read-only">
          <span>Воркспейс в режиме чтения: оплата не прошла. Изменения недоступны, пока владелец не оплатит счёт.</span>
          {billingLink}
        </Banner>
      )}
      {ws.cancel_at_period_end && ws.status !== "read_only" && (
        <Banner tone="warn" testId="banner-cancel">
          <span>Подписка будет отменена в конце периода ({new Date(ws.current_period_end ?? "").toLocaleDateString("ru-RU")}).</span>
          {billingLink}
        </Banner>
      )}
    </>
  );
}

function Sidebar({ workspaces }: { workspaces: { id: number; name: string; slug: string }[] }) {
  const { ws, can } = useWorkspace();
  const { user, signOut } = useAuth();
  const nav = useNavigate();
  const [creating, setCreating] = useState(false);
  const projects = useQuery({ queryKey: ["projects", ws.id], queryFn: () => api.projects(ws.id) });
  const item = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm ${isActive ? "bg-accent-soft font-medium text-accent" : "text-ink hover:bg-black/5"}`;
  return (
    <div className="flex h-full flex-col p-3">
      <select
        aria-label="Воркспейс"
        className="mb-3 h-9 w-full rounded-md border border-line bg-surface px-2 text-sm font-medium"
        value={ws.slug}
        onChange={(e) => (e.target.value === "__new" ? setCreating(true) : nav(`/w/${e.target.value}`))}
      >
        {workspaces.map((w) => (
          <option key={w.id} value={w.slug}>{w.name}</option>
        ))}
        <option value="__new">+ Новый воркспейс…</option>
      </select>
      <nav className="space-y-0.5">
        <NavLink to={`/w/${ws.slug}/my-tasks`} className={item}>Мои задачи</NavLink>
        <NavLink to={`/w/${ws.slug}/activity`} className={item}>Активность</NavLink>
        <NavLink to={`/w/${ws.slug}/members`} className={item}>Участники</NavLink>
        {can("billing.view") && <NavLink to={`/w/${ws.slug}/billing`} className={item}>Биллинг</NavLink>}
        {can("workspace.update") && <NavLink to={`/w/${ws.slug}/settings`} className={item}>Настройки</NavLink>}
      </nav>
      <div className="mb-1 mt-5 px-2.5 text-xs font-semibold uppercase tracking-wide text-muted">Проекты</div>
      <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto" aria-label="Проекты">
        {projects.isPending && <Skeleton className="mx-2 h-5 w-32" />}
        {projects.data?.map((p) => (
          <NavLink key={p.id} to={`/w/${ws.slug}/p/${p.id}`} className={item}>
            <span className="rounded bg-black/5 px-1 text-[10px] font-semibold text-muted">{p.key}</span>
            <span className="truncate">{p.name}</span>
          </NavLink>
        ))}
        {projects.data?.length === 0 && <p className="px-2.5 text-xs text-muted">Проектов пока нет</p>}
        {can("projects.manage") && <NewProject />}
      </nav>
      <div className="mt-3 border-t border-line pt-3 text-sm">
        <div className="truncate px-1 font-medium">{user?.name}</div>
        <div className="truncate px-1 text-xs text-muted">{user?.email}</div>
        <button
          className="mt-2 w-full rounded-md px-2.5 py-1.5 text-left text-muted hover:bg-black/5"
          onClick={() => void signOut().then(() => nav("/"))}
        >
          Выйти
        </button>
      </div>
      {creating && <CreateWorkspace onClose={() => setCreating(false)} />}
    </div>
  );
}

function NewProject() {
  const { ws } = useWorkspace();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const m = useMutation({
    mutationFn: () => api.createProject(ws.id, name),
    onSuccess: (p) => {
      void qc.invalidateQueries({ queryKey: ["projects", ws.id] });
      setOpen(false);
      setName("");
      nav(`/w/${ws.slug}/p/${p.id}`);
    },
  });
  return (
    <>
      <button className="w-full rounded-md px-2.5 py-1.5 text-left text-sm text-accent hover:bg-black/5" onClick={() => setOpen(true)}>
        + Новый проект
      </button>
      {open && (
        <Modal title="Новый проект" onClose={() => setOpen(false)}>
          <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }} className="space-y-3">
            {m.isError && <ErrorBox message={errorMessage(m.error)} />}
            <Field label="Название" required autoFocus value={name} onChange={(e) => setName(e.target.value)} />
            <Button type="submit" loading={m.isPending}>Создать</Button>
          </form>
        </Modal>
      )}
    </>
  );
}

function CreateWorkspace({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [name, setName] = useState("");
  const m = useMutation({
    mutationFn: () => api.createWorkspace(name),
    onSuccess: async (w) => {
      await qc.invalidateQueries({ queryKey: ["workspaces"] });
      onClose();
      nav(`/w/${w.slug}`);
    },
  });
  return (
    <Modal title="Новый воркспейс" onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }} className="space-y-3">
        {m.isError && <ErrorBox message={errorMessage(m.error)} />}
        <Field label="Название команды" required autoFocus value={name} onChange={(e) => setName(e.target.value)} />
        <Button type="submit" loading={m.isPending}>Создать</Button>
      </form>
    </Modal>
  );
}

/** Модалка «Перейдите на Pro»: показывается по коду plan_limit_* из API. */
export function UpgradeModal({ prompt, onClose }: { prompt: UpgradePrompt; onClose: () => void }) {
  const { ws, can } = useWorkspace();
  return (
    <Modal title="Это возможность тарифа Pro" onClose={onClose}>
      <div data-testid="upgrade-modal">
        <p className="text-sm">{prompt.message}</p>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted">
          <li>Участники без лимита и бесплатные гости</li>
          <li>Неограниченные проекты и свои колонки</li>
          <li>Полная история активности и экспорт CSV</li>
        </ul>
        <p className="mt-3 text-sm text-muted">Pro — {money(49000)} за место в месяц, 14 дней пробного периода без карты.</p>
        <div className="mt-4 flex gap-2">
          {can("billing.manage") ? (
            <Link to={`/w/${ws.slug}/billing`} onClick={onClose}><Button>Перейти на Pro</Button></Link>
          ) : (
            <p className="text-sm text-muted">Попросите владельца воркспейса перейти на Pro.</p>
          )}
          <Button variant="secondary" onClick={onClose}>Не сейчас</Button>
        </div>
      </div>
    </Modal>
  );
}
