import {
  DndContext,
  DragOverlay,
  PointerSensor,
  TouchSensor,
  closestCorners,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { SortableContext, useSortable, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Board as BoardData, Member, Priority, Status, Task } from "../api/types";
import { TaskModal } from "../components/TaskModal";
import { Avatar, Button, Empty, ErrorBox, PriorityBadge, Select, Skeleton } from "../components/ui";
import { useWorkspace } from "../hooks/workspace";
import { date, errorMessage, priorityLabel } from "../lib/format";
import { toast } from "../lib/ui-events";

const EMPTY: Task[] = [];

export function boardKey(wsId: number, pid: number, filters: object) {
  return ["board", wsId, pid, filters] as const;
}

/** Куда упала карточка: колонка и карточка, ПОСЛЕ которой она встанет (null — в начало). */
export function computeDrop(
  tasks: Task[],
  activeId: number,
  overId: string,
): { statusId: number; afterId: number | null } | null {
  const active = tasks.find((t) => t.id === activeId);
  if (!active) return null;
  const column = (statusId: number) =>
    tasks.filter((t) => t.status_id === statusId && t.id !== activeId).sort((a, b) => a.position - b.position);
  if (overId.startsWith("col-")) {
    const statusId = Number(overId.slice(4));
    const list = column(statusId);
    return { statusId, afterId: list.length ? list[list.length - 1].id : null };
  }
  const over = tasks.find((t) => `task-${t.id}` === overId);
  if (!over || over.id === active.id) return null;
  const list = column(over.status_id);
  const idx = list.findIndex((t) => t.id === over.id);
  const full = tasks.filter((t) => t.status_id === over.status_id).sort((a, b) => a.position - b.position);
  const movingDown = active.status_id === over.status_id && full.findIndex((t) => t.id === active.id) < full.findIndex((t) => t.id === over.id);
  const insertAt = movingDown ? idx + 1 : idx;
  return { statusId: over.status_id, afterId: insertAt > 0 ? list[insertAt - 1].id : null };
}

export function BoardPage() {
  const { projectId } = useParams();
  const pid = Number(projectId);
  const { ws, can, readOnly } = useWorkspace();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const filters = useMemo(
    () => ({
      assignee_id: params.get("assignee") ? Number(params.get("assignee")) : undefined,
      priority: params.get("priority") ?? undefined,
      mine: params.get("mine") === "1" || undefined,
    }),
    [params],
  );
  const key = boardKey(ws.id, pid, filters);
  const board = useQuery({ queryKey: key, queryFn: () => api.board(ws.id, pid, filters) });
  const members = useQuery({ queryKey: ["members", ws.id], queryFn: () => api.members(ws.id) });
  const [openTask, setOpenTask] = useState<number | null>(null);
  const [dragging, setDragging] = useState<Task | null>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 180, tolerance: 8 } }),
  );

  const move = useMutation({
    mutationFn: (v: { taskId: number; statusId: number; afterId: number | null }) =>
      api.moveTask(ws.id, v.taskId, v.statusId, v.afterId),
    // Оптимистично: карточка сразу переезжает, при ошибке откатываем снимок.
    onMutate: async (v) => {
      await qc.cancelQueries({ queryKey: ["board", ws.id, pid] });
      const snapshots = qc.getQueriesData<BoardData>({ queryKey: ["board", ws.id, pid] });
      qc.setQueriesData<BoardData>({ queryKey: ["board", ws.id, pid] }, (old) => {
        if (!old) return old;
        const others = old.tasks.filter((t) => t.id !== v.taskId && t.status_id === v.statusId).sort((a, b) => a.position - b.position);
        const prev = v.afterId === null ? null : others.find((t) => t.id === v.afterId) ?? null;
        const next = prev ? others[others.indexOf(prev) + 1] : others[0];
        const position = prev && next ? (prev.position + next.position) / 2 : prev ? prev.position + 1024 : next ? next.position / 2 : 1024;
        return { ...old, tasks: old.tasks.map((t) => (t.id === v.taskId ? { ...t, status_id: v.statusId, position } : t)) };
      });
      return { snapshots };
    },
    onError: (err, _v, ctx) => {
      ctx?.snapshots.forEach(([k, data]) => qc.setQueryData(k, data));
      toast(errorMessage(err), "error");
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ["board", ws.id, pid] }),
  });

  if (board.isPending) return <BoardSkeleton />;
  if (board.isError) return <div className="p-6"><ErrorBox message={errorMessage(board.error)} onRetry={() => void board.refetch()} /></div>;

  const { project, statuses, tasks } = board.data;
  const editable = can("tasks.write") && !readOnly && !project.archived;
  const byStatus = (id: number) => tasks.filter((t) => t.status_id === id).sort((a, b) => a.position - b.position);

  const onDragStart = (e: DragStartEvent) => setDragging(tasks.find((t) => `task-${t.id}` === e.active.id) ?? null);
  const onDragEnd = (e: DragEndEvent) => {
    setDragging(null);
    if (!e.over || !editable) return;
    const activeId = Number(String(e.active.id).slice(5));
    const drop = computeDrop(tasks, activeId, String(e.over.id));
    if (drop) move.mutate({ taskId: activeId, ...drop });
  };

  const setFilter = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-surface px-4 py-2.5">
        <h1 className="mr-2 text-lg font-semibold">{project.name}</h1>
        {project.archived && <span className="rounded bg-black/5 px-2 py-0.5 text-xs text-muted">В архиве · только чтение</span>}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-sm">
            <input type="checkbox" checked={params.get("mine") === "1"} onChange={(e) => setFilter("mine", e.target.checked ? "1" : null)} />
            Только мои
          </label>
          <Select aria-label="Исполнитель" value={params.get("assignee") ?? ""} onChange={(e) => setFilter("assignee", e.target.value || null)}>
            <option value="">Все исполнители</option>
            {members.data?.filter((m) => m.role !== "guest").map((m) => <option key={m.user_id} value={m.user_id}>{m.name}</option>)}
          </Select>
          <Select aria-label="Приоритет" value={params.get("priority") ?? ""} onChange={(e) => setFilter("priority", e.target.value || null)}>
            <option value="">Любой приоритет</option>
            {(Object.keys(priorityLabel) as Priority[]).map((p) => <option key={p} value={p}>{priorityLabel[p]}</option>)}
          </Select>
          {can("projects.manage") && !readOnly && <ProjectMenu project={project} />}
        </div>
      </div>

      <DndContext sensors={sensors} collisionDetection={closestCorners} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={() => setDragging(null)}>
        <div className="flex min-h-0 flex-1 gap-3 overflow-x-auto p-4" data-testid="board">
          {statuses.map((s) => (
            <Column key={s.id} status={s} tasks={byStatus(s.id) ?? EMPTY} members={members.data ?? []} editable={editable} onOpen={setOpenTask} projectId={pid} canManage={can("statuses.manage") && !readOnly} />
          ))}
          {can("statuses.manage") && !project.archived && !readOnly && <AddColumn projectId={pid} />}
        </div>
        <DragOverlay>{dragging ? <Card task={dragging} members={members.data ?? []} overlay /> : null}</DragOverlay>
      </DndContext>

      {openTask !== null && <TaskModal taskId={openTask} projectKey={project.key} statuses={statuses} onClose={() => setOpenTask(null)} />}
    </div>
  );
}

