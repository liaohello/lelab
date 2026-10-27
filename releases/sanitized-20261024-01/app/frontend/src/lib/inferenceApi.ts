import { Fetcher, apiRequest } from "./apiClient";

export interface StartInferenceRequest {
  follower_port: string;
  follower_config: string;
  policy_ref: string;
  task: string;
  cameras: Record<string, {
    type: string;
    camera_index?: number;
    width: number;
    height: number;
    fps?: number;
  }>;
  duration_s: number;
  // "local" runs the policy on this Jetson; "remote" runs it on the GPU
  // training server (LeLab starts the PolicyServer + SSH tunnel for you).
  target: "local" | "remote";
  policy_type?: string;
  actions_per_chunk?: number;
  fps?: number;
  chunk_size_threshold?: number;
}

export interface InferenceStatus {
  inference_active: boolean;
  started_at: number | null;
  rollout_started_at: number | null;
  elapsed_s: number;
  rollout_elapsed_s: number;
  duration_s: number | null;
  policy_ref: string | null;
  log_path: string | null;
  exited?: boolean;
  exit_code?: number | null;
  // Cloud sessions only.
  target?: "local" | "remote" | null;
  phase?: string | null;
  resolved_policy_path?: string | null;
  server_log_path?: string | null;
  server_pid?: number | null;
  tunnel_ready?: boolean | null;
  outcome?: string | null;
  error?: string | null;
  hint?: string | null;
}

export interface RemotePolicyServerStatus {
  configured: boolean;
  running: boolean;
  pid: number | null;
  log_path: string | null;
  session_dir: string | null;
  endpoint?: string;
  error: string | null;
}

export async function startInference(
  baseUrl: string,
  fetcher: Fetcher,
  request: StartInferenceRequest,
): Promise<{ message: string; log_path: string }> {
  return apiRequest<{ message: string; log_path: string }>(
    baseUrl,
    fetcher,
    "/start-inference",
    { method: "POST", body: request, action: "Start inference" },
  );
}

export async function stopInference(
  baseUrl: string,
  fetcher: Fetcher,
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>(baseUrl, fetcher, "/stop-inference", {
    method: "POST",
    action: "Stop inference",
  });
}

export async function getInferenceStatus(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<InferenceStatus> {
  return apiRequest<InferenceStatus>(baseUrl, fetcher, "/inference-status", {
    signal,
    action: "Get inference status",
  });
}

export async function getRemotePolicyServerStatus(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<RemotePolicyServerStatus> {
  return apiRequest<RemotePolicyServerStatus>(
    baseUrl,
    fetcher,
    "/inference/remote/server-status",
    { signal, action: "Get cloud policy server status" },
  );
}

export async function stopRemotePolicyServer(
  baseUrl: string,
  fetcher: Fetcher,
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>(
    baseUrl,
    fetcher,
    "/inference/remote/server/stop",
    { method: "POST", action: "Stop cloud policy server" },
  );
}
