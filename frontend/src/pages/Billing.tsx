import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Invoice, Period } from "../api/types";
import { Button, Card, Empty, ErrorBox, Modal, Select, Skeleton, Spinner } from "../components/ui";
import { useWorkspace } from "../hooks/workspace";
import { date, errorMessage, money, periodLabel } from "../lib/format";
import { toast } from "../lib/ui-events";

const statusLabel: Record<string, string> = {
  trialing: "Пробный период",
  active: "Активна",
  past_due: "Просрочена",
  read_only: "Только чтение",
  canceled: "Отменена",
};
const invoiceStatus: Record<string, string> = { draft: "Черновик", open: "Ожидает оплаты", paid: "Оплачен", failed: "Не прошёл", void: "Аннулирован" };
const kindLabel: Record<string, string> = { subscription: "Подписка", proration: "Доплата за место", renewal: "Продление", card_update: "Проверка карты" };

export function BillingPage() {
  const { ws, can } = useWorkspace();
  const qc = useQueryClient();
  const manage = can("billing.manage");
  const billing = useQuery({ queryKey: ["billing", ws.id], queryFn: () => api.billing(ws.id) });
  const invoices = useQuery({ queryKey: ["invoices", ws.id], queryFn: () => api.invoices(ws.id) });
  const [period, setPeriod] = useState<Period>("month");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["billing", ws.id] });
    void qc.invalidateQueries({ queryKey: ["workspace", ws.id] });
    void qc.invalidateQueries({ queryKey: ["workspaces"] });
    void qc.invalidateQueries({ queryKey: ["invoices", ws.id] });
  };
  const onError = (e: unknown) => toast(errorMessage(e), "error");
  const redirect = (r: { checkout_url: string }) => {
    window.location.href = r.checkout_url;
  };
  const trial = useMutation({ mutationFn: () => api.startTrial(ws.id, period), onSuccess: refresh, onError });
  const checkout = useMutation({ mutationFn: () => api.checkout(ws.id, period), onSuccess: redirect, onError });
  const card = useMutation({ mutationFn: () => api.cardCheckout(ws.id), onSuccess: redirect, onError });
  const pay = useMutation({ mutationFn: () => api.payOutstanding(ws.id), onSuccess: redirect, onError });
  const cancel = useMutation({ mutationFn: () => api.cancelSubscription(ws.id), onSuccess: () => { setConfirmCancel(false); refresh(); }, onError });
  const resume = useMutation({ mutationFn: () => api.resumeSubscription(ws.id), onSuccess: refresh, onError });
  const seats = useMutation({ mutationFn: (n: number) => api.setSeats(ws.id, n), onSuccess: refresh, onError });
  const setPeriodM = useMutation({ mutationFn: (p: Period) => api.setPeriod(ws.id, p), onSuccess: refresh, onError });

  if (billing.isPending) return <div className="mx-auto max-w-3xl space-y-4 p-6"><Skeleton className="h-40 w-full" /><Skeleton className="h-40 w-full" /></div>;
  if (billing.isError) return <div className="p-6"><ErrorBox message={errorMessage(billing.error)} onRetry={() => void billing.refetch()} /></div>;
  const b = billing.data;
  const isPro = b.plan === "pro";

  return (
    <div className="mx-auto max-w-3xl space-y-5 p-4 sm:p-6">
      <h1 className="text-xl font-semibold">Биллинг</h1>
      {!manage && <p className="rounded-lg bg-bg px-4 py-2 text-sm text-muted">Вы видите биллинг в режиме просмотра: управлять подпиской может только владелец.</p>}

      <Card title="Тариф" actions={<span data-testid="plan-badge" className={`rounded px-2 py-0.5 text-xs font-semibold ${isPro ? "bg-accent-soft text-accent" : "bg-black/5 text-muted"}`}>{isPro ? "Pro" : "Free"}</span>}>
        <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
          <Row label="Статус" value={<span data-testid="sub-status">{isPro || b.status === "canceled" ? statusLabel[b.status] : "Free"}</span>} />
          {isPro && <Row label="Период оплаты" value={`${periodLabel[b.billing_period]}${b.pending_period ? ` → ${periodLabel[b.pending_period]} со следующего` : ""}`} />}
          {isPro && b.trial_end && <Row label="Пробный период до" value={date(b.trial_end)} />}
          {isPro && b.current_period_end && !b.trial_end && <Row label="Текущий период до" value={date(b.current_period_end)} />}
          {isPro && <Row label="Следующее списание" value={b.cancel_at_period_end ? "Подписка отменена" : b.next_charge_at ? `${money(b.next_charge_amount)} · ${date(b.next_charge_at)}` : "—"} />}
          <Row label="Карта" value={b.card ? `${b.card.brand} •••• ${b.card.last4}` : "Не привязана"} />
        </dl>

        {b.status === "past_due" && <div className="mt-4"><ErrorBox message="Последнее списание не прошло. Мы повторим попытку через 1, 3 и 5 дней; через 7 дней воркспейс перейдёт в режим чтения." /></div>}
        {b.status === "read_only" && <div className="mt-4"><ErrorBox message="Воркспейс в режиме чтения. Обновите карту и оплатите счёт, чтобы вернуть редактирование." /></div>}

        {manage && (
          <div className="mt-5 flex flex-wrap items-center gap-2">
            {!isPro && (
              <>
                <Select aria-label="Период" value={period} onChange={(e) => setPeriod(e.target.value as Period)}>
                  <option value="month">{money(b.seat_price_month)} за место в месяц</option>
                  <option value="year">{money(b.seat_price_year)} за место в год</option>
                </Select>
                <Button onClick={() => checkout.mutate()} loading={checkout.isPending}>Перейти на Pro</Button>
                {b.trial_available && <Button variant="secondary" onClick={() => trial.mutate()} loading={trial.isPending}>14 дней бесплатно</Button>}
              </>
            )}
            {b.status === "trialing" && (
              <>
                <Button onClick={() => checkout.mutate()} loading={checkout.isPending}>Оплатить сейчас</Button>
                <Button variant="secondary" onClick={() => card.mutate()} loading={card.isPending}>Привязать карту</Button>
              </>
            )}
            {(b.status === "past_due" || b.status === "read_only") && b.outstanding_invoice_id && (
              <Button onClick={() => pay.mutate()} loading={pay.isPending}>Обновить карту и оплатить</Button>
            )}
            {b.status === "active" && isPro && <Button variant="secondary" onClick={() => card.mutate()} loading={card.isPending}>Обновить карту</Button>}
            {isPro && !b.cancel_at_period_end && <Button variant="ghost" onClick={() => setConfirmCancel(true)}>Отменить подписку</Button>}
            {isPro && b.cancel_at_period_end && <Button variant="secondary" onClick={() => resume.mutate()} loading={resume.isPending}>Отменить отмену</Button>}
          </div>
        )}
        {b.provider === "mock" && <p className="mt-3 text-xs text-muted">Тестовый провайдер: карты <code>4242…4242</code> (успех), <code>4000…0002</code> (отказ), <code>4000…9995</code> (потом автосписания отклоняются).</p>}
      </Card>

      {isPro && (
        <Card title="Места">
          <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
            <Row label="Занято" value={`${b.seats_used}${b.seats_reserved ? ` (+${b.seats_reserved} приглашено)` : ""}`} />
            <Row label="Оплачено" value={String(b.seats_paid)} />
            <Row label="Со следующего периода" value={String(b.pending_seats ?? b.seats_paid)} />
            <Row label="Цена места" value={`${money(b.billing_period === "year" ? b.seat_price_year : b.seat_price_month)} / ${periodLabel[b.billing_period]}`} />
          </dl>
          <p className="mt-3 text-xs text-muted">Новое место в середине периода оплачивается пропорционально остатку. Уменьшение мест действует со следующего периода, деньги за текущий не возвращаются.</p>
          {manage && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {b.seats_paid > Math.max(b.seats_used + b.seats_reserved, 1) && (
                <Button variant="secondary" onClick={() => seats.mutate(Math.max(b.seats_used + b.seats_reserved, 1))}>Освободить лишние места</Button>
              )}
              <Select aria-label="Период на следующий цикл" value={b.pending_period ?? b.billing_period} onChange={(e) => setPeriodM.mutate(e.target.value as Period)}>
                <option value="month">Платить помесячно</option>
                <option value="year">Платить раз в год</option>
              </Select>
            </div>
          )}
        </Card>
      )}

      <Card title="Счета">
        {invoices.isPending && <Skeleton className="h-16 w-full" />}
        {invoices.data?.length === 0 && <Empty title="Счетов пока нет" />}
        {invoices.data && invoices.data.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-sm" data-testid="invoices">
              <thead className="text-left text-muted">
                <tr><th className="pb-2 font-medium">Дата</th><th className="pb-2 font-medium">Тип</th><th className="pb-2 font-medium">Статус</th><th className="pb-2 text-right font-medium">Сумма</th></tr>
              </thead>
              <tbody>
                {invoices.data.map((i: Invoice) => (
                  <InvoiceRows key={i.id} i={i} open={expanded === i.id} toggle={() => setExpanded(expanded === i.id ? null : i.id)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {confirmCancel && (
        <Modal title="Отменить подписку?" onClose={() => setConfirmCancel(false)}>
          <p className="text-sm">Pro будет работать до {date(b.current_period_end)}, затем воркспейс перейдёт на Free. Данные не удаляются: лишние проекты уйдут в архив (только чтение).</p>
          <div className="mt-4 flex gap-2">
            <Button variant="danger" onClick={() => cancel.mutate()} loading={cancel.isPending}>Отменить в конце периода</Button>
            <Button variant="secondary" onClick={() => setConfirmCancel(false)}>Оставить</Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 font-medium">{value}</dd>
    </div>
  );
}

function InvoiceRows({ i, open, toggle }: { i: Invoice; open: boolean; toggle: () => void }) {
  return (
    <>
      <tr className="cursor-pointer border-t border-line hover:bg-bg" onClick={toggle}>
        <td className="py-2">{date(i.created_at)}</td>
        <td className="py-2">{kindLabel[i.kind] ?? i.kind}</td>
        <td className="py-2">{invoiceStatus[i.status] ?? i.status}</td>
        <td className="py-2 text-right font-medium">{money(i.amount)}</td>
      </tr>
      {open && (
        <tr className="bg-bg">
          <td colSpan={4} className="px-3 py-2 text-xs text-muted">
            {i.lines.map((l) => <div key={l.description}>{l.description} — {l.quantity} × {money(l.unit_amount)} = {money(l.amount)}</div>)}
            {i.period_start && i.period_end && <div>Период: {date(i.period_start)} — {date(i.period_end)}</div>}
          </td>
        </tr>
      )}
    </>
  );
}

/** Страница возврата из checkout: состояние меняет только вебхук, мы лишь опрашиваем. */
export function BillingReturn() {
  const { ws } = useWorkspace();
  const [params] = useSearchParams();
  const invoiceId = Number(params.get("invoice"));
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["invoice-status", ws.id, invoiceId],
    queryFn: () => api.invoiceStatus(ws.id, invoiceId),
    refetchInterval: (query) => (query.state.data?.invoice_status === "open" ? 1000 : false),
    enabled: Number.isFinite(invoiceId),
  });
  const status = q.data?.invoice_status;
  useEffect(() => {
    if (status && status !== "open") {
      void qc.invalidateQueries({ queryKey: ["billing", ws.id] });
      void qc.invalidateQueries({ queryKey: ["workspace", ws.id] });
      void qc.invalidateQueries({ queryKey: ["workspaces"] });
      void qc.invalidateQueries({ queryKey: ["invoices", ws.id] });
    }
  }, [status, qc, ws.id]);

  return (
    <div className="mx-auto max-w-md p-6 text-center" data-testid="billing-return">
      {(!status || status === "open") && !q.isError && (
        <>
          <div className="mb-3 flex justify-center"><Spinner /></div>
          <p className="font-medium">Ожидаем подтверждение оплаты…</p>
          <p className="mt-1 text-sm text-muted">Платёжный провайдер сообщит результат вебхуком. Обычно это несколько секунд.</p>
        </>
      )}
      {status === "paid" && (
        <>
          <p className="text-xl font-semibold text-ok" data-testid="payment-success">Оплата прошла</p>
          <p className="mt-1 text-sm text-muted">Тариф: {q.data?.plan === "pro" ? "Pro" : "Free"}, статус подписки: {statusLabel[q.data?.subscription_status ?? "active"]}.</p>
        </>
      )}
      {(status === "failed" || status === "void") && (
        <>
          <p className="text-xl font-semibold text-danger" data-testid="payment-failed">Оплата не прошла</p>
          <p className="mt-1 text-sm text-muted">Карта отклонена. Подписка не изменилась; попробуйте другую карту.</p>
        </>
      )}
      {q.isError && <ErrorBox message={errorMessage(q.error)} />}
      <Link to={`/w/${ws.slug}/billing`} className="mt-5 inline-block"><Button variant="secondary">К биллингу</Button></Link>
    </div>
  );
}
