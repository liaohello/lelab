import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Loader2, Radio } from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";
import HybridDataReview from "@/components/control/HybridDataReview";
import {
  CALIBRATION_ACTIONS,
  CALIBRATION_KINDS,
  DEFAULT_STATUS_MAX_AGE_MS,
  MANUAL_PHASES,
  actionAllowed,
  controlBody,
  endEpisodeSequence,
  evaluateVideo,
  gateCapabilities,
  gateFeedback,
  isKnownCalibrationAction,
  isKnownCalibrationKind,
  isForeignSession,
  parseCalibrationDraft,
  phaseBody,
  reasonText,
  selectableProfiles,
  sessionRef,
  simulationLabel,
  stopSessionSequence,
  trustLabel,
  type FeedbackGate,
  type VideoState,
} from "@/components/control/hybridContract";

const button = "rounded border border-gray-600 px-3 py-1.5 text-sm disabled:opacity-40";
const box = "rounded-lg border border-gray-800 bg-gray-950/60 p-3";
const th = "py-1 pr-2 font-normal text-gray-500";
const td = "py-1 pr-2 font-mono text-gray-200";

const show = (value: unknown): string => {
  if (value === null || value === undefined) return "未知";
  if (typeof value === "string") return value || "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "非有限值";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "无法序列化";
  }
};

/** Leases arrive as booleans, strings or nested objects; unknown is never shown as held. */
const leaseText = (lease: unknown): string => {
  if (lease === null || lease === undefined) return "未知";
  if (typeof lease === "boolean") return lease ? "持有" : "无";
  if (typeof lease === "string") return lease || "未知";
  if (typeof lease === "object") {
    const record = lease as Record<string, unknown>;
    if (record.held === true || record.active === true) return "持有";
    if (record.held === false || record.active === false) return "无";
    if (typeof record.owner === "string") return `持有者 ${record.owner}`;
  }
  return show(lease);
};

const channelText = (channel: unknown): string => {
  if (channel === null || channel === undefined) return "未知";
  if (typeof channel === "string") return channel || "未知";
  if (typeof channel === "object") {
    const record = channel as Record<string, unknown>;
    const state = record.state ?? record.status;
    if (typeof state === "string") return state;
    const flags = ["enabled", "locked", "engaged", "armed"]
      .filter((key) => typeof record[key] === "boolean")
      .map((key) => `${key}=${record[key] ? "是" : "否"}`);
    if (flags.length > 0) return flags.join(" · ");
  }
  return show(channel);
};

