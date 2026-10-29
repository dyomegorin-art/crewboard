import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { Button, ErrorBox, Skeleton } from "../components/ui";
import { useAuth } from "../hooks/auth";
import { errorMessage, roleLabel } from "../lib/format";

export function InviteAccept() {
  const { token = "" } = useParams();
  const { user, booting } = useAuth();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["invite", token], queryFn: () => api.publicInvite(token), retry: false });
  const accept = useMutation({
    mutationFn: () => api.acceptInvite(token),
    onSuccess: (r) => nav(`/w/${r.slug}`, { replace: true }),
  });
  const next = encodeURIComponent(`/invite/${token}`);

  return (
    <div className="flex min-h-full items-center justify-center bg-bg px-4">
      <div className="w-full max-w-sm rounded-xl border border-line bg-surface p-6 shadow-sm">
        <h1 className="mb-3 text-xl font-semibold">Приглашение</h1>
        {(q.isPending || booting) && <Skeleton className="h-20 w-full" />}
        {q.isError && <ErrorBox message="Приглашение не найдено" />}
        {q.data && !booting && (
          <>
            <p className="text-sm">
              Вас приглашают в <b>{q.data.workspace_name}</b> как «{roleLabel[q.data.role]}» ({q.data.email}).
            </p>
            {q.data.status !== "pending" ? (
              <p className="mt-3 text-sm text-muted">Статус приглашения: {q.data.status}.</p>
            ) : user ? (
              <>
                {accept.isError && <div className="mt-3"><ErrorBox message={errorMessage(accept.error)} /></div>}
                <Button className="mt-4 w-full" onClick={() => accept.mutate()} loading={accept.isPending}>
                  Принять приглашение
                </Button>
                <p className="mt-2 text-xs text-muted">Вы вошли как {user.email}.</p>
              </>
            ) : (
              <div className="mt-4 flex gap-2">
                <Link className="flex-1" to={`/login?next=${next}`}>
                  <Button className="w-full">Войти</Button>
                </Link>
                <Link className="flex-1" to="/register">
                  <Button variant="secondary" className="w-full">Регистрация</Button>
                </Link>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
