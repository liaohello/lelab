import { Fetcher, apiRequest } from "./apiClient";

export interface RemoteCheckpoint {
  step: number;
  size_mb: number;
}

export interface RemoteCheckpointJob {
  id: string;
  ours: boolean;
  state: string | null;
  checkpoint_mb: number;
  checkpoints: RemoteCheckpoint[];
}

export interface CheckpointInventory {
  ssh_ok: boolean;
  error: string | null;
  disk: {
    total_gb: number | null;
    used_gb: number | null;
    free_gb: number | null;
    free_percent: number | null;
  };
  usage: { jobs_gb: number | null; policy_server_gb: number | null };
  keep_n: number;
  reclaimable_mb: number;
  reclaimable: { job_id: string; step: number; size_mb: number }[];
  jobs: RemoteCheckpointJob[];
}

export interface MetricsHistoryPoint {
  step: number;
  loss: number | null;
  lr: number | null;
  grad_norm: number | null;
}

export async function getCheckpointInventory(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<CheckpointInventory> {
  return apiRequest<CheckpointInventory>(baseUrl, fetcher, "/system/train-server/checkpoints", {
    signal,
    action: "读取训练机权重清单",
  });
}

export async function pruneCheckpoints(
  baseUrl: string,
  fetcher: Fetcher,
  body: { keep_n: number; job_ids?: string[]; dry_run: boolean },
): Promise<{
  dry_run: boolean;
  would_delete?: { job_id: string; step: number; size_mb: number }[];
  reclaimable_mb?: number;
  deleted?: { job_id: string; step: number; size_mb: number }[];
  freed_mb?: number;
}> {
  return apiRequest(baseUrl, fetcher, "/system/train-server/checkpoints/prune", {
    method: "POST",
    body,
    action: "清理中间权重",
  });
}

export async function deleteCheckpoints(
  baseUrl: string,
  fetcher: Fetcher,
  body: { job_id: string; steps: number[] },
): Promise<{ deleted: { job_id: string; step: number; size_mb: number }[]; freed_mb: number }> {
  return apiRequest(baseUrl, fetcher, "/system/train-server/checkpoints", {
    method: "DELETE",
    body,
    action: "删除权重",
  });
}

export async function getJobMetricsHistory(
  baseUrl: string,
  fetcher: Fetcher,
  jobId: string,
  signal?: AbortSignal,
): Promise<MetricsHistoryPoint[]> {
  const body = await apiRequest<{ points: MetricsHistoryPoint[] }>(
    baseUrl,
    fetcher,
    `/jobs/${jobId}/metrics-history`,
    { signal, action: "读取训练曲线" },
  );
  return body.points;
}

export interface LocalModelEntry {
  job_id: string;
  step?: number | null;
  size_mb?: number;
  pulled_at?: number;
  source?: string;
  path?: string;
}

export interface TrashedModelEntry {
  job_id: string;
  trash_name: string;
  step?: number | null;
  size_mb: number;
  trashed_at?: number | null;
}

export interface LocalModelsResponse {
  keep: number;
  models: LocalModelEntry[];
  trash: TrashedModelEntry[];
}

export interface PullProgress {
  phase: "idle" | "transferring" | "verifying" | "completed" | "failed";
  job_id?: string;
  step?: number;
  error?: string;
  moved_to_trash?: string[];
}

export interface RemoteModelLibrary {
  ssh_ok: boolean;
  error: string | null;
  endpoint?: string;
  library: {
    job_id: string;
    state: string | null;
    checkpoints: { step: number; size_mb: number }[];
    total_mb: number;
    local: boolean;
    local_step?: number | null;
  }[];
  weights: { kind: string; name: string; size_mb: number; remote_path: string }[];
}

export async function getLocalModels(baseUrl: string, fetcher: Fetcher, signal?: AbortSignal) {
  return apiRequest<LocalModelsResponse>(baseUrl, fetcher, "/models/local", {
    signal,
    action: "读取本机模型",
  });
}

export async function getPullProgress(baseUrl: string, fetcher: Fetcher, signal?: AbortSignal) {
  return apiRequest<PullProgress>(baseUrl, fetcher, "/models/pull-status", {
    signal,
    action: "读取下载进度",
  });
}

export async function getRemoteModelLibrary(baseUrl: string, fetcher: Fetcher, signal?: AbortSignal) {
  return apiRequest<RemoteModelLibrary>(baseUrl, fetcher, "/system/train-server/models", {
    signal,
    action: "读取远程模型库",
  });
}

export async function pullRemoteCheckpoint(
  baseUrl: string,
  fetcher: Fetcher,
  body: { job_id: string; step: number },
) {
  return apiRequest<{ job_id: string; step: number }>(baseUrl, fetcher, "/models/pull", {
    method: "POST",
    body,
    action: "下载 checkpoint",
  });
}

export async function trashLocalModel(baseUrl: string, fetcher: Fetcher, jobId: string) {
  return apiRequest<{ trash_name: string }>(baseUrl, fetcher, `/models/local/${jobId}/trash`, {
    method: "POST",
    action: "移入回收站",
  });
}

export async function restoreLocalModel(baseUrl: string, fetcher: Fetcher, trashName: string) {
  return apiRequest<{ job_id: string }>(
    baseUrl,
    fetcher,
    `/models/local/trash/${trashName}/restore`,
    { method: "POST", action: "恢复模型" },
  );
}

export async function purgeLocalModel(baseUrl: string, fetcher: Fetcher, trashName: string) {
  return apiRequest<{ purged: string }>(baseUrl, fetcher, `/models/local/trash/${trashName}`, {
    method: "DELETE",
    action: "彻底删除模型",
  });
}
