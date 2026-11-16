import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, RotateCw } from "lucide-react";
import { Link } from "react-router-dom";
import { useApi } from "@/contexts/ApiContext";
import { Button } from "@/components/ui/button";
import UrdfViewer from "@/components/UrdfViewer";
import {
  fetchRigBindings,
  targetsBySide,
  type FollowerTarget,
  type RigSide,
} from "@/lib/rigBindings";

/**
 * Read-only follower validation.  The device comes from the saved serial-role
 * bindings, and the four evidence states — port connected, calibration file,
 * live data, hardware test — are reported separately so none can imply
 * another.  This page never writes motors, so it can never pass a hardware test.
 */

type ObserveMode = RigSide | "bimanual";

const SIDE_LABEL: Record<RigSide, string> = { left: "左侧设备组", right: "右侧设备组" };
const MODES: { value: ObserveMode; label: string }[] = [
  { value: "left", label: "左组" },
  { value: "right", label: "右组" },
  { value: "bimanual", label: "双臂" },
];

interface FollowerSummary {
  port: string;
  port_connected: boolean;
  calibration: string | null;
  calibration_file_exists: boolean;
  calibrated: boolean;
  unit?: string;
}

interface ObserveStatus {
  active: boolean;
  phase: string;
  fault: string | null;
  frames: number;
  state: Record<string, number>;
  arms?: Record<string, Record<string, number>>;
  bindings?: Partial<Record<RigSide, FollowerSummary>>;
  camera_ages_ms: Record<string, number>;
  torque: Record<string, number>;
  actions_sent: number;
  mode: string;
  selected_side?: RigSide | null;
  started_mono?: number | null;
}

interface CameraPort {
  id: string;
  by_id: string | null;
  present: boolean;
  camera_key: string;
  index: number;
}

interface JointTrend {
  min: number;
  max: number;
  current: number;
}

const emptyStatus: ObserveStatus = {
  active: false,
  phase: "idle",
  fault: null,
  frames: 0,
  state: {},
  camera_ages_ms: {},
  torque: {},
  actions_sent: 0,
  mode: "idle",
};

const parseCameras = (text: string): Record<string, Record<string, number>> => {
  const cameras: Record<string, Record<string, number>> = {};
  for (const item of text.split(",")) {
    const [name, rawIndex] = item.split("=").map((part) => part.trim());
    if (!name || !rawIndex) continue;
    cameras[name] = { index: Number(rawIndex), width: 640, height: 480, fps: 30 };
  }
  return cameras;
};

const updateTrends = (
  previous: Record<string, JointTrend>,
  values: Record<string, number>
): Record<string, JointTrend> => {
  const next = { ...previous };
  for (const [key, raw] of Object.entries(values)) {
    const value = Number(raw);
    if (!Number.isFinite(value)) continue;
    const old = next[key];
    next[key] = old
      ? { min: Math.min(old.min, value), max: Math.max(old.max, value), current: value }
      : { min: value, max: value, current: value };
  }
  return next;
};

const Chip: React.FC<{ state: "ok" | "warn" | "bad" | "idle"; children: string }> = ({
  state,
  children,
}) => (
  <span
    className={`rounded px-2 py-1 text-xs font-mono ${
      state === "ok"
        ? "bg-emerald-950 text-emerald-300"
        : state === "warn"
          ? "bg-amber-950 text-amber-300"
          : state === "bad"
            ? "bg-red-950 text-red-300"
            : "bg-gray-900 text-gray-500"
    }`}
  >
    {children}
  </span>
);

const EvidenceRow: React.FC<{ label: string; children: React.ReactNode }> = ({
  label,
  children,
}) => (
  <div className="flex items-center justify-between gap-2 text-xs">
    <span className="text-gray-500">{label}</span>
    <span className="flex flex-wrap justify-end gap-1">{children}</span>
  </div>
);

