import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";
import { api } from "../api";
import type { Role, WorkspaceDetail } from "../api/types";

interface WorkspaceState {
  ws: WorkspaceDetail;
  can: (permission: string) => boolean;
  role: Role;
  readOnly: boolean;
}

const Ctx = createContext<WorkspaceState | null>(null);

export const wsKey = (id: number) => ["workspace", id] as const;

export function WorkspaceProvider({ id, children }: { id: number; children: (ws: WorkspaceState | null, q: { isError: boolean; isPending: boolean }) => ReactNode }) {
  const q = useQuery({ queryKey: wsKey(id), queryFn: () => api.workspace(id) });
  const ws = q.data;
  const value: WorkspaceState | null = ws
    ? { ws, can: (p) => ws.permissions.includes(p), role: ws.role, readOnly: ws.status === "read_only" }
    : null;
  return <Ctx.Provider value={value}>{children(value, q)}</Ctx.Provider>;
}

export function useWorkspace(): WorkspaceState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkspace outside WorkspaceProvider");
  return v;
}
