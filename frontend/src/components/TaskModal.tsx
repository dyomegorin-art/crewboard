import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";
import type { Activity, Priority, Status } from "../api/types";
import { useWorkspace } from "../hooks/workspace";
import { dateTime, errorMessage, priorityLabel, renderMarkdown } from "../lib/format";
import { toast } from "../lib/ui-events";
import { Avatar, Button, ErrorBox, Modal, Select, Skeleton } from "./ui";

function describe(a: Activity): string {
  const d = a.detail as Record<string, unknown>;
  switch (a.action) {
    case "task.created":
      return "создал(а) задачу";
    case "task.moved":
      return `перенёс(ла) из «${String(d.from)}» в «${String(d.to)}»`;
    case "task.updated":
      return `изменил(а): ${Object.keys((d.changes as object) ?? {}).join(", ")}`;
    case "comment.added":
      return "оставил(а) комментарий";
    default:
      return a.action;
  }
}

export function TaskModal({ taskId, projectKey, statuses, onClose }: { taskId: number; projectKey: string; statuses: Status[]; onClose: () => void }) {
  const { ws, can, readOnly } = useWorkspace();
  const qc = useQueryClient();
  const task = useQuery({ queryKey: ["task", ws.id, taskId], queryFn: () => api.task(ws.id, taskId) });
  const members = useQuery({ queryKey: ["members", ws.id], queryFn: () => api.members(ws.id) });
  const comments = useQuery({ queryKey: ["task", ws.id, taskId, "comments"], queryFn: () => api.comments(ws.id, taskId) });
  const history = useQuery({ queryKey: ["task", ws.id, taskId, "history"], queryFn: () => api.history(ws.id, taskId) });
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [preview, setPreview] = useState(true);
  const [comment, setComment] = useState("");
  const t = task.data;
  useEffect(() => {
    if (t) {
      setTitle(t.title);
      setDescription(t.description);
    }
  }, [t?.id, t?.updated_at]); // eslint-disable-line react-hooks/exhaustive-deps

  const editable = can("tasks.write") && !readOnly;
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["board", ws.id] });
    void qc.invalidateQueries({ queryKey: ["task", ws.id, taskId] });
    void qc.invalidateQueries({ queryKey: ["my-tasks", ws.id] });
  };
  const update = useMutation({
    mutationFn: (patch: Parameters<typeof api.updateTask>[2]) => api.updateTask(ws.id, taskId, patch),
    onSuccess: refresh,
    onError: (e) => toast(errorMessage(e), "error"),
  });
  const move = useMutation({
    mutationFn: (statusId: number) => api.moveTask(ws.id, taskId, statusId, null),
    onSuccess: refresh,
    onError: (e) => toast(errorMessage(e), "error"),
  });
  const addComment = useMutation({
    mutationFn: () => api.addComment(ws.id, taskId, comment),
    onSuccess: () => {
      setComment("");
      void qc.invalidateQueries({ queryKey: ["task", ws.id, taskId] });
    },
    onError: (e) => toast(errorMessage(e), "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteTask(ws.id, taskId),
    onSuccess: () => {
      refresh();
      void qc.invalidateQueries({ queryKey: ["projects", ws.id] });
      onClose();
    },
    onError: (e) => toast(errorMessage(e), "error"),
  });

  return (
    <Modal title={t ? `${projectKey}-${t.number}` : "Задача"} onClose={onClose} wide>
      {task.isPending && <Skeleton className="h-40 w-full" />}
      {task.isError && <ErrorBox message={errorMessage(task.error)} onRetry={() => void task.refetch()} />}
      {t && (
        <div className="grid gap-6 md:grid-cols-[1fr_16rem]">
          <div className="min-w-0 space-y-4">
            <input
              aria-label="Заголовок"
              className="w-full rounded-md border border-transparent px-1 py-1 text-xl font-semibold outline-none hover:border-line focus:border-accent"
              value={title}
              disabled={!editable}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={() => title.trim() && title !== t.title && update.mutate({ title })}
            />
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-sm font-medium">Описание</span>
                {editable && <button className="text-xs text-accent" onClick={() => setPreview(!preview)}>{preview ? "Редактировать" : "Предпросмотр"}</button>}
              </div>
              {preview || !editable ? (
                <div className="md min-h-[4rem] rounded-md border border-line p-3 text-sm" dangerouslySetInnerHTML={{ __html: renderMarkdown(description) || '<p class="text-muted">Нет описания</p>' }} />
              ) : (
                <textarea
                  aria-label="Описание"
                  rows={6}
                  className="w-full rounded-md border border-line p-3 text-sm outline-none focus:border-accent"
                  placeholder="Поддерживается markdown: **жирный**, *курсив*, `код`, списки"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  onBlur={() => description !== t.description && update.mutate({ description })}
                />
              )}
            </div>

            <div>
              <h3 className="mb-2 text-sm font-medium">Комментарии</h3>
              {comments.isPending && <Skeleton className="h-10 w-full" />}
              <ul className="space-y-3">
                {comments.data?.map((c) => (
                  <li key={c.id} className="flex gap-2">
                    <Avatar name={c.author_name ?? "?"} />
                    <div className="min-w-0 flex-1 rounded-md bg-bg px-3 py-2">
                      <div className="text-xs text-muted">{c.author_name ?? "Удалённый пользователь"} · {dateTime(c.created_at)}</div>
                      <div className="md mt-0.5 text-sm" dangerouslySetInnerHTML={{ __html: renderMarkdown(c.body) }} />
                    </div>
                  </li>
                ))}
              </ul>
              {can("comments.write") && !readOnly && (
                <form className="mt-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (comment.trim()) addComment.mutate(); }}>
                  <input aria-label="Комментарий" placeholder="Написать комментарий…" className="h-9 min-w-0 flex-1 rounded-md border border-line px-3 text-sm outline-none focus:border-accent" value={comment} onChange={(e) => setComment(e.target.value)} />
                  <Button type="submit" loading={addComment.isPending}>Отправить</Button>
                </form>
              )}
            </div>
          </div>

          <aside className="space-y-3 text-sm">
            <Select label="Статус" value={t.status_id} disabled={!editable} onChange={(e) => move.mutate(Number(e.target.value))} className="w-full">
              {statuses.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
            <Select label="Исполнитель" value={t.assignee_id ?? ""} disabled={!editable} onChange={(e) => update.mutate({ assignee_id: e.target.value ? Number(e.target.value) : null })} className="w-full">
              <option value="">Не назначен</option>
              {members.data?.filter((m) => m.role !== "guest").map((m) => <option key={m.user_id} value={m.user_id}>{m.name}</option>)}
            </Select>
            <Select label="Приоритет" value={t.priority} disabled={!editable} onChange={(e) => update.mutate({ priority: e.target.value as Priority })} className="w-full">
              {(Object.keys(priorityLabel) as Priority[]).map((p) => <option key={p} value={p}>{priorityLabel[p]}</option>)}
            </Select>
            <label className="block">
              <span className="mb-1 block font-medium">Срок</span>
              <input type="date" className="h-9 w-full rounded-md border border-line bg-surface px-2" disabled={!editable} value={t.due_date ?? ""} onChange={(e) => update.mutate({ due_date: e.target.value || null })} />
            </label>
            {editable && <Button variant="danger" className="w-full" onClick={() => window.confirm("Удалить задачу?") && remove.mutate()}>Удалить задачу</Button>}
            <div>
              <h3 className="mb-1 font-medium">История</h3>
              {history.isPending && <Skeleton className="h-8 w-full" />}
              <ul className="space-y-1.5 text-xs text-muted">
                {history.data?.slice(0, 12).map((h) => (
                  <li key={h.id}><b className="font-medium text-ink">{h.actor_name ?? "Система"}</b> {describe(h)} · {dateTime(h.created_at)}</li>
                ))}
              </ul>
            </div>
          </aside>
        </div>
      )}
    </Modal>
  );
}
