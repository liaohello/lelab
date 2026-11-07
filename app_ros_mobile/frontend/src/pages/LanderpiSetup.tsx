import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  Camera,
  Check,
  ChevronRight,
  CircleAlert,
  Gamepad2,
  Loader2,
  RefreshCw,
  Unplug,
} from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

const STEPS = [
  { title: "设备连接", hint: "主臂、车端和相机" },
  { title: "主臂标定", hint: "复用 LeRobot 标定" },
  { title: "树莓派从臂", hint: "五轴手动 + 夹爪拖动" },
  { title: "配对预览", hint: "无输出联动确认" },
];

const ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"];
const JOINTS = [...ARM_JOINTS, "r_joint"];
const SERVO_IDS: Record<string, number> = {
  joint1: 1,
  joint2: 2,
  joint3: 3,
  joint4: 4,
  joint5: 5,
  r_joint: 10,
};
const TARGET_INTERVAL_MS = 120; // <= 10 Hz ceiling enforced by the car
const DEFAULT_MANUAL_RANGES: Record<string, { min_raw: number; max_raw: number }> = {
  joint1: { min_raw: 10, max_raw: 998 },
  joint2: { min_raw: 10, max_raw: 961 },
  joint3: { min_raw: 0, max_raw: 990 },
  joint4: { min_raw: 100, max_raw: 980 },
  joint5: { min_raw: 80, max_raw: 910 },
};

