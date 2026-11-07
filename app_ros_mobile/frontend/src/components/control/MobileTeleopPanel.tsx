import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Loader2, ShieldAlert } from "lucide-react";
import { useApi } from "@/contexts/ApiContext";
import { apiRequest } from "@/lib/apiClient";

/**
 * SO-101 leader → car follower teleoperation. The browser only drives the flow and displays it:
 * the serial loop, the lease pairing, the anti-jump limits and the estop live in the upper-host
 * coordinator and the car gateway.
 */

const LAYERS = [
  { kind: "leader", label: "A · SO-101 主臂标定", hint: "复用现有主臂标定；主臂 wizard 会写 EEPROM，calibrate=False 也不等于无写入" },
  { kind: "follower", label: "B · 小车从臂校准", hint: "原始脉冲↔物理弧度、方向、零位、软限位与夹爪关系；不要求拖动带载机械臂" },
  { kind: "mapping", label: "C · 主从映射", hint: "仅关节空间映射；夹爪按 百分比→脉冲→连杆几何 换算，绝不把百分比当弧度" },
] as const;

const LIVE_STATES = ["preview", "aligned", "armed", "following", "paused"];

function tone(state: string): "ok" | "warn" | "bad" | "idle" {
  if (state === "following") return "ok";
  if (state === "armed" || state === "aligned" || state === "preview" || state === "calibrated") return "warn";
  if (state === "estop" || state === "fault") return "bad";
  return "idle";
}

function num(value: unknown, digits = 3): string {
  return value == null || Number.isNaN(Number(value)) ? "缺失" : Number(value).toFixed(digits);
}

function age(value: unknown): string {
  return value == null ? "缺失" : `${Number(value).toFixed(2)}s`;
}

