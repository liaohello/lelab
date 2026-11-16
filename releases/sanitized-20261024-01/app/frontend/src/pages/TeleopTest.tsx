import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ShieldAlert } from "lucide-react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useApi } from "@/contexts/ApiContext";
import { Button } from "@/components/ui/button";
import UrdfViewer from "@/components/UrdfViewer";
import {
  fetchTeleopReadiness,
  TEST_STATE_CLASS,
  TEST_STATE_LABEL,
  type TeleopReadinessResponse,
  type TeleopTestSide,
} from "@/lib/teleopReadiness";

/**
 * One group at a time: connect its leader and follower, follow by hand, and let
 * the operator record the verdict.  The verdict is stored against the identity
 * fingerprint, so a re-calibrated or re-plugged arm loses the pass instead of
 * quietly inheriting it.
 */

type Side = TeleopTestSide;

interface JointTelemetry {
  raw: number | null;
  sent: number | null;
  measured: number | null;
  delta: number | null;
  clamp_count: number;
  max_clamp: number | null;
  max_delta: number | null;
}

interface TestSnapshot {
  side: Side;
  record: string;
  fingerprint: string;
  elapsed_seconds: number;
  steps: number;
  clamped_steps: number;
  safety: Record<string, number | null>;
  joints: Record<string, JointTelemetry>;
  last_error: string | null;
}

interface TeleopStatus {
  teleoperation_active: boolean;
  mode: string;
  test: TestSnapshot | null;
  available_controls: { stop_teleoperation: boolean };
}

const SIDE_LABEL: Record<Side, string> = { left: "左组（left_rig）", right: "右组（right_rig）" };

const formatSafety = (safety: Record<string, number | null> | undefined): string => {
  if (!safety) return "未知";
  const parts = Object.entries(safety).map(
    ([name, value]) => `${name}=${value === null ? "关闭" : value}`
  );
  return parts.length ? parts.join(" · ") : "未设置";
};

