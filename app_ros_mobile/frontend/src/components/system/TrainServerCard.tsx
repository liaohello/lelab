import React from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Cpu, HardDrive, RefreshCw, Server, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useApi } from "@/contexts/ApiContext";
import { useToast } from "@/hooks/use-toast";
import { getTrainServerLiveStatus, stopRemotePolicyServer } from "@/lib/systemStorageApi";

const POLL_MS = 5_000;

const fmtGb = (value?: number | null) => (value == null ? "—" : `${value.toFixed(1)} GB`);

const freeTone = (percent?: number | null) => {
  if (percent == null) return "text-slate-300";
  if (percent < 10) return "text-red-400";
  if (percent < 15) return "text-amber-300";
  return "text-emerald-300";
};

/**
 * Live, read-only view of the GPU training server, collected over SSH by the
 * backend control plane (never through the inference port 28081).
 */
const TrainServerCard: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();

  const statusQuery = useQuery({
    queryKey: ["train-server-live-status", baseUrl],
    queryFn: ({ signal }) => getTrainServerLiveStatus(baseUrl, fetchWithHeaders, signal),
    refetchInterval: POLL_MS,
    retry: 1,
  });

  const status = statusQuery.data;
  const port = status?.policy_port;
  const disk = status?.disk;

  const handleCleanup = async () => {
    try {
      await stopRemotePolicyServer(baseUrl, fetchWithHeaders);
      toast({ title: "已请求清理遗留推理进程" });
      await statusQuery.refetch();
    } catch (e) {
      toast({
        title: "清理失败",
        description: e instanceof Error ? e.message : String(e),
        variant: "destructive",
      });
    }
  };

  const activeSessions = (status?.sessions ?? []).filter((s) => s.state === "running");
  const straySessions = (status?.sessions ?? []).filter((s) => s.state !== "running" && s.ours);
  const runningJobs = (status?.jobs ?? []).filter((j) => j.state === "running");

  return (
    <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
      <CardHeader className="flex flex-row items-start justify-between space-y-0 p-5 pb-3">
        <div>
          <CardDescription className="flex items-center gap-2 text-slate-400">
            <Server className="h-4 w-4" /> 训练服务器（GPU 主机）
          </CardDescription>
          <CardTitle className="pt-1 text-base">
            {status?.endpoint ?? "未配置"}
            {status && !status.ssh_ok ? (
              <span className="ml-2 inline-flex items-center gap-1 text-sm text-red-300">
                <AlertTriangle className="h-4 w-4" /> SSH 不可达
              </span>
            ) : null}
          </CardTitle>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="border-slate-700 bg-transparent text-slate-200 hover:bg-slate-800"
          onClick={() => void statusQuery.refetch()}
          disabled={statusQuery.isFetching}
        >
          <RefreshCw className={`mr-2 h-4 w-4 ${statusQuery.isFetching ? "animate-spin" : ""}`} />
          刷新
        </Button>
      </CardHeader>
      <CardContent className="space-y-4 px-5 pb-5">
        {status?.error ? (
          <p className="break-words text-xs text-red-300">{status.error}</p>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {(status?.gpus ?? []).map((gpu) => (
            <div key={gpu.index} className="rounded-lg border border-slate-800 bg-slate-950/40 p-3">
              <div className="flex items-center justify-between text-xs text-slate-400">
                <span className="inline-flex items-center gap-1">
                  <Cpu className="h-3.5 w-3.5" /> GPU {gpu.index}
                </span>
                <span>{gpu.temp_c}°C</span>
              </div>
              <p className="mt-1 text-sm text-slate-200">
                空闲 <span className="font-semibold text-emerald-300">{gpu.mem_free_mb}</span> /{" "}
                {gpu.mem_total_mb} MB
              </p>
              <p className="text-xs text-slate-500">
                利用率 {gpu.util_percent}% · 我们的作业 {gpu.our_jobs.length} · 他人进程{" "}
                {gpu.foreign_procs}
              </p>
            </div>
          ))}
          {(status?.gpus ?? []).length === 0 ? (
            <p className="text-xs text-slate-500">
              {statusQuery.isLoading ? "读取中…" : "暂无 GPU 数据"}
            </p>
          ) : null}
        </div>

        <div className="grid gap-3 lg:grid-cols-3">
          <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-sm">
            <p className="text-xs text-slate-400">推理端口 28081</p>
            <p className="mt-1">
              {port?.state === "free"
                ? "空闲"
                : port?.state === "ours"
                  ? "被本系统会话占用"
                  : port?.state === "foreign"
                    ? "被外来进程占用"
                    : "未知"}
            </p>
            <p className="text-xs text-slate-500">
              运行中会话 {activeSessions.length} · 遗留 {straySessions.length}
            </p>
            {straySessions.length > 0 ? (
              <Button
                size="sm"
                variant="outline"
                className="mt-2 border-amber-700 bg-transparent text-amber-200 hover:bg-amber-950/40"
                onClick={handleCleanup}
              >
                <Trash2 className="mr-2 h-3.5 w-3.5" /> 清理遗留会话
              </Button>
            ) : null}
          </div>

          <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-sm">
            <p className="text-xs text-slate-400">训练磁盘</p>
            <p className={`mt-1 font-semibold ${freeTone(disk?.free_percent)}`}>
              {disk?.free_percent == null ? "—" : `${disk.free_percent}% 可用`}
            </p>
            <p className="text-xs text-slate-500">
              {fmtGb(disk?.free_gb)} 空闲 / {fmtGb(disk?.total_gb)} · 作业目录{" "}
              {status?.usage?.jobs_gb ?? "—"} GB
            </p>
            <p className="text-xs text-slate-500">
              {disk?.free_percent != null && disk.free_percent < 10
                ? "低于 10%：将拒绝新建作业"
                : disk?.free_percent != null && disk.free_percent < 15
                  ? "低于 15%：建议清理中间权重"
                  : "水位正常"}
            </p>
          </div>

          <div className="rounded-lg border border-slate-800 bg-slate-950/40 p-3 text-sm">
            <p className="text-xs text-slate-400">我们的作业</p>
            {runningJobs.length === 0 ? (
              <p className="mt-1 text-slate-400">无运行中的训练作业</p>
            ) : (
              <ul className="mt-1 space-y-1 text-xs">
                {runningJobs.map((job) => (
                  <li key={job.id} className="truncate" title={job.id}>
                    {job.id}
                    {job.gpu != null ? ` · GPU ${job.gpu}` : ""}
                    {job.mem_mb ? ` · ${job.mem_mb} MB` : ""}
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-1 flex items-center gap-1 text-xs text-slate-500">
              <HardDrive className="h-3 w-3" />
              权重 {status?.usage?.jobs_gb ?? "—"} GB · 策略缓存{" "}
              {status?.usage?.policy_server_gb ?? "—"} GB
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
};

export default TrainServerCard;
