import React, { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AlertTriangle, CheckCircle2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import LandingTopBar from "@/components/landing/LandingTopBar";
import CameraConfiguration, { CameraConfig } from "@/components/recording/CameraConfiguration";
import { useApi } from "@/contexts/ApiContext";
import { useRobots } from "@/hooks/useRobots";
import { useToast } from "@/hooks/use-toast";
import {
  fetchTeleopReadiness,
  TEST_STATE_CLASS,
  TEST_STATE_LABEL,
  type TeleopReadinessResponse,
} from "@/lib/teleopReadiness";
import {
  fetchCameraBinding,
  type CameraBindingResponse,
} from "@/lib/cameraBinding";
import { deviceStatuses, STATE_CLASS, STATE_LABEL } from "@/lib/robotDevices";

const PROFILE_KEY = "lelab.localBimanualProfile.v2";

type ArmPair = {
  leader_port: string;
  follower_port: string;
  leader_config: string;
  follower_config: string;
};

type ValidationDevice = {
  side: string;
  role: string;
  port: string;
  calibration: { file: string; sha256: string; role?: string; side?: string };
};

type ValidationResult = {
  success: boolean;
  error?: string;
  warnings?: string[];
  rig?: { devices: Record<string, ValidationDevice> };
  cameras?: Array<{ name: string; camera_index?: number }>;
};

const readProfile = () => {
  try {
    return JSON.parse(localStorage.getItem(PROFILE_KEY) || "{}");
  } catch {
    return {};
  }
};

type StepState = "done" | "warn" | "todo";

const STEP_CHIP_CLASS: Record<StepState, string> = {
  done: "border-emerald-800 bg-emerald-950/40 text-emerald-200",
  warn: "border-amber-800 bg-amber-950/40 text-amber-200",
  todo: "border-gray-700 bg-gray-900 text-gray-400",
};

const FlowProgress: React.FC<{ steps: { label: string; state: StepState; note: string }[] }> = ({
  steps,
}) => (
  <ol className="flex flex-wrap gap-2">
    {steps.map((step, index) => (
      <li
        key={step.label}
        className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-xs ${STEP_CHIP_CLASS[step.state]}`}
      >
        <span className="font-mono opacity-70">{index + 1}</span>
        <span className="font-medium">{step.label}</span>
        <span className="opacity-80">{step.note}</span>
      </li>
    ))}
  </ol>
);

const StepHeading: React.FC<{ index: number; title: string; state: StepState; note?: string }> = ({
  index,
  title,
  state,
  note,
}) => (
  <div className="flex flex-wrap items-center gap-2">
    <span
      className={`flex h-6 w-6 items-center justify-center rounded-full border text-xs font-mono ${STEP_CHIP_CLASS[state]}`}
    >
      {index}
    </span>
    <h2 className="font-semibold text-gray-100">{title}</h2>
    {note ? <span className="text-xs text-gray-500">{note}</span> : null}
  </div>
);

const pairFromRecord = (record: {
  leader_port: string;
  follower_port: string;
  leader_config: string;
  follower_config: string;
}): ArmPair => ({
  leader_port: record.leader_port,
  follower_port: record.follower_port,
  leader_config: record.leader_config,
  follower_config: record.follower_config,
});

const BimanualRecording: React.FC = () => {
  const { records, availableNames, isLoading } = useRobots();
  const { baseUrl, fetchWithHeaders } = useApi();
  const navigate = useNavigate();
  const { toast } = useToast();
  const profile = readProfile();

  const [leftName, setLeftName] = useState<string>(profile.left || "");
  const [rightName, setRightName] = useState<string>(profile.right || "");
  const [cameras, setCameras] = useState<CameraConfig[]>(profile.cameras || []);
  const [showCameras, setShowCameras] = useState(false);
  const [dataset, setDataset] = useState(profile.dataset || "so101_bimanual");
  const [task, setTask] = useState(profile.task || "");
  const [episodes, setEpisodes] = useState(profile.episodes || 5);
  const [duration, setDuration] = useState(profile.duration || 60);
  const [fps, setFps] = useState(profile.fps || 30);
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [readiness, setReadiness] = useState<TeleopReadinessResponse | null>(null);
  const [binding, setBinding] = useState<CameraBindingResponse | null>(null);
  const [validating, setValidating] = useState(false);
  const [starting, setStarting] = useState(false);
  const releaseStreamsRef = useRef<(() => void) | null>(null);

  const left = records[leftName];
  const right = records[rightName];
  const rig =
    left && right && leftName && rightName && leftName !== rightName
      ? { left: pairFromRecord(left), right: pairFromRecord(right) }
      : null;

  useEffect(() => {
    localStorage.setItem(
      PROFILE_KEY,
      JSON.stringify({
        left: leftName,
        right: rightName,
        cameras,
        dataset,
        task,
        episodes,
        duration,
        fps,
      })
    );
  }, [leftName, rightName, cameras, dataset, task, episodes, duration, fps]);

  useEffect(() => () => releaseStreamsRef.current?.(), []);

  const loadReadiness = useCallback(async () => {
    const [teleop, cameras] = await Promise.all([
      fetchTeleopReadiness(fetchWithHeaders, baseUrl),
      fetchCameraBinding(fetchWithHeaders, baseUrl),
    ]);
    if (teleop) setReadiness(teleop);
    if (cameras) setBinding(cameras);
  }, [baseUrl, fetchWithHeaders]);

  useEffect(() => {
    void loadReadiness();
  }, [loadReadiness]);

  const cameraPayload = useCallback(
    () =>
      Object.fromEntries(
        cameras.map((camera) => [
          camera.name,
          {
            type: camera.type,
            camera_index: camera.camera_index,
            width: camera.width,
            height: camera.height,
            fps: camera.fps,
            backend: camera.backend,
            fourcc: camera.fourcc,
          },
        ])
      ),
    [cameras]
  );

  const validateRig = useCallback(async (): Promise<ValidationResult | null> => {
    if (!rig) return null;
    setValidating(true);
    try {
      const response = await fetchWithHeaders(`${baseUrl}/bimanual/validate`, {
        method: "POST",
        body: JSON.stringify({ rig, cameras: cameraPayload() }),
      });
      const data = (await response.json()) as ValidationResult;
      setValidation(data);
      return data;
    } catch (error) {
      toast({
        title: "核验失败",
        description: `无法连接后端：${String(error)}`,
        variant: "destructive",
      });
      return null;
    } finally {
      setValidating(false);
    }
  }, [baseUrl, cameraPayload, fetchWithHeaders, rig, toast]);

  useEffect(() => {
    setValidation(null);
  }, [leftName, rightName, cameras]);

  const cameraNames = cameras.map((camera) => camera.name);
  const duplicateCameraName = new Set(cameraNames).size !== cameraNames.length;
  const bindingMissing = binding?.missing ?? [];
  const basicReady =
    Boolean(rig) &&
    Boolean(task.trim()) &&
    Boolean(dataset.trim()) &&
    cameras.length > 0 &&
    !duplicateCameraName &&
    !bindingMissing.length;

  const groups = (["left", "right"] as const).map((side) => ({
    side,
    name: side === "left" ? leftName : rightName,
    devices: deviceStatuses(side === "left" ? left : right),
  }));
  const bothGroupsSelected = Boolean(leftName && rightName && leftName !== rightName);
  const allCalibrated = groups.every((group) =>
    group.devices.every((device) => device.state === "calibrated")
  );
  const testsPassed = Boolean(
    readiness &&
      (["left", "right"] as const).every(
        (side) => readiness.sides[side]?.test.state === "passed"
      )
  );
  const steps: { label: string; state: StepState; note: string }[] = [
    {
      label: "设备与标定",
      state: !bothGroupsSelected ? "todo" : allCalibrated ? "done" : "warn",
      note: !bothGroupsSelected
        ? "未选择两组"
        : allCalibrated
          ? "四台都有标定文件"
          : "有设备缺标定或端口未接",
    },
    {
      label: "单臂遥操作测试",
      state: testsPassed ? "done" : "warn",
      note: testsPassed ? "两组均已确认通过" : "逐组测试并由操作员确认",
    },
    {
      label: "相机与画面确认",
      state: binding?.ready ? "done" : binding?.cameras.length ? "warn" : "todo",
      note: binding?.ready
        ? `${binding.cameras.length} 路已确认画面`
        : binding?.cameras.length
          ? "有路尚未确认画面"
          : "尚无已映射相机",
    },
    {
      label: "采集配置",
      state: basicReady ? "done" : "todo",
      note: basicReady ? "可以开始采集" : "补齐任务与相机列表",
    },
  ];

  // Turn the confirmed /cameras mapping into the recorder's camera list. The
  // stable by-id link is kept as device_id, so an index drift shows up as a
  // missing device instead of silently recording another camera position.
  const applyBinding = () => {
    const rows = binding?.cameras ?? [];
    setCameras(
      rows.map((row) => ({
        id: row.id,
        name: row.camera_key,
        type: "opencv",
        camera_index: row.index ?? undefined,
        device_id: row.device ?? row.id,
        width: 640,
        height: 480,
        fps: 30,
      }))
    );
    setShowCameras(true);
  };

  const start = async () => {
    if (!rig || !basicReady) return;
    if (duplicateCameraName) {
      toast({
        title: "相机名称重复",
        description: "每路相机必须使用唯一名称（例如 front、left_wrist、right_wrist）。",
        variant: "destructive",
      });
      return;
    }
    setStarting(true);
    try {
      const checked = await validateRig();
      if (!checked?.success) {
        toast({
          title: "配置未通过核验",
          description: checked?.error || "四个设备、标定文件或相机绑定不完整。",
          variant: "destructive",
        });
        return;
      }
      releaseStreamsRef.current?.();
      const recordingConfig = {
        bimanual: rig,
        dataset_repo_id: `local/${dataset.trim()}`,
        single_task: task.trim(),
        num_episodes: episodes,
        episode_time_s: duration,
        reset_time_s: 10,
        fps,
        video: true,
        push_to_hub: false,
        resume: false,
        streaming_encoding: true,
        cameras: cameraPayload(),
      };
      const response = await fetchWithHeaders(`${baseUrl}/start-recording`, {
        method: "POST",
        body: JSON.stringify(recordingConfig),
      });
      const data = await response.json();
      if (!response.ok || data.success === false) {
        toast({
          title: "无法开始双臂采集",
          description: data.detail || data.message || "后端拒绝了采集请求。",
          variant: "destructive",
        });
        return;
      }
      navigate("/recording", {
        state: { recordingConfig, sessionStarted: true },
      });
    } catch (error) {
      toast({
        title: "启动失败",
        description: `无法连接后端：${String(error)}`,
        variant: "destructive",
      });
    } finally {
      setStarting(false);
    }
  };

  const devices = validation?.rig?.devices ? Object.values(validation.rig.devices) : [];

  return (
    <div className="min-h-screen bg-black text-white">
      <LandingTopBar />
      <main className="mx-auto max-w-6xl space-y-6 p-6">
        <div>
          <Link to="/" className="text-sm text-gray-400 hover:text-white">
            ← 返回工作台
          </Link>
          <h1 className="mt-4 text-2xl font-semibold">本机双臂采集</h1>
          <p className="mt-2 max-w-3xl text-gray-400">
            选择左右两组已保存的 leader/follower 设备。四台机械臂各自使用本地标定文件；
            采集时左右臂与相机写入同一个 LeRobot 数据集。页面加载不会连接机械臂或打开相机。
          </p>
        </div>

        <FlowProgress steps={steps} />

        <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-900 p-5">
          <StepHeading
            index={1}
            title="设备与标定"
            state={steps[0].state}
            note="示教臂与工作臂分列；标定文件存在不等于实机可用"
          />
          <div className="grid gap-4 md:grid-cols-2">
            {groups.map((group) => {
              const setter = group.side === "left" ? setLeftName : setRightName;
              return (
                <div
                  key={group.side}
                  className="space-y-3 rounded-lg border border-gray-800 bg-black p-4"
                >
                  <Label htmlFor={`${group.side}-pair`}>
                    {group.side === "left" ? "左侧机械臂组" : "右侧机械臂组"}
                  </Label>
                  <select
                    id={`${group.side}-pair`}
                    value={group.name}
                    disabled={isLoading}
                    onChange={(event) => setter(event.target.value)}
                    className="w-full rounded-md border border-gray-600 bg-gray-800 p-2 text-white"
                  >
                    <option value="">选择已保存的机械臂组</option>
                    {availableNames.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                  {group.name ? (
                    <div className="space-y-2">
                      {group.devices.map((device) => (
                        <div
                          key={device.kind}
                          className="rounded border border-gray-800 bg-gray-950 p-2 text-xs"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-sm text-gray-200">
                              {group.side === "left" ? "左" : "右"}
                              {device.label}
                            </span>
                            <span className={STATE_CLASS[device.state]}>
                              {STATE_LABEL[device.state]}
                              {device.state === "calibrated" ? " · 待实机测试" : ""}
                            </span>
                          </div>
                          <div className="mt-1 break-all font-mono text-gray-500">
                            {device.port || "端口未配置"}
                          </div>
                          <div className="mt-1 break-all font-mono text-gray-500">
                            标定：{device.calibrationName || "未绑定"}
                            {device.calibrationExists ? null : " · 文件不存在"}
                          </div>
                          {device.issues.map((issue) => (
                            <div key={issue} className="mt-1 text-amber-300">
                              {issue}
                            </div>
                          ))}
                          <Button
                            size="sm"
                            variant="outline"
                            className="mt-2"
                            onClick={() =>
                              navigate("/calibration", {
                                state: {
                                  robot_name: group.name,
                                  device_type: device.kind === "leader" ? "teleop" : "robot",
                                  port: device.port,
                                  hardware_role: `${group.side}_${device.kind}`,
                                },
                              })
                            }
                          >
                            {device.calibrationExists ? "查看 / 重新标定" : "开始标定"}
                          </Button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-gray-500">尚未选择机械臂组。</p>
                  )}
                </div>
              );
            })}
          </div>
          {leftName && rightName && leftName === rightName ? (
            <p className="text-amber-300" role="alert">
              左右两组不能选择同一条设备记录。
            </p>
          ) : null}
        </section>

        <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-900 p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <StepHeading
              index={2}
              title="单臂遥操作测试"
              state={steps[1].state}
              note="每次只连一组，示教臂带动工作臂"
            />
            <Button variant="outline" size="sm" onClick={() => void loadReadiness()}>
              <RefreshCw className="mr-1 h-4 w-4" />
              刷新就绪状态
            </Button>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            {(["left", "right"] as const).map((side) => {
              const group = readiness?.sides[side];
              const expected = side === "left" ? leftName : rightName;
              return (
                <div
                  key={side}
                  className="rounded-lg border border-gray-800 bg-black p-3 text-sm"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-gray-300">{side === "left" ? "左组" : "右组"}</span>
                    <span className={group ? TEST_STATE_CLASS[group.test.state] : "text-gray-500"}>
                      {group ? TEST_STATE_LABEL[group.test.state] : "读取中…"}
                    </span>
                  </div>
                  <div className="mt-1 break-words font-mono text-xs text-gray-500">
                    {group?.record ?? "—"} ·{" "}
                    {group?.available
                      ? "记录含两台标定"
                      : group?.reason ?? "正在读取设备记录…"}
                  </div>
                  {group?.test.entry ? (
                    <div className="mt-1 text-xs text-gray-500">
                      {group.test.entry.recorded_at} · {group.test.entry.note || "无备注"} ·{" "}
                      {group.test.entry.summary
                        ? `${group.test.entry.summary.steps} 步 / 限幅 ${group.test.entry.summary.clamped_steps}`
                        : "无遥测摘要"}
                    </div>
                  ) : null}
                  {group?.test.reason ? (
                    <div className="mt-1 text-xs text-amber-300">{group.test.reason}</div>
                  ) : null}
                  {expected && group && expected !== group.record ? (
                    <div className="mt-1 text-xs text-amber-300">
                      本结论属于 {group.record}，与上方所选的 {expected} 不是同一条记录。
                    </div>
                  ) : null}
                  <Button
                    variant="ghost"
                    size="sm"
                    className="mt-2 px-0 text-blue-300 hover:bg-transparent hover:text-blue-200"
                    onClick={() => navigate(`/teleop-test?side=${side}`)}
                  >
                    打开该组遥操作测试 →
                  </Button>
                </div>
              );
            })}
          </div>
          <p className="text-xs text-gray-500">
            两组都确认通过后这一步才变绿；端口、标定文件或生效限幅一变，结论自动变成“已过期”。
            未通过也可以继续采集，但示范质量没有实机验证支撑。
          </p>
        </section>

        <section className="space-y-4 rounded-xl border border-gray-700 bg-gray-900 p-5">
          <StepHeading
            index={3}
            title="相机与画面确认"
            state={steps[2].state}
            note="逐台确认画面归属后生成采集相机；名称不会烧进视频"
          />
          <p className="text-sm text-gray-400">
            相机名称必须唯一（建议 front、left_wrist、right_wrist）。只有点击下方按钮才会扫描和预览设备。
          </p>
          <div className="rounded-lg border border-gray-800 bg-black p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm text-gray-300">
                物理相机 → 数据字段 对照（在 /cameras 逐台确认画面后保存）
              </div>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => void loadReadiness()}>
                  <RefreshCw className="mr-1 h-4 w-4" />
                  刷新映射
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={applyBinding}
                  disabled={!binding?.cameras.length}
                >
                  用确认结果生成采集相机
                </Button>
              </div>
            </div>
            <table className="mt-2 w-full text-xs">
              <thead className="text-left text-gray-500">
                <tr>
                  <th className="py-1">显示名称</th>
                  <th className="py-1">数据字段</th>
                  <th className="py-1">稳定设备</th>
                  <th className="py-1">状态</th>
                </tr>
              </thead>
              <tbody className="align-top font-mono text-gray-300">
                {(binding?.cameras ?? []).map((row) => (
                  <tr key={row.id} className="border-t border-gray-900">
                    <td className="py-1 font-sans">{row.label}</td>
                    <td className="py-1">observation.images.{row.camera_key}</td>
                    <td className="py-1 break-all text-gray-500">{row.source}</td>
                    <td
                      className={`py-1 font-sans ${
                        row.ready ? "text-emerald-300" : "text-amber-300"
                      }`}
                    >
                      {row.ready ? "已确认画面" : "画面未确认"}
                    </td>
                  </tr>
                ))}
                {bindingMissing.map((row) => (
                  <tr key={row.id} className="border-t border-gray-900 text-red-300">
                    <td className="py-1 font-sans">{row.label}</td>
                    <td className="py-1">observation.images.{row.camera_key}</td>
                    <td className="py-1 break-all">{row.id}</td>
                    <td className="py-1 font-sans">设备未连接，已阻止开始采集</td>
                  </tr>
                ))}
                {!binding?.cameras.length && !bindingMissing.length ? (
                  <tr>
                    <td colSpan={4} className="py-2 text-gray-500">
                      尚无已映射的相机；可在 /cameras 逐台确认，也可在下方手动配置。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
          {showCameras ? (
            <CameraConfiguration
              cameras={cameras}
              onCamerasChange={setCameras}
              releaseStreamsRef={releaseStreamsRef}
            />
          ) : (
            <Button variant="outline" onClick={() => setShowCameras(true)}>
              配置并预览相机（已保存 {cameras.length} 路）
            </Button>
          )}
        </section>
        <section className="space-y-3 rounded-xl border border-gray-700 bg-gray-900 p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <StepHeading
              index={4}
              title="采集配置与核验"
              state={steps[3].state}
              note="只读核验四个串口与标定文件，不连接设备"
            />
            <Button variant="outline" onClick={validateRig} disabled={!rig || validating}>
              <RefreshCw className={`mr-2 h-4 w-4 ${validating ? "animate-spin" : ""}`} />
              核验配置
            </Button>
          </div>
          {validation && !validation.success ? (
            <div className="flex items-start gap-2 rounded-lg border border-red-700 bg-red-950/40 p-3 text-red-200">
              <AlertTriangle className="mt-0.5 h-4 w-4" />
              <div>
                <p className="font-medium">配置错误</p>
                <p className="text-sm">{validation.error}</p>
              </div>
            </div>
          ) : null}
          {validation?.success ? (
            <div className="grid gap-3 md:grid-cols-2">
              {devices.map((device) => (
                <div
                  key={`${device.side}-${device.role}`}
                  className="rounded-lg border border-gray-700 bg-gray-950 p-3 text-sm"
                >
                  <p className="flex items-center gap-2 font-medium text-green-300">
                    <CheckCircle2 className="h-4 w-4" />
                    {device.side === "left" ? "左" : "右"}
                    {device.role === "leader" ? "示教臂" : "工作臂"}
                  </p>
                  <p className="text-gray-400">端口：{device.port}</p>
                  <p className="break-all text-gray-400">标定：{device.calibration.file}</p>
                  <p className="font-mono text-xs text-gray-500">
                    sha256 {device.calibration.sha256.slice(0, 16)}…
                  </p>
                </div>
              ))}
            </div>
          ) : null}
          {validation?.warnings?.length ? (
            <ul className="list-disc pl-5 text-sm text-amber-300">
              {validation.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
        </section>

        <section className="grid gap-4 rounded-xl border border-gray-700 bg-gray-900 p-5 md:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="bi-dataset">数据集名称</Label>
            <Input
              id="bi-dataset"
              className="border-gray-600 bg-gray-800 text-white"
              value={dataset}
              onChange={(event) =>
                setDataset(event.target.value.replace(/[^A-Za-z0-9._-]/g, "_"))
              }
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-task">任务描述</Label>
            <Input
              id="bi-task"
              className="border-gray-600 bg-gray-800 text-white"
              value={task}
              onChange={(event) => setTask(event.target.value)}
              placeholder="例如：左右臂协作折叠毛巾"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-episodes">示范集数</Label>
            <Input
              id="bi-episodes"
              type="number"
              min={1}
              className="border-gray-600 bg-gray-800 text-white"
              value={episodes}
              onChange={(event) => setEpisodes(Math.max(1, Number(event.target.value)))}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-duration">每集最长秒数</Label>
            <Input
              id="bi-duration"
              type="number"
              min={1}
              className="border-gray-600 bg-gray-800 text-white"
              value={duration}
              onChange={(event) => setDuration(Math.max(1, Number(event.target.value)))}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="bi-fps">目标采集帧率</Label>
            <Input
              id="bi-fps"
              type="number"
              min={1}
              max={120}
              className="border-gray-600 bg-gray-800 text-white"
              value={fps}
              onChange={(event) =>
                setFps(Math.min(120, Math.max(1, Number(event.target.value))))
              }
            />
          </div>
          <p className="self-end text-sm text-gray-400">
            到时自动保存并进入复位；也可保存本集、丢弃重录或结束并保存。采集阶段不加载策略模型。
            左右串口按左后右顺序读写，属于软件同步，不是硬件级同时采样。
          </p>
        </section>


        {!basicReady ? (
          <p className="text-amber-300" role="status">
            选择左右两组不同设备记录、填写任务并配置至少一路唯一名称的相机后才能开始。
            {bindingMissing.length ? " 已映射的相机当前未连接，请先接回设备或去掉该映射。" : ""}
          </p>
        ) : null}

        <Button
          size="lg"
          onClick={start}
          disabled={!basicReady || starting || validating}
          className="w-full bg-green-600 py-6 text-lg font-semibold text-white hover:bg-green-700"
        >
          {starting ? "正在连接并开始采集…" : "开始双臂采集"}
        </Button>
      </main>
    </div>
  );
};

export default BimanualRecording;