function State({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-1 text-xs ${
      ok ? "bg-emerald-500/10 text-emerald-300" : "bg-amber-500/10 text-amber-300"
    }`}>
      {ok ? <Check className="h-3 w-3" /> : <CircleAlert className="h-3 w-3" />}
      {children}
    </span>
  );
}

function DeviceRow({
  icon,
  title,
  value,
  detail,
  ready,
}: {
  icon: React.ReactNode;
  title: string;
  value: string;
  detail: string;
  ready: boolean;
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-slate-800 bg-slate-950/60 p-4">
      <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-slate-900 text-slate-300">
        {icon}
      </div>
      <div className="min-w-0 flex-1">
        <p className="text-sm text-slate-300">{title}</p>
        <p className="truncate font-mono text-xs text-slate-500">{detail}</p>
      </div>
      <State ok={ready}>{value}</State>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-slate-600">{label}</p>
      <p className="font-mono text-xs text-slate-300">{value}</p>
    </div>
  );
}

export default function LanderpiSetup() {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [step, setStep] = useState(0);
  const [status, setStatus] = useState<any>(null);
  const [capabilities, setCapabilities] = useState<any>(null);
  const [leaderCalibrations, setLeaderCalibrations] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewTick, setPreviewTick] = useState(0);

  // Calibration job: the car keeps the sampler and the gripper loop running.
  const [job, setJob] = useState<any>(null);
  const [snapshot, setSnapshot] = useState<any>(null);
  const [calibrationError, setCalibrationError] = useState<string | null>(null);
  const [safeMin, setSafeMin] = useState<number | "">("");
  const [safeMax, setSafeMax] = useState<number | "">("");
  const [windowConfirmed, setWindowConfirmed] = useState(false);
  const [supportConfirmed, setSupportConfirmed] = useState(false);
  const [slider, setSlider] = useState(500);
  const [dragging, setDragging] = useState(false);
  const [lastTarget, setLastTarget] = useState<any>(null);
  const [commitResult, setCommitResult] = useState<any>(null);
  const [manualRanges, setManualRanges] = useState(DEFAULT_MANUAL_RANGES);
  const [manualGripper, setManualGripper] = useState({ closed_raw: 504, open_raw: 192 });
  const [manualRangesSavedAt, setManualRangesSavedAt] = useState<string | null>(null);

  const sliderRef = useRef(500);
  const dragRef = useRef<{
    generation: number | null;
    seq: number;
    lastSent: number;
  }>({ generation: null, seq: 0, lastSent: -1 });
  const jobRef = useRef<any>(null);

  useEffect(() => {
    jobRef.current = job;
  }, [job]);

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const [nextStatus, nextCaps, nextCalibrations] = await Promise.all([
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/status"),
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/capabilities"),
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/teleop/leader-calibrations"),
      ]);
      setStatus(nextStatus);
      setCapabilities(nextCaps);
      setLeaderCalibrations(nextCalibrations);
      setError(null);
    } catch (exc: any) {
      setError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [baseUrl, fetchWithHeaders]);

  const probeLeader = useCallback(async () => {
    setBusy(true);
    try {
      const result = await apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/probe-leader", {
        method: "POST",
        body: {},
      });
      setError(result?.healthy === true ? null : (result?.reason ?? "主臂没有返回完整六轴数据"));
      await refresh();
    } catch (exc: any) {
      setError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [baseUrl, fetchWithHeaders, refresh]);

  const postCalibration = useCallback(
    async (action: string, body: Record<string, unknown>) => {
      return apiRequest<any>(
        baseUrl, fetchWithHeaders, `/ros-mobile/hybrid/follower-calibration/${action}`,
        { method: "POST", body });
    },
    [baseUrl, fetchWithHeaders],
  );

  const startCalibration = useCallback(async () => {
    setBusy(true);
    try {
      const result = await postCalibration("start", {
        safe_window_raw: [Number(safeMin), Number(safeMax)],
        operator_confirmed_safe_window: windowConfirmed,
        support_confirmed: supportConfirmed,
      });
      setJob({ token: result.token, job_id: result.job_id, generation: result.generation });
      setSnapshot(result);
      dragRef.current = { generation: null, seq: 0, lastSent: -1 };
      setCalibrationError(null);
      setCommitResult(null);
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [postCalibration, safeMax, safeMin, supportConfirmed, windowConfirmed]);

  const stopCalibration = useCallback(async () => {
    if (!job) return;
    setDragging(false);
    setBusy(true);
    try {
      const result = await postCalibration("stop", {
        token: job.token, job_id: job.job_id, generation: job.generation,
        reason: "operator_stop",
      });
      setSnapshot(result);
      setCalibrationError(null);
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [job, postCalibration]);

  const setJointTorque = useCallback(async (joint: string, enabled: boolean) => {
    if (!job) return;
    setBusy(true);
    try {
      const result = await postCalibration("torque", {
        token: job.token, job_id: job.job_id, generation: job.generation,
        enabled, joints: [joint], support_confirmed: supportConfirmed,
      });
      setSnapshot((prev: any) => ({
        ...(prev ?? {}),
        torque: {
          ...(prev?.torque ?? {}),
          released_joints: result.released_joints,
          all_enabled: result.all_enabled,
        },
      }));
      setCalibrationError(null);
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [job, postCalibration, supportConfirmed]);

  const applyManualRanges = useCallback(async () => {
    if (!job) return;
    setBusy(true);
    try {
      const result = await postCalibration("manual-ranges", {
        token: job.token, job_id: job.job_id, generation: job.generation,
        ranges: manualRanges, gripper: manualGripper,
      });
      setSnapshot((prev: any) => ({ ...(prev ?? {}), ...result }));
      setManualRangesSavedAt(result?.manual_arm_ranges_at_utc ?? new Date().toISOString());
      setCalibrationError(null);
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [job, manualGripper, manualRanges, postCalibration]);

  const sendTarget = useCallback(async (permille: number, openGeneration: boolean) => {
    const active = jobRef.current;
    if (!active) return;
    const ref = dragRef.current;
    ref.seq += 1;
    const body: Record<string, unknown> = {
      token: active.token,
      job_id: active.job_id,
      generation: active.generation,
      target_permille: permille,
      client_seq: ref.seq,
      ttl_ms: 200,
      pointer_active: true,
      safe_min_raw: Number(safeMin),
      safe_max_raw: Number(safeMax),
    };
    body.pointer_generation = openGeneration || ref.generation === null ? null : ref.generation;
    try {
      const result = await postCalibration("target", body);
      if (typeof result?.pointer_generation === "number") {
        ref.generation = result.pointer_generation;
      }
      setLastTarget(result);
      if (result && typeof result === "object") {
        setSnapshot((prev: any) => ({
          ...(prev ?? {}),
          ...(result.phase ? { phase: result.phase } : {}),
          ...(result.gripper ? { gripper: result.gripper } : {}),
          ...(result.command_budget_remaining != null
            ? { command_budget_remaining: result.command_budget_remaining }
            : {}),
        }));
      }
      if (result?.accepted === false && result?.reason && result.reason !== "rate_limited") {
        setCalibrationError(`目标被拒绝：${result.reason}`);
      }
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    }
  }, [postCalibration, safeMax, safeMin]);

  const beginDrag = useCallback(() => {
    if (!job) return;
    setDragging(true);
    setCommitResult(null);
    dragRef.current.generation = null;
    dragRef.current.lastSent = sliderRef.current;
    void sendTarget(sliderRef.current, true);
  }, [job, sendTarget]);

  // A pointerup anywhere (including outside the slider) must stop new targets.
  useEffect(() => {
    if (!dragging) return;
    const release = () => {
      dragRef.current.generation = null;
      dragRef.current.lastSent = -1;
      setDragging(false);
    };
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    window.addEventListener("blur", release);
    const onHidden = () => {
      if (document.visibilityState === "hidden") release();
    };
    document.addEventListener("visibilitychange", onHidden);
    return () => {
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
      window.removeEventListener("blur", release);
      document.removeEventListener("visibilitychange", onHidden);
    };
  }, [dragging]);

  useEffect(() => {
    if (!dragging) return;
    const id = window.setInterval(() => {
      const value = sliderRef.current;
      if (value === dragRef.current.lastSent) return;
      dragRef.current.lastSent = value;
      void sendTarget(value, false);
    }, TARGET_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [dragging, sendTarget]);

  const markEnd = useCallback(async (end: "closed" | "open") => {
    if (!job) return;
    setBusy(true);
    try {
      const result = await postCalibration("mark", {
        token: job.token, job_id: job.job_id, generation: job.generation, end,
      });
      setSnapshot((prev: any) => ({ ...(prev ?? {}), gripper: result.gripper }));
      setCalibrationError(null);
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [job, postCalibration]);

  const stopGripper = useCallback(async () => {
    if (!job) return;
    setDragging(false);
    setBusy(true);
    try {
      const result = await postCalibration("gripper-stop", {
        token: job.token, job_id: job.job_id, generation: job.generation,
      });
      setSnapshot((prev: any) => ({
        ...(prev ?? {}),
        phase: result.phase,
        gripper: result.gripper,
      }));
      setCalibrationError(null);
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [job, postCalibration]);

  const commitCalibration = useCallback(async () => {
    if (!job) return;
    setBusy(true);
    try {
      const result = await postCalibration("commit", {
        token: job.token, job_id: job.job_id, generation: job.generation,
      });
      setCommitResult(result);
      setCalibrationError(result?.saved === true
        ? null
        : `保存被拒绝：${(result?.reasons ?? ["未知原因"]).join("；")}`);
      if (result?.saved === true) await refresh();
    } catch (exc: any) {
      setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
    } finally {
      setBusy(false);
    }
  }, [job, postCalibration, refresh]);

  useEffect(() => {
    void refresh();
  }, []); // initial load only; polling below avoids busy-state feedback loops

  useEffect(() => {
    const id = window.setInterval(() => {
      void Promise.all([
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/status"),
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/capabilities"),
      ]).then(([nextStatus, nextCaps]) => {
        setStatus(nextStatus);
        setCapabilities(nextCaps);
      }).catch((exc: any) => {
        setError(exc?.detail ?? exc?.message ?? String(exc));
      });
    }, 1500);
    return () => window.clearInterval(id);
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    const id = window.setInterval(() => setPreviewTick((value) => value + 1), 400);
    return () => window.clearInterval(id);
  }, []);

  // Status polling is the job heartbeat: without it the car stops the job.
  useEffect(() => {
    if (!job) return;
    const id = window.setInterval(() => {
      void postCalibration("status", {
        token: job.token, job_id: job.job_id, generation: job.generation,
      }).then((result) => {
        setSnapshot(result);
      }).catch((exc: any) => {
        setCalibrationError(exc?.detail ?? exc?.message ?? String(exc));
      });
    }, 500);
    return () => window.clearInterval(id);
  }, [job, postCalibration]);

  const feedback = status?.feedback ?? {};
  const leader = status?.leader ?? {};
  const calibrations = status?.calibrations ?? {};
  const devices = status?.devices ?? {};
  const rows = Array.isArray(feedback?.samples) ? feedback.samples : [];
  const rawByJoint = useMemo(() => {
    const map: Record<string, any> = {};
    for (const row of rows) map[row.name] = row;
    return map;
  }, [rows]);
  const leaderReady = (leader.connected === true && leader.healthy === true)
    || (status?.session_id != null && status?.devices?.leader?.connected === true);
  const leaderDetected = leader.detected === true || leader.connected === true
    || status?.devices?.leader?.connected === true;
  const followerReady = devices?.follower?.driver_bound === true && rows.length === 6;
  const missingFeedback = (feedback?.missing_joints ?? []).filter(
    (name: unknown): name is string => typeof name === "string");
  const cameraReady = status?.video?.valid === true && status?.video?.stale === false;
  const calibrationReady = calibrations.ready === true;
  const previewReady = status?.hardware_preview?.available === true;
  const completion = [leaderReady && followerReady && cameraReady, calibrationReady, followerReady, previewReady];

  const phase = (
    snapshot?.phase === "manual_range" && lastTarget?.phase === "gripper_drag"
      ? "gripper_drag"
      : snapshot?.phase
  ) ?? "idle";
  const active = phase === "manual_range" || phase === "gripper_drag";
  const motionActive = phase === "gripper_drag";
  const joints = snapshot?.joints ?? {};
  const gripper = snapshot?.gripper ?? {};
  const releasedJoints: string[] = snapshot?.torque?.released_joints ?? [];
  const armValid = snapshot?.arm_valid === true;
  const currentGripperRaw = Number(rawByJoint?.r_joint?.raw_position);
  const hasCurrentGripperRaw = Number.isFinite(currentGripperRaw);
  const suggestedSafeWindow = useMemo(() => {
    if (!hasCurrentGripperRaw) return null;
    let low = Math.max(0, Math.round(currentGripperRaw) - 50);
    let high = Math.min(1000, Math.round(currentGripperRaw) + 50);
    if (high - low < 40) {
      low = Math.max(0, high - 40);
      high = Math.min(1000, low + 40);
    }
    return low < high ? [low, high] as const : null;
  }, [currentGripperRaw, hasCurrentGripperRaw]);
  const windowValid = typeof safeMin === "number" && typeof safeMax === "number"
    && Number.isInteger(safeMin) && Number.isInteger(safeMax)
    && safeMin >= 0 && safeMax <= 1000 && safeMin < safeMax
    && safeMax - safeMin >= 40 && safeMax - safeMin <= 1000;
  const startBlocker = !followerReady
    ? `等待树莓派从臂六路真实反馈${missingFeedback.length > 0 ? `：${missingFeedback.join("、")}` : ""}`
    : !windowValid
      ? "先填写夹爪安全窗 raw 最小值和最大值"
      : !windowConfirmed
        ? "请勾选夹爪安全窗现场确认"
        : !supportConfirmed
          ? "请勾选机械臂支撑、清场和急停确认"
          : null;

  const rounds = Number(gripper?.rounds_completed ?? 0);
  const currentRound = Math.min(2, rounds + 1);
  const marksThisRound = (gripper?.marks ?? []).filter((mark: any) => mark?.round === currentRound);
  const markedClosed = marksThisRound.some((mark: any) => mark?.end === "closed");
  const markedOpen = marksThisRound.some((mark: any) => mark?.end === "open");
  const settleRemaining = Number(gripper?.settle_remaining_ms ?? 0);
  const feedbackAge = gripper?.feedback_age_s;
  const gripperFault = gripper?.fault;
  const gripperValid = gripper?.valid === true;

  const markBlocker = useMemo(() => {
    if (phase !== "gripper_drag") return "先拖动滑轨开始夹爪受控运动";
    if (dragging) return "松开滑轨后再标记端点";
    if (gripperFault) return `已停止：${gripperFault}`;
    if (settleRemaining > 0) return `松开滑轨 ${Math.ceil(settleRemaining / 100) / 10} 秒后才能标记`;
    if (feedbackAge == null || Number(feedbackAge) > 0.5) return "没有新鲜的 ID10 反馈";
    if (gripper?.last_target_raw == null) return "还没有发送过目标";
    return null;
  }, [dragging, feedbackAge, gripper?.last_target_raw, gripperFault, phase, settleRemaining]);
  const sliderBlocker = !job
    ? "先填写安全窗、勾选两项确认并开始标定任务"
    : phase === "stopped"
      ? "标定任务已停止，需重新开始"
      : phase === "fault"
        ? `标定任务故障：${snapshot?.stop_reason ?? gripperFault ?? "unknown"}`
        : phase === "review"
          ? "全部手动设置已完成，请生成 B 层结果"
          : releasedJoints.length > 0
            ? "先恢复所有已释放关节的力矩"
            : null;

  const instruction = !job
    ? "先填写并确认安全窗，再开始标定任务。开始动作本身不会让机械臂运动。"
    : phase === "manual_range"
      ? (armValid
        ? "五个手臂关节覆盖已达标。可以进入夹爪拖动标定。"
        : "托住机械臂后逐轴释放力矩，用手把每个关节缓慢经过两侧安全端点。")
      : phase === "gripper_drag"
        ? (
          rounds >= 2
            ? "第 1、2 轮开闭均已记录。点击“结束夹爪会话”，然后生成 B 层。"
            : markedClosed && markedOpen
              ? `第 ${currentRound} 轮已完成。请再执行一轮：拖到闭端标记，再拖到开端标记。`
              : markedClosed
                ? `第 ${currentRound} 轮闭端已确认；现在移动到开端并标记。`
                : markedOpen
                  ? `第 ${currentRound} 轮开端已确认；现在移动到闭端并标记。`
                  : `当前是第 ${currentRound} 轮：拖动滑轨到闭端标记，再移动到开端标记。`
        )
        : phase === "review"
          ? (gripperValid ? "夹爪两次开闭重复性合格，可以生成 B 层标定。" : "夹爪标定还不完整，检查标记与重复性。")
          : phase === "stopped"
            ? `任务已停止（${snapshot?.stop_reason ?? "operator"}）。${gripperValid ? "结果可用于保存。" : "结果不完整。"}`
            : phase === "fault"
              ? `任务因故障停止：${snapshot?.stop_reason ?? gripperFault ?? "unknown"}`
              : "";

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-950/90">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-5 py-4">
          <Link to="/ros-mobile" className="rounded-lg p-2 text-slate-400 hover:bg-slate-900 hover:text-white">
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-emerald-500/15 text-emerald-300">
            <Gamepad2 className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="font-semibold">LanderPi 设置</h1>
            <p className="text-xs text-slate-500">按 LeRobot 的设备 → 标定 → 测试顺序完成真机配置</p>
          </div>
          {motionActive ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-1 text-xs text-amber-300">
              <CircleAlert className="h-3 w-3" />
              受控运动中：仅 ID10
            </span>
          ) : (
            <State ok={status?.outputs_enabled === false}>执行输出关闭</State>
          )}
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={busy}
            className="rounded-lg border border-slate-700 p-2 text-slate-300 hover:bg-slate-900 disabled:opacity-40"
            aria-label="重新检测"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          </button>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl gap-6 px-5 py-6 md:grid-cols-[220px_1fr]">
        <nav className="space-y-1">
          {STEPS.map((item, index) => (
            <button
              type="button"
              key={item.title}
              onClick={() => setStep(index)}
              className={`flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left transition ${
                step === index ? "bg-slate-800 text-white" : "text-slate-400 hover:bg-slate-900"
              }`}
            >
              <span className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs ${
                completion[index]
                  ? "bg-emerald-500 text-slate-950"
                  : step === index
                    ? "bg-sky-500 text-slate-950"
                    : "bg-slate-800 text-slate-500"
              }`}>
                {completion[index] ? <Check className="h-4 w-4" /> : index + 1}
              </span>
              <span className="min-w-0">
                <span className="block text-sm">{item.title}</span>
                <span className="block truncate text-[11px] text-slate-500">{item.hint}</span>
              </span>
            </button>
          ))}
          <Link
            to="/ros-mobile"
            className="mt-4 flex items-center justify-between rounded-xl border border-slate-800 px-3 py-2 text-xs text-slate-500 hover:text-slate-300"
          >
            完整诊断与原始数据 <ChevronRight className="h-3 w-3" />
          </Link>
        </nav>

        <section className="min-w-0 rounded-2xl border border-slate-800 bg-slate-900/40 p-6">
          <div className="mb-6">
            <p className="text-xs uppercase tracking-widest text-slate-600">
              STEP {step + 1} / {STEPS.length}
            </p>
            <h2 className="mt-1 text-xl font-semibold">{STEPS[step].title}</h2>
          </div>

          {error && (
            <div className="mb-5 rounded-xl border border-red-900 bg-red-950/30 p-3 text-sm text-red-300">
              {error}
            </div>
          )}

          {step === 0 && (
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <DeviceRow
                  icon={<Gamepad2 className="h-5 w-5" />}
                  title="SO-101 主臂"
                  value={leaderReady ? "数据正常" : leaderDetected ? "串口已检测" : "未检测"}
                  detail={leaderReady ? (leader.source ?? "串口已连接") : (leader.reason ?? "请连接上位机 USB")}
                  ready={leaderReady}
                />
                <DeviceRow
                  icon={<Unplug className="h-5 w-5" />}
                  title="LanderPi 从臂"
                  value={followerReady ? "六路正常" : "等待反馈"}
                  detail={devices?.follower?.source ?? "唯一串口 owner"}
                  ready={followerReady}
                />
                <DeviceRow
                  icon={<Camera className="h-5 w-5" />}
                  title="腕部相机"
                  value={cameraReady ? "画面正常" : "无新画面"}
                  detail={status?.video?.source ?? "未取得相机话题"}
                  ready={cameraReady}
                />
                <DeviceRow
                  icon={<Check className="h-5 w-5" />}
                  title="控制通道"
                  value={status?.outputs_enabled === false ? "安全检查通过" : "输出状态未知"}
                  detail="输出保持关闭；当前仅允许查看和标定"
                  ready={status?.outputs_enabled === false}
                />
              </div>
              {leaderDetected && !leaderReady && (
                <div className="rounded-xl border border-sky-800 bg-sky-950/20 p-4 text-sm text-sky-200">
                  已识别主臂串口。点击检测会以独占、只读方式打开并检查六个电机。
                  <button
                    type="button"
                    onClick={() => void probeLeader()}
                    disabled={busy}
                    className="ml-3 rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-slate-950 disabled:opacity-40"
                  >
                    检测主臂
                  </button>
                </div>
              )}
              {!leaderDetected ? (
                <div className="rounded-xl border border-amber-800/60 bg-amber-950/20 p-4 text-sm text-amber-200">
                  没有检测到 SO-101 主臂。这里只使用上位机 by-id 主臂；
                  树莓派上的车端从臂固定通过 ROS 控制器访问，不占用上位机 USB。
                </div>
              ) : null}
              <button
                type="button"
                onClick={() => setStep(1)}
                className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-slate-950 disabled:opacity-40"
                disabled={!leaderReady}
              >
                继续到主臂标定
              </button>
            </div>
          )}

          {step === 1 && (
            <div className="space-y-4">
              <div className="rounded-xl border border-slate-800 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-medium">主臂配置</h3>
                    <p className="mt-1 text-xs text-slate-500">
                      复用 LeRobot 已经生成的 same-motor-id 校准，不重新写 EEPROM。
                    </p>
                  </div>
                  <State ok={leaderCalibrations?.exists === true}>{leaderCalibrations?.exists ? "找到配置" : "未找到"}</State>
                </div>
                <div className="mt-4 space-y-2">
                  {(leaderCalibrations?.entries ?? []).filter((row: any) => row.usable).map((row: any) => (
                    <div key={row.path} className="flex items-center justify-between rounded-lg bg-slate-950/70 p-3">
                      <div>
                        <p className="font-mono text-sm text-slate-200">{row.name}</p>
                        <p className="text-[11px] text-slate-600">{row.ids?.join(", ") || "motors unavailable"}</p>
                      </div>
                      <span className="font-mono text-[11px] text-emerald-400">
                        {String(row.sha256 ?? "").slice(0, 10)}
                      </span>
                    </div>
                  ))}
                  {(leaderCalibrations?.entries ?? []).filter((row: any) => row.usable).length === 0 && (
                    <div className="rounded-lg border border-dashed border-slate-700 p-4 text-sm text-slate-500">
                      当前服务目录没有可用的 SO-101 主臂标定。打开原有 LeRobot 标定页完成一次向导后返回这里。
                    </div>
                  )}
                </div>
                <div className="mt-4 flex flex-wrap gap-2">
                  <Link to="/calibration" className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800">
                    打开 LeRobot 标定
                  </Link>
                  <button type="button" onClick={() => void refresh()} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300">
                    重新读取
                  </button>
                </div>
              </div>
              <div className={`rounded-xl border p-4 ${
                calibrationReady ? "border-emerald-800 bg-emerald-950/20" : "border-amber-800/60 bg-amber-950/20"
              }`}>
                <p className="text-sm">{calibrationReady ? "三层标定已就绪" : "三层标定还没有完成"}</p>
                <p className="mt-1 break-words text-xs text-slate-400">
                  {calibrationReady
                    ? calibrations.bundle_hash
                    : (calibrations.reasons ?? ["需要主臂、从臂和映射资产"]).join("；")}
                </p>
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="space-y-4">
              <div className="rounded-xl border border-sky-900/60 bg-sky-950/20 p-4">
                <h3 className="text-sm font-medium text-sky-200">当前目标：树莓派上的 LanderPi 从臂</h3>
                <p className="mt-1 text-xs leading-5 text-slate-400">
                  控制板由 MentorPi 容器内唯一的 `ros_robot_controller` 持有；
                  关节为 ID 1–5 加夹爪 ID 10。采样由车端后台任务持续执行，本页面只创建任务、拖动滑轨和读取结果。
                </p>
              </div>

              {calibrationError && (
                <div
                  data-testid="calibration-error"
                  className="rounded-xl border border-red-900 bg-red-950/30 p-3 text-sm text-red-300"
                >
                  {calibrationError}
                </div>
              )}

              {!job && (
                <div className="space-y-3 rounded-xl border border-slate-700 bg-slate-900/70 p-4">
                  <p className="text-sm font-medium text-slate-200">1. 确认夹爪安全窗</p>
                  <p className="text-xs leading-5 text-slate-400">
                    安全窗必须来自机构资料或现场确认。可使用供应商完整软件范围
                    0–1000 tick；车端仍按 10 Hz 限速并在跟踪超差或位置卡滞时停止。
                  </p>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="text-xs text-slate-400">
                      安全窗最小 raw
                      <input
                        data-testid="safe-min"
                        type="number"
                        min={0}
                        max={999}
                        value={safeMin}
                        onChange={(event) => setSafeMin(
                          event.target.value === "" ? "" : Number(event.target.value))}
                        className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100"
                      />
                    </label>
                    <label className="text-xs text-slate-400">
                      安全窗最大 raw
                      <input
                        data-testid="safe-max"
                        type="number"
                        min={1}
                        max={1000}
                        value={safeMax}
                        onChange={(event) => setSafeMax(
                          event.target.value === "" ? "" : Number(event.target.value))}
                        className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100"
                      />
                    </label>
                  </div>
                  {!windowValid && (
                    <p className="text-xs text-red-300">当前窗口不合法：需要 0 ≤ min &lt; max ≤ 1000，跨度 40–1000。</p>
                  )}
                  {suggestedSafeWindow && (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/70 p-3">
                      <p className="text-xs text-slate-400">
                        当前 r_joint 为 <span className="font-mono text-slate-200">{Math.round(currentGripperRaw)}</span> raw。
                        可先使用当前位置 ±50 tick 的窄窗做方向验证。
                      </p>
                      <button
                        type="button"
                        onClick={() => {
                          setSafeMin(suggestedSafeWindow[0]);
                          setSafeMax(suggestedSafeWindow[1]);
                        }}
                        className="rounded-lg border border-sky-700 px-3 py-1.5 text-xs text-sky-200 hover:bg-sky-950/40"
                      >
                        填入建议窄窗 {suggestedSafeWindow[0]}–{suggestedSafeWindow[1]}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setSafeMin(0);
                          setSafeMax(1000);
                        }}
                        className="rounded-lg border border-amber-700 px-3 py-1.5 text-xs text-amber-200 hover:bg-amber-950/30"
                      >
                        使用完整软件范围 0–1000
                      </button>
                    </div>
                  )}
                  <label className="flex items-start gap-2 text-xs text-amber-200">
                    <input
                      type="checkbox"
                      checked={windowConfirmed}
                      onChange={(event) => setWindowConfirmed(event.target.checked)}
                      className="mt-0.5"
                    />
                    我已现场确认：夹爪在这个窗口内运动不会顶到未知机械极限。
                  </label>
                  <label className="flex items-start gap-2 text-xs text-amber-200">
                    <input
                      type="checkbox"
                      checked={supportConfirmed}
                      onChange={(event) => setSupportConfirmed(event.target.checked)}
                      className="mt-0.5"
                    />
                    机械臂已支撑或托住，周围已清场；确认后才允许逐轴释放力矩。
                  </label>
                  <button
                    type="button"
                    data-testid="start-calibration"
                    disabled={!followerReady || busy || !windowValid || !windowConfirmed || !supportConfirmed}
                    onClick={() => void startCalibration()}
                    className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-slate-950 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    开始从臂标定任务
                  </button>
                  {startBlocker && (
                    <p data-testid="start-blocker" className="text-xs text-amber-300">
                      {startBlocker}
                    </p>
                  )}
                </div>
              )}

              {job && (
                <div className="rounded-xl border border-slate-700 bg-slate-900/70 p-4">
                  <span
                    data-testid="calibration-phase-debug"
                    data-phase={phase}
                    data-target-phase={lastTarget?.phase ?? ""}
                    className="hidden"
                  />
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="rounded-full bg-slate-800 px-2 py-1 font-mono text-[11px] text-slate-300">
                      {String(job.job_id ?? "").slice(0, 8)} · gen {job.generation}
                    </span>
                    <State ok={active}>{phase}</State>
                    <span className="text-[11px] text-slate-500">
                      空闲超时 {snapshot?.idle_expires_in_s != null ? `${Number(snapshot.idle_expires_in_s).toFixed(0)} s` : "—"}
                      · 任务总剩余 {snapshot?.expires_in_s != null
                        ? `${Math.floor(Number(snapshot.expires_in_s) / 60)}分${Math.ceil(Number(snapshot.expires_in_s) % 60)}秒`
                        : "—"}
                      （页面关闭后车端自动停止）
                    </span>
                  </div>
                  <p className="mt-2 text-base font-medium text-slate-100">{instruction}</p>
                  <div className="mt-3 grid gap-2 text-xs text-slate-400 sm:grid-cols-4">
                    <div className={phase === "manual_range" ? "font-medium text-sky-300" : ""}>1. 五轴手动范围</div>
                    <div className={phase === "gripper_drag" ? "font-medium text-sky-300" : ""}>2. 夹爪拖动</div>
                    <div className={phase === "review" ? "font-medium text-sky-300" : ""}>3. 两次开闭确认</div>
                    <div className={commitResult?.saved ? "font-medium text-emerald-300" : ""}>4. 生成 B 层</div>
                  </div>
                  <div className="mt-3 rounded-xl border border-slate-700 bg-slate-950/60 p-3">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <p className="text-sm font-medium text-slate-200">五轴与夹爪全部手动设置</p>
                        <p className="mt-1 text-xs text-slate-500">
                          输入你现场读取的 raw 端点。此路径标记为操作员录入，不冒充自动采样或重复性测试。
                        </p>
                      </div>
                      <button
                        type="button"
                        data-testid="apply-manual-ranges"
                        disabled={busy}
                        onClick={() => void applyManualRanges()}
                        className="rounded-lg bg-sky-600 px-3 py-2 text-sm font-medium text-slate-950 disabled:opacity-40"
                      >
                        应用全部手动设置
                      </button>
                    </div>
                    <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
                      {ARM_JOINTS.map((name) => (
                        <div key={name} className="rounded-lg border border-slate-800 bg-slate-900/70 p-2">
                          <p className="mb-1 font-mono text-xs text-slate-300">
                            {name} / ID {SERVO_IDS[name]}
                          </p>
                          <div className="grid grid-cols-2 gap-1">
                            <label className="text-[10px] text-slate-500">
                              min
                              <input
                                type="number"
                                min={-1000}
                                max={1000}
                                value={manualRanges[name].min_raw}
                                onChange={(event) => setManualRanges((previous) => ({
                                  ...previous,
                                  [name]: {
                                    ...previous[name],
                                    min_raw: Number(event.target.value),
                                  },
                                }))}
                                className="mt-0.5 w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-1 font-mono text-xs text-slate-100"
                              />
                            </label>
                            <label className="text-[10px] text-slate-500">
                              max
                              <input
                                type="number"
                                min={-1000}
                                max={1000}
                                value={manualRanges[name].max_raw}
                                onChange={(event) => setManualRanges((previous) => ({
                                  ...previous,
                                  [name]: {
                                    ...previous[name],
                                    max_raw: Number(event.target.value),
                                  },
                                }))}
                                className="mt-0.5 w-full rounded border border-slate-700 bg-slate-950 px-1.5 py-1 font-mono text-xs text-slate-100"
                              />
                            </label>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="mt-3 grid gap-2 rounded-lg border border-slate-800 bg-slate-900/70 p-3 sm:grid-cols-2">
                      <label className="text-xs text-slate-400">
                        夹爪闭端 raw
                        <input
                          data-testid="manual-gripper-closed"
                          type="number"
                          min={-1000}
                          max={1000}
                          value={manualGripper.closed_raw}
                          onChange={(event) => setManualGripper((previous) => ({
                            ...previous,
                            closed_raw: Number(event.target.value),
                          }))}
                          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100"
                        />
                      </label>
                      <label className="text-xs text-slate-400">
                        夹爪开端 raw
                        <input
                          data-testid="manual-gripper-open"
                          type="number"
                          min={-1000}
                          max={1000}
                          value={manualGripper.open_raw}
                          onChange={(event) => setManualGripper((previous) => ({
                            ...previous,
                            open_raw: Number(event.target.value),
                          }))}
                          className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-sm text-slate-100"
                        />
                      </label>
                    </div>
                    {manualRangesSavedAt && (
                      <p data-testid="manual-ranges-saved" className="mt-2 text-xs text-emerald-300">
                        五轴手动范围已提交：{manualRangesSavedAt}
                      </p>
                    )}
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => void stopCalibration()}
                      disabled={busy || phase === "stopped"}
                      className="rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-300 disabled:opacity-40"
                    >
                      停止任务
                    </button>
                    {(phase === "fault" || (phase === "stopped" && !gripperValid)) && (
                      <button
                        type="button"
                        data-testid="restart-calibration"
                        onClick={() => {
                          setJob(null);
                          setSnapshot(null);
                          setCalibrationError(null);
                          setCommitResult(null);
                          setLastTarget(null);
                          setSlider(500);
                          sliderRef.current = 500;
                          setManualRangesSavedAt(null);
                        }}
                        className="rounded-lg bg-amber-500 px-3 py-2 text-sm font-medium text-slate-950"
                      >
                        {phase === "fault" ? "清除故障并重新设置" : "放弃未完成结果并重新开始"}
                      </button>
                    )}
                    {phase === "gripper_drag" && (
                      <button
                        type="button"
                        onClick={() => void stopGripper()}
                        disabled={busy}
                        className="rounded-lg border border-slate-600 px-3 py-2 text-sm text-slate-300 disabled:opacity-40"
                      >
                        结束夹爪会话
                      </button>
                    )}
                  </div>
                </div>
              )}

              <div className="grid gap-2 sm:grid-cols-2">
                {ARM_JOINTS.map((name) => {
                  const range = joints[name] ?? {};
                  const row = rawByJoint[name];
                  const released = releasedJoints.includes(name);
                  return (
                    <div key={name} className="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-xs text-slate-400">
                          {name} <span className="text-slate-600">/ ID {SERVO_IDS[name]}</span>
                        </span>
                        <State ok={range.valid === true}>
                          {range.valid === true ? "范围已覆盖" : "未达标"}
                        </State>
                      </div>
                      <div className="mt-2 flex items-baseline gap-2">
                        <span className="text-xl font-semibold">{range.last_raw ?? row?.raw_position ?? "—"}</span>
                        <span className="text-xs text-slate-600">raw</span>
                        {row?.age_s != null && (
                          <span className="ml-auto text-[11px] text-slate-500">{Number(row.age_s).toFixed(2)} s</span>
                        )}
                      </div>
                      <div className="mt-2 grid grid-cols-3 gap-1">
                        <Metric label="参考" value={range.reference_raw ?? "—"} />
                        <Metric label="min" value={range.observed_min_raw ?? "—"} />
                        <Metric label="max" value={range.observed_max_raw ?? "—"} />
                        <Metric label="跨度" value={`${range.span_raw ?? 0} / ${range.min_span_raw ?? 80}`} />
                        <Metric label="样本" value={`${range.samples ?? 0} / ${range.min_samples ?? 30}`} />
                        <Metric label="噪声" value={range.noise_raw == null ? "—" : Number(range.noise_raw).toFixed(2)} />
                      </div>
                      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-800">
                        <div
                          className={`h-full ${range.valid ? "bg-emerald-500" : "bg-sky-500"}`}
                          style={{ width: `${Math.min(100, Number(range.span_raw ?? 0) / 0.8)}%` }}
                        />
                      </div>
                      <div className="mt-2 flex items-center justify-between gap-2">
                        <span className="text-[11px] text-slate-500">{released ? "力矩已释放" : "力矩保持"}</span>
                        <button
                          type="button"
                          data-testid={`torque-${name}`}
                          disabled={busy || !job || phase !== "manual_range" || !supportConfirmed}
                          onClick={() => void setJointTorque(name, released)}
                          className={`rounded-md px-2 py-1 text-[11px] ${
                            released
                              ? "border border-emerald-700 text-emerald-300"
                              : "border border-amber-700 text-amber-200"
                          } disabled:opacity-40`}
                        >
                          {released ? "恢复力矩" : "释放力矩"}
                        </button>
                      </div>
                      {(range.reasons ?? []).length > 0 && (
                        <p className="mt-1 text-[11px] text-amber-300">{(range.reasons ?? []).join(", ")}</p>
                      )}
                    </div>
                  );
                })}
              </div>

              <div className="rounded-xl border border-slate-700 bg-slate-950/70 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-medium text-slate-200">夹爪手动驱动标定</h3>
                  <State ok={gripperValid}>{gripperValid ? "重复性合格" : "未完成"}</State>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  安全窗 raw {String(snapshot?.safe_window_raw?.[0] ?? safeMin)}..{String(snapshot?.safe_window_raw?.[1] ?? safeMax)} ·
                  当前只控制 r_joint / ID10 · joint1–joint5 保持位置与力矩
                </p>

                <div className="mt-3 flex items-center justify-between text-[11px] text-slate-500">
                  <span>安全窗最小 raw</span>
                  <span>安全窗最大 raw</span>
                </div>
                <input
                  type="range"
                  data-testid="gripper-slider"
                  aria-label="夹爪目标"
                  min={0}
                  max={1000}
                  step={1}
                  value={slider}
                  disabled={!job || !["manual_range", "gripper_drag"].includes(phase)
                    || releasedJoints.length > 0}
                  onPointerDown={beginDrag}
                  onChange={(event) => {
                    const value = Number(event.target.value);
                    sliderRef.current = value;
                    setSlider(value);
                  }}
                  className="mt-1 w-full accent-sky-500 disabled:opacity-40"
                />
                {sliderBlocker && (
                  <p data-testid="gripper-slider-blocker" className="mt-1 text-xs text-amber-300">
                    {sliderBlocker}
                  </p>
                )}
                <div className="flex items-center justify-between text-[11px] text-slate-500">
                  <span>{slider} ‰</span>
                  <span>
                    目标 raw {lastTarget?.target_raw ?? gripper?.last_target_raw ?? "—"} ·
                    实测 {gripper?.feedback_raw ?? "—"} ·
                    误差 {gripper?.error_raw ?? "—"} tick ·
                    反馈年龄 {feedbackAge == null ? "—" : `${Number(feedbackAge).toFixed(2)} s`}
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400" data-testid="gripper-state">
                  {motionActive
                    ? (dragging ? "正在发送受控目标（≤10 Hz）" : "受控运动中：松开滑轨后不再发送新目标")
                    : phase === "gripper_drag" ? "夹爪会话已开启" : "尚未开始夹爪受控运动"}
                </p>

                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  <div className="rounded-lg bg-slate-900/70 p-2">
                    <p className="text-[11px] text-slate-500">已确认闭端</p>
                    <p className="font-mono text-sm text-slate-200">
                      {gripper?.observed_closed_raw != null ? `${gripper.observed_closed_raw} raw` : "未确认"}
                    </p>
                  </div>
                  <div className="rounded-lg bg-slate-900/70 p-2">
                    <p className="text-[11px] text-slate-500">已确认开端</p>
                    <p className="font-mono text-sm text-slate-200">
                      {gripper?.observed_open_raw != null ? `${gripper.observed_open_raw} raw` : "未确认"}
                    </p>
                  </div>
                  <div className="rounded-lg bg-slate-900/70 p-2">
                    <p className="text-[11px] text-slate-500">重复性</p>
                    <p className="font-mono text-sm text-slate-200">
                      第 {currentRound} 次 / 2 次
                      {gripper?.repeatability_raw != null ? ` · ${gripper.repeatability_raw} tick` : ""}
                    </p>
                  </div>
                  <div className="rounded-lg bg-slate-900/70 p-2">
                    <p className="text-[11px] text-slate-500">可用区间</p>
                    <p className="font-mono text-sm text-slate-200">
                      {gripper?.usable_closed_raw != null && gripper?.usable_open_raw != null
                        ? `${gripper.usable_closed_raw}..${gripper.usable_open_raw}`
                        : "—"}
                    </p>
                  </div>
                </div>

                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    data-testid="mark-closed"
                    disabled={busy || !job || markBlocker !== null || markedClosed}
                    onClick={() => void markEnd("closed")}
                    className="rounded-lg bg-slate-700 px-3 py-2 text-sm text-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    标记当前为闭端
                  </button>
                  <button
                    type="button"
                    data-testid="mark-open"
                    disabled={busy || !job || markBlocker !== null || markedOpen}
                    onClick={() => void markEnd("open")}
                    className="rounded-lg bg-slate-700 px-3 py-2 text-sm text-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    标记当前为开端
                  </button>
                </div>
                {markBlocker && (
                  <p data-testid="mark-blocker" className="mt-2 text-[11px] text-amber-300">
                    不能标记：{markBlocker}
                  </p>
                )}
                <p className="mt-3 rounded-lg border border-amber-900/60 bg-amber-950/20 p-2 text-[11px] leading-5 text-amber-200">
                  没有电流、力或接触检测，禁止自动顶到未知机械极限。拖动松手后停止发送新目标；
                  页面关闭或网络断开后车端会在空闲超时内自动停止并恢复力矩。
                </p>
              </div>

              <div className="grid gap-2 sm:grid-cols-4">
                <Metric label="温度" value={snapshot?.diagnostics?.temperature ?? "unavailable"} />
                <Metric label="电压" value={snapshot?.diagnostics?.voltage ?? "unavailable"} />
                <Metric label="力矩状态读回" value={snapshot?.diagnostics?.torque_readback ?? "unavailable"} />
                <Metric label="接触/负载" value={snapshot?.diagnostics?.load ?? "unavailable"} />
              </div>

              {job && (
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    type="button"
                    data-testid="commit-calibration"
                    disabled={busy || !gripperValid || phase === "manual_range" || phase === "gripper_drag"}
                    onClick={() => void commitCalibration()}
                    className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-medium text-slate-950 disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    完成两次测试并生成结果
                  </button>
                  <span className="text-xs text-slate-500">
                    {gripperValid ? "两次开闭重复性已通过，可以生成 B 层标定。" : "至少两次开闭确认与一次重复性检查后才可保存。"}
                  </span>
                </div>
              )}

              {commitResult?.saved && (
                <div className="rounded-xl border border-emerald-800 bg-emerald-950/20 p-4 text-sm text-emerald-200">
                  已保存 follower 标定 v{commitResult.schema_version}，版本 {String(commitResult.record?.version ?? "").slice(0, 12)}。
                  {commitResult.requires_repair
                    ? "旧 C 层映射已失效，需要重新配对主臂与从臂后才能预览。"
                    : "没有需要失效的旧映射。"}
                </div>
              )}

              {!followerReady && (
                <p className="rounded-xl border border-amber-800/60 bg-amber-950/20 p-3 text-sm text-amber-200">
                  六路反馈不完整，不能继续标定或配对。
                </p>
              )}

              <details className="rounded-xl border border-slate-800 p-3 text-xs text-slate-500">
                <summary className="cursor-pointer text-slate-400">当前从臂标定边界</summary>
                <p className="mt-2 leading-5">
                  joint1–joint5 使用人工范围采集：逐轴释放力矩后用手覆盖安全行程，车端后台持续采样，
                  只统计新鲜、序号递增、来源为 servo_position_readback 的样本。重复、过期、缺失与
                  不合理跳变都不会扩大覆盖范围。夹爪只使用已确认安全窗内的拖动目标，不进行未知端点搜索。
                </p>
              </details>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-4">
              <div className="overflow-hidden rounded-xl border border-slate-800 bg-black">
                <img
                  src={`${baseUrl}/ros-mobile/preview.jpg?t=${previewTick}`}
                  alt="LanderPi camera preview"
                  className="aspect-video w-full object-contain"
                />
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <div className="rounded-xl bg-slate-950/60 p-3">
                  <p className="text-xs text-slate-500">主臂</p>
                  <p className="mt-1 text-sm">{leaderReady ? "数据正常" : "等待连接"}</p>
                </div>
                <div className="rounded-xl bg-slate-950/60 p-3">
                  <p className="text-xs text-slate-500">从臂</p>
                  <p className="mt-1 text-sm">{followerReady ? "六路正常" : "等待反馈"}</p>
                </div>
                <div className="rounded-xl bg-slate-950/60 p-3">
                  <p className="text-xs text-slate-500">执行输出</p>
                  <p className="mt-1 text-sm text-emerald-300">保持关闭</p>
                </div>
              </div>
              <div className="rounded-xl border border-slate-800 p-4">
                <p className="text-sm">配对预览</p>
                <p className="mt-1 text-xs text-slate-500">
                  {previewReady
                    ? "主臂、从臂、相机和标定身份齐全。可在完整诊断页执行无输出预览。"
                    : (status?.hardware_preview?.reason ?? "等待三层标定和设备连接")}
                </p>
                <Link to="/landerpi/teleop" className="mt-3 inline-block rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-slate-950">
                  打开主从遥操作控制台
                </Link>
              </div>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}
