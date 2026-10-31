import React from "react";
import { AlertTriangle, CheckCircle2, Database, Loader2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import {
  checkCompatibility,
  CompositeBuild,
  CompositeReport,
  ensureCompositeReady,
  WeightsMode,
} from "@/lib/compositesApi";
import { useApi } from "@/contexts/ApiContext";
import { useToast } from "@/hooks/use-toast";

interface Props {
  repoIds: string[];
  weightsMode: WeightsMode;
  weights: number[];
  onWeightsModeChange: (mode: WeightsMode) => void;
  onWeightsChange: (weights: number[]) => void;
  onReport: (report: CompositeReport | null) => void;
}

const formatBytes = (value?: number | null): string => {
  if (!value || value <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
};

const WEIGHT_MODES: { value: WeightsMode; label: string; hint: string }[] = [
  { value: "frames", label: "按帧数比例", hint: "大数据集出现频率更高（默认，零改动）" },
  { value: "equal", label: "每个数据集等权", hint: "无论大小，每个数据集贡献相同" },
  { value: "custom", label: "自定义权重", hint: "例如 0.5 / 0.3 / 0.2" },
];

const CompositePanel: React.FC<Props> = ({
  repoIds,
  weightsMode,
  weights,
  onWeightsModeChange,
  onWeightsChange,
  onReport,
}) => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();
  const [report, setReport] = React.useState<CompositeReport | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [preparing, setPreparing] = React.useState(false);
  const [build, setBuild] = React.useState<CompositeBuild | null>(null);

  const effectiveWeights = React.useMemo(() => {
    if (weightsMode === "custom") return weights;
    return undefined;
  }, [weightsMode, weights]);

  React.useEffect(() => {
    if (repoIds.length < 2) {
      setReport(null);
      onReport(null);
      setError(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setLoading(true);
      checkCompatibility(baseUrl, fetchWithHeaders, repoIds, {
        weightsMode,
        weights: effectiveWeights,
        signal: controller.signal,
      })
        .then((next) => {
          setReport(next);
          onReport(next);
          setError(null);
        })
        .catch((e: unknown) => {
          if ((e as { name?: string })?.name === "AbortError") return;
          setError(e instanceof Error ? e.message : String(e));
          setReport(null);
          onReport(null);
        })
        .finally(() => setLoading(false));
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseUrl, fetchWithHeaders, JSON.stringify(repoIds), weightsMode, JSON.stringify(effectiveWeights)]);

  React.useEffect(() => {
    if (weightsMode === "custom" && weights.length !== repoIds.length) {
      onWeightsChange(new Array(repoIds.length).fill(1));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [weightsMode, repoIds.length]);

  const handlePrepare = async () => {
    setPreparing(true);
    setBuild(null);
    try {
      const ready = await ensureCompositeReady(baseUrl, fetchWithHeaders, repoIds, {
        weightsMode,
        weights: effectiveWeights,
        onProgress: setBuild,
      });
      const refreshed = { ...ready, cached: true, remote_state: "ready" };
      setReport(refreshed);
      onReport(refreshed);
      toast({
        title: "组合数据集就绪",
        description: `${refreshed.composite_repo_id}（${refreshed.members.length} 个成员）`,
      });
    } catch (e) {
      toast({
        title: "准备组合数据集失败",
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    } finally {
      setPreparing(false);
      setBuild(null);
    }
  };

  if (repoIds.length < 2) return null;

  const blockers = report?.blockers ?? [];
  const warnings = report?.warnings ?? [];
  const cached = report?.cached === true;
  const progress = Math.round((build?.progress ?? 0) * 100);

  return (
    <div className="space-y-3 rounded-xl border border-slate-700 bg-slate-900/60 p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
          <Database className="h-4 w-4" />
          组合数据集
        </div>
        {loading && <Loader2 className="h-4 w-4 animate-spin text-slate-400" />}
        {!loading && report?.ok && cached && (
          <Badge className="border-emerald-500/40 bg-emerald-500/10 text-emerald-200">
            训练机上已就绪
          </Badge>
        )}
        {!loading && report?.ok && !cached && (
          <Badge className="border-amber-500/40 bg-amber-500/10 text-amber-200">
            需在训练机构建
          </Badge>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-2 text-xs text-red-300">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {report && (
        <>
          <div className="grid grid-cols-2 gap-2 text-xs text-slate-400 md:grid-cols-4">
            {report.members.map((member) => (
              <div key={member.repo_id} className="rounded-lg border border-slate-800 p-2">
                <div className="truncate text-slate-300" title={member.repo_id}>
                  {member.repo_id}
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <span>{member.total_episodes ?? "—"} ep</span>
                  <span>{member.total_frames ?? "—"} 帧</span>
                  {member.sealed && (
                    <span className="text-emerald-300" title="已封存">
                      封存
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>

          {blockers.length > 0 && (
            <div className="space-y-1 rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-200">
              {blockers.map((blocker) => (
                <div key={`${blocker.code}-${blocker.message}`} className="flex gap-2">
                  <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>{blocker.message}</span>
                </div>
              ))}
            </div>
          )}

          {warnings.length > 0 && (
            <div className="space-y-1 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-200">
              {warnings.map((warning) => (
                <div key={`${warning.code}-${warning.message}`} className="flex gap-2">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>{warning.message}</span>
                </div>
              ))}
            </div>
          )}

          <div className="space-y-2">
            <Label className="text-xs text-slate-400">采样权重</Label>
            <div className="flex flex-wrap gap-2">
              {WEIGHT_MODES.map((mode) => (
                <button
                  key={mode.value}
                  type="button"
                  title={mode.hint}
                  onClick={() => onWeightsModeChange(mode.value)}
                  className={cn(
                    "rounded-lg border px-3 py-1.5 text-xs transition",
                    weightsMode === mode.value
                      ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-200"
                      : "border-slate-700 text-slate-300 hover:bg-slate-800",
                  )}
                >
                  {mode.label}
                </button>
              ))}
            </div>
            {weightsMode === "custom" && (
              <div className="flex flex-wrap gap-2">
                {repoIds.map((repoId, index) => (
                  <div key={repoId} className="flex items-center gap-1">
                    <span className="max-w-[120px] truncate text-xs text-slate-500" title={repoId}>
                      {repoId}
                    </span>
                    <Input
                      type="number"
                      min={0.01}
                      step={0.1}
                      value={weights[index] ?? 1}
                      onChange={(e) => {
                        const next = [...weights];
                        while (next.length < repoIds.length) next.push(1);
                        next[index] = Number(e.target.value);
                        onWeightsChange(next);
                      }}
                      className="h-8 w-20 bg-slate-950 border-slate-700 text-white"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="flex items-center justify-between text-xs text-slate-400">
            <span>
              预计占用 {formatBytes(report.estimated_size_bytes)}
              {report.composite_repo_id ? ` · ${report.composite_repo_id}` : ""}
            </span>
            {report.ok && !cached && (
              <Button
                size="sm"
                variant="outline"
                className="border-slate-700 bg-transparent hover:bg-slate-800"
                onClick={() => void handlePrepare()}
                disabled={preparing}
              >
                {preparing ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : null}
                准备组合数据集
              </Button>
            )}
            {report.ok && cached && (
              <span className="flex items-center gap-1 text-emerald-300">
                <CheckCircle2 className="h-3.5 w-3.5" />
                可直接开始训练
              </span>
            )}
          </div>

          {preparing && (
            <div className="space-y-1">
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                <div
                  className="h-full bg-emerald-500 transition-all"
                  style={{ width: `${Math.max(progress, 5)}%` }}
                />
              </div>
              <p className="text-xs text-slate-500">
                {build?.phase === "verifying"
                  ? "校验训练机上的成员数据集…"
                  : build?.phase === "publishing"
                  ? "发布组合数据集…"
                  : `正在训练机合并数据集… ${progress}%`}
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default CompositePanel;
