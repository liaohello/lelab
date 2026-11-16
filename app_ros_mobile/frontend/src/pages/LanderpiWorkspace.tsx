import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Camera,
  CircleAlert,
  Database,
  Loader2,
  Pause,
  Play,
  RotateCcw,
  Save,
  Square,
  Trash2,
  Video,
} from "lucide-react";
import { Link } from "react-router-dom";
import LanderpiNav from "@/components/landerpi/LanderpiNav";
import StableCameraPreview from "@/components/landerpi/StableCameraPreview";
import {
  CaptureResponse,
  CaptureStatus,
  captureAction,
  captureStatus,
  formatElapsed,
  reasonText,
} from "@/components/landerpi/captureApi";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

const JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"] as const;
const ACTIVE_STATE_MAX_AGE_MS = 5000;
const HISTORY_LEN = 60;

function pill(ok: boolean | null, text: string, testId?: string) {
  const tone = ok === null
    ? "border-slate-700 bg-slate-900 text-slate-300"
    : ok
      ? "border-emerald-700 bg-emerald-950/50 text-emerald-300"
      : "border-red-700 bg-red-950/50 text-red-300";
  return <span data-testid={testId} className={`rounded-full border px-2.5 py-1 text-xs ${tone}`}>{text}</span>;
}

function numeric(value: unknown, digits = 3): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

type HistoryPoint = { target: (number | null)[]; measured: (number | null)[] };

function Sparkline({ history, index }: { history: HistoryPoint[]; index: number }) {
  const width = 150;
  const height = 34;
  const series = (key: "target" | "measured") => {
    const values = history.map((point) => point[key][index]);
    const finite = values.filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    if (finite.length < 2) return null;
    const min = Math.min(...finite);
    const max = Math.max(...finite);
    const span = max - min || 1;
    return values.map((value, i) => {
      if (typeof value !== "number" || !Number.isFinite(value)) return null;
      const x = (i / Math.max(1, HISTORY_LEN - 1)) * width;
      const y = height - 3 - ((value - min) / span) * (height - 6);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).filter(Boolean).join(" ");
  };
  const target = series("target");
  const measured = series("measured");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-9 w-full" preserveAspectRatio="none" aria-hidden>
      {measured && <polyline points={measured} fill="none" stroke="#38bdf8" strokeWidth="1.2" />}
      {target && <polyline points={target} fill="none" stroke="#a78bfa" strokeWidth="1.2" strokeDasharray="3 2" />}
    </svg>
  );
}

