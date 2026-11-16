import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { TrainingStatus, TrainingRunState } from "../types";
import {
  Activity,
  AlertTriangle,
  CheckCircle,
  Clock,
  Gauge,
  LineChart as LineChartIcon,
  Scale,
  Sigma,
} from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import {
  JobMonitorInfo,
  MetricEvent,
  getJobMetricsEvents,
} from "@/lib/jobsApi";
import {
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface MonitoringStatsProps {
  jobId: string;
  trainingStatus: TrainingStatus;
  getProgressPercentage: () => number;
  formatTime: (seconds: number) => string;
  monitor?: JobMonitorInfo | null;
}

interface LossPoint {
  step: number;
  attempt?: string | null;
  eventId: number;
  trainLoss?: number;
  valLoss?: number;
}

interface ValuePoint {
  step: number;
  attempt?: string | null;
  eventId: number;
  value: number;
}

interface WeightPoint {
  step: number;
  attempt?: string | null;
  eventId: number;
  weightNorm?: number;
  trainableWeightNorm?: number;
}

const PLOT_POINT_CAP = 1500;
const PAGE_SIZE = 1000;
const MAX_PAGES = 50;
const ACTIVE_STATES: TrainingRunState[] = ["running", "pause_requested", "resuming"];

const STATE_LABEL: Record<TrainingRunState, string> = {
  running: "运行中",
  pause_requested: "暂停请求中",
  paused: "已暂停",
  pause_failed: "暂停失败",
  resuming: "续训启动中",
  done: "已完成",
  failed: "失败",
  interrupted: "已中断",
};

const STATE_CLASS: Record<TrainingRunState, string> = {
  running: "bg-emerald-900/40 text-emerald-200 border-emerald-700",
  pause_requested: "bg-amber-900/40 text-amber-200 border-amber-700",
  paused: "bg-sky-900/40 text-sky-200 border-sky-700",
  pause_failed: "bg-red-900/40 text-red-200 border-red-700",
  resuming: "bg-amber-900/40 text-amber-200 border-amber-700",
  done: "bg-slate-700 text-slate-200 border-slate-600",
  failed: "bg-red-900/40 text-red-200 border-red-700",
  interrupted: "bg-orange-900/40 text-orange-200 border-orange-700",
};

const numberOrUndefined = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const formatNumber = (value: number | undefined | null, digits = 4): string =>
  value == null || !Number.isFinite(value) ? "—" : value.toFixed(digits);

const formatScientific = (value: number | undefined | null): string =>
  value == null || !Number.isFinite(value) ? "—" : value.toExponential(3);

function downsample<T>(points: T[], cap = PLOT_POINT_CAP): T[] {
  if (points.length <= cap) return points;
  const stride = Math.ceil(points.length / cap);
  const sampled = points.filter((_, index) => index % stride === 0);
  const last = points[points.length - 1];
  if (sampled[sampled.length - 1] !== last) sampled.push(last);
  return sampled;
}

const chartTooltip = (color: string) => ({
  contentStyle: {
    background: "#1e293b",
    border: "1px solid #475569",
    borderRadius: 8,
  },
  labelStyle: { color: "#cbd5e1" },
  itemStyle: { color },
});

const EmptyHint: React.FC<{ children: React.ReactNode; error?: boolean }> = ({
  children,
  error,
}) => (
  <div
    className={`flex h-full items-center justify-center text-sm px-6 text-center ${
      error ? "text-red-300" : "text-slate-500"
    }`}
  >
    {children}
  </div>
);

const MonitoringStats: React.FC<MonitoringStatsProps> = ({
  jobId,
  trainingStatus,
  getProgressPercentage,
  formatTime,
  monitor,
}) => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [events, setEvents] = useState<MetricEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const cursorRef = useRef(0);
  const inFlightRef = useRef(false);
  const activeRef = useRef(false);
  const stateRef = useRef(trainingStatus.state);
  stateRef.current = trainingStatus.state;
  activeRef.current = ACTIVE_STATES.includes(trainingStatus.state);

  const mergeEvents = useCallback((incoming: MetricEvent[]) => {
    if (incoming.length === 0) return;
    setEvents((prev) => {
      const seen = new Set(prev.map((event) => event.event_id));
      const merged = [...prev];
      for (const event of incoming) {
        if (!seen.has(event.event_id)) merged.push(event);
      }
      merged.sort((a, b) => a.event_id - b.event_id);
      return merged;
    });
  }, []);

  // Full (paged) seed on mount, refresh, job switch or manual retry. This is
  // what restores the complete history after a browser reload, disconnect or
  // LeLab restart; the event cursor keeps the live poll incremental.
  useEffect(() => {
    let cancelled = false;
    setEvents([]);
    cursorRef.current = 0;
    setLoading(true);
    setHistoryError(null);
    const load = async () => {
      let after = 0;
      for (let page = 0; page < MAX_PAGES; page += 1) {
        const result = await getJobMetricsEvents(
          baseUrl,
          fetchWithHeaders,
          jobId,
          {
            after,
            limit: PAGE_SIZE,
            kinds: ["train", "validation", "weight_norm", "attempt_started", "pause"],
          },
        );
        if (cancelled) return;
        mergeEvents(result.events);
        after = result.next_cursor;
        cursorRef.current = after;
        if (!result.has_more) break;
      }
      if (!cancelled) setLoading(false);
    };
    load().catch((error) => {
      if (cancelled) return;
      setHistoryError(error instanceof Error ? error.message : String(error));
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [baseUrl, fetchWithHeaders, jobId, reloadKey, mergeEvents]);

  const pollOnce = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const result = await getJobMetricsEvents(
        baseUrl,
        fetchWithHeaders,
        jobId,
        {
          after: cursorRef.current,
          limit: PAGE_SIZE,
          kinds: ["train", "validation", "weight_norm", "attempt_started", "pause"],
        },
      );
      mergeEvents(result.events);
      cursorRef.current = result.next_cursor;
      setHistoryError(null);
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : String(error));
    } finally {
      inFlightRef.current = false;
    }
  }, [baseUrl, fetchWithHeaders, jobId, mergeEvents]);

  // Live incremental poll while the run is active. When it pauses/finishes,
  // one final fetch catches the last events (including the pause marker).
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    if (activeRef.current) {
      timer = setInterval(() => {
        void pollOnce();
      }, 1000);
    } else {
      void pollOnce();
    }
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [pollOnce, trainingStatus.state]);

  const { trainEvents, validationEvents, weightEvents, boundaries } = useMemo(() => {
    const train: MetricEvent[] = [];
    const validation: MetricEvent[] = [];
    const weight: MetricEvent[] = [];
    const boundaryMap = new Map<string, { step: number; label: string }>();
    const attemptNumbers = new Map<string, number>();
    let attemptIndex = 0;
    for (const event of events) {
      if (event.kind === "train") train.push(event);
      else if (event.kind === "validation") validation.push(event);
      else if (event.kind === "weight_norm") weight.push(event);
      else if (event.kind === "attempt_started") {
        attemptIndex += 1;
        if (event.attempt_id) attemptNumbers.set(event.attempt_id, attemptIndex);
        const step = event.step ?? 0;
        if (step > 0) {
          boundaryMap.set(`resume-${event.attempt_id}`, {
            step,
            label: `续训 #${attemptIndex}`,
          });
        }
      } else if (event.kind === "pause" && event.data?.state === "saved") {
        const step = event.step ?? 0;
        boundaryMap.set(`pause-${event.event_id}`, {
          step,
          label: "暂停",
        });
      }
    }
    return {
      trainEvents: train,
      validationEvents: validation,
      weightEvents: weight,
      boundaries: Array.from(boundaryMap.values()),
    };
  }, [events]);

  const {
    lossData,
    gradData,
    weightData,
    lrData,
    groupNames,
    lossRawCount,
    gradRawCount,
    weightRawCount,
  } = useMemo(() => {
    // Train and validation arrive at different cadences but their points are
    // merged per (attempt, step) so the shared step axis has one tick per step
    // and both series can be compared directly.
    const lossByStep = new Map<string, LossPoint>();
    const weightByStep = new Map<string, WeightPoint>();
    const grad: ValuePoint[] = [];
    const lr: ValuePoint[] = [];
    const groups = new Set<string>();
    const lossKey = (event: MetricEvent) => `${event.attempt_id ?? "run"}:${event.step ?? 0}`;
    for (const event of trainEvents) {
      const value = numberOrUndefined(event.data.loss);
      if (value != null) {
        const key = lossKey(event);
        const existing = lossByStep.get(key);
        lossByStep.set(key, {
          step: event.step ?? 0,
          eventId: existing ? Math.min(existing.eventId, event.event_id) : event.event_id,
          attempt: event.attempt_id,
          trainLoss: value,
          valLoss: existing?.valLoss,
        });
      }
      const gradValue = numberOrUndefined(event.data.grad_norm);
      if (gradValue != null) {
        grad.push({
          step: event.step ?? 0,
          eventId: event.event_id,
          attempt: event.attempt_id,
          value: gradValue,
        });
      }
      const lrValue = numberOrUndefined(event.data.lr);
      if (lrValue != null) {
        lr.push({
          step: event.step ?? 0,
          eventId: event.event_id,
          attempt: event.attempt_id,
          value: lrValue,
        });
      }
      const groupLosses = event.data.group_losses;
      if (groupLosses && typeof groupLosses === "object") {
        for (const name of Object.keys(groupLosses as Record<string, unknown>)) groups.add(name);
      }
    }
    for (const event of validationEvents) {
      const value = numberOrUndefined(event.data.loss);
      if (value != null) {
        const key = lossKey(event);
        const existing = lossByStep.get(key);
        lossByStep.set(key, {
          step: event.step ?? 0,
          eventId: existing ? Math.min(existing.eventId, event.event_id) : event.event_id,
          attempt: event.attempt_id,
          trainLoss: existing?.trainLoss,
          valLoss: value,
        });
      }
    }
    for (const event of weightEvents) {
      const key = lossKey(event);
      const existing = weightByStep.get(key);
      weightByStep.set(key, {
        step: event.step ?? 0,
        eventId: existing ? Math.max(existing.eventId, event.event_id) : event.event_id,
        attempt: event.attempt_id,
        weightNorm: numberOrUndefined(event.data.weight_norm) ?? existing?.weightNorm,
        trainableWeightNorm:
          numberOrUndefined(event.data.trainable_weight_norm) ?? existing?.trainableWeightNorm,
      });
    }
    const loss = Array.from(lossByStep.values()).sort((a, b) => a.eventId - b.eventId);
    const weight = Array.from(weightByStep.values()).sort((a, b) => a.eventId - b.eventId);
    grad.sort((a, b) => a.eventId - b.eventId);
    lr.sort((a, b) => a.eventId - b.eventId);
    return {
      lossData: downsample(loss),
      gradData: downsample(grad),
      weightData: downsample(weight),
      lrData: downsample(lr),
      groupNames: Array.from(groups),
      lossRawCount: loss.length,
      gradRawCount: grad.length,
      weightRawCount: weight.length,
    };
  }, [trainEvents, validationEvents, weightEvents]);

  const latestTrain = trainEvents[trainEvents.length - 1];
  const latestValidation = validationEvents[validationEvents.length - 1];
  const latestWeight = weightEvents[weightEvents.length - 1];
  const latestGroupLosses = (latestTrain?.data.group_losses ?? {}) as Record<string, unknown>;
  const rawPointCount = trainEvents.length + validationEvents.length + weightEvents.length;
  const isDownsampled =
    lossRawCount > PLOT_POINT_CAP ||
    gradRawCount > PLOT_POINT_CAP ||
    weightRawCount > PLOT_POINT_CAP;
  const latestWeightNorm = numberOrUndefined(monitor?.latest?.weight_norm?.weight_norm) ??
    numberOrUndefined(latestWeight?.data.weight_norm);
  const latestTrainableNorm = numberOrUndefined(
    monitor?.latest?.weight_norm?.trainable_weight_norm,
  ) ?? numberOrUndefined(latestWeight?.data.trainable_weight_norm);
  const latestLr = numberOrUndefined(latestTrain?.data.lr);
  const latestGrad = numberOrUndefined(latestTrain?.data.grad_norm);
  const legacySource = events.some((event) => event.source === "legacy_log");

  const progress = getProgressPercentage();
  const isStarting = trainingStatus.training_active && trainingStatus.total_steps === 0;
  const stepLabel = isStarting
    ? "Training starting…"
    : `${trainingStatus.current_step.toLocaleString()} / ${trainingStatus.total_steps.toLocaleString()}`;
  const etaLabel =
    trainingStatus.eta_seconds != null ? formatTime(trainingStatus.eta_seconds) : "—";

  const state = trainingStatus.state;
  const pauseStatus = monitor?.pause?.status ?? null;
  const pauseStatusLabel =
    pauseStatus === "saving"
      ? "正在保存 checkpoint…"
      : pauseStatus === "requested"
        ? "等待安全更新边界…"
        : pauseStatus === "failed"
          ? "暂停失败"
          : pauseStatus === "saved"
            ? "checkpoint 已保存"
            : null;

  const chartCard = (
    title: string,
    current: string,
    icon: React.ReactNode,
    data: Array<Record<string, unknown>>,
    lines: Array<{ key: string; color: string; name: string }>,
    emptyHint: React.ReactNode,
    yTickFormatter?: (value: number) => string,
  ) => (
    <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-3 text-white text-base">
          {icon}
          <span>
            {title}{" "}
            <span className="text-slate-400 text-sm font-normal">({current})</span>
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="h-52">
          {data.length === 0 ? (
            emptyHint
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                <XAxis
                  dataKey="step"
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  stroke="#475569"
                />
                <YAxis
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  stroke="#475569"
                  width={56}
                  tickFormatter={yTickFormatter}
                />
                <Tooltip
                  {...chartTooltip(lines[0]?.color ?? "#34d399")}
                  formatter={(value: number) => value.toPrecision(5)}
                  labelFormatter={(label) => `step ${label}`}
                />
                {boundaries.map((boundary) => (
                  <ReferenceLine
                    key={`${boundary.label}-${boundary.step}`}
                    x={boundary.step}
                    stroke="#f59e0b"
                    strokeDasharray="4 4"
                    label={{ value: boundary.label, fill: "#fbbf24", fontSize: 10, position: "insideTopRight" }}
                  />
                ))}
                {lines.map((line) => (
                  <Line
                    key={line.key}
                    type="monotone"
                    dataKey={line.key}
                    name={line.name}
                    stroke={line.color}
                    strokeWidth={2}
                    dot={false}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </CardContent>
    </Card>
  );

  return (
    <div className="space-y-6">
      <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
        <CardContent className="p-6 space-y-4">
          <div className="flex items-baseline justify-between mb-3">
            <div className="flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-500/20 text-blue-400">
                <Activity className="w-5 h-5" />
              </div>
              <div>
                <h3 className="text-sm text-slate-400">Progress</h3>
                <div className="text-base font-semibold text-white">{stepLabel}</div>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <div className="flex items-center gap-2 text-slate-300">
                <Clock className="w-4 h-4 text-purple-400" />
                <span className="text-sm">
                  ETA <span className="font-semibold text-white">{etaLabel}</span>
                </span>
              </div>
              <span className={`text-xs px-2 py-1 rounded border ${STATE_CLASS[state]}`}>
                {STATE_LABEL[state]}
                {pauseStatusLabel ? ` · ${pauseStatusLabel}` : ""}
              </span>
              {monitor?.capabilities.pause_resume && monitor?.attempts?.length ? (
                <span className="text-xs text-slate-400">
                  attempt {monitor.attempts.length}
                </span>
              ) : null}
            </div>
          </div>
          <div className="relative h-8 w-full overflow-hidden rounded-md bg-slate-900 border border-slate-700">
            <div
              className="h-full bg-gradient-to-r from-blue-500 to-sky-400 transition-[width] duration-500"
              style={{ width: `${progress}%` }}
            />
            <div className="absolute inset-0 flex items-center justify-center font-semibold text-white text-sm tabular-nums drop-shadow">
              {isStarting ? "warming up…" : `${progress.toFixed(1)}%`}
            </div>
          </div>
          <div className="flex flex-wrap gap-4 text-xs text-slate-300">
            <span>
              Train loss <span className="text-emerald-300 font-semibold">{formatNumber(numberOrUndefined(latestTrain?.data.loss))}</span>
            </span>
            <span>
              Validation loss{" "}
              <span className="text-sky-300 font-semibold">
                {formatNumber(numberOrUndefined(latestValidation?.data.loss))}
              </span>
            </span>
            <span>
              Grad norm <span className="text-amber-300 font-semibold">{formatNumber(latestGrad, 3)}</span>
            </span>
            <span>
              Weight L2 <span className="text-violet-300 font-semibold">{formatNumber(latestWeightNorm, 4)}</span>
              {latestTrainableNorm != null && (
                <span className="text-slate-500"> / trainable {formatNumber(latestTrainableNorm, 4)}</span>
              )}
            </span>
            <span>
              LR <span className="text-orange-300 font-semibold">{formatScientific(latestLr ?? trainingStatus.current_lr)}</span>
            </span>
            {legacySource && <span className="text-amber-300">来源：旧版文本日志</span>}
            {isDownsampled && (
              <span className="text-slate-500">
                图表降采样显示；原始 {rawPointCount} 个事件仍可完整分页查询
              </span>
            )}
          </div>
          {monitor?.pause?.error && (
            <div className="flex items-start gap-2 text-xs text-red-300">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span>{monitor.pause.error}</span>
            </div>
          )}
          {monitor?.pause?.resume_error && (
            <div className="flex items-start gap-2 text-xs text-red-300">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span>{monitor.pause.resume_error}</span>
            </div>
          )}
        </CardContent>
      </Card>

      {historyError && (
        <Card className="bg-red-900/20 border-red-800 rounded-xl">
          <CardContent className="p-4 flex items-center justify-between gap-4">
            <div className="flex items-start gap-2 text-sm text-red-200">
              <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
              <span>监测历史加载失败：{historyError}</span>
            </div>
            <Button
              onClick={() => setReloadKey((value) => value + 1)}
              variant="ghost"
              className="text-red-200 hover:text-white"
            >
              重试
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {chartCard(
          "Loss (train / validation)",
          `${formatNumber(numberOrUndefined(latestTrain?.data.loss))} / ${formatNumber(
            numberOrUndefined(latestValidation?.data.loss),
          )}`,
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-green-500/20 text-green-400">
            <CheckCircle className="w-4 h-4" />
          </div>,
          lossData as unknown as Array<Record<string, unknown>>,
          [
            { key: "trainLoss", color: "#34d399", name: "train loss" },
            { key: "valLoss", color: "#38bdf8", name: "validation loss" },
          ],
          <EmptyHint>
            {loading
              ? "Loading history…"
              : monitor && !monitor.validation.configured
                ? "未配置验证集：设置验证间隔与显式 validation episodes 后才会产生 validation loss"
                : "Waiting for first metric tick…"}
          </EmptyHint>,
        )}

        {chartCard(
          "Gradient Norm",
          formatNumber(latestGrad, 3),
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-amber-500/20 text-amber-400">
            <Gauge className="w-4 h-4" />
          </div>,
          gradData as unknown as Array<Record<string, unknown>>,
          [{ key: "value", color: "#f59e0b", name: "global grad norm (pre-clip)" }],
          <EmptyHint>
            {loading ? "Loading history…" : "等待首个 log step 的梯度范数（禁用裁剪时在 log step 无副作用计算）"}
          </EmptyHint>,
        )}

        {chartCard(
          "Model Weight Norm (global L2)",
          latestWeightNorm == null ? "—" : `${formatNumber(latestWeightNorm, 3)}`,
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-violet-500/20 text-violet-400">
            <Scale className="w-4 h-4" />
          </div>,
          weightData as unknown as Array<Record<string, unknown>>,
          [
            { key: "weightNorm", color: "#a78bfa", name: "all parameters" },
            { key: "trainableWeightNorm", color: "#f472b6", name: "trainable only" },
          ],
          <EmptyHint>
            {loading
              ? "Loading history…"
              : monitor?.requested?.weight_norm_freq === 0
                ? "采样已关闭（weight_norm_freq=0）"
                : "等待首个采样步（全模型 FP32 L2，去重共享参数）"}
          </EmptyHint>,
        )}

        {chartCard(
          "Learning Rate",
          formatScientific(latestLr ?? trainingStatus.current_lr),
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-orange-500/20 text-orange-400">
            <Activity className="w-4 h-4" />
          </div>,
          lrData as unknown as Array<Record<string, unknown>>,
          [{ key: "value", color: "#fb923c", name: "optimizer LR" }],
          <EmptyHint>{loading ? "Loading history…" : "Waiting for first metric tick…"}</EmptyHint>,
          (value: number) => value.toExponential(0),
        )}
      </div>

      <p className="text-xs text-slate-500">
        Weight norm = <code>sqrt(sum(p.float()**2))</code> over deduplicated parameters, FP32
        accumulation; the trainable-only line covers <code>requires_grad</code> parameters.
        Sampled every {String(monitor?.resolved_config?.weight_norm_freq ?? monitor?.requested?.weight_norm_freq ?? 0)} steps
        {numberOrUndefined(monitor?.latest?.weight_norm?.duration_ms) != null
          ? `; last sampling pass took ${formatNumber(
              numberOrUndefined(monitor?.latest?.weight_norm?.duration_ms),
              1,
            )} ms`
          : ""}
        . Gradient norm is the global pre-clip norm of the optimizer update (computed without
        side effects when clipping is disabled).
      </p>

      {groupNames.length > 0 && (
        <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-3 text-white text-base">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-pink-500/20 text-pink-400">
                <Sigma className="w-4 h-4" />
              </div>
              Grouped flow loss (same masked loss, per action group)
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {groupNames.map((name) => (
                <div key={name} className="rounded-lg bg-slate-900/60 border border-slate-700 p-3">
                  <div className="text-xs text-slate-400">{name}</div>
                  <div className="text-lg font-semibold text-pink-300">
                    {formatNumber(numberOrUndefined(latestGroupLosses[name]))}
                  </div>
                </div>
              ))}
            </div>
            <p className="mt-3 text-xs text-slate-500">
              这是同一 flow-matching 总 loss 在动作维分组上的精确分解（非独立目标），仅用于诊断耦合。
            </p>
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-3 text-white text-base">
              <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-cyan-500/20 text-cyan-400">
                <LineChartIcon className="w-4 h-4" />
              </div>
              Resolved training configuration
            </CardTitle>
          </CardHeader>
          <CardContent className="pt-0 text-sm">
            {monitor?.resolved_config ? (
              <div className="space-y-3">
                <div className="grid grid-cols-2 gap-2">
                  <span className="text-slate-400">Optimizer</span>
                  <span className="text-slate-100">
                    {String(monitor.resolved_config.optimizer?.config_type ?? "—")} · base LR{" "}
                    {formatScientific(
                      numberOrUndefined(monitor.resolved_config.optimizer?.config?.lr),
                    )}
                  </span>
                  <span className="text-slate-400">Param groups</span>
                  <span className="text-slate-100 break-all">
                    {monitor.resolved_config.optimizer?.param_groups?.length ?? 0} group(s)
                    {monitor.resolved_config.optimizer?.param_groups?.length
                      ? ` · LRs ${(monitor.resolved_config.optimizer.param_groups ?? [])
                          .map((group) => formatScientific(numberOrUndefined(group.lr)))
                          .join(", ")}`
                      : ""}
                  </span>
                  <span className="text-slate-400">Scheduler</span>
                  <span className="text-slate-100">
                    {String(monitor.resolved_config.scheduler?.config_type ?? "none")}
                    {monitor.resolved_config.scheduler?.config
                      ? ` · warmup ${String(
                          monitor.resolved_config.scheduler.config.num_warmup_steps ?? "—",
                        )} · decay ${String(
                          monitor.resolved_config.scheduler.config.num_decay_steps ?? "—",
                        )} · floor ${formatScientific(
                          numberOrUndefined(monitor.resolved_config.scheduler.config.decay_lr),
                        )}`
                      : ""}
                  </span>
                  <span className="text-slate-400">Train episodes</span>
                  <span className="text-slate-100 break-all">
                    {Array.isArray(monitor.resolved_config.dataset?.train_episodes)
                      ? (monitor.resolved_config.dataset.train_episodes as number[]).join(", ")
                      : "—"}
                  </span>
                  <span className="text-slate-400">Validation episodes</span>
                  <span className="text-slate-100 break-all">
                    {Array.isArray(monitor.resolved_config.dataset?.eval_episodes) &&
                    (monitor.resolved_config.dataset.eval_episodes as number[]).length > 0
                      ? (monitor.resolved_config.dataset.eval_episodes as number[]).join(", ")
                      : "未配置"}
                  </span>
                  <span className="text-slate-400">Validation cadence</span>
                  <span className="text-slate-100">
                    {String(monitor.resolved_config.eval_steps ?? 0)} steps
                  </span>
                  <span className="text-slate-400">Weight norm cadence</span>
                  <span className="text-slate-100">
                    {String(monitor.resolved_config.weight_norm_freq ?? 0)} steps
                  </span>
                  <span className="text-slate-400">Grad norm scope</span>
                  <span className="text-slate-100">global pre-clip · log step</span>
                </div>
                <div className="rounded-lg bg-slate-900/60 border border-slate-700 p-3 text-xs text-slate-400">
                  请求值：LR {formatScientific(numberOrUndefined(monitor.requested?.optimizer_lr))} ·
                  scheduler {String(monitor.requested?.scheduler_type ?? "preset")} ·
                  warmup {String(monitor.requested?.scheduler_warmup_steps ?? "preset")} ·
                  decay {String(monitor.requested?.scheduler_decay_steps ?? "preset")} ·
                  floor {formatScientific(numberOrUndefined(monitor.requested?.scheduler_floor_lr))}
                </div>
              </div>
            ) : (
              <p className="text-slate-500">
                {loading ? "Loading…" : "尚无 resolved 配置事件（旧任务或 worker 尚未启动）。"}
              </p>
            )}
          </CardContent>
        </Card>

        <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
          <CardHeader className="pb-2">
            <CardTitle className="text-white text-base">Attempts & pause checkpoint</CardTitle>
          </CardHeader>
          <CardContent className="pt-0 text-sm space-y-3">
            {monitor?.attempts?.length ? (
              <div className="space-y-2">
                {monitor.attempts.map((attempt, index) => (
                  <div
                    key={attempt.id}
                    className="rounded-lg bg-slate-900/60 border border-slate-700 p-3 flex items-start justify-between gap-3"
                  >
                    <div>
                      <div className="text-slate-200 font-medium">
                        #{index + 1} · {attempt.id}
                      </div>
                      <div className="text-xs text-slate-500">
                        {attempt.resumed_from_step != null
                          ? `从 step ${attempt.resumed_from_step} 续训`
                          : "初始启动"}
                        {attempt.resume_checkpoint ? ` · ${attempt.resume_checkpoint}` : ""}
                      </div>
                      {attempt.error && (
                        <div className="text-xs text-red-300 mt-1">{attempt.error}</div>
                      )}
                    </div>
                    <span className={`text-xs px-2 py-1 rounded border ${STATE_CLASS[attempt.state]}`}>
                      {STATE_LABEL[attempt.state]}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-slate-500">No attempts recorded.</p>
            )}
            {monitor?.pause?.checkpoint && (
              <div className="rounded-lg bg-sky-900/20 border border-sky-800 p-3 text-xs text-sky-200 break-all">
                Pause checkpoint · step {monitor.pause.step ?? "—"} · {monitor.pause.checkpoint}
              </div>
            )}
            {!monitor?.capabilities?.pause_resume && monitor?.capabilities?.reason && (
              <p className="text-xs text-slate-500">{monitor.capabilities.reason}</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default MonitoringStats;
