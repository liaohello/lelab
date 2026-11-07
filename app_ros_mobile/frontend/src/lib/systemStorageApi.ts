import { apiRequest, Fetcher } from "./apiClient";
import { DatasetItem } from "./replayApi";

export interface CpuStatus {
  percent: number;
  count: number;
  load_1m: number | null;
}

export interface MemoryStatus {
  total: number;
  used: number;
  available: number;
  percent: number;
}

export interface DiskStatus {
  path: string;
  total: number;
  used: number;
  free: number;
  percent: number;
}

export interface SystemStatus {
  timestamp: string | number;
  host: string;
  platform: string;
  cpu: CpuStatus;
  memory: MemoryStatus;
  disk: DiskStatus;
  uptime_seconds: number;
  temperatures: unknown;
  gpu: unknown;
  network: unknown;
}

export interface StorageSummary {
  root: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  percent_used: number;
  warning_level: string;
  datasets: number | unknown[];
  total_dataset_bytes: number;
}

export interface DatasetSyncState {
  state:
    | "unknown"
    | "not-synced"
    | "syncing"
    | "queued"
    | "synced"
    | "failed"
    | "cancelled";
  phase?: string | null;
  progress?: number | null;
  transferred?: number | null;
  total?: number | null;
  rate_bytes_per_second?: number | null;
  eta_seconds?: number | null;
  synced_at?: string | null;
  remote_path?: string | null;
  manifest_id?: string | null;
  error?: string | null;
}

export interface LocalDatasetSummary {
  repo_id: string;
  path?: string;
  size_bytes?: number | null;
  codebase_version?: string | null;
  robot_type?: string | null;
  fps?: number | null;
  total_episodes?: number | null;
  total_frames?: number | null;
  duration_seconds?: number | null;
  camera_count?: number | null;
  last_recorded_at?: string | null;
  finalized?: boolean;
  storage_format?: string | null;
  sealed?: boolean;
  seal_state?: "unsealed" | "sealing" | "sealed" | "corrupt";
  sealed_at?: string | null;
  seal_manifest_sha256?: string | null;
  seal_file_count?: number | null;
  seal_size_bytes?: number | null;
  sync?: DatasetSyncState;
}

export interface DatasetIntegrityCheck {
  name: string;
  status: "ok" | "warning" | "error";
  detail: string;
}

export interface DatasetIntegrity {
  repo_id: string;
  status: "ok" | "warning" | "error";
  checks: DatasetIntegrityCheck[];
  data_file_count?: number;
}

export interface TrainServerStatus {
  configured: boolean;
  error?: string;
  endpoint?: string;
  host?: string;
  port?: number;
  user?: string;
  root?: string;
  ssh_key?: string | null;
  jobs_root?: string;
  datasets_root?: string;
  policy_endpoint?: { host: string; port: number };
}

export interface DatasetSyncResult {
  success: boolean;
  sync: {
    id: string;
    phase: string;
    repo_id: string;
    progress: number;
    transferred: number;
    total: number | null;
    error: string | null;
    paths?: { source: string; staging: string; target: string; previous: string };
    commands?: Record<string, unknown>;
  };
}

export interface DatasetStorageDetail {
  repo_id?: string;
  root?: string;
  path?: string;
  size_bytes?: number;
  total_bytes?: number;
  num_episodes?: number;
  total_episodes?: number;
  num_frames?: number;
  total_frames?: number;
  storage_format?: string;
  format?: string | Record<string, unknown>;
  storage?: Record<string, unknown>;
  meta?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  info?: Record<string, unknown>;
  features?: unknown;
  path_templates?: unknown;
  file_tree?: unknown;
  parquet_schema?: unknown;
  [key: string]: unknown;
}

export interface LocalDatasetStorageResult {
  dataset: DatasetItem;
  detail: DatasetStorageDetail | null;
  error: string | null;
}

