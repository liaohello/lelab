import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApi } from "@/contexts/ApiContext";
import { Button } from "@/components/ui/button";

/**
 * Read-only single- or bimanual-follower monitor.  There is deliberately no
 * action control on this page; missing calibration is surfaced as raw values.
 */

type ObserveMode = "observe" | "bimanual";

interface ArmBinding {
  port: string | null;
  calibration: string | null;
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
  bindings?: Record<string, ArmBinding>;
  camera_ages_ms: Record<string, number>;
  torque: Record<string, number>;
  actions_sent: number;
  mode: string;
  port?: string;
}

interface InventoryPort {
  id: string;
  by_id: string | null;
  present: boolean;
  role: string;
  serial: string | null;
}

interface RobotRecordResponse {
  robot: {
    configuration?: {
      checks?: {
        follower_calibration?: { exists?: boolean; path?: string };
      };
    };
  } | null;
}

const DEFAULT_RIGHT_PORT =
  "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B91045575-if00";
const DEFAULT_CALIBRATION =
  "/home/liaozhengyan/.local/share/lelab-jetson-20261031/calibration/my_robot.json";
const DEFAULT_CONTRACT =
  "/home/liaozhengyan/.local/share/lelab-jetson-20261031/checkpoints";

const emptyStatus: ObserveStatus = {
  active: false,
  phase: "idle",
  fault: null,
  frames: 0,
  state: {},
  camera_ages_ms: {},
  torque: {},
  actions_sent: 0,
  mode: "observe",
};

const parseCameras = (text: string): Record<string, Record<string, number>> => {
  const cameras: Record<string, Record<string, number>> = {};
  for (const item of text.split(",")) {
    const [name, rawIndex] = item.split("=").map((part) => part.trim());
    if (!name || !rawIndex) continue;
    cameras[name] = {
      index: Number(rawIndex),
      width: 640,
      height: 480,
      fps: 30,
    };
  }
  return cameras;
};

const splitJoints = (
  joints: Record<string, number>
): Record<string, Record<string, number>> => {
  const result: Record<string, Record<string, number>> = { left: {}, right: {} };
  for (const [name, value] of Object.entries(joints)) {
    if (name.startsWith("left_")) result.left[name] = value;
    if (name.startsWith("right_")) result.right[name] = value;
  }
  return result;
};

