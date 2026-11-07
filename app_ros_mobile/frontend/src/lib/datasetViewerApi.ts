import { Fetcher, apiRequest } from "./apiClient";

export interface ViewerDataset {
  repo_id: string;
  total_episodes: number | null;
  total_frames: number | null;
  fps?: number | null;
  robot_type?: string | null;
}

export interface ViewerEpisode {
  index: number;
  length: number;
  duration_s: number | null;
  task: string;
  cameras: string[];
  videos: Record<string, string>;
  /** v3 分块视频：多集共用同一个 mp4，用这两个时间戳定位本集片段。 */
  video_ranges?: Record<string, { from_s?: number; to_s?: number }>;
}

export interface EpisodeList {
  repo_id: string;
  fps: number;
  robot_type: string | null;
  total_episodes: number;
  total_frames: number | null;
  cameras: string[];
  episodes: ViewerEpisode[];
}

export interface EpisodeTimeseries {
  episode: number;
  points: number;
  total_frames?: number;
  t: number[];
  series: { state?: number[][]; action?: number[][] };
}

export async function listLocalDatasetsForViewer(
  baseUrl: string,
  fetcher: Fetcher,
  signal?: AbortSignal,
): Promise<ViewerDataset[]> {
  const body = await apiRequest<ViewerDataset[]>(baseUrl, fetcher, "/datasets/local", {
    signal,
    action: "读取本机数据集",
  });
  return body ?? [];
}

export async function getDatasetEpisodes(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  signal?: AbortSignal,
): Promise<EpisodeList> {
  return apiRequest<EpisodeList>(baseUrl, fetcher, `/datasets/local/${repoId}/episodes`, {
    signal,
    action: "读取分集列表",
  });
}

export async function getEpisodeTimeseries(
  baseUrl: string,
  fetcher: Fetcher,
  repoId: string,
  episode: number,
  signal?: AbortSignal,
): Promise<EpisodeTimeseries> {
  return apiRequest<EpisodeTimeseries>(
    baseUrl,
    fetcher,
    `/datasets/local/${repoId}/episodes/${episode}/timeseries`,
    { signal, action: "读取关节曲线" },
  );
}
