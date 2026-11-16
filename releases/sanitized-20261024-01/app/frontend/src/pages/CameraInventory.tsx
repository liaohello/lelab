import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Camera, Download, RefreshCw, Save } from "lucide-react";
import { Link } from "react-router-dom";
import { useApi } from "@/contexts/ApiContext";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

/**
 * Identify physical cameras without opening a recording session. The stable
 * /dev/v4l/by-id path is the inventory key; /dev/videoN is only a runtime slot.
 */

interface CameraPort {
  id: string;
  stable_source: string | null;
  by_id: string | null;
  by_path: string | null;
  device: string | null;
  index: number;
  name: string | null;
  present: boolean;
  camera_key: string;
  label: string;
  confirmed_at: string | null;
  note: string;
}

interface CameraInventoryResponse {
  status: string;
  cameras: CameraPort[];
  saved_at: string | null;
  latest_path: string;
  recommended_keys: string[];
  missing_recommended: string[];
}

const KEY_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,63}$/;
const RECOMMENDED = ["front", "left_wrist", "right_wrist"];

/** Keep operator edits and only add entries for cameras seen for the first time. */
function fillMissing<T>(
  current: Record<string, T>,
  cameras: CameraPort[],
  pick: (camera: CameraPort) => [string, T]
): Record<string, T> {
  const next = { ...current };
  for (const camera of cameras) {
    const [id, value] = pick(camera);
    if (!(id in next)) next[id] = value;
  }
  return next;
}