const Observe: React.FC = () => {
  const { baseUrl, wsBaseUrl, fetchWithHeaders } = useApi();
  const [mode, setMode] = useState<ObserveMode>("bimanual");
  const [leftPort, setLeftPort] = useState("");
  const [leftCalibration, setLeftCalibration] = useState("");
  const [rightPort, setRightPort] = useState(DEFAULT_RIGHT_PORT);
  const [rightCalibration, setRightCalibration] = useState(DEFAULT_CALIBRATION);
  const [port, setPort] = useState(DEFAULT_RIGHT_PORT);
  const [calibration, setCalibration] = useState(DEFAULT_CALIBRATION);
  const [contractDir, setContractDir] = useState(DEFAULT_CONTRACT);
  const [cameraText, setCameraText] = useState("right=0,up=2");
  const [fps, setFps] = useState(5);
  const [status, setStatus] = useState<ObserveStatus>(emptyStatus);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [filling, setFilling] = useState(false);
  const [tick, setTick] = useState(0);
  const [wsConnected, setWsConnected] = useState(false);
  const [wsCount, setWsCount] = useState(0);
  const [wsJoints, setWsJoints] = useState<Record<string, number>>({});
  const websocketRef = useRef<WebSocket | null>(null);
  const cameraKeys = Object.keys(parseCameras(cameraText));
  const isBimanual = mode === "bimanual";

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
    const timer = window.setInterval(() => {
      void refresh();
    }, 500);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const ws = new WebSocket(`${wsBaseUrl}/ws/joint-data`);
    websocketRef.current = ws;
    ws.onopen = () => setWsConnected(true);
    ws.onclose = () => setWsConnected(false);
    ws.onerror = () => setWsConnected(false);
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "joint_update") {
          setWsCount((count) => count + 1);
          setWsJoints(data.joints ?? {});
        }
      } catch {
        // Ignore unrelated broadcast messages.
      }
    };
    return () => {
      ws.close(1000);
      websocketRef.current = null;
      setWsConnected(false);
    };
  }, [wsBaseUrl]);

  const fillFromSavedRoles = useCallback(async () => {
    setFilling(true);
    setError(null);
    setNotice(null);
    try {
      const [inventoryResponse, leftRobotResponse, rightRobotResponse] =
        await Promise.all([
          fetchWithHeaders(`${baseUrl}/hardware/inventory`),
          fetchWithHeaders(`${baseUrl}/robots/left_rig`),
          fetchWithHeaders(`${baseUrl}/robots/right_rig`),
        ]);
      if (!inventoryResponse.ok) throw new Error("未找到已保存的硬件角色");
      const inventory = (await inventoryResponse.json()) as { ports?: InventoryPort[] };
      const ports = inventory.ports ?? [];
      const findPort = (role: string) =>
        ports.find((item) => item.present && item.role === role)?.by_id;
      const left = findPort("left_follower");
      const right = findPort("right_follower");
      if (!left || !right) throw new Error("硬件角色中缺少已连接的左/右从臂");

      const calibratedPath = async (response: Response) => {
        if (!response.ok) return "";
        const payload = (await response.json().catch(() => ({}))) as RobotRecordResponse;
        const check = payload.robot?.configuration?.checks?.follower_calibration;
        return check?.exists ? check.path ?? "" : "";
      };
      const [leftCalibrationPath, rightCalibrationPath] = await Promise.all([
        calibratedPath(leftRobotResponse),
        calibratedPath(rightRobotResponse),
      ]);

      setLeftPort(left);
      setRightPort(right);
      setLeftCalibration(leftCalibrationPath);
      setRightCalibration(rightCalibrationPath);
      setPort(right);
      setCalibration(rightCalibrationPath || DEFAULT_CALIBRATION);
      setNotice(
        leftCalibrationPath
          ? "已从硬件角色填充两台已标定 follower。"
          : "已填充端口；左从臂尚无标定文件，数值将按原始位置显示。"
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setFilling(false);
    }
  }, [baseUrl, fetchWithHeaders]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const endpoint = isBimanual ? "/observe/bimanual/start" : "/observe/start";
      const body = isBimanual
        ? {
            left_port: leftPort,
            left_calibration: leftCalibration || null,
            right_port: rightPort,
            right_calibration: rightCalibration || null,
            cameras: parseCameras(cameraText),
            fps,
          }
        : {
            port,
            calibration,
            contract_dir: contractDir,
            cameras: parseCameras(cameraText),
            fps,
          };
      const response = await fetchWithHeaders(`${baseUrl}${endpoint}`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail ?? payload.message ?? "启动失败");
      }
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
      const response = await fetchWithHeaders(`${baseUrl}/observe/stop`, {
        method: "POST",
      });
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

  const joints = useMemo(
    () => (Object.keys(wsJoints).length ? wsJoints : status.state),
    [status.state, wsJoints]
  );
  const arms = useMemo(() => splitJoints(joints), [joints]);
  const ageEntries = Object.entries(status.camera_ages_ms);
  const torqueEntries = Object.entries(status.torque);
  const canStart =
    !busy &&
    !status.active &&
    (!isBimanual || Boolean(leftPort.trim() && rightPort.trim()));

  return (
    <div className="min-h-screen bg-black p-6 text-gray-100">
      <div className="mx-auto max-w-7xl space-y-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">只读观测</h1>
            <p className="mt-1 text-sm text-gray-400">
              只读取 follower 关节与相机；不创建策略客户端、不发送动作、不改变力矩。
            </p>
          </div>
          <div className="flex rounded-lg border border-gray-800 bg-gray-950 p-1">
            <Button
              size="sm"
              variant={isBimanual ? "default" : "ghost"}
              onClick={() => setMode("bimanual")}
              disabled={status.active}
            >
              双臂
            </Button>
            <Button
              size="sm"
              variant={isBimanual ? "ghost" : "default"}
              onClick={() => setMode("observe")}
              disabled={status.active}
            >
              单臂（已验证）
            </Button>
          </div>
        </div>

        <div className="grid gap-4 rounded-lg border border-gray-800 bg-gray-950 p-4 md:grid-cols-2">
          {isBimanual ? (
            <>
              <label className="text-sm">
                左从臂串口
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={leftPort}
                  onChange={(event) => setLeftPort(event.target.value)}
                  placeholder="/dev/serial/by-id/..."
                />
              </label>
              <label className="text-sm">
                右从臂串口
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={rightPort}
                  onChange={(event) => setRightPort(event.target.value)}
                />
              </label>
              <label className="text-sm">
                左从臂标定（可留空）
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={leftCalibration}
                  onChange={(event) => setLeftCalibration(event.target.value)}
                />
              </label>
              <label className="text-sm">
                右从臂标定（可留空）
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={rightCalibration}
                  onChange={(event) => setRightCalibration(event.target.value)}
                />
              </label>
            </>
          ) : (
            <>
              <label className="text-sm">
                串口
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={port}
                  onChange={(event) => setPort(event.target.value)}
                />
              </label>
              <label className="text-sm">
                标定 JSON
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={calibration}
                  onChange={(event) => setCalibration(event.target.value)}
                />
              </label>
              <label className="text-sm">
                契约目录
                <input
                  className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
                  value={contractDir}
                  onChange={(event) => setContractDir(event.target.value)}
                />
              </label>
            </>
          )}
          <label className="text-sm">
            相机（name=index）
            <input
              className="mt-1 w-full rounded border border-gray-700 bg-black px-2 py-1 font-mono text-xs"
              value={cameraText}
              onChange={(event) => setCameraText(event.target.value)}
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
            />
          </label>
          <div className="flex items-end gap-2">
            <Button onClick={start} disabled={!canStart}>
              {isBimanual ? "开始双臂观测" : "开始观测"}
            </Button>
            <Button variant="secondary" onClick={stop} disabled={busy || !status.active}>
              停止
            </Button>
            <Button
              variant="outline"
              onClick={() => void fillFromSavedRoles()}
              disabled={busy || status.active || filling}
            >
              {filling ? "填充中…" : "从硬件角色填充"}
            </Button>
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

        <div className="grid gap-4 md:grid-cols-3">
          <div className="rounded border border-gray-800 bg-gray-950 p-3">
            <div className="text-xs uppercase text-gray-500">phase / mode</div>
            <div className="mt-1 text-lg">
              {status.phase} · {status.mode}
            </div>
            {status.fault ? (
              <div className="mt-2 break-words text-sm text-red-300">{status.fault}</div>
            ) : null}
          </div>
          <div className="rounded border border-gray-800 bg-gray-950 p-3">
            <div className="text-xs uppercase text-gray-500">actions_sent</div>
            <div className="mt-1 text-lg text-emerald-300">{status.actions_sent}</div>
            <div className="text-xs text-gray-500">frames {status.frames}</div>
          </div>
          <div className="rounded border border-gray-800 bg-gray-950 p-3">
            <div className="text-xs uppercase text-gray-500">WebSocket</div>
            <div className="mt-1 text-lg">{wsConnected ? "connected" : "disconnected"}</div>
            <div className="text-xs text-gray-500">joint_update {wsCount}</div>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          {(isBimanual ? ["left", "right"] : [null]).map((side) => (
            <div key={side ?? "single"} className="rounded border border-gray-800 bg-gray-950 p-3">
              <div className="mb-2 text-sm font-medium">
                {side && status.bindings?.[side]?.unit === "raw_steps"
                  ? "关节（原始位置）"
                  : "关节（实时）"}
                {side ? ` · ${side}` : ""}
              </div>
              <table className="w-full text-sm">
                <tbody>
                  {Object.entries(
                    side
                      ? arms[side] ?? {}
                      : joints
                  ).map(([name, value]) => (
                    <tr key={name} className="border-b border-gray-900">
                      <td className="py-1 text-gray-400">{name}</td>
                      <td className="py-1 text-right font-mono">{Number(value).toFixed(3)}</td>
                    </tr>
                  ))}
                  {!Object.keys(
                    side ? arms[side] ?? {} : joints
                  ).length ? (
                    <tr>
                      <td className="py-2 text-gray-500">等待观测数据…</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
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
              <div className="text-gray-500">等待观测数据…</div>
            ) : null}
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          {cameraKeys.map((key) => (
            <div key={key} className="rounded border border-gray-800 bg-gray-950 p-3">
              <div className="mb-2 text-sm font-medium">camera {key}</div>
              <img
                alt={`camera ${key}`}
                className="w-full rounded bg-black"
                src={`${baseUrl}/observe/camera/${key}?t=${tick}`}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

export default Observe;
