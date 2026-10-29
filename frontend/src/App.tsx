import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api";
import { ApiError } from "./api/client";
import { AppShell } from "./components/AppShell";
import { Skeleton } from "./components/ui";
import { AuthProvider, useAuth } from "./hooks/auth";
import { errorMessage } from "./lib/format";
import { promptUpgrade, toast } from "./lib/ui-events";
import { BillingPage, BillingReturn } from "./pages/Billing";
import { AuthCallback, CheckEmail, ForgotPassword, Login, Register, ResetPassword, VerifyEmail } from "./pages/Auth";
import { BoardPage, NoProject } from "./pages/Board";
import { InviteAccept } from "./pages/Invite";
import { Landing } from "./pages/Landing";
import { ActivityPage, MyTasks } from "./pages/Lists";
import { MembersPage } from "./pages/Members";
import { MockCheckout } from "./pages/MockCheckout";
import { SettingsPage } from "./pages/Settings";

/** plan_limit_* от API → модалка «Перейдите на Pro» (кроме мест, где её показывает сама страница). */
function handleGlobalError(err: unknown): void {
  if (err instanceof ApiError && err.code.startsWith("plan_limit_")) promptUpgrade({ code: err.code, message: errorMessage(err) });
}

function makeClient(): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache(),
    mutationCache: new MutationCache({ onError: handleGlobalError }),
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
        refetchOnWindowFocus: false,
      },
    },
  });
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, booting } = useAuth();
  const loc = useLocation();
  if (booting) return <div className="p-10"><Skeleton className="h-8 w-48" /></div>;
  if (!user) return <Navigate to={`/login?next=${encodeURIComponent(loc.pathname + loc.search)}`} replace />;
  return <>{children}</>;
}

/** /app → первый воркспейс пользователя. */
function AppHome() {
  const nav = useNavigate();
  useEffect(() => {
    void api.workspaces().then((list) => nav(list[0] ? `/w/${list[0].slug}` : "/", { replace: true }));
  }, [nav]);
  return <div className="p-10"><Skeleton className="h-8 w-48" /></div>;
}

/** /w/:slug → первый проект или подсказка. */
function WorkspaceHome() {
  const nav = useNavigate();
  const loc = useLocation();
  useEffect(() => {
    const slug = loc.pathname.split("/")[2];
    void api.workspaces().then(async (list) => {
      const ws = list.find((w) => w.slug === slug);
      if (!ws) return;
      const projects = await api.projects(ws.id);
      if (projects[0]) nav(`/w/${slug}/p/${projects[0].id}`, { replace: true });
    }).catch(() => toast("Не удалось загрузить проекты", "error"));
  }, [loc.pathname, nav]);
  return <NoProject />;
}

export default function App() {
  const [client] = useState(makeClient);
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/check-email" element={<CheckEmail />} />
            <Route path="/verify-email" element={<VerifyEmail />} />
            <Route path="/forgot-password" element={<ForgotPassword />} />
            <Route path="/reset-password" element={<ResetPassword />} />
            <Route path="/auth/callback" element={<AuthCallback />} />
            <Route path="/invite/:token" element={<InviteAccept />} />
            <Route path="/mock-checkout/:session" element={<MockCheckout />} />
            <Route path="/app" element={<RequireAuth><AppHome /></RequireAuth>} />
            <Route path="/w/:slug" element={<RequireAuth><AppShell /></RequireAuth>}>
              <Route index element={<WorkspaceHome />} />
              <Route path="p/:projectId" element={<BoardPage />} />
              <Route path="my-tasks" element={<MyTasks />} />
              <Route path="activity" element={<ActivityPage />} />
              <Route path="members" element={<MembersPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="billing" element={<BillingPage />} />
              <Route path="billing/return" element={<BillingReturn />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
