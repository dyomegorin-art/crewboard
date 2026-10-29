export type Role = "owner" | "admin" | "member" | "guest";
export type Priority = "low" | "medium" | "high" | "urgent";
export type Period = "month" | "year";
export type SubStatus = "trialing" | "active" | "past_due" | "read_only" | "canceled";

export interface User {
  id: number;
  email: string;
  name: string;
  email_verified: boolean;
  is_demo: boolean;
}
export interface AuthOut {
  access_token: string;
  user: User;
}
export interface WorkspaceSummary {
  id: number;
  name: string;
  slug: string;
  role: Role;
  plan: "free" | "pro";
  status: SubStatus;
}
export interface WorkspaceDetail extends WorkspaceSummary {
  permissions: string[];
  cancel_at_period_end: boolean;
  trial_end: string | null;
  current_period_end: string | null;
  past_due_since: string | null;
  seats: number;
  paid_seats_used: number;
}
export interface Member {
  user_id: number;
  name: string;
  email: string;
  role: Role;
  is_owner: boolean;
  created_at: string;
}
export type InviteStatus = "pending" | "awaiting_payment" | "accepted" | "revoked" | "expired";
export interface Invitation {
  id: number;
  email: string;
  role: Role;
  status: InviteStatus;
  expires_at: string;
  invoice_id: number | null;
  amount: number | null;
  created_at: string;
}
export interface InvitePreview {
  requires_payment: boolean;
  amount: number;
  currency: string;
  seats_after: number;
}
export interface InvitePublic {
  workspace_name: string;
  email: string;
  role: Role;
  status: InviteStatus;
}
export interface Project {
  id: number;
  name: string;
  key: string;
  archived: boolean;
  task_count: number;
  created_at: string;
}
export interface Status {
  id: number;
  project_id: number;
  name: string;
  position: number;
  is_done: boolean;
}
export interface Task {
  id: number;
  project_id: number;
  status_id: number;
  number: number;
  key: string;
  title: string;
  description: string;
  assignee_id: number | null;
  priority: Priority;
  due_date: string | null;
  position: number;
  created_by: number | null;
  created_at: string;
  updated_at: string;
}
export interface Board {
  project: Project;
  statuses: Status[];
  tasks: Task[];
}
export interface Comment {
  id: number;
  task_id: number;
  author_id: number | null;
  author_name: string | null;
  body: string;
  created_at: string;
}
export interface Activity {
  id: number;
  actor_id: number | null;
  actor_name: string | null;
  entity: string;
  entity_id: number | null;
  action: string;
  detail: Record<string, unknown>;
  created_at: string;
}
export interface ActivityPage {
  items: Activity[];
  next_cursor: string | null;
}
export interface Billing {
  plan: "free" | "pro";
  status: SubStatus;
  billing_period: Period;
  pending_period: Period | null;
  seats_paid: number;
  seats_used: number;
  seats_reserved: number;
  pending_seats: number | null;
  current_period_start: string | null;
  current_period_end: string | null;
  trial_end: string | null;
  cancel_at_period_end: boolean;
  past_due_since: string | null;
  next_charge_at: string | null;
  next_charge_amount: number;
  card: { brand: string; last4: string } | null;
  outstanding_invoice_id: number | null;
  seat_price_month: number;
  seat_price_year: number;
  currency: string;
  trial_available: boolean;
  provider: string;
}
export interface InvoiceLine {
  description: string;
  quantity: number;
  unit_amount: number;
  amount: number;
}
export interface Invoice {
  id: number;
  kind: string;
  amount: number;
  currency: string;
  status: string;
  period_start: string | null;
  period_end: string | null;
  created_at: string;
  paid_at: string | null;
  lines: InvoiceLine[];
}
export interface InvoiceStatus {
  invoice_id: number;
  invoice_status: string;
  subscription_status: SubStatus;
  plan: "free" | "pro";
}
export interface MockCheckout {
  session_id: string;
  invoice_id: number;
  amount: number;
  currency: string;
  status: string;
  kind: string;
  workspace_name: string;
  slug: string;
  lines: InvoiceLine[];
}
