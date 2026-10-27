import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  ReactNode,
} from "react";
import { useApi } from "./ApiContext";

export type HfAuthState =
  | { status: "loading" }
  | { status: "authenticated"; username: string; orgs: string[] }
  | {
      status: "unverified";
      /** A token is stored locally but the Hub could not be reached (or refused it). */
      tokenPath: string | null;
      rejected: boolean;
      error: string | null;
    }
  | { status: "unauthenticated"; loginCommand: string; tokenPath: string | null };

interface HfAuthValue {
  auth: HfAuthState;
  refetch: () => Promise<void>;
  /** Validate + persist a pasted token; throws with the backend message. */
  login: (token: string) => Promise<void>;
  /** Forget the stored token. */
  logout: () => Promise<void>;
}

const HfAuthContext = createContext<HfAuthValue | undefined>(undefined);

export const HfAuthProvider: React.FC<{ children: ReactNode }> = ({
  children,
}) => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [auth, setAuth] = useState<HfAuthState>({ status: "loading" });

  const fetchStatus = useCallback(async () => {
    setAuth({ status: "loading" });
    try {
      const response = await fetchWithHeaders(`${baseUrl}/hf-auth-status`);
      const data = await response.json();
      if (data.authenticated) {
        setAuth({
          status: "authenticated",
          username: data.username,
          orgs: data.orgs ?? [],
        });
      } else if (data.token_present) {
        // Token on disk but not verified: keep it and show the reason instead
        // of pretending the user is logged out (e.g. proxy down, revoked token).
        setAuth({
          status: "unverified",
          tokenPath: data.token_path ?? null,
          rejected: Boolean(data.token_rejected),
          error: data.error ?? null,
        });
      } else {
        setAuth({
          status: "unauthenticated",
          loginCommand: data.login_command ?? "hf auth login",
          tokenPath: data.token_path ?? null,
        });
      }
    } catch (err) {
      console.warn("HF auth status fetch failed:", err);
      // Drop to a terminal state so consumers waiting on `status !== "loading"`
      // don't hang forever when the backend is unreachable.
      setAuth({
        status: "unauthenticated",
        loginCommand: "hf auth login",
        tokenPath: null,
      });
    }
  }, [baseUrl, fetchWithHeaders]);

  const login = useCallback(
    async (token: string) => {
      const response = await fetchWithHeaders(`${baseUrl}/hf-auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${response.status}`);
      }
      const body = await response.json().catch(() => ({}));
      setAuth({
        status: "authenticated",
        username: body.username ?? "authenticated",
        orgs: body.orgs ?? [],
      });
      await fetchStatus();
    },
    [baseUrl, fetchWithHeaders, fetchStatus],
  );

  const logout = useCallback(async () => {
    const response = await fetchWithHeaders(`${baseUrl}/hf-auth/logout`, {
      method: "POST",
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${response.status}`);
    }
    await fetchStatus();
  }, [baseUrl, fetchWithHeaders, fetchStatus]);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  const value = useMemo(
    () => ({ auth, refetch: fetchStatus, login, logout }),
    [auth, fetchStatus, login, logout]
  );

  return (
    <HfAuthContext.Provider value={value}>
      {children}
    </HfAuthContext.Provider>
  );
};

export const useHfAuth = (): HfAuthValue => {
  const ctx = useContext(HfAuthContext);
  if (ctx === undefined) {
    throw new Error("useHfAuth must be used within an HfAuthProvider");
  }
  return ctx;
};
