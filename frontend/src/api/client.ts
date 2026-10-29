// Тонкая обёртка над fetch: единый формат ошибок, access-токен в памяти, авто-refresh по cookie.

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
  ) {
    super(message);
  }
}

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;
const listeners = new Set<(token: string | null) => void>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  listeners.forEach((fn) => fn(token));
}

export function onTokenChange(fn: (token: string | null) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

async function toError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as { error?: { code: string; message: string; details?: unknown } };
    if (body.error) return new ApiError(res.status, body.error.code, body.error.message, body.error.details);
  } catch {
    /* тело не JSON */
  }
  return new ApiError(res.status, `http_${res.status}`, res.statusText || "Request failed");
}

/** Обновляет access-токен по refresh-cookie. Параллельные вызовы делят один запрос. */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      const res = await fetch("/api/auth/refresh", {
        method: "POST",
        headers: { "X-Requested-With": "crewboard" },
        credentials: "same-origin",
      });
      if (!res.ok) {
        setAccessToken(null);
        return false;
      }
      const body = (await res.json()) as { access_token: string };
      setAccessToken(body.access_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export async function request<T>(
  path: string,
  init: Omit<RequestInit, "body"> & { body?: unknown; auth?: boolean } = {},
): Promise<T> {
  const { body, auth = true, ...rest } = init;
  const send = () =>
    fetch(path, {
      ...rest,
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        "X-Requested-With": "crewboard",
        ...(auth && accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...rest.headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    });
  let res = await send();
  if (res.status === 401 && auth && (await refreshSession())) res = await send();
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const get = <T>(path: string, params?: Record<string, string | number | boolean | null | undefined>) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  const q = qs.toString();
  return request<T>(q ? `${path}?${q}` : path);
};
export const post = <T>(path: string, body?: unknown, auth = true) =>
  request<T>(path, { method: "POST", body: body ?? {}, auth });
export const patch = <T>(path: string, body: unknown) => request<T>(path, { method: "PATCH", body });
export const put = <T>(path: string, body: unknown) => request<T>(path, { method: "PUT", body });
export const del = <T = void>(path: string) => request<T>(path, { method: "DELETE" });
