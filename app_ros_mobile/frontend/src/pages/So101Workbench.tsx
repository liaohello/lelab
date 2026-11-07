import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, CircleAlert, Eye, Play, Square, Wrench } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { apiRequest } from "@/lib/apiClient";
import { listLocalDatasetsForViewer } from "@/lib/datasetViewerApi";
import { useApi } from "@/contexts/ApiContext";
import { useRobots, type RobotRecord } from "@/hooks/useRobots";
import { useRealTimeJoints, type JointUpdate } from "@/hooks/useRealTimeJoints";
import { useToast } from "@/hooks/use-toast";
import JointTrackPanel, { JointSample } from "@/components/control/JointTrackPanel";
import TeleopCameraPanel from "@/components/control/TeleopCameraPanel";
import UrdfViewer from "@/components/UrdfViewer";

interface ArmBinding {
  role: string;
  config: string;
  content_sha256: string;
  ids: Record<string, number>;
  measured_utc: string;
}

interface PortRow {
  port: string;
  connected: boolean;
  device: string;
  serial: string;
  binding: ArmBinding | null;
}

interface CalibrationRow {
  config: string;
  readable: boolean;
  path: string;
  ids: Record<string, number>;
  content_sha256: string;
  bound_ports: string[];
}

interface DevicesResponse {
  status: string;
  bindings_file: string;
  ports: PortRow[];
  calibrations: Record<"leader" | "follower", CalibrationRow[]>;
  note: string;
  lease: { state: string; task: string | null; owner: string | null; expires_in: number | null };
  teleoperation: {
    teleoperation_active: boolean;
    stop_reason: string | null;
    clutch?: { engage_requested: boolean; engaged: boolean; aligned: boolean; refusal: string | null };
    session: {
      leader_port: string;
      follower_port: string;
      leader_config: string;
      follower_config: string;
      preview?: boolean;
    } | null;
  };
}

interface IdentityResponse {
  refusals: string[];
  warnings: string[];
  ready: boolean;
  ports: Record<string, ArmBinding | null>;
}

const STEPS = ["设备识别", "标定", "遥操作", "数据检查"];
const SHORT = (value: string, count = 8) => (value.length > count ? `${value.slice(0, count)}…` : value);

/**
 * Desktop bimanual workbench: pick which arm is the leader and which is the
 * follower from the stable /dev/serial/by-id paths, see what the host can prove
 * about each port, then start teleoperation and watch target against measurement.
 *
 * It reuses the existing desktop flow end to end — /move-arm, /stop-teleoperation,
 * the /ws/joint-data stream and /calibration — and adds only what those pages could
 * not show: both arms' readings in one place, and the arm-binding evidence.
 */