const CameraInventory: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [inventory, setInventory] = useState<CameraInventoryResponse | null>(null);
  const [cameras, setCameras] = useState<CameraPort[]>([]);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [previewTick, setPreviewTick] = useState(0);
  const initializedRef = useRef(false);
  const inFlightRef = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setLoading(true);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/cameras/inventory`);
      const payload = (await response.json().catch(() => ({}))) as
        | CameraInventoryResponse
        | { detail?: string };
      if (!response.ok || !("cameras" in payload)) {
        throw new Error(
          "detail" in payload && payload.detail
            ? payload.detail
            : "读取相机列表失败"
        );
      }
      setInventory(payload);
      setCameras(payload.cameras);
      const seed = (current: Record<string, string>) =>
        fillMissing(current, payload.cameras, (camera) => [camera.id, camera.camera_key]);
      const seedNotes = (current: Record<string, string>) =>
        fillMissing(current, payload.cameras, (camera) => [camera.id, camera.note]);
      const seedLabels = (current: Record<string, string>) =>
        fillMissing(current, payload.cameras, (camera) => [camera.id, camera.label]);
      const seedConfirmed = (current: Record<string, boolean>) =>
        fillMissing(current, payload.cameras, (camera) => [camera.id, Boolean(camera.confirmed_at)]);

      if (initializedRef.current) {
        setKeys(seed);
        setNotes(seedNotes);
        setLabels(seedLabels);
        setConfirmed(seedConfirmed);
      } else {
        setKeys(seed({}));
        setNotes(seedNotes({}));
        setLabels(seedLabels({}));
        setConfirmed(seedConfirmed({}));
        initializedRef.current = true;
      }
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    void refresh();
    if (!autoRefresh) return;
    const timer = window.setInterval(() => void refresh(), 2000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, refresh]);

  useEffect(() => {
    if (!selected) return;
    const timer = window.setInterval(() => setPreviewTick((value) => value + 1), 15000);
    return () => window.clearInterval(timer);
  }, [selected]);

  const selectedCamera = useMemo(
    () => cameras.find((camera) => camera.id === selected) ?? null,
    [cameras, selected]
  );
  const assignedKeys = useMemo(
    () => new Set(Object.values(keys).filter((key) => key !== "unassigned")),
    [keys]
  );
  const missingRecommended = RECOMMENDED.filter((key) => !assignedKeys.has(key));
  const duplicateKeys = useMemo(() => {
    const values = Object.values(keys).filter((key) => key && key !== "unassigned");
    return [...new Set(values.filter((key) => values.indexOf(key) !== values.lastIndexOf(key)))];
  }, [keys]);
  const invalidKey = Object.values(keys).some(
    (key) => key !== "unassigned" && !KEY_RE.test(key)
  );

  const changeKey = (camera: CameraPort, key: string) => {
    setKeys((current) => ({ ...current, [camera.id]: key }));
    setDirty(true);
    setMessage(null);
  };

  const save = async () => {
    if (duplicateKeys.length || invalidKey) return;
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/cameras/inventory/save`, {
        method: "POST",
        body: JSON.stringify({
          assignments: keys,
          notes,
          labels,
          confirmations: confirmed,
        }),
      });
      const payload = (await response.json().catch(() => ({}))) as {
        detail?: string;
        saved_path?: string;
        missing_recommended?: string[];
        cameras?: CameraPort[];
      };
      if (!response.ok) throw new Error(payload.detail ?? "保存相机映射失败");
      if (payload.cameras) {
        setKeys(
          Object.fromEntries(payload.cameras.map((camera) => [camera.id, camera.camera_key]))
        );
        setLabels(
          Object.fromEntries(payload.cameras.map((camera) => [camera.id, camera.label]))
        );
        setConfirmed(
          Object.fromEntries(
            payload.cameras.map((camera) => [camera.id, Boolean(camera.confirmed_at)])
          )
        );
        setNotes(Object.fromEntries(payload.cameras.map((camera) => [camera.id, camera.note])));
      }
      setDirty(false);
      setMessage(
        payload.missing_recommended?.length
          ? `已保存：${payload.saved_path}（仍缺 ${payload.missing_recommended.join("、")}）`
          : `已保存：${payload.saved_path}`
      );
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  const download = async () => {
    setDownloading(true);
    setError(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/cameras/inventory/latest`);
      if (!response.ok) throw new Error("尚无已保存的相机映射");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "camera-inventory-latest.json";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="min-h-screen bg-black p-6 text-gray-100">
      <div className="mx-auto max-w-7xl space-y-5">
        <Button asChild variant="ghost" size="sm" className="px-2 text-gray-400 hover:text-white">
          <Link to="/">
            <ArrowLeft className="mr-1 h-4 w-4" />
            返回主界面
          </Link>
        </Button>

        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold">
              <Camera className="h-6 w-6 text-blue-400" />
              相机识别与映射
            </h1>
            <p className="mt-1 max-w-3xl text-sm text-gray-400">
              使用 <span className="font-mono">/dev/v4l/by-id</span> 稳定路径确认物理机位；
              选择一路后点预览，只有预览页面会临时打开该相机。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => void refresh()} disabled={loading}>
              <RefreshCw className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`} />
              刷新
            </Button>
            <Button
              onClick={() => void save()}
              disabled={saving || !cameras.length || duplicateKeys.length > 0 || invalidKey}
            >
              <Save className="mr-1 h-4 w-4" />
              保存映射
            </Button>
            <Button variant="outline" onClick={() => void download()} disabled={downloading || !inventory?.latest_path}>
              <Download className="mr-1 h-4 w-4" />
              下载 JSON
            </Button>
          </div>
        </div>

        <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge className="bg-emerald-950 text-emerald-300">
              检测到 {cameras.filter((camera) => camera.present).length} 路
            </Badge>
            {missingRecommended.length ? (
              <Badge className="bg-amber-950 text-amber-300">
                缺少：{missingRecommended.join("、")}
              </Badge>
            ) : (
              <Badge className="bg-emerald-600 text-white">front/left/right 已配置</Badge>
            )}
            {duplicateKeys.length ? (
              <Badge className="bg-red-950 text-red-300">重复：{duplicateKeys.join("、")}</Badge>
            ) : null}
            {dirty ? <Badge className="bg-amber-600 text-white">未保存</Badge> : null}
            <label className="ml-auto flex items-center gap-2 text-xs text-gray-400">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(event) => setAutoRefresh(event.target.checked)}
              />
              自动刷新（2 秒）
            </label>
          </div>
          <div className="mt-2 break-all font-mono text-xs text-gray-500">
            映射文件：{inventory?.latest_path || "—"}
          </div>
        </div>

        {error ? (
          <div className="rounded border border-red-800 bg-red-950 p-3 text-sm text-red-200">
            {error}
          </div>
        ) : null}
        {message ? (
          <div className="break-all rounded border border-emerald-800 bg-emerald-950 p-3 text-sm text-emerald-200">
            {message}
          </div>
        ) : null}

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_420px]">
          <div className="space-y-3">
            {cameras.map((camera) => {
              const key = keys[camera.id] ?? camera.camera_key;
              const selectedHere = selected === camera.id;
              return (
                <div
                  key={camera.id}
                  className={`rounded-lg border p-4 ${
                    selectedHere ? "border-blue-600 bg-blue-950/20" : "border-gray-800 bg-gray-950"
                  }`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span
                          className={`h-2.5 w-2.5 rounded-full ${
                            camera.present ? "bg-emerald-400" : "bg-amber-500"
                          }`}
                        />
                        <span className="text-sm font-medium text-gray-100">{camera.name}</span>
                        <Badge className="bg-gray-800 text-gray-300">video{camera.index}</Badge>
                        {camera.present ? null : (
                          <Badge className="bg-amber-950 text-amber-300">当前未连接</Badge>
                        )}
                      </div>
                      <div className="mt-1 break-all font-mono text-xs text-gray-500">
                        {camera.by_id || camera.by_path || camera.id}
                      </div>
                      <div className="mt-1 break-all font-mono text-xs text-gray-600">
                        {camera.device}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant={selectedHere ? "default" : "outline"}
                      onClick={() => setSelected(selectedHere ? null : camera.id)}
                      disabled={!camera.present}
                    >
                      {selectedHere ? "停止预览" : "预览"}
                    </Button>
                  </div>
                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    <label className="text-xs text-gray-500">
                      数据字段（observation.images.…）
                      <input
                        value={key}
                        onChange={(event) => changeKey(camera, event.target.value)}
                        className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-sm"
                        placeholder="left_wrist"
                      />
                    </label>
                    <label className="text-xs text-gray-500">
                      画面显示名称（只给操作员看）
                      <input
                        value={labels[camera.id] ?? camera.label}
                        onChange={(event) => {
                          setLabels((current) => ({
                            ...current,
                            [camera.id]: event.target.value,
                          }));
                          setDirty(true);
                        }}
                        maxLength={60}
                        className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 text-sm"
                        placeholder="左手腕相机"
                      />
                    </label>
                    <label className="text-xs text-gray-500 sm:col-span-2">
                      备注
                      <input
                        value={notes[camera.id] ?? camera.note}
                        onChange={(event) => {
                          setNotes((current) => ({ ...current, [camera.id]: event.target.value }));
                          setDirty(true);
                        }}
                        maxLength={200}
                        className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 text-sm"
                        placeholder="左腕 / 桌面视角"
                      />
                    </label>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {["front", "left_wrist", "right_wrist", "up", "unassigned"].map((candidate) => (
                      <Button
                        key={candidate}
                        size="sm"
                        variant="ghost"
                        onClick={() => changeKey(camera, candidate)}
                        className="h-7 px-2 text-xs text-gray-400 hover:text-white"
                      >
                        {candidate}
                      </Button>
                    ))}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-gray-900 pt-2">
                    <label
                      className={`flex items-center gap-2 text-xs ${
                        camera.present && key !== "unassigned"
                          ? "text-gray-200"
                          : "text-gray-600"
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={Boolean(confirmed[camera.id])}
                        disabled={!camera.present || key === "unassigned"}
                        onChange={(event) => {
                          setConfirmed((current) => ({
                            ...current,
                            [camera.id]: event.target.checked,
                          }));
                          setDirty(true);
                        }}
                      />
                      我已逐台遮挡/指向，确认预览就是这台相机
                    </label>
                    <span className="font-mono text-[11px] text-gray-500">
                      {camera.confirmed_at ? `已确认 ${camera.confirmed_at}` : "画面未确认"}
                      {camera.present ? null : " · 设备当前未连接"}
                    </span>
                  </div>
                </div>
              );
            })}
            {!cameras.length ? (
              <div className="rounded border border-gray-800 bg-gray-950 p-4 text-sm text-gray-400">
                未发现稳定相机节点。请确认 USB 相机已接通；识别页不会尝试打开设备。
              </div>
            ) : null}
          </div>

          <div className="h-fit rounded-lg border border-gray-800 bg-gray-950 p-4">
            <div className="text-sm font-medium">单路预览</div>
            {selectedCamera ? (
              <>
                <div className="mt-1 break-all font-mono text-xs text-gray-500">
                  {selectedCamera.name} · {selectedCamera.device}
                </div>
                <img
                  alt={`preview ${selectedCamera.name}`}
                  className="mt-3 aspect-video w-full rounded border border-gray-800 bg-black object-cover"
                  src={`${baseUrl}/camera-preview/${selectedCamera.index}?fps=10&tick=${previewTick}`}
                />
              </>
            ) : (
              <div className="mt-3 flex aspect-video items-center justify-center rounded border border-dashed border-gray-800 text-sm text-gray-500">
                选择一路相机后在此预览
              </div>
            )}
            <div className="mt-3 space-y-2 text-xs text-gray-500">
              <p>验证方法：遮住或指向当前相机，只有对应画面变化才算同一设备。</p>
              <p>预览只临时打开所选相机；录制、推理和观测启动前会自动释放预览。</p>
              <p>保存后请记录设备路径与键名；拔插 USB 后仍可追踪。</p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default CameraInventory;
