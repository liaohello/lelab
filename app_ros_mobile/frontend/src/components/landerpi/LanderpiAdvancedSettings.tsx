import React, { useCallback, useEffect, useRef, useState } from "react";
import { Settings2 } from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

const JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"] as const;
const MAPPING_ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5"] as const;
const LEADER_FOR_FOLLOWER: Record<string, string> = {
  joint1: "shoulder_pan",
  joint2: "shoulder_lift",
  joint3: "elbow_flex",
  joint4: "wrist_flex",
  joint5: "wrist_roll",
  r_joint: "gripper",
};

type AnchorDraft = {
  enabled: boolean;
  leaderLow: string;
  leaderHigh: string;
  followerLow: string;
  followerHigh: string;
};

function initialAnchorDrafts(): Record<string, AnchorDraft> {
  return Object.fromEntries(MAPPING_ARM_JOINTS.map((name) => [
    name,
    name === "joint2"
      ? { enabled: true, leaderLow: "865", leaderHigh: "3250", followerLow: "900", followerHigh: "50" }
      : { enabled: false, leaderLow: "", leaderHigh: "", followerLow: "", followerHigh: "" },
  ]));
}

function draftsFromMapping(mapping: any): Record<string, AnchorDraft> {
  const drafts = initialAnchorDrafts();
  const rows = new Map<string, any>(
    ((mapping?.joints as any[]) ?? []).map((item) => [item?.follower, item]),
  );
  for (const name of MAPPING_ARM_JOINTS) {
    const row = rows.get(name);
    const anchors = row?.absolute_raw;
    if (row?.mapping_mode === "absolute_raw_linear" && anchors) {
      drafts[name] = {
        enabled: true,
        leaderLow: String(anchors.leader_low_raw ?? ""),
        leaderHigh: String(anchors.leader_high_raw ?? ""),
        followerLow: String(anchors.follower_at_leader_low_raw ?? ""),
        followerHigh: String(anchors.follower_at_leader_high_raw ?? ""),
      };
    } else if (name !== "joint2") {
      drafts[name].enabled = false;
    }
  }
  return drafts;
}

function errText(exc: unknown): string {
  const anyExc = exc as any;
  return String(anyExc?.detail ?? anyExc?.message ?? exc);
}

/** Mapping anchors, feedback ranges and pairing measurements live here, not on
 * the capture page: they change data meaning and must not be edited mid-recording. */