export default function MobileTeleopPanel({ readOnly }: { readOnly: boolean }) {
  const { baseUrl, fetchWithHeaders } = useApi();
  const [teleop, setTeleop] = useState<any>(null);
  const [preview, setPreview] = useState<any>(null);
  const [devices, setDevices] = useState<any>(null);
  const [readiness, setReadiness] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [identities, setIdentities] = useState({ leader: "", follower: "", mapping: "" });
  const [port, setPort] = useState<string>("");
  const [task, setTask] = useState("teleop grasp and place");
  const [episodeLabel, setEpisodeLabel] = useState<string>("unknown");
  const [episodeNote, setEpisodeNote] = useState<string>("");
  const [layer, setLayer] = useState<string>("leader");
  const [draft, setDraft] = useState<string>("");
  const [draftNote, setDraftNote] = useState<string>("");
  const [leaderCalibs, setLeaderCalibs] = useState<any>(null);
  const polling = useRef(false);

  const post = useCallback(async (path: string, body?: any, label?: string) => {
    setError(null);
    setBusy(label ?? path);
    try {
      return await apiRequest<any>(baseUrl, fetchWithHeaders, path, { method: "POST", body: body ?? {}, action: label });
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
      return null;
    } finally {
      setBusy(null);
    }
  }, [baseUrl, fetchWithHeaders]);

  const get = useCallback(async (path: string, label?: string) => {
    try {
      return await apiRequest<any>(baseUrl, fetchWithHeaders, path, { action: label });
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
      return null;
    }
  }, [baseUrl, fetchWithHeaders]);

  const refresh = useCallback(async () => {
    if (polling.current) return;
    polling.current = true;
    try {
      // /teleop/status is local to the backend and carries the last observed gate; readiness is the
      // only call that re-polls the car, so it runs solely while this session holds the leases.
      const [status, ready] = await Promise.all([
        get("/ros-mobile/teleop/status", "遥操作状态"),
        teleop?.car_token_active ? get("/ros-mobile/teleop/readiness", "跟随前置条件") : Promise.resolve(null),
      ]);
      if (status) setTeleop(ready ? { ...status, readiness: ready } : status);
    } finally {
      polling.current = false;
    }
  }, [get, teleop?.car_token_active]);

  useEffect(() => {
    if (readOnly) return;
    refresh();
    const id = window.setInterval(refresh, 900);
    return () => window.clearInterval(id);
  }, [readOnly, refresh]);

  useEffect(() => {
    if (readOnly) return;
    get("/ros-mobile/teleop/devices", "主臂串口").then((data) => {
      if (!data) return;
      setDevices(data);
    });
  }, [readOnly, get]);

  const state = teleop?.state ?? "disconnected";
  const snapshot = teleop?.snapshot ?? {};
  const leader = teleop?.leader ?? {};
  const feedback = preview?.feedback ?? {};
  const live = LIVE_STATES.includes(state);

  // The preview is computed on the upper host and never reaches an actuator; poll it only
  // while a session is past calibration so a disconnected page does not hammer the car gateway.
  useEffect(() => {
    if (readOnly || !live) return;
    let cancelled = false;
    const id = window.setInterval(async () => {
      const data = await get("/ros-mobile/teleop/preview", "映射预览");
      if (!cancelled && data) setPreview(data);
    }, 400);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [readOnly, live, get]);

  const sendState = useCallback(async (event: string, label: string, extra?: Record<string, string>) => {
    const out = await post("/ros-mobile/teleop/state", { event, ...(extra ?? {}) }, label);
    if (out) setTeleop((current: any) => ({ ...(current ?? {}), state: out.state, snapshot: out }));
    await refresh();
  }, [post, refresh]);

  const loadLayerExample = async (kind: string) => {
    const data = await get(`/ros-mobile/teleop/calibration/example?kind=${kind}`, "载入标定模板");
    if (!data) return;
    setDraft(JSON.stringify(data.payload, null, 2));
    setDraftNote(`模板来自 ${data.path}：标记 example_not_measured / derived_not_measured 的值必须逐项实测替换后再保存`);
  };

  const loadLayerSaved = async (kind: string, identity: string) => {
    if (!identity) { setError("请先填写该层的设备身份 (identity)"); return; }
    const data = await get(`/ros-mobile/teleop/calibration?kind=${kind}&identity=${encodeURIComponent(identity)}`, "读取已存标定");
    if (!data) return;
    setDraft(JSON.stringify(data.data, null, 2));
    setDraftNote(`已加载版本 ${data.version} · sha256 ${String(data.sha256).slice(0, 12)}…`);
  };

  const saveLayer = async () => {
    const identity = identities[layer as keyof typeof identities];
    if (!identity) { setError("请先填写该层的设备身份 (identity)"); return; }
    let payload: unknown;
    try {
      payload = JSON.parse(draft);
    } catch (exc: any) {
      setError(`JSON 解析失败：${exc.message}`);
      return;
    }
    const out = await post("/ros-mobile/teleop/calibration", { kind: layer, identity, payload }, "保存标定层");
    if (out) setDraftNote(`已原子写入 ${out.kind} 层，版本 ${out.version}`);
  };

  const checkReadiness = async () => {
    const params = new URLSearchParams(identities).toString();
    const data = await get(`/ros-mobile/teleop/calibration/readiness?${params}`, "三层标定就绪检查");
    if (data) setReadiness(data);
  };

  const listLeaderCalibrations = async () => {
    const data = await get("/ros-mobile/teleop/leader-calibrations", "现有主臂标定");
    if (data) setLeaderCalibs(data);
  };

  const useLeaderCalibration = (entry: any) => {
    // Reuse the numbers the existing SO-101 wizard produced instead of retyping 6 motors x 5 fields.
    let payload: any;
    try {
      payload = draft ? JSON.parse(draft) : {};
    } catch (exc: any) {
      setError(`当前草稿不是合法 JSON，先"载入模板"再填入现有标定：${exc.message}`);
      return;
    }
    payload.lerobot_calibration = entry.calibration;
    payload.source = { ...(payload.source ?? {}), lerobot_calibration_file: entry.path,
                       read_only_reuse: true,
                       note: "本页面只读取该文件，从不写回 LeRobot 的标定树" };
    setDraft(JSON.stringify(payload, null, 2));
    setDraftNote(`已填入 ${entry.robot_type}/${entry.name}（sha256 ${entry.sha256}…，来自 ${entry.path}）；`
                 + "仍需你确认这台就是当前接上的主臂，再原子保存");
  };

  const joints = useMemo(() => {
    const targets = preview?.targets ?? {};
    const names = new Set<string>([...Object.keys(targets), ...Object.keys(feedback?.positions_rad ?? {})]);
    return [...names].sort();
  }, [preview?.targets, feedback?.positions_rad]);

  if (readOnly) {
    return (
      <section className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
        <div className="flex items-start gap-2 rounded border border-amber-800/60 bg-amber-950/30 p-2 text-xs text-amber-200">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>实机只读模式保留无输出默认：不开放主臂串口、遥操作会话与录制入口。要遥操作请部署 hardware-teleop-v1 profile。</span>
        </div>
      </section>
    );
  }

  return (
    <div className="space-y-3">
      {error && (
        <div className="flex items-start gap-2 rounded border border-red-800 bg-red-950/50 p-2 text-sm text-red-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> <span className="break-all">{error}</span>
          <button onClick={() => setError(null)} className="ml-auto shrink-0 underline">关闭错误</button>
        </div>
      )}

      <section className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">设备与三层标定</h2>
        <div className="grid gap-2 md:grid-cols-2">
          <label className="text-xs text-gray-400">
            主臂串口（USB 在本上位机，网络可达≠串口可用）
            <select value={port} onChange={(e) => setPort(e.target.value)}
                    className="mt-1 w-full rounded border border-gray-700 bg-gray-900 p-1.5 text-xs text-gray-100">
              <option value="">未选择</option>
              {(devices?.ports ?? []).map((p: any) => (
                <option key={p.by_id} value={p.by_id}>序列 {p.serial}（当前 {p.device}）· {p.by_id}</option>
              ))}
            </select>
            <p className="mt-1 text-[11px] text-gray-500">
              只有 /dev/serial/by-id 序列是稳定身份，ttyACM 编号会随枚举变化；端口不会自动选中，请确认序列号对应哪支主臂。
            </p>
          </label>
          <div className="text-xs text-gray-400">
            设备身份
            {(["leader", "follower", "mapping"] as const).map((kind) => (
              <input key={kind} value={identities[kind]} placeholder={`${kind} identity`}
                     onChange={(e) => setIdentities({ ...identities, [kind]: e.target.value })}
                     className="mt-1 w-full rounded border border-gray-700 bg-gray-900 p-1.5 text-xs text-gray-100" />
            ))}
          </div>
        </div>
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          <button onClick={() => post("/ros-mobile/teleop/leader", { action: "open", port, leader_identity: identities.leader }, "打开主臂串口")}
                  disabled={!port || !!busy || leader.connected || !identities.leader.trim()}
                  className="rounded border border-gray-700 px-2.5 py-1 disabled:opacity-40">只读打开主臂</button>
          <button onClick={() => post("/ros-mobile/teleop/leader", { action: "close" }, "关闭主臂串口")}
                  disabled={!leader.connected || !!busy}
                  className="rounded border border-gray-700 px-2.5 py-1 disabled:opacity-40">释放串口</button>
          <button onClick={checkReadiness} disabled={!!busy} className="rounded border border-gray-700 px-2.5 py-1">检查三层就绪</button>
          <span className="self-center text-gray-500">
            主臂：{leader.connected ? `已连接 ${num(leader.age_s)}s` : `未连接（${leader.reason ?? "尚未打开"}）`} ·
            健康 {leader.healthy ? "是" : "否"}
          </span>
        </div>

        <div className="mt-3 flex gap-1 text-xs">
          {LAYERS.map((item) => (
            <button key={item.kind} onClick={() => setLayer(item.kind)}
                    className={`rounded px-2 py-1 ${layer === item.kind ? "bg-sky-800 text-white" : "border border-gray-700 text-gray-300"}`}>
              {item.label}
            </button>
          ))}
        </div>
        <p className="mt-1 text-[11px] text-gray-500">{LAYERS.find((l) => l.kind === layer)?.hint}</p>
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={10} spellCheck={false}
                  className="mt-2 w-full rounded border border-gray-800 bg-black/60 p-2 font-mono text-[11px] text-gray-200" />
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
          <button onClick={() => loadLayerExample(layer)} className="rounded border border-gray-700 px-2.5 py-1">载入模板</button>
          <button onClick={() => loadLayerSaved(layer, identities[layer as keyof typeof identities])}
                  className="rounded border border-gray-700 px-2.5 py-1">读取已存</button>
          <button onClick={saveLayer} disabled={!draft || !!busy} className="rounded bg-emerald-700 px-2.5 py-1 disabled:opacity-40">
            原子保存该层
          </button>
          <span className="break-all text-gray-500">{draftNote}</span>
        </div>
        {layer === "leader" && (
          <div className="mt-2 rounded border border-gray-800 bg-black/30 p-2 text-xs">
            <button onClick={listLeaderCalibrations} disabled={!!busy}
                    className="rounded border border-gray-700 px-2.5 py-1 disabled:opacity-40">
              列出现有主臂标定
            </button>
            {!leaderCalibs && <span className="ml-2 text-gray-500">复用现有 SO-101 向导产出的标定，不必手抄 6 个电机 × 5 个字段</span>}
            {leaderCalibs && (
              <div className="mt-1 space-y-1">
                <p className="break-all text-gray-500">读取目录：{leaderCalibs.root}{leaderCalibs.exists ? "" : "（不存在）"}</p>
                {(leaderCalibs.entries ?? []).length === 0 && (
                  <p className="text-amber-300">这台主机上没找到已保存的主臂标定：要么还没用现有向导标定过，要么服务进程的 HF_LEROBOT_CALIBRATION 不指向你标定时的目录。两种情况都要先解决，不要凭记忆手填数值。</p>
                )}
                {(leaderCalibs.entries ?? []).map((entry: any) => (
                  <div key={entry.path} className="flex flex-wrap items-center gap-2">
                    <button onClick={() => useLeaderCalibration(entry)} disabled={!entry.usable || !!busy}
                            className="rounded border border-sky-800 px-2 py-0.5 text-sky-200 disabled:opacity-40">
                      填入
                    </button>
                    <span className="text-gray-300">{entry.robot_type}/{entry.name}</span>
                    <span className="break-all text-gray-500">sha256 {entry.sha256} · id {Object.values(entry.motors ?? {}).join(",")}</span>
                    {!entry.usable && (
                      <span className="text-red-300">不可用：{entry.reason
                        ?? `缺 ${JSON.stringify(entry.missing_motors)} / 字段不全 ${JSON.stringify(entry.incomplete_motors)}`}</span>
                    )}
                  </div>
                ))}
                <p className="text-gray-500">只读：本页面从不写回 LeRobot 的标定树；填入后仍需你核对"这台就是现在接上的主臂"再保存。</p>
              </div>
            )}
          </div>
        )}
        {readiness && (
          <div className={`mt-2 rounded border p-2 text-xs ${readiness.ready ? "border-emerald-800 bg-emerald-950/30 text-emerald-200" : "border-amber-800 bg-amber-950/30 text-amber-200"}`}>
            就绪：{readiness.ready ? "是" : "否"} · 标定版本 {readiness.calibration_version ?? "—"}
            <ul className="mt-1 list-disc pl-4">
              {(readiness.reasons ?? ["三层已匹配设备身份与关节顺序"]).map((r: string) => <li key={r}>{r}</li>)}
            </ul>
          </div>
        )}
      </section>

      <section className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-gray-200">遥操作会话</h2>
          <div className="flex flex-wrap gap-1 text-[11px]">
            <span className={`rounded border px-2 py-0.5 ${tone(state) === "ok" ? "border-emerald-700 bg-emerald-900/50 text-emerald-300" : tone(state) === "bad" ? "border-red-700 bg-red-900/50 text-red-300" : tone(state) === "warn" ? "border-amber-700 bg-amber-900/40 text-amber-300" : "border-gray-700 bg-gray-900 text-gray-400"}`}>状态 {state}</span>
            <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 text-gray-300">离合 {snapshot.clutch_engaged ? "接合" : "断开"}</span>
            <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 text-gray-300">已对齐 {snapshot.aligned ? "是" : "否"}</span>
            <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 text-gray-300">标定 {teleop?.calibration_version ?? "—"}</span>
            <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 text-gray-300">
              租约 上位机 {teleop?.upper_lease_active ? "持有" : "无"} / 车端 {teleop?.car_token_active ? "持有" : "无"}
            </span>
            <span className={`rounded border px-2 py-0.5 ${feedback.available ? "border-emerald-700 text-emerald-300" : "border-red-700 text-red-300"}`}>
              反馈 {feedback.available ? age(feedback.age_s) : `缺失（${feedback.reason ?? "未订阅"}）`}
            </span>
            <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 text-gray-300">序号 {teleop?.seq ?? -1}</span>
            <span title={teleop?.car_hold_policy ?? "车端未上报保持策略"}
                  className={`rounded border px-2 py-0.5 ${preview?.arm_stream?.live ? "border-emerald-700 text-emerald-300" : preview?.arm_stream?.expected ? "border-amber-700 text-amber-300" : "border-gray-700 text-gray-400"}`}>
              车端手臂流 {preview?.arm_stream
                ? (preview.arm_stream.live
                    ? `在时效内（剩 ${preview.arm_stream.remaining_s}s / TTL ${preview.arm_stream.ttl_s}s）`
                    : preview.arm_stream.expected
                      ? `已超龄（车端已不再收到目标，仅保持最后指令）`
                      : "尚未收到任何目标")
                : "未知（车端未上报）"}
            </span>
            <span className="rounded border border-gray-700 bg-gray-900 px-2 py-0.5 text-gray-300">
              底盘{teleop?.chassis_locked === null || teleop?.chassis_locked === undefined
                ? " 未知（尚未取得车端状态）"
                : ` ${teleop.chassis_locked ? "锁定" : "未锁定"}${teleop?.gate_age_s != null ? ` · 观测 ${teleop.gate_age_s}s 前` : ""}`}
            </span>
            <span className={`rounded border px-2 py-0.5 ${!teleop?.car_exclusivity ? "border-gray-700 text-gray-400" : teleop.car_exclusivity.clean ? "border-emerald-700 text-emerald-300" : "border-red-700 text-red-300"}`}>
              车端独占{!teleop?.car_exclusivity
                ? " 未检测（车端尚未尝试使能）"
                : teleop.car_exclusivity.clean
                  ? " 可用"
                  : ` 不可用（${teleop.car_exclusivity.reason === "vendor_publisher_present"
                      ? Object.values<string[]>(teleop.car_exclusivity.foreign).flat().join("、")
                      : "无法枚举发布者"}）`}
            </span>
          </div>
        </div>

        <div className="flex flex-wrap gap-2 text-sm">
          <button disabled={!!busy || teleop?.car_token_active}
                  onClick={async () => {
                    const out = await post("/ros-mobile/teleop/acquire", {
                      owner: "browser-operator", port,
                      leader_identity: identities.leader, follower_identity: identities.follower,
                      mapping_identity: identities.mapping,
                    }, "获取遥操作会话");
                    if (out) await refresh();
                  }}
                  className="rounded bg-emerald-600 px-3 py-1.5 font-medium disabled:opacity-40">取得双层租约</button>
          <button disabled={!teleop?.car_token_active || !!busy}
                  onClick={async () => { await post("/ros-mobile/teleop/release", { reason: "browser" }, "释放遥操作会话"); await refresh(); }}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">停止会话（先停控制再收尾录包）</button>
          <button disabled={!["calibrated", "paused"].includes(state) || !!busy} onClick={() => sendState("enter_preview", "进入无输出预览")}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">无输出预览</button>
          <button disabled={!["preview", "aligned"].includes(state) || !!busy} onClick={() => sendState("align", "记录对齐参考")}
                  className="rounded border border-sky-700 px-3 py-1.5 text-sky-200 disabled:opacity-40">记录主/从参考</button>
          <button disabled={state !== "aligned" || !!busy} onClick={() => sendState("arm", "待命")}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">待命</button>
          <button disabled={state !== "armed" || !!busy} onClick={() => sendState("engage", "接合跟随")}
                  className="rounded bg-sky-600 px-3 py-1.5 font-medium disabled:opacity-40">接合跟随</button>
          <button disabled={state !== "following" || !!busy} onClick={() => sendState("release", "释放离合")}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">释放离合（重新接合会重建参考）</button>
          <button disabled={!["following", "armed"].includes(state) || !!busy} onClick={() => sendState("pause", "暂停跟随")}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">暂停跟随</button>
          <button disabled={state === "estop" || state === "disconnected" || !!busy}
                  onClick={() => sendState("estop", "急停", { reason: "browser-estop" })}
                  className="ml-auto rounded bg-red-600 px-3 py-1.5 font-semibold disabled:opacity-40">急停</button>
          <button disabled={state !== "estop" || !!busy} onClick={() => sendState("estop_cleared", "恢复急停")}
                  className="rounded border border-amber-700 px-3 py-1.5 text-amber-200 disabled:opacity-40">急停恢复（需重新对齐）</button>
        </div>

        {snapshot.deny_reasons?.length > 0 && (
          <div className="mt-2 rounded border border-amber-800/60 bg-amber-950/30 p-2 text-xs text-amber-200">
            被拒绝：{snapshot.deny_reasons.join(", ")}
          </div>
        )}
        {(preview?.gate_missing ?? teleop?.readiness?.missing_for_following ?? teleop?.missing_for_following)?.length > 0 && (
          <div className="mt-2 text-xs text-gray-400">
            跟随前置缺失：{String((preview?.gate_missing ?? teleop?.readiness?.missing_for_following
              ?? teleop?.missing_for_following)).split(",").join(" · ")}
          </div>
        )}
        {teleop?.last_error && <div className="mt-2 text-xs text-red-300">最近错误：{teleop.last_error}</div>}
        <div className="mt-1 text-[11px] text-gray-500">
          保持策略：暂停=停止下发新目标并保持舵机通电位置保持，绝不整臂断力矩；急停=停止下发并由车端 watchdog 取消活动轨迹目标。
        </div>
      </section>

      <section className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-200">无输出映射预览</h2>
          <span className={`text-xs ${preview?.outputs_actuators === false ? "text-emerald-300" : "text-red-300"}`}>
            {preview?.outputs_actuators === false ? "本表不产生执行器输出" : "尚未取得预览结果"}
          </span>
        </div>
        {preview?.error && <div className="mb-2 text-xs text-amber-300">预览中断：{preview.error}</div>}
        <div className="mb-2 flex flex-wrap gap-2 text-[11px] text-gray-400">
          <span className="text-gray-500">主臂实测读数 (deg)：</span>
          {Object.entries(preview?.leader_deg ?? {}).map(([name, value]: [string, any]) => (
            <span key={name} className="rounded border border-gray-800 bg-gray-900 px-1.5 py-0.5 font-mono">{name} {num(value, 1)}</span>
          ))}
          {preview?.gripper_percent != null && (
            <span className="rounded border border-gray-800 bg-gray-900 px-1.5 py-0.5 font-mono">夹爪 {num(preview.gripper_percent, 1)} %</span>
          )}
          {!preview?.leader_deg && <span className="text-amber-300">主臂读数缺失：不显示估计值</span>}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-gray-500">
              <tr>
                <th className="py-1 pr-2 font-normal">从臂关节</th>
                <th className="py-1 pr-2 font-normal">主臂相对变化 (rad)</th>
                <th className="py-1 pr-2 font-normal">方向/比例</th>
                <th className="py-1 pr-2 font-normal">映射前目标</th>
                <th className="py-1 pr-2 font-normal">限幅后目标</th>
                <th className="py-1 pr-2 font-normal">已发送目标</th>
                <th className="py-1 pr-2 font-normal">从臂实测反馈</th>
                <th className="py-1 pr-2 font-normal">跟随误差</th>
                <th className="py-1 font-normal">限幅</th>
              </tr>
            </thead>
            <tbody className="font-mono text-gray-200">
              {joints.length === 0 && (
                <tr><td colSpan={9} className="py-2 text-gray-500">缺少主臂读数或未加载映射，宁可显示缺失也不补零</td></tr>
              )}
              {joints.map((name) => {
                const target = preview?.targets?.[name] ?? {};
                const sent = preview?.sent_target_rad;
                const sentIndex = joints.indexOf(name);
                return (
                  <tr key={name} className="border-t border-gray-800/60">
                    <td className="py-1 pr-2 font-sans text-gray-400">{name}</td>
                    <td className="py-1 pr-2">{num(target.leader_delta_rad)}</td>
                    <td className="py-1 pr-2">{target.direction == null ? "—" : `${target.direction > 0 ? "+" : "-"}×${num(target.scale, 2)}${target.wrapped ? " ↺" : ""}`}</td>
                    <td className="py-1 pr-2">{num(target.pre_clip_rad)}</td>
                    <td className="py-1 pr-2">{num(target.target_rad)}</td>
                    <td className="py-1 pr-2">{Array.isArray(sent) ? num(sent[sentIndex]) : "未发送"}</td>
                    <td className="py-1 pr-2">{num(feedback?.positions_rad?.[name])}</td>
                    <td className="py-1 pr-2">{num(preview?.follow_error_rad?.[name], 4)}</td>
                    <td className="py-1">{(preview?.clipped_joints ?? []).includes(name) ? "是" : "否"}</td>
                  </tr>
                );
              })}
              {preview?.gripper && (
                <tr className="border-t border-gray-800/60 text-gray-100">
                  <td className="py-1 pr-2 font-sans text-gray-400">{preview.gripper.joint}（夹爪）</td>
                  <td className="py-1 pr-2">{num(preview.gripper.leader_percent, 1)} %（非角度）</td>
                  <td className="py-1 pr-2">{preview.gripper.flipped ? "翻转关节" : "正向"}</td>
                  <td className="py-1 pr-2">{num(preview.gripper.pre_clip_pulse, 1)} 脉冲</td>
                  <td className="py-1 pr-2">{num(preview.gripper.target_pulse, 1)} 脉冲 · {num(preview.gripper.target_rad)}</td>
                  <td className="py-1 pr-2">{num(preview.gripper.target_rad)}</td>
                  <td className="py-1 pr-2">{num(feedback?.positions_rad?.[preview.gripper.joint])}</td>
                  <td className="py-1 pr-2">{num(preview?.follow_error_rad?.[preview.gripper.joint], 4)}</td>
                  <td className="py-1">{preview.gripper.clipped ? "是" : "否"} · 宽 {num(preview.gripper.jaw_width_mm_estimate, 1)}mm</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {preview?.unmapped_joints?.length > 0 && (
          <div className="mt-2 text-xs text-amber-300">无法映射：{preview.unmapped_joints.join(", ")}</div>
        )}
      </section>

      <section className="rounded-lg border border-gray-800 bg-gray-950/60 p-3">
        <h2 className="mb-2 text-sm font-semibold text-gray-200">录制（同一会话子任务，不再申请第二个租约）</h2>
        <input value={task} onChange={(e) => setTask(e.target.value)}
               className="mb-2 w-full rounded border border-gray-700 bg-gray-900 p-1.5 text-xs text-gray-100" placeholder="任务名" />
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
          <label className="flex items-center gap-1.5 text-gray-400">本集人工标注
            <select value={episodeLabel} onChange={(e) => setEpisodeLabel(e.target.value)}
                    className="rounded border border-gray-700 bg-gray-900 p-1 text-gray-100">
              <option value="unknown">unknown（默认）</option>
              <option value="success">成功</option>
              <option value="failure">失败</option>
            </select>
          </label>
          <input value={episodeNote} onChange={(e) => setEpisodeNote(e.target.value)}
                 className="min-w-[12rem] flex-1 rounded border border-gray-700 bg-gray-900 p-1.5 text-gray-100"
                 placeholder="备注（可选，写入 manifest 的 evaluation.note）" />
        </div>
        <div className="flex flex-wrap gap-2 text-sm">
          <button disabled={!teleop?.car_token_active || !!busy || teleop?.recording_session}
                  onClick={async () => { await post("/ros-mobile/teleop/recording", { action: "start_session", task }, "开始录制会话"); await refresh(); }}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">开始会话</button>
          <button disabled={!teleop?.recording_session || !!busy || teleop?.recording_episode}
                  onClick={async () => { await post("/ros-mobile/teleop/recording", { action: "start_episode", task }, "开始 episode"); await refresh(); }}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">开始本集</button>
          <button disabled={!teleop?.recording_episode || !!busy}
                  onClick={async () => {
                    // 结束本集默认先停止下发再收尾录包，急停不等保存
                    if (state === "following") await post("/ros-mobile/teleop/state", { event: "pause" }, "结束前暂停跟随");
                    const out = await post("/ros-mobile/teleop/recording", {
                      action: "end_episode",
                      // unknown 是默认：真机没有仿真真值，不替操作员判定成功
                      success: episodeLabel === "unknown" ? null : episodeLabel === "success",
                      note: episodeNote,
                    }, "结束 episode");
                    if (out) { setEpisodeLabel("unknown"); setEpisodeNote(""); }
                    await refresh();
                  }}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">结束本集（默认暂停跟随）</button>
          <button disabled={!teleop?.recording_session || !!busy}
                  onClick={async () => { await post("/ros-mobile/teleop/recording", { action: "stop_session" }, "停止录制会话"); await refresh(); }}
                  className="rounded border border-gray-700 px-3 py-1.5 disabled:opacity-40">停止录制</button>
          <a href="#ros-mobile-sync-export" className="rounded border border-gray-700 px-3 py-1.5">前往同步与导出</a>
        </div>
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-gray-400">
          <span>会话 {teleop?.recording_session ?? "—"}</span>
          <span>episode {teleop?.recording_episode ?? "—"}</span>
          <span>单位：主臂 deg→rad，夹爪 0–100 %；从臂 rad（脉冲经换算）</span>
          <span>成功标注在“结束本集”时人工填写，未填即 unknown；真机不套用 Gazebo 真值判定</span>
        </div>
        <div className="mt-1 flex items-center gap-2 text-[11px] text-gray-500">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
          <span>原始数据记录：主臂读数、映射前目标、限幅后发送目标、网关接受/拒绝、可取得的控制器回执、从臂实测反馈、离合/故障/标定版本/相机/episode 事件。</span>
        </div>
      </section>
    </div>
  );
}
