import { Fetcher, apiRequest } from "./apiClient";

export type DatasetSource = "local" | "hub" | "both";

export interface DatasetItem {
  repo_id: string;
  last_modified: string | null;
  private: boolean;
  source: DatasetSource;
  // Summary fields the local-only listing adds; the Hub-only listing omits them.
  sealed?: boolean;
  seal_state?: "unsealed" | "sealing" | "sealed" | "corrupt";
  sealed_at?: string | null;
  finalized?: boolean;
  total_episodes?: number | null;
  total_frames?: number | null;
}

export async function listDatasets(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<DatasetItem[]> {
  return apiRequest<DatasetItem[]>(baseUrl, fetcher, "/datasets", {
    signal,
    action: "List datasets",
  });
}