function BoardSkeleton() {
  return (
    <div className="flex gap-3 p-4">
      {[0, 1, 2].map((i) => (
        <div key={i} className="w-72 shrink-0 space-y-2">
          <Skeleton className="h-5 w-24" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      ))}
    </div>
  );
}

function Column({ status, tasks, members, editable, onOpen, projectId, canManage }: { status: Status; tasks: Task[]; members: Member[]; editable: boolean; onOpen: (id: number) => void; projectId: number; canManage: boolean }) {
  const { ws } = useWorkspace();
  const { setNodeRef, isOver } = useDroppable({ id: `col-${status.id}` });
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const create = useMutation({
    mutationFn: () => api.createTask(ws.id, projectId, { title, status_id: status.id }),
    onSuccess: () => {
      setTitle("");
      setAdding(false);
      void qc.invalidateQueries({ queryKey: ["board", ws.id, projectId] });
      void qc.invalidateQueries({ queryKey: ["projects", ws.id] });
    },
    onError: (e) => toast(errorMessage(e), "error"),
  });
  const rename = useMutation({
    mutationFn: (name: string) => api.renameStatus(ws.id, status.id, name),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["board", ws.id, projectId] }),
    onError: (e) => toast(errorMessage(e), "error"),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteStatus(ws.id, status.id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["board", ws.id, projectId] }),
    onError: (e) => toast(errorMessage(e), "error"),
  });
  return (
    <section ref={setNodeRef} aria-label={status.name} className={`flex max-h-full w-72 shrink-0 flex-col rounded-xl bg-black/[0.035] p-2 ${isOver ? "ring-2 ring-accent/40" : ""}`}>
      <header className="mb-2 flex items-center gap-2 px-1.5 pt-1">
        <h3 className="text-sm font-semibold">{status.name}</h3>
        <span className="text-xs text-muted">{tasks.length}</span>
        {canManage && editable && (
          <span className="ml-auto flex gap-1 text-xs text-muted">
            <button className="rounded px-1 hover:bg-black/5" title="Переименовать" onClick={() => { const n = window.prompt("Название колонки", status.name); if (n?.trim()) rename.mutate(n.trim()); }}>✎</button>
            <button className="rounded px-1 hover:bg-black/5" title="Удалить пустую колонку" onClick={() => remove.mutate()}>🗑</button>
          </span>
        )}
      </header>
      <SortableContext items={tasks.map((t) => `task-${t.id}`)} strategy={verticalListSortingStrategy}>
        <div className="flex min-h-[3rem] flex-1 flex-col gap-2 overflow-y-auto">
          {tasks.map((t) => <SortableCard key={t.id} task={t} members={members} editable={editable} onOpen={onOpen} />)}
        </div>
      </SortableContext>
      {editable && (adding ? (
        <form className="mt-2 space-y-2" onSubmit={(e) => { e.preventDefault(); if (title.trim()) create.mutate(); }}>
          <input aria-label="Название задачи" placeholder="Название задачи" autoFocus className="h-9 w-full rounded-md border border-line bg-surface px-3 text-sm outline-none focus:border-accent" value={title} onChange={(e) => setTitle(e.target.value)} />
          <div className="flex gap-2">
            <Button type="submit" loading={create.isPending}>Добавить</Button>
            <Button type="button" variant="ghost" onClick={() => setAdding(false)}>Отмена</Button>
          </div>
        </form>
      ) : (
        <button className="mt-2 rounded-md px-2 py-1.5 text-left text-sm text-muted hover:bg-black/5" onClick={() => setAdding(true)}>+ Добавить задачу</button>
      ))}
    </section>
  );
}

