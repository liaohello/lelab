import React, { useCallback, useMemo, useRef, useState } from "react";
import { AlertTriangle, Download, Loader2, RefreshCw, Upload, XCircle } from "lucide-react";
import { apiRequest } from "@/lib/apiClient";
import { reasonText, type SessionRef } from "@/components/control/hybridContract";

/**
 * Synchronise → export → inspect, for the hybrid session.
 *
 * The browser never invents a dataset: it asks the backend for the export report and reads the
 * exported dataset back through the existing dataset-viewer endpoints. Phase video is the whole
 * episode mp4 played at a `#t=from,to` fragment — there is no phase-scoped video endpoint yet,
 * and the UI says so instead of pretending the slice is a separate asset.
 */

type Props = {
  baseUrl: string;
  fetchWithHeaders: (url: string, options?: RequestInit) => Promise<Response>;
  online: boolean;
  session: SessionRef;
  profileReady: boolean;
  simulated: boolean;
  syncAllowed: boolean;
  exportAllowed: boolean;
};

const button = "rounded border border-gray-600 px-3 py-1.5 text-sm disabled:opacity-40";
const box = "rounded-lg border border-gray-800 bg-gray-950/60 p-3";
const th = "py-1 pr-2 font-normal text-gray-500";
const td = "py-1 pr-2 font-mono text-gray-200";

const encodeRepo = (repoId: string): string =>
  repoId.split("/").map((segment) => encodeURIComponent(segment)).join("/");

const numberText = (value: unknown, digits = 3): string =>
  typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";

const PALETTE = ["#38bdf8", "#f472b6", "#facc15", "#4ade80", "#a78bfa", "#fb923c", "#22d3ee", "#f87171", "#c084fc"];

type PhaseSlice = {
  phase: string;
  fromS: number | null;
  toS: number | null;
  videoRanges?: Record<string, { from_s?: number; to_s?: number }>;
  source?: string;
};

function readPhases(episode: any): PhaseSlice[] {
  const raw = Array.isArray(episode?.phases) ? episode.phases : [];
  return raw
    .map((item: any) => {
      const fromS = typeof item?.from_s === "number" ? item.from_s : null;
      const toS = typeof item?.to_s === "number" ? item.to_s : null;
      return {
        phase: typeof item?.phase === "string" ? item.phase : "unknown",
        fromS,
        toS,
        videoRanges: item?.video_ranges,
        source: typeof item?.source === "string" ? item.source : undefined,
      };
    })
    .filter((item: PhaseSlice) => item.phase !== "unknown");
}

