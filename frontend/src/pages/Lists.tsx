import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Button, Empty, ErrorBox, PriorityBadge, Skeleton } from "../components/ui";
import { useWorkspace } from "../hooks/workspace";
import { date, dateTime, errorMessage } from "../lib/format";

export function MyTasks() {
  const { ws } = useWorkspace();
  const q = useQuery({ queryKey: ["my-tasks", ws.id], queryFn: () => api.myTasks(ws.id) });
  const projects = useQuery({ queryKey: ["projects", ws.id], queryFn: () => api.projects(ws.id) });
  return (
    <div className="mx-auto max-w-3xl p-4 sm:p-6">
      <h1 className="mb-4 text-xl font-semibold">Мои задачи</h1>
      {q.isPending && <div className="space-y-2">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-12 w-full" />)}</div>}
      {q.isError && <ErrorBox message={errorMessage(q.error)} onRetry={() => void q.refetch()} />}
      {q.data?.length === 0 && <Empty title="Задач на вас нет" hint="Когда кого-то назначат исполнителем, задачи появятся здесь." />}
      <ul className="space-y-2">
        {q.data?.map((t) => (
          <li key={t.id}>
            <Link to={`/w/${ws.slug}/p/${t.project_id}`} className="flex items-center gap-3 rounded-lg border border-line bg-surface px-4 py-3 hover:border-accent/50">
              <span className="w-16 shrink-0 text-xs text-muted">{t.key}</span>
              <span className="min-w-0 flex-1 truncate font-medium">{t.title}</span>
              <PriorityBadge priority={t.priority} />
              <span className="hidden w-24 text-right text-xs text-muted sm:block">{date(t.due_date)}</span>
            </Link>
          </li>
        ))}
      </ul>
      {projects.data && null}
    </div>
  );
}

function label(action: string, detail: Record<string, unknown>): string {
  const d = detail as Record<string, string | undefined>;
  const map: Record<string, string> = {
    "task.created": `создал(а) задачу ${d.key ?? ""} «${d.title ?? ""}»`,
    "task.updated": "обновил(а) задачу",
    "task.moved": `перенёс(ла) задачу: ${d.from} → ${d.to}`,
    "task.deleted": `удалил(а) задачу ${d.key ?? ""}`,
    "comment.added": "оставил(а) комментарий",
    "project.created": `создал(а) проект «${d.name ?? ""}»`,
    "project.renamed": `переименовал(а) проект в «${d.new ?? ""}»`,
    "project.archived": "архивировал(а) проект",
    "project.unarchived": "вернул(а) проект из архива",
    "member.joined": "присоединился(ась) к воркспейсу",
    "member.removed": "удалил(а) участника",
    "member.role_changed": `сменил(а) роль: ${d.old} → ${d.new}`,
    "invitation.created": `пригласил(а) ${d.email ?? ""}`,
    "invitation.revoked": `отозвал(а) приглашение ${d.email ?? ""}`,
    "billing.trial_started": "начал(а) пробный период Pro",
    "billing.payment_succeeded": "Оплата прошла",
    "billing.payment_failed": "Оплата не прошла",
    "billing.past_due": "Подписка просрочена",
    "billing.read_only": "Воркспейс переведён в режим чтения",
    "billing.downgraded": "Подписка закончилась — тариф Free",
    "billing.cancel_requested": "Отмена подписки в конце периода",
    "billing.cancel_revoked": "Отмена подписки отменена",
    "billing.checkout_started": "Начата оплата",
    "billing.renewal_invoiced": "Выставлен счёт на продление",
    access_denied: `попытка действия без прав (${d.permission ?? ""})`,
  };
  return map[action] ?? action;
}

export function ActivityPage() {
  const { ws } = useWorkspace();
  const q = useInfiniteQuery({
    queryKey: ["activity", ws.id],
    queryFn: ({ pageParam }) => api.activity(ws.id, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="mx-auto max-w-3xl p-4 sm:p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Активность</h1>
        {ws.plan === "free" && <span className="text-xs text-muted">Free: последние 7 дней</span>}
      </div>
      {q.isPending && <div className="space-y-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-8 w-full" />)}</div>}
      {q.isError && <ErrorBox message={errorMessage(q.error)} onRetry={() => void q.refetch()} />}
      {q.isSuccess && items.length === 0 && <Empty title="Пока тихо" hint="Здесь появятся действия команды." />}
      <ul className="divide-y divide-line rounded-xl border border-line bg-surface">
        {items.map((a) => (
          <li key={a.id} className="flex flex-wrap items-baseline gap-x-2 px-4 py-2.5 text-sm">
            <b className="font-medium">{a.actor_name ?? "Система"}</b>
            <span>{label(a.action, a.detail)}</span>
            <span className="ml-auto text-xs text-muted">{dateTime(a.created_at)}</span>
          </li>
        ))}
      </ul>
      {q.hasNextPage && (
        <div className="mt-4 flex justify-center">
          <Button variant="secondary" onClick={() => void q.fetchNextPage()} loading={q.isFetchingNextPage}>Показать ещё</Button>
        </div>
      )}
    </div>
  );
}
