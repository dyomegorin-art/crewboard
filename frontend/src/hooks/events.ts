import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { api } from "../api";
import { wsKey } from "./workspace";

/** SSE: сервер публикует изменения воркспейса, мы просто инвалидируем затронутые запросы. */
export function useWorkspaceEvents(workspaceId: number, myUserId: number): void {
  const qc = useQueryClient();
  useEffect(() => {
    let source: EventSource | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = async () => {
      try {
        // EventSource не умеет заголовков: меняем access-токен на короткоживущий билет.
        const { ticket } = await api.eventTicket(workspaceId);
        if (closed) return;
        source = new EventSource(`/api/w/${workspaceId}/events?ticket=${encodeURIComponent(ticket)}`);
        source.onmessage = (e: MessageEvent<string>) => {
          const ev = JSON.parse(e.data) as { type: string; actor_id?: number; project_id?: number };
          if (ev.type === "billing.changed") {
            void qc.invalidateQueries({ queryKey: wsKey(workspaceId) });
            void qc.invalidateQueries({ queryKey: ["billing", workspaceId] });
            void qc.invalidateQueries({ queryKey: ["invoices", workspaceId] });
            void qc.invalidateQueries({ queryKey: ["invitations", workspaceId] });
            void qc.invalidateQueries({ queryKey: ["members", workspaceId] });
            return;
          }
          if (ev.actor_id === myUserId && ev.type !== "project.changed") return; // свои правки уже в кэше
          void qc.invalidateQueries({ queryKey: ["board", workspaceId] });
          void qc.invalidateQueries({ queryKey: ["projects", workspaceId] });
          void qc.invalidateQueries({ queryKey: ["my-tasks", workspaceId] });
          void qc.invalidateQueries({ queryKey: ["task", workspaceId] });
          void qc.invalidateQueries({ queryKey: ["activity", workspaceId] });
        };
        source.onerror = () => {
          source?.close();
          if (!closed) retry = setTimeout(() => void connect(), 3000);
        };
      } catch {
        if (!closed) retry = setTimeout(() => void connect(), 5000);
      }
    };
    void connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      source?.close();
    };
  }, [workspaceId, myUserId, qc]);
}
