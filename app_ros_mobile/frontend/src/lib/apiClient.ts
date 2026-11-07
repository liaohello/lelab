export type Fetcher = (
  url: string,
  options?: RequestInit
) => Promise<Response>;

export class ApiError extends Error {
  status: number;
  detail: string | null;
  constructor(message: string, status: number, detail: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export interface ApiRequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Human-readable label for the error message, e.g. "Start training". */
  action?: string;
}

/**
 * Performs a request against the lelab backend and parses the JSON response.
 * Throws ApiError with FastAPI's `detail` field on non-2xx, or on JSON parse
 * failure. Use this in place of ad-hoc `r.ok` / `r.json()` branching.
 */
/**
 * FastAPI handlers sometimes raise `HTTPException(detail={"message": ..., "holder": ...})`
 * to carry structure (the hardware lease does this). Returning that object as-is makes
 * every caller that interpolates it into a message render "[object Object]" — or throw,
 * when it ends up as a React child — so the real reason never reaches the operator.
 */
export function describeDetail(raw: unknown): string | null {
  if (raw === null || raw === undefined) return null;
  if (typeof raw === "string") return raw;
  if (typeof raw === "object") {
    const record = raw as Record<string, unknown>;
    if (typeof record.message === "string") {
      const holder = record.holder as Record<string, unknown> | undefined;
      return holder && typeof holder.owner === "string"
        ? `${record.message}（持有者 ${holder.owner}${typeof holder.task === "string" ? ` / ${holder.task}` : ""}）`
        : record.message;
    }
    return JSON.stringify(raw);
  }
  return String(raw);
}

export async function apiRequest<T = unknown>(
  baseUrl: string,
  fetcher: Fetcher,
  path: string,
  { method = "GET", body, signal, action }: ApiRequestOptions = {}
): Promise<T> {
  const init: RequestInit = { method, signal };
  if (body !== undefined) init.body = JSON.stringify(body);

  const url = `${baseUrl}${path}`;
  const r = await fetcher(url, init);
  if (!r.ok) {
    let detail: string | null = null;
    try {
      const errBody = await r.json();
      detail = describeDetail(errBody?.detail ?? errBody?.message ?? null);
    } catch {
      // body wasn't JSON
    }
    const label = action || `${method} ${path}`;
    throw new ApiError(
      `${label} failed: ${detail ?? r.status}`,
      r.status,
      detail
    );
  }
  // 204 No Content
  if (r.status === 204) return undefined as T;
  return r.json() as Promise<T>;
}