const DevicePanel: React.FC<{
  side: RigSide;
  target: FollowerTarget | null;
  summary: FollowerSummary | undefined;
  dataLive: boolean;
  unitLabel: string;
  entries: [string, number][];
  trends: Record<string, JointTrend>;
  showUrdf: boolean;
  blockedReason: string;
}> = ({
  side,
  target,
  summary,
  dataLive,
  unitLabel,
  entries,
  trends,
  showUrdf,
  blockedReason,
}) => {
  const portConnected = summary ? summary.port_connected : null;
  const calibrationKnown = summary ? summary.calibration_file_exists : target?.calibrationExists;
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-950 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-medium text-gray-100">{SIDE_LABEL[side]}</div>
        <div className="font-mono text-xs text-gray-500">
          {target?.robotName ?? "未绑定记录"} · {unitLabel}
        </div>
      </div>

      <div className="mt-2 space-y-1">
        <EvidenceRow label="串口节点">
          {portConnected === null ? (
            <Chip state={target?.portDisconnected ? "warn" : "idle"}>
              {target?.portDisconnected ? "记录端口已断开" : "未开始观测"}
            </Chip>
          ) : (
            <Chip state={portConnected ? "ok" : "bad"}>
              {portConnected ? "设备已连接" : "设备节点缺失"}
            </Chip>
          )}
        </EvidenceRow>
        <EvidenceRow label="标定文件">
          <Chip
            state={calibrationKnown === undefined ? "idle" : calibrationKnown ? "ok" : "warn"}
          >
            {calibrationKnown === undefined
              ? "未知"
              : calibrationKnown
                ? "标定文件存在"
                : "标定文件缺失"}
          </Chip>
        </EvidenceRow>
        <EvidenceRow label="数据实时">
          <Chip
            state={
              !summary ? "idle" : dataLive ? "ok" : summary.port_connected ? "bad" : "warn"
            }
          >
            {!summary ? "未开始观测" : dataLive ? "数据实时" : "数据停更"}
          </Chip>
        </EvidenceRow>
        <EvidenceRow label="实机测试">
          <Chip state="idle">本页不判定</Chip>
        </EvidenceRow>
      </div>

      <div className="mt-2 space-y-1 font-mono text-[11px] text-gray-500">
        <div className="truncate">port: {summary?.port ?? target?.port ?? "—"}</div>
        <div className="truncate">
          calibration: {summary?.calibration ?? target?.calibrationPath ?? "—"}
        </div>
        {target && !target.recordExists ? (
          <div className="text-amber-300">机器人记录 {target.robotName} 尚不存在。</div>
        ) : null}
        {target?.issues.map((issue) => (
          <div key={issue} className="text-amber-300">
            {issue}
          </div>
        ))}
      </div>

      <div className="mt-3 h-56 rounded border border-gray-800 bg-black">
        {showUrdf ? (
          <UrdfViewer arm={side} />
        ) : (
          <div className="flex h-full items-center justify-center px-3 text-center text-xs text-gray-500">
            {blockedReason}
          </div>
        )}
      </div>

      <div className="mt-2 space-y-1">
        {entries.map(([name, value]) => {
          const trend = trends[name];
          const span = trend ? trend.max - trend.min : 0;
          const percent = span > 0 && trend ? ((value - trend.min) / span) * 100 : 50;
          return (
            <div key={name}>
              <div className="flex items-center justify-between font-mono text-xs text-gray-400">
                <span className="truncate">{name}</span>
                <span>{Number(value).toFixed(1)}</span>
              </div>
              <div className="mt-1 h-2 overflow-hidden rounded bg-gray-900">
                <div
                  className="h-full rounded bg-blue-400"
                  style={{ width: `${Math.max(0, Math.min(100, percent))}%` }}
                />
              </div>
            </div>
          );
        })}
        {!entries.length ? (
          <div className="py-2 text-sm text-gray-500">等待该设备组的观测数据…</div>
        ) : null}
      </div>
    </div>
  );
};

