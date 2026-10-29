import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { ApiError } from "../api/client";
import type { Invitation, Member, Role } from "../api/types";
import { Avatar, Button, Card, Empty, ErrorBox, Field, Modal, Select, Skeleton } from "../components/ui";
import { useAuth } from "../hooks/auth";
import { useWorkspace } from "../hooks/workspace";
import { date, errorMessage, money, roleLabel } from "../lib/format";
import { promptUpgrade, toast } from "../lib/ui-events";

const inviteStatus: Record<Invitation["status"], string> = {
  pending: "Ожидает принятия",
  awaiting_payment: "Ожидает оплаты места",
  accepted: "Принято",
  revoked: "Отозвано",
  expired: "Истекло",
};

function onPlanError(err: unknown): boolean {
  if (err instanceof ApiError && err.code.startsWith("plan_limit_")) {
    promptUpgrade({ code: err.code, message: err.message });
    return true;
  }
  return false;
}

export function MembersPage() {
  const { ws, can, readOnly } = useWorkspace();
  const { user } = useAuth();
  const qc = useQueryClient();
  const [inviting, setInviting] = useState(false);
  const members = useQuery({ queryKey: ["members", ws.id], queryFn: () => api.members(ws.id) });
  const invites = useQuery({ queryKey: ["invitations", ws.id], queryFn: () => api.invitations(ws.id), enabled: can("members.invite") });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["members", ws.id] });
    void qc.invalidateQueries({ queryKey: ["invitations", ws.id] });
    void qc.invalidateQueries({ queryKey: ["workspace", ws.id] });
  };
  const fail = (e: unknown) => {
    if (!onPlanError(e)) toast(errorMessage(e), "error");
  };
  const role = useMutation({ mutationFn: (v: { id: number; role: Role }) => api.changeRole(ws.id, v.id, v.role), onSuccess: refresh, onError: fail });
  const remove = useMutation({ mutationFn: (id: number) => api.removeMember(ws.id, id), onSuccess: refresh, onError: fail });
  const revoke = useMutation({ mutationFn: (id: number) => api.revokeInvite(ws.id, id), onSuccess: refresh, onError: fail });
  const resend = useMutation({ mutationFn: (id: number) => api.resendInvite(ws.id, id), onSuccess: () => { refresh(); toast("Приглашение отправлено повторно"); }, onError: fail });
  const canManage = can("members.manage") && !readOnly;

  return (
    <div className="mx-auto max-w-3xl space-y-5 p-4 sm:p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Участники</h1>
        {can("members.invite") && !readOnly && <Button onClick={() => setInviting(true)}>Пригласить</Button>}
      </div>

      <Card>
        {members.isPending && <Skeleton className="h-24 w-full" />}
        {members.isError && <ErrorBox message={errorMessage(members.error)} />}
        <ul className="-my-2 divide-y divide-line" data-testid="members-list">
          {members.data?.map((m: Member) => (
            <li key={m.user_id} className="flex flex-wrap items-center gap-3 py-3">
              <Avatar name={m.name} size={32} />
              <div className="min-w-0 flex-1">
                <div className="truncate font-medium">{m.name}{m.user_id === user?.id && <span className="ml-1 text-xs text-muted">(вы)</span>}</div>
                <div className="truncate text-xs text-muted">{m.email}</div>
              </div>
              {canManage && !m.is_owner && m.user_id !== user?.id ? (
                <Select aria-label={`Роль ${m.name}`} value={m.role} onChange={(e) => role.mutate({ id: m.user_id, role: e.target.value as Role })}>
                  {(m.role === "guest" ? ["guest"] : ["admin", "member"]).map((r) => <option key={r} value={r}>{roleLabel[r as Role]}</option>)}
                </Select>
              ) : (
                <span className="rounded bg-black/5 px-2 py-0.5 text-xs">{roleLabel[m.role]}</span>
              )}
              {!m.is_owner && ((canManage && m.user_id !== user?.id) || m.user_id === user?.id) && !readOnly && (
                <Button variant="ghost" onClick={() => window.confirm(m.user_id === user?.id ? "Покинуть воркспейс?" : `Удалить ${m.name}?`) && remove.mutate(m.user_id)}>
                  {m.user_id === user?.id ? "Выйти" : "Удалить"}
                </Button>
              )}
            </li>
          ))}
        </ul>
        {ws.plan === "pro" && (
          <p className="mt-4 text-xs text-muted">
            Платные места: занято {ws.paid_seats_used} из {ws.seats}. Гости бесплатны. Удалённый участник освобождает место со следующего периода.
          </p>
        )}
      </Card>

      {can("members.invite") && (
        <Card title="Приглашения">
          {invites.isPending && <Skeleton className="h-12 w-full" />}
          {invites.data?.length === 0 && <Empty title="Приглашений нет" />}
          <ul className="-my-2 divide-y divide-line" data-testid="invitations-list">
            {invites.data?.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{i.email}</div>
                  <div className="text-xs text-muted">
                    {roleLabel[i.role]} · {inviteStatus[i.status]}
                    {i.status === "awaiting_payment" && i.amount ? ` · доплата ${money(i.amount)}` : ""}
                    {i.status === "pending" && ` · до ${date(i.expires_at)}`}
                  </div>
                </div>
                {!readOnly && ["pending", "expired"].includes(i.status) && (
                  <Button variant="secondary" onClick={() => resend.mutate(i.id)}>Отправить снова</Button>
                )}
                {!readOnly && ["pending", "expired", "awaiting_payment"].includes(i.status) && (
                  <Button variant="ghost" onClick={() => revoke.mutate(i.id)}>Отозвать</Button>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}
      {inviting && <InviteModal onClose={() => setInviting(false)} onDone={refresh} />}
    </div>
  );
}

function InviteModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { ws } = useWorkspace();
  const { user } = useAuth();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [amount, setAmount] = useState<number | null>(null);
  const preview = useMutation({
    mutationFn: () => api.previewInvite(ws.id, email, role),
    onSuccess: (p) => {
      if (p.requires_payment) setAmount(p.amount);
      else send.mutate(undefined);
    },
    onError: (e) => onPlanError(e) && onClose(),
  });
  const send = useMutation({
    mutationFn: (confirm?: number) => api.invite(ws.id, email, role, confirm),
    onSuccess: () => {
      toast(amount ? "Приглашение создано: ждём оплату места" : "Приглашение отправлено");
      onDone();
      onClose();
    },
    onError: (e) => onPlanError(e) && onClose(),
  });
  const err = (preview.error ?? send.error) as unknown;
  return (
    <Modal title={amount !== null ? "Подтвердите доплату" : "Пригласить в воркспейс"} onClose={onClose}>
      {!user?.email_verified && <div className="mb-3"><ErrorBox message="Подтвердите свой email, чтобы приглашать участников." /></div>}
      {amount !== null ? (
        <div data-testid="surcharge-confirm">
          <p className="text-sm">
            Будет списано <b>{money(amount)}</b> за остаток текущего периода: пропорциональная доплата за новое место для {email}.
            Дальше место входит в обычное продление.
          </p>
          <p className="mt-2 text-xs text-muted">Приглашение уйдёт после подтверждения оплаты. Деньги за места не возвращаются.</p>
          {err ? <div className="mt-3"><ErrorBox message={errorMessage(err)} /></div> : null}
          <div className="mt-4 flex gap-2">
            <Button onClick={() => send.mutate(amount)} loading={send.isPending}>Оплатить {money(amount)} и пригласить</Button>
            <Button variant="secondary" onClick={() => setAmount(null)}>Назад</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); preview.mutate(); }} className="space-y-3">
          {err ? <ErrorBox message={errorMessage(err)} /> : null}
          <Field label="Email" type="email" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
          <Select label="Роль" value={role} onChange={(e) => setRole(e.target.value as Role)} className="w-full">
            <option value="member">Участник</option>
            <option value="admin">Админ</option>
            <option value="guest">Гость (бесплатно, только Pro)</option>
          </Select>
          <Button type="submit" loading={preview.isPending || send.isPending}>Продолжить</Button>
        </form>
      )}
    </Modal>
  );
}