export default function LanderpiAdvancedSettings() {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [status, setStatus] = useState<any>(null);
  const [mapping, setMapping] = useState<any>(null);
  const [anchorDrafts, setAnchorDrafts] = useState<Record<string, AnchorDraft>>(initialAnchorDrafts);
  const [feedbackSettings, setFeedbackSettings] = useState<any>(null);
  const [feedbackDraft, setFeedbackDraft] = useState<Record<string, [string, string]>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [measurementTargets, setMeasurementTargets] = useState<Record<string, string>>({});
  const [measurements, setMeasurements] = useState<any[]>(() => {
    try {
      const raw = window.localStorage.getItem("landerpi.measurement-points.v1");
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const mappingDraftsDirty = useRef(false);
  const mappingVersion = useRef<string | null>(null);

  const request = useCallback(<T,>(path: string, body?: unknown, method = body === undefined ? "GET" : "POST") =>
    apiRequest<T>(baseUrl, fetchWithHeaders, path, { method, body }), [baseUrl, fetchWithHeaders]);

  const session = {
    session_id: status?.session_id ?? null,
    session_epoch: status?.session_epoch ?? null,
  };
  const hasSession = Boolean(session.session_id);
  const recordingActive = status?.recording?.status === "active";

  const loadMapping = useCallback(async () => {
    try {
      const next = await request<any>("/ros-mobile/hybrid/mapping");
      setMapping(next);
      const nextVersion = String(next.version ?? next.sha256 ?? "");
      if (!mappingDraftsDirty.current && nextVersion !== mappingVersion.current) {
        mappingVersion.current = nextVersion;
        setAnchorDrafts(draftsFromMapping(next));
      }
    } catch (exc) {
      setError(`映射读取失败：${errText(exc)}`);
    }
  }, [request]);

  const loadFeedbackSettings = useCallback(async () => {
    try {
      const settings = await request<any>("/ros-mobile/hybrid/feedback-limits");
      setFeedbackSettings(settings);
      setFeedbackDraft(Object.fromEntries(JOINTS.map((name) => [name,
        [String(settings.ranges[name][0]), String(settings.ranges[name][1])]])));
    } catch (exc) {
      setError(`范围读取失败：${errText(exc)}`);
    }
  }, [request]);

  useEffect(() => {
    void loadMapping();
    void loadFeedbackSettings();
    const poll = window.setInterval(async () => {
      try {
        setStatus(await request<any>("/ros-mobile/hybrid/status"));
      } catch {
        setStatus(null);
      }
    }, 2000);
    return () => window.clearInterval(poll);
  }, [request, loadMapping, loadFeedbackSettings]);

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (exc) {
      setError(errText(exc));
    } finally {
      setBusy(null);
    }
  };

  const pauseAndRelease = async (reason: string) => {
    if (!hasSession) return;
    try {
      await request("/ros-mobile/hybrid/control", { action: "pause_all", ...session });
    } catch {
      // A faulted session may already be stopped; release is still required.
    }
    await request("/ros-mobile/hybrid/session", { action: "release", reason, ...session });
  };

  const updateAnchorDraft = (name: string, patch: Partial<AnchorDraft>) => {
    mappingDraftsDirty.current = true;
    setAnchorDrafts((previous) => ({
      ...previous,
      [name]: { ...(previous[name] ?? initialAnchorDrafts()[name]), ...patch },
    }));
  };

  const saveAnchors = () => run("保存映射端点", async () => {
    if (hasSession) throw new Error("当前仍持有控制权。请先暂停并释放控制权，再保存新映射。");
    const anchors: Record<string, any> = {};
    for (const name of MAPPING_ARM_JOINTS) {
      const draft = anchorDrafts[name] ?? initialAnchorDrafts()[name];
      if (!draft.enabled) {
        anchors[name] = { mode: "relative" };
        continue;
      }
      const rawValues = [draft.leaderLow, draft.leaderHigh, draft.followerLow, draft.followerHigh];
      if (rawValues.some((value) => value.trim() === "")) throw new Error(`${name} 已启用绝对映射，请填写全部四个 raw 端点。`);
      const numericRawValues = rawValues.map(Number);
      if (numericRawValues.some((value) => !Number.isInteger(value))) throw new Error(`${name} 的四个端点都必须是整数 raw。`);
      if (numericRawValues[0] === numericRawValues[1]) throw new Error(`${name} 的主臂低端点与高端点不能相同。`);
      if (numericRawValues[2] === numericRawValues[3]) throw new Error(`${name} 的两个对应从臂 raw 不能相同。`);
      anchors[name] = {
        mode: "absolute_raw_linear",
        leader_low_raw: numericRawValues[0],
        leader_high_raw: numericRawValues[1],
        follower_at_leader_low_raw: numericRawValues[2],
        follower_at_leader_high_raw: numericRawValues[3],
      };
    }
    const saved = await request<any>("/ros-mobile/hybrid/mapping/anchors", { anchors });
    mappingDraftsDirty.current = false;
    setNotice(`映射已保存为新版本 ${String(saved?.version ?? "").slice(0, 12)}；重新获取控制、对齐并显式接合后生效。`);
    await loadMapping();
  });

  const saveFeedbackSettings = () => run("保存反馈允许范围", async () => {
    if (!feedbackSettings) throw new Error("feedback_limits_unavailable");
    if (recordingActive) throw new Error("feedback_limits_recording_active: 先结束当前采集，再修改范围。");
    const ranges = Object.fromEntries(JOINTS.map((name) => {
      const pair = feedbackDraft[name];
      if (!pair || pair.some((value) => !value.trim() || !Number.isFinite(Number(value)))) {
        throw new Error(`请输入 ${name} 的有效上下限`);
      }
      const [low, high] = pair.map(Number);
      const editable = feedbackSettings.editable_bounds?.[name] ?? [0, 1000];
      if (low >= high) throw new Error(`${name} 的最小值必须小于最大值`);
      if (low < editable[0] || high > editable[1]) throw new Error(`${name} 范围必须在 ${editable[0]}..${editable[1]} 内`);
      return [name, [low, high]];
    }));
    if (hasSession) await pauseAndRelease("feedback_limits_update");
    const saved = await request<any>("/ros-mobile/hybrid/feedback-limits", {
      ranges, expected_version: feedbackSettings.version,
    });
    await loadFeedbackSettings();
    setNotice(`反馈允许范围已保存为 ${saved.version}；重新获取控制、对齐并显式接合后生效。`);
  });

  const captureMeasurement = () => {
    if (!status?.leader?.raw_available || !status?.leader?.raw_positions) {
      setNotice("主臂原始舵机读数尚未就绪；先在采集页获取控制或等待读数刷新。");
      return;
    }
    const targetRaw: Record<string, number | null> = {};
    for (const name of JOINTS) {
      const parsed = Number(measurementTargets[name]);
      targetRaw[name] = Number.isFinite(parsed) && measurementTargets[name] !== "" ? parsed : null;
    }
    const targetByJoint = new Map<string, any>(
      ((status.hardware_preview?.targets as any[]) ?? []).map((item) => [item.joint, item]),
    );
    const row = {
      captured_utc: new Date().toISOString(),
      calibration_bundle_hash: status.calibration_bundle_hash ?? null,
      session_id: status.session_id ?? null,
      data_identity: "hardware",
      leader: status.leader.raw_positions,
      follower_raw: status.feedback?.servo_pulses ?? {},
      follower_rad: status.feedback?.positions_rad ?? {},
      requested_target_raw: targetRaw,
      measured_target_rad: Object.fromEntries(
        JOINTS.map((name) => [name, targetByJoint.get(name)?.target_rad ?? null]),
      ),
      note: "operator-recorded pose; target_raw is the operator's desired follower target, not a command",
    };
    const next = [...measurements, row];
    setMeasurements(next);
    try {
      window.localStorage.setItem("landerpi.measurement-points.v1", JSON.stringify(next));
    } catch {
      // The visible record remains available even if localStorage is disabled.
    }
    setNotice(`已记录第 ${next.length} 个主臂/从臂配对点；没有发送目标。`);
  };

  const leaderRaw = status?.leader?.raw_positions ?? {};
  const mappingRows = new Map<string, any>(
    ((mapping?.joints as any[]) ?? []).map((item) => [item?.follower, item]),
  );

  return (
    <div className="mx-auto max-w-[1440px] space-y-4 px-4 py-5 sm:px-6">
      <div className="rounded-xl border border-amber-800/70 bg-amber-950/25 p-3 text-sm text-amber-100">
        高级设置：映射端点、反馈范围与配对测量会改变数据含义。录制进行中禁止保存；保存前需暂停并释放控制权。
      </div>
      {error && <p role="alert" className="rounded-xl border border-red-800 bg-red-950/40 p-3 text-sm text-red-100">{error}</p>}
      {notice && <p className="rounded-xl border border-sky-800/60 bg-sky-950/30 p-3 text-sm text-sky-200">{notice}</p>}

      <section className="rounded-xl border border-cyan-900/70 bg-cyan-950/15 p-4" data-testid="mapping-anchor-editor">
        <div className="flex items-center gap-2">
          <Settings2 className="h-4 w-4 text-cyan-300" />
          <h2 className="font-semibold text-cyan-100">主臂 → 从臂映射端点</h2>
          <span className="ml-auto text-[11px] text-cyan-300/80">当前映射 {mapping?.version?.slice(0, 12) ?? "—"}</span>
        </div>
        {hasSession && (
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-800/80 bg-amber-950/25 p-2.5">
            <p className="text-xs text-amber-200">当前会话会锁定旧映射。释放控制权后编辑，避免运行中静默替换目标。</p>
            <button type="button" disabled={!!busy} onClick={() => void run("暂停并释放控制权", () => pauseAndRelease("operator editing mapping anchors"))}
              className="rounded-lg border border-amber-600 px-3 py-2 text-xs font-medium text-amber-100 disabled:opacity-40">
              暂停并释放控制权以编辑
            </button>
          </div>
        )}
        <div className="mt-3 space-y-2">
          {MAPPING_ARM_JOINTS.map((name) => {
            const draft = anchorDrafts[name] ?? initialAnchorDrafts()[name];
            const row = mappingRows.get(name);
            const rawRange = row?.follower_raw_range as number[] | null | undefined;
            return (
              <div key={name} className="rounded-lg border border-slate-800 bg-slate-950/70 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <label className="flex items-center gap-2 text-sm text-slate-100">
                    <input type="checkbox" checked={draft.enabled}
                      onChange={(event) => updateAnchorDraft(name, { enabled: event.target.checked })}
                      className="h-4 w-4 accent-cyan-500" />
                    <span className="font-mono">{name}</span>
                    <span className="text-xs text-slate-500">← {LEADER_FOR_FOLLOWER[name]}</span>
                  </label>
                  <span className={`rounded-full border px-2 py-0.5 text-[11px] ${row?.mapping_mode === "absolute_raw_linear" ? "border-cyan-700 text-cyan-300" : "border-slate-700 text-slate-400"}`}>
                    当前：{row?.mapping_mode === "absolute_raw_linear" ? "绝对 raw" : "相对增量"}
                  </span>
                </div>
                <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                  {([
                    ["主臂低端点 raw", "leaderLow", 0, 4095],
                    ["对应从臂 raw", "followerLow", rawRange?.[0] ?? 0, rawRange?.[1] ?? 1000],
                    ["主臂高端点 raw", "leaderHigh", 0, 4095],
                    ["对应从臂 raw", "followerHigh", rawRange?.[0] ?? 0, rawRange?.[1] ?? 1000],
                  ] as const).map(([label, key, min, max]) => (
                    <label key={key} className="text-[10px] text-slate-500">
                      {label}
                      <input type="number" min={min} max={max} step={1} disabled={!draft.enabled}
                        value={(draft as any)[key]}
                        onChange={(event) => updateAnchorDraft(name, { [key]: event.target.value } as Partial<AnchorDraft>)}
                        className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-sm text-slate-100 disabled:opacity-35" />
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" disabled={!!busy || hasSession} onClick={() => void saveAnchors()}
            className="rounded-lg bg-cyan-500 px-3 py-2 text-sm font-semibold text-slate-950 disabled:opacity-35">
            保存为新映射版本
          </button>
          <button type="button" disabled={!!busy || !mapping} onClick={() => {
            mappingDraftsDirty.current = false;
            setAnchorDrafts(draftsFromMapping(mapping));
          }} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 disabled:opacity-35">
            丢弃未保存修改
          </button>
        </div>
      </section>

      <section className="rounded-xl border border-amber-900/70 bg-slate-900/65 p-4" data-testid="feedback-limits-editor">
        <h2 className="font-semibold">六轴范围配置 · raw</h2>
        <p className="mt-1 text-xs text-slate-400">
          反馈允许范围决定实测位置是否可接受；目标软限位只限制实际发送目标。保存前要求无位置输出并释放控制权；录制中拒绝修改。
        </p>
        <div className="mt-2 text-[11px] text-slate-500">
          保存版本 <span className="font-mono text-slate-300">{feedbackSettings?.version ?? "—"}</span>
          · 驱动已加载 <span className="font-mono text-slate-300">{status?.frame_process?.feedback_raw_limits?.version ?? "—"}</span>
        </div>
        <div className="mt-3 space-y-2">
          {JOINTS.map((name) => {
            const editable = feedbackSettings?.editable_bounds?.[name] as number[] | undefined;
            const pair = feedbackDraft[name] ?? ["", ""];
            return (
              <div key={name} className="rounded-lg border border-slate-800 bg-slate-950/60 p-3">
                <div className="flex items-center justify-between">
                  <p className="font-mono text-sm text-slate-100">{name}</p>
                  <p className="text-[11px] text-slate-500">当前实测 <b className="text-slate-200">{status?.feedback?.servo_pulses?.[name] ?? "—"} raw</b></p>
                </div>
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  {([0, 1] as const).map((index) => (
                    <label key={index} className="text-[11px] text-amber-200/80">
                      {index === 0 ? "反馈允许最小值" : "反馈允许最大值"}
                      <input type="number" step={1} aria-label={`${name} 反馈${index === 0 ? "最小值" : "最大值"}`}
                        value={pair[index]} disabled={!!busy || !feedbackSettings}
                        min={editable?.[0]} max={editable?.[1]}
                        onChange={(event) => setFeedbackDraft((old) => ({
                          ...old,
                          [name]: index === 0 ? [event.target.value, old[name]?.[1] ?? ""]
                            : [old[name]?.[0] ?? "", event.target.value],
                        }))}
                        className="mt-1 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1.5 font-mono text-slate-100" />
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button type="button" disabled={!!busy || !feedbackSettings || recordingActive}
            onClick={() => void saveFeedbackSettings()}
            className="rounded border border-amber-700 px-3 py-2 text-sm text-amber-200 disabled:opacity-40">
            保存允许范围{hasSession ? "（自动暂停并释放）" : ""}
          </button>
          <button type="button" disabled={!!busy} onClick={() => void loadFeedbackSettings()} className="text-xs text-slate-400 underline">
            重新读取配置
          </button>
        </div>
      </section>

      <section className="rounded-xl border border-emerald-900/70 bg-emerald-950/15 p-4" data-testid="leader-follower-measurement">
        <h2 className="font-semibold text-emerald-100">人工映射测量 · 主臂位置 → 从臂目标</h2>
        <p className="mt-1 text-xs text-slate-400">只保存测量点，不向从臂发送目标。主臂 raw 是 Feetech tick，从臂目标 raw 是 0–1000 舵机位置。</p>
        <div className="mt-3 space-y-2">
          {JOINTS.map((name) => (
            <div key={name} className="grid items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/60 p-2 sm:grid-cols-[110px_repeat(3,minmax(0,1fr))]">
              <div>
                <p className="font-mono text-sm text-slate-100">{name}</p>
                <p className="text-[10px] text-slate-500">← {LEADER_FOR_FOLLOWER[name]}</p>
              </div>
              <div><p className="text-[10px] text-slate-500">主臂 raw</p><p className="font-mono text-sm text-violet-200">{leaderRaw[LEADER_FOR_FOLLOWER[name]] ?? "—"}</p></div>
              <div><p className="text-[10px] text-slate-500">从臂实测 raw</p><p className="font-mono text-sm text-slate-200">{status?.feedback?.servo_pulses?.[name] ?? "—"}</p></div>
              <label className="text-[10px] text-emerald-300">
                指定从臂目标 raw
                <input type="number" min={0} max={1000} step={1} value={measurementTargets[name] ?? ""}
                  onChange={(event) => setMeasurementTargets((previous) => ({ ...previous, [name]: event.target.value }))}
                  className="mt-1 w-full rounded border border-emerald-800 bg-slate-950 px-2 py-1.5 font-mono text-sm text-emerald-100" />
              </label>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" disabled={!status?.leader?.raw_available} onClick={captureMeasurement}
            className="rounded-lg bg-emerald-600 px-3 py-2 text-sm font-semibold text-slate-950 disabled:opacity-35">
            记录当前配对
          </button>
          <button type="button" disabled={measurements.length === 0} onClick={() => {
            setMeasurements([]);
            try { window.localStorage.removeItem("landerpi.measurement-points.v1"); } catch { /* ignore */ }
          }} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 disabled:opacity-35">
            清空本页记录
          </button>
          <span className="text-xs text-slate-500">已记录 {measurements.length} 个配对点 · 不自动发送</span>
        </div>
      </section>
    </div>
  );
}