export async function getSystemStatus(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<SystemStatus> {
  return apiRequest<SystemStatus>(baseUrl, fetcher, "/system/status", {
    signal,
    action: "读取系统状态",
  });
}

export async function getStorageSummary(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<StorageSummary> {
  return apiRequest<StorageSummary>(baseUrl, fetcher, "/storage/summary", {
    signal,
    action: "读取存储摘要",
  });
}

export async function getDatasetStorage(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<DatasetStorageDetail> {
  const encodedRepoId = repoId
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return apiRequest<DatasetStorageDetail>(
    baseUrl,
    fetcher,
    `/datasets/local/${encodedRepoId}/storage`,
    { signal, action: `读取数据集 ${repoId}` },
  );
}

export async function listLocalDatasetStorage(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<LocalDatasetStorageResult[]> {
  // Use the local-only route so opening the Jetson storage dashboard never
  // performs a Hugging Face Hub request, even when the user is signed in.
  const localDatasets = await apiRequest<DatasetItem[]>(
    baseUrl,
    fetcher,
    "/datasets/local",
    { signal, action: "读取本地数据集" },
  );

  return Promise.all(
    localDatasets.map(async (dataset) => {
      try {
        const detail = await getDatasetStorage(
          baseUrl,
          fetcher,
          dataset.repo_id,
          signal,
        );
        return { dataset, detail, error: null };
      } catch (error) {
        if (signal?.aborted) throw error;
        return {
          dataset,
          detail: null,
          error: error instanceof Error ? error.message : "数据集详情不可用",
        };
      }
    }),
  );
}

const encodeRepoId = (repoId: string): string =>
  repoId
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");

export async function syncDatasetToTrain(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<DatasetSyncResult> {
  return apiRequest<DatasetSyncResult>(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/sync-train`,
    { method: "POST", body: { dry_run: false }, signal, action: `同步 ${repoId} 到训练机` },
  );
}

export async function getDatasetSyncStatus(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<{ repo_id: string; active: boolean; state: DatasetSyncState }> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/sync-status`,
    { signal, action: `读取 ${repoId} 同步状态` },
  );
}

export async function getDatasetIntegrity(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<DatasetIntegrity> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/integrity`,
    { signal, action: `检查 ${repoId} 完整性` },
  );
}

export async function trashDataset(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<{ success: boolean; trashed: { repo_id: string; entry: string } }> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/trash`,
    { method: "POST", body: { reason: "web-ui" }, signal, action: `移动 ${repoId} 到回收站` },
  );
}

export interface DatasetSealStatus {
  repo_id: string;
  state: "unknown" | "unsealed" | "sealing" | "sealed" | "failed" | "cancelled" | "cancelling";
  sealed_at?: string | null;
  manifest_sha256?: string | null;
  file_count?: number | null;
  size_bytes?: number | null;
  files_done?: number | null;
  files_total?: number | null;
  bytes_done?: number | null;
  bytes_total?: number | null;
  progress?: number | null;
  error?: string | null;
}

export async function sealDataset(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<{ success: boolean; seal: DatasetSealStatus }> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/seal`,
    { method: "POST", body: {}, signal, action: `封存 ${repoId}` },
  );
}

export async function getDatasetSealStatus(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<{ repo_id: string; seal: DatasetSealStatus }> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/seal`,
    { signal, action: `读取 ${repoId} 封存状态` },
  );
}

export async function unsealDataset(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<{ success: boolean }> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/unseal`,
    { method: "POST", body: {}, signal, action: `解封 ${repoId}` },
  );
}

export async function verifyDatasetSeal(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<{
  repo_id: string;
  ok: boolean;
  added: string[];
  removed: string[];
  changed: string[];
}> {
  return apiRequest(
    baseUrl,
    fetcher,
    `/datasets/local/${encodeRepoId(repoId)}/seal/verify`,
    { method: "POST", body: {}, signal, action: `校验 ${repoId} 封存` },
  );
}

export async function getTrainServerStatus(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<TrainServerStatus> {
  return apiRequest<TrainServerStatus>(baseUrl, fetcher, "/system/train-server", {
    signal,
    action: "读取训练机配置",
  });
}

export interface TrainServerGpu {
  index: number;
  name: string;
  mem_total_mb: number;
  mem_used_mb: number;
  mem_free_mb: number;
  util_percent: number;
  temp_c: number;
  our_jobs: { pid: number; mem_mb: number }[];
  foreign_procs: number;
}

export interface TrainServerJob {
  id: string;
  pid: number | null;
  exit_code: number | null;
  state: "running" | "exited" | "lost";
  gpu: number | null;
  mem_mb: number | null;
  checkpoint_count: number;
  checkpoint_mb: number;
  checkpoints: { step: number; size_mb: number }[];
  ours: boolean;
}

export interface TrainServerSession {
  stamp: string;
  pid: number | null;
  state: "running" | "stopped";
  gpu: number | null;
  port: number;
  policy: string | null;
  ours: boolean;
  client_connected: boolean;
  last_action: boolean;
}

/** Live control-plane snapshot (SSH-collected; never through 28081). */
export interface TrainServerLiveStatus {
  ssh_ok: boolean;
  fetched_at: number;
  endpoint?: string;
  error: string | null;
  policy_port: { port: number | null; state: "free" | "ours" | "foreign" | "unknown"; pid: number | null };
  gpus: TrainServerGpu[];
  compute_apps: { pid: number; name: string; mem_mb: number; gpu_index: number | null; ours: boolean }[];
  jobs: TrainServerJob[];
  sessions: TrainServerSession[];
  disk: {
    total_gb: number | null;
    used_gb: number | null;
    free_gb: number | null;
    free_percent: number | null;
  };
  usage: { jobs_gb: number | null; policy_server_gb: number | null };
}

export async function getTrainServerLiveStatus(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<TrainServerLiveStatus> {
  return apiRequest<TrainServerLiveStatus>(baseUrl, fetcher, "/system/train-server/status", {
    signal,
    action: "读取训练机实时状态",
  });
}

export async function stopRemotePolicyServer(
  baseUrl: string,
  fetcher: Fetcher,
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>(baseUrl, fetcher, "/inference/remote/server/stop", {
    method: "POST",
    action: "清理训练机遗留推理进程",
  });
}

export interface HardwareLease {
  state: "idle" | "held";
  task: string | null;
  owner: string | null;
  token: string | null;
  expires_in: number | null;
  ttl_seconds: number;
  last_release: {
    task: string;
    owner: string;
    reason: string;
    released_at: string | null;
  } | null;
}

export async function getHardwareLease(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<HardwareLease> {
  return apiRequest<HardwareLease>(baseUrl, fetcher, "/hardware/lease", {
    signal,
    action: "读取硬件控制锁",
  });
}

export async function renewHardwareLease(
  baseUrl: string,
  fetcher: Fetcher,
  options: { token?: string; task?: string; ttlSeconds?: number } = {},
  signal?: AbortSignal,
): Promise<{ success: boolean; lease: HardwareLease }> {
  return apiRequest(baseUrl, fetcher, "/hardware/lease/renew", {
    method: "POST",
    body: {
      ...(options.token ? { token: options.token } : {}),
      ...(options.task ? { task: options.task } : {}),
      ...(options.ttlSeconds ? { ttl_seconds: options.ttlSeconds } : {}),
    },
    signal,
    action: "续期硬件控制锁",
  });
}

export async function emergencyStop(
  baseUrl: string,
  fetcher: Fetcher,
  reason: string,
  signal?: AbortSignal,
): Promise<{ success: boolean }> {
  return apiRequest(baseUrl, fetcher, "/emergency-stop", {
    method: "POST",
    body: { reason },
    signal,
    action: "紧急停止",
  });
}
