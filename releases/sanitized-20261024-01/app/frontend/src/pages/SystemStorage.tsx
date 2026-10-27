import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import {
  Activity,
  ArrowLeft,
  Cpu,
  CloudUpload,
  Database,
  HardDrive,
  Layers,
  Lock,
  MemoryStick,
  RefreshCw,
  Server,
  Trash2,
  Thermometer,
  Unlock,
  Wifi,
} from "lucide-react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import TrainServerCard from "@/components/system/TrainServerCard";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useApi } from "@/contexts/ApiContext";
import { useStatusStream } from "@/hooks/useStatusStream";
import { cn } from "@/lib/utils";
import {
  DatasetStorageDetail,
  DatasetSyncState,
  getStorageSummary,
  getSystemStatus,
  listLocalDatasetStorage,
  LocalDatasetStorageResult,
  StorageSummary,
  SystemStatus,
  getDatasetSealStatus,
  sealDataset,
  syncDatasetToTrain,
  trashDataset,
  unsealDataset,
  verifyDatasetSeal,
} from "@/lib/systemStorageApi";
import {
  listCompositeTrash,
  listComposites,
  RemoteComposite,
  restoreComposite,
  trashComposite,
} from "@/lib/compositesApi";

const POLL_INTERVAL_MS = 2_000;
const DATASET_REFRESH_MS = 15_000;

type RecordValue = Record<string, unknown>;

interface HistoryPoint {
  time: string;
  cpu: number;
  memory: number;
  gpu?: number;
}

const isRecord = (value: unknown): value is RecordValue =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const valueAtPath = (value: unknown, path: string[]): unknown => {
  let current = value;
  for (const key of path) {
    if (!isRecord(current)) return undefined;
    current = current[key];
  }
  return current;
};

const firstNumber = (...values: unknown[]): number | null => {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() !== "") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
};

const firstText = (...values: unknown[]): string | null => {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
};

const formatBytes = (value: unknown): string => {
  const bytes = firstNumber(value);
  if (bytes === null || bytes < 0) return "—";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  const amount = bytes / 1024 ** exponent;
  return `${amount.toFixed(amount >= 10 || exponent === 0 ? 0 : 1)} ${units[exponent]}`;
};

const formatPercent = (value: unknown): string => {
  const percent = firstNumber(value);
  return percent === null ? "—" : `${percent.toFixed(1)}%`;
};

const formatInteger = (value: unknown): string => {
  const number = firstNumber(value);
  return number === null ? "—" : Math.round(number).toLocaleString();
};

