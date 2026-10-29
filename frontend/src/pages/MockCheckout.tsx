import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Button, ErrorBox, Field, Skeleton } from "../components/ui";
import { errorMessage, money } from "../lib/format";

const CARDS = [
  ["4242 4242 4242 4242", "Успех"],
  ["4000 0000 0000 0002", "Отказ"],
  ["4000 0000 0000 9995", "Успех, дальше автосписания отклоняются"],
] as const;

export function MockCheckout() {
  const { session = "" } = useParams();
  const nav = useNavigate();
  const [card, setCard] = useState("4242 4242 4242 4242");
  const q = useQuery({ queryKey: ["mock-checkout", session], queryFn: () => api.mockCheckout(session), retry: false });
  const pay = useMutation({
    mutationFn: () => api.mockPay(session, card),
    onSuccess: () => {
      const c = q.data;
      if (c) nav(`/w/${c.slug}/billing/return?invoice=${c.invoice_id}`);
    },
  });

  return (
    <div className="min-h-full bg-bg">
      <div className="bg-amber-400 px-4 py-2 text-center text-sm font-semibold text-black" data-testid="test-payment-banner">
        Тестовый платёж — деньги не списываются
      </div>
      <div className="mx-auto max-w-md px-4 py-10">
        {q.isPending && <Skeleton className="h-64 w-full" />}
        {q.isError && <ErrorBox message="Сессия оплаты не найдена" />}
        {q.data && (
          <div className="rounded-xl border border-line bg-surface p-6 shadow-sm">
            <p className="text-sm text-muted">{q.data.workspace_name}</p>
            <p className="mt-1 text-3xl font-semibold">{money(q.data.amount)}</p>
            <ul className="mt-3 space-y-1 text-sm text-muted">
              {q.data.lines.map((l) => (
                <li key={l.description}>
                  {l.description} × {l.quantity}
                </li>
              ))}
            </ul>
            {q.data.status === "paid" ? (
              <p className="mt-6 text-sm">Этот счёт уже оплачен.</p>
            ) : (
              <form
                className="mt-6 space-y-3"
                onSubmit={(e) => {
                  e.preventDefault();
                  pay.mutate();
                }}
              >
                {pay.isError && <ErrorBox message={errorMessage(pay.error)} />}
                <Field label="Номер карты" inputMode="numeric" value={card} onChange={(e) => setCard(e.target.value)} />
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Срок" defaultValue="12/34" />
                  <Field label="CVC" defaultValue="123" />
                </div>
                <div className="space-y-1 text-xs text-muted">
                  {CARDS.map(([num, label]) => (
                    <button type="button" key={num} className="block text-left hover:text-ink" onClick={() => setCard(num)}>
                      <code>{num}</code> — {label}
                    </button>
                  ))}
                </div>
                <Button type="submit" className="w-full" loading={pay.isPending}>
                  Оплатить {money(q.data.amount)}
                </Button>
              </form>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