const Observe: React.FC = () => {
  const { baseUrl, wsBaseUrl, fetchWithHeaders } = useApi();
  const [mode, setMode] = useState<ObserveMode>("left");
  const [targets, setTargets] = useState<Record<RigSide, FollowerTarget | null>>({
    left: null,
    right: null,
  });
  const [bindingsError, setBindingsError] = useState<string | null>(null);
  const [loadingBindings, setLoadingBindings] = useState(false);
  const [portBySide, setPortBySide] = useState<Record<RigSide, string>>({
    left: "",
    right: "",
  });
  const [calibrationBySide, setCalibrationBySide] = useState<Record<RigSide, string>>({
    left: "",
    right: "",
  });
  const [cameraText, setCameraText] = useState("");
  const [fps, setFps] = useState(5);
  const [status, setStatus] = useState<ObserveStatus>(emptyStatus);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const [wsConnected, setWsConnected] = useState(false);
  const [wsCount, setWsCount] = useState(0);
  const [wsValues, setWsValues] = useState<Record<string, number>>({});
  const [trends, setTrends] = useState<Record<string, JointTrend>>({});
  const [lastUpdateAt, setLastUpdateAt] = useState<number | null>(null);
  const sessionRef = useRef<string>("idle");

  const isBimanual = mode === "bimanual";
  const sides: RigSide[] = isBimanual ? ["left", "right"] : [mode];

  const refillFromRecords = useCallback(
    async (notify: boolean) => {
      setLoadingBindings(true);
      setBindingsError(null);
      try {
        const payload = await fetchRigBindings(fetchWithHeaders, baseUrl);
        const next = targetsBySide(payload);
        setTargets(next);
        setPortBySide({
          left: next.left?.port ?? "",
          right: next.right?.port ?? "",
        });
        setCalibrationBySide({
          left: next.left?.calibrationPath ?? "",
          right: next.right?.calibrationPath ?? "",
        });
        if (notify) {
          const missing = (["left", "right"] as RigSide[]).filter(
            (side) => !next[side]?.port
          );
          setNotice(
            missing.length
              ? `已从保存记录填充；${missing.map((side) => SIDE_LABEL[side]).join("、")}还没有串口端口，请先在硬件角色页确认。`
              : "已从保存记录填充左右从臂端口与标定文件。"
          );
        }
      } catch (exc) {
        setBindingsError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setLoadingBindings(false);
      }
    },
    [baseUrl, fetchWithHeaders]
  );

  const loadCameraMapping = useCallback(async () => {
    try {
      const response = await fetchWithHeaders(`${baseUrl}/cameras/inventory`);
      if (!response.ok) return;
      const payload = (await response.json()) as { cameras?: CameraPort[] };
      const assigned = (payload.cameras ?? [])
        .filter((camera) => camera.present && camera.camera_key !== "unassigned")
        .sort((left, right) => (left.by_id || left.id).localeCompare(right.by_id || right.id))
        .map((camera) => `${camera.camera_key}=${camera.index}`);
      if (assigned.length) setCameraText(assigned.join(","));
    } catch {
      // Keep the manual field usable when the inventory is unavailable.
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    void refillFromRecords(false);
    void loadCameraMapping();
  }, [refillFromRecords, loadCameraMapping]);

  const refresh = useCallback(async () => {
    try {
      const response = await fetchWithHeaders(`${baseUrl}/observe/status`);
      if (!response.ok) return;
      const payload = (await response.json()) as ObserveStatus;
      setStatus(payload);
      setTick((value) => value + 1);
    } catch {
      // Keep the last status; the next poll retries.
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // Every read-only session gets a fresh trend window so a restarted or
  // switched observation cannot be shown with the previous device's history.
  const sessionKey = `${status.mode}:${status.selected_side ?? "-"}:${
    status.started_mono ?? "-"
  }`;
  useEffect(() => {
    if (sessionRef.current === sessionKey) return;
    sessionRef.current = sessionKey;
    setWsValues({});
    setTrends({});
    setWsCount(0);
    setLastUpdateAt(null);
  }, [sessionKey]);

  useEffect(() => {
    const ws = new WebSocket(`${wsBaseUrl}/ws/joint-data`);
    ws.onopen = () => setWsConnected(true);
    ws.onclose = () => setWsConnected(false);
    ws.onerror = () => setWsConnected(false);
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // Only observation traffic belongs on this page; teleop, recording and
        // inference broadcasts describe a different control state.
        if (data.type !== "joint_update" || data.source !== "observe_only") return;
        setWsCount((count) => count + 1);
        setLastUpdateAt(Date.now());
        const values =
          data.values && Object.keys(data.values).length ? data.values : data.joints ?? {};
        setWsValues(values);
        setTrends((current) => updateTrends(current, values));
      } catch {
        // Ignore unrelated broadcast messages.
      }
    };
    return () => {
      ws.close(1000);
      setWsConnected(false);
    };
  }, [wsBaseUrl]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const endpoint = isBimanual ? "/observe/bimanual/start" : "/observe/follower/start";
      const body = isBimanual
        ? {
            left_port: portBySide.left,
            left_calibration: calibrationBySide.left || null,
            right_port: portBySide.right,
            right_calibration: calibrationBySide.right || null,
            cameras: parseCameras(cameraText),
            fps,
          }
        : {
            side: mode,
            port: portBySide[mode],
            calibration: calibrationBySide[mode] || null,
            cameras: parseCameras(cameraText),
            fps,
          };
      const response = await fetchWithHeaders(`${baseUrl}${endpoint}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail ?? payload.message ?? "启动失败");
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/observe/stop`, { method: "POST" });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail ?? "停止失败");
      }
      await refresh();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const updateAgeMs = lastUpdateAt === null ? null : Date.now() - lastUpdateAt;
  const dataLive =
    status.active && status.frames > 0 && wsConnected && (updateAgeMs ?? Infinity) < 3000;

  const entriesFor = useCallback(
    (side: RigSide): [string, number][] => {
      if (!status.bindings?.[side]) return [];
      if (status.mode === "bimanual") {
        return Object.entries(status.arms?.[side] ?? {});
      }
      if (status.selected_side !== side) return [];
      return Object.entries(status.state);
    },
    [status.arms, status.bindings, status.mode, status.selected_side, status.state]
  );

  const blockedReason = (side: RigSide): string => {
    const summary = status.bindings?.[side];
    if (!summary) return "尚未开始观测：本卡片只反映已保存记录，连接与标定要在观测中才会实时验证。";
    if (!summary.port_connected) return "串口节点当前不存在，无法读取该设备组的关节数据。";
    if (!summary.calibrated) return "该设备组没有标定文件，encoder step 不能当作角度驱动 URDF。";
    return "等待第一条实时数据…";
  };

  const unitLabel = (side: RigSide): string => {
    const summary = status.bindings?.[side];
    if (summary) return summary.unit === "raw_steps" ? "未标定 · raw_steps" : "已标定 · degrees";
    return targets[side]?.calibrationExists ? "记录含标定" : "记录无标定";
  };

  const missingPort = sides.find((side) => !portBySide[side].trim());
  const samePort =
    isBimanual &&
    portBySide.left.trim() &&
    portBySide.left.trim() === portBySide.right.trim();
  const startBlock = missingPort
    ? `请先为${SIDE_LABEL[missingPort]}填写从臂串口。`
    : samePort
      ? "左右从臂必须使用不同串口。"
      : null;

  const ageEntries = Object.entries(status.camera_ages_ms);
  const torqueEntries = Object.entries(status.torque);
  const cameraKeys = Object.keys(parseCameras(cameraText));

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
            <h1 className="text-2xl font-semibold">只读设备校验（排障入口）</h1>
            <p className="mt-1 text-sm text-gray-400">
              只读取所选设备组的 follower 关节与相机；不发送动作、不改变力矩，也不作为标定或遥操作测试的通过依据。
            </p>
          </div>
          <div className="flex rounded-lg border border-gray-800 bg-gray-950 p-1">
            {MODES.map((item) => (
              <Button
                key={item.value}
                size="sm"
                variant={mode === item.value ? "default" : "ghost"}
                onClick={() => setMode(item.value)}
                disabled={status.active}
              >
                {item.label}
              </Button>
            ))}
          </div>
        </div>

        <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium">
              当前检查：{isBimanual ? "左 + 右两组" : SIDE_LABEL[mode]}
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void refillFromRecords(true)}
              disabled={status.active || loadingBindings}
            >
              <RotateCw className={`mr-1 h-4 w-4 ${loadingBindings ? "animate-spin" : ""}`} />
              从保存记录重新填充
            </Button>
          </div>
          {bindingsError ? (
            <div className="mt-2 rounded border border-red-800 bg-red-950 p-2 text-xs text-red-200">
              {bindingsError}
            </div>
          ) : (
            <div className="mt-2 text-xs text-gray-500">
              端口与标定来自 /hardware 保存的角色绑定；下方输入框可临时改成别的设备。
            </div>
          )}

          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {sides.map((side) => (
              <label key={side} className="text-sm">
                {SIDE_LABEL[side]}串口
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={portBySide[side]}
                  onChange={(event) =>
                    setPortBySide((current) => ({ ...current, [side]: event.target.value }))
                  }
                  placeholder="/dev/serial/by-id/..."
                  disabled={status.active}
                />
                <span className="mt-1 block">
                  标定文件（可留空按原始位置读取）
                  <input
                    className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                    value={calibrationBySide[side]}
                    onChange={(event) =>
                      setCalibrationBySide((current) => ({
                        ...current,
                        [side]: event.target.value,
                      }))
                    }
                    disabled={status.active}
                  />
                </span>
              </label>
            ))}
          </div>

          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <label className="text-sm">
              相机（数据字段=索引，可留空）
              <input
                className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                value={cameraText}
                onChange={(event) => setCameraText(event.target.value)}
                placeholder="left_wrist=0,right_wrist=2"
                disabled={status.active}
              />
            </label>
            <label className="text-sm">
              采样 Hz
              <input
                className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1"
                type="number"
                min={1}
                max={30}
                value={fps}
                onChange={(event) => setFps(Number(event.target.value))}
                disabled={status.active}
              />
            </label>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <Button onClick={start} disabled={busy || status.active || Boolean(startBlock)}>
              开始观测
            </Button>
            <Button variant="secondary" onClick={stop} disabled={busy || !status.active}>
              停止
            </Button>
            {startBlock ? (
              <span className="text-xs text-amber-300">{startBlock}</span>
            ) : null}
          </div>
        </div>

        {error ? (
          <div className="rounded border border-red-800 bg-red-950 p-3 text-sm text-red-200">
            {error}
          </div>
        ) : null}
        {notice ? (
          <div className="rounded border border-blue-800 bg-blue-950 p-3 text-sm text-blue-200">
            {notice}
          </div>
        ) : null}

        <div className="grid gap-4 md:grid-cols-4">
          <div className="rounded border border-gray-800 bg-gray-950 p-3">
            <div className="text-xs uppercase text-gray-500">phase</div>
            <div className="mt-1 text-lg">{status.phase}</div>
            <div className="text-xs text-gray-500">{status.mode}</div>
          </div>
          <div className="rounded border border-gray-800 bg-gray-950 p-3">
            <div className="text-xs uppercase text-gray-500">数据流</div>
            <div className={`mt-1 text-lg ${dataLive ? "text-emerald-300" : "text-gray-400"}`}>
              {lastUpdateAt === null ? "无" : `${Math.round(updateAgeMs ?? 0)} ms`}
            </div>
            <div className="text-xs text-gray-500">frames {status.frames}</div>
          </div>
          <div className="rounded border border-gray-800 bg-gray-950 p-3">
            <div className="text-xs uppercase text-gray-500">actions_sent</div>
            <div className="mt-1 text-lg text-emerald-300">{status.actions_sent}</div>
            <div className="text-xs text-gray-500">只读会话固定为 0</div>
          </div>
          <div className="rounded border border-gray-800 bg-gray-950 p-3">
            <div className="text-xs uppercase text-gray-500">WebSocket</div>
            <div className="mt-1 text-lg">{wsConnected ? "connected" : "disconnected"}</div>
            <div className="text-xs text-gray-500">observe 消息 {wsCount}</div>
          </div>
        </div>

        {status.fault ? (
          <div className="rounded border border-red-800 bg-red-950 p-3 text-sm break-words text-red-200">
            {status.fault}
          </div>
        ) : null}

        <div className="grid gap-4 lg:grid-cols-2">
          {sides.map((side) => (
            <DevicePanel
              key={side}
              side={side}
              target={targets[side]}
              summary={status.bindings?.[side]}
              dataLive={dataLive}
              unitLabel={unitLabel(side)}
              entries={entriesFor(side)}
              trends={trends}
              showUrdf={Boolean(status.bindings?.[side]?.calibrated) && dataLive}
              blockedReason={blockedReason(side)}
            />
          ))}
        </div>

        <div className="rounded border border-gray-800 bg-gray-950 p-3">
          <div className="mb-2 text-sm font-medium">相机帧年龄 / 力矩寄存器</div>
          <div className="space-y-1 font-mono text-xs text-gray-300">
            {ageEntries.map(([key, value]) => (
              <div key={key}>
                {key}: {Number(value).toFixed(1)} ms
              </div>
            ))}
            {torqueEntries.map(([key, value]) => (
              <div key={`t-${key}`} className="text-gray-500">
                torque {key}: {value}
              </div>
            ))}
            {!ageEntries.length && !torqueEntries.length ? (
              <div className="text-gray-500">未配置相机或尚未开始观测。</div>
            ) : null}
          </div>
        </div>

        {cameraKeys.length ? (
          <div className="grid gap-4 md:grid-cols-2">
            {cameraKeys.map((key) => {
              const age = status.camera_ages_ms[key];
              const stale = status.active && (age === undefined || age > 3000);
              return (
                <div key={key} className="rounded border border-gray-800 bg-gray-950 p-3">
                  <div className="mb-2 flex items-center justify-between text-sm font-medium">
                    <span className="font-mono">{key}</span>
                    <span
                      className={`font-mono text-xs ${
                        stale ? "text-red-300" : "text-gray-500"
                      }`}
                    >
                      {typeof age === "number" ? `${age.toFixed(1)} ms` : "无帧"}
                    </span>
                  </div>
                  {status.active ? (
                    <div className="relative">
                      <img
                        alt={`camera ${key}`}
                        className="w-full rounded bg-black"
                        src={`${baseUrl}/observe/camera/${key}?t=${tick}`}
                      />
                      {stale ? (
                        <div className="absolute inset-0 flex items-center justify-center bg-black/60 text-xs text-red-300">
                          画面停更，不能当作实时帧
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <div className="flex aspect-video items-center justify-center rounded border border-dashed border-gray-800 bg-black text-sm text-gray-500">
                      开始观测后显示该相机画面
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <div className="rounded border border-gray-800 bg-gray-950 p-3 text-sm text-gray-500">
            未填写相机映射；本页可以只验证关节数据。
          </div>
        )}

        <p className="text-xs text-gray-500">
          要确认可用于采集，请接着完成 /calibration 的逐台标定与 /teleoperation 的单臂遥操作测试；
          本页只证明“能读到”，不证明“能动、能动对”。
        </p>
      </div>
    </div>
  );
};

export default Observe;
