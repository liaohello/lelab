import React, { createContext, useContext, ReactNode, useState, useCallback, useMemo } from "react";

interface ApiContextType {
  baseUrl: string;
  wsBaseUrl: string;
  /** Stable per-browser label; the backend uses it as the hardware-lease owner. */
  clientId: string;
  fetchWithHeaders: (url: string, options?: RequestInit) => Promise<Response>;
}

const ApiContext = createContext<ApiContextType | undefined>(undefined);

const STORAGE_KEY = "lelab.apiBaseUrl";
const CLIENT_KEY = "lelab.clientId";
const DEFAULT_LOCALHOST = "http://localhost:8000";

const cleanBaseUrl = (url: string): string => url.replace(/\/$/, "");

const httpToWs = (url: string): string => url.replace(/^http(s?):/, "ws$1:");

const resolveClientId = (): string => {
  if (typeof window === "undefined") return "server";
  try {
    const existing = window.localStorage.getItem(CLIENT_KEY);
    if (existing) return existing;
    const generated =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID().slice(0, 8)
        : Math.random().toString(36).slice(2, 10);
    const label = `${window.navigator.platform || "web"}-${generated}`;
    window.localStorage.setItem(CLIENT_KEY, label);
    return label;
  } catch {
    return "web-client";
  }
};

const resolveInitialBaseUrl = (): string => {
  if (typeof window === "undefined") return DEFAULT_LOCALHOST;

  const configured = import.meta.env.VITE_API_URL?.trim();
  if (configured) return cleanBaseUrl(configured);

  const fromQuery = new URLSearchParams(window.location.search).get("api");
  if (fromQuery) {
    try {
      new URL(fromQuery);
      const clean = cleanBaseUrl(fromQuery);
      window.localStorage.setItem(STORAGE_KEY, clean);
      return clean;
    } catch {
      console.warn("Invalid `api` query param, ignoring:", fromQuery);
    }
  }

  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored) return cleanBaseUrl(stored);

  // The Vite development server runs separately from FastAPI. Production is
  // served by the Jetson backend, so same-origin keeps Windows access working
  // without a machine-specific hostname.
  if (import.meta.env.DEV) return DEFAULT_LOCALHOST;
  return cleanBaseUrl(window.location.origin || DEFAULT_LOCALHOST);
};

export const ApiProvider: React.FC<{ children: ReactNode }> = ({
  children,
}) => {
  const [baseUrl] = useState<string>(resolveInitialBaseUrl);
  const [clientId] = useState<string>(resolveClientId);
  const wsBaseUrl = httpToWs(baseUrl);

  const fetchWithHeaders = useCallback(async (url: string, options: RequestInit = {}): Promise<Response> => {
    return fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-LeLab-Client": clientId,
        ...options.headers,
      },
    });
  }, [clientId]);

  const value = useMemo(
    () => ({ baseUrl, wsBaseUrl, clientId, fetchWithHeaders }),
    [baseUrl, wsBaseUrl, clientId, fetchWithHeaders]
  );

  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>;
};

export const useApi = (): ApiContextType => {
  const context = useContext(ApiContext);
  if (context === undefined) {
    throw new Error("useApi must be used within an ApiProvider");
  }
  return context;
};
