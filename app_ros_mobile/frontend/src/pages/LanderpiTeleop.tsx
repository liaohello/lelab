import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  Camera,
  Check,
  CircleAlert,
  Hand,
  Link2,
  Loader2,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
  Unplug,
} from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

const PROFILE = "hardware_hybrid_follow_v1";
const JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"] as const;
const LEADER_JOINTS = [
  "shoulder_pan",
  "shoulder_lift",
  "elbow_flex",
  "wrist_flex",
  "wrist_roll",
] as const;
const LABELS: Record<string, string> = {
  joint1: "底座旋转",
  joint2: "肩部",
  joint3: "肘部",
  joint4: "腕俯仰",
  joint5: "腕旋转",
};
const HOME_TARGETS: Record<string, number> = {
  joint1: 531,
  joint2: 492,
  joint3: 525,
  joint4: 502,
  joint5: 510,
  r_joint: 457,
};

function Badge({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs ${
      ok
        ? "border-emerald-700/70 bg-emerald-950/40 text-emerald-300"
        : "border-amber-700/70 bg-amber-950/30 text-amber-300"
    }`}>
      {ok ? <Check className="h-3 w-3" /> : <CircleAlert className="h-3 w-3" />}
      {children}
    </span>
  );
}

function number(value: unknown, digits = 3): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(digits)
    : "—";
}

function faultExplanation(value: unknown): string {
  if (typeof value !== "string" || !value) return "服务端没有提供具体故障原因。";
  const descriptions: Array<[string, string]> = [
    ["follow_feedback_untrusted", "六路从臂反馈在 1.0 秒门限内没有形成一组有效新扫描，跟随已主动停止输出。"],
    ["stale_or_invalid_timestamp", "六轴共用同一次扫描时间戳；时间戳过期或非法时整组反馈会被安全门禁拒绝。"],
    ["frame_follow_failed", "跟随线程触发反馈门禁后主动退出，网页服务本身仍在运行。"],
    ["window_expired", "车端接收窗口已过期，旧目标没有执行；请重新获取会话后再试。"],
    ["frame_admission_timeout", "跟随帧没有在限定时间内获得车端接收确认，输出已停止。"],
    ["frame_stream_error", "跟随帧流链路出现错误，旧目标不会重放。"],
    ["feedback_valid_missing", "六路从臂反馈在检查时缺失、超过 1.0 秒未更新，或未绑定当前标定。"],
    ["leader_connected_missing", "没有检测到 SO-101 主臂串口。"],
    ["leader_healthy_missing", "主臂串口已检测到，但连续读取失败或读数已过期。"],
    ["exclusive_control_missing", "车端驱动或独占控制状态不满足预览条件。"],
    ["lease_lost", "上层或车端控制租约已失效，常见原因是页面关闭或心跳中断。"],
    ["calibration_changed_or_unavailable", "三层标定缺失、读取失败，或标定 bundle 已改变。"],
    ["heartbeat_failed", "会话续租失败，服务端已主动停止逻辑通道。"],
    ["stop_failed", "安全停止请求没有成功确认，旧车端控制 token 可能已经过期。"],
    ["release_failed", "旧会话没有完整释放，需要重新获取会话。"],
    ["session_not_ready", "当前会话不在 ready 状态，不能继续预览或控制。"],
    ["observed_prerequisites_missing", "获取会话时主臂、六路反馈或独占控制条件未全部满足。"],
    ["estop_latched", "急停已经锁存，必须显式清除；重新获取会话不会自动恢复。"],
    ["estop_not_latched", "当前没有锁存急停，无需执行清除操作。"],
    ["clear_estop", "正在清除急停锁存；清除后仍需重新获取会话和对齐。"],
  ];
  return value.split(";").map((part) => {
    const trimmed = part.trim();
    return descriptions.find(([prefix]) => trimmed.includes(prefix))?.[1]
      ?? `未识别的服务端状态：${trimmed}`;
  }).join(" ");
}

function JointRow({
  name,
  leader,
  target,
  follower,
  unclipped,
  clipped,
}: {
  name: string;
  leader: unknown;
  target: unknown;
  follower: unknown;
  unclipped: unknown;
  clipped: boolean | undefined;
}) {
  const leaderValue = typeof leader === "number" ? leader : null;
  const targetValue = typeof target === "number" ? target : null;
  const followerValue = typeof follower === "number" ? follower : null;
  const spread = targetValue != null && followerValue != null
    ? Math.abs(targetValue - followerValue)
    : null;
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-3">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="font-mono text-sm text-slate-200">{name}</p>
          <p className="text-[11px] text-slate-500">{LABELS[name]}</p>
        </div>
        <Badge ok={spread != null && spread < 0.08}>
          {spread == null ? "等待映射" : `预览差值 ${spread.toFixed(3)} rad`}
        </Badge>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-lg bg-slate-950/70 p-2">
          <p className="text-[10px] uppercase tracking-wider text-slate-600">主臂</p>
          <p className="mt-1 font-mono text-sm text-sky-300">{number(leader)}</p>
          <p className="text-[10px] text-slate-600">rad</p>
        </div>
        <div className="rounded-lg bg-slate-950/70 p-2">
          <p className="text-[10px] uppercase tracking-wider text-slate-600">预览目标</p>
          <p className="mt-1 font-mono text-sm text-emerald-300">{number(target)}</p>
          <p className="text-[10px] text-slate-600">rad，未下发</p>
        </div>
        <div className="rounded-lg bg-slate-950/70 p-2">
          <p className="text-[10px] uppercase tracking-wider text-slate-600">从臂实测</p>
          <p className="mt-1 font-mono text-sm text-slate-100">{number(follower)}</p>
          <p className="text-[10px] text-slate-600">真实反馈</p>
        </div>
      </div>
      <p className={`mt-2 text-xs ${clipped ? "text-amber-300" : "text-slate-400"}`}>
        限幅前 {number(unclipped)} rad · {clipped === undefined ? "限幅状态未知" : (clipped ? "已触及限位" : "未触及限位")} · 仅预览
      </p>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800">
        <div
          className="h-full rounded-full bg-gradient-to-r from-sky-500 to-emerald-400"
          style={{ width: `${Math.min(100, Math.max(0, ((leaderValue ?? -2.1) + 2.1) / 4.2 * 100))}%` }}
        />
      </div>
    </div>
  );
}

export default function LanderpiTeleop() {
  const { baseUrl, fetchWithHeaders, clientId } = useApi();
  const [status, setStatus] = useState<any>(null);
  const [capabilities, setCapabilities] = useState<any>(null);
  const [preview, setPreview] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [homeOnlyJoint, setHomeOnlyJoint] = useState<string>("joint1");
  const [estopBusy, setEstopBusy] = useState(false);
  const [mappingConfig, setMappingConfig] = useState<any>(null);
  const [mappingDraft, setMappingDraft] = useState<Record<string, number>>({});
  const [mappingBusy, setMappingBusy] = useState(false);
  const refreshing = useRef(false);
  const heartbeatInFlight = useRef(false);

  const request = useCallback(async (path: string, init?: RequestInit) => {
    return apiRequest<any>(baseUrl, fetchWithHeaders, path, {
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
  }, [baseUrl, fetchWithHeaders]);

  const refresh = useCallback(async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    try {
      const [nextStatus, nextCaps] = await Promise.all([
        request("/ros-mobile/hybrid/status"),
        request("/ros-mobile/hybrid/capabilities"),
      ]);
      setStatus(nextStatus);
      setCapabilities(nextCaps);
      if (nextStatus?.state === "ready" && nextStatus?.hardware_preview?.available) {
        setPreview(nextStatus.hardware_preview);
      } else {
        setPreview(null);
      }
    } catch (exc: any) {
      setError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      refreshing.current = false;
    }
  }, [request]);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => void refresh(), 350);
    return () => window.clearInterval(id);
  }, [refresh]);

  const sessionId = status?.session_id ?? null;
  const sessionEpoch = status?.session_epoch ?? null;
  const sessionBundleHash = status?.calibration_bundle_hash ?? null;
  const statusState = status?.state ?? "disconnected";
  const session = useMemo(() => ({
    session_id: sessionId,
    session_epoch: sessionEpoch,
    calibration_bundle_hash: sessionBundleHash,
  }), [sessionId, sessionEpoch, sessionBundleHash]);
  const hasSession = typeof session.session_id === "string" && session.session_id.length > 0;

  const loadMapping = useCallback(async () => {
    try {
      const mapping = await request("/ros-mobile/hybrid/mapping");
      setMappingConfig(mapping);
      const directions: Record<string, number> = {};
      for (const item of mapping?.joints ?? []) {
        if (typeof item?.follower === "string") directions[item.follower] = item.direction;
      }
      const gripperFollower = mapping?.gripper?.follower ?? "r_joint";
      directions[gripperFollower] = mapping?.gripper?.direction;
      setMappingDraft(directions);
    } catch {
      // Mapping editing is available only with the hardware facade.
    }
  }, [request]);

  useEffect(() => {
    if (!hasSession) void loadMapping();
  }, [hasSession, loadMapping]);

  useEffect(() => {
    if (!hasSession || status?.state !== "ready") return;
    let cancelled = false;
    const heartbeat = async () => {
      if (heartbeatInFlight.current) return;
      heartbeatInFlight.current = true;
      try {
        await request("/ros-mobile/hybrid/heartbeat", {
          method: "POST",
          body: JSON.stringify(session),
        });
      } catch (exc: any) {
        if (!cancelled) setError(exc?.detail ?? exc?.message ?? String(exc));
      } finally {
        heartbeatInFlight.current = false;
      }
    };
    void heartbeat();
    const id = window.setInterval(() => void heartbeat(), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [hasSession, request, session, statusState]);

  const beginPreview = useCallback(async () => {
    const acquired = await request("/ros-mobile/hybrid/session", {
      method: "POST",
      body: JSON.stringify({ action: "acquire", profile: PROFILE, owner: clientId }),
    });
    const identity = {
      session_id: acquired?.session_id,
      session_epoch: acquired?.session_epoch,
      calibration_bundle_hash: acquired?.calibration_bundle_hash,
    };
    await request("/ros-mobile/hybrid/control", {
      method: "POST",
      body: JSON.stringify({ action: "preview", ...identity }),
    });
    await new Promise((resolve) => window.setTimeout(resolve, 150));
    await request("/ros-mobile/hybrid/control", {
      method: "POST",
      body: JSON.stringify({ action: "align", ...identity }),
    });
  }, [clientId, request]);

  const run = useCallback(async (label: string, operation: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    try {
      await operation();
      await refresh();
    } catch (exc: any) {
      setError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(null);
    }
  }, [refresh]);

  const acquire = () => run("获取控制权", async () => {
    await beginPreview();
  });

  const probeLeader = () => run("连接主臂", async () => {
    await request("/ros-mobile/hybrid/probe-leader", {
      method: "POST",
      body: JSON.stringify({}),
    });
  });

  const control = (action: string) => run(action, async () => {
    const body = { action, ...session };
    const out = await request("/ros-mobile/hybrid/control", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (out?.hardware_preview) {
      if (out.hardware_preview.available && statusState === "ready") {
        setPreview({
          ...out.hardware_preview,
          leader: out.leader,
          follower_feedback: out.follower_feedback,
        });
      } else {
        setPreview(null);
      }
    }
  });

  const emergencyStop = useCallback(async () => {
    setEstopBusy(true);
    try {
      await request("/ros-mobile/hybrid/control", {
        method: "POST",
        body: JSON.stringify({ action: "estop", reason: "operator-emergency-stop" }),
      });
      setPreview(null);
    } catch (exc: any) {
      setError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setEstopBusy(false);
    }
  }, [request]);

  const release = () => run("释放会话", async () => {
    await request("/ros-mobile/hybrid/session", {
      method: "POST",
      body: JSON.stringify({ action: "release", ...session, reason: "operator-release" }),
    });
    setPreview(null);
  });

  const startHoming = () => run("从臂回中", async () => {
    await request("/ros-mobile/hybrid/home", {
      method: "POST",
      body: JSON.stringify({
        action: "start",
        ...session,
        ...(homeOnlyJoint ? { only_joint: homeOnlyJoint } : {}),
      }),
    });
  });

  const cancelHoming = () => run("取消回中", async () => {
    await request("/ros-mobile/hybrid/home", {
      method: "POST",
      body: JSON.stringify({ action: "cancel", ...session }),
    });
  });

  const restartSession = () => run("重建会话", async () => {
    if (hasSession) {
      await request("/ros-mobile/hybrid/session", {
        method: "POST",
        body: JSON.stringify({ action: "release", ...session, reason: "operator-recover-fault" }),
      });
    }
    setPreview(null);
    await request("/ros-mobile/hybrid/clear-estop", {
      method: "POST",
      body: JSON.stringify({}),
    });
    await beginPreview();
  });

  const saveMappingDirections = async () => {
    const required = [...JOINTS, "r_joint"];
    if (!required.every((name) => mappingDraft[name] === 1 || mappingDraft[name] === -1)) {
      setError("六个控制轴的映射方向都必须选择正向或反向。");
      return;
    }
    setMappingBusy(true);
    setError(null);
    try {
      const result = await request("/ros-mobile/hybrid/mapping/directions", {
        method: "POST",
        body: JSON.stringify({ directions: mappingDraft }),
      });
      await loadMapping();
      await refresh();
      setError(`映射已保存为版本 ${result?.version ?? "新版本"}，请重新开始无输出联动并记录对齐。`);
    } catch (exc: any) {
      setError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setMappingBusy(false);
    }
  };

  const follow = preview ?? {};
  const leaderRad = follow.leader_rad ?? {};
  const targets = useMemo(() => {
    const map: Record<string, { target: number; unclipped: unknown; clipped: boolean }> = {};
    for (const row of follow.targets ?? []) {
      if (typeof row?.joint === "string" && typeof row?.target_rad === "number") {
        map[row.joint] = { target: row.target_rad, unclipped: row.pre_clip_rad, clipped: row.clipped === true };
      }
    }
    return map;
  }, [follow.targets]);
  const followerRad = follow.follower_rad ?? {};
  const videoSeq = status?.video?.preview_seq;
  const cameraOk = status?.video?.valid === true && status?.video?.stale === false
    && Number.isSafeInteger(videoSeq);
  const calibrationReady = capabilities?.calibration_readiness?.ready === true;
  const leaderState = follow.leader ?? status?.leader ?? {};
  const leaderOk = leaderState.healthy === true;
  const leaderDetected = leaderState.detected === true
    || leaderState.connected === true
    || status?.devices?.leader?.connected === true
    || (typeof leaderState.source === "string" && leaderState.source.length > 0);
  const followerOk = status?.devices?.follower?.driver_bound === true
    && status?.feedback?.valid === true
    && status?.feedback?.trusted === true
    && (status?.feedback?.samples?.length ?? 0) === 6;
  // Before a new epoch, the car intentionally marks samples untrusted until
  // the calibration bundle is bound. Permit an acquire attempt only when the
  // six expected raw servo samples are fresh; the backend still performs the
  // authoritative calibration, identity and trusted-feedback checks.
  const unboundSamples = status?.feedback?.samples;
  const expectedServoIds: Record<string, number> = {
    joint1: 1, joint2: 2, joint3: 3, joint4: 4, joint5: 5, r_joint: 10,
  };
  const followerPendingBinding = !hasSession && Array.isArray(unboundSamples)
    && unboundSamples.length === 6
    && new Set(unboundSamples.map((sample: any) => sample?.name)).size === 6
    && unboundSamples.every((sample: any) => typeof expectedServoIds[sample?.name] === "number"
      && expectedServoIds[sample?.name] === sample?.servo_id
      && sample?.source_kind === "servo_position_readback"
      && typeof sample?.raw_position === "number" && Number.isFinite(sample.raw_position)
      && typeof sample?.age_s === "number" && sample.age_s >= 0 && sample.age_s <= 1.0
      && typeof sample?.error === "string" && sample.error.includes("calibration_not_configured"));
  const outputsOff = capabilities?.outputs_enabled === false && status?.outputs_enabled === false;
  const outputsOn = status?.outputs_enabled === true && status?.state === "ready";
  const frameProcess = status?.frame_process ?? null;
  const armMode = status?.channels?.arm?.state ?? "idle";
  const homeTask = status?.homing ?? null;
  const homeActive = homeTask?.state === "starting" || homeTask?.state === "moving";
  const homeRaw = homeTask?.observed_raw ?? status?.feedback?.servo_pulses ?? {};
  const homeProgress = typeof homeTask?.progress_pct === "number"
    ? Math.min(100, Math.max(0, homeTask.progress_pct))
    : 0;
  const homeTorque = homeTask?.torque ?? null;
  const homeTorqueRows: any[] = Object.values(homeTorque?.results ?? {})
    .filter((row: any) => row && typeof row === "object")
    .sort((a: any, b: any) => Number(a.servo_id ?? 0) - Number(b.servo_id ?? 0));
  const homeTorqueState = typeof homeTorque?.state === "string" ? homeTorque.state : null;
  const homeFeedbackAgeMs = typeof homeTask?.feedback_age_s === "number"
    ? Math.round(homeTask.feedback_age_s * 1000)
    : null;
  const homeFeedbackGeneration = homeTask?.feedback_publisher_generation;
  const homeFeedbackInstance = homeTask?.feedback_publisher_instance;
  const realFollowing = outputsOn && armMode === "following";
  const armedState = typeof status?.state === "string" ? status.state : "disconnected";

  return (
    <div className="min-h-screen bg-[radial-gradient(circle_at_top,_#102033_0,_#070c14_42%,_#030609_100%)] text-slate-100">
      <header className="border-b border-slate-800/80 bg-slate-950/70 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-5 py-4">
          <Link to="/landerpi" className="rounded-lg border border-slate-700 p-2 text-slate-300 hover:bg-slate-900">
            <RotateCcw className="h-4 w-4" />
          </Link>
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-sky-500/15 text-sky-300">
            <Activity className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-lg font-semibold">LanderPi 主从遥操作</h1>
            <p className="text-xs text-slate-500">
              SO-101 主臂 → 树莓派从臂 · {realFollowing ? "真实跟随已接合" : "预览与显式输出门禁"}
            </p>
          </div>
          <Badge ok={leaderOk}>
            主臂 {leaderOk ? "在线" : leaderDetected ? "串口已检测" : "未检测"}
          </Badge>
          <Badge ok={followerOk}>从臂 {followerOk ? "六路反馈" : followerPendingBinding ? "六路待绑定" : "反馈异常"}</Badge>
          <Badge ok={cameraOk}>相机 {cameraOk ? "实时" : "不可用"}</Badge>
          <Badge ok={outputsOff}>{outputsOn ? "执行输出开启" : "执行输出关闭"}</Badge>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-4 px-5 py-5">
        {status?.trial_motion?.allowed_joints?.length > 0 && (
          <div className="rounded-xl border border-amber-700 bg-amber-950/40 p-3 text-sm text-amber-200">
            逐轴试运行：仅 {status.trial_motion.allowed_joints.join(", ")} 可输出；
            单次最大位移 {number(status.trial_motion.max_displacement_rad, 3)} rad，其他轴和夹爪不发送目标。
          </div>
        )}
        {error && (
          <div className="flex items-start gap-2 rounded-xl border border-red-800 bg-red-950/40 p-3 text-sm text-red-200">
            <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="break-all">
              {error}
              <span className="mt-1 block text-xs text-red-300/80">{faultExplanation(error)}</span>
            </span>
            <button type="button" onClick={() => setError(null)} className="ml-auto text-xs underline">关闭</button>
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.35fr)_minmax(300px,0.65fr)]">
          <section className="overflow-hidden rounded-2xl border border-slate-800 bg-black/70 shadow-2xl shadow-black/30">
            <div className="relative aspect-[16/10]">
              {cameraOk ? (
                <img
                  src={`${baseUrl}/ros-mobile/preview.mjpg`}
                  alt="LanderPi 腕部相机实时预览"
                  className="h-full w-full object-cover"
                />
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-amber-300">等待新鲜相机画面</div>
              )}
              <div className="absolute left-3 top-3 flex items-center gap-2 rounded-full bg-black/65 px-3 py-1.5 text-xs text-slate-200 backdrop-blur">
                <Camera className="h-3.5 w-3.5" /> 腕部相机 · {number(status?.video?.age_s, 2)} s
              </div>
              <div className={`absolute right-3 top-3 rounded-full border px-3 py-1.5 text-xs font-medium backdrop-blur ${
                outputsOn
                  ? "border-amber-600/70 bg-amber-950/85 text-amber-200"
                  : "border-emerald-700/70 bg-emerald-950/80 text-emerald-300"
              }`}>
                {outputsOn ? "真实执行输出已开启" : "无执行输出"}
              </div>
              <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 to-transparent px-4 pb-3 pt-12 text-xs text-slate-300">
                六路反馈同步跨度 {number(status?.feedback?.scan_span_s, 3)} s · 相机帧序号 {videoSeq ?? "—"}
              </div>
            </div>
          </section>

          <section className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-slate-600">Session</p>
                <p className="mt-1 font-mono text-sm text-slate-200">{armedState} · {armMode}</p>
              </div>
              <span className="rounded-full border border-slate-700 px-3 py-1 text-xs text-slate-400">
                {hasSession ? "租约已持有" : "未获取"}
              </span>
            </div>

            <div className="mt-4 space-y-2">
              {!hasSession && (
                <button
                  type="button"
                  disabled={!!busy}
                  onClick={() => void run("清除急停", async () => {
                    await request("/ros-mobile/hybrid/clear-estop", {
                      method: "POST",
                      body: JSON.stringify({ reason: "operator-clear-estop" }),
                    });
                  })}
                  className="flex w-full items-center justify-center gap-2 rounded-xl border border-amber-700 bg-amber-950/30 px-4 py-3 text-sm font-medium text-amber-200 disabled:opacity-35"
                >
                  {busy === "清除急停" ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />}
                  清除车端急停
                </button>
              )}
              {hasSession && armedState === "fault" && (
                <div className="rounded-xl border border-red-800 bg-red-950/35 p-3 text-xs text-red-200">
                  <p className="font-medium">当前会话故障，预览不会跟随。</p>
                  <p className="mt-1 text-red-300/80">
                    {status?.primary_error ?? status?.last_error ?? "lease_or_observation_lost"}
                  </p>
                  <p className="mt-1 leading-5 text-red-200/80">
                    {faultExplanation(status?.primary_error ?? status?.last_error ?? "lease_or_observation_lost")}
                  </p>
                  <button
                    type="button"
                    disabled={!!busy}
                    onClick={() => void restartSession()}
                    className="mt-2 rounded-lg bg-red-600 px-3 py-2 font-medium text-white disabled:opacity-35"
                  >
                    释放故障会话并重新获取
                  </button>
                </div>
              )}
              {!leaderDetected && (
                <button
                  type="button"
                  disabled={!!busy}
                  onClick={() => void probeLeader()}
                  className="flex w-full items-center justify-center gap-2 rounded-xl border border-sky-700 bg-sky-950/30 px-4 py-3 text-sm font-medium text-sky-200 disabled:opacity-35"
                >
                  {busy === "连接主臂" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Hand className="h-4 w-4" />}
                  只读连接 SO-101 主臂
                </button>
              )}
              <button
                type="button"
                disabled={!!busy || hasSession || !calibrationReady || !leaderDetected || !(followerOk || followerPendingBinding)}
                onClick={() => void acquire()}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-emerald-500 px-4 py-3 text-sm font-semibold text-slate-950 disabled:cursor-not-allowed disabled:opacity-35"
              >
                {busy === "获取控制权" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}
                开始无输出联动
              </button>
              <button
                type="button"
                disabled={!hasSession || !!busy || armMode === "following" || homeActive}
                onClick={() => void startHoming()}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-orange-500 px-4 py-3 text-sm font-semibold text-slate-950 disabled:cursor-not-allowed disabled:opacity-35"
              >
                {busy === "从臂回中" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
                回到安全姿态（真实运动）
              </button>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  disabled={!hasSession || !!busy || homeActive}
                  onClick={() => void control("preview")}
                  className="flex items-center justify-center gap-2 rounded-xl border border-sky-700 bg-sky-950/30 px-3 py-2.5 text-sm text-sky-200 disabled:opacity-35"
                >
                  <Play className="h-4 w-4" /> 无输出预览
                </button>
                <button
                  type="button"
                  disabled={!hasSession || !!busy || homeActive}
                  onClick={() => void control("align")}
                  className="flex items-center justify-center gap-2 rounded-xl border border-slate-700 px-3 py-2.5 text-sm text-slate-200 disabled:opacity-35"
                >
                  <Hand className="h-4 w-4" /> 记录对齐
                </button>
                <button
                  type="button"
                  disabled={!hasSession || !!busy || homeActive || armMode !== "aligned"}
                  onClick={() => void control("arm")}
                  className="flex items-center justify-center gap-2 rounded-xl border border-amber-700 bg-amber-950/25 px-3 py-2.5 text-sm text-amber-200 disabled:opacity-35"
                >
                  <ShieldCheck className="h-4 w-4" /> 接合离合
                </button>
                <button
                  type="button"
                  disabled={!hasSession || !!busy || homeActive || armMode !== "armed"}
                  onClick={() => void control("engage")}
                  className="flex items-center justify-center gap-2 rounded-xl bg-amber-500 px-3 py-2.5 text-sm font-semibold text-slate-950 disabled:opacity-35"
                >
                  <Play className="h-4 w-4" /> 开始真实跟随
                </button>
                <button
                  type="button"
                  disabled={!hasSession || !!busy}
                  onClick={() => void control("pause_all")}
                  className="flex items-center justify-center gap-2 rounded-xl border border-amber-800 bg-amber-950/20 px-3 py-2.5 text-sm text-amber-200 disabled:opacity-35"
                >
                  <Pause className="h-4 w-4" /> 暂停
                </button>
                <button
                  type="button"
                  disabled={estopBusy}
                  onClick={() => void emergencyStop()}
                  className="flex items-center justify-center gap-2 rounded-xl bg-red-600 px-3 py-2.5 text-sm font-semibold text-white disabled:opacity-60"
                >
                  {estopBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CircleAlert className="h-4 w-4" />} 急停
                </button>
              </div>
              <button
                type="button"
                disabled={!hasSession || !!busy}
                onClick={() => void release()}
                className="flex w-full items-center justify-center gap-2 rounded-xl border border-slate-700 px-3 py-2 text-xs text-slate-400 disabled:opacity-35"
              >
                <Unplug className="h-3.5 w-3.5" /> 释放会话
              </button>
            </div>

            <div className="mt-4 rounded-xl border border-slate-800 bg-black/30 p-3 text-xs text-slate-400">
              <p className="flex items-center gap-2 text-slate-300">
                <ShieldCheck className={`h-4 w-4 ${outputsOn ? "text-amber-300" : "text-emerald-400"}`} />
                {outputsOn ? "真实执行输出已通过显式门禁" : "预览模式固定关闭执行输出"}
              </p>
              <p className="mt-2 leading-5">
                {outputsOn
                  ? "后台控制循环正以 10 Hz 发送限速目标；暂停、故障或 0.6 秒无新命令会停止并发布保持帧。"
                  : "先记录对齐，再“接合离合”和“开始真实跟随”。未显式接合时主臂只更新预览目标。"}
              </p>
              <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 border-t border-slate-800 pt-2">
                <span>上位机提交</span>
                <span className="text-right font-mono text-slate-300">{status?.follow_runtime?.commands_sent ?? 0}</span>
                <span>车端收到</span>
                <span className="text-right font-mono text-slate-300">{frameProcess?.frames_received ?? "—"}</span>
                <span>车端实发</span>
                <span className="text-right font-mono text-emerald-300">{frameProcess?.publish_count ?? "—"}</span>
                <span>车端拒绝</span>
                <span className="text-right font-mono text-amber-300">{frameProcess?.frames_refused ?? "—"}</span>
                <span>有效帧率</span>
                <span className="text-right font-mono text-slate-300">
                  {typeof frameProcess?.effective_emit_hz === "number"
                    ? `${frameProcess.effective_emit_hz.toFixed(2)} Hz`
                    : "—"}
                </span>
              </div>
              {frameProcess?.last_refusal && (
                <p className="mt-2 break-all text-[11px] text-amber-300">
                  最近拒绝：{frameProcess.last_refusal.frame_seq} · {frameProcess.last_refusal.reason}
                </p>
              )}
              <div className="mt-3 flex items-center justify-between border-t border-slate-800 pt-2">
                <span>底盘</span>
                <span className="text-amber-300">锁定</span>
              </div>
              <div className="mt-1 flex items-center justify-between">
                <span>映射身份</span>
                <span className="max-w-[180px] truncate font-mono text-slate-300">
                  {capabilities?.calibration_readiness?.layers?.mapping?.identity ?? "—"}
                </span>
              </div>
            </div>
          </section>
        </div>

        <section className="rounded-2xl border border-orange-900/70 bg-orange-950/15 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-orange-100">从臂指定原始姿态</h2>
              <p className="mt-1 text-xs text-orange-200/70">
                五轴 raw 500，夹爪 raw 350。任务只移动当前轴，每段最多前进 14 tick、车端硬上限 20 tick；底盘保持锁定。
              </p>
            </div>
            <Badge ok={homeTask?.state === "complete"}>
              {homeTask?.state === "complete"
                ? (homeTask?.scope === "single_axis"
                  ? `${homeTask?.only_joint ?? "所选轴"} 已到位`
                  : "六轴已到位")
                : homeActive ? "回中执行中"
                : homeTask?.state === "fault" ? "回中故障"
                : homeTask?.state === "cancelled" || homeTask?.state === "stopped" ? "已停止"
                : "未开始"}
            </Badge>
          </div>
          <div className="mt-3 flex flex-wrap items-end gap-2">
            <label className="text-[11px] text-orange-200/70">
              回中范围
              <select
                aria-label="回中轴"
                value={homeOnlyJoint}
                onChange={(event) => setHomeOnlyJoint(event.target.value)}
                disabled={homeActive || !hasSession}
                className="ml-2 rounded-lg border border-orange-900/60 bg-slate-950 px-2 py-1 text-xs text-orange-100 disabled:opacity-50"
              >
                <option value="">全部六轴</option>
                {Object.keys(HOME_TARGETS).map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            </label>
            <span className="text-[11px] text-orange-200/50">
              单轴回中只移动所选轴；其余轴按实测保持，并与任务起始位置独立比较漂移。
            </span>
          </div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-900">
            <div className="h-full rounded-full bg-gradient-to-r from-orange-500 to-emerald-400 transition-[width]"
              style={{ width: `${homeProgress}%` }} />
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-orange-100/60">
            <span>{homeTask?.current_joint ? `当前轴 ${homeTask.current_joint}` : "当前无运动轴"}</span>
            <span>
              {homeProgress.toFixed(1)}% · 反馈 seq {homeTask?.feedback_sample_seq ?? "—"}
              {homeFeedbackAgeMs != null ? ` · 年龄 ${homeFeedbackAgeMs} ms` : ""}
              {typeof homeFeedbackInstance === "number"
                ? ` · 发布者 #${homeFeedbackInstance} / g${homeFeedbackGeneration ?? 0}`
                : ""}
            </span>
          </div>
          <div className="mt-3 rounded-xl border border-orange-900/40 bg-slate-950/40 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[11px] text-orange-200/70">逐轴力矩加载确认（requested → verified / failed）</span>
              <Badge ok={homeTorqueState === "verified"}>
                {homeTorqueState === "verified" ? "六轴已读回"
                  : homeTorqueState === "failed" ? "加载失败"
                  : homeTorqueState === "loading" || homeTorqueState === "verifying" ? "正在确认"
                  : homeTorqueState === "idle" ? "未加载" : "—"}
              </Badge>
            </div>
            {homeTorqueRows.length > 0 ? (
              <div className="mt-2 flex flex-wrap gap-2">
                {homeTorqueRows.map((row: any) => (
                  <span
                    key={String(row.servo_id)}
                    className={`rounded-lg border px-2 py-1 font-mono text-[11px] ${
                      row.state === "verified"
                        ? "border-emerald-800 bg-emerald-950/30 text-emerald-200"
                        : row.state === "failed"
                          ? "border-red-800 bg-red-950/30 text-red-200"
                          : "border-slate-700 bg-slate-900/50 text-slate-300"
                    }`}
                  >
                    ID{row.servo_id}{" "}
                    {row.state === "verified"
                      ? "verified"
                      : row.state === "failed"
                        ? `failed:${row.reason ?? "unknown"}`
                        : "requested"}
                  </span>
                ))}
              </div>
            ) : (
              <p className="mt-2 text-[11px] text-slate-500">
                尚无逐轴回读记录；只有显式请求回中并授予输出后才会写入力矩。
              </p>
            )}
            {homeTorque?.error && (
              <p className="mt-2 font-mono text-[11px] text-red-300">{String(homeTorque.error)}</p>
            )}
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {Object.entries(HOME_TARGETS).map(([name, target]) => {
              const observed = homeRaw?.[name];
              const error = typeof observed === "number" ? Math.abs(observed - target) : null;
              return (
                <div key={name} className="rounded-xl border border-orange-900/50 bg-slate-950/50 p-3">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs text-slate-200">{name}</span>
                    <Badge ok={error != null && error <= 2}>{error != null ? `误差 ${error} raw` : "等待反馈"}</Badge>
                  </div>
                  <div className="mt-2 grid grid-cols-2 gap-2 text-center">
                    <div className="rounded-lg bg-black/25 p-2">
                      <p className="text-[10px] text-slate-500">目标</p>
                      <p className="font-mono text-sm text-orange-300">{target}</p>
                    </div>
                    <div className="rounded-lg bg-black/25 p-2">
                      <p className="text-[10px] text-slate-500">真实 raw</p>
                      <p className="font-mono text-sm text-slate-100">{typeof observed === "number" ? observed : "—"}</p>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          {homeActive && (
            <button
              type="button"
              disabled={!!busy}
              onClick={() => void cancelHoming()}
              className="mt-3 w-full rounded-xl border border-red-700 bg-red-950/30 px-3 py-2 text-sm font-medium text-red-200 disabled:opacity-35"
            >
              {busy === "取消回中" ? "正在取消…" : "取消回中并停止当前轴"}
            </button>
          )}
          {homeTask?.state === "complete" && (
            <p className="mt-3 rounded-xl border border-emerald-800 bg-emerald-950/25 p-3 text-xs leading-5 text-emerald-200">
              {homeTask?.scope === "single_axis"
                ? `${homeTask?.only_joint ?? "所选轴"} 已到达目标容差；其余轴保持任务起始位置，未参与运动。`
                : "从臂已到达目标容差。"}
              现在手动把 SO-101 主臂摆到相同姿态，然后依次点击“无输出预览”“记录对齐”“接合离合”“开始真实跟随”。
            </p>
          )}
          {(homeTask?.state === "fault" || homeTask?.state === "cancelled"
            || homeTask?.state === "stopped") && (
            <p className="mt-3 rounded-xl border border-red-800 bg-red-950/25 p-3 text-xs leading-5 text-red-200">
              回中已停止（{homeTask?.state}）：{homeTask?.last_error ?? "操作者取消或已停止"}。
              输出许可已撤销，不会自动重放，也不会自动恢复运动。
            </p>
          )}
        </section>

        <section className="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-semibold text-slate-200">主臂 → 从臂联动</h2>
              <p className="mt-1 text-xs text-slate-500">
                {outputsOn
                  ? "真实跟随中：目标经会话、标定、反馈、TTL、限速和车端 driver 多级门禁。"
                  : "行程归一化增量映射，从当前实测位置建立参考；未接合时目标不会发送到舵机。"}
              </p>
            </div>
            <span className="text-xs text-slate-500">
              {follow.available ? "对齐参考已采集" : (follow.reason ?? "等待无输出预览与对齐")}
            </span>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            {JOINTS.map((joint, index) => (
              <JointRow
                key={joint}
                name={joint}
                leader={leaderRad[LEADER_JOINTS[index]]}
                target={targets[joint]?.target}
                unclipped={targets[joint]?.unclipped}
                clipped={targets[joint]?.clipped}
                follower={followerRad[joint]}
              />
            ))}
          </div>
          <div className="mt-3 grid gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-3">
              <p className="text-[10px] uppercase tracking-wider text-slate-600">主臂夹爪</p>
              <p className="mt-1 font-mono text-lg text-sky-300">
                {number(follow.gripper?.leader_percent, 1)}%
              </p>
            </div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-3">
              <p className="text-[10px] uppercase tracking-wider text-slate-600">映射目标</p>
              <p className="mt-1 font-mono text-lg text-emerald-300">
                {number(follow.gripper?.target_pulse, 0)} raw
              </p>
              <p className="mt-1 text-[10px] text-slate-500">
                {follow.gripper?.mapping_mode === "absolute_normalized"
                  ? "绝对端点归一化 · 车端限速收敛"
                  : "相对增量映射"}
              </p>
            </div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/50 p-3">
              <p className="text-[10px] uppercase tracking-wider text-slate-600">从臂实测</p>
              <p className="mt-1 font-mono text-lg text-slate-100">
                {status?.feedback?.servo_pulses?.r_joint ?? "—"} raw
              </p>
            </div>
          </div>
        </section>

        <section className="grid gap-3 md:grid-cols-3">
          {[
            ["A · SO-101 主臂", capabilities?.calibration_readiness?.layers?.leader?.identity],
            ["B · LanderPi 从臂", capabilities?.calibration_readiness?.layers?.follower?.identity],
            ["C · 主从映射", capabilities?.calibration_readiness?.layers?.mapping?.identity],
          ].map(([label, identity]) => (
            <div key={label} className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-400">{label}</span>
                <Badge ok={Boolean(identity)}>{identity ? "已绑定" : "缺失"}</Badge>
              </div>
              <p className="mt-2 truncate font-mono text-xs text-slate-500">{identity ?? "—"}</p>
            </div>
          ))}
        </section>

        <section className="rounded-2xl border border-slate-800 bg-slate-950/55 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-slate-200">主从方向变换</h2>
              <p className="mt-1 text-xs text-slate-500">
                每个从臂轴可独立设为正向或反向。修改后生成新的 C 层版本，并必须重新获取会话、对齐和接合。
              </p>
            </div>
            <Badge ok={!hasSession}>
              {hasSession ? "请先释放会话再修改" : `映射 ${mappingConfig?.version ?? "—"}`}
            </Badge>
          </div>
          <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {[...JOINTS, "r_joint"].map((follower) => {
              const row = follower === "r_joint"
                ? mappingConfig?.gripper
                : mappingConfig?.joints?.find((item: any) => item.follower === follower);
              return (
                <label key={follower} className="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-black/20 p-3 text-xs">
                  <span>
                    <span className="font-mono text-slate-200">{row?.leader ?? "—"}</span>
                    <span className="mx-2 text-slate-600">→</span>
                    <span className="font-mono text-sky-300">{follower}</span>
                  </span>
                  <select
                    value={mappingDraft[follower] ?? 1}
                    disabled={hasSession || mappingBusy || !mappingConfig}
                    onChange={(event) => setMappingDraft((current) => ({
                      ...current,
                      [follower]: Number(event.target.value),
                    }))}
                    className="rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-slate-200 disabled:opacity-40"
                  >
                    <option value={1}>正向 +</option>
                    <option value={-1}>反向 −</option>
                  </select>
                </label>
              );
            })}
          </div>
          <button
            type="button"
            disabled={hasSession || mappingBusy || !mappingConfig}
            onClick={() => void saveMappingDirections()}
            className="mt-3 w-full rounded-xl border border-violet-700 bg-violet-950/30 px-4 py-2.5 text-sm font-medium text-violet-200 disabled:opacity-35"
          >
            {mappingBusy ? "保存映射方向…" : "保存方向变换"}
          </button>
        </section>

        <p className="pb-6 text-center text-xs text-slate-600">
          真实跟随仅在显式接合后开启；底盘保持锁定，停止、超时和急停始终可用。
        </p>
      </main>
    </div>
  );
}
