import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import { refreshSession, setAccessToken } from "../api/client";
import type { AuthOut, User } from "../api/types";

interface AuthState {
  user: User | null;
  /** true, пока при загрузке страницы пытаемся восстановить сессию по refresh-cookie */
  booting: boolean;
  signIn: (out: AuthOut) => void;
  signOut: () => Promise<void>;
  reload: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const qc = useQueryClient();

  useEffect(() => {
    let alive = true;
    void (async () => {
      if (await refreshSession()) {
        const me = await api.me().catch(() => null);
        if (alive) setUser(me);
      }
      if (alive) setBooting(false);
    })();
    return () => {
      alive = false;
    };
  }, []);

  const signIn = useCallback((out: AuthOut) => {
    setAccessToken(out.access_token);
    setUser(out.user);
  }, []);

  const signOut = useCallback(async () => {
    await api.logout();
    setUser(null);
    qc.clear();
  }, [qc]);

  const reload = useCallback(async () => {
    setUser(await api.me());
  }, []);

  const value = useMemo(() => ({ user, booting, signIn, signOut, reload }), [user, booting, signIn, signOut, reload]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