const So101Workbench: React.FC = () => {
  const navigate = useNavigate();
  const { baseUrl, fetchWithHeaders } = useApi();
  const { toast } = useToast();
  const { records, selectedName, selectedRecord, availableNames, selectRobot, isLoading: robotsLoading } =
    useRobots();

  const [step, setStep] = useState(0);
  const [leaderPort, setLeaderPort] = useState("");
  const [followerPort, setFollowerPort] = useState("");
  const [leaderConfig, setLeaderConfig] = useState("");
  const [followerConfig, setFollowerConfig] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(false);
  const [sessionPreview, setSessionPreview] = useState(false);
  const [clutch, setClutch] = useState<{ engaged: boolean; aligned: boolean; refusal: string | null } | null>(null);

  const devicesQuery = useQuery<DevicesResponse>({
    queryKey: ["so101-devices", baseUrl],
    queryFn: ({ signal }) =>
      apiRequest<DevicesResponse>(baseUrl, fetchWithHeaders, "/so101/devices", { signal, action: "读取设备清单" }),
    refetchInterval: 3000,
  });
  const devices = devicesQuery.data;

  const identityQuery = useQuery<IdentityResponse>({
    queryKey: ["so101-identity", baseUrl, leaderPort, leaderConfig, followerPort, followerConfig],
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({
        leader_port: leaderPort,
        leader_config: leaderConfig,
        follower_port: followerPort,
        follower_config: followerConfig,
      });
      return apiRequest<IdentityResponse>(baseUrl, fetchWithHeaders, `/so101/identity?${params}`, {
        signal,
        action: "核查标定归属",
      });
    },
    enabled: Boolean(leaderPort && followerPort && leaderConfig && followerConfig),
  });

  // A selected rig fills the pickers, so the page starts from what the operator
  // already configured on the landing page instead of asking again.
  useEffect(() => {
    if (!selectedRecord) return;
    setLeaderPort(selectedRecord.leader_port);
    setFollowerPort(selectedRecord.follower_port);
    setLeaderConfig(selectedRecord.leader_config);
    setFollowerConfig(selectedRecord.follower_config);
  }, [selectedRecord]);

  const bufferRef = useRef<JointSample[]>([]);
  const [samples, setSamples] = useState<JointSample[]>([]);
  const onFrame = useCallback((frame: JointUpdate) => {
    const buffer = bufferRef.current;
    buffer.push({ ...frame, receivedAt: Date.now() });
    // ~20 s at the 20 FPS broadcast; older frames leave the window anyway.
    if (buffer.length > 400) buffer.splice(0, buffer.length - 400);
  }, []);
  const { isConnected } = useRealTimeJoints({ onFrame });

  // The follow loop keeps running after this page is gone, holding both serial
  // ports, so leaving has to stop it — same net as /teleoperation: a keepalive POST
  // on a browser-level leave (which never runs React cleanup) and a normal stop on
  // unmount. Whether a session is live is taken from the server, not from local
  // state, because a reload erases the local flag.
  const sessionLiveRef = useRef(false);
  useEffect(() => {
    if (devices?.teleoperation?.teleoperation_active != null) {
      sessionLiveRef.current = devices.teleoperation.teleoperation_active;
      setSessionPreview(Boolean(devices.teleoperation.session?.preview));
      if (devices.teleoperation.clutch) {
        setClutch({
          engaged: devices.teleoperation.clutch.engaged,
          aligned: devices.teleoperation.clutch.aligned,
          refusal: devices.teleoperation.clutch.refusal,
        });
      }
    }
  }, [devices]);
  useEffect(() => {
    const stopIfLive = () => {
      if (!sessionLiveRef.current) return;
      sessionLiveRef.current = false;
      // A bare POST (no JSON Content-Type) stays a CORS "simple request", so a browser-level
      // leave isn't dropped to a preflight mid-unload; keepalive lets it survive the unload.
      fetch(`${baseUrl}/stop-teleoperation`, { method: "POST", keepalive: true }).catch(() => {});
    };
    window.addEventListener("pagehide", stopIfLive);
    return () => {
      window.removeEventListener("pagehide", stopIfLive);
      stopIfLive();
    };
  }, [baseUrl]);

  // Redraw the charts at 5 Hz; the socket keeps feeding the buffer at full rate, so
  // curve resolution does not depend on React render timing.
  useEffect(() => {
    const id = window.setInterval(() => setSamples([...bufferRef.current]), 200);
    return () => window.clearInterval(id);
  }, []);

  const stopBuffers = () => {
    bufferRef.current = [];
    setSamples([]);
  };

  const startTeleoperation = async (preview: boolean) => {
    setError(null);
    setBusy(preview ? "启动预览" : "启动遥操作");
    try {
      const result = await apiRequest<{ success: boolean; message: string; identity_warnings?: string[] }>(
        baseUrl,
        fetchWithHeaders,
        "/move-arm",
        {
          method: "POST",
          action: "启动遥操作",
          body: {
            leader_port: leaderPort,
            follower_port: followerPort,
            leader_config: leaderConfig,
            follower_config: followerConfig,
            preview,
            engage: !preview,
          },
        }
      );
      // /move-arm answers HTTP 200 with success=false for a logical refusal
      // (module busy, calibration not this arm's), so gate on the body.
      if (!result.success) {
        setError(result.message);
        toast({ title: "遥操作未启动", description: result.message, variant: "destructive" });
        return;
      }
      setActive(true);
      setSessionPreview(preview);
      setClutch({ engaged: false, aligned: false, refusal: null });
      sessionLiveRef.current = true;
      (result.identity_warnings ?? []).forEach((warning) =>
        toast({ title: "标定证据提示", description: warning })
      );
      toast({
        title: preview ? "预览已启动" : "遥操作已启动",
        description: preview
          ? `只映射不下发：主 ${SHORT(leaderPort, 14)} → 从 ${SHORT(followerPort, 14)}`
          : `主臂 ${SHORT(leaderPort, 14)} → 从臂 ${SHORT(followerPort, 14)}`,
      });
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
    } finally {
      setBusy(null);
    }
  };

  const setClutchEngage = async (engage: boolean) => {
    setBusy(engage ? "接合离合" : "断开离合");
    try {
      const result = await apiRequest<{
        success: boolean;
        message: string;
        engaged: boolean;
        aligned: boolean;
        refusal: string | null;
      }>(baseUrl, fetchWithHeaders, "/teleop-clutch", {
        method: "POST",
        action: engage ? "接合离合" : "断开离合",
        body: { engage },
      });
      setClutch({ engaged: result.engaged, aligned: result.aligned, refusal: result.refusal });
      if (!result.success && result.refusal) setError(`离合未接合：${result.refusal}`);
      toast({
        title: result.success ? (engage ? "已请求接合" : "已断开离合") : "离合操作未完成",
        description: result.message,
        variant: result.success ? "default" : "destructive",
      });
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
    } finally {
      setBusy(null);
    }
  };

  const stopTeleoperation = async () => {
    setBusy("停止遥操作");
    try {
      const result = await apiRequest<{ success: boolean; message: string }>(
        baseUrl,
        fetchWithHeaders,
        "/stop-teleoperation",
        { method: "POST", action: "停止遥操作", body: {} }
      );
      toast({
        title: result.success ? "遥操作已停止" : "停止未确认",
        description: result.message,
        variant: result.success ? "default" : "destructive",
      });
    } catch (exc: any) {
      setError(exc?.detail || exc?.message || String(exc));
    } finally {
      setActive(false);
      setSessionPreview(false);
      // The 3 s poll resyncs this from the server, so an optimistic false here is only an
      // attempt to avoid posting a second stop on unmount.
      sessionLiveRef.current = false;
      stopBuffers();
      setBusy(null);
      devicesQuery.refetch();
    }
  };

  const ports = devices?.ports ?? [];
  const chosen = useMemo(
    () => ({
      leader: ports.find((row) => row.port === leaderPort) ?? null,
      follower: ports.find((row) => row.port === followerPort) ?? null,
    }),
    [ports, leaderPort, followerPort]
  );
  const identity = identityQuery.data;
  const bothConnected = chosen.leader?.connected && chosen.follower?.connected;
  // The backend re-checks on start and refuses before opening a port; greying the
  // button out here just keeps the operator from aiming at a known-bad pair.
  const blockedByIdentity = identity ? !identity.ready : false;
  const canStart = Boolean(leaderPort && followerPort && leaderConfig && followerConfig) && !active && !blockedByIdentity;

  const configOptions = (role: "leader" | "follower") => devices?.calibrations?.[role] ?? [];

  return (
    <div className="min-h-screen bg-black text-slate-100">
      <header className="border-b border-gray-800 bg-gray-950/80 px-4 py-3">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => navigate("/")} className="text-slate-300">
            <ArrowLeft className="mr-1 h-4 w-4" /> 返回
          </Button>
          <div className="flex-1">
            <h1 className="text-lg font-semibold">SO-101 标定与遥操作工作台</h1>
            <p className="text-xs text-slate-400">
              上位机桌面双臂：指定主臂/从臂 → 核查标定归属 → 跟随 → 采集检查
            </p>
          </div>
          <span
            className={`rounded px-2 py-1 text-xs font-mono ${
              isConnected ? "bg-green-900/60 text-green-300" : "bg-red-900/60 text-red-300"
            }`}
          >
            {isConnected ? "关节流已连接" : "关节流未连接"}
          </span>
          <span
            className={`rounded px-2 py-1 text-xs font-mono ${
              active ? "bg-amber-900/60 text-amber-200" : "bg-gray-800 text-slate-400"
            }`}
          >
            {active ? "遥操作会话进行中" : "无遥操作会话"}
          </span>
        </div>
      </header>

      <nav className="mx-auto mt-4 flex max-w-7xl gap-2 px-4">
        {STEPS.map((name, index) => (
          <button
            key={name}
            onClick={() => setStep(index)}
            className={`flex-1 rounded border px-3 py-2 text-left text-sm transition-colors ${
              step === index
                ? "border-sky-600 bg-sky-950/60 text-sky-100"
                : "border-gray-800 bg-gray-900/60 text-slate-400 hover:border-gray-700"
            }`}
          >
            <span className="mr-2 font-mono text-xs text-slate-500">{index + 1}</span>
            {name}
          </button>
        ))}
      </nav>

      <main className="mx-auto mt-4 grid max-w-7xl gap-4 px-4 pb-10 lg:grid-cols-[1.1fr_1fr]">
        <div className="space-y-4">
          {step === 0 && (
            <Card className="border-slate-800 bg-slate-900/70">
              <CardHeader className="p-4 pb-2">
                <CardTitle className="text-base">设备识别</CardTitle>
                <CardDescription className="text-slate-400">
                  按 by-id 路径指定主/从臂。这里不输入字符串，只从系统列出的串口里选。
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3 p-4 pt-0">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-slate-400">配置</span>
                  <select
                    value={selectedName ?? ""}
                    onChange={(event) => selectRobot(event.target.value)}
                    disabled={robotsLoading || availableNames.length === 0 || active}
                    className="rounded border border-gray-700 bg-gray-900 px-2 py-1 text-slate-200"
                  >
                    <option value="">{availableNames.length ? "选择已保存的臂…" : "没有已保存的臂"}</option>
                    {availableNames.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                  {selectedRecord && (
                    <span
                      className={`font-mono text-xs ${
                        selectedRecord.is_clean ? "text-green-400" : "text-amber-400"
                      }`}
                    >
                      {selectedRecord.is_clean ? "配置完整" : "配置不完整"}
                    </span>
                  )}
                </div>

                {devicesQuery.isLoading ? (
                  <p className="text-sm text-slate-400">读取串口清单…</p>
                ) : ports.length === 0 ? (
                  <p className="rounded border border-amber-700/60 bg-amber-950/30 p-3 text-xs text-amber-200">
                    这台主机没有列出任何外部串口。主臂必须接在<b>上位机</b>的 USB 上——网络可达不等于串口可访问。
                  </p>
                ) : (
                  <div className="overflow-hidden rounded border border-slate-800">
                    <table className="w-full text-xs">
                      <thead className="bg-slate-950/70 text-slate-400">
                        <tr>
                          <th className="p-2 text-left">主臂</th>
                          <th className="p-2 text-left">从臂</th>
                          <th className="p-2 text-left">by-id 路径</th>
                          <th className="p-2 text-left">序列号 / 设备</th>
                          <th className="p-2 text-left">上次标定</th>
                        </tr>
                      </thead>
                      <tbody>
                        {ports.map((row) => (
                          <tr key={row.port} className="border-t border-slate-800">
                            <td className="p-2">
                              <input
                                type="radio"
                                name="leader"
                                checked={leaderPort === row.port}
                                onChange={() => setLeaderPort(row.port)}
                                disabled={active}
                              />
                            </td>
                            <td className="p-2">
                              <input
                                type="radio"
                                name="follower"
                                checked={followerPort === row.port}
                                onChange={() => setFollowerPort(row.port)}
                                disabled={active}
                              />
                            </td>
                            <td className="break-all p-2 font-mono text-slate-300">
                              {row.connected ? "" : "（不在线）"}
                              {row.port.replace("/dev/serial/by-id/", "")}
                            </td>
                            <td className="p-2 font-mono text-slate-400">
                              {row.serial || "—"} / {row.device || "—"}
                            </td>
                            <td className="p-2">
                              {row.binding ? (
                                <span
                                  className={`font-mono ${
                                    row.binding.role === "leader" ? "text-sky-300" : "text-purple-300"
                                  }`}
                                >
                                  {row.binding.role} · {row.binding.config} ·{" "}
                                  {SHORT(row.binding.content_sha256, 8)}
                                </span>
                              ) : (
                                <span className="text-slate-600">无记录</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <p className="text-[11px] text-slate-500">
                  绑定表：{devices?.bindings_file ?? "…"}（{devices?.note ?? ""}）
                </p>
              </CardContent>
            </Card>
          )}

          {step === 1 && (
            <Card className="border-slate-800 bg-slate-900/70">
              <CardHeader className="p-4 pb-2">
                <CardTitle className="text-base">标定</CardTitle>
                <CardDescription className="text-slate-400">
                  每台臂的标定文件按端口记录；换臂重新量一次，绑定就会更新到最新一次测量。
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4 p-4 pt-0">
                {(["leader", "follower"] as const).map((role) => (
                  <div key={role} className="space-y-2">
                    <div className="flex items-center justify-between">
                      <h3 className="text-sm font-medium text-slate-300">
                        {role === "leader" ? "主臂" : "从臂"}标定
                        <span className="ml-2 font-mono text-xs text-slate-500">
                          {role === "leader" ? SHORT(leaderPort, 22) : SHORT(followerPort, 22)}
                        </span>
                      </h3>
                      <Button
                        size="sm"
                        variant="outline"
                        className="border-gray-700 text-slate-300"
                        onClick={() =>
                          navigate("/calibration", { state: { robot_name: selectedName ?? undefined } })
                        }
                      >
                        <Wrench className="mr-1 h-4 w-4" /> 打开标定页
                      </Button>
                    </div>
                    {configOptions(role).length === 0 ? (
                      <p className="rounded border border-amber-700/60 bg-amber-950/30 p-2 text-xs text-amber-200">
                        这台主机没有可读的{role === "leader" ? "主臂" : "从臂"}标定文件。
                      </p>
                    ) : (
                      <div className="space-y-1">
                        {configOptions(role).map((row) => {
                          const selected = (role === "leader" ? leaderConfig : followerConfig) === row.config;
                          const boundHere = row.bound_ports.includes(
                            role === "leader" ? leaderPort : followerPort
                          );
                          return (
                            <button
                              key={row.config}
                              onClick={() =>
                                role === "leader" ? setLeaderConfig(row.config) : setFollowerConfig(row.config)
                              }
                              className={`w-full rounded border px-3 py-2 text-left text-xs transition-colors ${
                                selected
                                  ? "border-sky-600 bg-sky-950/50 text-slate-100"
                                  : "border-slate-800 bg-slate-950/40 text-slate-400 hover:border-slate-700"
                              }`}
                            >
                              <span className="font-mono text-slate-200">{row.config}</span>
                              <span className="ml-2">
                                {row.readable
                                  ? `${Object.keys(row.ids).length} 关节 · ${SHORT(row.content_sha256, 8)}`
                                  : "无法解析"}
                              </span>
                              <span className={`ml-2 ${boundHere ? "text-green-400" : "text-slate-500"}`}>
                                {boundHere
                                  ? "在当前选定的端口上量得"
                                  : row.bound_ports.length
                                    ? `量自 ${row.bound_ports.map((p) => SHORT(p, 12)).join(", ")}`
                                    : "无端口记录"}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          )}

          {step === 2 && (
            <Card className="border-slate-800 bg-slate-900/70">
              <CardHeader className="p-4 pb-2">
                <CardTitle className="text-base">遥操作</CardTitle>
                <CardDescription className="text-slate-400">
                  启动前由后端再次核查标定归属；核查不过就不会打开串口，也不会写舵机。
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3 p-4 pt-0">
                <div className="flex flex-wrap items-center gap-2">
                  {!active ? (
                    <>
                      <Button
                        onClick={() => startTeleoperation(true)}
                        disabled={!canStart || busy !== null}
                        variant="outline"
                        className="border-slate-600 text-slate-200"
                      >
                        <Eye className="mr-1 h-4 w-4" />
                        {busy === "启动预览" ? "启动中…" : "仅预览（不下发目标）"}
                      </Button>
                      <Button onClick={() => startTeleoperation(false)} disabled={!canStart || busy !== null} className="bg-green-600">
                        <Play className="mr-1 h-4 w-4" />
                        {busy === "启动遥操作" ? "启动中…" : "开始跟随"}
                      </Button>
                    </>
                  ) : (
                    <Button onClick={stopTeleoperation} disabled={busy !== null} className="bg-red-600">
                      <Square className="mr-1 h-4 w-4" />
                      {busy === "停止遥操作" ? "停止中…" : "停止遥操作"}
                    </Button>
                  )}
                  {active && sessionPreview && (
                    <p className="rounded border border-amber-700/70 bg-amber-950/40 px-2 py-1 text-amber-200">
                      预览中：主臂读数、映射目标与从臂实测照常刷新，但没有一个字节写到从臂总线，
                      从臂也未 configure（力矩保持关闭）。看清误差后再停止本会话，重新选"开始跟随"。
                    </p>
                  )}
                  {active && !sessionPreview && (
                    <>
                      {clutch?.engaged ? (
                        <Button
                          onClick={() => setClutchEngage(false)}
                          disabled={busy !== null}
                          variant="outline"
                          className="border-amber-600 text-amber-300"
                        >
                          断开离合（立刻停止下发目标）
                        </Button>
                      ) : (
                        <Button onClick={() => setClutchEngage(true)} disabled={busy !== null} className="bg-emerald-700">
                          {busy === "接合离合" ? "接合中…" : "接合离合（开始下发目标）"}
                        </Button>
                      )}
                      <span className="text-xs text-slate-400">
                        {clutch?.engaged
                          ? "已用接合那一帧的主臂读数与从臂实测建立参考，之后只下发相对变化"
                          : "会话在跑，但没有一个字节写到从臂；接合会取下一个实测帧作为参考"}
                        {clutch?.refusal ? ` · 上一次接合被拒：${clutch.refusal}` : ""}
                      </span>
                    </>
                  )}
                  {!canStart && !active && (
                    <span className="text-xs text-amber-400">
                      {blockedByIdentity
                        ? "标定归属核查不过：先改正主/从臂选择，或重新标定这台臂。"
                        : "先选定两台臂与各自的标定文件。"}
                    </span>
                  )}
                </div>

                {identity && identity.refusals.length > 0 && (
                  <div className="rounded border border-red-700/70 bg-red-950/40 p-3 text-xs text-red-200">
                    <p className="mb-1 flex items-center gap-1 font-medium">
                      <CircleAlert className="h-4 w-4" /> 启动会被拒绝
                    </p>
                    {identity.refusals.map((line) => (
                      <p key={line} className="break-words">
                        {line}
                      </p>
                    ))}
                  </div>
                )}
                {identity && identity.warnings.length > 0 && (
                  <div className="rounded border border-amber-700/60 bg-amber-950/30 p-3 text-xs text-amber-200">
                    {identity.warnings.map((line) => (
                      <p key={line} className="break-words">
                        {line}
                      </p>
                    ))}
                  </div>
                )}
                {error && (
                  <p className="rounded border border-red-700/70 bg-red-950/40 p-3 text-xs break-words text-red-200">
                    {error}
                  </p>
                )}

                <div className="grid gap-2 text-xs text-slate-400 sm:grid-cols-2">
                  <p>
                    控制权租约：
                    <span
                      className={`ml-1 font-mono ${
                        devices?.lease.state === "idle" ? "text-green-400" : "text-amber-300"
                      }`}
                    >
                      {devices?.lease.state ?? "…"}
                      {devices?.lease.task ? ` (${devices.lease.task})` : ""}
                      {devices?.lease.expires_in != null ? ` 剩余 ${devices.lease.expires_in}s` : ""}
                    </span>
                  </p>
                  <p>
                    会话中的臂：
                    <span className="ml-1 break-all font-mono text-slate-300">
                      {devices?.teleoperation?.session
                        ? `主 ${SHORT(devices.teleoperation.session.leader_port.replace("/dev/serial/by-id/", ""), 20)} / ` +
                          `从 ${SHORT(devices.teleoperation.session.follower_port.replace("/dev/serial/by-id/", ""), 20)}` +
                          ` · ${devices.teleoperation.session.leader_config}+${devices.teleoperation.session.follower_config}` +
                          (devices.teleoperation.session.preview ? " · 仅预览" : "")
                        : "无（未启动会话）"}
                    </span>
                  </p>
                  {devices?.teleoperation?.stop_reason && !active && (
                    <p className="rounded border border-red-700/70 bg-red-950/40 px-2 py-1 text-red-200">
                      上一次会话自行结束：{devices.teleoperation.stop_reason}
                    </p>
                  )}
                  <p>
                    两台臂在线：
                    <span className={`ml-1 font-mono ${bothConnected ? "text-green-400" : "text-amber-300"}`}>
                      {bothConnected ? "yes" : "no"}
                      {chosen.leader?.device ? ` 主=${chosen.leader.device}` : ""}
                      {chosen.follower?.device ? ` 从=${chosen.follower.device}` : ""}
                    </span>
                  </p>
                </div>
              </CardContent>
            </Card>
          )}

          {step === 3 && (
            <Card className="border-slate-800 bg-slate-900/70">
              <CardHeader className="p-4 pb-2">
                <CardTitle className="text-base">数据检查</CardTitle>
                <CardDescription className="text-slate-400">
                  采集与遥操作互斥：先停止跟随，再走 landing 的采集流程。
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3 p-4 pt-0">
                <div className="flex gap-2">
                  <Button variant="outline" className="border-gray-700 text-slate-300" onClick={() => navigate("/")}>
                    去采集配置（Landing）
                  </Button>
                  <Button variant="outline" className="border-gray-700 text-slate-300" onClick={() => navigate("/dataset-viewer")}>
                    打开数据检查页 <ArrowRight className="ml-1 h-4 w-4" />
                  </Button>
                </div>
                <LocalDatasets />
              </CardContent>
            </Card>
          )}

          <Card className="border-slate-800 bg-slate-900/70">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="text-base">当前配置</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1 p-4 pt-0 text-xs">
              {[
                ["主臂端口", leaderPort],
                ["主臂标定", leaderConfig],
                ["从臂端口", followerPort],
                ["从臂标定", followerConfig],
              ].map(([label, value]) => (
                <p key={label as string} className="flex gap-2">
                  <span className="w-20 shrink-0 text-slate-500">{label}</span>
                  <span className="break-all font-mono text-slate-300">{(value as string) || "—"}</span>
                </p>
              ))}
              {selectedRecord && (
                <p className="pt-1 text-slate-500">
                  来自配置 <span className="font-mono text-slate-400">{selectedRecord.name}</span>
                  {" · "}
                  {Object.entries(records).length > 0 ? `${Object.keys(records).length} 台已保存` : ""}
                </p>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <JointTrackPanel samples={samples} />
          <Card className="border-slate-800 bg-slate-900/70">
            <CardHeader className="p-4 pb-2">
              <CardTitle className="text-base">从臂姿态（URDF）</CardTitle>
              <CardDescription className="text-slate-400">
                与曲线同一条关节流；主臂读数只在曲线里显示，3D 始终画实测。
              </CardDescription>
            </CardHeader>
            <CardContent className="p-4 pt-0">
              <div className="h-72 overflow-hidden rounded border border-slate-800">
                <UrdfViewer />
              </div>
            </CardContent>
          </Card>
          <div className="h-80 overflow-hidden rounded-lg border border-slate-800">
            <TeleopCameraPanel />
          </div>
        </div>
      </main>
    </div>
  );
};

const LocalDatasets: React.FC = () => {
  const { baseUrl, fetchWithHeaders } = useApi();
  const query = useQuery({
    queryKey: ["so101-local-datasets", baseUrl],
    queryFn: ({ signal }) => listLocalDatasetsForViewer(baseUrl, fetchWithHeaders, signal),
    staleTime: 15_000,
  });

  if (query.isLoading) return <p className="text-xs text-slate-400">读取本机数据集…</p>;
  if (!query.data?.length) return <p className="text-xs text-slate-500">本机还没有数据集。</p>;
  return (
    <ul className="space-y-1 text-xs">
      {query.data.map((dataset) => (
        <li key={dataset.repo_id} className="flex items-center justify-between gap-2 rounded border border-slate-800 bg-slate-950/40 px-2 py-1">
          <span className="truncate font-mono text-slate-300">{dataset.repo_id}</span>
          <span className="shrink-0 text-slate-400">
            {dataset.total_episodes ?? "?"} 集 / {dataset.total_frames ?? "?"} 帧
            {dataset.fps ? ` · ${dataset.fps} fps` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
};

export default So101Workbench;