const TeleopTest: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const initialSide: Side = searchParams.get("side") === "right" ? "right" : "left";
  const { baseUrl, fetchWithHeaders } = useApi();
  const [side, setSide] = useState<Side>(initialSide);
  const [readiness, setReadiness] = useState<TeleopReadinessResponse | null>(null);
  const [status, setStatus] = useState<TeleopStatus | null>(null);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const stoppedRef = useRef(false);

  const loadReadiness = useCallback(async () => {
    const payload = await fetchTeleopReadiness(fetchWithHeaders, baseUrl);
    if (payload) setReadiness(payload);
  }, [baseUrl, fetchWithHeaders]);

  const loadStatus = useCallback(async () => {
    try {
      const response = await fetchWithHeaders(`${baseUrl}/teleoperation-status`);
      if (!response.ok) return;
      setStatus((await response.json()) as TeleopStatus);
    } catch {
      // Ignore a transient status failure.
    }
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    void loadReadiness();
    void loadStatus();
  }, [loadReadiness, loadStatus]);

  useEffect(() => {
    const timer = window.setInterval(() => void loadStatus(), 500);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  const stopTest = useCallback(async () => {
    try {
      await fetchWithHeaders(`${baseUrl}/stop-teleoperation`, { method: "POST" });
    } catch {
      // best-effort: the worker also releases the bus on its own exit
    } finally {
      void loadStatus();
      void loadReadiness();
    }
  }, [baseUrl, fetchWithHeaders, loadReadiness, loadStatus]);

  // Leaving the page must never leave the arms following: cleanup on unmount,
  // plus a keepalive stop for browser-level navigation.
  useEffect(() => {
    const handlePageHide = () => {
      fetch(`${baseUrl}/stop-teleoperation`, { method: "POST", keepalive: true }).catch(() => {});
    };
    window.addEventListener("pagehide", handlePageHide);
    return () => {
      window.removeEventListener("pagehide", handlePageHide);
      if (!stoppedRef.current) {
        stoppedRef.current = true;
        void stopTest();
      }
    };
  }, [baseUrl, stopTest]);

  const group = readiness?.sides[side] ?? null;
  const identity = group?.identity;
  const telemetry = status?.test && status.test.side === side ? status.test : null;
  const running = Boolean(status?.teleoperation_active);
  const jointEntries = useMemo(
    () => (telemetry ? Object.entries(telemetry.joints) : []),
    [telemetry]
  );

  const startTest = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/move-arm`, {
        method: "POST",
        body: JSON.stringify({ side }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.success === false) {
        throw new Error(payload.detail ?? payload.message ?? "启动测试失败");
      }
      stoppedRef.current = false;
      setNotice(`${SIDE_LABEL[side]} 测试中：缓慢扳动示教臂，观察下表与 3D 姿态。`);
      await loadStatus();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (passed: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/teleoperation/test/confirm`, {
        method: "POST",
        body: JSON.stringify({ side, passed, note: passed ? "operator confirmed" : "operator rejected" }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail ?? "保存测试结论失败");
      setNotice(
        passed
          ? `已记录 ${SIDE_LABEL[side]} 遥操作测试通过。`
          : `已记录 ${SIDE_LABEL[side]} 测试异常；请检修或重新标定后再测。`
      );
      await Promise.all([loadReadiness(), loadStatus()]);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const canStart = !running && !busy && armed && Boolean(identity?.record_complete);

  return (
    <div className="min-h-screen bg-black p-6 text-gray-100">
      <div className="mx-auto max-w-6xl space-y-5">
        <Button asChild variant="ghost" size="sm" className="px-2 text-gray-400 hover:text-white">
          <Link to="/">
            <ArrowLeft className="mr-1 h-4 w-4" />
            返回主界面
          </Link>
        </Button>

        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">单臂遥操作测试</h1>
            <p className="mt-1 text-sm text-gray-400">
              一次只连接一组示教臂与工作臂。这一步会真实下发动作，是采集前的实机验证；
              /observe 只读观测不能替代它。
            </p>
          </div>
          <div className="flex rounded-lg border border-gray-800 bg-gray-950 p-1">
            {(["left", "right"] as Side[]).map((item) => (
              <Button
                key={item}
                size="sm"
                variant={side === item ? "default" : "ghost"}
                onClick={() => setSide(item)}
                disabled={running}
              >
                {SIDE_LABEL[item]}
              </Button>
            ))}
          </div>
        </div>

        <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium">
              {SIDE_LABEL[side]} · {group?.record ?? "未找到记录"}
            </div>
            <div
              className={`text-sm ${
                group ? TEST_STATE_CLASS[group.test.state] : "text-gray-400"
              }`}
            >
              {group ? TEST_STATE_LABEL[group.test.state] : "读取中…"}
            </div>
          </div>
          {!group?.available ? (
            <div className="mt-2 rounded border border-amber-800 bg-amber-950 p-2 text-xs text-amber-200">
              {group?.reason ?? "正在读取设备记录…"}
              <Link className="ml-2 underline" to="/hardware">
                去硬件角色页
              </Link>
              <Link className="ml-2 underline" to="/calibration">
                去标定
              </Link>
            </div>
          ) : (
            <div className="mt-2 grid gap-2 text-xs text-gray-400 md:grid-cols-2">
              <div className="font-mono">
                示教臂：{identity?.leader_port}
                <div className="truncate">标定：{identity?.leader_calibration.path}</div>
              </div>
              <div className="font-mono">
                工作臂：{identity?.follower_port}
                <div className="truncate">标定：{identity?.follower_calibration.path}</div>
              </div>
              <div className="md:col-span-2">
                生效限幅：{formatSafety(identity?.safety)}
              </div>
              {group.test.entry ? (
                <div className="md:col-span-2 text-gray-500">
                  上次结论 {group.test.entry.recorded_at} · {group.test.entry.note} ·{" "}
                  {group.test.entry.summary
                    ? `${group.test.entry.summary.steps} 步 / 限幅 ${group.test.entry.summary.clamped_steps}`
                    : "无遥测摘要"}
                </div>
              ) : null}
              {group.test.reason ? (
                <div className="md:col-span-2 text-amber-300">{group.test.reason}</div>
              ) : null}
            </div>
          )}
        </div>

        <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
          <label className="flex items-start gap-2 text-sm text-gray-300">
            <input
              type="checkbox"
              className="mt-1"
              checked={armed}
              onChange={(event) => setArmed(event.target.checked)}
              disabled={running}
            />
            <span>
              我已确认工作臂周围没有障碍、夹爪未夹持物体，并且可以随时使用急停。
            </span>
          </label>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button onClick={startTest} disabled={!canStart}>
              开始测试
            </Button>
            <Button variant="secondary" onClick={() => void stopTest()} disabled={!running}>
              停止测试
            </Button>
            <Button variant="outline" onClick={() => navigate("/bimanual")} disabled={running}>
              返回采集准备
            </Button>
            {running ? (
              <span className="text-xs text-amber-300">
                会话中：{SIDE_LABEL[side]} 正在跟随，租约到期或离开页面会自动断开。
              </span>
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

        <div className="grid gap-4 lg:grid-cols-2">
          <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
            <div className="flex items-center justify-between text-sm font-medium">
              <span>工作臂实时姿态</span>
              <span className="text-xs text-gray-500">
                {telemetry ? `${telemetry.steps} 步 · 限幅步 ${telemetry.clamped_steps}` : "无遥测"}
              </span>
            </div>
            <div className="mt-2 h-72 rounded border border-gray-800 bg-black">
              {identity?.follower_calibration.sha256 && running ? (
                <UrdfViewer />
              ) : (
                <div className="flex h-full items-center justify-center px-3 text-center text-xs text-gray-500">
                  {running
                    ? "等待第一帧跟随数据…"
                    : "开始测试后显示工作臂姿态；本视图只在会话中驱动。"}
                </div>
              )}
            </div>
            {telemetry?.last_error ? (
              <div className="mt-2 flex items-start gap-2 rounded border border-red-800 bg-red-950 p-2 text-xs text-red-200">
                <ShieldAlert className="mt-0.5 h-4 w-4" />
                <span className="break-words">{telemetry.last_error}</span>
              </div>
            ) : null}
          </div>

          <div className="rounded-lg border border-gray-800 bg-gray-950 p-4">
            <div className="text-sm font-medium">示教目标 / 下发目标 / 实测（度）</div>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-gray-500">
                  <tr>
                    <th className="py-1 text-left">关节</th>
                    <th className="py-1 text-right">原始</th>
                    <th className="py-1 text-right">下发</th>
                    <th className="py-1 text-right">实测</th>
                    <th className="py-1 text-right">差值</th>
                    <th className="py-1 text-right">限幅</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {jointEntries.map(([name, value]) => (
                    <tr key={name} className="border-t border-gray-900">
                      <td className="py-1 text-gray-400">{name.replace(".pos", "")}</td>
                      <td className="py-1 text-right">{value.raw ?? "—"}</td>
                      <td className="py-1 text-right">{value.sent ?? "—"}</td>
                      <td className="py-1 text-right">{value.measured ?? "—"}</td>
                      <td
                        className={`py-1 text-right ${
                          (value.max_delta ?? 0) > 10 ? "text-amber-300" : ""
                        }`}
                      >
                        {value.delta ?? "—"}
                      </td>
                      <td
                        className={`py-1 text-right ${
                          value.clamp_count ? "text-amber-300" : "text-gray-500"
                        }`}
                      >
                        {value.clamp_count} / {value.max_clamp ?? 0}
                      </td>
                    </tr>
                  ))}
                  {!jointEntries.length ? (
                    <tr>
                      <td colSpan={6} className="py-3 text-center text-gray-500">
                        开始测试后逐关节显示三个值；“限幅”列是 次数 / 最大幅度。
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button onClick={() => confirm(true)} disabled={busy || !telemetry}>
                标记通过
              </Button>
              <Button variant="outline" onClick={() => confirm(false)} disabled={busy || !telemetry}>
                标记异常
              </Button>
            </div>
            <p className="mt-2 text-xs text-gray-500">
              结论写入 {readiness?.result_path ?? "…"}，并把端口、标定内容摘要和限幅一起记入指纹；
              之后任何一项变化都会把结论标成“已过期”。
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};

export default TeleopTest;