/** Minimal SVG line chart: no dependency, deterministic, and it says "no data" instead of zeros. */
function PhaseCurves({ timeseries, fromS, toS }: { timeseries: any; fromS: number | null; toS: number | null }) {
  const groups = useMemo(() => {
    const times: number[] = Array.isArray(timeseries?.t) ? timeseries.t : [];
    const series = timeseries?.series ?? {};
    const entries: { key: string; matrix: number[][] }[] = [];
    if (Array.isArray(series.state)) entries.push({ key: "state", matrix: series.state });
    if (Array.isArray(series.action)) entries.push({ key: "action", matrix: series.action });
    if (times.length === 0 || entries.length === 0) return null;

    const low = fromS ?? times[0];
    const high = toS ?? times[times.length - 1];
    const indices = times
      .map((time, index) => ({ time, index }))
      .filter(({ time }) => time >= low && time <= high);
    if (indices.length === 0) return null;

    const stride = Math.max(1, Math.ceil(indices.length / 240));
    const sampled = indices.filter((_, position) => position % stride === 0);

    const result = entries.map(({ key, matrix }) => {
      const dims = Math.max(0, ...matrix.map((row) => (Array.isArray(row) ? row.length : 0)));
      const lines = Array.from({ length: dims }, (_, dim) => {
        const values = sampled.map(({ index }) => {
          const row = matrix[index];
          const value = Array.isArray(row) ? Number(row[dim]) : NaN;
          return Number.isFinite(value) ? value : null;
        });
        const finite = values.filter((value): value is number => value !== null);
        const min = finite.length > 0 ? Math.min(...finite) : 0;
        const max = finite.length > 0 ? Math.max(...finite) : 0;
        const span = max - min || 1;
        const width = 480;
        const height = 96;
        const points = values
          .map((value, position) => {
            if (value === null) return null;
            const x = sampled.length > 1 ? (position / (sampled.length - 1)) * width : 0;
            const y = height - ((value - min) / span) * (height - 8) - 4;
            return `${x.toFixed(1)},${y.toFixed(1)}`;
          })
          .filter((point): point is string => point !== null)
          .join(" ");
        return { dim, points, min, max };
      });
      return { key, dims, lines };
    });
    return { low, high, count: sampled.length, result };
  }, [timeseries, fromS, toS]);

  if (!groups) {
    return <p className="mt-1 text-xs text-amber-300" data-testid="curves-empty">该阶段窗口没有可用的 state/action 数值序列；不补零、不外推。</p>;
  }

  return (
    <div className="mt-2 space-y-2" data-testid="phase-curves">
      <p className="text-xs text-gray-500">
        窗口 {numberText(groups.low)}–{numberText(groups.high)} s · {groups.count} 采样点（下采样）· 仅展示曲线形状，纵轴为窗口内归一化，不代表物理量程
      </p>
      {groups.result.map((group) => (
        <div key={group.key}>
          <p className="text-xs text-gray-400">{group.key} · {group.dims} 维</p>
          <svg viewBox="0 0 480 96" className="mt-1 w-full rounded border border-gray-800 bg-black/40" role="img" aria-label={`${group.key} 曲线`}>
            {group.lines.map((line) => (
              <polyline
                key={line.dim}
                fill="none"
                stroke={PALETTE[line.dim % PALETTE.length]}
                strokeWidth={1.2}
                points={line.points}
              />
            ))}
          </svg>
          <p className="font-mono text-[10px] text-gray-500">
            {group.lines.map((line) => `d${line.dim} ${numberText(line.min, 2)}…${numberText(line.max, 2)}`).join(" · ")}
          </p>
        </div>
      ))}
    </div>
  );
}

