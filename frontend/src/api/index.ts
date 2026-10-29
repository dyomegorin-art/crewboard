import { del, get, patch, post, put, request, setAccessToken } from "./client";
import type {
  ActivityPage,
  AuthOut,
  Billing,
  Board,
  Comment,
  Invitation,
  InvitePreview,
  InvitePublic,
  Invoice,
  InvoiceStatus,
  Member,
  MockCheckout,
  Period,
  Priority,
  Project,
  Role,
  Status,
  Task,
  User,
  WorkspaceDetail,
  WorkspaceSummary,
  Activity,
} from "./types";

const w = (id: number) => `/api/w/${id}`;

export interface TaskPatch {
  title?: string;
  description?: string;
  assignee_id?: number | null;
  priority?: Priority;
  due_date?: string | null;
}

export const api = {
  // --- auth
  providers: () => get<{ github: boolean; demo: boolean }>("/api/auth/providers"),
  register: (b: { email: string; password: string; name: string }) =>
    post<AuthOut>("/api/auth/register", b, false),
  login: (b: { email: string; password: string }) => post<AuthOut>("/api/auth/login", b, false),
  demo: () => post<AuthOut>("/api/auth/demo", {}, false),
  logout: async () => {
    await post<void>("/api/auth/logout", {}, false).catch(() => undefined);
    setAccessToken(null);
  },
  logoutAll: () => post<void>("/api/auth/logout-all"),
  me: () => get<User>("/api/auth/me"),
  verifyEmail: (token: string) => post<User>("/api/auth/verify-email", { token }, false),
  resendVerification: () => post<{ status: string }>("/api/auth/resend-verification"),
  requestReset: (email: string) => post<{ status: string }>("/api/auth/password-reset/request", { email }, false),
  confirmReset: (token: string, password: string) =>
    post<void>("/api/auth/password-reset/confirm", { token, password }, false),

  // --- workspaces
  workspaces: () => get<WorkspaceSummary[]>("/api/workspaces"),
  createWorkspace: (name: string) => post<WorkspaceSummary>("/api/workspaces", { name }),
  workspace: (id: number) => get<WorkspaceDetail>(w(id)),
  renameWorkspace: (id: number, name: string) => patch<{ name: string }>(w(id), { name }),
  deleteWorkspace: (id: number) => del(w(id)),
  members: (id: number) => get<Member[]>(`${w(id)}/members`),
  changeRole: (id: number, userId: number, role: Role) => patch<Member>(`${w(id)}/members/${userId}`, { role }),
  removeMember: (id: number, userId: number) => del(`${w(id)}/members/${userId}`),
  transferOwnership: (id: number, userId: number) => post<void>(`${w(id)}/ownership-transfer`, { user_id: userId }),

  // --- invitations
  invitations: (id: number) => get<Invitation[]>(`${w(id)}/invitations`),
  previewInvite: (id: number, email: string, role: Role) =>
    post<InvitePreview>(`${w(id)}/invitations/preview`, { email, role }),
  invite: (id: number, email: string, role: Role, confirmAmount?: number) =>
    post<Invitation>(`${w(id)}/invitations`, { email, role, confirm_amount: confirmAmount ?? null }),
  revokeInvite: (id: number, inviteId: number) => post<void>(`${w(id)}/invitations/${inviteId}/revoke`),
  resendInvite: (id: number, inviteId: number) => post<Invitation>(`${w(id)}/invitations/${inviteId}/resend`),
  publicInvite: (token: string) => get<InvitePublic>(`/api/invitations/${token}`),
  acceptInvite: (token: string) => post<{ workspace_id: number; slug: string }>(`/api/invitations/${token}/accept`),

  // --- projects & tasks
  projects: (id: number, includeArchived = false) =>
    get<Project[]>(`${w(id)}/projects`, { include_archived: includeArchived }),
  createProject: (id: number, name: string) => post<Project>(`${w(id)}/projects`, { name }),
  renameProject: (id: number, pid: number, name: string) => patch<Project>(`${w(id)}/projects/${pid}`, { name }),
  archiveProject: (id: number, pid: number) => post<Project>(`${w(id)}/projects/${pid}/archive`),
  unarchiveProject: (id: number, pid: number) => post<Project>(`${w(id)}/projects/${pid}/unarchive`),
  board: (id: number, pid: number, f: { assignee_id?: number; priority?: string; mine?: boolean }) =>
    get<Board>(`${w(id)}/projects/${pid}/board`, f),
  addStatus: (id: number, pid: number, name: string) => post<Status>(`${w(id)}/projects/${pid}/statuses`, { name }),
  renameStatus: (id: number, sid: number, name: string) => patch<Status>(`${w(id)}/statuses/${sid}`, { name }),
  deleteStatus: (id: number, sid: number) => del(`${w(id)}/statuses/${sid}`),
  reorderStatuses: (id: number, pid: number, ids: number[]) =>
    put<Status[]>(`${w(id)}/projects/${pid}/statuses/order`, { ids }),
  createTask: (id: number, pid: number, b: { title: string; status_id?: number }) =>
    post<Task>(`${w(id)}/projects/${pid}/tasks`, b),
  task: (id: number, tid: number) => get<Task>(`${w(id)}/tasks/${tid}`),
  updateTask: (id: number, tid: number, b: TaskPatch) => patch<Task>(`${w(id)}/tasks/${tid}`, b),
  moveTask: (id: number, tid: number, statusId: number, afterId: number | null) =>
    post<Task>(`${w(id)}/tasks/${tid}/move`, { status_id: statusId, after_id: afterId }),
  deleteTask: (id: number, tid: number) => del(`${w(id)}/tasks/${tid}`),
  myTasks: (id: number) => get<Task[]>(`${w(id)}/my-tasks`),
  comments: (id: number, tid: number) => get<Comment[]>(`${w(id)}/tasks/${tid}/comments`),
  addComment: (id: number, tid: number, body: string) => post<Comment>(`${w(id)}/tasks/${tid}/comments`, { body }),
  history: (id: number, tid: number) => get<Activity[]>(`${w(id)}/tasks/${tid}/history`),
  activity: (id: number, cursor?: string) => get<ActivityPage>(`${w(id)}/activity`, { cursor, limit: 30 }),
  eventTicket: (id: number) => post<{ ticket: string }>(`${w(id)}/events/ticket`),
  exportCsvUrl: (id: number) => `${w(id)}/export/tasks.csv`,

  // --- billing
  billing: (id: number) => get<Billing>(`${w(id)}/billing`),
  invoices: (id: number) => get<Invoice[]>(`${w(id)}/billing/invoices`),
  invoiceStatus: (id: number, invoiceId: number) =>
    get<InvoiceStatus>(`${w(id)}/billing/invoices/${invoiceId}/status`),
  startTrial: (id: number, period: Period) => post<Billing>(`${w(id)}/billing/trial`, { period }),
  checkout: (id: number, period: Period) => post<{ checkout_url: string; invoice_id: number }>(`${w(id)}/billing/checkout`, { period }),
  cardCheckout: (id: number) => post<{ checkout_url: string; invoice_id: number }>(`${w(id)}/billing/card/checkout`),
  payOutstanding: (id: number) => post<{ checkout_url: string; invoice_id: number }>(`${w(id)}/billing/pay-outstanding`),
  cancelSubscription: (id: number) => post<Billing>(`${w(id)}/billing/cancel`),
  resumeSubscription: (id: number) => post<Billing>(`${w(id)}/billing/resume`),
  setSeats: (id: number, seats: number) => post<Billing>(`${w(id)}/billing/seats`, { seats }),
  setPeriod: (id: number, period: Period) => post<Billing>(`${w(id)}/billing/period`, { period }),

  // --- mock checkout
  mockCheckout: (session: string) => request<MockCheckout>(`/api/mock/checkout/${session}`, { auth: false }),
  mockPay: (session: string, card: string) =>
    post<{ status: string; invoice_id: string }>(`/api/mock/checkout/${session}/pay`, { card_number: card }, false),
};
