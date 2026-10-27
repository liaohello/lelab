import { apiRequest, Fetcher } from "./apiClient";

export type WeightsMode = "frames" | "equal" | "custom";

export interface CompositeMember {
  repo_id: string;
  robot_type?: string | null;
  fps?: number | null;
  total_episodes?: number | null;
  total_frames?: number | null;
  size_bytes?: number | null;
  duration_seconds?: number | null;
  sealed?: boolean;
  sealed_at?: string | null;
  cameras?: { name: string }[];
}

export interface CompositeIssue {
  code: string;
  message: string;
  repo_ids: string[];
}

export interface CompositeReport {
  ok: boolean;
  repo_ids: string[];
  members: CompositeMember[];
  blockers: CompositeIssue[];
  warnings: CompositeIssue[];
  estimated_size_bytes: number;
  composite_hash: string | null;
  composite_repo_id: string | null;
  weights_mode: WeightsMode;
  weights: number[] | null;
  cached?: boolean;
  remote_state?: string;
  remote_error?: string;
}

export type CompositeBuildState =
  | "unknown"
  | "verifying"
  | "merging"
  | "publishing"
  | "ready"
  | "failed"
  | "cancelled"
  | "cancelling";

export interface CompositeBuild {
  composite_hash: string;
  composite_repo_id?: string;
  state: CompositeBuildState;
  phase?: string;
  progress?: number;
  error?: string | null;
  log_tail?: string[];
  repo_ids?: string[];
  cached?: boolean;
}

export interface RemoteComposite {
  repo_id: string;
  hash?: string | null;
  members: string[];
  built_at?: number | null;
  size_bytes?: number | null;
  estimated_size_bytes?: number | null;
  file_count?: number | null;
  weights_mode?: WeightsMode;
  ready: boolean;
}

export interface CompositeTrashEntry {
  repo_id: string;
  entry: string;
  trashed_at?: string;
}

export async function checkCompatibility(
  baseUrl: string,
  fetcher: Fetcher,
  repoIds: string[],
  options: { weightsMode?: WeightsMode; weights?: number[]; signal?: AbortSignal } = {},
): Promise<CompositeReport> {
  return apiRequest<CompositeReport>(baseUrl, fetcher, "/datasets/compatibility", {
    method: "POST",
    body: {
      repo_ids: repoIds,
      weights_mode: options.weightsMode ?? "frames",
      weights: options.weights,
    },
    signal: options.signal,
    action: "检查数据集兼容性",
  });
}

export async function buildComposite(
  baseUrl: string,
  fetcher: Fetcher,
  repoIds: string[],
  options: { weightsMode?: WeightsMode; weights?: number[]; signal?: AbortSignal } = {},
): Promise<{ success: boolean; cached: boolean; composite: CompositeReport; build: CompositeBuild }> {
  return apiRequest(baseUrl, fetcher, "/datasets/composite", {
    method: "POST",
    body: {
      repo_ids: repoIds,
      weights_mode: options.weightsMode ?? "frames",
      weights: options.weights,
    },
    signal: options.signal,
    action: "准备组合数据集",
  });
}

export async function getCompositeBuildStatus(
  baseUrl: string,
  fetcher: Fetcher,
  key: string,
  signal?: AbortSignal,
): Promise<{ build: CompositeBuild; remote: { state?: string } | null }> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/composite/status?key=${encodeURIComponent(key)}`,
    { signal, action: "读取组合数据集进度" },
  );
}

export async function cancelCompositeBuild(
  baseUrl: string,
  fetcher: Fetcher,
  key: string,
): Promise<{ success: boolean }> {
  return apiRequest(baseUrl, fetcher, "/datasets/composite/cancel", {
    method: "POST",
    body: { key },
    action: "取消组合数据集构建",
  });
}

export async function listComposites(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<RemoteComposite[]> {
  const body = await apiRequest<{ composites: RemoteComposite[] }>(
    baseUrl,
    fetcher,
    "/datasets/composites",
    { signal, action: "列出组合数据集" },
  );
  return body.composites;
}

export async function listCompositeTrash(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<CompositeTrashEntry[]> {
  const body = await apiRequest<{ trash: CompositeTrashEntry[] }>(
    baseUrl,
    fetcher,
    "/datasets/composites/trash",
    { signal, action: "列出组合数据集回收站" },
  );
  return body.trash;
}

export async function trashComposite(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
): Promise<{ success: boolean; trashed: CompositeTrashEntry }> {
  return apiRequest(baseUrl, fetcher, "/datasets/composites/trash", {
    method: "POST",
    body: { repo_id: repoId },
    action: `移动 ${repoId} 到回收站`,
  });
}

export async function restoreComposite(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  entry: string,
): Promise<{ success: boolean }> {
  return apiRequest(baseUrl, fetcher, "/datasets/composites/restore", {
    method: "POST",
    body: { repo_id: repoId, entry },
    action: `恢复 ${repoId}`,
  });
}

const BUILD_POLL_MS = 2000;
const BUILD_TIMEOUT_MS = 6 * 60 * 60 * 1000;

/**
 * Resolve a multi-dataset selection to a ready composite on the training
 * server, building it when necessary and polling until it is ready.
 */
export async function ensureCompositeReady(
  baseUrl: string,
  fetcher: Fetcher,
  repoIds: string[],
  options: {
    weightsMode?: WeightsMode;
    weights?: number[];
    signal?: AbortSignal;
    onProgress?: (build: CompositeBuild) => void;
  } = {},
): Promise<CompositeReport> {
  const started = await buildComposite(baseUrl, fetcher, repoIds, options);
  if (started.cached) return started.composite;
  const key = started.build.composite_hash || started.composite.composite_repo_id || "";
  const deadline = Date.now() + BUILD_TIMEOUT_MS;
  let build = started.build;
  while (Date.now() < deadline) {
    if (build.state === "ready") return started.composite;
    if (build.state === "failed" || build.state === "cancelled") {
      throw new Error(build.error || `组合数据集构建${build.state === "failed" ? "失败" : "已取消"}`);
    }
    options.onProgress?.(build);
    await new Promise((resolve) => setTimeout(resolve, BUILD_POLL_MS));
    const status = await getCompositeBuildStatus(baseUrl, fetcher, key, options.signal);
    build = status.build;
    if (build.state === "unknown" && status.remote?.state === "ready") {
      return { ...started.composite, cached: true, remote_state: "ready" };
    }
  }
  throw new Error("组合数据集构建超时，请到数据页查看进度");
}
