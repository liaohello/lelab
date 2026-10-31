import React, { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Film, Loader2 } from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useApi } from "@/contexts/ApiContext";
import {
  getEpisodeTimeseries,
  getDatasetEpisodes,
  listLocalDatasetsForViewer,
  type EpisodeTimeseries,
} from "@/lib/datasetViewerApi";

const COLORS = ["#34d399", "#60a5fa", "#f472b6", "#fbbf24", "#a78bfa", "#22d3ee", "#f87171"];

const DatasetViewer: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [repoId, setRepoId] = useState<string>("");
  const [episode, setEpisode] = useState<number | null>(null);

  const datasetsQuery = useQuery({
    queryKey: ["viewer-datasets", baseUrl],
    queryFn: ({ signal }) => listLocalDatasetsForViewer(baseUrl, fetchWithHeaders, signal),
    staleTime: 15_000,
  });
  useEffect(() => {
    if (!repoId && datasetsQuery.data?.length) setRepoId(datasetsQuery.data[0].repo_id);
  }, [datasetsQuery.data, repoId]);

  const episodesQuery = useQuery({
    queryKey: ["viewer-episodes", baseUrl, repoId],
    queryFn: ({ signal }) => getDatasetEpisodes(baseUrl, fetchWithHeaders, repoId, signal),
    enabled: Boolean(repoId),
  });
  useEffect(() => {
    const first = episodesQuery.data?.episodes?.[0]?.index ?? null;
    setEpisode(first);
  }, [episodesQuery.data]);

  const timeseriesQuery = useQuery({
    queryKey: ["viewer-timeseries", baseUrl, repoId, episode],
    queryFn: ({ signal }) => getEpisodeTimeseries(baseUrl, fetchWithHeaders, repoId, episode as number, signal),
    enabled: Boolean(repoId) && episode != null,
  });

  const data = episodesQuery.data;
  const current = data?.episodes.find((e) => e.index === episode) ?? null;
  const chartRows = useMemo(() => {
    const series: EpisodeTimeseries | undefined = timeseriesQuery.data;
    if (!series) return [];
    return series.t.map((t, i) => {
      const row: Record<string, number> = { t };
      Object.entries(series.series).forEach(([label, values]) => {
        const vector = values[i];
        if (!vector) return;
        vector.forEach((value, joint) => {
          row[`${label[0]}${joint}`] = Number(value.toFixed(4));
        });
      });
      return row;
    });
  }, [timeseriesQuery.data]);
  const joints = useMemo(() => {
    const first = timeseriesQuery.data?.series.state?.[0] ?? timeseriesQuery.data?.series.action?.[0];
    return first ? first.length : 0;
  }, [timeseriesQuery.data]);

  return (
    <div className="min-h-screen bg-slate-950 px-4 py-6 text-slate-100 sm:px-8">
      <div className="mx-auto max-w-7xl space-y-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">数据可视化</h1>
            <p className="text-sm text-slate-500">
              浏览本机录制数据集：分集视频回放 + 关节 state / action 曲线（只读）
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Select value={repoId} onValueChange={setRepoId}>
              <SelectTrigger className="w-[320px] border-slate-700 bg-slate-900">
                <SelectValue placeholder="选择数据集" />
              </SelectTrigger>
              <SelectContent className="border-slate-700 bg-slate-900 text-slate-100">
                {(datasetsQuery.data ?? []).map((dataset) => (
                  <SelectItem key={dataset.repo_id} value={dataset.repo_id}>
                    {dataset.repo_id} · {dataset.total_episodes ?? "?"} 集
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {episodesQuery.isLoading ? (
          <p className="flex items-center gap-2 text-sm text-slate-400">
            <Loader2 className="h-4 w-4 animate-spin" /> 读取分集信息…
          </p>
        ) : null}
        {episodesQuery.isError ? (
          <p className="text-sm text-red-300">
            读取失败：{episodesQuery.error instanceof Error ? episodesQuery.error.message : "未知错误"}
          </p>
        ) : null}

        {data ? (
          <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
            <Card className="border-slate-800 bg-slate-900/70">
              <CardHeader className="p-4 pb-2">
                <CardDescription className="text-slate-400">分集</CardDescription>
                <CardTitle className="text-sm">
                  {data.total_episodes} 集 · {data.total_frames} 帧 · {data.fps} fps ·{" "}
                  {data.robot_type ?? "—"}
                </CardTitle>
              </CardHeader>
              <CardContent className="max-h-[520px] space-y-1 overflow-y-auto p-4 pt-0">
                {data.episodes.map((item) => (
                  <button
                    key={item.index}
                    type="button"
                    onClick={() => setEpisode(item.index)}
                    className={`flex w-full items-center justify-between rounded border px-2 py-1 text-left text-xs ${
                      item.index === episode
                        ? "border-emerald-600 bg-emerald-950/40 text-emerald-100"
                        : "border-slate-800 bg-slate-950/40 text-slate-300 hover:bg-slate-900"
                    }`}
                  >
                    <span>第 {item.index} 集</span>
                    <span className="text-slate-400">
                      {item.length} 帧 / {item.duration_s ?? "—"}s
                    </span>
                  </button>
                ))}
              </CardContent>
            </Card>

            <div className="space-y-4">
              {current?.task ? (
                <p className="text-xs text-slate-400">
                  任务描述：<span className="text-slate-200">{current.task}</span>
                </p>
              ) : null}
              <div className="grid gap-3 sm:grid-cols-2">
                {(current?.cameras ?? []).map((camera) => (
                  <Card key={camera} className="border-slate-800 bg-slate-900/70">
                    <CardHeader className="p-3 pb-1">
                      <CardDescription className="flex items-center gap-2 text-slate-400">
                        <Film className="h-4 w-4" /> {camera}
                        {current?.video_ranges?.[camera]?.from_s != null ? (
                          <span className="text-[10px] text-slate-500">
                            {current.video_ranges[camera].from_s}s – {current.video_ranges[camera].to_s ?? "?"}s
                          </span>
                        ) : null}
                      </CardDescription>
                    </CardHeader>
                    <CardContent className="p-3 pt-0">
                      {repoId && episode != null ? (
                        <video
                          key={`${repoId}-${episode}-${camera}`}
                          controls
                          muted
                          loop
                          preload="metadata"
                          className="w-full rounded bg-black"
                          /* #t=<start> 让播放器直接跳到本集在该 mp4 中的片段起点 */
                          src={`${baseUrl}/datasets/local/${repoId}/episodes/${episode}/video/${camera}#t=${
                            current?.video_ranges?.[camera]?.from_s ?? 0
                          }`}
                        />
                      ) : null}
                    </CardContent>
                  </Card>
                ))}
                {(current?.cameras ?? []).length === 0 ? (
                  <p className="text-xs text-slate-500">该分集没有视频轨道。</p>
                ) : null}
              </div>

              <Card className="border-slate-800 bg-slate-900/70">
                <CardHeader className="p-4 pb-2">
                  <CardDescription className="text-slate-400">
                    state / action（{joints} 维，已降采样到 ≤1200 点）
                  </CardDescription>
                </CardHeader>
                <CardContent className="h-72 p-4 pt-0">
                  {timeseriesQuery.isLoading ? (
                    <p className="flex items-center gap-2 text-sm text-slate-400">
                      <Loader2 className="h-4 w-4 animate-spin" /> 读取曲线…
                    </p>
                  ) : chartRows.length === 0 ? (
                    <p className="text-sm text-slate-500">没有可绘制的数据。</p>
                  ) : (
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={chartRows} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
                        <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                        <XAxis dataKey="t" stroke="#64748b" tick={{ fontSize: 10 }} unit="s" />
                        <YAxis stroke="#64748b" tick={{ fontSize: 10 }} />
                        <ChartTooltip
                          contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 11 }}
                          labelFormatter={(value) => `${value}s`}
                        />
                        <Legend wrapperStyle={{ fontSize: 10 }} />
                        {Array.from({ length: joints }).map((_, joint) => (
                          <Line
                            key={`s${joint}`}
                            type="monotone"
                            dataKey={`s${joint}`}
                            name={`state[${joint}]`}
                            stroke={COLORS[joint % COLORS.length]}
                            dot={false}
                            strokeWidth={1.5}
                          />
                        ))}
                        {Array.from({ length: joints }).map((_, joint) => (
                          <Line
                            key={`a${joint}`}
                            type="monotone"
                            dataKey={`a${joint}`}
                            name={`action[${joint}]`}
                            stroke={COLORS[joint % COLORS.length]}
                            strokeDasharray="4 3"
                            dot={false}
                            strokeWidth={1}
                          />
                        ))}
                      </LineChart>
                    </ResponsiveContainer>
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
};

export default DatasetViewer;