export default function HybridDataReview({
  baseUrl,
  fetchWithHeaders,
  online,
  session,
  profileReady,
  simulated,
  syncAllowed,
  exportAllowed,
}: Props) {
  const [sessions, setSessions] = useState<any[]>([]);
  const [selected, setSelected] = useState("");
  const [syncResult, setSyncResult] = useState<any>(null);
  const [job, setJob] = useState<any>(null);
  const [report, setReport] = useState<any>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [repoId, setRepoId] = useState("");
  const [episodes, setEpisodes] = useState<any[]>([]);
  const [episodeIndex, setEpisodeIndex] = useState<number | "">("");
  // -1 means "whole episode"; >= 0 indexes `phases` directly (no off-by-one in the select value).
  const [phaseIndex, setPhaseIndex] = useState(-1);
  const [timeseries, setTimeseries] = useState<any>(null);
  const pollAbort = useRef(false);

  const get = useCallback(async (path: string, label: string) => {
    try {
      return await apiRequest<any>(baseUrl, fetchWithHeaders, path, { action: label });
    } catch (exc: any) {
      setError(`${label}：${reasonText(exc?.detail ?? exc?.message ?? exc)}`);
      return null;
    }
  }, [baseUrl, fetchWithHeaders]);

  const post = useCallback(async (path: string, body: Record<string, unknown>, label: string) => {
    setBusy(label);
    try {
      return await apiRequest<any>(baseUrl, fetchWithHeaders, path, { method: "POST", body });
    } catch (exc: any) {
      setError(`${label}：${reasonText(exc?.detail ?? exc?.message ?? exc)}`);
      return null;
    } finally {
      setBusy(null);
    }
  }, [baseUrl, fetchWithHeaders]);

  const loadSessions = useCallback(async () => {
    const data = await get("/ros-mobile/sessions", "读取 session 列表");
    if (!data) return;
    const list = Array.isArray(data.sessions) ? data.sessions : [];
    setSessions(list);
    if (!selected && list.length > 0) setSelected(list[list.length - 1].session_id);
  }, [get, selected]);

  const sync = useCallback(async () => {
    if (!selected) return;
    setError(null);
    const data = await post("/ros-mobile/sync", { session_id: selected }, "同步原始数据");
    if (data) setSyncResult(data);
  }, [post, selected]);

  const pollJob = useCallback(async (jobId: string) => {
    pollAbort.current = false;
    for (let attempt = 0; attempt < 120; attempt += 1) {
      if (pollAbort.current) return;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const snapshot = await get(`/ros-mobile/export/status/${encodeURIComponent(jobId)}`, "查询导出状态");
      if (!snapshot) return;
      setJob(snapshot);
      if (snapshot.status !== "running") {
        if (snapshot.status === "completed") {
          setReport(snapshot.report ?? null);
          if (snapshot.report?.repo_id) setRepoId(snapshot.report.repo_id);
        } else if (snapshot.status === "failed") {
          setError(`导出失败：${reasonText(snapshot.error ?? "未知原因")}`);
        }
        return;
      }
    }
    setError("导出任务轮询超时；任务可能仍在后台运行，请稍后刷新任务状态。");
  }, [get]);

  const exportSession = useCallback(async () => {
    if (!selected) return;
    setError(null);
    const started = await post("/ros-mobile/export", { session_id: selected }, "导出 LeRobot");
    if (!started?.job_id) return;
    setJob(started);
    setReport(null);
    await pollJob(started.job_id);
  }, [post, selected, pollJob]);

  // The restricted contract has its own route so the ordinary training-oriented export
  // can never pick it up by accident; the backend still re-checks the session identity.
  const exportSimulated6d = useCallback(async () => {
    if (!selected) return;
    setError(null);
    const started = await post("/ros-mobile/export/offline-simulated-6d",
      { session_id: selected }, "受限模拟 6D 导出");
    if (!started?.job_id) return;
    setJob(started);
    setReport(null);
    await pollJob(started.job_id);
  }, [post, selected, pollJob]);

  const cancelExport = useCallback(async () => {
    if (!job?.job_id) return;
    pollAbort.current = true;
    const out = await post(`/ros-mobile/export/cancel/${encodeURIComponent(job.job_id)}`, {}, "取消导出");
    if (out) setJob(out);
  }, [job?.job_id, post]);

  const loadEpisodes = useCallback(async () => {
    const repo = repoId.trim();
    if (!repo) {
      setError("请先填写已导出数据集的 repo_id");
      return;
    }
    const data = await get(`/datasets/local/${encodeRepo(repo)}/episodes`, "读取分集与阶段");
    if (!data) return;
    const list = Array.isArray(data.episodes) ? data.episodes : [];
    setEpisodes(list);
    setTimeseries(null);
    setEpisodeIndex(list.length > 0 ? 0 : "");
    setPhaseIndex(-1);
  }, [get, repoId]);

  const currentEpisode = episodeIndex === "" ? null : episodes[episodeIndex] ?? null;
  const phases = useMemo(() => readPhases(currentEpisode), [currentEpisode]);
  const currentPhase: PhaseSlice | null = phases[phaseIndex] ?? null;

  const loadTimeseries = useCallback(async () => {
    if (!currentEpisode || episodeIndex === "") return;
    const data = await get(
      `/datasets/local/${encodeRepo(repoId.trim())}/episodes/${currentEpisode.index ?? episodeIndex}/timeseries`,
      "读取动作/state 曲线",
    );
    if (data) setTimeseries(data);
  }, [currentEpisode, episodeIndex, get, repoId]);

  const camera = useMemo(() => {
    const fromEpisode = Array.isArray(currentEpisode?.cameras) ? currentEpisode.cameras[0] : null;
    if (fromEpisode) return fromEpisode;
    const fromVideos = currentEpisode?.videos && typeof currentEpisode.videos === "object"
      ? Object.keys(currentEpisode.videos)[0]
      : null;
    return fromVideos ?? null;
  }, [currentEpisode]);

  const videoSrc = useMemo(() => {
    if (!currentEpisode || !camera || episodeIndex === "") return null;
    const base = `${baseUrl}/datasets/local/${encodeRepo(repoId.trim())}/episodes/${currentEpisode.index ?? episodeIndex}/video/${encodeURIComponent(camera)}`;
    const from = currentPhase?.videoRanges?.[camera]?.from_s ?? currentPhase?.fromS;
    const to = currentPhase?.videoRanges?.[camera]?.to_s ?? currentPhase?.toS;
    if (typeof from === "number" && typeof to === "number") {
      return `${base}#t=${from.toFixed(3)},${to.toFixed(3)}`;
    }
    return base;
  }, [baseUrl, camera, currentEpisode, currentPhase, episodeIndex, repoId]);

  const exportBlockedReason = !online
    ? "状态未连接"
    : !session.valid
      ? "未取得联合会话"
      : !profileReady
        ? "需要有效 hybrid profile 与六路实测反馈；九维导出不接受补零或缓存来源"
        : !exportAllowed
          ? "后端 capabilities.actions 未允许 export"
          : null;

  return (
    <section className={box} data-testid="hybrid-data-review">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">同步、导出与阶段检查</h2>
        <span className="text-xs text-gray-500">
          {simulated ? "模拟数据源（仅测试）" : "暂存 → 校验 → 原子发布"}
        </span>
      </div>
      <p className="mt-1 text-xs text-gray-400">
        同步与导出复用现有 `/ros-mobile/sync`、`/ros-mobile/export`；九维导出在缺少有效底盘估计或六路实测时由后端拒绝，本页不补 0。模拟 session 另有独立的 `/ros-mobile/export/offline-simulated-6d` 受限入口，普通导出与训练路径仍然拒绝。
      </p>

      {error && (
        <div role="alert" className="mt-2 flex items-start gap-2 rounded border border-red-700 p-2 text-sm text-red-200" data-testid="data-review-error">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="break-all">{error}</span>
          <button className="ml-auto shrink-0 underline" onClick={() => setError(null)}>关闭错误</button>
        </div>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <select value={selected} onChange={(event) => setSelected(event.target.value)} className="min-w-[16rem] rounded border border-gray-700 bg-gray-900 p-1.5 text-sm" aria-label="选择 session">
          <option value="">选择 session</option>
          {sessions.slice().reverse().map((item: any) => (
            <option key={item.session_id} value={item.session_id}>
              {item.session_id} · {item.status}{item.upper?.ready ? " · 已同步" : ""}
            </option>
          ))}
        </select>
        <button className={button} disabled={!online} onClick={loadSessions}><RefreshCw className="mr-1 inline h-3.5 w-3.5" />刷新 session</button>
        <button
          className={button}
          disabled={!selected || !!busy || !syncAllowed}
          title={syncAllowed ? "同步原始数据" : "后端 capabilities.actions 未允许 sync"}
          onClick={sync}
        >
          <Upload className="mr-1 inline h-3.5 w-3.5" />同步原始数据
        </button>
        <button
          className="rounded bg-sky-700 px-3 py-1.5 text-sm disabled:opacity-40"
          disabled={!selected || !!busy || job?.status === "running" || exportBlockedReason !== null}
          title={exportBlockedReason ?? "导出需要后端校验通过"}
          onClick={exportSession}
        >
          <Download className="mr-1 inline h-3.5 w-3.5" />导出 LeRobot
        </button>
        {simulated && (
          <button
            className="rounded bg-emerald-700 px-3 py-1.5 text-sm disabled:opacity-40"
            disabled={!selected || !!busy || job?.status === "running"}
            title="受限 simulated_offline 六维导出；产物仍是集成证据，training_eligible=false"
            data-testid="export-simulated-6d"
            onClick={exportSimulated6d}
          >
            <Download className="mr-1 inline h-3.5 w-3.5" />受限模拟 6D 导出
          </button>
        )}
        {job?.status === "running" && (
          <button className="rounded border border-red-700 px-3 py-1.5 text-sm text-red-200 disabled:opacity-40" disabled={!!busy} onClick={cancelExport}>
            <XCircle className="mr-1 inline h-3.5 w-3.5" />取消导出
          </button>
        )}
        {busy && <span className="flex items-center gap-1 text-xs text-gray-400"><Loader2 className="h-3.5 w-3.5 animate-spin" />{busy}</span>}
      </div>
      {exportBlockedReason && <p className="mt-1 text-xs text-amber-300" data-testid="export-blocked">导出已禁用：{exportBlockedReason}</p>}

      {syncResult && (
        <p className="mt-2 break-all text-xs text-emerald-300" data-testid="sync-result">
          同步 {reasonText(syncResult.status)} · {reasonText(syncResult.path)} · sha256 {String(syncResult.manifest_sha256 ?? "未提供").slice(0, 16)}…
        </p>
      )}
      {job && (
        <p className="mt-1 text-xs text-gray-400">
          导出任务 {job.job_id} · {job.status}{job.error ? <span className="text-red-300"> · {reasonText(job.error)}</span> : null}
        </p>
      )}
      {report && (
        <div className="mt-2 rounded border border-emerald-800 bg-emerald-950/30 p-2 text-xs text-emerald-200" data-testid="export-report">
          repo_id {report.repo_id} · {report.total_episodes} episodes · {report.total_frames} frames · schema {report.export_schema?.kind ?? "未声明"} ({report.export_schema?.dims ?? "?"} 维)
          <div className="mt-1 font-mono break-all">{report.dataset_root}</div>
          <div className="mt-1 text-emerald-300/80">
            阶段切片：
            {(report.episodes ?? []).flatMap((episode: any) =>
              (episode.phases ?? []).map((slice: any) => `${episode.episode_id}/${slice.phase}`)
            ).join(" · ") || "报告未包含阶段"}
          </div>
        </div>
      )}

      <details className="mt-3 rounded border border-gray-800 p-2" open>
        <summary className="cursor-pointer text-sm text-gray-300">按 episode/phase 检查视频与曲线</summary>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            aria-label="数据集 repo_id"
            placeholder="repo_id（导出报告或本机数据集列表中的名称）"
            value={repoId}
            onChange={(event) => setRepoId(event.target.value)}
            className="min-w-[18rem] rounded border border-gray-700 bg-gray-900 p-1.5 text-xs"
          />
          <button className={button} disabled={!online || !repoId.trim()} onClick={loadEpisodes}>读取分集</button>
          <select
            aria-label="选择 episode"
            className="rounded border border-gray-700 bg-gray-900 p-1.5 text-xs"
            value={episodeIndex === "" ? "" : String(episodeIndex)}
            onChange={(event) => {
              const value = event.target.value;
              setEpisodeIndex(value === "" ? "" : Number(value));
              setTimeseries(null);
              setPhaseIndex(-1);
            }}
          >
            <option value="">选择 episode</option>
            {episodes.map((episode: any, index: number) => (
              <option key={episode.index ?? index} value={index}>
                #{episode.index ?? index} · {episode.length ?? "?"} frames · {(episode.phases ?? []).length} phases
              </option>
            ))}
          </select>
          <select
            aria-label="选择阶段"
            className="rounded border border-gray-700 bg-gray-900 p-1.5 text-xs"
            value={String(phaseIndex)}
            onChange={(event) => setPhaseIndex(Number(event.target.value))}
          >
            <option value="-1">整集</option>
            {phases.map((slice, index) => (
              <option key={`${slice.phase}-${index}`} value={index}>
                {slice.phase} · {numberText(slice.fromS, 2)}–{numberText(slice.toS, 2)} s
              </option>
            ))}
          </select>
          <button className={button} disabled={!currentEpisode || !!busy} onClick={loadTimeseries}>读取曲线</button>
        </div>

        {currentEpisode && (
          <p className="mt-2 text-xs text-gray-400">
            episode #{currentEpisode.index ?? episodeIndex} · {currentEpisode.length ?? "?"} frames · 相机 {camera ?? "未声明"} · 阶段 {(currentEpisode.phases ?? []).length}
          </p>
        )}
        {currentEpisode && phases.length === 0 && (
          <p className="mt-1 text-xs text-amber-300" data-testid="phases-missing">
            该 episode 没有 phase 索引；不推断阶段边界，也不把整集当作 observe/grasp。
          </p>
        )}
        {phases.length > 0 && (
          <p className="mt-1 text-xs text-gray-500">
            当前窗口：{phaseIndex < 0 ? "整集" : `${currentPhase?.phase} ${numberText(currentPhase?.fromS, 2)}–${numberText(currentPhase?.toS, 2)} s`}
            · 阶段来源 {currentPhase?.source ?? "phase 索引"}
          </p>
        )}

        {videoSrc ? (
          <div className="mt-2">
            <video key={videoSrc} src={videoSrc} controls preload="metadata" className="aspect-video w-full rounded border border-gray-800 bg-black" data-testid="phase-video" />
            <p className="mt-1 text-xs text-gray-500">
              这是整集 mp4 的 `#t=from,to` 时间片段，不是后端按阶段切出的独立视频文件；倍速/拖动会离开该阶段。
            </p>
          </div>
        ) : (
          <p className="mt-2 text-xs text-gray-500">选择已导出 episode 后显示视频。</p>
        )}

        <PhaseCurves timeseries={timeseries} fromS={phaseIndex < 0 ? null : currentPhase?.fromS ?? null} toS={phaseIndex < 0 ? null : currentPhase?.toS ?? null} />
      </details>
    </section>
  );
}