export default function HybridTeleopPanel() {
  const { baseUrl, fetchWithHeaders, clientId } = useApi();
  const [status, setStatus] = useState<any>(null);
  const [caps, setCaps] = useState<any>(null);
  const [recording, setRecording] = useState<any>(null);
  const [recordingFromStatus, setRecordingFromStatus] = useState(false);
  const [online, setOnline] = useState(false);
  const [lastOkMs, setLastOkMs] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [error, setError] = useState<{ text: string; at: number } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<any>(null);
  const [kind, setKind] = useState<string>("base");
  const [identity, setIdentity] = useState("");
  const [draft, setDraft] = useState("{}");
  const [profile, setProfile] = useState("");
  const [outcome, setOutcome] = useState("unknown");
  const [note, setNote] = useState("");
  const [videoFrame, setVideoFrame] = useState<{ seq: number; changedAt: number } | null>(null);
  const [boundFrame, setBoundFrame] = useState<{ seq: number; url: string; fetchedAt: number } | null>(null);
  const [frameBinding, setFrameBinding] = useState<{ seq: number | null; ok: boolean; reason: string }>({
    seq: null,
    ok: false,
    reason: "等待视频来源元数据",
  });
  const polling = useRef(false);
  const lastSeqRef = useRef<number | null>(null);

  const pushError = useCallback((prefix: string, detail: unknown) => {
    setError({ text: `${prefix}：${reasonText(detail)}`, at: Date.now() });
  }, []);

  const refresh = useCallback(async () => {
    if (polling.current) return;
    polling.current = true;
    const controller = new AbortController();
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 4000);
    try {
      const [nextStatus, nextCaps, nextRecording] = await Promise.all([
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/status", { signal: controller.signal }),
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/capabilities", { signal: controller.signal }),
        apiRequest<any>(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/recording/status", { signal: controller.signal }),
      ]);
      setStatus(nextStatus);
      setCaps(nextCaps);
      if (nextRecording && typeof nextRecording === "object" && Object.keys(nextRecording).length > 0) {
        setRecording(nextRecording);
        setRecordingFromStatus(false);
      } else {
        // An empty successful reply is not evidence that nothing is recording.
        setRecording(nextStatus?.recording ?? nextRecording);
        setRecordingFromStatus(true);
      }
      setLastOkMs(Date.now());
      setOnline(true);
      // Polling deliberately never clears an operator-visible error; only "关闭错误" or a
      // successful retry of the same action does.
    } catch (exc: any) {
      setOnline(false);
      if (exc?.name !== "AbortError" || timedOut) {
        setError((previous) => previous ?? {
          text: timedOut
            ? "联合状态请求超时"
            : `联合状态不可用：${reasonText(exc?.detail ?? exc?.message ?? exc)}`,
          at: Date.now(),
        });
      }
    } finally {
      window.clearTimeout(timer);
      polling.current = false;
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 1200);
    return () => window.clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, []);

  const gate = useMemo(
    () => gateCapabilities(caps, { nowMs: now, lastOkMs, maxAgeMs: DEFAULT_STATUS_MAX_AGE_MS }),
    [caps, now, lastOkMs]
  );
  const profileOptions = useMemo(() => selectableProfiles(gate), [gate]);
  const session = useMemo(() => sessionRef(status), [status]);
  const activeProfile = typeof status?.profile === "string" ? status.profile : null;
  const simulated = gate.simulated;
  // The browser supplies operator-presence heartbeats only. The server owns
  // both leases and the car watchdog remains authoritative for stopping.
  useEffect(() => {
    if (!session.valid || status?.state !== "ready") return;
    const identity = { session_id: session.session_id, session_epoch: session.session_epoch };
    let cancelled = false;
    let pending = false;
    const renew = async () => {
      if (pending || cancelled) return;
      pending = true;
      try {
        await apiRequest(baseUrl, fetchWithHeaders, "/ros-mobile/hybrid/heartbeat", {
          method: "POST", body: identity,
        });
      } catch (exc: any) {
        if (!cancelled) {
          setOnline(false);
          pushError("联合会话心跳失败", exc?.detail ?? exc?.message ?? exc);
          void refresh();
        }
      } finally {
        pending = false;
      }
    };
    void renew();
    const timer = window.setInterval(renew, 5000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [baseUrl, fetchWithHeaders, pushError, refresh, session.valid,
      session.session_id, session.session_epoch, status?.state]);
  const feedbackGate: FeedbackGate = useMemo(
    () => gateFeedback(status?.feedback, {
      simulated,
      expectedCalibrationHash:
        status?.calibration_bundle_hash ??
        status?.calibrations?.bundle_hash ??
        status?.calibrations?.bundle?.content_sha256 ??
        null,
    }),
    [status?.feedback, status?.calibration_bundle_hash, status?.calibrations, simulated]
  );

  const video: VideoState = useMemo(
    () => evaluateVideo({
      video: status?.video,
      nowMs: now,
      lastOkMs,
      lastSeq: lastSeqRef.current,
      lastSeqChangedMs: videoFrame?.changedAt ?? null,
    }),
    [status?.video, now, lastOkMs, videoFrame?.changedAt]
  );

  // The status sequence must identify the bytes the browser fetched. A plain <img> cannot
  // prove that; require a sequence header that exactly matches the status source frame.
  useEffect(() => {
    const seq = video.seq;
    if (!video.fresh || seq === null) {
      setFrameBinding({
        seq,
        ok: false,
        reason: video.fresh ? "缺少可绑定的视频帧序号" : video.reason,
      });
      return;
    }

    const controller = new AbortController();
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 4000);
    let cancelled = false;
    let objectUrl: string | null = null;
    setFrameBinding({ seq, ok: false, reason: "正在获取同帧 JPEG" });

    void (async () => {
      try {
        const response = await fetchWithHeaders(
          `${baseUrl}/ros-mobile/preview.jpg?frame_seq=${encodeURIComponent(String(seq))}`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) throw new Error(`JPEG HTTP ${response.status}`);
        const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
        if (!contentType.startsWith("image/jpeg")) throw new Error(`JPEG content-type 无效：${contentType || "缺失"}`);

        const headerNames = ["x-lelab-frame-seq", "x-frame-seq", "x-lelab-preview-seq", "x-preview-seq"];
        const rawHeader = headerNames
          .map((name) => response.headers.get(name))
          .find((value): value is string => value !== null && value.trim() !== "");
        if (!rawHeader) throw new Error("JPEG 响应未提供同帧序号头");
        const returnedSeq = Number(rawHeader.trim());
        if (!Number.isSafeInteger(returnedSeq) || returnedSeq !== seq) {
          throw new Error(`JPEG 帧序号 ${rawHeader} 与状态帧 ${seq} 不一致`);
        }

        const blob = await response.blob();
        if (blob.size === 0) throw new Error("JPEG 响应为空");
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setBoundFrame({ seq, url: objectUrl, fetchedAt: Date.now() });
        setFrameBinding({ seq, ok: true, reason: "JPEG 与状态帧序号一致" });
      } catch (exc: any) {
        if (cancelled) return;
        if (exc?.name === "AbortError" && !timedOut) return;
        setFrameBinding({
          seq,
          ok: false,
          reason: timedOut ? "同帧 JPEG 请求超时" : reasonText(exc?.message ?? exc),
        });
      } finally {
        window.clearTimeout(timer);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
      window.clearTimeout(timer);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [baseUrl, fetchWithHeaders, video.fresh, video.reason, video.seq]);

  // Track sequence changes for display only. The img onLoad event is deliberately ignored:
  // a cached JPEG that renders again must not be treated as a new frame.
  useEffect(() => {
    const seq = video.seq;
    if (seq === null) return;
    if (lastSeqRef.current !== seq) {
      lastSeqRef.current = seq;
      setVideoFrame({ seq, changedAt: Date.now() });
    }
  }, [video.seq]);

  // Keep the selection consistent with what the capabilities actually allow.
  useEffect(() => {
    if (profile && !profileOptions.includes(profile)) setProfile("");
  }, [profile, profileOptions]);

  const allowed = (name: string) =>
    actionAllowed({ name, gate, session, online, activeProfile }) && (name === "estop" || !busy);

  const post = useCallback(async (path: string, body: Record<string, unknown>, label: string) => {
    setBusy(label);
    try {
      const response = await apiRequest<any>(baseUrl, fetchWithHeaders, path, { method: "POST", body });
      if (isForeignSession(response, sessionRef(status))) {
        pushError(label, "响应属于旧会话/旧 epoch，请重新获取联合会话");
        return null;
      }
      setResult(response);
      setError(null);
      return response;
    } catch (exc: any) {
      pushError(label, exc?.detail ?? exc?.message ?? exc);
      return null;
    } finally {
      setBusy(null);
    }
  }, [baseUrl, fetchWithHeaders, pushError, status]);

  const control = useCallback(async (action: string) => {
    await post("/ros-mobile/hybrid/control", controlBody(action, session), action);
    await refresh();
  }, [post, session, refresh]);

  const sessionAction = useCallback(async (action: "acquire" | "release") => {
    // `owner` is required by the T3 facade's acquire_session(); `profile` selects the hybrid rig.
    const body = action === "acquire"
      ? { action, profile, owner: clientId }
      : { action, session_id: session.session_id, session_epoch: session.session_epoch };
    await post("/ros-mobile/hybrid/session", body, action === "acquire" ? "获取联合会话" : "释放联合会话");
    await refresh();
  }, [post, profile, session, refresh, clientId]);

  const record = useCallback(async (action: string, extra: Record<string, unknown> = {}) => {
    const body = { action, session_id: session.session_id, session_epoch: session.session_epoch, ...extra };
    const out = await post("/ros-mobile/hybrid/recording", body, action);
    await refresh();
    return out;
  }, [post, session, refresh]);

  // End of episode always stops motion first; a failed stop aborts the close so we never end up
  // with a closed episode while the arm/base may still be moving.
  const endEpisode = useCallback(async () => {
    const first = endEpisodeSequence()[0];
    const stopped = await post("/ros-mobile/hybrid/control", controlBody(first, session), "结束前暂停全部运动");
    if (!stopped) {
      await refresh();
      return;
    }
    await record("end_episode", { success: outcome === "unknown" ? null : outcome === "success", note });
  }, [post, session, record, outcome, note, refresh]);

  const stopRecording = useCallback(async () => {
    const first = stopSessionSequence()[0];
    const stopped = await post("/ros-mobile/hybrid/control", controlBody(first, session), "停止录制前暂停全部运动");
    if (!stopped) {
      await refresh();
      return;
    }
    await record("stop_session");
  }, [post, session, record, refresh]);

  const calibrate = useCallback(async (action: string) => {
    if (!isKnownCalibrationAction(action)) {
      pushError("标定", "未知标定动作");
      return;
    }
    const parsed = parseCalibrationDraft(draft);
    if (!parsed.ok) {
      pushError("标定", parsed.error);
      return;
    }
    await post("/ros-mobile/hybrid/calibration", { action, kind, identity, payload: parsed.value }, `标定 ${action}`);
    await refresh();
  }, [draft, kind, identity, post, pushError, refresh]);

  const statusAgeMs = lastOkMs === null ? null : Math.max(0, now - lastOkMs);
  const fresh = online && statusAgeMs !== null && statusAgeMs <= DEFAULT_STATUS_MAX_AGE_MS;
  const outputs = status?.outputs_enabled === true;
  const lastPhase = Array.isArray(recording?.phases) && recording.phases.length > 0
    ? recording.phases[recording.phases.length - 1]?.phase ?? null
    : null;
  const phase = recording?.phase ?? lastPhase ?? status?.phase ?? null;
  const recordingActive = recording?.session_id != null && recording?.active !== false;
  const episodeActive = recording?.episode_id != null;
  const frameReady = video.fresh && boundFrame?.seq === video.seq && frameBinding.ok;

  return (
    <div className="space-y-3" data-testid="hybrid-panel">
      <section className={box} data-testid="hybrid-status">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-2 font-semibold">
            <Radio className="h-4 w-4 text-sky-400" /> 联合遥操作 · 网关控制
          </h2>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className={`rounded border px-2 py-0.5 ${fresh ? "border-emerald-700 text-emerald-300" : "border-red-700 text-red-300"}`}>
              {fresh ? "状态已更新" : "状态未知或过期"}
            </span>
            <span className={`rounded border px-2 py-0.5 ${outputs ? "border-amber-700 text-amber-300" : "border-gray-700 text-gray-300"}`}>
              输出 {status?.outputs_enabled === true ? "已使能" : status?.outputs_enabled === false ? "关闭" : "未知"}
            </span>
            <span className="rounded border border-gray-700 px-2 py-0.5 text-gray-300">profile {show(status?.profile)}</span>
            <span className="rounded border border-gray-700 px-2 py-0.5 text-gray-300">state {show(status?.state)}</span>
            <span className="rounded border border-gray-700 px-2 py-0.5 text-gray-300">阶段 {show(phase)}</span>
          </div>
        </div>
        <p className="mt-1 text-xs text-gray-400">
          浏览器只发离散操作事件；手柄与主臂采样、实时控制与安全循环由车端网关处理。能力缺失、状态过期或反馈来源不明时控制自动关闭。
        </p>
        {simulationLabel(gate) && (
          <p className="mt-2 rounded border border-violet-700 bg-violet-950/40 p-2 text-xs text-violet-200" data-testid="simulation-banner">
            {simulationLabel(gate)}
          </p>
        )}
        {!gate.ok && (
          <p className="mt-2 rounded border border-amber-800/60 bg-amber-950/30 p-2 text-xs text-amber-200" data-testid="capability-block">
            控制已关闭：{show(gate.reason)}。仅“暂停全部运动/急停”保持尽力可用。
          </p>
        )}

        {error && (
          <div role="alert" className="mt-2 flex items-start gap-2 rounded border border-red-700 p-2 text-sm text-red-200" data-testid="hybrid-error">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="break-all">{error.text}</span>
            <button className="ml-auto shrink-0 underline" onClick={() => setError(null)}>关闭错误</button>
          </div>
        )}

        <div className="mt-2 flex flex-wrap gap-2">
          <select
            aria-label="联合 profile"
            value={profile}
            onChange={(event) => setProfile(event.target.value)}
            className="rounded bg-gray-900 p-1 text-sm"
          >
            <option value="">选择联合 profile</option>
            {profileOptions.map((item) => <option key={item}>{item}</option>)}
          </select>
          <button className={button} disabled={!profile || !allowed("acquire")} onClick={() => sessionAction("acquire")}>
            获取联合会话
          </button>
          <button className={button} disabled={!allowed("release")} onClick={() => sessionAction("release")}>
            释放联合会话
          </button>
          {[
            ["preview", "无输出预览"],
            ["align", "对齐"],
            ["arm", "机械臂待命"],
            ["engage", "接合离合"],
            ["release_clutch", "释放离合"],
            ["enable_base", "使能底盘"],
            ["lock_base", "锁定底盘"],
            ["clear_estop", "清除急停"],
          ].map(([action, label]) => (
            <button key={action} className={button} disabled={!allowed(action)} onClick={() => control(action)}>
              {label}
            </button>
          ))}
          <button className="rounded bg-amber-700 px-3 py-1.5 text-sm font-medium disabled:opacity-40" disabled={!allowed("pause_all")} onClick={() => control("pause_all")}>
            暂停全部运动
          </button>
          <button className="rounded bg-red-600 px-3 py-1.5 text-sm font-semibold disabled:opacity-40" disabled={!allowed("estop")} onClick={() => control("estop")}>
            急停
          </button>
        </div>
        <p className="mt-2 text-xs text-gray-400">
          会话 {show(status?.session_id)} / epoch {show(status?.session_epoch)} · 缺少条件 {show(status?.missing_for_following)} · 最近故障 {show(status?.last_error)}
        </p>
        {busy && <p className="mt-1 flex items-center gap-2 text-xs text-gray-400"><Loader2 className="h-3.5 w-3.5 animate-spin" />{busy} 进行中…</p>}
      </section>

      <section className={box} data-testid="hybrid-devices">
        <h2 className="font-semibold">设备、能力、版本与租约</h2>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <tbody>
              <tr className="border-t border-gray-800/60"><td className={th}>契约版本</td><td className={td}>{show(gate.contractVersion)}</td><td className={th}>能力来源</td><td className={td}>{gate.simulated ? "模拟声明" : "后端声明"}</td></tr>
              <tr className="border-t border-gray-800/60"><td className={th}>允许动作</td><td className={td} colSpan={3}>{gate.ok ? [...gate.actions].join(", ") : "未声明（控制关闭）"}</td></tr>
              <tr className="border-t border-gray-800/60"><td className={th}>上位机租约</td><td className={td}>{leaseText(status?.leases?.upper)}</td><td className={th}>车端租约</td><td className={td}>{leaseText(status?.leases?.car)}</td></tr>
              <tr className="border-t border-gray-800/60"><td className={th}>底盘通道</td><td className={td}>{channelText(status?.channels?.base)}</td><td className={th}>机械臂通道</td><td className={td}>{channelText(status?.channels?.arm)}</td></tr>
              <tr className="border-t border-gray-800/60"><td className={th}>夹爪通道</td><td className={td}>{channelText(status?.channels?.gripper)}</td><td className={th}>状态年龄</td><td className={td}>{statusAgeMs === null ? "未知" : `${(statusAgeMs / 1000).toFixed(1)} s`}</td></tr>
            </tbody>
          </table>
        </div>
        <p className={`mt-2 text-xs ${
          status?.calibrations?.ready === true ? "text-emerald-300" : "text-amber-300"
        }`} data-testid="calibration-readiness">
          三层标定 {status?.calibrations?.ready === true
            ? `就绪 · ${show(status?.calibrations?.bundle_hash)}`
            : `未就绪 · ${show(status?.calibrations?.reasons)}`}
        </p>
        <details className="mt-2 text-xs">
          <summary>设备与标定原始声明</summary>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap">{show({ devices: status?.devices, calibrations: status?.calibrations })}</pre>
        </details>
      </section>

      <section className={box} data-testid="hybrid-feedback">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">从臂反馈来源与年龄</h2>
          <span className={`rounded border px-2 py-0.5 text-xs ${feedbackGate.ok ? "border-emerald-700 text-emerald-300" : "border-red-700 text-red-300"}`} data-testid="feedback-gate">
            {feedbackGate.ok ? "六路实测可用于跟随" : `不可用于跟随：${show(feedbackGate.reason)}`}
          </span>
        </div>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead><tr><th className={th}>关节</th><th className={th}>来源</th><th className={th}>判定</th><th className={th}>位置 rad</th><th className={th}>raw</th><th className={th}>age</th><th className={th}>valid</th><th className={th}>错误</th></tr></thead>
            <tbody>
              {feedbackGate.rows.map((row) => (
                <tr key={row.name} className="border-t border-gray-800/60">
                  <td className="py-1 pr-2 text-gray-400">{row.name}</td>
                  <td className={td}>{show(row.sourceKind)}</td>
                  <td className={`py-1 pr-2 font-mono ${row.trust === "measured" ? "text-emerald-300" : "text-amber-300"}`}>{trustLabel(row.trust)}</td>
                  <td className={td}>{show(row.positionRad)}</td>
                  <td className={td}>{show(row.rawPosition)}</td>
                  <td className={td}>{row.ageS === null ? "未知" : `${row.ageS.toFixed(3)} s`}</td>
                  <td className={`py-1 pr-2 font-mono ${row.valid ? "text-emerald-300" : "text-red-300"}`}>{row.valid ? "是" : "否"}</td>
                  <td className="py-1 pr-2 font-mono text-red-300">{show(row.error)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-2 text-xs text-gray-500">
          只有 `servo_position_readback` 且有效、新鲜、标定匹配才算实测；`command_cache` / `model_placeholder` 永不显示为实测，`synthetic` 仅限排练 profile。缺一路即阻断跟随与九维导出。
        </p>
      </section>

      <section className={box} data-testid="hybrid-hardware-preview">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">真实主臂 → 从臂无输出预览</h2>
          <span className={`rounded border px-2 py-0.5 text-xs ${
            status?.hardware_preview?.available === true
              ? "border-emerald-700 text-emerald-300"
              : "border-amber-700 text-amber-300"
          }`}>
            {status?.hardware_preview?.available === true ? "预览数据齐全" : show(status?.hardware_preview?.reason ?? "尚未取得预览")}
          </span>
        </div>
        <p className="mt-1 text-xs text-gray-400">
          主臂 {show(status?.leader?.reason)} · 执行提交 {show(status?.hardware_preview?.motion_commands_sent)} · 输出 {status?.outputs_enabled === false ? "关闭" : "未知"}
        </p>
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead><tr><th className={th}>从臂关节</th><th className={th}>实测 rad</th><th className={th}>映射前 rad</th><th className={th}>限幅后目标 rad</th><th className={th}>软限位</th><th className={th}>截断</th></tr></thead>
            <tbody>
              {(Array.isArray(status?.hardware_preview?.targets) ? status.hardware_preview.targets : []).map((target: any) => (
                <tr key={target.joint} className="border-t border-gray-800/60">
                  <td className={td}>{show(target.joint)}</td>
                  <td className={td}>{show(status?.hardware_preview?.follower_rad?.[target.joint])}</td>
                  <td className={td}>{show(target.pre_clip_rad)}</td>
                  <td className={td}>{show(target.target_rad)}</td>
                  <td className={td}>{show(target.soft_limits_rad)}</td>
                  <td className={td}>{target.clipped ? "是" : "否"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details className="mt-2 text-xs">
          <summary>主臂、映射与参考姿态原始值</summary>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap">{show({
            leader: status?.leader,
            preview: status?.hardware_preview,
            probe: status?.hardware_probe,
          })}</pre>
        </details>
      </section>

      <section className={box} data-testid="hybrid-calibration">
        <h2 className="font-semibold">六类标定资产</h2>
        <div className="mt-2 flex flex-wrap gap-2">
          <select aria-label="标定类型" className="bg-gray-900 p-1 text-sm" value={kind} onChange={(event) => setKind(event.target.value)}>
            {CALIBRATION_KINDS.map((item) => <option key={item}>{item}</option>)}
          </select>
          <input aria-label="标定身份" placeholder="设备身份" value={identity} onChange={(event) => setIdentity(event.target.value)} className="rounded bg-gray-900 p-1 text-sm" />
          {CALIBRATION_ACTIONS.map((action) => (
            <button key={action} className={button} disabled={!identity || !isKnownCalibrationKind(kind) || !allowed(action)} onClick={() => calibrate(action)}>
              {action === "validate" ? "校验标定" : "保存标定"}
            </button>
          ))}
        </div>
        <textarea aria-label="标定 JSON" value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} className="mt-2 w-full rounded bg-gray-900 p-2 font-mono text-xs" />
        <p className="text-xs text-gray-400">资产内容、版本与实测来源由后端校验；示例值或未知数据不可视作实测。保存标定要求当前没有活动联合会话（T3 会拒绝在会话中换标定）。</p>
      </section>

      <section className={box} data-testid="hybrid-video">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">联合视频</h2>
          <span className={`text-xs ${frameReady ? "text-emerald-300" : "text-amber-300"}`} data-testid="video-gate">
            {frameReady ? frameBinding.reason : `不可用于操作：${frameBinding.reason}`}
          </span>
        </div>
        <p className="mt-1 text-xs text-gray-400">
          source {show(video.source)} · {video.seqField ?? "frame_seq"} {show(video.seq)} · 源 age {video.sourceAgeS === null ? "未知" : `${video.sourceAgeS.toFixed(3)} s`} · 序号未变化 {video.seqUnchangedS === null ? "未知" : `${video.seqUnchangedS.toFixed(1)} s`}
        </p>
        <div className="relative mt-2">
          {frameReady && boundFrame ? (
            <img
              src={boundFrame.url}
              alt={`联合视频预览，帧 ${boundFrame.seq}`}
              className="aspect-video w-full bg-black object-contain"
            />
          ) : (
            <div className="aspect-video w-full bg-black" />
          )}
          {!frameReady && (
            <div className="absolute inset-0 flex items-center justify-center bg-black/70 p-3 text-center text-xs text-amber-200">
              JPEG 与状态帧尚未绑定：HTTP 200、可解码或缓存命中都不等于新画面
            </div>
          )}
        </div>
        <p className="mt-1 text-xs text-gray-500">
          JPEG 响应必须携带与源 frame_seq/preview_seq 一致的同帧序号头；不把 img onLoad 或旁路计数当作新帧证据。
          {boundFrame ? ` 最近绑定 ${boundFrame.seq} @ ${new Date(boundFrame.fetchedAt).toLocaleTimeString()}` : ""}
        </p>
      </section>

      <section className={box} data-testid="hybrid-recording">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">同一 episode · 人工阶段</h2>
          <span className="text-xs text-gray-400">录制 {show(recording?.session_id ?? recording?.active)} · episode {show(recording?.episode_id)} · 阶段 {show(phase)}</span>
        </div>
        <p className="mt-1 text-xs text-gray-400">
          {status?.transport === "frame_stream_v1"
            ? "开始新回合与切换阶段都会先暂停并确认车端录制上下文；完成后需重新对齐并接合，才会继续跟随。"
            : "阶段切换记录阶段事件，不自动开始新回合；结束本集前会暂停运动。"}
          {recordingFromStatus ? " 录制信息来自 status 回退，请以 recording/status 为准。" : ""}
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          <button className={button} disabled={!allowed("start_session")} onClick={() => record("start_session")}>开始联合录制</button>
          <button className={button} disabled={!allowed("start_episode")} onClick={() => record("start_episode")}>开始联合回合</button>
          {MANUAL_PHASES.map((item) => (
            <button
              key={item}
              className={button}
              disabled={!allowed("set_phase") || !episodeActive}
              onClick={() => post("/ros-mobile/hybrid/recording", phaseBody(item, session), `阶段 ${item}`).then(refresh)}
            >
              阶段 {item}
            </button>
          ))}
          <button className={button} disabled={!allowed("pause_all")} onClick={() => control("pause_all")}>暂停录制期间的运动</button>
          <select aria-label="人工结果" value={outcome} onChange={(event) => setOutcome(event.target.value)} className="bg-gray-900 p-1 text-sm">
            <option value="unknown">unknown（默认）</option>
            <option value="success">success</option>
            <option value="failure">failure</option>
          </select>
          <input aria-label="回合备注" placeholder="回合备注" value={note} onChange={(event) => setNote(event.target.value)} className="rounded bg-gray-900 p-1 text-sm" />
          <button className="rounded bg-emerald-700 px-3 py-1.5 text-sm disabled:opacity-40" disabled={!episodeActive || !allowed("end_episode") || !allowed("pause_all")} onClick={endEpisode}>
            暂停并结束联合回合
          </button>
          <button className={button} disabled={!recordingActive || !allowed("stop_session")} onClick={stopRecording}>暂停并停止联合录制</button>
        </div>
        <p className="mt-2 text-xs text-gray-500">成功/失败由操作员在结束时标注，默认 unknown；不套用仿真真值。</p>
      </section>

      <HybridDataReview
        baseUrl={baseUrl}
        fetchWithHeaders={fetchWithHeaders}
        online={online}
        session={session}
        profileReady={
          gate.ok &&
          activeProfile !== null &&
          profileOptions.includes(activeProfile) &&
          session.valid &&
          feedbackGate.ok
        }
        simulated={simulated}
        syncAllowed={allowed("sync")}
        exportAllowed={allowed("export")}
      />

      {result != null && (
        <details className="rounded border border-gray-800 p-2 text-xs">
          <summary>最近操作响应（浏览器只记录网关回执，不代表机械执行成功）</summary>
          <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap">{show(result)}</pre>
        </details>
      )}
    </div>
  );
}