function SortableCard({ task, members, editable, onOpen }: { task: Task; members: Member[]; editable: boolean; onOpen: (id: number) => void }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: `task-${task.id}`, disabled: !editable });
  return (
    <div ref={setNodeRef} style={{ transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.4 : 1 }} {...attributes} {...listeners}>
      <Card task={task} members={members} onOpen={() => onOpen(task.id)} />
    </div>
  );
}

function Card({ task, members, onOpen, overlay }: { task: Task; members: Member[]; onOpen?: () => void; overlay?: boolean }) {
  const assignee = members.find((m) => m.user_id === task.assignee_id);
  const overdue = task.due_date && new Date(task.due_date) < new Date(new Date().toDateString());
  return (
    <article
      data-testid="task-card"
      onClick={onOpen}
      className={`cursor-pointer rounded-lg border border-line bg-surface p-2.5 text-sm shadow-sm hover:border-accent/50 ${overlay ? "rotate-1 shadow-lg" : ""}`}
    >
      <div className="mb-1 text-xs text-muted">{task.key}</div>
      <div className="font-medium leading-snug">{task.title}</div>
      <div className="mt-2 flex items-center gap-2">
        <PriorityBadge priority={task.priority} />
        {task.due_date && <span className={`text-xs ${overdue ? "text-danger" : "text-muted"}`}>{date(task.due_date)}</span>}
        {assignee && <span className="ml-auto"><Avatar name={assignee.name} /></span>}
      </div>
    </article>
  );
}

function AddColumn({ projectId }: { projectId: number }) {
  const { ws } = useWorkspace();
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: (name: string) => api.addStatus(ws.id, projectId, name),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["board", ws.id, projectId] }),
    onError: (e) => toast(errorMessage(e), "error"),
  });
  return (
    <button className="h-10 w-48 shrink-0 rounded-xl border border-dashed border-line px-3 text-sm text-muted hover:bg-black/[0.03]" onClick={() => { const n = window.prompt("Название колонки"); if (n?.trim()) m.mutate(n.trim()); }}>
      + Колонка
    </button>
  );
}

function ProjectMenu({ project }: { project: BoardData["project"] }) {
  const { ws } = useWorkspace();
  const qc = useQueryClient();
  const done = () => {
    void qc.invalidateQueries({ queryKey: ["board", ws.id] });
    void qc.invalidateQueries({ queryKey: ["projects", ws.id] });
  };
  const rename = useMutation({ mutationFn: (n: string) => api.renameProject(ws.id, project.id, n), onSuccess: done, onError: (e) => toast(errorMessage(e), "error") });
  const archive = useMutation({
    mutationFn: () => (project.archived ? api.unarchiveProject(ws.id, project.id) : api.archiveProject(ws.id, project.id)),
    onSuccess: done,
    onError: (e) => toast(errorMessage(e), "error"),
  });
  return (
    <>
      {!project.archived && <Button variant="secondary" onClick={() => { const n = window.prompt("Новое название", project.name); if (n?.trim()) rename.mutate(n.trim()); }}>Переименовать</Button>}
      <Button variant="secondary" onClick={() => archive.mutate()} loading={archive.isPending}>{project.archived ? "Вернуть из архива" : "В архив"}</Button>
    </>
  );
}

export function NoProject() {
  return <div className="p-6"><Empty title="Выберите проект слева" hint="Или создайте новый — кнопка «Новый проект» в боковой панели." /></div>;
}