function DatasetDialog({ busy, error, onClose, onSubmit }: {
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (payload: Record<string, unknown>) => void;
}) {
  const [name, setName] = useState(() => {
    const now = new Date();
    const stamp = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}-${String(now.getHours()).padStart(2, "0")}${String(now.getMinutes()).padStart(2, "0")}`;
    return `landerpi-${stamp}`;
  });
  const [description, setDescription] = useState("");
  const [fps, setFps] = useState("14");
  const [target, setTarget] = useState("10");
  const [mode, setMode] = useState<"new" | "append">("new");
  const [repoId, setRepoId] = useState("lelab/");
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-label="开始数据集">
      <div className="w-full max-w-lg rounded-xl border border-slate-700 bg-slate-900 p-5">
        <h2 className="text-lg font-semibold">开始数据集</h2>
        <p className="mt-1 text-xs text-slate-400">建立一次采集任务；此时不录制任何集，页面显示未录制。</p>
        <div className="mt-4 space-y-3">
          <label className="block text-xs text-slate-400">数据集名称
            <input data-testid="dataset-name" value={name} onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" />
          </label>
          <label className="block text-xs text-slate-400">任务描述（进入每集 task 字段）
            <input data-testid="dataset-task" value={description} onChange={(e) => setDescription(e.target.value)}
              placeholder="例如：抓取方块并放入托盘"
              className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block text-xs text-slate-400">FPS
              <input data-testid="dataset-fps" type="number" min={1} max={120} value={fps} onChange={(e) => setFps(e.target.value)}
                className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" />
            </label>
            <label className="block text-xs text-slate-400">目标集数
              <input data-testid="dataset-target" type="number" min={1} value={target} onChange={(e) => setTarget(e.target.value)}
                className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" />
            </label>
          </div>
          <div className="flex gap-3 text-sm">
            <label className="flex items-center gap-2">
              <input type="radio" name="dataset-mode" checked={mode === "new"} onChange={() => setMode("new")} />新建数据集
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" name="dataset-mode" checked={mode === "append"} onChange={() => setMode("append")} />追加现有数据集
            </label>
          </div>
          {mode === "append" && (
            <label className="block text-xs text-slate-400">现有数据集 repo_id
              <input data-testid="dataset-repo" value={repoId} onChange={(e) => setRepoId(e.target.value)}
                className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100" />
              <span className="mt-1 block text-[11px] text-slate-500">追加前检查 robot_type、特征、关节顺序、单位、相机与数据身份；FPS 在导出时按实际行率复核。</span>
            </label>
          )}
          {error && <p className="rounded border border-red-800 bg-red-950/40 p-2 text-xs text-red-200">{error}</p>}
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} disabled={busy}
            className="rounded-lg border border-slate-700 px-4 py-2 text-sm text-slate-300 disabled:opacity-40">取消</button>
          <button type="button" data-testid="dataset-submit" disabled={busy || !name.trim() || !description.trim()}
            onClick={() => onSubmit({
              name: name.trim(), task: description.trim(),
              fps: Number(fps), target_episodes: Number(target) || null,
              mode, ...(mode === "append" ? { repo_id: repoId.trim() } : {}),
            })}
            className="flex items-center gap-2 rounded-lg bg-sky-500 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-40">
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}开始数据集
          </button>
        </div>
      </div>
    </div>
  );
}

export default function LanderpiWorkspace() {
  const { baseUrl, fetchWithHeaders, clientId } = useApi();
  const [capture, setCapture] = useState<CaptureStatus | null>(null);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ raw: unknown; action?: string } | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [resultLabel, setResultLabel] = useState<"unknown" | "success" | "failure">("unknown");
  const [displayElapsed, setDisplayElapsed] = useState(0);
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const refreshing = useRef<Promise<void> | null>(null);
  const lastElapsedRef = useRef<{ at: number; value: number } | null>(null);

  const request = useCallback(<T,>(path: string, body?: unknown, method = body === undefined ? "GET" : "POST") =>
    apiRequest<T>(baseUrl, fetchWithHeaders, path, { method, body }), [baseUrl, fetchWithHeaders]);

  const refresh = useCallback((): Promise<void> => {
    if (refreshing.current) return refreshing.current.then(() => refresh());
    const task = (async () => {
      try {
        const next = await captureStatus(baseUrl, fetchWithHeaders);
        setCapture(next);
        setLastOk(Date.now());
        if (next.recording_active && next.elapsed_s != null) {
          lastElapsedRef.current = { at: Date.now(), value: next.elapsed_s };
          setDisplayElapsed(next.elapsed_s);
        }
        const control = next.control || {};
        const targets = control.hardware_preview?.targets ?? [];
        const targetMap = new Map<string, any>(targets.map((item: any) => [item.joint, item]));
        const samples = control.feedback?.joint_samples ?? {};
        setHistory((old) => [...old, {
          target: JOINTS.map((name) => {
            const value = targetMap.get(name)?.target_rad;
            return typeof value === "number" ? value : null;
          }),
          measured: JOINTS.map((name) => {
            const value = samples[name]?.position_rad;
            return typeof value === "number" ? value : null;
          }),
        }].slice(-HISTORY_LEN));
      } catch (exc) {
        setLastOk(null);
        setError({ raw: (exc as any)?.detail ?? (exc as any)?.message ?? exc });
      }
    })();
    refreshing.current = task.finally(() => { refreshing.current = null; });
    return refreshing.current;
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    void refresh();
    const poll = window.setInterval(() => void refresh(), 1000);
    return () => window.clearInterval(poll);
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const base = lastElapsedRef.current;
      if (base && capture?.recording_active) {
        setDisplayElapsed(base.value + (Date.now() - base.at) / 1000);
      }
    }, 250);
    return () => window.clearInterval(timer);
  }, [capture?.recording_active]);

  const control = capture?.control ?? {};
  const sessionId = control.session_id ?? null;
  const sessionEpoch = control.session_epoch ?? null;
  const sessionReady = Boolean(sessionId) && control.state === "ready";

  useEffect(() => {
    if (!sessionId || typeof sessionEpoch !== "number" || !sessionReady) return;
    const identity = {
      session_id: sessionId, session_epoch: sessionEpoch,
      calibration_bundle_hash: control.calibration_bundle_hash ?? null,
    };
    let pending = false;
    const renew = async () => {
      if (pending) return;
      pending = true;
      try {
        await request("/ros-mobile/hybrid/heartbeat", identity);
      } catch (exc) {
        setError({ raw: (exc as any)?.detail ?? (exc as any)?.message ?? exc, action: "heartbeat" });
      } finally {
        pending = false;
      }
    };
    void renew();
    const timer = window.setInterval(renew, 5000);
    return () => window.clearInterval(timer);
  }, [request, sessionId, sessionEpoch, sessionReady, control.calibration_bundle_hash]);

  const applyResponse = useCallback((response: CaptureResponse) => {
    if (response.capture) setCapture(response.capture);
    if (response.state === "failed") {
      setError({ raw: `${response.error?.reason ?? "unknown"}: ${response.error?.detail ?? ""}`, action: response.action });
    } else {
      setError(null);
    }
  }, []);

  const run = useCallback(async (label: string, action: string, payload: Record<string, unknown> = {}) => {
    setBusy(label);
    try {
      const response = await captureAction(baseUrl, fetchWithHeaders, action, payload);
      applyResponse(response);
      return response;
    } catch (exc) {
      const aborted = (exc as any)?.name === "AbortError";
      setError({
        raw: aborted ? "request_timeout: 请求超时；服务端状态已刷新，按页面当前步骤继续，不要重复点击"
          : ((exc as any)?.detail ?? (exc as any)?.message ?? exc),
        action,
      });
      await refresh();
      return null;
    } finally {
      setBusy(null);
    }
  }, [baseUrl, fetchWithHeaders, applyResponse, refresh]);

  const controlAction = useCallback(async (label: string, action: string, extra: Record<string, unknown> = {}) => {
    setBusy(label);
    setError(null);
    try {
      await request("/ros-mobile/hybrid/control", {
        action,
        ...(sessionId ? { session_id: sessionId, session_epoch: sessionEpoch } : {}),
        ...extra,
      });
      await refresh();
    } catch (exc) {
      setError({ raw: (exc as any)?.detail ?? (exc as any)?.message ?? exc, action });
      await refresh();
    } finally {
      setBusy(null);
    }
  }, [request, sessionId, sessionEpoch, refresh]);

  const online = lastOk !== null && Date.now() - lastOk <= ACTIVE_STATE_MAX_AGE_MS;
  const stage = online ? (capture?.operator_stage ?? "offline") : "offline";
  const recordingActive = capture?.recording_active === true;
  const task = capture?.task ?? null;
  const attempt = capture?.attempt ?? null;
  const video = control.video ?? {};
  const feedback = control.feedback ?? {};
  const feedbackOk = feedback.valid === true && feedback.trusted === true;
  const cameraOk = video.available === true && video.stale === false
    && typeof video.age_s === "number" && video.age_s <= 1;
  const outputsOn = control.outputs_enabled === true;
  const estopLatched = control.estop_latched === true || control.state === "estop";

  const errorInfo = useMemo(() => {
    if (error) return reasonText(error.raw);
    if (stage === "interrupted" && attempt?.interrupt_reason) {
      return reasonText(attempt.interrupt_reason);
    }
    if (stage === "fault") return reasonText(control.primary_error ?? control.last_error);
    if (stage === "pipeline_failed" && task?.pipeline?.error) return reasonText(task.pipeline.error);
    if (capture?.control_error) return reasonText(capture.control_error.reason);
    return null;
  }, [error, stage, attempt, control, task, capture]);

  const primary = useMemo((): { label: string; run: () => void; disabled?: boolean; hint: string; hide?: boolean } => {
    switch (stage) {
      case "offline":
        return { label: "等待车端状态", run: () => {}, disabled: true, hint: "车端状态不可确认；不要根据旧数值操作。" };
      case "no_control":
        return { label: "获取控制", run: () => void run("获取控制", "acquire", { owner: clientId }), hint: "连接主臂、检查标定与反馈、完成无输出预览和对齐；不会自动运动。" };
      case "estop":
        return {
          label: "清除急停", hint: "清除后不会自动恢复运动；需要重新获取控制并显式开始。",
          run: () => void (async () => {
            setBusy("清除急停");
            try {
              await request("/ros-mobile/hybrid/clear-estop", {});
              await refresh();
            } catch (exc) {
              setError({ raw: (exc as any)?.detail ?? exc, action: "clear_estop" });
            } finally { setBusy(null); }
          })(),
        };
      case "fault":
        return {
          label: "释放并重建控制", hint: "旧集已停止并保留证据；重建后需显式开始新集。",
          run: () => void (async () => {
            setBusy("释放并重建控制");
            try {
              if (sessionId) {
                await request("/ros-mobile/hybrid/session", {
                  action: "release", reason: "operator fault recovery",
                  session_id: sessionId, session_epoch: sessionEpoch,
                }).catch(() => {});
              }
              const response = await captureAction(baseUrl, fetchWithHeaders, "acquire", { owner: clientId });
              applyResponse(response);
            } catch (exc) {
              setError({ raw: (exc as any)?.detail ?? exc, action: "recover" });
            } finally { setBusy(null); await refresh(); }
          })(),
        };
      case "control_ready":
        return { label: "开始数据集", run: () => { setDialogError(null); setDialogOpen(true); }, hint: "设置名称、任务描述、FPS 与目标集数；此时不录制。" };
      case "dataset_idle":
        return {
          label: "REC 开始本集", disabled: !cameraOk || !feedbackOk,
          run: () => void run("开始本集", "start_episode"),
          hint: "点击后将启用跟随：后端自动完成新鲜度检查、对齐、待命与接合，车端确认后才显示 REC。",
        };
      case "starting":
        return { label: "正在开始本集…", run: () => {}, disabled: true, hint: "等待车端确认本集开始；失败会停止输出并给出原因。" };
      case "recording":
        return { label: "结束本集", run: () => void run("结束本集", "end_episode", { result: resultLabel }), hint: "先暂停输出，再封闭本集时间窗口，进入待审核。" };
      case "paused":
        return { label: "恢复跟随", run: () => void run("恢复跟随", "resume_follow"), hint: "输出当前关闭；恢复会重新对齐并显式接合，不会自动续动。" };
      case "reset":
        return { label: "准备下一集", run: () => void run("准备下一集", "next_episode"), hint: "复位完成后确认；下一次 REC 重新建立本集。复位数据不进入训练样本。" };
      case "interrupted":
        return { label: "收尾旧采集", run: () => void run("收尾旧采集", "recover"), hint: "关闭车端旧录包并保留原始证据；之后可开始新数据集。" };
      case "review":
        return { label: "", run: () => {}, disabled: true, hide: true, hint: "用下方保存 / 丢弃 / 重录处理本集。" };
      case "pipeline":
        return { label: "数据处理中…", run: () => {}, disabled: true, hint: "停止控制不等待同步与导出；进度见下方。" };
      case "pipeline_failed":
        return { label: "重试同步导出", run: () => void run("重试同步导出", "retry_pipeline"), hint: "原始数据已保留；也可到数据页手动处理。" };
      case "done":
        return {
          label: "开始新数据集", hint: "本数据集已导出；可回看或开始新的采集。",
          run: () => void (async () => {
            if (!sessionReady) {
              const response = await run("获取控制", "acquire", { owner: clientId });
              if (!response || response.state !== "completed") return;
            }
            setDialogError(null);
            setDialogOpen(true);
          })(),
        };
      default:
        return { label: "等待状态", run: () => {}, disabled: true, hint: "" };
    }
  }, [stage, run, clientId, cameraOk, feedbackOk, resultLabel, sessionReady, sessionId, sessionEpoch, request, refresh, baseUrl, fetchWithHeaders, applyResponse]);

  const reviewAttempt = stage === "review" ? attempt : null;
  const phaseLabel = attempt?.phase === "grasp" ? "抓取" : attempt?.phase === "observe" ? "观察" : "—";
  const cameraState = !online ? "不可确认" : !sessionId ? "只读检查" : cameraOk ? "实时" : "画面过期";

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <LanderpiNav />
      <main className="mx-auto max-w-[1440px] space-y-4 px-4 py-5 sm:px-6">
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-800 bg-slate-900/70 p-3" data-testid="capture-status-strip">
          <span className="mr-2 text-sm font-semibold">状态</span>
          {pill(online, online ? "车端在线" : "车端不可确认", "pill-car")}
          {pill(online ? cameraOk : null, `相机 ${cameraState}`, "pill-camera")}
          {pill(online ? feedbackOk : null, feedbackOk ? "六路反馈正常" : "六路反馈异常", "pill-feedback")}
          {pill(online ? !outputsOn : null, outputsOn ? "输出开启" : "输出关闭", "pill-outputs")}
          {pill(online ? !estopLatched : null, estopLatched ? "急停锁存" : "急停未锁存", "pill-estop")}
          <span className="ml-auto text-xs text-slate-500">
            {task ? `${task.name} · 已保存 ${task.saved_count}${task.target_episodes ? ` / ${task.target_episodes}` : ""} 集` : "未开始数据集"}
          </span>
        </div>

        {errorInfo && (
          <div role="alert" data-testid="capture-error-card" className="rounded-xl border border-red-800 bg-red-950/40 p-3 text-sm text-red-100">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="font-medium">{errorInfo.title}</p>
                <p className="mt-1 text-xs text-red-200/80">
                  {outputsOn ? "注意：输出仍开启，请先暂停。" : "当前输出已停止。"}{" "}{errorInfo.next}
                </p>
                <details className="mt-2 text-xs">
                  <summary className="cursor-pointer">查看详情</summary>
                  <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all">{errorInfo.detail}</pre>
                </details>
              </div>
              {error && <button type="button" onClick={() => setError(null)} className="shrink-0 text-xs underline">关闭</button>}
            </div>
          </div>
        )}

        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(360px,0.8fr)]">
          <section className="overflow-hidden rounded-xl border border-slate-800 bg-black">
            <div className="flex items-center justify-between gap-2 border-b border-slate-800 bg-slate-900/80 px-4 py-3">
              <div className="flex items-center gap-2">
                <Camera className="h-4 w-4 text-sky-400" />
                <h1 className="font-semibold">腕部相机</h1>
                <span className="text-xs text-slate-500">{recordingActive ? "本集画面" : "只读检查 · 未录制"}</span>
              </div>
              <span className={`text-xs ${cameraOk ? "text-emerald-300" : "text-amber-300"}`} data-testid="camera-state">
                {cameraState}{typeof video.age_s === "number" ? ` · ${numeric(video.age_s, 2)}s` : ""}
              </span>
            </div>
            <div className="relative h-[clamp(260px,50vh,440px)] w-full bg-slate-950">
              {sessionId && online ? (
                <StableCameraPreview active={Boolean(sessionId) && online} sourceFresh={cameraOk} />
              ) : (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-center text-sm text-slate-400">
                  <Camera className="h-8 w-8 text-slate-600" />
                  {online ? "获取控制后显示实时相机；此处不是录制画面" : "车端状态不可确认，暂不显示画面"}
                </div>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-3 border-t border-slate-800 bg-slate-900/80 px-4 py-2 text-xs text-slate-400">
              <span>话题 {video.topic ?? "—"}</span>
              <span>帧 {video.preview_seq ?? video.frame_seq ?? "—"}</span>
            </div>
          </section>

          <section className="flex flex-col rounded-xl border border-sky-800/70 bg-slate-900/90 p-4 shadow-lg shadow-sky-950/20">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="text-xs uppercase tracking-wider text-sky-400">当前步骤</p>
                <h2 className="mt-1 text-lg font-semibold" data-testid="capture-stage">{stage}</h2>
              </div>
              {recordingActive ? (
                <span data-testid="rec-banner" className="flex items-center gap-2 rounded-lg border border-red-600 bg-red-950/60 px-3 py-1.5 text-sm font-bold text-red-300">
                  <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-red-500" />REC {formatElapsed(displayElapsed)}
                </span>
              ) : (
                <span data-testid="not-recording" className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm text-slate-400">未录制</span>
              )}
            </div>

            {task && (
              <p className="mt-2 text-xs text-slate-400">
                {task.name} · {task.description} · 已保存 {task.saved_count} 集
                {task.mode === "append" && task.append_to ? ` · 追加到 ${task.append_to.repo_id}` : ""}
              </p>
            )}
            <p className="mt-2 text-sm text-slate-400">{primary.hint}</p>

            {stage === "paused" && (
              <p className="mt-2 rounded border border-amber-700/70 bg-amber-950/30 px-2 py-1.5 text-xs text-amber-200">
                本集已暂停 · 未录制。{attempt?.pause_reason ? `原因：${attempt.pause_reason}` : ""}
              </p>
            )}
            {stage === "reset" && (
              <p className="mt-2 rounded border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-300">
                复位中 · 未录制。摆放物体并调整主从臂姿态；需要遥操作复位时用下方入口，复位数据不进训练集。
              </p>
            )}

            {(stage === "recording" || stage === "paused") && attempt && (
              <div className="mt-3 rounded-lg border border-slate-700 bg-slate-950/70 p-3 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-slate-400">本集阶段</span>
                  <span className="font-medium text-slate-100" data-testid="attempt-phase">{phaseLabel}</span>
                </div>
                {stage === "recording" && attempt.phase === "observe" && (
                  <button type="button" data-testid="mark-grasp" disabled={!!busy}
                    onClick={() => void run("标记抓取", "mark_grasp")}
                    className="mt-2 w-full rounded-lg border border-violet-600 bg-violet-950/40 px-3 py-2 text-sm text-violet-100 disabled:opacity-40">
                    标记抓取（observe → grasp）
                  </button>
                )}
                {stage === "recording" && (
                  <div className="mt-2 flex items-center gap-2">
                    <label className="text-slate-400">本集结果
                      <select data-testid="result-label" value={resultLabel}
                        onChange={(e) => setResultLabel(e.target.value as any)}
                        className="ml-2 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-slate-100">
                        <option value="unknown">unknown（默认）</option>
                        <option value="success">success</option>
                        <option value="failure">failure</option>
                      </select>
                    </label>
                  </div>
                )}
                {stage === "paused" && (
                  <button type="button" disabled={!!busy} onClick={() => void run("结束本集", "end_episode", { result: resultLabel })}
                    className="mt-2 w-full rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-200 disabled:opacity-40">
                    结束本集
                  </button>
                )}
              </div>
            )}

            {reviewAttempt && (
              <div className="mt-3 rounded-lg border border-emerald-800/70 bg-emerald-950/20 p-3" data-testid="episode-review">
                <p className="text-sm font-medium text-emerald-100">本集待审核</p>
                <p className="mt-1 text-xs text-emerald-300/70">
                  时长 {formatElapsed(capture?.elapsed_s ?? null)} · 阶段 {reviewAttempt.phase === "grasp" ? "观察+抓取" : "仅观察"} · 结果 {reviewAttempt.result}
                </p>
                <p className="mt-1 text-[11px] text-slate-500">原始包已保留；保存只增加一次计数，丢弃不进训练集，重录创建新尝试。</p>
                <div className="mt-3 grid grid-cols-3 gap-2">
                  <button type="button" data-testid="review-save" disabled={!!busy}
                    onClick={() => void run("保存本集", "review", { attempt_id: reviewAttempt.attempt_id, decision: "save" })}
                    className="flex items-center justify-center gap-1 rounded-lg bg-emerald-600 px-2 py-2 text-xs font-semibold text-slate-950 disabled:opacity-40">
                    <Save className="h-3.5 w-3.5" />保存
                  </button>
                  <button type="button" data-testid="review-discard" disabled={!!busy}
                    onClick={() => void run("丢弃本集", "review", { attempt_id: reviewAttempt.attempt_id, decision: "discard" })}
                    className="flex items-center justify-center gap-1 rounded-lg border border-red-700 px-2 py-2 text-xs text-red-200 disabled:opacity-40">
                    <Trash2 className="h-3.5 w-3.5" />丢弃
                  </button>
                  <button type="button" data-testid="review-rerecord" disabled={!!busy}
                    onClick={() => void run("重录本集", "review", { attempt_id: reviewAttempt.attempt_id, decision: "rerecord" })}
                    className="flex items-center justify-center gap-1 rounded-lg border border-amber-700 px-2 py-2 text-xs text-amber-200 disabled:opacity-40">
                    <RotateCcw className="h-3.5 w-3.5" />重录
                  </button>
                </div>
              </div>
            )}

            {stage === "pipeline" && task && (
              <div className="mt-3 rounded-lg border border-slate-700 bg-slate-950/70 p-3 text-xs text-slate-300" data-testid="pipeline-progress">
                <p>原始录包收尾 → 校验同步 → LeRobot 导出</p>
                <p className="mt-1 text-slate-400">当前：{task.pipeline?.stage ?? "stopping"}{task.pipeline?.job_id ? ` · ${task.pipeline.job_id}` : ""}</p>
                <Link to="/landerpi/data" className="mt-1 inline-block text-sky-300 underline">到数据页查看</Link>
              </div>
            )}
            {stage === "done" && task && (
              <div className="mt-3 rounded-lg border border-emerald-800/70 bg-emerald-950/20 p-3 text-xs text-emerald-100" data-testid="pipeline-done">
                <p>数据集已导出：{task.pipeline?.repo_id ?? "—"}</p>
                <p className="mt-1 text-emerald-300/70">{task.pipeline?.total_episodes ?? "—"} 集 · {task.pipeline?.total_frames ?? "—"} 帧</p>
                <Link to="/landerpi/data" className="mt-1 inline-block text-sky-300 underline">回看数据</Link>
              </div>
            )}

            <div className="mt-auto grid gap-2 pt-4">
              {capture?.blocking && capture.blocking.length > 0 && stage === "dataset_idle" && (
                <p className="rounded border border-amber-700/70 bg-amber-950/30 px-2 py-1.5 text-xs text-amber-200" data-testid="rec-blocking">
                  {capture.blocking.map((b) => b.message).join("；")}
                </p>
              )}
              <button type="button" data-testid="capture-primary"
                hidden={primary.hide}
                disabled={!!busy || primary.disabled}
                onClick={primary.run}
                className={`flex items-center justify-center gap-2 rounded-lg px-4 py-3 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${
                  stage === "recording" ? "bg-red-600 text-white" : "bg-sky-500 text-slate-950"}`}>
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : stage === "recording" ? <Square className="h-4 w-4" /> : <Play className="h-4 w-4" />}
                {busy ?? primary.label}
              </button>
              {stage === "reset" && (
                <button type="button" disabled={!!busy || !sessionReady} onClick={() => void run("遥操作复位", "resume_follow")}
                  className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 disabled:opacity-40">
                  遥操作复位（不录制）
                </button>
              )}
              <div className="grid grid-cols-2 gap-2">
                <button type="button" data-testid="pause-button" disabled={!sessionId || !!busy || !online}
                  onClick={() => void controlAction("暂停输出", "pause_all")}
                  className="flex items-center justify-center gap-1.5 rounded-lg border border-amber-700/80 px-3 py-2 text-xs text-amber-200 disabled:opacity-40">
                  <Pause className="h-3.5 w-3.5" />暂停输出
                </button>
                <button type="button" data-testid="estop-button" disabled={!!busy}
                  onClick={() => void controlAction("软件急停", "estop", { reason: "operator software stop" })}
                  className="flex items-center justify-center gap-1.5 rounded-lg bg-red-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">
                  <CircleAlert className="h-3.5 w-3.5" />软件急停
                </button>
              </div>
              {task && task.stage === "open" && ["dataset_idle", "reset"].includes(stage) && (
                <button type="button" data-testid="close-dataset" disabled={!!busy || !sessionReady}
                  onClick={() => void run("结束数据集", "close_dataset")}
                  className="rounded-lg border border-slate-700 px-3 py-2 text-xs text-slate-300 disabled:opacity-40">
                  结束数据集（停止输出并收尾、同步、导出）
                </button>
              )}
            </div>
          </section>
        </div>

        <section className="rounded-xl border border-slate-800 bg-slate-900/65 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="font-semibold">六轴：目标 / 实测 / 主从曲线</h2>
              <p className="mt-1 text-xs text-slate-500">目标来自主臂映射，实测来自唯一舵机 owner；没有实测值时不补零。曲线为页面采样，仅用于观察。</p>
            </div>
            <span className={`text-xs ${feedbackOk ? "text-emerald-300" : "text-amber-300"}`}>
              {feedbackOk ? "六路反馈新鲜" : "反馈未满足采集条件"}
            </span>
          </div>
          <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {JOINTS.map((name, index) => {
              const sample = feedback.joint_samples?.[name];
              const targets = control.hardware_preview?.targets ?? [];
              const target = targets.find((item: any) => item.joint === name);
              const measured = sample?.position_rad;
              const desired = target?.target_rad;
              const err = typeof desired === "number" && typeof measured === "number" ? Math.abs(desired - measured) : null;
              return (
                <div key={name} className="rounded-lg border border-slate-800 bg-slate-950/70 p-3">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-sm text-slate-200">{name}</span>
                    <span className={`text-xs ${sample?.valid && (sample?.age_s ?? 1) < 0.5 ? "text-emerald-300" : "text-amber-300"}`}>
                      {numeric(sample?.age_s, 2)}s
                    </span>
                  </div>
                  <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                    <div><p className="text-slate-600">目标</p><p className="mt-1 font-mono text-sky-300">{numeric(desired)}</p></div>
                    <div><p className="text-slate-600">实测</p><p className="mt-1 font-mono text-slate-100">{numeric(measured)}</p></div>
                    <div><p className="text-slate-600">误差</p><p className={`mt-1 font-mono ${err !== null && err < 0.08 ? "text-emerald-300" : "text-amber-300"}`}>{numeric(err)}</p></div>
                  </div>
                  <Sparkline history={history} index={index} />
                </div>
              );
            })}
          </div>
        </section>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Link to="/landerpi/calibration" className="rounded border border-slate-700 px-3 py-2 text-slate-300 hover:bg-slate-800">标定 / 映射 / 范围（高级设置）</Link>
          <Link to="/landerpi/data" className="rounded border border-slate-700 px-3 py-2 text-slate-300 hover:bg-slate-800"><Database className="mr-1 inline h-3 w-3" />数据处理</Link>
          <Link to="/landerpi/diagnostics" className="rounded border border-slate-700 px-3 py-2 text-slate-400 hover:bg-slate-800"><Video className="mr-1 inline h-3 w-3" />诊断与旧入口</Link>
          <span className="text-slate-600">录制期间请勿修改影响数据含义的配置；raw 诊断在诊断页。</span>
        </div>
      </main>

      {dialogOpen && (
        <DatasetDialog busy={busy === "开始数据集"} error={dialogError}
          onClose={() => setDialogOpen(false)}
          onSubmit={(payload) => void (async () => {
            setDialogError(null);
            const response = await run("开始数据集", "open_dataset", payload);
            if (response && response.state === "completed") {
              setDialogOpen(false);
            } else if (response?.state === "failed") {
              setDialogError(`${response.error?.reason ?? ""}: ${response.error?.detail ?? ""}`);
            }
          })()} />
      )}
    </div>
  );
}
