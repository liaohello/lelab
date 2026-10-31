import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Camera, Gamepad2, Loader2, Play, StopCircle, Upload } from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

const BASE_KEYS: Record<string, [number, number, number]> = {
  w: [0.08, 0, 0],
  s: [-0.08, 0, 0],
  a: [0, 0.08, 0],
  d: [0, -0.08, 0],
  q: [0, 0, 0.35],
  e: [0, 0, -0.35],
};

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

export default function RosMobile() {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [status, setStatus] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [sessions, setSessions] = useState<any[]>([]);
  const [selectedSession, setSelectedSession] = useState<string>("");
  const [exportReport, setExportReport] = useState<any>(null);
  const [jointTargets, setJointTargets] = useState<number[]>([0, 0.84, -2.03, -1.19, 0]);
  const [gripperTarget, setGripperTarget] = useState(0);
  const [previewTick, setPreviewTick] = useState(0);
  const keys = useRef<Set<string>>(new Set());
  const zeroSent = useRef(true);
  const refreshing = useRef(false);
  const heartbeating = useRef(false);

  const call = useCallback(async (path: string, body?: any, action?: string) => {
    setError(null);
    try {
      return await apiRequest(baseUrl, fetchWithHeaders, path, { method: "POST", body: body ?? {}, action });
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
      return null;
    }
  }, [baseUrl, fetchWithHeaders]);

  const refresh = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 5000);
    try {
      const data = await apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/status", { action: "刷新 ROS 状态", signal: controller.signal });
      setStatus(data);
    } catch (exc: any) {
      if (exc?.name !== "AbortError") setError(exc?.detail || exc?.message || String(exc));
    } finally {
      window.clearTimeout(timer);
      refreshing.current = false;
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 1200);
    return () => window.clearInterval(id);
  }, [refresh]);

  const controlActive = Boolean(status?.agent?.control?.active);
  const mode = status?.agent?.control?.mode ?? "idle";
  const estop = Boolean(status?.agent?.control?.estop_latched);

  useEffect(() => {
    if (!controlActive) return;
    const id = window.setInterval(async () => {
      if (heartbeating.current) return;
      heartbeating.current = true;
      try { await call("/ros-mobile/control/heartbeat"); } finally { heartbeating.current = false; }
    }, 3000);
    return () => window.clearInterval(id);
  }, [controlActive, call]);

  useEffect(() => {
    const id = window.setInterval(() => setPreviewTick((tick) => tick + 1), 300);
    return () => window.clearInterval(id);
  }, []);

  const sendBase = useCallback(async (vx: number, vy: number, wz: number) => {
    if (!controlActive) return;
    await call("/ros-mobile/control/base", { vx, vy, wz }, "底盘控制");
    zeroSent.current = vx === 0 && vy === 0 && wz === 0;
  }, [call, controlActive]);

  useEffect(() => {
    const down = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if (!(key in BASE_KEYS)) return;
      if ((event.target as HTMLElement)?.tagName === "INPUT") return;
      event.preventDefault();
      keys.current.add(key);
    };
    const up = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if (!(key in BASE_KEYS)) return;
      keys.current.delete(key);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, []);

  useEffect(() => {
    if (!controlActive || mode !== "keyboard") return;
    const id = window.setInterval(() => {
      let vx = 0, vy = 0, wz = 0;
      keys.current.forEach((key) => {
        const v = BASE_KEYS[key];
        if (v) { vx += v[0]; vy += v[1]; wz += v[2]; }
      });
      if (vx === 0 && vy === 0 && wz === 0) {
        if (!zeroSent.current) sendBase(0, 0, 0);
      } else {
        sendBase(vx, vy, wz);
      }
    }, 120);
    return () => {
      window.clearInterval(id);
      sendBase(0, 0, 0);
    };
  }, [controlActive, mode, sendBase]);

  const acquire = async () => {
    setBusy("acquire");
    const out = await call("/ros-mobile/control/acquire", {}, "获取控制权");
    setBusy(null);
    if (out) {
      await call("/ros-mobile/control/mode", { mode: "keyboard" }, "切换键盘模式");
      await refresh();
    }
  };
  const release = async () => { setBusy("release"); await call("/ros-mobile/control/release"); setBusy(null); await refresh(); };
  const setMode = async (next: string) => { setBusy(`mode-${next}`); await call("/ros-mobile/control/mode", { mode: next }, "切换模式"); setBusy(null); await refresh(); };
  const emergency = async () => { await call("/ros-mobile/control/estop", { reason: "web-estop" }, "急停"); await refresh(); };
  const clearEstop = async () => { await call("/ros-mobile/control/clear-estop", {}, "清除急停"); await refresh(); };

  const startSession = async () => {
    setBusy("session");
    await call("/ros-mobile/recording/start-session", {}, "开始采集");
    setBusy(null); await refresh(); await loadSessions();
  };
  const startEpisode = async () => { await call("/ros-mobile/recording/start-episode", { source: "scripted_expert" }, "开始回合"); await refresh(); };
  const endEpisode = async (discarded = false) => { await call(discarded ? "/ros-mobile/recording/discard-episode" : "/ros-mobile/recording/end-episode", { discarded, reason: discarded ? "operator-discard" : "operator-stop" }, "结束回合"); await refresh(); };
  const stopSession = async () => { setBusy("stop-session"); await call("/ros-mobile/recording/stop-session", {}, "停止采集"); setBusy(null); await refresh(); await loadSessions(); };
  const runExpert = async () => {
    setBusy("expert");
    await call("/ros-mobile/control/mode", { mode: "scripted_expert" }, "切换专家模式");
    const out = await call("/ros-mobile/expert/run", {}, "运行专家");
    setBusy(null);
    if (out) await refresh();
  };
  const stopExpert = async () => { await call("/ros-mobile/expert/stop", {}, "停止专家"); await refresh(); };
  const resetTask = async () => { await call("/ros-mobile/task/reset", {}, "复位任务"); await refresh(); };

  const applyArm = async () => {
    await call("/ros-mobile/control/arm", { positions: jointTargets, duration_s: 1.5 }, "关节控制");
    await refresh();
  };
  const applyGripper = async (value: number) => {
    setGripperTarget(value);
    await call("/ros-mobile/control/gripper", { position: value, duration_s: 1.0 }, "夹爪控制");
    await refresh();
  };

  const loadSessions = useCallback(async () => {
    try {
      const data = await apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/sessions", { action: "读取 session" });
      setSessions(data?.sessions || []);
      if (!selectedSession && data?.sessions?.length) {
        setSelectedSession(data.sessions[data.sessions.length - 1].session_id);
      }
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
    }
  }, [baseUrl, fetchWithHeaders, selectedSession]);

  useEffect(() => { loadSessions(); }, [loadSessions]);

  const syncAndExport = async () => {
    if (!selectedSession) return;
    setBusy("sync");
    const synced = await call("/ros-mobile/sync", { session_id: selectedSession }, "同步原始数据");
    setBusy(null);
    if (!synced) return;
    await loadSessions();
    setBusy("export");
    const report = await call("/ros-mobile/export", { session_id: selectedSession }, "导出 LeRobot");
    setBusy(null);
    setExportReport(report);
    await refresh();
  };

  const agent = status?.agent;
  const recording = agent?.recording;
  const expert = agent?.expert;
  const sensor = agent?.sensors;
  const joints = agent?.joints || {};
  const topics = agent?.topics;
  const sessionsForDisplay = useMemo(() => sessions.slice().reverse(), [sessions]);
  const expertRunning = Boolean(expert?.running);
  const previewSrc = `${baseUrl}/ros-mobile/preview.jpg?t=${previewTick}`;

  return (
    <div className="min-h-screen bg-black text-gray-100">
      <div className="mx-auto max-w-7xl px-4 py-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Gamepad2 className="h-5 w-5 text-emerald-400" />
            <h1 className="text-lg font-semibold">ROS 移动操作小车</h1>
            <Pill tone={controlActive ? "ok" : "idle"}>{controlActive ? `已取得控制权 · ${mode}` : "未取得控制权"}</Pill>
            {estop && <Pill tone="bad">急停锁定</Pill>}
            {agent?.safety?.heartbeat_age_s != null && <Pill tone={agent.safety.heartbeat_age_s < 0.6 ? "ok" : "bad"}>watchdog {agent.safety.heartbeat_age_s}s</Pill>}
          </div>
          <div className="flex gap-2">
            <Link to="/dataset-viewer" className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800">数据集</Link>
            <Link to="/" className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-200 hover:bg-gray-800">返回</Link>
          </div>
        </div>

        {error && (
          <div className="mb-3 flex items-start gap-2 rounded border border-red-800 bg-red-950/50 p-2 text-sm text-red-200">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> <span className="break-all">{error}</span>
          </div>
        )}

        <div className="grid gap-3 lg:grid-cols-[1.15fr_0.85fr]">
          <div className="space-y-3">
            <Card title="控制权与安全" right={<span className="text-xs text-gray-500">{agent?.profile}</span>}>
              <div className="flex flex-wrap gap-2">
                <button disabled={!!busy || controlActive} onClick={acquire} className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium disabled:opacity-40">{busy === "acquire" ? "获取中…" : "获取控制权"}</button>
                <button disabled={!controlActive} onClick={release} className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40">释放</button>
                <button disabled={!controlActive || estop} onClick={() => setMode("keyboard")} className={`rounded px-3 py-1.5 text-sm ${mode === "keyboard" ? "bg-sky-700" : "border border-gray-700"}`}>键盘驾驶</button>
                <button disabled={!controlActive || estop || expertRunning} onClick={() => setMode("scripted_expert")} className={`rounded px-3 py-1.5 text-sm ${mode === "scripted_expert" ? "bg-violet-700" : "border border-gray-700"}`}>专家模式</button>
                <button disabled={!controlActive || estop} onClick={() => setMode("idle")} className="rounded border border-gray-700 px-3 py-1.5 text-sm">空闲</button>
                <button disabled={!controlActive} onClick={emergency} className="ml-auto rounded bg-red-600 px-3 py-1.5 text-sm font-semibold">急停</button>
                <button disabled={!controlActive || !estop} onClick={clearEstop} className="rounded border border-amber-700 px-3 py-1.5 text-sm text-amber-200">清除急停</button>
              </div>
              <div className="mt-2 flex flex-wrap gap-3 text-xs text-gray-400">
                <span>控制租约：{status?.bridge?.lease?.lease?.task ?? status?.bridge?.lease?.task ?? "无"}</span>
                <span>客户端：{status?.bridge?.owner ?? "—"}</span>
                <span>拒绝原因：{agent?.control?.last_reject?.reason ?? "无"}</span>
              </div>
            </Card>

            <Card title="腕部 RGB（link4）" right={<span className="flex items-center gap-1 text-xs text-gray-500"><Camera className="h-3.5 w-3.5" />{sensor?.rgb?.fps ?? 0} fps · {sensor?.rgb?.age_s ?? "—"}s</span>}>
              <div className="overflow-hidden rounded border border-gray-800 bg-black">
                <img src={previewSrc} alt="wrist rgb" className="aspect-[4/3] w-full object-contain" />
              </div>
              <div className="mt-2 flex flex-wrap gap-2 text-xs text-gray-400">
                <span>depth {sensor?.depth?.encoding ?? "—"} {sensor?.depth?.width}×{sensor?.depth?.height} / {sensor?.depth?.age_s ?? "—"}s</span>
                <span>lidar {sensor?.lidar?.ranges ?? "—"} / {sensor?.lidar?.age_s ?? "—"}s</span>
                <span>imu {sensor?.imu?.age_s ?? "—"}s</span>
              </div>
            </Card>

            <Card title="状态" right={<span className="text-xs text-gray-500">状态 {agent?.topics?.missing?.length ? "缺话题" : "正常"}</span>}>
              <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
                {["joint1", "joint2", "joint3", "joint4", "joint5"].map((name) => (
                  <div key={name} className="rounded bg-gray-900/70 p-2">
                    <div className="text-xs text-gray-500">{name}</div>
                    <div className="font-mono text-gray-100">{Number(joints[name] ?? 0).toFixed(3)} rad</div>
                  </div>
                ))}
                <div className="rounded bg-gray-900/70 p-2">
                  <div className="text-xs text-gray-500">夹爪主关节</div>
                  <div className="font-mono text-gray-100">{Number(joints["gripper_base_joint"] ?? 0).toFixed(3)} rad</div>
                </div>
                <div className="rounded bg-gray-900/70 p-2">
                  <div className="text-xs text-gray-500">底盘速度</div>
                  <div className="font-mono text-xs text-gray-100">vx {Number(agent?.base?.vx ?? 0).toFixed(3)} / vy {Number(agent?.base?.vy ?? 0).toFixed(3)} / wz {Number(agent?.base?.wz ?? 0).toFixed(3)}</div>
                </div>
                <div className="rounded bg-gray-900/70 p-2">
                  <div className="text-xs text-gray-500">方块 → 容器</div>
                  <div className="font-mono text-xs text-gray-100">{JSON.stringify(agent?.task_state?.cube ?? [])} → {JSON.stringify(agent?.task_state?.container ?? [])}</div>
                </div>
              </div>
              {topics?.missing?.length > 0 && <div className="mt-2 text-xs text-red-300">缺少话题：{topics.missing.join(", ")}</div>}
            </Card>
          </div>

          <div className="space-y-3">
            <Card title="键盘驾驶（Web 来源）" right={<span className="text-xs text-gray-500">{mode === "keyboard" ? "W/S 前后 · A/D 横移 · Q/E 旋转" : "先切到键盘驾驶"}</span>}>
              <div className="text-xs text-gray-400">按住按键持续发送带 TTL 的速度请求；松键、切换模式或失去控制权时发送零速度。watchdog 在 0.3 s 无新命令时停车。</div>
              <div className="mt-2 grid grid-cols-3 gap-2 text-center text-xs">
                <div className="rounded border border-gray-800 p-2">W<br /><span className="text-gray-500">前进</span></div>
                <div className="rounded border border-gray-800 p-2">S<br /><span className="text-gray-500">后退</span></div>
                <div className="rounded border border-gray-800 p-2">A / D<br /><span className="text-gray-500">横移</span></div>
              </div>
            </Card>

            <Card title="机械臂 / 夹爪调试" right={<span className="text-xs text-gray-500">手动命令同样经过网关与 watchdog</span>}>
              {[0, 1, 2, 3, 4].map((index) => (
                <label key={index} className="mb-2 block text-xs text-gray-400">
                  joint{index + 1}: <span className="font-mono text-gray-200">{jointTargets[index].toFixed(2)}</span>
                  <input type="range" min={-2.09} max={2.09} step={0.01} value={jointTargets[index]} onChange={(e) => setJointTargets((prev) => { const next = [...prev]; next[index] = Number(e.target.value); return next; })} className="mt-1 w-full" />
                </label>
              ))}
              <div className="flex flex-wrap gap-2">
                <button disabled={!controlActive || estop || expertRunning} onClick={applyArm} className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40">应用关节目标</button>
                <button disabled={!controlActive || estop || expertRunning} onClick={() => applyGripper(0)} className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40">张开夹爪</button>
                <button disabled={!controlActive || estop || expertRunning} onClick={() => applyGripper(-1.5)} className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40">闭合夹爪</button>
              </div>
              <div className="mt-2 text-xs text-gray-500">当前夹爪目标 {gripperTarget.toFixed(2)} rad（五关节显式映射）</div>
            </Card>

            <Card title="采集与专家" right={expertRunning ? <Pill tone="warn">{expert?.phase}</Pill> : <Pill tone={recording?.active ? "ok" : "idle"}>{recording?.active ? "录制中" : "未录制"}</Pill>}>
              <div className="flex flex-wrap gap-2">
                <button disabled={!controlActive || recording?.active || estop} onClick={startSession} className="rounded bg-emerald-700 px-3 py-1.5 text-sm disabled:opacity-40">开始 session</button>
                <button disabled={!controlActive || !recording?.active || !!recording?.current_episode || estop} onClick={startEpisode} className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40">开始回合</button>
                <button disabled={!recording?.current_episode} onClick={() => endEpisode(false)} className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40">结束回合</button>
                <button disabled={!recording?.current_episode} onClick={() => endEpisode(true)} className="rounded border border-amber-700 px-3 py-1.5 text-sm text-amber-200 disabled:opacity-40">弃用重录</button>
                <button disabled={!recording?.active} onClick={stopSession} className="rounded border border-gray-700 px-3 py-1.5 text-sm disabled:opacity-40">停止 session</button>
                <button disabled={!recording?.active || estop || expertRunning} onClick={runExpert} className="rounded bg-violet-700 px-3 py-1.5 text-sm disabled:opacity-40"><Play className="mr-1 inline h-4 w-4" />运行脚本专家</button>
                <button disabled={!expertRunning} onClick={stopExpert} className="rounded border border-red-700 px-3 py-1.5 text-sm text-red-200 disabled:opacity-40"><StopCircle className="mr-1 inline h-4 w-4" />中止专家</button>
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-gray-400">
                <div>session：<span className="font-mono text-gray-200">{recording?.session_id ?? "—"}</span></div>
                <div>episode：<span className="font-mono text-gray-200">{recording?.episode_id ?? "—"}</span></div>
                <div>阶段：<span className="text-gray-200">{expert?.phase ?? "—"}</span>{expert?.error && <span className="text-red-300"> · {expert.error}</span>}</div>
                <div>最大抬升：<span className="text-gray-200">{Number(recording?.episode_max_lift_m ?? 0).toFixed(3)} m</span></div>
                <div>夹爪接触事件：<span className="text-gray-200">{recording?.episode_contacts ?? 0}</span></div>
                <div>队列/错误：<span className="text-gray-200">{agent?.safety?.rejected ?? 0} rejected</span></div>
              </div>
              {recording?.episodes?.length > 0 && (
                <div className="mt-2 max-h-32 overflow-auto rounded border border-gray-800 p-2 text-xs text-gray-400">
                  {recording.episodes.map((ep: any) => (
                    <div key={ep.episode_id} className="flex justify-between gap-2"><span className="font-mono">{ep.episode_id}</span><span className={ep.evaluation?.success ? "text-emerald-300" : "text-red-300"}>{ep.status} · {ep.evaluation?.success ? "success" : "failed"}</span></div>
                  ))}
                </div>
              )}
            </Card>

            <Card title="同步与 LeRobot 导出" right={<span className="text-xs text-gray-500">{exportReport ? "已导出" : "暂存 → 校验 → 原子发布"}</span>}>
              <select value={selectedSession} onChange={(e) => setSelectedSession(e.target.value)} className="mb-2 w-full rounded border border-gray-700 bg-gray-900 px-2 py-1.5 text-sm">
                <option value="">选择 session</option>
                {sessionsForDisplay.map((s: any) => <option key={s.session_id} value={s.session_id}>{s.session_id} · {s.status}{s.upper?.ready ? " · 已同步" : ""}</option>)}
              </select>
              <div className="flex flex-wrap gap-2">
                <button disabled={!selectedSession || !!busy || recording?.active} onClick={syncAndExport} className="rounded bg-sky-700 px-3 py-1.5 text-sm disabled:opacity-40"><Upload className="mr-1 inline h-4 w-4" />同步并导出</button>
                <button disabled={!!busy} onClick={loadSessions} className="rounded border border-gray-700 px-3 py-1.5 text-sm">刷新列表</button>
                <Link to="/dataset-viewer" className="rounded border border-gray-700 px-3 py-1.5 text-sm">打开数据查看器</Link>
              </div>
              {busy && <div className="mt-2 flex items-center gap-2 text-xs text-gray-400"><Loader2 className="h-3.5 w-3.5 animate-spin" />{busy} 进行中…</div>}
              {exportReport && (
                <div className="mt-2 rounded border border-emerald-800 bg-emerald-950/30 p-2 text-xs text-emerald-200">
                  repo_id {exportReport.repo_id} · {exportReport.total_episodes} episodes · {exportReport.total_frames} frames
                  <div className="font-mono break-all text-emerald-300">{exportReport.dataset_root}</div>
                </div>
              )}
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}
