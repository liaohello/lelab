import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ConfigComponentProps } from "../types";
import { RunnerFlavor } from "@/lib/jobsApi";
import { useApi } from "@/contexts/ApiContext";
import { getTrainServerLiveStatus, getTrainServerStatus } from "@/lib/systemStorageApi";
import { Input } from "@/components/ui/input";

interface TargetCardProps extends ConfigComponentProps {
  authenticated: boolean;
  flavors: RunnerFlavor[];
  loading: boolean;
}

const formatHourly = (unitCostUsd: number, unitLabel: string): string => {
  const hourly = unitLabel === "minute" ? unitCostUsd * 60 : unitCostUsd;
  return `$${hourly.toFixed(2)}/hr`;
};

const formatFlavorLine = (f: RunnerFlavor): string => {
  const accel = f.accelerator ? f.accelerator : f.cpu;
  return `${f.pretty_name} · ${accel} · ${formatHourly(f.unit_cost_usd, f.unit_label)}`;
};

const TargetCard: React.FC<TargetCardProps> = ({
  config,
  updateConfig,
  authenticated,
  flavors,
  loading,
}) => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const target = config.target;
  // The SSH training target is only offered when a train server is configured
  // (deploy/config/servers.example.toml or LELAB_TRAIN_* environment).
  const trainServerQuery = useQuery({
    queryKey: ["train-server-status", baseUrl],
    queryFn: ({ signal }) => getTrainServerStatus(baseUrl, fetchWithHeaders, signal),
    staleTime: 60_000,
    retry: 1,
  });
  const trainServer = trainServerQuery.data;
  const isRemote = target.runner === "ssh_remote";
  // Live GPU numbers so the picker shows what is actually free right now.
  const liveQuery = useQuery({
    queryKey: ["train-server-live-status", baseUrl],
    queryFn: ({ signal }) => getTrainServerLiveStatus(baseUrl, fetchWithHeaders, signal),
    enabled: isRemote,
    refetchInterval: 5_000,
    retry: 1,
  });
  const gpus = liveQuery.data?.gpus ?? [];
  const minFree = target.min_free_vram_mb ?? 4096;
  const value =
    target.runner === "local"
      ? "local"
      : target.runner === "ssh_remote"
      ? "ssh_remote"
      : `hf:${target.flavor ?? ""}`;

  const handleChange = (v: string) => {
    if (v === "local") {
      updateConfig("target", { runner: "local" });
    } else if (v === "ssh_remote") {
      updateConfig("target", { runner: "ssh_remote" });
    } else if (v.startsWith("hf:")) {
      const flavor = v.slice("hf:".length);
      updateConfig("target", { runner: "hf_cloud", flavor });
    }
  };

  return (
    <Card className="bg-slate-800/50 border-slate-700 rounded-xl">
      <CardHeader>
        <CardTitle className="text-white">Compute target</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div>
          <Label className="text-slate-300">Run training on</Label>
          <Select value={value} onValueChange={handleChange}>
            <SelectTrigger className="bg-slate-900 border-slate-600 text-white rounded-lg mt-1">
              <SelectValue placeholder={loading ? "Loading…" : "Select target"} />
            </SelectTrigger>
            <SelectContent className="bg-slate-800 border-slate-600 text-white">
              <SelectItem value="local">Local — your machine (free)</SelectItem>
              <SelectItem value="ssh_remote" disabled={!trainServer?.configured}>
                Train server — {trainServer?.endpoint ?? "not configured"} (SSH)
                {!trainServer?.configured && (
                  <span className="text-amber-300 ml-2 text-xs">
                    configure LELAB_TRAIN_HOST
                  </span>
                )}
              </SelectItem>
              {flavors.map((f) => (
                <SelectItem
                  key={f.name}
                  value={`hf:${f.name}`}
                  disabled={!authenticated}
                >
                  {formatFlavorLine(f)}
                  {!authenticated && (
                    <span className="text-amber-300 ml-2 text-xs">
                      log in to HF
                    </span>
                  )}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {isRemote ? (
            <div className="space-y-3 pt-2">
              <div>
                <Label className="text-slate-300">GPU</Label>
                <Select
                  value={target.gpu == null ? "auto" : String(target.gpu)}
                  onValueChange={(v) =>
                    updateConfig("target", {
                      ...target,
                      gpu: v === "auto" ? undefined : Number(v),
                    })
                  }
                >
                  <SelectTrigger className="bg-slate-900 border-slate-600 text-white rounded-lg mt-1">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="bg-slate-800 border-slate-600 text-white">
                    <SelectItem value="auto">
                      Auto — 空闲显存最多的卡
                      {gpus.length > 0
                        ? `（${gpus
                            .slice()
                            .sort((a, b) => b.mem_free_mb - a.mem_free_mb)[0]
                            .mem_free_mb.toFixed(0)} MB）`
                        : ""}
                    </SelectItem>
                    {gpus.map((gpu) => (
                      <SelectItem key={gpu.index} value={String(gpu.index)}>
                        GPU {gpu.index} — 空闲 {gpu.mem_free_mb.toFixed(0)} MB /{" "}
                        {gpu.mem_total_mb.toFixed(0)} MB · {gpu.util_percent}%
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <Label className="text-slate-300" htmlFor="min-free-vram">
                  最低空闲显存（MB）
                </Label>
                <Input
                  id="min-free-vram"
                  type="number"
                  min={0}
                  value={minFree}
                  onChange={(e) =>
                    updateConfig("target", {
                      ...target,
                      min_free_vram_mb: e.target.value === "" ? undefined : Number(e.target.value),
                    })
                  }
                  className="bg-slate-900 border-slate-600 text-white mt-1"
                />
                <p className="text-[11px] text-slate-500 mt-1">
                  低于该值会拒绝启动（默认 4096）。多作业可并发，显存够就能起。
                </p>
              </div>
              {liveQuery.data && !liveQuery.data.ssh_ok ? (
                <p className="text-[11px] text-red-300">
                  训练服务器不可达：{liveQuery.data.error}
                </p>
              ) : null}
            </div>
          ) : null}
          <p className="text-xs text-slate-500 mt-1">
            Cost shown is per running hour. Final policy uploads to your HF
            account when training completes.
          </p>
        </div>
      </CardContent>
    </Card>
  );
};

export default TargetCard;
