import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Button, ErrorBox, Field } from "../components/ui";
import { useAuth } from "../hooks/auth";
import { errorMessage } from "../lib/format";

function Shell({ title, children, footer }: { title: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center bg-bg px-4 py-10">
      <div className="w-full max-w-sm">
        <Link to="/" className="mb-6 block text-center text-lg font-semibold tracking-tight">
          Crewboard
        </Link>
        <div className="rounded-xl border border-line bg-surface p-6 shadow-sm">
          <h1 className="mb-4 text-xl font-semibold">{title}</h1>
          {children}
        </div>
        {footer && <div className="mt-4 text-center text-sm text-muted">{footer}</div>}
      </div>
    </div>
  );
}

const oauthErrors: Record<string, string> = {
  email_unverified: "Этот email уже зарегистрирован, но не подтверждён. Подтвердите его и повторите вход.",
  invalid_state: "Не удалось проверить вход через GitHub. Попробуйте ещё раз.",
  github_failed: "GitHub не подтвердил вход. Попробуйте ещё раз.",
};

async function goHome(nav: ReturnType<typeof useNavigate>, next: string | null) {
  nav(next && next.startsWith("/") ? next : "/app", { replace: true });
}

export function Login() {
  const { signIn } = useAuth();
  const nav = useNavigate();
  const [params] = useSearchParams();
  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const m = useMutation({
    mutationFn: () => api.login({ email, password }),
    onSuccess: (out) => {
      signIn(out);
      void goHome(nav, params.get("next"));
    },
  });
  const oauthError = params.get("error");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    m.mutate();
  };
  return (
    <Shell
      title="Вход"
      footer={
        <>
          Нет аккаунта? <Link className="text-accent" to="/register">Зарегистрируйтесь</Link>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-3">
        {oauthError && <ErrorBox message={oauthErrors[oauthError] ?? "Не удалось войти"} />}
        {m.isError && <ErrorBox message={errorMessage(m.error)} />}
        <Field label="Email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        <Field label="Пароль" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        <Button type="submit" className="w-full" loading={m.isPending}>
          Войти
        </Button>
        <div className="text-right text-sm">
          <Link className="text-muted hover:text-ink" to="/forgot-password">
            Забыли пароль?
          </Link>
        </div>
      </form>
      {providers.data?.github && (
        <a href="/api/auth/github/login" className="mt-3 flex h-9 items-center justify-center rounded-md border border-line text-sm font-medium hover:bg-bg">
          Войти через GitHub
        </a>
      )}
    </Shell>
  );
}

export function Register() {
  const { signIn } = useAuth();
  const nav = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const m = useMutation({
    mutationFn: () => api.register({ name, email, password }),
    onSuccess: (out) => {
      signIn(out);
      nav("/check-email", { replace: true });
    },
  });
  return (
    <Shell
      title="Регистрация"
      footer={
        <>
          Уже есть аккаунт? <Link className="text-accent" to="/login">Войти</Link>
        </>
      }
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          m.mutate();
        }}
        className="space-y-3"
      >
        {m.isError && <ErrorBox message={errorMessage(m.error)} />}
        <Field label="Имя" required value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
        <Field label="Email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        <Field label="Пароль" type="password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} hint="Не короче 8 символов" autoComplete="new-password" />
        <Button type="submit" className="w-full" loading={m.isPending}>
          Создать аккаунт
        </Button>
      </form>
    </Shell>
  );
}

export function CheckEmail() {
  const { user } = useAuth();
  const resend = useMutation({ mutationFn: api.resendVerification });
  return (
    <Shell title="Проверьте почту">
      <p className="text-sm text-muted">
        Мы отправили письмо со ссылкой подтверждения{user ? ` на ${user.email}` : ""}. Пока email не подтверждён, нельзя
        приглашать участников и оплачивать подписку.
      </p>
      <p className="mt-3 text-sm text-muted">
        В dev-окружении письма попадают в Mailpit: <a className="text-accent" href="http://localhost:8025" target="_blank" rel="noreferrer">localhost:8025</a>.
      </p>
      <div className="mt-4 flex gap-2">
        <Link to="/app" className="flex-1">
          <Button className="w-full">Продолжить</Button>
        </Link>
        <Button variant="secondary" onClick={() => resend.mutate()} loading={resend.isPending} disabled={resend.isSuccess}>
          {resend.isSuccess ? "Отправлено" : "Отправить ещё раз"}
        </Button>
      </div>
    </Shell>
  );
}

export function VerifyEmail() {
  const [params] = useSearchParams();
  const { reload, user } = useAuth();
  const token = params.get("token") ?? "";
  const started = useRef(false);
  const m = useMutation({
    mutationFn: () => api.verifyEmail(token),
    onSuccess: () => {
      if (user) void reload();
    },
  });
  useEffect(() => {
    if (started.current) return; // StrictMode вызывает эффект дважды, а токен одноразовый
    started.current = true;
    m.mutate();
  }, [m]);
  return (
    <Shell title="Подтверждение email">
      {m.isPending && <p className="text-sm text-muted">Проверяем ссылку…</p>}
      {m.isSuccess && (
        <>
          <p className="text-sm" data-testid="email-verified">Email подтверждён. Спасибо!</p>
          <Link to="/app" className="mt-4 block">
            <Button className="w-full">Перейти в приложение</Button>
          </Link>
        </>
      )}
      {m.isError && <ErrorBox message={errorMessage(m.error)} />}
    </Shell>
  );
}

export function ForgotPassword() {
  const [email, setEmail] = useState("");
  const m = useMutation({ mutationFn: () => api.requestReset(email) });
  return (
    <Shell title="Сброс пароля" footer={<Link className="text-accent" to="/login">Назад ко входу</Link>}>
      {m.isSuccess ? (
        <p className="text-sm text-muted">
          Если такой email зарегистрирован, мы отправили ссылку для сброса пароля. Она действует 30 минут.
        </p>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            m.mutate();
          }}
          className="space-y-3"
        >
          {m.isError && <ErrorBox message={errorMessage(m.error)} />}
          <Field label="Email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
          <Button type="submit" className="w-full" loading={m.isPending}>
            Отправить ссылку
          </Button>
        </form>
      )}
    </Shell>
  );
}

export function ResetPassword() {
  const [params] = useSearchParams();
  const [password, setPassword] = useState("");
  const m = useMutation({ mutationFn: () => api.confirmReset(params.get("token") ?? "", password) });
  return (
    <Shell title="Новый пароль">
      {m.isSuccess ? (
        <>
          <p className="text-sm text-muted">Пароль изменён, все сессии завершены. Войдите с новым паролем.</p>
          <Link to="/login" className="mt-4 block">
            <Button className="w-full">Войти</Button>
          </Link>
        </>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            m.mutate();
          }}
          className="space-y-3"
        >
          {m.isError && <ErrorBox message={errorMessage(m.error)} />}
          <Field label="Новый пароль" type="password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          <Button type="submit" className="w-full" loading={m.isPending}>
            Сохранить пароль
          </Button>
        </form>
      )}
    </Shell>
  );
}

/** Возврат из GitHub OAuth: refresh-cookie уже выставлена, сессию подхватит AuthProvider. */
export function AuthCallback() {
  const { user, booting } = useAuth();
  const nav = useNavigate();
  useEffect(() => {
    if (!booting) nav(user ? "/app" : "/login?error=github_failed", { replace: true });
  }, [booting, user, nav]);
  return <Shell title="Входим…">{null}</Shell>;
}
