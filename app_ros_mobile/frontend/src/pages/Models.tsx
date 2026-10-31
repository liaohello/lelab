import React, { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { HardDrive, Loader2, RefreshCw, Trash2 } from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useApi } from "@/contexts/ApiContext";
import { useToast } from "@/hooks/use-toast";
import {
  deleteCheckpoints,
  getCheckpointInventory,
  getJobMetricsHistory,
  getLocalModels,
  getPullProgress,
  getRemoteModelLibrary,
  pruneCheckpoints,
  pullRemoteCheckpoint,
  purgeLocalModel,
  restoreLocalModel,
  RemoteCheckpointJob,
  trashLocalModel,
} from "@/lib/modelsApi";

const REFRESH_MS = 10_000;

const tone = (percent?: number | null) =>
  percent == null ? "text-slate-300" : percent < 10 ? "text-red-400" : percent < 15 ? "text-amber-300" : "text-emerald-300";

const JobWeights: React.FC<{ job: RemoteCheckpointJob; keepN: number }> = ({ job, keepN }) => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const newset = useMemo(
    () => Math.max(...job.checkpoints.map((c) => c.step), 0),
    [job.checkpoints],
  );
  const metricsQuery = useQuery({
    queryKey: ["job-metrics-history", baseUrl, job.id],
    queryFn: ({ signal }) => getJobMetricsHistory(baseUrl, fetchWithHeaders, job.id, signal),
    retry: 0,
  });
  const points = (metricsQuery.data ?? []).filter((p) => p.loss != null);

  const removeStep = async (step: number) => {
    if (!window.confirm(`删除作业 ${job.id} 的 step ${step}？（真删，不可恢复）`)) return;
    setBusy(true);
    try {
      const result = await deleteCheckpoints(baseUrl, fetchWithHeaders, { job_id: job.id, steps: [step] });
      toast({ title: `已删除 step ${step}`, description: `释放 ${result.freed_mb} MB` });
      await queryClient.invalidateQueries({ queryKey: ["checkpoint-inventory"] });
    } catch (e) {
      toast({
        title: "删除失败",
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
      <CardHeader className="space-y-1 p-4 pb-2">
        <CardDescription className="truncate font-mono text-xs" title={job.id}>
          {job.id}
        </CardDescription>
        <CardTitle className="text-sm">
          {job.checkpoints.length} 个 checkpoint · {job.checkpoint_mb} MB · 状态 {job.state}
          {job.checkpoints.length > keepN ? (
            <span className="ml-2 text-xs text-amber-300">超出保留数 {keepN}，可回收</span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-0">
        {points.length > 1 ? (
          <div className="h-36 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
                <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                <XAxis dataKey="step" stroke="#64748b" tick={{ fontSize: 10 }} />
                <YAxis stroke="#64748b" tick={{ fontSize: 10 }} />
                <Tooltip
                  contentStyle={{ background: "#0f172a", border: "1px solid #334155", fontSize: 12 }}
                  labelFormatter={(value) => `step ${value}`}
                />
                <Line type="monotone" dataKey="loss" stroke="#34d399" dot={false} strokeWidth={2} />
                {selected != null ? <ReferenceLine x={selected} stroke="#fbbf24" /> : null}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-xs text-slate-500">
            {metricsQuery.isLoading ? "读取训练曲线…" : "暂无 loss 曲线（日志已被清理或作业未产生指标）"}
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          {job.checkpoints.map((ckpt) => {
            const isNewest = ckpt.step === newset;
            return (
              <div
                key={ckpt.step}
                onMouseEnter={() => setSelected(ckpt.step)}
                onMouseLeave={() => setSelected(null)}
                className={`flex items-center gap-2 rounded border px-2 py-1 text-xs ${
                  isNewest ? "border-emerald-700 bg-emerald-950/30" : "border-slate-700 bg-slate-950/40"
                }`}
              >
                <span className="font-mono">step {ckpt.step}</span>
                <span className="text-slate-400">{ckpt.size_mb} MB</span>
                {isNewest ? (
                  <span className="text-emerald-300">保留</span>
                ) : (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void removeStep(ckpt.step)}
                    className="text-red-300 hover:text-red-200 disabled:opacity-40"
                    title="真删该 checkpoint"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
};

const Models: React.FC = () => {
  return (
    <div className="min-h-screen bg-slate-950 px-4 py-6 text-slate-100 sm:px-8">
      <div className="mx-auto max-w-7xl space-y-5">
        <TrainWeightsSection />
        <RemoteLibrarySection />
        <LocalModelsSection />
      </div>
    </div>
  );
};

/** Remote model library: our job checkpoints (single-step download) + staged weights. */
const RemoteLibrarySection: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const libraryQuery = useQuery({
    queryKey: ["remote-model-library", baseUrl],
    queryFn: ({ signal }) => getRemoteModelLibrary(baseUrl, fetchWithHeaders, signal),
    refetchInterval: REFRESH_MS,
    retry: 1,
  });
  const progressQuery = useQuery({
    queryKey: ["model-pull-progress", baseUrl],
    queryFn: ({ signal }) => getPullProgress(baseUrl, fetchWithHeaders, signal),
    refetchInterval: 2_000,
    retry: 0,
  });
  const progress = progressQuery.data;
  const pulling = progress?.phase === "transferring" || progress?.phase === "verifying";

  const download = async (jobId: string, step: number) => {
    try {
      await pullRemoteCheckpoint(baseUrl, fetchWithHeaders, { job_id: jobId, step });
      toast({ title: `开始下载 ${jobId.slice(-18)} · step ${step}`, description: "进度见下方进度条" });
      await progressQuery.refetch();
    } catch (e) {
      toast({
        title: "下载失败",
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    }
  };

  const library = libraryQuery.data?.library ?? [];

  return (
    <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
      <CardHeader className="flex flex-row items-center justify-between space-y-0 p-4 pb-2">
        <div>
          <CardDescription className="text-slate-400">远程模型库（训练机）</CardDescription>
          <CardTitle className="text-base">
            按需下载单个 checkpoint · 已在本机的会标出
          </CardTitle>
        </div>
        {pulling ? (
          <span className="text-xs text-amber-300">
            下载中：{progress?.job_id?.slice(-18)} · step {progress?.step}（{progress?.phase}）
          </span>
        ) : null}
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-0">
        {libraryQuery.isError ? (
          <p className="text-xs text-red-300">读取失败：{String(libraryQuery.error)}</p>
        ) : null}
        {library.length === 0 && libraryQuery.data?.ssh_ok ? (
          <p className="text-xs text-slate-500">训练机上还没有本系统的 checkpoint。</p>
        ) : null}
        {library.map((item) => (
          <div key={item.job_id} className="rounded border border-slate-800 bg-slate-950/40 p-2">
            <p className="truncate font-mono text-xs text-slate-400" title={item.job_id}>
              {item.job_id}
            </p>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {item.checkpoints.map((ckpt) => {
                const local = item.local && item.local_step === ckpt.step;
                return (
                  <button
                    key={ckpt.step}
                    type="button"
                    disabled={pulling || local}
                    onClick={() => void download(item.job_id, ckpt.step)}
                    className={`rounded border px-2 py-1 text-xs disabled:opacity-50 ${
                      local
                        ? "border-emerald-700 bg-emerald-950/30 text-emerald-200"
                        : "border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800"
                    }`}
                    title={local ? "已在本机" : "下载到 Jetson"}
                  >
                    step {ckpt.step} · {ckpt.size_mb} MB{local ? " · 本地已有" : " ↓"}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
        {(libraryQuery.data?.weights ?? []).length > 0 ? (
          <div className="rounded border border-slate-800 bg-slate-950/40 p-2">
            <p className="text-xs text-slate-400">原始权重（只读）</p>
            <div className="mt-1 flex flex-wrap gap-2 text-xs text-slate-300">
              {(libraryQuery.data?.weights ?? []).map((weight) => (
                <span key={weight.remote_path} className="rounded border border-slate-700 px-2 py-1 font-mono">
                  {weight.kind}/{weight.name} · {weight.size_mb} MB
                </span>
              ))}
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
};

/** Models pulled to this Jetson + the manual trash. */
const LocalModelsSection: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);

  const localQuery = useQuery({
    queryKey: ["local-models", baseUrl],
    queryFn: ({ signal }) => getLocalModels(baseUrl, fetchWithHeaders, signal),
    refetchInterval: REFRESH_MS,
    retry: 1,
  });
  const models = localQuery.data?.models ?? [];
  const trash = localQuery.data?.trash ?? [];

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      toast({ title: label });
      await queryClient.invalidateQueries({ queryKey: ["local-models"] });
    } catch (e) {
      toast({
        title: `${label}失败`,
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
      <CardHeader className="p-4 pb-2">
        <CardDescription className="text-slate-400">本机模型（Jetson）</CardDescription>
        <CardTitle className="text-base">
          {models.length} 个模型 · 只保留最近 {localQuery.data?.keep ?? 10} 个；超出的自动进回收站
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-0">
        {models.map((model) => (
          <div
            key={model.job_id}
            className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-800 bg-slate-950/40 p-2 text-xs"
          >
            <span className="truncate font-mono" title={model.job_id}>
              {model.job_id}
            </span>
            <span className="text-slate-400">
              step {model.step ?? "—"} · {model.size_mb ?? "—"} MB ·{" "}
              {model.pulled_at ? new Date(model.pulled_at * 1000).toLocaleString() : ""}
            </span>
            <Button
              size="sm"
              variant="outline"
              className="border-amber-800 bg-transparent text-amber-200 hover:bg-amber-950/40"
              disabled={busy}
              onClick={() => void run("已移入回收站", () => trashLocalModel(baseUrl, fetchWithHeaders, model.job_id))}
            >
              <Trash2 className="mr-1 h-3.5 w-3.5" /> 移入回收站
            </Button>
          </div>
        ))}
        {models.length === 0 ? (
          <p className="text-xs text-slate-500">还没有回传的模型（训练成功后会自动拉回最终 checkpoint）。</p>
        ) : null}

        {trash.length > 0 ? (
          <div className="space-y-2 rounded border border-amber-900/60 bg-amber-950/20 p-2">
            <p className="text-xs text-amber-200">回收站（手动彻底删除，不会自动过期）</p>
            {trash.map((entry) => (
              <div key={entry.trash_name} className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span className="truncate font-mono" title={entry.job_id}>
                  {entry.job_id}
                </span>
                <span className="text-slate-400">
                  step {entry.step ?? "—"} · {entry.size_mb} MB
                </span>
                <span className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    className="border-slate-700 bg-transparent"
                    disabled={busy}
                    onClick={() =>
                      void run("已恢复", () => restoreLocalModel(baseUrl, fetchWithHeaders, entry.trash_name))
                    }
                  >
                    恢复
                  </Button>
                  <Button
                    size="sm"
                    className="bg-red-600 text-white hover:bg-red-700"
                    disabled={busy}
                    onClick={() => {
                      if (!window.confirm(`彻底删除 ${entry.job_id}？（不可恢复）`)) return;
                      void run("已彻底删除", () => purgeLocalModel(baseUrl, fetchWithHeaders, entry.trash_name));
                    }}
                  >
                    彻底删除
                  </Button>
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
};

const TrainWeightsSection: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [keepN, setKeepN] = useState(3);
  const [busy, setBusy] = useState(false);

  const inventoryQuery = useQuery({
    queryKey: ["checkpoint-inventory", baseUrl],
    queryFn: ({ signal }) => getCheckpointInventory(baseUrl, fetchWithHeaders, signal),
    refetchInterval: REFRESH_MS,
    retry: 1,
  });
  const inventory = inventoryQuery.data;
  const disk = inventory?.disk;

  const runPrune = async (dryRun: boolean) => {
    setBusy(true);
    try {
      const result = await pruneCheckpoints(baseUrl, fetchWithHeaders, { keep_n: keepN, dry_run: dryRun });
      if (result.dry_run) {
        toast({
          title: `可回收 ${result.reclaimable_mb ?? 0} MB`,
          description: `将删除 ${result.would_delete?.length ?? 0} 个中间 checkpoint（保留最近 ${keepN} 个）`,
        });
      } else {
        toast({
          title: `已删除 ${result.deleted?.length ?? 0} 个 checkpoint`,
          description: `释放 ${result.freed_mb ?? 0} MB`,
        });
        await queryClient.invalidateQueries({ queryKey: ["checkpoint-inventory"] });
      }
    } catch (e) {
      toast({
        title: "清理失败",
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const jobs = (inventory?.jobs ?? []).filter((job) => job.ours && job.checkpoints.length > 0);

  return (
    <div className="space-y-5">
      <div>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">权重管理</h1>
            <p className="text-sm text-slate-500">
              训练机上的 checkpoint 清单（真删中间权重释放磁盘）；本机模型与远程模型库在后续版本合入
            </p>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="border-slate-700 bg-transparent"
            onClick={() => void inventoryQuery.refetch()}
            disabled={inventoryQuery.isFetching}
          >
            <RefreshCw className={`mr-2 h-4 w-4 ${inventoryQuery.isFetching ? "animate-spin" : ""}`} />
            刷新
          </Button>
        </div>

        {inventoryQuery.isError ? (
          <p className="text-sm text-red-300">
            {inventoryQuery.error instanceof Error ? inventoryQuery.error.message : "读取失败"}
          </p>
        ) : null}
        {inventory && !inventory.ssh_ok ? (
          <p className="text-sm text-red-300">训练服务器不可达：{inventory.error}</p>
        ) : null}

        <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 p-4 pb-2">
            <div>
              <CardDescription className="flex items-center gap-2 text-slate-400">
                <HardDrive className="h-4 w-4" /> 训练机磁盘
              </CardDescription>
              <CardTitle className={`text-lg ${tone(disk?.free_percent)}`}>
                {disk?.free_percent == null ? "—" : `${disk.free_percent}% 可用`}
                <span className="ml-2 text-xs text-slate-400">
                  {disk?.free_gb ?? "—"} GB / {disk?.total_gb ?? "—"} GB · 权重共{" "}
                  {inventory?.usage.jobs_gb ?? "—"} GB
                </span>
              </CardTitle>
            </div>
            <div className="flex items-center gap-2">
              <label className="text-xs text-slate-400" htmlFor="keep-n">
                保留最近
              </label>
              <Input
                id="keep-n"
                type="number"
                min={1}
                value={keepN}
                onChange={(e) => setKeepN(Math.max(1, Number(e.target.value) || 1))}
                className="h-8 w-16 border-slate-700 bg-slate-950 text-sm"
              />
              <Button
                size="sm"
                variant="outline"
                className="border-slate-700 bg-transparent"
                disabled={busy}
                onClick={() => void runPrune(true)}
              >
                预览
              </Button>
              <Button
                size="sm"
                className="bg-red-600 text-white hover:bg-red-700"
                disabled={busy}
                onClick={() => void runPrune(false)}
              >
                {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Trash2 className="mr-2 h-4 w-4" />}
                删除可回收（{inventory?.reclaimable_mb ?? 0} MB）
              </Button>
            </div>
          </CardHeader>
          <CardContent className="p-4 pt-0 text-xs text-slate-500">
            每个作业永远保留最新一个 checkpoint（供续训/回传）；<span className="text-amber-300">低于 15% 黄</span>
            、<span className="text-red-300">低于 10% 红色并拒绝新建作业</span>。
          </CardContent>
        </Card>

        {jobs.map((job) => (
          <JobWeights key={job.id} job={job} keepN={inventory?.keep_n ?? 3} />
        ))}
        {jobs.length === 0 && inventory?.ssh_ok ? (
          <p className="text-sm text-slate-500">训练机上还没有本系统的 checkpoint。</p>
        ) : null}
      </div>
    </div>
  );
};

export default Models;
