import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, AlertTriangle, Camera, Download, Loader2, StopCircle } from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

function Pill({ tone, children }: { tone: "ok" | "warn" | "bad" | "idle"; children: React.ReactNode }) {
  const cls = {
    ok: "bg-emerald-900/50 text-emerald-300 border-emerald-700",
    warn: "bg-amber-900/40 text-amber-300 border-amber-700",
    bad: "bg-red-900/50 text-red-300 border-red-700",
    idle: "bg-gray-900 text-gray-400 border-gray-700",
  }[tone];
  return <span className={`rounded border px-2 py-0.5 text-xs ${cls}`}>{children}</span>;
}

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-200">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  );
}

type FrameSize = { width: number; height: number };

/**
 * 小车实机只读采集页。
 *
 * 这一页只做三件事：显示车端真实相机的帧、显示帧龄/尺寸与传感器健康、
 * 以及在只读模式下落一次原始 session。页面不提供任何底盘/机械臂/夹爪入口，
 * 也不把"画面来了"当成"采集成功"。
 */
export default function RosHardware() {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [overview, setOverview] = useState<any>(null);
  const [snapshots, setSnapshots] = useState<any[]>([]);
  const [lastSnapshot, setLastSnapshot] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [frame, setFrame] = useState<(FrameSize & { receivedAt: number }) | null>(null);
  const mounted = useRef(true);

  const get = useCallback(
    async (path: string) => {
      try {
        return await apiRequest<any>(baseUrl, fetchWithHeaders, path, { method: "GET" });
      } catch (exc: any) {
        setError(exc?.detail || exc?.message || String(exc));
        return null;
      }
    },
    [baseUrl, fetchWithHeaders]
  );

  const post = useCallback(
    async (path: string, body: any, action: string) => {
      setError(null);
      try {
        return await apiRequest<any>(baseUrl, fetchWithHeaders, path, { method: "POST", body: body ?? {}, action });
      } catch (exc: any) {
        setError(exc?.detail || exc?.message || String(exc));
        return null;
      }
    },
    [baseUrl, fetchWithHeaders]
  );

  const loadOverview = useCallback(async () => {
    const out = await get("/ros-mobile/hardware");
    if (out && mounted.current) setOverview(out);
  }, [get]);

  const loadSnapshots = useCallback(async () => {
    const out = await get("/ros-mobile/hardware/snapshots?limit=8");
    if (out && mounted.current) setSnapshots(out.snapshots || []);
  }, [get]);

  useEffect(() => {
    mounted.current = true;
    loadOverview();
    loadSnapshots();
    const statusTimer = window.setInterval(loadOverview, 2000);
    const snapshotTimer = window.setInterval(loadSnapshots, 8000);
    const frameTimer = window.setInterval(() => setTick((value) => value + 1), 1000);
    return () => {
      mounted.current = false;
      window.clearInterval(statusTimer);
      window.clearInterval(snapshotTimer);
      window.clearInterval(frameTimer);
    };
  }, [loadOverview, loadSnapshots]);

  const camera = overview?.camera;
  const recording = overview?.recording;
  const agentOk = Boolean(overview?.agent_ok);
  const readOnly = overview?.read_only !== false;
  const cameraReady = Boolean(camera?.topic) && camera?.stale === false;
  const expected = useMemo(() => {
    if (!camera?.expected_width || !camera?.expected_height) return null;
    return `${camera.expected_width}×${camera.expected_height}`;
  }, [camera]);

  const run = async (key: string, path: string, body: any, label: string) => {
    setBusy(key);
    const out = await post(path, body, label);
    setBusy(null);
    if (out) await loadOverview();
  };

  const captureSnapshot = async () => {
    setBusy("snapshot");
    const out = await post("/ros-mobile/hardware/snapshot", {}, "采集证据快照");
    setBusy(null);
    if (out) {
      setLastSnapshot(out);
      await loadSnapshots();
    }
  };

  const recActive = Boolean(recording?.active ?? recording?.session_id);
  const episodeOpen = Boolean(recording?.current_episode ?? recording?.episode_id);

  return (
    <div className="min-h-screen bg-black text-gray-100">
      <div className="mx-auto max-w-7xl px-4 py-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <Camera className="h-5 w-5 text-emerald-400" />
            <h1 className="text-lg font-semibold">小车实机只读采集</h1>
            <Pill tone={agentOk ? "ok" : "bad"}>{agentOk ? "车端代理在线" : "车端代理离线"}</Pill>
            <Pill tone="warn">无执行器输出</Pill>
            <Pill tone={readOnly ? "warn" : "bad"}>{readOnly ? "hardware_readonly" : "非只读"}</Pill>
            {overview?.echoes?.training_export === false && <Pill tone="idle">训练导出已拒绝</Pill>}
          </div>
          <div className="flex gap-2">
            <Link to="/ros-mobile" className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800">ROS 控制台</Link>
            <Link to="/dataset-viewer" className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800">数据集</Link>
            <Link to="/" className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800">返回</Link>
          </div>
        </div>

        {error && (
          <div className="mb-3 flex items-start gap-2 rounded border border-red-800 bg-red-950/50 p-2 text-sm text-red-200">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> <span className="break-all">{error}</span>
          </div>
        )}

        {overview?.agent_error && (
          <div className="mb-3 flex items-start gap-2 rounded border border-amber-800 bg-amber-950/40 p-2 text-sm text-amber-200">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> 车端状态不可用：<span className="break-all">{overview.agent_error}</span>
            <span className="text-amber-300/80">（配置仍从本地 profile 读取，画面与采集不可用）</span>
          </div>
        )}

        <div className="grid gap-3 lg:grid-cols-[1.15fr_0.85fr]">
          <div className="space-y-3">
            <Card
              title="车端真实相机画面"
              right={
                <span className="flex flex-wrap items-center gap-2 text-xs text-gray-500">
                  <Camera className="h-3.5 w-3.5" />
                  {camera?.fps ?? 0} fps · 帧龄 {camera?.age_s ?? "—"}s
                  {frame ? ` · 实际 ${frame.width}×${frame.height}` : ""}
                  {expected ? ` · 配置 ${expected}` : ""}
                  <Pill tone={cameraReady ? "ok" : camera?.topic ? "bad" : "idle"}>
                    {cameraReady ? "画面新鲜" : camera?.topic ? "画面过期" : "相机未配置"}
                  </Pill>
                </span>
              }
            >
              <div className="relative overflow-hidden rounded border border-gray-800 bg-black">
                <img
                  src={`${baseUrl}/ros-mobile/preview.jpg?t=${tick}`}
                  alt="car rgb"
                  className="aspect-[16/10] w-full object-contain"
                  onLoad={(event) => {
                    const image = event.currentTarget;
                    setFrame({ width: image.naturalWidth, height: image.naturalHeight, receivedAt: Date.now() });
                  }}
                  onError={() => setFrame(null)}
                />
                {!cameraReady && (
                  <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-black/70 text-center text-xs text-amber-200">
                    <AlertTriangle className="h-5 w-5" />
                    <span>
                      {camera?.topic ? `无新图像（帧龄 ${camera?.age_s ?? "?"} s），显示的是最后一帧` : "车端未配置真实相机话题"}
                    </span>
                  </div>
                )}
              </div>
              <div className="mt-2 flex flex-wrap gap-3 text-xs text-gray-400">
                <span className="break-all">topic {camera?.topic ?? "—"}</span>
                <span>帧序号 {camera?.preview_seq ?? "—"}</span>
                <span>预览后端 {camera?.preview_backend ?? "—"}</span>
                <span>feature {camera?.feature_key ?? "—"}</span>
                <span>frame {camera?.frame_id ?? "—"}</span>
              </div>
              {frame && expected && `${frame.width}×${frame.height}` !== expected && (
                <div className="mt-2 rounded border border-amber-800/60 bg-amber-950/30 p-2 text-xs text-amber-200">
                  实际帧 {frame.width}×{frame.height} 与 profile 记录的 {expected} 不一致：相机可能被改过配置或换了设备，采集前先核对。
                </div>
              )}
            </Card>

            <Card title="传感器健康" right={<span className="text-xs text-gray-500">{agentOk ? "来自车端 /status" : "车端离线"}</span>}>
              <table className="w-full text-left text-xs">
                <thead className="text-gray-500">
                  <tr>
                    <th className="py-1">项</th>
                    <th className="py-1">话题</th>
                    <th className="py-1">帧龄</th>
                    <th className="py-1">状态</th>
                  </tr>
                </thead>
                <tbody className="font-mono text-gray-300">
                  {(overview?.sensors ?? []).map((row: any) => (
                    <tr key={row.key} className="border-t border-gray-800/70">
                      <td className="py-1 whitespace-nowrap">
                        {row.label}
                        {row.required && <span className="ml-1 text-red-300">*</span>}
                      </td>
                      <td className="py-1 break-all">{row.topic ?? "—"}</td>
                      <td className="py-1">{row.age_s == null ? "—" : `${row.age_s}s`}</td>
                      <td className="py-1">
                        <Pill tone={row.present ? "ok" : row.required ? "bad" : "idle"}>{row.present ? "有数据" : "无数据"}</Pill>
                      </td>
                    </tr>
                  ))}
                  {!(overview?.sensors ?? []).length && (
                    <tr>
                      <td colSpan={4} className="py-2 text-gray-500">车端离线，读不到传感器年龄。</td>
                    </tr>
                  )}
                </tbody>
              </table>
              <p className="mt-2 text-xs text-gray-500">* 为只读 profile 的必需项；缺任一必需项时车端会拒绝开 session。</p>
            </Card>

            <Card title="原始 session（只读）" right={<Pill tone={recActive ? "ok" : "idle"}>{recActive ? "session 进行中" : "无 session"}</Pill>}>
              <div className="flex flex-wrap gap-2">
                <button
                  disabled={!agentOk || !readOnly || recActive || !!busy}
                  onClick={() => run("start-session", "/ros-mobile/recording/start-session", { task: "hardware readonly observation" }, "开始只读 session")}
                  className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium disabled:opacity-40"
                >
                  {busy === "start-session" ? "开始中…" : "开始 session"}
                </button>
                <button
                  disabled={!agentOk || !recActive || episodeOpen || !!busy}
                  onClick={() => run("start-episode", "/ros-mobile/recording/start-episode", { task: "hardware readonly observation" }, "开始 episode")}
                  className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  {busy === "start-episode" ? "开始中…" : "开始 episode"}
                </button>
                <button
                  disabled={!agentOk || !episodeOpen || !!busy}
                  onClick={() => run("end-episode", "/ros-mobile/recording/end-episode", {}, "结束 episode")}
                  className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40"
                >
                  结束并保存本集
                </button>
                <button
                  disabled={!agentOk || !episodeOpen || !!busy}
                  onClick={() => run("discard-episode", "/ros-mobile/recording/discard-episode", { reason: "operator_discard" }, "丢弃本集")}
                  className="rounded border border-amber-700 px-3 py-1.5 text-sm text-amber-200 disabled:opacity-40"
                >
                  丢弃本集
                </button>
                <button
                  disabled={!agentOk || !recActive || !!busy}
                  onClick={() => run("stop-session", "/ros-mobile/recording/stop-session", {}, "停止 session")}
                  className="ml-auto rounded bg-red-600 px-3 py-1.5 text-sm font-semibold disabled:opacity-40"
                >
                  <StopCircle className="mr-1 inline h-4 w-4" />
                  停止 session
                </button>
              </div>
              <div className="mt-2 flex flex-wrap gap-3 text-xs text-gray-400">
                <span>session {recording?.session_id ?? "—"}</span>
                <span>episode {recording?.episode_id ?? "—"}</span>
                <span>已存集数 {recording?.episodes?.length ?? recording?.saved ?? "—"}</span>
                <span>原始存储 {overview?.echoes?.raw_storage ?? "—"}</span>
                <span>磁盘下限 {overview?.echoes?.min_free_bytes ? `${Math.round(overview.echoes.min_free_bytes / 2 ** 30)} GiB` : "—"}</span>
              </div>
              <p className="mt-2 text-xs text-gray-500">
                只读 session 只落原始传感器与事件；成功标签固定为 unknown、训练导出在车端与上位机两侧都会被拒。
              </p>
            </Card>
          </div>

          <div className="space-y-3">
            <Card title="证据快照" right={<Pill tone={lastSnapshot?.has_frame ? "ok" : lastSnapshot ? "bad" : "idle"}>{lastSnapshot ? (lastSnapshot.has_frame ? "已抓帧" : "无帧") : "未采集"}</Pill>}>
              <p className="mb-2 text-xs text-gray-400">
                车端链路会成分钟地掉到 NO-CARRIER。点一次就把真实帧、帧尺寸、话题和当时的传感器年龄一起落盘，事后仍可核对。
              </p>
              <button
                disabled={!agentOk || !!busy}
                onClick={captureSnapshot}
                className="flex items-center gap-1 rounded bg-sky-700 px-3 py-1.5 text-sm font-medium disabled:opacity-40"
              >
                {busy === "snapshot" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
                采集快照
              </button>
              {lastSnapshot && (
                <dl className="mt-2 space-y-1 text-xs text-gray-400">
                  <div className="flex justify-between gap-2"><dt>目录</dt><dd className="break-all text-gray-300">{lastSnapshot.snapshot_id}</dd></div>
                  <div className="flex justify-between gap-2"><dt>JPEG</dt><dd className="font-mono text-gray-300">{lastSnapshot.jpeg_bytes ?? "—"} B · {lastSnapshot.jpeg_width ?? "?"}×{lastSnapshot.jpeg_height ?? "?"}</dd></div>
                  <div className="flex justify-between gap-2"><dt>与 profile 一致</dt><dd className="text-gray-300">{lastSnapshot.size_matches_profile == null ? "无法判断" : lastSnapshot.size_matches_profile ? "是" : "否"}</dd></div>
                  <div className="flex justify-between gap-2"><dt>帧龄</dt><dd className="text-gray-300">{lastSnapshot.camera_age_s ?? "—"} s</dd></div>
                  <div className="flex justify-between gap-2"><dt>sha256</dt><dd className="break-all font-mono text-gray-400">{lastSnapshot.jpeg_sha256 ?? "—"}</dd></div>
                  {lastSnapshot.notes?.length > 0 && (
                    <div className="rounded border border-amber-800/60 bg-amber-950/30 p-1 text-amber-200">{lastSnapshot.notes.join(" / ")}</div>
                  )}
                </dl>
              )}
              <div className="mt-3 space-y-1">
                {snapshots.map((item) => (
                  <div key={item.snapshot_id} className="flex items-center justify-between gap-2 border-t border-gray-800/70 py-1 text-xs">
                    <span className="truncate font-mono text-gray-400">{item.snapshot_id}</span>
                    <span className="shrink-0 text-gray-500">
                      {item.jpeg_width ? `${item.jpeg_width}×${item.jpeg_height}` : "无帧"} · {item.camera_age_s ?? "—"}s ·{" "}
                      {item.size_matches_profile === false ? "尺寸不符" : item.has_frame ? "OK" : "缺帧"}
                    </span>
                  </div>
                ))}
                {!snapshots.length && <div className="text-xs text-gray-500">本机还没有快照。</div>}
              </div>
            </Card>

            <Card title="这份配置能证明什么" right={<Activity className="h-4 w-4 text-gray-500" />}>
              <dl className="space-y-1 text-xs text-gray-400">
                <div className="flex justify-between gap-2"><dt>profile</dt><dd className="break-all text-gray-300">{overview?.profile_path ?? "—"}</dd></div>
                <div className="flex justify-between gap-2"><dt>sha256</dt><dd className="break-all font-mono text-gray-400">{overview?.profile_sha256?.slice(0, 16) ?? "—"}…</dd></div>
                <div className="flex justify-between gap-2"><dt>模式</dt><dd className="text-gray-300">{overview?.mode ?? "—"}</dd></div>
                <div className="flex justify-between gap-2"><dt>执行器输出</dt><dd className="text-gray-300">{overview?.echoes?.actuator_outputs === false ? "关闭（不会发动作）" : "未声明"}</dd></div>
                <div className="flex justify-between gap-2"><dt>训练导出</dt><dd className="text-gray-300">{overview?.echoes?.training_export ? "允许" : "拒绝"}</dd></div>
              </dl>
              {overview?.echoes?.training_export_reason && (
                <p className="mt-2 rounded border border-gray-800 bg-gray-900/60 p-2 text-xs text-gray-400">
                  {overview.echoes.training_export_reason}
                </p>
              )}
              <p className="mt-2 text-xs text-gray-500">
                画面新鲜只说明相机在出图；采集是否成立要看 session 落盘、哈希同步与 bag/sqlite 可读，二者不互相替代。
              </p>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}