const formatUptime = (secondsValue: unknown): string => {
  const seconds = firstNumber(secondsValue);
  if (seconds === null) return "—";
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分钟`;
  return `${minutes} 分钟`;
};

const formatTimestamp = (value: unknown): string => {
  if (typeof value !== "string" && typeof value !== "number") return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
};

const clampPercent = (value: unknown): number =>
  Math.min(100, Math.max(0, firstNumber(value) ?? 0));

const metadataFor = (detail: DatasetStorageDetail | null): RecordValue => {
  if (!detail) return {};
  if (isRecord(detail.meta)) return detail.meta;
  if (isRecord(detail.metadata)) return detail.metadata;
  if (isRecord(detail.info)) return detail.info;
  return {};
};

const datasetSize = (detail: DatasetStorageDetail | null): number | null =>
  firstNumber(
    detail?.size_bytes,
    detail?.total_bytes,
    valueAtPath(detail, ["summary", "size_bytes"]),
    valueAtPath(detail, ["summary", "total_bytes"]),
    valueAtPath(detail, ["storage", "size_bytes"]),
  );

const datasetEpisodes = (detail: DatasetStorageDetail | null): number | null => {
  const meta = metadataFor(detail);
  return firstNumber(
    detail?.num_episodes,
    detail?.total_episodes,
    valueAtPath(detail, ["format", "total_episodes"]),
    meta.total_episodes,
    meta.num_episodes,
  );
};

const datasetFrames = (detail: DatasetStorageDetail | null): number | null => {
  const meta = metadataFor(detail);
  return firstNumber(
    detail?.num_frames,
    detail?.total_frames,
    valueAtPath(detail, ["format", "total_frames"]),
    meta.total_frames,
    meta.num_frames,
  );
};

const datasetFormat = (detail: DatasetStorageDetail | null): string => {
  if (!detail) return "—";
  const meta = metadataFor(detail);
  const version = firstText(
    valueAtPath(detail, ["format", "codebase_version"]),
    meta.codebase_version,
  );
  const explicit = firstText(
    detail.storage_format,
    detail.format,
    meta.storage_format,
    meta.format,
  );
  if (explicit) return explicit;
  return version ? `LeRobot ${version} (Parquet / MP4)` : "LeRobot (Parquet / MP4)";
};

const datasetPath = (detail: DatasetStorageDetail | null): string =>
  firstText(
    detail?.root,
    detail?.path,
    valueAtPath(detail, ["storage", "root"]),
  ) ?? "—";

const gpuMetric = (gpu: unknown, kind: "usage" | "temperature"): number | null => {
  if (!isRecord(gpu)) return null;
  if (kind === "usage") {
    return firstNumber(
      gpu.percent,
      gpu.utilization_percent,
      gpu.utilization,
      gpu.gpu_percent,
      valueAtPath(gpu, ["utilization", "gpu"]),
    );
  }
  return firstNumber(
    gpu.temperature,
    gpu.temperature_c,
    gpu.temp,
    valueAtPath(gpu, ["thermal", "temperature_c"]),
  );
};

const telemetrySummary = (value: unknown): string => {
  if (value === null || value === undefined) return "未检测到";
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }
  if (Array.isArray(value)) return value.length ? `${value.length} 项` : "无数据";
  if (isRecord(value)) {
    const label = firstText(value.name, value.model, value.interface, value.status);
    if (label) return label;
    const keys = Object.keys(value);
    return keys.length ? keys.slice(0, 3).join("、") : "无数据";
  }
  return "未知";
};

const JsonPanel = ({ title, value }: { title: string; value: unknown }) => {
  const empty =
    value === null ||
    value === undefined ||
    (Array.isArray(value) && value.length === 0) ||
    (isRecord(value) && Object.keys(value).length === 0);

  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold text-slate-200">{title}</h3>
      {empty ? (
        <div className="rounded-md border border-dashed border-slate-700 px-3 py-4 text-sm text-slate-500">
          后端未返回此项
        </div>
      ) : (
        <pre className="max-h-64 overflow-auto rounded-md border border-slate-800 bg-slate-950 p-3 text-xs leading-5 text-slate-300">
          {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
        </pre>
      )}
    </section>
  );
};

const ProgressBar = ({
  value,
  tone = "green",
}: {
  value: unknown;
  tone?: "green" | "amber" | "red" | "blue";
}) => {
  const colors = {
    green: "bg-emerald-500",
    amber: "bg-amber-500",
    red: "bg-red-500",
    blue: "bg-sky-500",
  };
  return (
    <div className="h-2 overflow-hidden rounded-full bg-slate-800">
      <div
        className={cn("h-full rounded-full transition-all", colors[tone])}
        style={{ width: `${clampPercent(value)}%` }}
      />
    </div>
  );
};

const MetricCard = ({
  title,
  value,
  subtitle,
  percent,
  icon: Icon,
  tone = "green",
}: {
  title: string;
  value: string;
  subtitle: string;
  percent?: number | null;
  icon: typeof Cpu;
  tone?: "green" | "amber" | "red" | "blue";
}) => (
  <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
    <CardHeader className="space-y-0 p-5 pb-3">
      <div className="flex items-center justify-between">
        <CardDescription className="text-slate-400">{title}</CardDescription>
        <Icon className="h-5 w-5 text-slate-500" />
      </div>
      <CardTitle className="pt-2 text-2xl">{value}</CardTitle>
    </CardHeader>
    <CardContent className="space-y-3 px-5 pb-5">
      {percent !== undefined && percent !== null && (
        <ProgressBar value={percent} tone={tone} />
      )}
      <p className="truncate text-xs text-slate-500" title={subtitle}>
        {subtitle}
      </p>
    </CardContent>
  </Card>
);

const ErrorPanel = ({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) => (
  <div className="flex flex-col items-start justify-between gap-3 rounded-lg border border-red-900/80 bg-red-950/30 p-4 text-sm text-red-200 sm:flex-row sm:items-center">
    <span>{message}</span>
    <Button
      size="sm"
      variant="outline"
      className="border-red-800 bg-transparent text-red-100 hover:bg-red-950"
      onClick={onRetry}
    >
      重试
    </Button>
  </div>
);

const storageTone = (
  summary: StorageSummary | undefined,
): "green" | "amber" | "red" => {
  const level = summary?.warning_level?.toLowerCase() ?? "";
  const used = firstNumber(summary?.percent_used) ?? 0;
  if (
    used >= 90 ||
    ["critical", "danger", "error", "red", "stop"].some((word) =>
      level.includes(word),
    )
  ) {
    return "red";
  }
  if (
    used >= 80 ||
    ["warning", "warn", "amber", "yellow"].some((word) =>
      level.includes(word),
    )
  ) {
    return "amber";
  }
  return "green";
};

const DatasetDetailDialog = ({
  row,
  onOpenChange,
}: {
  row: LocalDatasetStorageResult | null;
  onOpenChange: (open: boolean) => void;
}) => {
  if (!row) return null;
  const { detail, dataset, error } = row;
  const meta = metadataFor(detail);
  const features =
    detail?.features ?? valueAtPath(detail, ["format", "features"]) ?? meta.features;
  const templates =
    detail?.path_templates ??
    valueAtPath(detail, ["format", "storage_templates"]) ??
    meta.path_templates ?? {
      data_path: meta.data_path,
      video_path: meta.video_path,
      image_path: meta.image_path,
    };
  const fileTree =
    detail?.file_tree ??
    valueAtPath(detail, ["files", "tree"]) ??
    valueAtPath(detail, ["storage", "file_tree"]);
  const parquetSchema =
    detail?.parquet_schema ??
    valueAtPath(detail, ["schema", "parquet"]) ??
    meta.parquet_schema;

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88vh] max-w-4xl overflow-y-auto border-slate-800 bg-slate-900 text-slate-100">
        <DialogHeader>
          <DialogTitle className="pr-8 text-xl">{dataset.repo_id}</DialogTitle>
          <DialogDescription className="break-all text-slate-400">
            {datasetPath(detail)}
          </DialogDescription>
        </DialogHeader>

        {error && (
          <div className="rounded-md border border-amber-800 bg-amber-950/30 p-3 text-sm text-amber-200">
            详情暂不可用：{error}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-4">
          {[
            ["占用空间", formatBytes(datasetSize(detail))],
            ["Episodes", formatInteger(datasetEpisodes(detail))],
            ["Frames", formatInteger(datasetFrames(detail))],
            ["存储格式", datasetFormat(detail)],
          ].map(([label, value]) => (
            <div
              key={label}
              className="rounded-lg border border-slate-800 bg-slate-950/70 p-3"
            >
              <div className="text-xs text-slate-500">{label}</div>
              <div className="mt-1 break-words text-sm font-medium text-slate-100">
                {value}
              </div>
            </div>
          ))}
        </div>

        <div className="grid gap-5 lg:grid-cols-2">
          <JsonPanel title="Meta / info.json" value={meta} />
          <JsonPanel title="数据格式摘要" value={detail?.format} />
          <JsonPanel title="Features" value={features} />
          <JsonPanel title="路径模板" value={templates} />
          <JsonPanel title="Parquet Schema" value={parquetSchema} />
          <div className="lg:col-span-2">
            <JsonPanel title="文件树" value={fileTree} />
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};

export type SystemStorageView = "all" | "system" | "storage";

const SyncStateChip = ({ sync }: { sync?: DatasetSyncState }) => {
  const state = sync?.state ?? "not-synced";
  if (state === "syncing" || state === "queued") {
    const progress = Math.round(sync?.progress ?? 0);
    return (
      <Badge className="border-sky-500/40 bg-sky-500/10 text-sky-200">
        <RefreshCw className="mr-1 h-3 w-3 animate-spin" />
        {state === "queued" ? "等待中" : `同步 ${progress}%`}
      </Badge>
    );
  }
  if (state === "synced") {
    return (
      <Badge className="border-emerald-500/40 bg-emerald-500/10 text-emerald-200">
        已同步
      </Badge>
    );
  }
  if (state === "failed") {
    return (
      <Badge className="border-red-500/40 bg-red-500/10 text-red-200" title={sync?.error ?? undefined}>
        同步失败
      </Badge>
    );
  }
  return (
    <Badge className="border-slate-700 bg-slate-800/60 text-slate-400">未同步</Badge>
  );
};

interface SystemStorageProps {
  /**
   * Which half of the dashboard to render. `/system` and `/storage` are thin
   * wrappers around this component so both routes share one data layer; the
   * legacy `/system-storage` route keeps `"all"`.
   */
  view?: SystemStorageView;
}

const SystemStorage = ({ view = "all" }: SystemStorageProps) => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const queryClient = useQueryClient();
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [selectedDataset, setSelectedDataset] =
    useState<LocalDatasetStorageResult | null>(null);
  const [pendingDataset, setPendingDataset] = useState<string | null>(null);

  // The backend pushes one snapshot per second over WebSocket; while that
  // socket is open React Query's polling is disabled and the pushed payloads
  // are written straight into the same cache entries.
  const systemStream = useStatusStream<SystemStatus>({
    path: "/ws/system-status",
    onMessage: (snapshot) =>
      queryClient.setQueryData(["system-status", baseUrl], snapshot),
  });
  const storageStream = useStatusStream<StorageSummary>({
    path: "/ws/storage-status",
    onMessage: (snapshot) =>
      queryClient.setQueryData(["storage-summary", baseUrl], snapshot),
  });

  const systemQuery = useQuery({
    queryKey: ["system-status", baseUrl],
    queryFn: ({ signal }) => getSystemStatus(baseUrl, fetchWithHeaders, signal),
    refetchInterval: systemStream === "open" ? false : POLL_INTERVAL_MS,
    retry: 2,
  });

  const storageQuery = useQuery({
    queryKey: ["storage-summary", baseUrl],
    queryFn: ({ signal }) => getStorageSummary(baseUrl, fetchWithHeaders, signal),
    refetchInterval: storageStream === "open" ? false : POLL_INTERVAL_MS,
    retry: 2,
  });

  const datasetsQuery = useQuery({
    queryKey: ["local-dataset-storage", baseUrl],
    queryFn: ({ signal }) =>
      listLocalDatasetStorage(baseUrl, fetchWithHeaders, signal),
    refetchInterval: DATASET_REFRESH_MS,
    retry: 1,
  });

  const compositesQuery = useQuery({
    queryKey: ["train-server-composites", baseUrl],
    queryFn: ({ signal }) => listComposites(baseUrl, fetchWithHeaders, signal),
    refetchInterval: DATASET_REFRESH_MS,
    retry: 1,
  });

  const compositeTrashQuery = useQuery({
    queryKey: ["train-server-composite-trash", baseUrl],
    queryFn: ({ signal }) => listCompositeTrash(baseUrl, fetchWithHeaders, signal),
    retry: 1,
  });

  useEffect(() => {
    if (!systemQuery.data || !systemQuery.dataUpdatedAt) return;
    const gpu = gpuMetric(systemQuery.data.gpu, "usage");
    const point: HistoryPoint = {
      time: new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }),
      cpu: firstNumber(systemQuery.data.cpu?.percent) ?? 0,
      memory: firstNumber(systemQuery.data.memory?.percent) ?? 0,
      ...(gpu !== null ? { gpu } : {}),
    };
    setHistory((current) => [...current.slice(-29), point]);
  }, [systemQuery.data, systemQuery.dataUpdatedAt]);

  const status = systemQuery.data;
  const storage = storageQuery.data;
  const rows = datasetsQuery.data ?? [];
  const diskTone = storageTone(storage);
  const gpuUsage = gpuMetric(status?.gpu, "usage");
  const gpuTemperature = gpuMetric(status?.gpu, "temperature");
  const datasetCount = useMemo(() => {
    if (Array.isArray(storage?.datasets)) return storage.datasets.length;
    return firstNumber(storage?.datasets) ?? rows.length;
  }, [storage?.datasets, rows.length]);
  const isRefreshing =
    systemQuery.isFetching || storageQuery.isFetching || datasetsQuery.isFetching;

  const refreshAll = () => {
    void systemQuery.refetch();
    void storageQuery.refetch();
    void datasetsQuery.refetch();
  };

  const handleSyncDataset = async (row: LocalDatasetStorageResult) => {
    const repoId = row.dataset.repo_id;
    setPendingDataset(repoId);
    try {
      const result = await syncDatasetToTrain(baseUrl, fetchWithHeaders, repoId);
      toast.success(`已开始同步 ${repoId}`, {
        description: result.sync.paths?.target
          ? `目标：${result.sync.paths.target}`
          : "已排入训练机传输队列",
      });
      void datasetsQuery.refetch();
    } catch (error) {
      toast.error("同步启动失败", {
        description: error instanceof Error ? error.message : "未知错误",
      });
    } finally {
      setPendingDataset(null);
    }
  };

  const handleTrashDataset = async (row: LocalDatasetStorageResult) => {
    const repoId = row.dataset.repo_id;
    if (
      !window.confirm(
        `将 ${repoId} 移动到数据盘 .trash？数据集不会被永久删除，可随时恢复。`
      )
    ) {
      return;
    }
    setPendingDataset(repoId);
    try {
      await trashDataset(baseUrl, fetchWithHeaders, repoId);
      toast.success(`已移动到回收站：${repoId}`);
      void datasetsQuery.refetch();
      void storageQuery.refetch();
    } catch (error) {
      toast.error("移动失败", {
        description: error instanceof Error ? error.message : "未知错误",
      });
    } finally {
      setPendingDataset(null);
    }
  };

  const handleSealDataset = async (repoId: string) => {
    if (
      !window.confirm(
        `封存 ${repoId}？封存后会计算全量 SHA-256，之后禁止录制、重录、移动和删除；训练、查看、同步、校验仍然可用。`
      )
    ) {
      return;
    }
    setPendingDataset(repoId);
    try {
      await sealDataset(baseUrl, fetchWithHeaders, repoId);
      // Hashing a dataset takes a while; poll until the seal completes.
      for (let attempt = 0; attempt < 900; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const { seal } = await getDatasetSealStatus(baseUrl, fetchWithHeaders, repoId);
        if (seal.state === "sealed") {
          toast.success(`已封存 ${repoId}`, {
            description: `manifest ${seal.manifest_sha256?.slice(0, 12) ?? ""}…`,
          });
          break;
        }
        if (seal.state === "failed" || seal.state === "cancelled") {
          throw new Error(seal.error ?? "封存失败");
        }
      }
      void datasetsQuery.refetch();
    } catch (error) {
      toast.error("封存失败", {
        description: error instanceof Error ? error.message : "未知错误",
      });
    } finally {
      setPendingDataset(null);
    }
  };

  const handleUnsealDataset = async (repoId: string) => {
    if (
      !window.confirm(
        `解封 ${repoId}？解封后数据集可以再次录制、重录或被删除，已构建的组合数据集不受影响。`
      )
    ) {
      return;
    }
    setPendingDataset(repoId);
    try {
      await unsealDataset(baseUrl, fetchWithHeaders, repoId);
      toast.success(`已解封 ${repoId}`);
      void datasetsQuery.refetch();
    } catch (error) {
      toast.error("解封失败", {
        description: error instanceof Error ? error.message : "未知错误",
      });
    } finally {
      setPendingDataset(null);
    }
  };

  const handleVerifySeal = async (repoId: string) => {
    setPendingDataset(repoId);
    try {
      const result = await verifyDatasetSeal(baseUrl, fetchWithHeaders, repoId);
      if (result.ok) {
        toast.success(`${repoId} 校验通过`, { description: "所有文件与封存清单一致" });
      } else {
        toast.error(`${repoId} 校验失败`, {
          description: `新增 ${result.added.length} / 缺失 ${result.removed.length} / 变更 ${result.changed.length}`,
        });
      }
    } catch (error) {
      toast.error("校验失败", {
        description: error instanceof Error ? error.message : "未知错误",
      });
    } finally {
      setPendingDataset(null);
    }
  };

  const handleTrashComposite = async (composite: RemoteComposite) => {
    if (
      !window.confirm(
        `将训练机上的组合数据集 ${composite.repo_id} 移到回收站？成员数据集不受影响，可随时恢复或按成员重新构建。`
      )
    ) {
      return;
    }
    try {
      await trashComposite(baseUrl, fetchWithHeaders, composite.repo_id);
      toast.success(`已移到训练机回收站：${composite.repo_id}`);
      void compositesQuery.refetch();
      void compositeTrashQuery.refetch();
    } catch (error) {
      toast.error("移动失败", {
        description: error instanceof Error ? error.message : "未知错误",
      });
    }
  };

  const handleRestoreComposite = async (repoId: string, entry: string) => {
    try {
      await restoreComposite(baseUrl, fetchWithHeaders, repoId, entry);
      toast.success(`已恢复 ${repoId}`);
      void compositesQuery.refetch();
      void compositeTrashQuery.refetch();
    } catch (error) {
      toast.error("恢复失败", {
        description: error instanceof Error ? error.message : "未知错误",
      });
    }
  };

  const datasetSyncState = (
    row: LocalDatasetStorageResult
  ): DatasetSyncState | undefined =>
    (row.dataset as { sync?: DatasetSyncState }).sync;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-30 border-b border-slate-800 bg-slate-950/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              asChild
              variant="ghost"
              size="icon"
              className="shrink-0 text-slate-300 hover:bg-slate-800 hover:text-white"
            >
              <Link to="/" aria-label="返回首页">
                <ArrowLeft />
              </Link>
            </Button>
            <div className="min-w-0">
              <h1 className="truncate text-lg font-semibold">系统与数据存储</h1>
              <p className="truncate text-xs text-slate-500">
                {status?.host || "Jetson"} · {baseUrl}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <div className="hidden items-center gap-2 text-xs text-slate-400 sm:flex">
              <span
                className={cn(
                  "h-2 w-2 rounded-full",
                  systemQuery.isError ? "bg-red-500" : "bg-emerald-500",
                )}
              />
              {systemQuery.isError ? "连接异常" : "每 2 秒刷新"}
            </div>
            <Button
              variant="outline"
              size="sm"
              className="border-slate-700 bg-slate-900 hover:bg-slate-800"
              onClick={refreshAll}
              disabled={isRefreshing}
            >
              <RefreshCw className={cn(isRefreshing && "animate-spin")} />
              刷新
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-6 px-4 py-6">
        {systemQuery.isError && !status && (
          <ErrorPanel
            message={`无法读取系统状态：${systemQuery.error instanceof Error ? systemQuery.error.message : "未知错误"}`}
            onRetry={() => void systemQuery.refetch()}
          />
        )}

        {view !== "storage" && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h2 className="text-lg font-semibold">Jetson 运行状态</h2>
              <p className="text-sm text-slate-500">
                {status
                  ? `${status.platform || "未知平台"} · 运行 ${formatUptime(status.uptime_seconds)}`
                  : "等待设备遥测数据"}
              </p>
            </div>
            <span className="text-xs text-slate-500">
              数据时间：{formatTimestamp(status?.timestamp)}
            </span>
          </div>

          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <MetricCard
              title="CPU"
              value={formatPercent(status?.cpu?.percent)}
              subtitle={`${formatInteger(status?.cpu?.count)} 核 · Load ${firstNumber(status?.cpu?.load_1m)?.toFixed(2) ?? "—"}`}
              percent={firstNumber(status?.cpu?.percent)}
              icon={Cpu}
              tone="blue"
            />
            <MetricCard
              title="内存"
              value={formatPercent(status?.memory?.percent)}
              subtitle={`${formatBytes(status?.memory?.used)} / ${formatBytes(status?.memory?.total)}`}
              percent={firstNumber(status?.memory?.percent)}
              icon={MemoryStick}
              tone="blue"
            />
            <MetricCard
              title="GPU"
              value={formatPercent(gpuUsage)}
              subtitle={`${telemetrySummary(status?.gpu)}${gpuTemperature !== null ? ` · ${gpuTemperature.toFixed(0)}°C` : ""}`}
              percent={gpuUsage}
              icon={Activity}
              tone="green"
            />
            <MetricCard
              title="系统温度"
              value={gpuTemperature !== null ? `${gpuTemperature.toFixed(0)}°C` : "查看传感器"}
              subtitle={telemetrySummary(status?.temperatures)}
              icon={Thermometer}
              tone="amber"
            />
          </div>

          <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
            <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
              <CardHeader className="pb-2">
                <CardTitle className="text-base">最近一分钟负载</CardTitle>
                <CardDescription className="text-slate-500">
                  浏览器保留最近 30 个采样点
                </CardDescription>
              </CardHeader>
              <CardContent className="h-56 pl-1 pr-4">
                {history.length < 2 ? (
                  <div className="flex h-full items-center justify-center text-sm text-slate-500">
                    正在积累实时数据…
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={history} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                      <CartesianGrid stroke="#1e293b" strokeDasharray="3 3" />
                      <XAxis dataKey="time" stroke="#64748b" tick={{ fontSize: 10 }} minTickGap={30} />
                      <YAxis domain={[0, 100]} stroke="#64748b" tick={{ fontSize: 10 }} />
                      <Tooltip
                        contentStyle={{
                          background: "#0f172a",
                          border: "1px solid #334155",
                          borderRadius: 8,
                        }}
                        formatter={(value: number) => `${value.toFixed(1)}%`}
                      />
                      <Legend wrapperStyle={{ fontSize: 12 }} />
                      <Line type="monotone" dataKey="cpu" name="CPU" stroke="#38bdf8" dot={false} isAnimationActive={false} />
                      <Line type="monotone" dataKey="memory" name="内存" stroke="#a78bfa" dot={false} isAnimationActive={false} />
                      {history.some((point) => point.gpu !== undefined) && (
                        <Line type="monotone" dataKey="gpu" name="GPU" stroke="#34d399" dot={false} isAnimationActive={false} />
                      )}
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </CardContent>
            </Card>

            <Card className="border-slate-800 bg-slate-900/70 text-slate-100">
              <CardHeader className="pb-3">
                <CardTitle className="text-base">设备信息</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4 text-sm">
                <div className="flex items-start gap-3">
                  <Server className="mt-0.5 h-4 w-4 text-slate-500" />
                  <div className="min-w-0">
                    <div className="text-slate-500">主机</div>
                    <div className="break-all">{status?.host || "—"}</div>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <Wifi className="mt-0.5 h-4 w-4 text-slate-500" />
                  <div className="min-w-0">
                    <div className="text-slate-500">网络</div>
                    <div className="break-all">{telemetrySummary(status?.network)}</div>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <HardDrive className="mt-0.5 h-4 w-4 text-slate-500" />
                  <div className="min-w-0">
                    <div className="text-slate-500">系统磁盘</div>
                    <div className="break-all">{status?.disk?.path || "—"}</div>
                    <div className="mt-1 text-xs text-slate-500">
                      {formatBytes(status?.disk?.free)} 可用
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </section>
        )}

        {view !== "storage" && <TrainServerCard />}

        {view !== "system" && (
        <>
        <section className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold">数据存储</h2>
            <p className="text-sm text-slate-500">
              Jetson 上的数据集为主副本；训练时再同步到远端服务器
            </p>
          </div>

          {storageQuery.isError && !storage && (
            <ErrorPanel
              message={`无法读取存储摘要：${storageQuery.error instanceof Error ? storageQuery.error.message : "未知错误"}`}
              onRetry={() => void storageQuery.refetch()}
            />
          )}

          <Card
            className={cn(
              "border-slate-800 bg-slate-900/70 text-slate-100",
              diskTone === "amber" && "border-amber-700/80",
              diskTone === "red" && "border-red-700/80",
            )}
          >
            <CardContent className="grid gap-6 p-5 lg:grid-cols-[2fr_1fr]">
              <div className="space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="text-sm text-slate-500">数据根目录</div>
                    <div className="break-all font-mono text-sm">
                      {storage?.root || "—"}
                    </div>
                  </div>
                  <Badge
                    variant="outline"
                    className={cn(
                      "border-emerald-700 text-emerald-300",
                      diskTone === "amber" && "border-amber-700 text-amber-300",
                      diskTone === "red" && "border-red-700 text-red-300",
                    )}
                  >
                    {storage?.warning_level || (diskTone === "green" ? "正常" : diskTone === "amber" ? "注意" : "空间紧张")}
                  </Badge>
                </div>
                <ProgressBar value={storage?.percent_used} tone={diskTone} />
                <div className="flex flex-wrap justify-between gap-2 text-xs text-slate-500">
                  <span>
                    已用 {formatBytes(storage?.used_bytes)}（{formatPercent(storage?.percent_used)}）
                  </span>
                  <span>可用 {formatBytes(storage?.free_bytes)}</span>
                </div>
                {diskTone !== "green" && (
                  <div
                    className={cn(
                      "rounded-md border px-3 py-2 text-sm",
                      diskTone === "amber"
                        ? "border-amber-800 bg-amber-950/30 text-amber-200"
                        : "border-red-800 bg-red-950/30 text-red-200",
                    )}
                  >
                    {diskTone === "red"
                      ? "剩余空间较低，请先清理或迁移数据；低于安全阈值时不要开始新录制。"
                      : "存储使用率已超过 80%，建议安排同步或清理。"}
                  </div>
                )}
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="rounded-lg border border-slate-800 bg-slate-950/70 p-4">
                  <Database className="mb-3 h-5 w-5 text-sky-400" />
                  <div className="text-2xl font-semibold">{formatInteger(datasetCount)}</div>
                  <div className="mt-1 text-xs text-slate-500">本地数据集</div>
                </div>
                <div className="rounded-lg border border-slate-800 bg-slate-950/70 p-4">
                  <HardDrive className="mb-3 h-5 w-5 text-emerald-400" />
                  <div className="text-2xl font-semibold">
                    {formatBytes(storage?.total_dataset_bytes)}
                  </div>
                  <div className="mt-1 text-xs text-slate-500">数据集占用</div>
                </div>
              </div>
            </CardContent>
          </Card>
        </section>

        <section className="space-y-3 pb-10">
          <div className="flex items-end justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">本地数据集</h2>
              <p className="text-sm text-slate-500">
                查看 LeRobot 元数据、Features、路径模板和 Parquet Schema
              </p>
            </div>
            <span className="text-xs text-slate-500">每 15 秒更新</span>
          </div>

          {datasetsQuery.isError ? (
            <ErrorPanel
              message={`无法读取数据集：${datasetsQuery.error instanceof Error ? datasetsQuery.error.message : "未知错误"}`}
              onRetry={() => void datasetsQuery.refetch()}
            />
          ) : (
            <div className="overflow-hidden rounded-lg border border-slate-800 bg-slate-900/70">
              <div className="overflow-x-auto">
                <table className="w-full min-w-[760px] text-left text-sm">
                  <thead className="border-b border-slate-800 bg-slate-900 text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-4 py-3 font-medium">数据集</th>
                      <th className="px-4 py-3 font-medium">大小</th>
                      <th className="px-4 py-3 font-medium">Episodes</th>
                      <th className="px-4 py-3 font-medium">Frames</th>
                      <th className="px-4 py-3 font-medium">格式</th>
                      <th className="px-4 py-3 font-medium">封存</th>
                      <th className="px-4 py-3 font-medium">训练机同步</th>
                      <th className="px-4 py-3 text-right font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800">
                    {datasetsQuery.isLoading ? (
                      <tr>
                        <td colSpan={8} className="px-4 py-12 text-center text-slate-500">
                          正在扫描本地数据集…
                        </td>
                      </tr>
                    ) : rows.length === 0 ? (
                      <tr>
                        <td colSpan={8} className="px-4 py-12 text-center text-slate-500">
                          Jetson 数据目录中暂未发现本地数据集
                        </td>
                      </tr>
                    ) : (
                      rows.map((row) => (
                        <tr key={row.dataset.repo_id} className="hover:bg-slate-800/40">
                          <td className="max-w-[280px] px-4 py-3">
                            <div className="truncate font-medium text-slate-200" title={row.dataset.repo_id}>
                              {row.dataset.repo_id}
                            </div>
                            <div className="mt-1 truncate text-xs text-slate-500" title={datasetPath(row.detail)}>
                              {row.error ? "详情接口暂不可用" : datasetPath(row.detail)}
                            </div>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-slate-300">
                            {formatBytes(datasetSize(row.detail))}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-slate-300">
                            {formatInteger(datasetEpisodes(row.detail))}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-slate-300">
                            {formatInteger(datasetFrames(row.detail))}
                          </td>
                          <td className="max-w-[180px] px-4 py-3 text-slate-300">
                            <span className="line-clamp-2">{datasetFormat(row.detail)}</span>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            {row.dataset.sealed ? (
                              <Badge
                                className="border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
                                title={`封存于 ${row.dataset.sealed_at ?? "未知时间"}`}
                              >
                                <Lock className="mr-1 h-3 w-3" />
                                已封存
                              </Badge>
                            ) : (
                              <Badge
                                className="border-slate-600 bg-slate-800/60 text-slate-400"
                                title="未封存：可以继续录制、重录和删除"
                              >
                                未封存
                              </Badge>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            <SyncStateChip sync={datasetSyncState(row)} />
                          </td>
                          <td className="px-4 py-3 text-right">
                            <div className="flex justify-end gap-2">
                              {row.dataset.sealed ? (
                                <>
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    className="border-slate-700 bg-transparent hover:bg-slate-800"
                                    onClick={() => void handleVerifySeal(row.dataset.repo_id)}
                                    disabled={pendingDataset === row.dataset.repo_id}
                                    title="重新计算 SHA-256 并与封存清单比对"
                                  >
                                    校验
                                  </Button>
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    className="border-slate-700 bg-transparent hover:bg-slate-800"
                                    onClick={() => void handleUnsealDataset(row.dataset.repo_id)}
                                    disabled={pendingDataset === row.dataset.repo_id}
                                    title="解封后可以再次修改数据集"
                                  >
                                    <Unlock className="h-4 w-4" />
                                  </Button>
                                </>
                              ) : (
                                <Button
                                  variant="outline"
                                  size="sm"
                                  className="border-emerald-700/60 bg-transparent text-emerald-200 hover:bg-emerald-500/10"
                                  onClick={() => void handleSealDataset(row.dataset.repo_id)}
                                  disabled={
                                    pendingDataset === row.dataset.repo_id ||
                                    row.dataset.finalized === false
                                  }
                                  title="封存：计算全量 SHA-256，禁止继续录制/重录/删除"
                                >
                                  <Lock className="h-4 w-4" />
                                  封存
                                </Button>
                              )}
                              <Button
                                variant="outline"
                                size="sm"
                                className="border-slate-700 bg-transparent hover:bg-slate-800"
                                onClick={() => void handleSyncDataset(row)}
                                disabled={pendingDataset === row.dataset.repo_id}
                                title="通过 SSH 增量同步到训练机"
                              >
                                <CloudUpload className="h-4 w-4" />
                                同步
                              </Button>
                              <Button
                                variant="outline"
                                size="sm"
                                className="border-slate-700 bg-transparent hover:bg-slate-800"
                                onClick={() => setSelectedDataset(row)}
                              >
                                查看详情
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="text-slate-400 hover:bg-red-500/10 hover:text-red-300"
                                onClick={() => void handleTrashDataset(row)}
                                disabled={pendingDataset === row.dataset.repo_id}
                                title="移动到数据盘回收站（可恢复）"
                              >
                                <Trash2 className="h-4 w-4" />
                              </Button>
                            </div>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </section>

        <section className="space-y-3 pb-10">
          <div className="flex items-end justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold">组合数据集（训练机）</h2>
              <p className="text-sm text-slate-500">
                多个已封存数据集合并出的只读数据集；成员数据集不被修改，可回收后重新构建
              </p>
            </div>
            <span className="text-xs text-slate-500">每 15 秒更新</span>
          </div>

          {compositesQuery.isError ? (
            <ErrorPanel
              message={`无法读取训练机组合数据集：${
                compositesQuery.error instanceof Error
                  ? compositesQuery.error.message
                  : "未知错误"
              }`}
              onRetry={() => void compositesQuery.refetch()}
            />
          ) : compositesQuery.isLoading ? (
            <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-10 text-center text-slate-500">
              正在读取训练机…
            </div>
          ) : (compositesQuery.data ?? []).length === 0 ? (
            <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-10 text-center text-slate-500">
              训练机上暂无组合数据集。在训练配置页选择 2 个以上已封存数据集即可构建。
            </div>
          ) : (
            <div className="space-y-3">
              {(compositesQuery.data ?? []).map((composite) => (
                <div
                  key={composite.repo_id}
                  className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-900/70 p-4"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <Layers className="h-4 w-4 text-sky-400" />
                      <span className="truncate font-medium text-slate-200">
                        {composite.repo_id}
                      </span>
                      <Badge className="border-emerald-500/40 bg-emerald-500/10 text-emerald-200">
                        已就绪
                      </Badge>
                      {composite.weights_mode && composite.weights_mode !== "frames" && (
                        <Badge className="border-sky-500/40 bg-sky-500/10 text-sky-200">
                          {composite.weights_mode === "equal" ? "等权采样" : "自定义权重"}
                        </Badge>
                      )}
                    </div>
                    <div className="mt-1 truncate text-xs text-slate-500" title={composite.members.join(", ")}>
                      成员：{composite.members.join(" · ")}
                    </div>
                  </div>
                  <div className="flex items-center gap-3 text-xs text-slate-400">
                    <span>{formatBytes(composite.size_bytes ?? composite.estimated_size_bytes)}</span>
                    {composite.built_at ? (
                      <span>
                        {new Date(composite.built_at * 1000).toLocaleString([], {
                          month: "2-digit",
                          day: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                    ) : null}
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-slate-400 hover:bg-red-500/10 hover:text-red-300"
                      onClick={() => void handleTrashComposite(composite)}
                      title="移动到训练机回收站（可恢复）"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {(compositeTrashQuery.data ?? []).length > 0 && (
            <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
              <div className="mb-2 text-sm font-medium text-slate-300">训练机回收站</div>
              <div className="space-y-2">
                {(compositeTrashQuery.data ?? []).map((entry) => (
                  <div
                    key={entry.entry}
                    className="flex items-center justify-between gap-3 text-xs text-slate-400"
                  >
                    <span className="truncate" title={`${entry.repo_id} · ${entry.trashed_at ?? ""}`}>
                      {entry.repo_id} · {entry.entry}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      className="border-slate-700 bg-transparent hover:bg-slate-800"
                      onClick={() => void handleRestoreComposite(entry.repo_id, entry.entry)}
                    >
                      恢复
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
        </>
        )}
      </main>

      <DatasetDetailDialog
        row={selectedDataset}
        onOpenChange={(open) => !open && setSelectedDataset(null)}
      />
    </div>
  );
};

export default SystemStorage;
